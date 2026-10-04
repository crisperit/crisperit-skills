#!/usr/bin/env python3
"""HTTP client for an OpenAI-compatible chat completions endpoint: retries, auth, the
tool-calling loop and usage/cost accounting. The one place cw_run.py and cw_mcp.py talk to a
model backend.

Stdlib only, no network except the configured profile's base_url.
"""

import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_store  # noqa: E402

BACKOFF_S = [1.0, 4.0]
RETRY_AFTER_MAX_S = 60
ATTEMPTS = 3

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_CONTEXT_MARKERS = ("context_length_exceeded", "context length", "maximum context")
_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_semaphore_lock = threading.Lock()
_semaphore = threading.BoundedSemaphore(4)


def configure(max_concurrency):
    """Replace the module semaphore. Call before any chat()."""
    global _semaphore
    with _semaphore_lock:
        _semaphore = threading.BoundedSemaphore(max_concurrency)


def _current_semaphore():
    with _semaphore_lock:
        return _semaphore


class Done:
    def __init__(self, value):
        self.value = value


class LLMError(cw_store.CWError):
    def __init__(self, message, remedy=None, *, kind="error", status=None):
        super().__init__(message, remedy)
        self.kind = kind
        self.status = status


def _expand_env(value):
    def _sub(match):
        name = match.group(1)
        if name not in os.environ:
            raise LLMError(
                f"{name} is not set", kind="config",
                remedy="export VAR in the shell that starts your agent, then cw_mcp.py stop",
            )
        return os.environ[name]

    return _ENV_VAR_RE.sub(_sub, value)


def _retry_sleep(headers, attempt):
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        try:
            seconds = int(retry_after)
        except ValueError:
            seconds = None
        if seconds is not None:
            return min(seconds, RETRY_AFTER_MAX_S)
    return BACKOFF_S[attempt]


def _finish(status, raw, profile, body):
    base_url = profile["base_url"].rstrip("/")
    text_body = raw.decode("utf-8", "replace")

    if status in (401, 403):
        raise LLMError(
            f"HTTP {status} from {base_url}", kind="auth", status=status,
            remedy=f"set {profile.get('api_key_env')} in the shell that starts your agent, then cw_mcp.py stop",
        )
    if status == 404:
        raise LLMError(
            f"HTTP 404 from {base_url}", kind="notfound", status=404,
            remedy=f"check model for profile {profile.get('name')}",
        )
    if status == 400:
        lowered = text_body.lower()
        if any(marker in lowered for marker in _CONTEXT_MARKERS):
            raise LLMError(
                f"context length exceeded for {profile.get('model')}", kind="context", status=400,
                remedy=f"this batch is too big for {profile.get('model')}: point the role at a larger-context model",
            )
        raise LLMError(text_body[:300], kind="bad_request", status=400)
    if status != 200:
        raise LLMError(f"HTTP {status} from {base_url}", kind="protocol", status=status)

    try:
        parsed = json.loads(text_body)
        choices = parsed["choices"]
        message_raw = choices[0]["message"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise LLMError("malformed reply: no choices", kind="protocol") from e

    message = {
        "role": message_raw.get("role"),
        "content": message_raw.get("content"),
        "tool_calls": message_raw.get("tool_calls"),
    }

    usage_raw = parsed.get("usage")
    if usage_raw:
        usage = {
            "prompt_tokens": usage_raw.get("prompt_tokens", 0),
            "completion_tokens": usage_raw.get("completion_tokens", 0),
            "estimated": False,
        }
    else:
        # ponytail: chars/4 estimate, not a real tokenizer; upgrade if accuracy on
        # usage-omitting providers matters.
        usage = {
            "prompt_tokens": len(json.dumps(body)) // 4,
            "completion_tokens": len(json.dumps(message)) // 4,
            "estimated": True,
        }
    return message, usage


def chat(profile, messages, tools=None, *, timeout=300, stream=False, on_chunk=None):
    if stream:
        raise NotImplementedError("streaming is phase 7")

    base_url = profile["base_url"].rstrip("/")
    url = f"{base_url}/chat/completions"
    headers = {"Content-Type": "application/json"}

    api_key_env = profile.get("api_key_env")
    if api_key_env:
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise LLMError(
                f"{api_key_env} is not set", kind="auth",
                remedy=f"set {api_key_env} in the shell that starts your agent, then cw_mcp.py stop",
            )
        headers["Authorization"] = f"Bearer {api_key}"

    for key, value in (profile.get("headers") or {}).items():
        headers[key] = _expand_env(value)

    body = {"model": profile["model"], "messages": messages}
    if tools is not None:
        body["tools"] = tools
    payload = json.dumps(body).encode()

    for attempt in range(ATTEMPTS):
        sem = _current_semaphore()
        sem.acquire()
        status = None
        raw = b""
        resp_headers = {}
        try:
            request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    status = response.status
                    raw = response.read()
                    resp_headers = dict(response.headers)
            except urllib.error.HTTPError as e:
                status = e.code
                raw = e.read()
                resp_headers = dict(e.headers or {})
        except socket.timeout as e:
            raise LLMError(
                str(e), kind="timeout",
                remedy=f"raise timeout_s in config.json or check {base_url}",
            ) from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, socket.timeout):
                raise LLMError(
                    str(e), kind="timeout",
                    remedy=f"raise timeout_s in config.json or check {base_url}",
                ) from e
            raise LLMError(str(e), kind="connect", remedy=f"nothing listening at {base_url}") from e
        except ConnectionRefusedError as e:
            raise LLMError(str(e), kind="connect", remedy=f"nothing listening at {base_url}") from e
        finally:
            sem.release()

        if status not in _RETRY_STATUSES:
            return _finish(status, raw, profile, body)

        if attempt == ATTEMPTS - 1:
            kind = "rate" if status == 429 else "server"
            raise LLMError(f"HTTP {status} from {base_url} after {ATTEMPTS} attempts", kind=kind, status=status)

        time.sleep(_retry_sleep(resp_headers, attempt))

    raise AssertionError("unreachable")  # pragma: no cover


