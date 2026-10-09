#!/usr/bin/env python3
"""HTTP client for an OpenAI-compatible chat completions endpoint: retries, auth, the
tool-calling loop and usage/cost accounting. The one place cw_run.py and cw_mcp.py talk to a
model backend.

Stdlib only, no network except the configured profile's base_url.
"""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
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

CLAUDE_TOOLS = {"read_file": "Read", "grep": "Grep", "list_dir": "Glob"}
_CLAUDE_ENV_DROP = (
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID", "CLAUDE_AGENT_SDK_VERSION",
)
_CLAUDE_NOT_ON_PATH_REMEDY = (
    "install Claude Code and log in, or point the role at an openai profile, then cw_mcp.py stop"
)
_CLAUDE_AUTH_REMEDY = "run `claude` in a terminal and log in (`claude auth login`)"
_CLAUDE_RATE_REMEDY = "wait for the reset the message names, or point the role at another profile"
_CLAUDE_AUTH_MARKERS = ("not logged in", "unauthorized", "authentication required")
_CLAUDE_RATE_MARKERS = ("usage limit", "rate limit", "rate_limit")
_CLAUDE_CONTEXT_MARKERS = ("prompt is too long", "context length")

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


def claude_env():
    return {k: v for k, v in os.environ.items() if k not in _CLAUDE_ENV_DROP}


def _classify_claude_error(parsed, stderr, profile):
    status = parsed.get("api_error_status")
    text = f"{parsed.get('result') or ''} {stderr or ''}".lower()

    if status in (401, 403) or any(marker in text for marker in _CLAUDE_AUTH_MARKERS):
        return "auth", _CLAUDE_AUTH_REMEDY
    if status == 404 or "unrecognized_model" in (stderr or ""):
        return "notfound", f"check model for profile {profile.get('name')}"
    if status == 429 or any(marker in text for marker in _CLAUDE_RATE_MARKERS):
        return "rate", _CLAUDE_RATE_REMEDY
    if any(marker in text for marker in _CLAUDE_CONTEXT_MARKERS):
        return "context", f"this batch is too big for {profile.get('model')}: lower batch_max_lines"
    if parsed.get("subtype") == "error_max_turns":
        return "rounds", None
    return "error", None


def claude_usage(parsed):
    usage_raw = parsed.get("usage") or {}
    return {
        "prompt_tokens": (usage_raw.get("input_tokens", 0) + usage_raw.get("cache_creation_input_tokens", 0)
                           + usage_raw.get("cache_read_input_tokens", 0)),
        "completion_tokens": usage_raw.get("output_tokens", 0),
        "estimated": False,
    }


def claude_call(profile, system, prompt, *, cwd, tools, schema=None, timeout=300):
    if not cwd or not Path(cwd).is_dir():
        raise LLMError(
            "cwd is not a directory", kind="config",
            remedy="re-run /code-walkthrough on the same target",
        )

    argv = [
        "claude", "-p", "--safe-mode", "--restricted", f"--tools={tools}",
        "--strict-mcp-config", "--no-session-persistence", "--permission-prompts", "none",
        f"--model={profile['model']}", "--output-format", "json", f"--system-prompt={system}",
    ]
    if schema:
        argv.append(f"--json-schema={json.dumps(schema)}")

    sem = _current_semaphore()
    sem.acquire()
    try:
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=claude_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True,
            )
        except FileNotFoundError as e:
            raise LLMError(
                "claude not on PATH", kind="config", remedy=_CLAUDE_NOT_ON_PATH_REMEDY,
            ) from e
        try:
            out, err = proc.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise LLMError(
                "claude process timed out", kind="timeout",
                remedy="raise timeout_s in config.json",
            ) from e
    finally:
        sem.release()

    try:
        parsed = json.loads(out)
    except ValueError as e:
        error = LLMError((err or out)[-300:], kind="protocol")
        error.usage = None
        raise error from e

    usage = claude_usage(parsed)

    if parsed.get("is_error") or proc.returncode != 0:
        kind, remedy = _classify_claude_error(parsed, err, profile)
        message = (parsed.get("result") or err or "")[:300]
        error = LLMError(message, kind=kind, remedy=remedy)
        error.usage = usage
        raise error

    return parsed, usage


def thread_argv(profile, system, *, mcp_config, add_dir, session_id=None, resume=None):
    # --safe-mode disables MCP servers; CLAUDE.md verified not loaded under --restricted.
    argv = [
        "claude", "-p", "--restricted", "--tools=Read,Grep,Glob", "--strict-mcp-config",
        f"--mcp-config={json.dumps(mcp_config)}", "--allowedTools=mcp__cw__propose_resolve",
        "--permission-prompts", "none", f"--model={profile['model']}",
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        f"--system-prompt={system}", f"--add-dir={add_dir}",
    ]
    return argv + (["--resume", resume] if resume else ["--session-id", session_id])


