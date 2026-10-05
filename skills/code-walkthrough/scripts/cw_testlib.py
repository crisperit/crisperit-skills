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
import sys
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


_FAKE_GH_BODY = '''
import json, os, re, sys, time
from pathlib import Path

root = Path(os.environ["CW_FAKE_GH_DIR"])
cfg = json.loads((root / "config.json").read_text())
log_path = root / "log.jsonl"
OP_RE = re.compile(r"(?:query|mutation)\\s+(\\w+)")


def _load(name, default):
    path = root / name
    return json.loads(path.read_text()) if path.exists() else default


def _save(name, data):
    (root / name).write_text(json.dumps(data))


def _log(op, argv, variables):
    entry = {"argv": argv, "cwd": os.getcwd(), "gh_token": "GH_TOKEN" in os.environ,
             "github_token": "GITHUB_TOKEN" in os.environ, "op": op, "variables": variables}
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\\n")


def _reply(op, argv, variables, data):
    _log(op, argv, variables)
    if op in cfg.get("fail_ops", []):
        sys.stderr.write("fake gh: %s configured to fail\\n" % op)
        sys.exit(1)
    sleep_s = cfg.get("sleep", {}).get(op)
    if sleep_s:
        time.sleep(sleep_s)
    print(json.dumps(data))
    sys.exit(0)


argv = sys.argv[1:]

if len(argv) >= 2 and argv[0] == "api" and re.match(r"^repos/[^/]+/[^/]+/pulls/\\d+/comments$", argv[1]):
    _reply("rest-comments", argv, None, _load("comments.json", []))

if len(argv) >= 2 and argv[0] == "api" and argv[1] == "graphql":
    payload = json.loads(sys.stdin.read())
    query = payload.get("query", "")
    variables = payload.get("variables")
    match = OP_RE.search(query)
    op = match.group(1) if match else "unknown"
    state = _load("state.json", {"review_opened": False, "comment_n": 0})

    if op == "ReviewThreads":
        threads = _load("threads.json", [])
        _reply(op, argv, variables, {"data": {"repository": {"pullRequest": {
            "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": threads}}}}})
    elif op == "PendingReview":
        nodes = [{"id": "R_1"}] if state.get("review_opened") else []
        _reply(op, argv, variables, {"data": {"repository": {"pullRequest": {
            "id": "PR_1", "reviews": {"nodes": nodes}}}}})
    elif op == "OpenReview":
        state["review_opened"] = True
        _save("state.json", state)
        _reply(op, argv, variables, {"data": {"addPullRequestReview": {
            "pullRequestReview": {"id": "R_1"}}}})
    elif op == "NewThread":
        state["comment_n"] = state.get("comment_n", 0) + 1
        n = state["comment_n"]
        _save("state.json", state)
        comments = _load("comments.json", [])
        comments.append({"id": 1000 + n, "path": variables.get("path"),
                          "line": variables.get("line"), "side": variables.get("side"),
                          "body": variables.get("body"), "user": {"login": "fake"},
                          "created_at": "2000-01-01T00:00:00Z", "html_url": "",
                          "in_reply_to_id": None})
        _save("comments.json", comments)
        _reply(op, argv, variables, {"data": {"addPullRequestReviewThread": {"thread": {
            "id": "T_%d" % n,
            "comments": {"nodes": [{"id": "C_%d" % n, "databaseId": 1000 + n}]}}}}})
    elif op == "ReplyThread":
        state["comment_n"] = state.get("comment_n", 0) + 1
        n = state["comment_n"]
        _save("state.json", state)
        _reply(op, argv, variables, {"data": {"addPullRequestReviewComment": {"comment": {
            "id": "C_%d" % n, "databaseId": 1000 + n}}}})
    elif op == "SubmitReview":
        _reply(op, argv, variables, {"data": {"submitPullRequestReview": {"pullRequestReview": {
            "id": "R_1", "state": "COMMENTED"}}}})
    elif op == "ResolveThread":
        _reply(op, argv, variables, {"data": {"resolveReviewThread": {"thread": {
            "id": variables.get("id"), "isResolved": True}}}})
    else:
        _reply(op, argv, variables, {"data": {}})

_log("unknown", argv, None)
sys.stderr.write("fake gh: unrecognized invocation: %r\\n" % argv)
sys.exit(1)
'''


class _FakeGH:
    def __init__(self, root):
        self._log_path = root / "log.jsonl"

    def log(self):
        if not self._log_path.exists():
            return []
        return [json.loads(line) for line in self._log_path.read_text().splitlines() if line.strip()]

    def count(self, op):
        return sum(1 for entry in self.log() if entry.get("op") == op)


@contextmanager
def fake_gh(tmp, *, comments=(), threads=None, fail_ops=(), sleep=None):
    """A `gh` on PATH that fakes the one REST call and the GraphQL ops notes.py sends,
    dispatched by the mutation/query name parsed from the request body (never from argv --
    see cw_run._gh/_notes: a note's body never reaches a command line). State a call needs
    across invocations (the pending review, the running comment counter) lives in
    CW_FAKE_GH_DIR as JSON, since each `gh` call is its own fresh process.
    """
    root = Path(tmp) / "fake-gh"
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (root / "comments.json").write_text(json.dumps(list(comments)))
    (root / "threads.json").write_text(json.dumps(threads if threads is not None else []))
    (root / "config.json").write_text(json.dumps({"fail_ops": list(fail_ops), "sleep": sleep or {}}))
    (root / "state.json").write_text(json.dumps({"review_opened": False, "comment_n": 0}))
    (root / "log.jsonl").write_text("")

    gh_path = bin_dir / "gh"
    gh_path.write_text("#!" + sys.executable + "\n" + _FAKE_GH_BODY)
    gh_path.chmod(0o755)

    old_path = os.environ.get("PATH", "")
    old_dir = os.environ.get("CW_FAKE_GH_DIR")
    os.environ["PATH"] = str(bin_dir) + os.pathsep + old_path
    os.environ["CW_FAKE_GH_DIR"] = str(root)
    try:
        yield _FakeGH(root)
    finally:
        os.environ["PATH"] = old_path
        if old_dir is None:
            os.environ.pop("CW_FAKE_GH_DIR", None)
        else:
            os.environ["CW_FAKE_GH_DIR"] = old_dir