def run_tools(profile, messages, tools, handlers, *, max_rounds=10, max_tokens=200000,
              timeout=300, on_usage=None, nudge="Reply only by calling one of the tools."):
    total_tokens = 0
    for _round in range(max_rounds):
        message, usage = chat(profile, messages, tools, timeout=timeout)
        if on_usage:
            on_usage(usage)
        messages.append(message)

        total_tokens += usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        if total_tokens > max_tokens:
            raise LLMError(
                "conversation exceeded max_conversation_tokens", kind="tokens",
                remedy="raise max_conversation_tokens in config.json",
            )

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            if nudge is None:
                return message.get("content") or ""
            messages.append({"role": "user", "content": nudge})
            continue

        for call in tool_calls:
            call_id = call.get("id")
            name = call["function"]["name"]
            arguments_raw = call["function"].get("arguments") or "{}"
            try:
                arguments = json.loads(arguments_raw)
            except ValueError as e:
                messages.append({"role": "tool", "tool_call_id": call_id,
                                  "content": f"invalid JSON arguments: {e}"})
                continue

            handler = handlers.get(name)
            if handler is None:
                messages.append({"role": "tool", "tool_call_id": call_id,
                                  "content": f"unknown tool {name}"})
                continue

            result = handler(arguments)
            if isinstance(result, Done):
                messages.append({"role": "tool", "tool_call_id": call_id, "content": "accepted"})
                return result.value
            messages.append({"role": "tool", "tool_call_id": call_id, "content": result})

    raise LLMError("tool loop exceeded max_rounds", kind="rounds")


def cost_usd(profile, usage):
    pricing = profile.get("price_per_mtok")
    if not pricing:
        return None
    prompt_cost = usage.get("prompt_tokens", 0) * pricing.get("input", 0)
    completion_cost = usage.get("completion_tokens", 0) * pricing.get("output", 0)
    return (prompt_cost + completion_cost) / 1e6


def check(profile, *, timeout=60):
    messages = [{"role": "user", "content": "Call the ping tool now."}]
    tools = [{"type": "function", "function": {"name": "ping", "description": "ping",
                                                "parameters": {"type": "object", "properties": {}}}}]
    message, _usage = chat(profile, messages, tools, timeout=timeout)
    if not message.get("tool_calls"):
        raise LLMError(
            "model did not reply with a tool call", kind="no_tools",
            remedy="point this role at a model that supports tool calls",
        )
