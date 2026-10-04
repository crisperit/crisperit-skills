#!/usr/bin/env python3
"""Test-only helpers for phase 2: an OpenAI-compatible stub chat endpoint plus the git-repo and
config builders shared by test_cw_store.py, test_cw_llm.py and the batch-1 suites. Deliberately
not `test_`-prefixed, so pytest does not try to collect it as a test module (section 0 of the
phase 2 spec).

Stdlib only. The "network" here is the loopback stub this module starts itself.
"""

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GIT_ENV = {
    "GIT_AUTHOR_NAME": "CW Test",
    "GIT_AUTHOR_EMAIL": "cw-test@example.com",
    "GIT_COMMITTER_NAME": "CW Test",
    "GIT_COMMITTER_EMAIL": "cw-test@example.com",
}


def git(repo, *args, env=None):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env={**os.environ, **GIT_ENV, **(env or {})},
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


def make_repo(tmp, base_files, head_files):
    """init -b main, commit base_files, apply head_files (None entries delete), commit again.
    Returns (repo, base_sha, head_sha)."""
    repo = Path(tmp) / "repo"
    repo.mkdir(exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    for path, content in base_files.items():
        full = repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base_sha = git(repo, "rev-parse", "HEAD").strip()

    for path, content in head_files.items():
        full = repo / path
        if content is None:
            full.unlink()
        else:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content)
    git(repo, "add", "-A")
    staged = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"])
    commit_args = ["commit", "-q", "-m", "head"]
    if staged.returncode == 0:
        commit_args.append("--allow-empty")
    git(repo, *commit_args)
    head_sha = git(repo, "rev-parse", "HEAD").strip()
    return repo, base_sha, head_sha


@contextmanager
def temp_home():
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("CODE_WALKTHROUGH_HOME")
        os.environ["CODE_WALKTHROUGH_HOME"] = tmp
        try:
            yield Path(tmp)
        finally:
            if old is None:
                os.environ.pop("CODE_WALKTHROUGH_HOME", None)
            else:
                os.environ["CODE_WALKTHROUGH_HOME"] = old


def write_config(home, profiles, roles, **limits):
    config = {"profiles": profiles, "roles": roles, **limits}
    Path(home, "config.json").write_text(json.dumps(config, indent=2))


_DEFAULT_USAGE = {"prompt_tokens": 10, "completion_tokens": 5}


def tool_call(name, args, *, usage=None, call_id=None):
    message = {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": call_id or "call_1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }],
    }
    reply = {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": message}]}
    if usage is not False:
        reply["usage"] = usage if usage is not None else dict(_DEFAULT_USAGE)
    return reply


def text(content, *, usage=None):
    message = {"role": "assistant", "content": content, "tool_calls": None}
    reply = {"choices": [{"index": 0, "finish_reason": "stop", "message": message}]}
    if usage is not False:
        reply["usage"] = usage if usage is not None else dict(_DEFAULT_USAGE)
    return reply


def http_error(status, body="", headers=None):
    return {"_cw_http_error": True, "status": status, "body": body, "headers": headers or {}}


def delayed(seconds, reply):
    return {"_cw_delay": seconds, "_cw_reply": reply}


def tool_names(body):
    return [t["function"]["name"] for t in body.get("tools", []) or []]


def last_user_text(body):
    for message in reversed(body.get("messages", [])):
        if message.get("role") == "user":
            return message.get("content", "")
    return ""


_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.S)


def first_json_block(text_):
    match = _JSON_BLOCK_RE.search(text_ or "")
    if not match:
        return None
    return json.loads(match.group(1))


class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        stub = self.server.stub
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        body["_headers"] = {k: v for k, v in self.headers.items()}
        model = body.get("model")

        with stub._lock:
            stub.requests.append(body)
            n = stub._counts.get(model, 0)
            stub._counts[model] = n + 1
            stub._in_flight += 1
            stub.peak_in_flight = max(stub.peak_in_flight, stub._in_flight)

        try:
            reply = stub._reply_for(model, n, body)
            delay = 0.0
            while isinstance(reply, dict) and reply.get("_cw_delay") is not None:
                delay += reply["_cw_delay"]
                reply = reply["_cw_reply"]
            if delay:
                time.sleep(delay)
            if stub.delay:
                time.sleep(stub.delay)
            self._send(model, reply)
        finally:
            with stub._lock:
                stub._in_flight -= 1
                stub.completed += 1

    def _send(self, model, reply):
        if isinstance(reply, dict) and reply.get("_cw_http_error"):
            payload = reply["body"]
            data = payload.encode() if isinstance(payload, str) else (payload or b"")
            self.send_response(reply["status"])
            for key, value in reply["headers"].items():
                self.send_header(key, str(value))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        stub = self.server.stub
        with stub._lock:
            stub._id_counter += 1
            stub_id = stub._id_counter
        full = {"id": f"stub-{stub_id}", "object": "chat.completion", "model": model, **reply}
        data = json.dumps(full).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class StubLLM:
    """OpenAI-compatible POST /v1/chat/completions on 127.0.0.1:<free port>, ThreadingHTTPServer
    in a daemon thread."""

    def __init__(self, script, *, delay=0.0):
        self.script = script
        self.delay = delay
        self.requests = []
        self.completed = 0
        self.peak_in_flight = 0
        self._in_flight = 0
        self._counts = {}
        self._id_counter = 0
        self._lock = threading.Lock()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self._server.daemon_threads = True
        self._server.stub = self
        self.base_url = f"http://127.0.0.1:{self._server.server_port}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _reply_for(self, model, n, body):
        entry = self.script(body, n) if callable(self.script) else self.script.get(model, [])
        if callable(self.script):
            return entry
        if not entry:
            raise RuntimeError(f"no script for model {model!r}")
        return entry[min(n, len(entry) - 1)]

    def count(self, model):
        with self._lock:
            return self._counts.get(model, 0)

    def profile(self, model, **extra):
        return {"base_url": self.base_url, "model": model, **extra}

    def close(self):
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
