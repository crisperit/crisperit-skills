#!/usr/bin/env python3
"""Self-check for cw_llm.py. Assert-based; run with pytest. Uses the loopback StubLLM from
cw_testlib, never a real network. conftest.py zeroes BACKOFF_S so the retry tests stay fast."""

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_llm  # noqa: E402
import cw_testlib  # noqa: E402

PING_TOOLS = [{"type": "function", "function": {"name": "ping", "parameters": {"type": "object", "properties": {}}}}]


def _expect_llm_error(fn):
    try:
        fn()
    except cw_llm.LLMError as e:
        return e
    raise AssertionError("expected cw_llm.LLMError")


def test_server_error_retries_three_times_then_fails():
    with cw_testlib.StubLLM({"m": [cw_testlib.http_error(500)] * 3}) as stub:
        profile = stub.profile("m")
        error = _expect_llm_error(lambda: cw_llm.chat(profile, [{"role": "user", "content": "hi"}], PING_TOOLS))
        assert error.kind == "server"
        assert stub.count("m") == 3


def test_http_error_kinds(monkeypatch):
    script = {
        "ctx": [cw_testlib.http_error(400, body="context_length_exceeded: too long")],
        "auth": [cw_testlib.http_error(401)],
        "nf": [cw_testlib.http_error(404)],
    }
    monkeypatch.setenv("MY_KEY", "secret")
    rows = [
        ("ctx", {}, "context", "ctx", 1),
        ("auth", {"api_key_env": "MY_KEY"}, "auth", "MY_KEY", None),
        ("nf", {"name": "prof-nf"}, "notfound", "prof-nf", None),
    ]
    with cw_testlib.StubLLM(script) as stub:
        for model, extra, kind, remedy_part, count in rows:
            profile = stub.profile(model, **extra)
            error = _expect_llm_error(lambda: cw_llm.chat(profile, [{"role": "user", "content": "hi"}], PING_TOOLS))
            assert error.kind == kind, model
            assert remedy_part in error.remedy, (model, error.remedy)
            if count is not None:
                assert stub.count(model) == count


def test_closed_port_gives_connect():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    profile = {"base_url": f"http://127.0.0.1:{port}/v1", "model": "m"}
    error = _expect_llm_error(lambda: cw_llm.chat(profile, [{"role": "user", "content": "hi"}], PING_TOOLS))
    assert error.kind == "connect"


def test_header_env_expansion_and_refusal_when_unset():
    with cw_testlib.StubLLM({"m": [cw_testlib.tool_call("ping", {})]}) as stub:
        profile = stub.profile("m", headers={"X-Custom": "${CW_TEST_HEADER}"})
        os.environ["CW_TEST_HEADER"] = "abc"
        try:
            cw_llm.chat(profile, [{"role": "user", "content": "hi"}], PING_TOOLS)
        finally:
            del os.environ["CW_TEST_HEADER"]
        assert stub.requests[-1]["_headers"].get("X-Custom") == "abc"

        error = _expect_llm_error(lambda: cw_llm.chat(profile, [{"role": "user", "content": "hi"}], PING_TOOLS))
        assert error.kind == "config"


def test_token_cap_raises_on_second_round():
    usage = {"prompt_tokens": 150000, "completion_tokens": 0}
    with cw_testlib.StubLLM({"m": [cw_testlib.tool_call("ping", {}, usage=usage)]}) as stub:
        profile = stub.profile("m")
        messages = [{"role": "user", "content": "go"}]
        error = _expect_llm_error(
            lambda: cw_llm.run_tools(profile, messages, PING_TOOLS, {"ping": lambda a: "pong"}, max_tokens=200000)
        )
        assert error.kind == "tokens"


def test_rounds_cap_raises():
    with cw_testlib.StubLLM({"m": [cw_testlib.tool_call("ping", {})]}) as stub:
        profile = stub.profile("m")
        messages = [{"role": "user", "content": "go"}]
        error = _expect_llm_error(
            lambda: cw_llm.run_tools(profile, messages, PING_TOOLS, {"ping": lambda a: "pong"}, max_rounds=2)
        )
        assert error.kind == "rounds"


