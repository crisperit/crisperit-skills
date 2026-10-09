#!/usr/bin/env python3
"""Test-only helpers for phase 2: an OpenAI-compatible stub chat endpoint plus the git-repo and
config builders shared by test_cw_store.py, test_cw_llm.py and the batch-1 suites. Deliberately
not `test_`-prefixed, so pytest does not try to collect it as a test module (section 0 of the
phase 2 spec).

Stdlib only. The "network" here is the loopback stub this module starts itself, and fake_claude
is a `claude` binary on PATH, never a real process.
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
    if op in cfg.get("fail_ops", []) or op in cfg.get("lose_response_ops", []):
        sys.stderr.write("fake gh: %s configured to fail\\n" % op)
        sys.exit(1)
    sleep_s = cfg.get("sleep", {}).get(op)
    if sleep_s:
        time.sleep(sleep_s)
    print(json.dumps(data))
    sys.exit(0)


argv = sys.argv[1:]

if argv[:2] == ["api", "user"]:
    _reply("viewer", argv, None, {"login": cfg.get("viewer", "me")})

if argv[:2] == ["pr", "view"]:
    _reply("pr-author", argv, None, {"author": {"login": cfg.get("pr_author", "me")}})

REST_POST_RE = re.compile(r"^repos/[^/]+/[^/]+/pulls/\\d+/comments(/(\\d+)/replies)?$")
if len(argv) >= 2 and argv[0] == "api" and "POST" in argv and REST_POST_RE.match(argv[1]):
    body = json.loads(sys.stdin.read())
    reply_to = REST_POST_RE.match(argv[1]).group(2)
    op = "rest-post-reply" if reply_to else "rest-post-comment"
    state = _load("state.json", {})
    if state.get("review_opened") and not reply_to:
        _log(op, argv, {"path": argv[1], "body": body})
        sys.stderr.write("fake gh: 422 User can only have one pending review per pull request\\n")
        sys.exit(1)
    state["rest_n"] = state.get("rest_n", 0) + 1
    _save("state.json", state)
    n = 2000 + state["rest_n"]
    comments = _load("comments.json", [])
    parent = next((c for c in comments if c["id"] == int(reply_to)), {}) if reply_to else {}
    if op in cfg.get("fail_ops", []):
        _reply(op, argv, {"path": argv[1], "body": body}, {})
    comments.append({"id": n, "node_id": "RC_%d" % n, "path": parent.get("path", body.get("path")),
                      "line": parent.get("line", body.get("line")), "side": parent.get("side", body.get("side")),
                      "body": body.get("body"), "user": {"login": "fake"}, "created_at": "2000-01-01T00:00:00Z",
                      "html_url": "https://github.com/o/r/pull/7#discussion_r%d" % n,
                      "in_reply_to_id": int(reply_to) if reply_to else None})
    _save("comments.json", comments)
    _reply(op, argv, {"path": argv[1], "body": body}, comments[-1])

if len(argv) >= 2 and argv[0] == "api" and re.match(r"^repos/[^/]+/[^/]+/pulls/\\d+/comments$", argv[1]):
    _reply("rest-comments", argv, None, _load("comments.json", []))

if len(argv) >= 2 and argv[0] == "api" and argv[1] == "graphql":
    payload = json.loads(sys.stdin.read())
    query = payload.get("query", "")
    variables = payload.get("variables")
    match = OP_RE.search(query)
    op = match.group(1) if match else "unknown"
    state = _load("state.json", {"review_opened": False, "comment_n": 0})

    calls = state.setdefault("calls", {})
    calls[op] = calls.get(op, 0) + 1
    _save("state.json", state)
    if op in cfg.get("fail_ops", []) or calls[op] in cfg.get("fail_nth", {}).get(op, []):
        _log(op, argv, variables)
        sys.stderr.write("fake gh: %s configured to fail\\n" % op)
        sys.exit(1)

    if op == "ReviewThreads":
        threads = _load("threads.json", [])
        _reply(op, argv, variables, {"data": {"repository": {"pullRequest": {
            "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": threads}}}}})
    elif op == "PendingReview":
        count = state.get("seed", 0) + state.get("delivered", 0)
        nodes = [{"id": "R_1", "comments": {"totalCount": count}}] if state.get("review_opened") else []
        _reply(op, argv, variables, {"data": {"repository": {"pullRequest": {
            "id": "PR_1", "reviews": {"nodes": nodes}}}}})
    elif op == "OpenReview":
        state["review_opened"] = True
        _save("state.json", state)
        _reply(op, argv, variables, {"data": {"addPullRequestReview": {
            "pullRequestReview": {"id": "R_1"}}}})
    elif op == "NewThread":
        state["comment_n"] = state.get("comment_n", 0) + 1
        state["delivered"] = state.get("delivered", 0) + 1
        n = state["comment_n"]
        _save("state.json", state)
        comments = _load("comments.json", [])
        comments.append({"id": 1000 + n, "node_id": "C_%d" % n, "path": variables.get("path"),
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
        state["delivered"] = state.get("delivered", 0) + 1
        n = state["comment_n"]
        _save("state.json", state)
        comments = _load("comments.json", [])
        parent_id = int(variables["inReplyTo"].split("_")[1]) + 1000
        parent = next((c for c in comments if c["id"] == parent_id), {})
        comments.append({"id": 1000 + n, "node_id": "C_%d" % n, "path": parent.get("path"),
                          "line": parent.get("line"), "side": parent.get("side"),
                          "body": variables.get("body"), "user": {"login": "fake"},
                          "created_at": "2000-01-01T00:00:00Z", "html_url": "",
                          "in_reply_to_id": parent_id})
        _save("comments.json", comments)
        _reply(op, argv, variables, {"data": {"addPullRequestReviewComment": {"comment": {
            "id": "C_%d" % n, "databaseId": 1000 + n}}}})
    elif op == "SubmitReview":
        state.update(review_opened=False, seed=0, delivered=0)
        _save("state.json", state)
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


_FAKE_CLAUDE_BODY = '''
import fcntl, json, os, queue, signal, subprocess, sys, threading, time
from pathlib import Path

root = Path(os.environ["CW_FAKE_CLAUDE_DIR"])
cfg = json.loads((root / "config.json").read_text())
log_path = root / "log.jsonl"
counts_path = root / "counts.json"
lock_path = root / "lock"


def _next_count(model):
    with open(lock_path, "a+") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            counts = json.loads(counts_path.read_text()) if counts_path.exists() else {}
            n = counts.get(model, 0)
            counts[model] = n + 1
            counts_path.write_text(json.dumps(counts))
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)
    return n


def _append_log(entry):
    with open(lock_path, "a+") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            with open(log_path, "a") as f:
                f.write(json.dumps(entry) + "\\n")
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)


argv = sys.argv[1:]

if argv[:2] == ["auth", "status"]:
    _append_log({"argv": argv, "cwd": os.getcwd(), "env_keys": sorted(os.environ.keys()),
                 "stdin": "", "model": None, "start": time.time(), "end": time.time(),
                 "child_pid": None})
    print(json.dumps(cfg["auth"]))
    sys.exit(0)

model = None
for i, a in enumerate(argv):
    if a == "--model" and i + 1 < len(argv):
        model = argv[i + 1]
    elif a.startswith("--model="):
        model = a.split("=", 1)[1]

entries = cfg["script"].get(model, [])
n = _next_count(model)
entry = entries[min(n, len(entries) - 1)] if entries else {"stdout": "", "exit": 1}

start = time.time()
_append_log({"event": "start", "argv": argv, "pid": os.getpid(), "pgid": os.getpgrp(),
             "cwd": os.getcwd(), "model": model, "start": start})
stdin_data = sys.stdin.read()
child_pid = None

while isinstance(entry, dict) and "_cw_delay" in entry:
    if entry.get("_cw_spawn_child"):
        devnull = open(os.devnull, "wb")
        child_pid = subprocess.Popen(["sleep", "30"], stdout=devnull, stderr=devnull).pid
    time.sleep(entry["_cw_delay"])
    entry = entry["_cw_entry"]

_END = {"event": "end", "pid": os.getpid(), "pgid": os.getpgrp(), "argv": argv,
        "cwd": os.getcwd(), "env_keys": sorted(os.environ.keys()), "stdin": stdin_data,
        "model": model, "start": start, "child_pid": child_pid}
stderr = entry.get("stderr", "")


def _emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()


class _Mcp:
    def __init__(self):
        self.proc = None
        self.lines = queue.Queue()
        self.next_id = 0

    def start(self, config_arg):
        try:
            text = config_arg if config_arg.lstrip().startswith("{") else Path(config_arg).read_text()
            servers = json.loads(text)["mcpServers"]
            self.name = "cw" if "cw" in servers else next(iter(servers))
            spec = servers[self.name]
            self.qid, self.dir = None, None
            args = spec.get("args", [])
            for i, a in enumerate(args):
                if a == "--qid" and i + 1 < len(args):
                    self.qid = args[i + 1]
                elif a == "--dir" and i + 1 < len(args):
                    self.dir = args[i + 1]
            self.proc = subprocess.Popen(
                [spec["command"], *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, env={**os.environ, **spec.get("env", {})})
        except Exception:
            return False
        threading.Thread(target=self._pump, daemon=True).start()
        try:
            if self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                       "clientInfo": {"name": "fake-claude", "version": "1"}}) is None:
                return False
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            return self.rpc("tools/list", {}) is not None
        except Exception:
            return False

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\\n")
        self.proc.stdin.flush()

    def rpc(self, method, params, timeout=10):
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        deadline = time.time() + timeout
        while True:
            try:
                line = self.lines.get(timeout=max(0.0, deadline - time.time()))
            except queue.Empty:
                return None
            if line is None:
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == self.next_id:
                return msg

    def close(self):
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()


mcp = _Mcp()


def _on_term(signum, frame):
    mcp.close()
    sys.exit(128 + signum)


signal.signal(signal.SIGTERM, _on_term)


def _resolve_placeholders(value):
    if value == "$FIRST_RESOLVABLE":
        try:
            anchor = json.loads((Path(mcp.dir) / "turns" / (mcp.qid + ".anchor.json")).read_text())
            return anchor["resolvable"][0]["id"]
        except (OSError, ValueError, LookupError):
            return "none-resolvable"
    if isinstance(value, dict):
        return {k: _resolve_placeholders(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_placeholders(v) for v in value]
    return value


def _do_mcp_call(call, k):
    tool_id = "toolu_fake_%d" % k
    arguments = _resolve_placeholders(call.get("arguments", {}))
    wire = "mcp__%s__%s" % (mcp.name, call["name"])
    _emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_id, "name": wire, "input": arguments}]}})
    reply = mcp.rpc("tools/call", {"name": call["name"], "arguments": arguments}, timeout=30)
    result = (reply or {}).get("result") or {}
    if reply is None or "error" in reply:
        result = {"isError": True, "content": [{"type": "text", "text": "mcp call failed"}]}
    _emit({"type": "user", "message": {"content": [{
        "type": "tool_result", "tool_use_id": tool_id,
        "is_error": True if result.get("isError") else None,
        "content": result.get("content", [])}]}})


if "stream" in entry:
    config_arg = None
    for i, a in enumerate(argv):
        if a == "--mcp-config" and i + 1 < len(argv):
            config_arg = argv[i + 1]
        elif a.startswith("--mcp-config="):
            config_arg = a.split("=", 1)[1]
    connected = mcp.start(config_arg) if config_arg else None
    if stderr:
        sys.stderr.write(stderr)
    calls = list(entry.get("mcp_calls", []))
    items = entry["stream"]
    k = 0
    for i, item in enumerate(items + [None]):
        for call in [c for c in calls if c["at"] == i or (item is None and c["at"] >= len(items))]:
            k += 1
            _do_mcp_call(call, k)
        if item is None:
            break
        if "_sleep" in item:
            time.sleep(item["_sleep"])
            continue
        if connected is not None and item.get("subtype") == "init":
            item = {**item, "mcp_servers": [
                {**s, "status": "connected" if connected else "failed"} if s.get("name") == "cw" else s
                for s in item.get("mcp_servers", [])]}
        _emit(item)
    _END["end"] = time.time()
    _append_log(_END)
    mcp.close()
    sys.exit(entry.get("exit", 0))

_END["end"] = time.time()
_append_log(_END)

if stderr:
    sys.stderr.write(stderr)

stdout = entry.get("stdout", "")
sys.stdout.write(stdout if isinstance(stdout, str) else json.dumps(stdout))
sys.exit(entry.get("exit", 0))
'''


def claude_result(structured=None, *, result="", usage=None, is_error=False, **extra):
    """One `claude -p --output-format json` reply: a successful or failed `result` event."""
    stdout = {
        "type": "result", "subtype": "success", "is_error": is_error, "result": result,
        "structured_output": structured,
        "usage": usage or {"input_tokens": 10, "output_tokens": 5,
                            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        "total_cost_usd": 0.01, "num_turns": 1, "permission_denials": [], **extra,
    }
    return {"stdout": stdout, "exit": 1 if is_error else 0}


def claude_raw(stdout, *, exit=1, stderr=""):
    """Non-JSON stdout, for the LLMError(kind="protocol") path."""
    return {"stdout": stdout, "exit": exit, "stderr": stderr}


def claude_delayed(seconds, entry, *, spawn_child=False):
    """Sleep `seconds` before resolving to `entry`; with spawn_child=True, start `sleep 30`
    first and log its pid, to prove a timeout kills the whole process group."""
    return {"_cw_delay": seconds, "_cw_entry": entry, "_cw_spawn_child": spawn_child}


def claude_stream_entry(chunks, *, session_id="s-1", result=None, usage=None, mcp_calls=(),
                        delay=0, init_cw=True):
    """A stream-json entry: init, per-chunk text deltas (`delay` seconds apart), the assistant
    message, then the result. mcp_calls[i]["at"] indexes the returned `stream` list (init is 0,
    _sleep items count)."""
    stream = [{"type": "system", "subtype": "init", "session_id": session_id,
               "mcp_servers": [{"name": "cw", "status": "connected"}] if init_cw else [],
               "tools": ["mcp__cw__propose_resolve", "mcp__cw__propose_page_edit"] if init_cw else []}]
    for i, chunk in enumerate(chunks):
        if i == 0:
            stream.append({"type": "stream_event", "event": {
                "type": "content_block_start", "index": 0, "content_block": {"type": "text"}}})
        elif delay:
            stream.append({"_sleep": delay})
        stream.append({"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": chunk}}})
    text_ = "".join(chunks)
    stream.append({"type": "assistant", "message": {"content": [{"type": "text", "text": text_}]}})
    stream.append({"type": "result", "subtype": "success", "is_error": False,
                   "result": text_ if result is None else result, "session_id": session_id,
                   "usage": usage or {"input_tokens": 10, "output_tokens": 5,
                                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}})
    return {"stream": stream, "exit": 0, "stderr": "", "mcp_calls": list(mcp_calls)}


class _FakeClaude:
    """log() holds one record per finished run (plus the auth-status calls); starts() holds the
    start-of-run records ({"event":"start", argv, pid, pgid, cwd, model, start}), written before
    stdin is read so a cancel test can find the pid of a run that never finishes."""

    def __init__(self, root):
        self._log_path = root / "log.jsonl"

    def _all(self):
        if not self._log_path.exists():
            return []
        return [json.loads(line) for line in self._log_path.read_text().splitlines() if line.strip()]

    def log(self):
        return [e for e in self._all() if e.get("event") != "start"]

    def starts(self):
        return [e for e in self._all() if e.get("event") == "start"]

    def count(self, model):
        return sum(1 for entry in self.log() if entry.get("model") == model)


@contextmanager
def fake_claude(tmp, script, *, auth=None):
    """A `claude` on PATH that fakes `claude auth status` and `claude -p ...`, replying from
    `script` ({model: [entries]}, keyed by the --model argv value) like StubLLM. State a call
    needs across invocations (the per-model call counter) lives in CW_FAKE_CLAUDE_DIR as JSON,
    since each `claude` call is its own fresh process."""
    root = Path(tmp) / "fake-claude"
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    default_auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}
    (root / "config.json").write_text(json.dumps({
        "script": script, "auth": auth if auth is not None else default_auth,
    }))
    (root / "log.jsonl").write_text("")

    claude_path = bin_dir / "claude"
    claude_path.write_text("#!" + sys.executable + "\n" + _FAKE_CLAUDE_BODY)
    claude_path.chmod(0o755)

    old_path = os.environ.get("PATH", "")
    old_dir = os.environ.get("CW_FAKE_CLAUDE_DIR")
    os.environ["PATH"] = str(bin_dir) + os.pathsep + old_path
    os.environ["CW_FAKE_CLAUDE_DIR"] = str(root)
    try:
        yield _FakeClaude(root)
    finally:
        os.environ["PATH"] = old_path
        if old_dir is None:
            os.environ.pop("CW_FAKE_CLAUDE_DIR", None)
        else:
            os.environ["CW_FAKE_CLAUDE_DIR"] = old_dir


@contextmanager
def fake_gh(tmp, *, comments=(), threads=None, fail_ops=(), sleep=None, pending=None,
            lose_response_ops=(), fail_nth=None, viewer="me", pr_author="me"):
    """A `gh` on PATH that fakes the one REST call and the GraphQL ops notes.py sends,
    dispatched by the mutation/query name parsed from the request body (never from argv --
    see cw_run._gh/_notes: a note's body never reaches a command line). State a call needs
    across invocations (the pending review, the running comment counter) lives in
    CW_FAKE_GH_DIR as JSON, since each `gh` call is its own fresh process.

    fail_ops fail before any side effect (a rejected call); lose_response_ops record the
    side effect, then exit non-zero (comment landed, reply lost); fail_nth is
    {op: [1-based call numbers]} to reject only chosen calls of an op. pending=N seeds an
    already-open pending review holding N comments.
    """
    root = Path(tmp) / "fake-gh"
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (root / "comments.json").write_text(json.dumps(list(comments)))
    (root / "threads.json").write_text(json.dumps(threads if threads is not None else []))
    (root / "config.json").write_text(json.dumps({
        "fail_ops": list(fail_ops), "sleep": sleep or {}, "lose_response_ops": list(lose_response_ops),
        "fail_nth": fail_nth or {}, "viewer": viewer, "pr_author": pr_author}))
    (root / "state.json").write_text(json.dumps({
        "review_opened": pending is not None, "comment_n": 0, "seed": pending or 0}))
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
