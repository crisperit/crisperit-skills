#!/usr/bin/env python3
"""Self-check for cw_testlib.fake_claude. Assert-based, no framework; also collected by pytest.
Proves the fake itself behaves before any batch-2 module relies on it: argv/model-keyed entries,
stdin capture, env_keys, auth status, and a spawn_child pid surviving in the log."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_testlib  # noqa: E402


def _run_claude(*args, input=""):
    return subprocess.run(["claude", *args], input=input, capture_output=True, text=True)


def test_model_keyed_entries_advance_per_call():
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [cw_testlib.claude_result(result="first"),
                              cw_testlib.claude_result(result="second")]}
        with cw_testlib.fake_claude(tmp, script) as fc:
            out1 = _run_claude("-p", "--model=sonnet", "--output-format", "json", input="hi")
            out2 = _run_claude("-p", "--model", "sonnet", "--output-format", "json", input="hi")
            out3 = _run_claude("-p", "--model=sonnet", "--output-format", "json", input="hi")
            assert out1.returncode == 0 and '"result": "first"' in out1.stdout
            assert out2.returncode == 0 and '"result": "second"' in out2.stdout
            assert out3.returncode == 0 and '"result": "second"' in out3.stdout  # clamps at last
            assert fc.count("sonnet") == 3


def test_stdin_and_argv_and_env_keys_logged():
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [cw_testlib.claude_result(result="ok")]}
        with cw_testlib.fake_claude(tmp, script) as fc:
            os.environ["ANTHROPIC_TEST_VAR"] = "x"
            try:
                _run_claude("-p", "--model=sonnet", "--system-prompt=sys", input="the prompt")
            finally:
                del os.environ["ANTHROPIC_TEST_VAR"]
            entry = fc.log()[-1]
            assert entry["stdin"] == "the prompt"
            assert "--system-prompt=sys" in entry["argv"]
            assert entry["model"] == "sonnet"
            assert "ANTHROPIC_TEST_VAR" in entry["env_keys"]


def test_claude_raw_gives_non_json_stdout():
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [cw_testlib.claude_raw("not json", exit=1, stderr="boom")]}
        with cw_testlib.fake_claude(tmp, script):
            out = _run_claude("-p", "--model=sonnet", input="hi")
            assert out.returncode == 1
            assert out.stdout == "not json"
            assert out.stderr == "boom"


def test_auth_status_reports_configured_login():
    with cw_testlib.temp_home() as tmp:
        with cw_testlib.fake_claude(tmp, {}, auth={"loggedIn": False, "authMethod": None,
                                                     "apiProvider": None}) as fc:
            out = _run_claude("auth", "status", "--json")
            assert out.returncode == 0
            assert '"loggedIn": false' in out.stdout
            assert fc.log()[-1]["model"] is None


def test_spawn_child_pid_is_logged_and_dies_with_group():
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [cw_testlib.claude_delayed(0.3, cw_testlib.claude_result(result="late"),
                                                         spawn_child=True)]}
        with cw_testlib.fake_claude(tmp, script) as fc:
            proc = subprocess.Popen(["claude", "-p", "--model=sonnet"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     start_new_session=True)
            out, _ = proc.communicate(input="hi", timeout=5)
            assert '"result": "late"' in out
            child_pid = fc.log()[-1]["child_pid"]
            assert child_pid is not None
            os.killpg(proc.pid, 9)
            time.sleep(0.2)
            with_err = subprocess.run(["kill", "-0", str(child_pid)])
            assert with_err.returncode != 0  # child died with the group


_MCP_SERVER = r"""
import json, sys
for line in sys.stdin:
    req = json.loads(line)
    if req.get("id") is None:
        continue
    m = req["method"]
    if m == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}}
    elif m == "tools/list":
        res = {"tools": [{"name": "echo"}]}
    else:
        a = req["params"]["arguments"]
        res = {"isError": "bad" in a, "content": [{"type": "text", "text": "got " + json.dumps(a)}]}
    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": res}), flush=True)