def test_retry_after_sleep_holds_no_semaphore_slot(monkeypatch):
    monkeypatch.setattr(cw_llm, "RETRY_AFTER_MAX_S", 0.5)
    cw_llm.configure(1)
    try:
        script = {
            "a": [cw_testlib.http_error(429, headers={"Retry-After": "2"}), cw_testlib.tool_call("ping", {})],
            "b": [cw_testlib.delayed(0.2, cw_testlib.tool_call("ping", {}))],
        }
        with cw_testlib.StubLLM(script) as stub:
            results = {}

            def run(name):
                start = time.time()
                cw_llm.chat(stub.profile(name), [{"role": "user", "content": "hi"}], PING_TOOLS)
                results[name] = time.time() - start

            ta = threading.Thread(target=run, args=("a",))
            ta.start()
            time.sleep(0.1)
            tb = threading.Thread(target=run, args=("b",))
            tb.start()
            ta.join(timeout=10)
            tb.join(timeout=10)

            assert results["a"] >= 0.5, results
            assert results["b"] < results["a"], results
    finally:
        cw_llm.configure(4)


def test_peak_in_flight_matches_configured_concurrency():
    cw_llm.configure(2)
    try:
        with cw_testlib.StubLLM({"m": [cw_testlib.tool_call("ping", {})]}, delay=0.1) as stub:
            threads = [
                threading.Thread(target=cw_llm.chat, args=(stub.profile("m"), [{"role": "user", "content": "hi"}], PING_TOOLS))
                for _ in range(6)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
            assert stub.peak_in_flight == 2
    finally:
        cw_llm.configure(4)


def test_missing_usage_gives_estimated():
    with cw_testlib.StubLLM({"m": [cw_testlib.tool_call("ping", {}, usage=False)]}) as stub:
        profile = stub.profile("m")
        _message, usage = cw_llm.chat(profile, [{"role": "user", "content": "hi"}], PING_TOOLS)
        assert usage["estimated"] is True
        assert usage["prompt_tokens"] > 0


def test_run_tools_unknown_tool_then_bad_json_then_done():
    def script(_body, n):
        if n == 0:
            return cw_testlib.tool_call("mystery", {})
        if n == 1:
            message = {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "c2", "type": "function",
                                 "function": {"name": "known", "arguments": "{bad"}}],
            }
            return {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": message}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        return cw_testlib.tool_call("known", {"x": 1})

    with cw_testlib.StubLLM(script) as stub:
        profile = stub.profile("m")
        messages = [{"role": "user", "content": "go"}]
        tools = [{"type": "function", "function": {"name": "known", "parameters": {}}}]
        result = cw_llm.run_tools(
            profile, messages, tools, {"known": lambda args: cw_llm.Done({"ok": True, "args": args})}
        )
        assert result == {"ok": True, "args": {"x": 1}}

        tool_messages = [m for m in messages if m.get("role") == "tool"]
        assert any(m["content"] == "unknown tool mystery" for m in tool_messages)
        assert any(isinstance(m["content"], str) and m["content"].startswith("invalid JSON arguments")
                   for m in tool_messages)


def test_run_tools_nudges_until_a_tool_call_arrives():
    def script(_body, n):
        if n == 0:
            return cw_testlib.text("thinking out loud")
        return cw_testlib.tool_call("known", {})

    with cw_testlib.StubLLM(script) as stub:
        profile = stub.profile("m")
        messages = [{"role": "user", "content": "go"}]
        tools = [{"type": "function", "function": {"name": "known", "parameters": {}}}]
        result = cw_llm.run_tools(
            profile, messages, tools, {"known": lambda a: cw_llm.Done("done")},
            nudge="Reply only by calling one of the tools.",
        )
        assert result == "done"
        assert any(m.get("role") == "user" and m.get("content") == "Reply only by calling one of the tools."
                   for m in messages)


def test_run_tools_nudge_none_returns_text():
    with cw_testlib.StubLLM({"m": [cw_testlib.text("final answer")]}) as stub:
        profile = stub.profile("m")
        messages = [{"role": "user", "content": "go"}]
        result = cw_llm.run_tools(profile, messages, [], {}, nudge=None)
        assert result == "final answer"


def test_check_raises_no_tools_on_text_reply():
    with cw_testlib.StubLLM({"m": [cw_testlib.text("no tools for you")]}) as stub:
        profile = stub.profile("m")
        error = _expect_llm_error(lambda: cw_llm.check(profile))
        assert error.kind == "no_tools"


# --- claude-code backend (section 2A) ---------------------------------------


def test_claude_env_drops_session_vars_keeps_anthropic_and_home():
    os.environ["CLAUDE_CODE_SESSION_ID"] = "abc"
    os.environ["ANTHROPIC_API_KEY"] = "key"
    try:
        env = cw_llm.claude_env()
    finally:
        del os.environ["CLAUDE_CODE_SESSION_ID"]
        del os.environ["ANTHROPIC_API_KEY"]
    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert env.get("ANTHROPIC_API_KEY") == "key"
    assert "HOME" in env


def test_claude_call_large_prompt_survives_stdin():
    cw_testlib.require_posix()
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [cw_testlib.claude_result(result="ok")]}
        with cw_testlib.fake_claude(tmp, script) as fc:
            big = "x" * 200_000
            cw_llm.claude_call({"model": "sonnet"}, "sys", big, cwd=Path(tmp), tools="")
            assert fc.log()[-1]["stdin"] == big


