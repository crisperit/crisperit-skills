#!/usr/bin/env python3
"""Self-check for cw_mcp.py. Assert-based, no framework; also collected by pytest. Every test
that starts a real cw_server.py daemon stops it (or waits for its own idle exit) in a finally
block, so no process is left running after this module finishes."""

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_mcp  # noqa: E402
import cw_store  # noqa: E402
import cw_testlib  # noqa: E402

SCRIPTS_DIR = str(Path(__file__).parent)


@contextlib.contextmanager
def _no_claude_on_path():
    """A PATH with no `claude` binary anywhere on it, for the "not on PATH" branch."""
    with tempfile.TemporaryDirectory() as empty_bin:
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = empty_bin
        try:
            yield
        finally:
            os.environ["PATH"] = old_path


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait(cond, timeout=5, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return cond()


@contextlib.contextmanager
def _stopped_afterward():
    """Whatever daemon this test starts, make sure it is gone before the test ends."""
    try:
        yield
    finally:
        info = cw_store.read_json(cw_store.server_json_path())
        if info and _pid_alive(info.get("pid")):
            try:
                cw_mcp.cmd_stop()
            except Exception:
                pass
        if info and _pid_alive(info.get("pid")):
            try:
                os.kill(info["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_handshake_does_not_start_the_daemon():
    with cw_testlib.temp_home() as home:
        proc = subprocess.Popen(
            [sys.executable, str(Path(SCRIPTS_DIR) / "cw_mcp.py"), "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            request = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18"}}
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            reply = json.loads(line)
            assert reply["id"] == 1
            assert reply["result"]["protocolVersion"] == "2025-06-18"
            assert reply["result"]["serverInfo"]["name"] == "code-walkthrough"
            assert not (home / "server.json").exists()

            # a notification (no id) must not get a reply
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            proc.stdin.flush()

            request = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
            reply = json.loads(proc.stdout.readline())
            names = [t["name"] for t in reply["result"]["tools"]]
            assert names == ["walkthrough_start", "walkthrough_get", "walkthrough_list"]

            request = {"jsonrpc": "2.0", "id": 3, "method": "nope"}
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
            reply = json.loads(proc.stdout.readline())
            assert reply["error"]["code"] == -32601
        finally:
            proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)


def test_two_concurrent_ensure_server_give_one_pid():
    with cw_testlib.temp_home():
        with _stopped_afterward():
            results = []

            def _call():
                results.append(cw_mcp.ensure_server())

            threads = [threading.Thread(target=_call) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

            assert len(results) == 2
            assert results[0]["pid"] == results[1]["pid"]
            assert _pid_alive(results[0]["pid"])


def test_dead_pid_lock_file_is_taken_over():
    with cw_testlib.temp_home():
        with _stopped_afterward():
            proc = subprocess.Popen([sys.executable, "-c", "pass"])
            dead_pid = proc.pid
            proc.wait()
            cw_store.lock_path().write_text(str(dead_pid))

            info = cw_mcp.ensure_server()
            assert _pid_alive(info["pid"])
            assert info["pid"] != dead_pid


def test_idle_exit_within_five_seconds():
    with cw_testlib.temp_home():
        os.environ["CW_IDLE_S"] = "1"
        try:
            with _stopped_afterward():
                info = cw_mcp.ensure_server()
                assert _wait(lambda: not _pid_alive(info["pid"]), timeout=5)
        finally:
            os.environ.pop("CW_IDLE_S", None)


def test_stop_reports_not_running_then_stopped():
    with cw_testlib.temp_home():
        with _stopped_afterward():
            assert cw_mcp.cmd_stop() == 0  # "not running", nothing started yet
            info = cw_mcp.ensure_server()
            assert cw_mcp.cmd_stop() == 0
            assert _wait(lambda: not _pid_alive(info["pid"]), timeout=5)


def test_setup_with_claude_on_path_writes_claude_code_template():
    with cw_testlib.temp_home() as home:
        with tempfile.TemporaryDirectory() as tmp:
            with cw_testlib.fake_claude(tmp, {}):
                err = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    assert cw_mcp.cmd_setup("print") == 0
                config = cw_store.read_json(home / "config.json")
                assert config["profiles"]["claude"]["kind"] == "claude-code"
                assert config["profiles"]["claude"]["model"] == "sonnet"
                assert config["roles"]["analysis"] == "claude"
                assert "claude-code template" in err.getvalue()


def test_setup_without_claude_on_path_writes_proxy_template():
    with cw_testlib.temp_home() as home:
        with _no_claude_on_path():
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                assert cw_mcp.cmd_setup("print") == 0
            config = cw_store.read_json(home / "config.json")
            assert "proxy" in config["profiles"]
            assert "kind" not in config["profiles"]["proxy"]
            assert "proxy template" in err.getvalue()


def test_check_with_fake_claude_ok():
    with cw_testlib.temp_home() as home:
        cw_testlib.write_config(
            home, {"claude": {"kind": "claude-code", "model": "sonnet"}}, {"analysis": "claude"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            script = {"sonnet": [cw_testlib.claude_result(structured={"ok": True})]}
            with cw_testlib.fake_claude(tmp, script):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    assert cw_mcp.cmd_check() == 0
                assert "ok analysis (claude/sonnet)" in out.getvalue()


def test_check_not_logged_in_prints_fail_and_remedy():
    with cw_testlib.temp_home() as home:
        cw_testlib.write_config(
            home, {"claude": {"kind": "claude-code", "model": "sonnet"}}, {"analysis": "claude"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            with cw_testlib.fake_claude(tmp, {}, auth={"loggedIn": False}):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    assert cw_mcp.cmd_check() == 1
                output = out.getvalue()
                assert "FAIL analysis" in output
                assert "remedy:" in output


def test_setup_leaves_existing_config_untouched():
    with cw_testlib.temp_home() as home:
        sentinel = {"profiles": {"mine": {"base_url": "http://x", "model": "m"}}, "roles": {}}
        cw_store.write_json(home / "config.json", sentinel)
        with tempfile.TemporaryDirectory() as tmp:
            with cw_testlib.fake_claude(tmp, {}):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    assert cw_mcp.cmd_setup("print") == 0
                assert cw_store.read_json(home / "config.json") == sentinel


if __name__ == "__main__":
    tests = [
        test_handshake_does_not_start_the_daemon,
        test_two_concurrent_ensure_server_give_one_pid,
        test_dead_pid_lock_file_is_taken_over,
        test_idle_exit_within_five_seconds,
        test_stop_reports_not_running_then_stopped,
        test_setup_with_claude_on_path_writes_claude_code_template,
        test_setup_without_claude_on_path_writes_proxy_template,
        test_setup_leaves_existing_config_untouched,
        test_check_with_fake_claude_ok,
        test_check_not_logged_in_prints_fail_and_remedy,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