"""


def _mcp_config(tmp, *, dir_, qid="q1", missing=False):
    script = Path(tmp) / "srv.py"
    script.write_text(_MCP_SERVER)
    command = "/nonexistent/server" if missing else sys.executable
    args = ["--dir", str(dir_), "--qid", qid] if missing else [str(script), "--dir", str(dir_), "--qid", qid]
    return "--mcp-config=" + json.dumps({"mcpServers": {"cw": {"command": command, "args": args}}})


def test_stream_lines_in_order_and_start_record_logged():
    with cw_testlib.temp_home() as tmp:
        entry = cw_testlib.claude_stream_entry(["Hel", "lo"], session_id="sid-9")
        with cw_testlib.fake_claude(tmp, {"haiku": [entry]}) as fc:
            out = _run_claude("-p", "--model=haiku", input="hi")
            lines = [json.loads(line) for line in out.stdout.splitlines()]
            assert lines[0]["subtype"] == "init" and lines[0]["session_id"] == "sid-9"
            deltas = [x["event"]["delta"]["text"] for x in lines
                      if x["type"] == "stream_event" and x["event"]["type"] == "content_block_delta"]
            assert deltas == ["Hel", "lo"]
            assert [x["type"] for x in lines][-2:] == ["assistant", "result"]
            assert lines[-1]["result"] == "Hello"
            start, = fc.starts()
            assert start["pid"] > 0 and start["pgid"] > 0 and "--model=haiku" in start["argv"]
            assert fc.log()[-1]["stdin"] == "hi" and fc.log()[-1]["pid"] == start["pid"]
            assert fc.count("haiku") == 1


def test_start_record_exists_while_run_is_in_flight():
    with cw_testlib.temp_home() as tmp:
        entry = cw_testlib.claude_stream_entry(["a", "b"], delay=30)
        with cw_testlib.fake_claude(tmp, {"haiku": [entry]}) as fc:
            proc = subprocess.Popen(["claude", "-p", "--model=haiku"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, start_new_session=True)
            proc.stdin.write("x")
            proc.stdin.close()
            proc.stdout.readline()
            start, = fc.starts()
            assert start["pid"] == proc.pid and start["pgid"] == proc.pid
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
            assert fc.log() == []


def test_mcp_calls_end_to_end():
    with cw_testlib.temp_home() as tmp:
        turns = Path(tmp) / "d" / "turns"
        turns.mkdir(parents=True)
        (turns / "q1.anchor.json").write_text(json.dumps({"resolvable": [{"id": "n7"}]}))
        calls = [{"at": 2, "name": "echo", "arguments": {"thread": "$FIRST_RESOLVABLE"}},
                 {"at": 3, "name": "echo", "arguments": {"bad": 1}}]
        entry = cw_testlib.claude_stream_entry(["x"], mcp_calls=calls)
        with cw_testlib.fake_claude(tmp, {"haiku": [entry]}):
            out = _run_claude("-p", "--model=haiku", _mcp_config(tmp, dir_=Path(tmp) / "d"), input="")
            lines = [json.loads(line) for line in out.stdout.splitlines()]
            assert lines[0]["mcp_servers"] == [{"name": "cw", "status": "connected"}]
            uses = [x["message"]["content"][0] for x in lines if x["type"] == "assistant"
                    and x["message"]["content"][0]["type"] == "tool_use"]
            assert uses[0]["name"] == "mcp__cw__echo" and uses[0]["input"] == {"thread": "n7"}
            results = [x["message"]["content"][0] for x in lines if x["type"] == "user"]
            assert results[0]["is_error"] is None and "n7" in results[0]["content"][0]["text"]
            assert results[1]["is_error"] is True
            assert results[0]["tool_use_id"] == uses[0]["id"]
            assert lines.index(next(x for x in lines if x["type"] == "user")) > 2


def test_first_resolvable_falls_back_when_none_resolvable():
    with cw_testlib.temp_home() as tmp:
        turns = Path(tmp) / "d" / "turns"
        turns.mkdir(parents=True)
        (turns / "q1.anchor.json").write_text(json.dumps({"resolvable": []}))
        calls = [{"at": 2, "name": "echo", "arguments": {"bad": 1, "thread": "$FIRST_RESOLVABLE"}}]
        entry = cw_testlib.claude_stream_entry(["x"], mcp_calls=calls)
        with cw_testlib.fake_claude(tmp, {"haiku": [entry]}):
            out = _run_claude("-p", "--model=haiku", _mcp_config(tmp, dir_=Path(tmp) / "d"), input="")
            assert out.returncode == 0
            lines = [json.loads(line) for line in out.stdout.splitlines()]
            use = next(x["message"]["content"][0] for x in lines if x["type"] == "assistant"
                       and x["message"]["content"][0]["type"] == "tool_use")
            assert use["input"]["thread"] == "none-resolvable"
            result = next(x["message"]["content"][0] for x in lines if x["type"] == "user")
            assert result["is_error"] is True
            assert lines[-1]["type"] == "result"


def test_init_failed_when_mcp_server_is_missing():
    with cw_testlib.temp_home() as tmp:
        entry = cw_testlib.claude_stream_entry(["x"])
        with cw_testlib.fake_claude(tmp, {"haiku": [entry]}):
            out = _run_claude("-p", "--model=haiku", _mcp_config(tmp, dir_=tmp, missing=True), input="")
            init = json.loads(out.stdout.splitlines()[0])
            assert init["mcp_servers"] == [{"name": "cw", "status": "failed"}]


def test_init_cw_false_lists_no_server():
    entry = cw_testlib.claude_stream_entry(["x"], init_cw=False)
    assert entry["stream"][0]["mcp_servers"] == []


if __name__ == "__main__":
    tests = [
        test_model_keyed_entries_advance_per_call,
        test_stdin_and_argv_and_env_keys_logged,
        test_claude_raw_gives_non_json_stdout,
        test_auth_status_reports_configured_login,
        test_spawn_child_pid_is_logged_and_dies_with_group,
        test_stream_lines_in_order_and_start_record_logged,
        test_start_record_exists_while_run_is_in_flight,
        test_mcp_calls_end_to_end,
        test_first_resolvable_falls_back_when_none_resolvable,
        test_init_failed_when_mcp_server_is_missing,
        test_init_cw_false_lists_no_server,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