def test_run_claude_tools_feedback_then_done_runs_two_processes():
    cw_testlib.require_posix()
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [
            cw_testlib.claude_result(structured={"text": "draft"}),
            cw_testlib.claude_result(structured={"text": "final"}),
        ]}
        with cw_testlib.fake_claude(tmp, script) as fc:
            profile = {"kind": "claude-code", "model": "sonnet"}
            tools = [{"type": "function", "function": {
                "name": "submit",
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
            }}]
            calls = {"n": 0}

            def handler(args):
                calls["n"] += 1
                if calls["n"] == 1:
                    return "needs more detail"
                return cw_llm.Done(args)

            messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
            result = cw_llm.run_tools(profile, messages, tools, {"submit": handler}, cwd=Path(tmp))
            assert result == {"text": "final"}
            assert fc.count("sonnet") == 2
            second_stdin = fc.log()[-1]["stdin"]
            assert "Your previous answer" in second_stdin
            assert "needs more detail" in second_stdin


def test_claude_call_usage_mapping_and_cost_none():
    cw_testlib.require_posix()
    with cw_testlib.temp_home() as tmp:
        usage = {"input_tokens": 10, "output_tokens": 5,
                  "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3}
        script = {"sonnet": [cw_testlib.claude_result(result="ok", usage=usage)]}
        with cw_testlib.fake_claude(tmp, script):
            profile = {"model": "sonnet"}
            _out, got = cw_llm.claude_call(profile, "sys", "go", cwd=Path(tmp), tools="")
            assert got == {"prompt_tokens": 15, "completion_tokens": 5, "estimated": False}
            assert cw_llm.cost_usd(profile, got) is None


def test_claude_call_error_kinds():
    cw_testlib.require_posix()
    not_found = cw_testlib.claude_result(is_error=True, result="bad model")
    not_found["stderr"] = "[claude-code:unrecognized_model] {}"
    script = {
        "auth": [cw_testlib.claude_result(is_error=True, result="no", api_error_status=401)],
        "rate": [cw_testlib.claude_result(is_error=True, result="rate limit hit", api_error_status=429)],
        "ctx": [cw_testlib.claude_result(is_error=True, result="Prompt is too long")],
        "nf": [not_found],
        "raw": [cw_testlib.claude_raw("not json", exit=1, stderr="boom")],
        "long": [cw_testlib.claude_result(is_error=True, result="z" * 500)],
    }
    with cw_testlib.temp_home() as tmp:
        with cw_testlib.fake_claude(tmp, script):
            def failure(model, **extra):
                return _expect_llm_error(lambda: cw_llm.claude_call(
                    {"model": model, **extra}, "sys", "go", cwd=Path(tmp), tools=""))

            assert failure("auth").kind == "auth"
            error = failure("rate")
            assert error.kind == "rate" and error.remedy is not None
            error = failure("ctx")
            assert error.kind == "context" and "ctx" in error.remedy
            error = failure("nf", name="prof-nf")
            assert error.kind == "notfound" and "prof-nf" in error.remedy
            assert failure("raw").kind == "protocol"
            error = failure("long")
            assert error.kind == "error" and len(str(error)) == 300


def test_claude_call_missing_binary_is_config():
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = "/nonexistent"
    try:
        error = _expect_llm_error(
            lambda: cw_llm.claude_call({"model": "sonnet"}, "sys", "go", cwd=Path("."), tools=""))
    finally:
        os.environ["PATH"] = old_path
    assert error.kind == "config"


def test_claude_call_missing_cwd_is_config():
    error = _expect_llm_error(
        lambda: cw_llm.claude_call({"model": "sonnet"}, "sys", "go", cwd="/no/such/dir", tools=""))
    assert error.kind == "config"


def test_claude_call_timeout_kills_the_process_group():
    cw_testlib.require_posix()
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [cw_testlib.claude_delayed(
            2, cw_testlib.claude_result(result="late"), spawn_child=True)]}
        with cw_testlib.fake_claude(tmp, script) as fc:
            start = time.time()
            error = _expect_llm_error(
                lambda: cw_llm.claude_call({"model": "sonnet"}, "sys", "go", cwd=Path(tmp),
                                            tools="", timeout=0.5))
            assert error.kind == "timeout"
            assert time.time() - start < 5
            pid = fc.child_pid()
            assert pid, "the fake never spawned its child"
            _assert_dies(pid)