def claude_stream(argv, prompt, *, cwd, timeout, on_line, on_spawn=None):
    """Run argv, feeding each parsed stdout JSON line to on_line. Returns (returncode,
    stderr_tail); a timeout raises LLMError(kind="timeout") like claude_call."""
    sem = _current_semaphore()
    sem.acquire()
    try:
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=claude_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True,
            )
        except FileNotFoundError as e:
            raise LLMError(
                "claude not on PATH", kind="config", remedy=_CLAUDE_NOT_ON_PATH_REMEDY,
            ) from e

        def kill():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        err_buf = []

        def drain():
            size = 0
            for chunk in iter(lambda: proc.stderr.read(4096), ""):
                err_buf.append(chunk)
                size += len(chunk)
                while size > 65536 and len(err_buf) > 1:
                    size -= len(err_buf.pop(0))

        def feed():
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        timed_out = threading.Event()

        def expire():
            timed_out.set()
            kill()

        timer = threading.Timer(timeout, expire)
        threads = [threading.Thread(target=drain, daemon=True), threading.Thread(target=feed, daemon=True)]
        try:
            if on_spawn:
                on_spawn(proc)
            timer.start()
            for t in threads:
                t.start()
            for line in iter(proc.stdout.readline, ""):
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    on_line(obj)
        except BaseException:
            kill()
            raise
        finally:
            timer.cancel()
            proc.wait()
            for t in threads:
                t.join(timeout=5)
    finally:
        sem.release()

    if timed_out.is_set():
        raise LLMError(
            "claude process timed out", kind="timeout", remedy="raise timeout_s in config.json",
        )
    return proc.returncode, "".join(err_buf)[-65536:]


def _render_claude_prompt(messages):
    parts = []
    for message in messages:
        if message.get("role") == "assistant":
            parts.append(f"Your previous answer:\n```json\n{message.get('content')}\n```")
        else:
            parts.append(message.get("content") or "")
    return "\n\n".join(parts)


def _run_claude_tools(profile, messages, tools, handlers, *, max_rounds, timeout, on_usage, cwd):
    # ponytail: max_tokens/nudge are HTTP-path knobs; each claude process runs its own tool
    # loop and timeout_s is the only budget here.
    submit = [t for t in tools if t["function"]["name"] not in CLAUDE_TOOLS]
    if len(submit) > 1:
        raise LLMError("claude-code backend supports at most one submit tool", kind="config")
    tools_arg = ",".join(
        CLAUDE_TOOLS[t["function"]["name"]] for t in tools if t["function"]["name"] in CLAUDE_TOOLS
    )
    schema = (submit[0]["function"]["parameters"] or None) if submit else None

    system = messages[0]["content"] + (
        "\n\nIn this session read_file, grep and list_dir are the Read, Grep and Glob tools, "
        "rooted at the current directory (the repo at head)."
    )
    if submit:
        system += (
            f"\n\nCalling `{submit[0]['function']['name']}` means returning its arguments as "
            "your final structured output; there is no tool by that name."
        )

    for _round in range(max_rounds):
        prompt = _render_claude_prompt(messages[1:])
        try:
            out, usage = claude_call(
                profile, system, prompt, cwd=cwd, tools=tools_arg, schema=schema, timeout=timeout,
            )
        except LLMError as e:
            if on_usage and getattr(e, "usage", None):
                on_usage(e.usage)
            raise
        if on_usage:
            on_usage(usage)

        if schema is None:
            result = out.get("result") or ""
            messages.append({"role": "assistant", "content": result})
            return result

        structured = out.get("structured_output")
        if not isinstance(structured, dict):
            raise LLMError("claude-code reply had no structured output", kind="protocol")
        messages.append({
            "role": "assistant", "content": json.dumps(structured),
            "permission_denials": out.get("permission_denials"),
        })
        result = handlers[submit[0]["function"]["name"]](structured)
        if isinstance(result, Done):
            return result.value
        messages.append({"role": "user", "content": result})

    raise LLMError("tool loop exceeded max_rounds", kind="rounds")


def run_tools(profile, messages, tools, handlers, *, max_rounds=10, max_tokens=200000,
              timeout=300, on_usage=None, nudge="Reply only by calling one of the tools.", cwd=None):
    if profile.get("kind") == "claude-code":
        return _run_claude_tools(
            profile, messages, tools, handlers, max_rounds=max_rounds, timeout=timeout,
            on_usage=on_usage, cwd=cwd,
        )
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
    if profile.get("kind") == "claude-code":
        try:
            auth = subprocess.run(
                ["claude", "auth", "status", "--json"], env=claude_env(),
                capture_output=True, text=True, timeout=timeout,
            )
        except FileNotFoundError as e:
            raise LLMError(
                "claude not on PATH", kind="config", remedy=_CLAUDE_NOT_ON_PATH_REMEDY,
            ) from e
        except subprocess.TimeoutExpired as e:
            raise LLMError(
                "claude auth status timed out", kind="timeout",
                remedy="raise timeout_s in config.json",
            ) from e
        try:
            auth_data = json.loads(auth.stdout)
        except ValueError:
            auth_data = {}
        if not auth_data.get("loggedIn"):
            raise LLMError("claude is not logged in", kind="auth", remedy=_CLAUDE_AUTH_REMEDY)

        schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
        with tempfile.TemporaryDirectory() as tmp:
            out, _usage = claude_call(
                profile, "", "Return ok: true.", cwd=tmp, tools="", schema=schema, timeout=timeout,
            )
        if not isinstance(out.get("structured_output"), dict):
            raise LLMError(
                "model did not reply with structured output", kind="no_tools",
                remedy="point this role at a model that supports structured output",
            )
        return

    messages = [{"role": "user", "content": "Call the ping tool now."}]
    tools = [{"type": "function", "function": {"name": "ping", "description": "ping",
                                                "parameters": {"type": "object", "properties": {}}}}]
    message, _usage = chat(profile, messages, tools, timeout=timeout)
    if not message.get("tool_calls"):
        raise LLMError(
            "model did not reply with a tool call", kind="no_tools",
            remedy="point this role at a model that supports tool calls",
        )
