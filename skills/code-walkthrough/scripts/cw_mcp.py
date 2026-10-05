#!/usr/bin/env python3
"""stdio MCP bridge: speaks newline-delimited JSON-RPC 2.0 to the agent harness, and HTTP to
cw_server.py (spawning it lazily on the first tool call). Also the CLI for setup/check/stop.

Usage:
  cw_mcp.py mcp                       run the stdio MCP server (default when no args)
  cw_mcp.py setup [--agent claude|print]
  cw_mcp.py check
  cw_mcp.py stop

Stdlib only. Logging goes to stderr; stdout carries only JSON-RPC replies.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_llm  # noqa: E402
import cw_store  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent
KNOWN_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

_STDOUT_LOCK = threading.Lock()

TOOLS = [
    {
        "name": "walkthrough_start",
        "description": "Start or resume a live code-walkthrough run for a diff and return its URL.",
        "inputSchema": {
            "type": "object", "required": ["repo", "base", "head", "target", "slug"],
            "properties": {
                "repo": {"type": "string"}, "base": {"type": "string"}, "head": {"type": "string"},
                "target": {"type": "string"}, "slug": {"type": "string"}, "pr": {"type": "integer"},
                "explain": {"type": "boolean"}, "paths": {"type": "array", "items": {"type": "string"}},
                "title": {"type": "string"}, "diff_file": {"type": "string"},
            },
        },
    },
    {
        "name": "walkthrough_get",
        "description": "Get the status, summary and usage of a walkthrough run, optionally waiting for it to finish.",
        "inputSchema": {
            "type": "object", "required": ["id"],
            "properties": {
                "id": {"type": "string"}, "key": {"type": "string"},
                "parts": {"type": "array", "items": {"enum": ["meta", "summary", "qa", "files", "notes"]}},
                "wait_s": {"type": "integer", "minimum": 0, "maximum": 600},
            },
        },
    },
    {
        "name": "walkthrough_list",
        "description": "List recent walkthroughs, newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}, "limit": {"type": "integer", "default": 20}},
        },
    },
]


def _log(message):
    print(message, file=sys.stderr, flush=True)


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _wait_pid_exit(pid, timeout=5):
    deadline = time.monotonic() + timeout
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)


def _probe_health(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _post(info, path, body=None, timeout=5):
    data = json.dumps(body or {}).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}{path}", data=data, method="POST",
        headers={"X-CW-Token": info["token"], "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read())


def _spawn_daemon():
    log_path = cw_store.log_path()
    log_f = open(log_path, "a")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPTS_DIR / "cw_server.py"), "serve"],
            start_new_session=True, stdin=subprocess.DEVNULL, stdout=log_f, stderr=subprocess.STDOUT,
            close_fds=True, cwd=str(cw_store.home()),
        )
    finally:
        log_f.close()
    # Reap the detached daemon on exit; otherwise it is a zombie that os.kill(pid, 0) still
    # sees as alive until this process exits.
    threading.Thread(target=proc.wait, daemon=True).start()


def ensure_server():
    """server.json names a live pid on our protocol: return it. A live pid on a different
    protocol gets shut down and waited out. Otherwise spawn and poll (CW_START_TIMEOUT)."""
    info = cw_store.read_json(cw_store.server_json_path())
    if info:
        health = _probe_health(info.get("port"))
        if health and health.get("protocol") == cw_store.PROTOCOL:
            return info
        if health is not None:
            try:
                _post(info, "/api/shutdown")
            except Exception:
                pass
            _wait_pid_exit(info.get("pid"))

    _spawn_daemon()
    timeout = float(os.environ.get("CW_START_TIMEOUT", 10))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = cw_store.read_json(cw_store.server_json_path())
        if info and _probe_health(info.get("port")):
            return info
        time.sleep(0.1)
    raise cw_store.CWError("daemon did not start", remedy=f"see {cw_store.log_path()}")


def rpc(tool, args, *, timeout=300):
    info = ensure_server()
    try:
        return _post(info, "/api/rpc", {"tool": tool, "args": args}, timeout=timeout)
    except (urllib.error.URLError, ConnectionRefusedError, ConnectionResetError):
        info = ensure_server()
        return _post(info, "/api/rpc", {"tool": tool, "args": args}, timeout=timeout)


def _write_response(obj):
    with _STDOUT_LOCK:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def _handle_initialize(req):
    requested = (req.get("params") or {}).get("protocolVersion")
    protocol = requested if requested in KNOWN_PROTOCOLS else KNOWN_PROTOCOLS[0]
    return {
        "protocolVersion": protocol, "capabilities": {"tools": {}},
        "serverInfo": {"name": "code-walkthrough", "version": "1"},
    }


def _handle_tools_list(_req):
    return {"tools": TOOLS}


def _handle_tools_call(req):
    params = req.get("params") or {}
    name, args = params.get("name"), params.get("arguments") or {}
    timeout = 300
    if name == "walkthrough_get":
        timeout = min(int(args.get("wait_s") or 0), 600) + 30
    try:
        reply = rpc(name, args, timeout=timeout)
    except cw_store.CWError as e:
        return {"content": [{"type": "text", "text": json.dumps({"error": str(e), "remedy": e.remedy})}],
                "isError": True}
    except Exception as e:
        return {"content": [{"type": "text", "text": json.dumps({"error": str(e), "remedy": None})}],
                "isError": True}
    if reply.get("ok"):
        return {"content": [{"type": "text", "text": json.dumps(reply["result"])}], "isError": False}
    return {"content": [{"type": "text", "text": json.dumps({"error": reply.get("error"), "remedy": reply.get("remedy")})}],
            "isError": True}


_METHODS = {"initialize": _handle_initialize, "tools/list": _handle_tools_list, "ping": lambda _req: {}}


def _dispatch(req):
    req_id = req.get("id")
    if req_id is None:
        return
    method = req.get("method")
    if method == "tools/call":
        threading.Thread(target=lambda: _write_response(
            {"jsonrpc": "2.0", "id": req_id, "result": _handle_tools_call(req)}), daemon=True).start()
        return
    handler = _METHODS.get(method)
    if handler is None:
        _write_response({"jsonrpc": "2.0", "id": req_id,
                          "error": {"code": -32601, "message": f"unknown method {method}"}})
        return
    _write_response({"jsonrpc": "2.0", "id": req_id, "result": handler(req)})


def cmd_mcp():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            _log(f"bad json-rpc line: {line!r}")
            continue
        _dispatch(req)
    return 0


_CONFIG_TEMPLATE = {
    "profiles": {
        "proxy": {"base_url": "http://localhost:4000/v1", "model": "<a model your proxy routes>",
                   "api_key_env": "LITELLM_API_KEY"},
        "local": {"base_url": "http://localhost:11434/v1", "model": "<an Ollama model with tools>"},
    },
    "roles": {"analysis": "proxy", "prose": "proxy", "ask": "proxy", "escalate": "proxy"},
    "max_concurrency": 4,
    "timeout_s": 300,
    "max_conversation_tokens": 200000,
}

_CLAUDE_CODE_TEMPLATE = {
    "profiles": {"claude": {"kind": "claude-code", "model": "sonnet"}},
    "roles": {"analysis": "claude", "prose": "claude", "ask": "claude"},
    "max_concurrency": 4,
    "timeout_s": 600,
    "max_conversation_tokens": 200000,
}


def cmd_setup(agent):
    config_path = cw_store.config_path()
    if not config_path.exists():
        if shutil.which("claude"):
            cw_store.write_json(config_path, _CLAUDE_CODE_TEMPLATE)
            _log(f"wrote {config_path} (claude-code template)")
        else:
            cw_store.write_json(config_path, _CONFIG_TEMPLATE)
            _log(f"wrote {config_path} (proxy template)")

    mcp_py = str(SCRIPTS_DIR / "cw_mcp.py")
    if agent == "print":
        print(json.dumps({"mcpServers": {"code-walkthrough": {"command": sys.executable, "args": [mcp_py, "mcp"]}}}))
        return 0
    if agent == "claude":
        if "/.claude/plugins/" in str(SCRIPTS_DIR):
            _log(f"MCP server already registered by the plugin install; config is at {config_path}")
            return 1
        subprocess.run(["claude", "mcp", "remove", "-s", "user", "code-walkthrough"], capture_output=True)
        result = subprocess.run(
            ["claude", "mcp", "add", "-s", "user", "code-walkthrough", "--", sys.executable, mcp_py, "mcp"]
        )
        return result.returncode
    _log(f"unknown --agent {agent!r}")
    return 2


def cmd_check():
    config = cw_store.load_config()
    failed = False
    for role in config.get("roles", {}):
        try:
            profile = cw_store.role_profile(config, role)
        except cw_store.CWError as e:
            print(f"FAIL {role}: {e}")
            print(f"  remedy: {e.remedy}")
            failed = True
            continue
        if profile is None:
            continue
        try:
            cw_llm.check(profile)
            print(f"ok {role} ({profile['name']}/{profile['model']})")
        except cw_llm.LLMError as e:
            print(f"FAIL {role}: {e}")
            print(f"  remedy: {e.remedy}")
            failed = True
    return 1 if failed else 0


def cmd_stop():
    info = cw_store.read_json(cw_store.server_json_path())
    if not info:
        print("not running")
        return 0
    try:
        _post(info, "/api/shutdown")
    except Exception:
        pass
    _wait_pid_exit(info.get("pid"))
    if _pid_alive(info.get("pid")):
        try:
            os.kill(info["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    print("stopped")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="cw_mcp.py")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("mcp")
    setup_parser = sub.add_parser("setup")
    setup_parser.add_argument("--agent", choices=["claude", "print"], default="print")
    sub.add_parser("check")
    sub.add_parser("stop")
    args = parser.parse_args(argv)

    if args.cmd in (None, "mcp"):
        return cmd_mcp()
    if args.cmd == "setup":
        return cmd_setup(args.agent)
    if args.cmd == "check":
        return cmd_check()
    if args.cmd == "stop":
        return cmd_stop()
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