def test_claude_call_concurrency_configure_one_serializes_calls():
    cw_testlib.require_posix()
    cw_llm.configure(1)
    try:
        with cw_testlib.temp_home() as tmp:
            script = {
                "a": [cw_testlib.claude_delayed(0.15, cw_testlib.claude_result(result="a"))],
                "b": [cw_testlib.claude_delayed(0.15, cw_testlib.claude_result(result="b"))],
            }
            with cw_testlib.fake_claude(tmp, script) as fc:
                def run(model):
                    cw_llm.claude_call({"model": model}, "sys", "go", cwd=Path(tmp), tools="")

                ta = threading.Thread(target=run, args=("a",))
                tb = threading.Thread(target=run, args=("b",))
                ta.start()
                time.sleep(0.05)
                tb.start()
                ta.join(timeout=10)
                tb.join(timeout=10)

                entries = {entry["model"]: entry for entry in fc.log()}
                # b's actual process can only start once a's claude_call releases the semaphore
                # slot, which happens right after a's process ends.
                assert entries["b"]["start"] >= entries["a"]["end"] - 0.05, entries
    finally:
        cw_llm.configure(4)


def test_check_claude_code_ok():
    cw_testlib.require_posix()
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [cw_testlib.claude_result(structured={"ok": True})]}
        with cw_testlib.fake_claude(tmp, script):
            cw_llm.check({"kind": "claude-code", "model": "sonnet"})


def test_check_claude_code_not_logged_in():
    cw_testlib.require_posix()
    with cw_testlib.temp_home() as tmp:
        with cw_testlib.fake_claude(tmp, {}, auth={"loggedIn": False, "authMethod": None,
                                                     "apiProvider": None}):
            error = _expect_llm_error(
                lambda: cw_llm.check({"kind": "claude-code", "model": "sonnet"}))
            assert error.kind == "auth"


def test_check_claude_code_null_structured_output():
    cw_testlib.require_posix()
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [cw_testlib.claude_result(structured=None)]}
        with cw_testlib.fake_claude(tmp, script):
            error = _expect_llm_error(
                lambda: cw_llm.check({"kind": "claude-code", "model": "sonnet"}))
            assert error.kind == "no_tools"


# --- thread turns -----------------------------------------------------------


def test_thread_argv_first_turn_and_follow_up():
    mcp = {"mcpServers": {"cw": {"args": ["cw_mcp.py", "outcomes", "--dir", "/d", "--qid", "q1"]}}}
    argv = cw_llm.thread_argv({"model": "sonnet"}, "sys", mcp_config=mcp, add_dir="/d/ctx",
                              session_id="uuid-1")
    assert argv[argv.index("--session-id") + 1] == "uuid-1"
    assert "--resume" not in argv
    for flag in ("--restricted", "--strict-mcp-config", "--verbose", "--include-partial-messages",
                 "--allowedTools=mcp__cw__propose_resolve,mcp__cw__propose_page_edit,mcp__cw__propose_github_draft,mcp__cw__propose_task", "--add-dir=/d/ctx", "--model=sonnet"):
        assert flag in argv
    assert "stream-json" in argv
    assert "--safe-mode" not in argv and "--no-session-persistence" not in argv
    config = next(a for a in argv if a.startswith("--mcp-config="))
    assert json.loads(config.split("=", 1)[1]) == mcp
    follow = cw_llm.thread_argv({"model": "sonnet"}, "sys", mcp_config=mcp, add_dir="/d/ctx", resume="sid-9")
    assert follow[follow.index("--resume") + 1] == "sid-9"
    assert "--session-id" not in follow


def test_claude_call_argv_is_pinned():
    cw_testlib.require_posix()
    base = ["-p", "--safe-mode", "--restricted", "--tools=Read", "--strict-mcp-config",
            "--no-session-persistence", "--permission-prompts", "none", "--model=sonnet",
            "--output-format", "json", "--system-prompt=sys"]
    schema = {"type": "object"}
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [cw_testlib.claude_result(result="a")] * 2}
        with cw_testlib.fake_claude(tmp, script) as fc:
            cw_llm.claude_call({"model": "sonnet"}, "sys", "p", cwd=Path(tmp), tools="Read")
            cw_llm.claude_call({"model": "sonnet"}, "sys", "p", cwd=Path(tmp), tools="Read", schema=schema)
            log = fc.log()
            assert log[-2]["cwd"] == str(tmp) and log[-2]["stdin"] == "p"
            assert log[-2]["argv"] == base
            assert log[-1]["argv"] == base + [f"--json-schema={json.dumps(schema)}"]


def _assert_dies(pid):
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise AssertionError("child survived the group kill")


def _script(tmp_path, body):
    path = tmp_path / "fake"
    path.write_text("#!" + sys.executable + "\nimport sys, time, os, json, subprocess\n" + body)
    path.chmod(0o755)
    return [str(path)]


def test_claude_stream_incremental_ordered_skips_non_json_and_calls_on_spawn(tmp_path):
    cw_testlib.require_posix()
    argv = _script(tmp_path, """
sys.stdin.read()
for i in range(3):
    print(json.dumps({"i": i}), flush=True)
    print("not json", flush=True)
    print("5", flush=True)
    print('"str"', flush=True)
    time.sleep(0.15)
""")
    got, spawned = [], []
    rc, _err = cw_llm.claude_stream(
        argv, "hi", cwd=tmp_path, timeout=10,
        on_line=lambda o: got.append((o["i"], time.monotonic())), on_spawn=spawned.append,
    )
    assert rc == 0
    assert [i for i, _ in got] == [0, 1, 2]
    assert got[1][1] - got[0][1] > 0.07 and got[2][1] - got[1][1] > 0.07
    assert len(spawned) == 1 and spawned[0].pid > 0


def test_claude_stream_timeout_kills_whole_group(tmp_path):
    cw_testlib.require_posix()
    pidfile = tmp_path / "child.pid"
    argv = _script(tmp_path, f"""
child = subprocess.Popen(["sleep", "60"])
open({str(pidfile)!r}, "w").write(str(child.pid))
print(json.dumps({{"up": 1}}), flush=True)
time.sleep(60)
""")
    error = _expect_llm_error(lambda: cw_llm.claude_stream(
        argv, "", cwd=tmp_path, timeout=0.6, on_line=lambda o: None))
    assert error.kind == "timeout"
    _assert_dies(int(pidfile.read_text()))


def test_reply_is_not_an_allowed_tool_of_a_turn():
    argv = cw_llm.thread_argv({"model": "sonnet"}, "SYS", mcp_config={"mcpServers": {}}, add_dir="/d", session_id="u")
    assert not any("walkthrough_reply" in a for a in argv)
