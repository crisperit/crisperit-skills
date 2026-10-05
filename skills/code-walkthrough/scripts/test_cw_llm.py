#!/usr/bin/env python3
"""Self-check for cw_llm.py. Assert-based, no framework; also collected by pytest. Uses the
loopback StubLLM from cw_testlib, never a real network. BACKOFF_S is zeroed so the retry tests
stay fast."""

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_llm  # noqa: E402
import cw_testlib  # noqa: E402

cw_llm.BACKOFF_S = [0, 0]

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


def test_context_length_400_does_not_retry():
    with cw_testlib.StubLLM({"m": [cw_testlib.http_error(400, body="context_length_exceeded: too long")]}) as stub:
        profile = stub.profile("m")
        error = _expect_llm_error(lambda: cw_llm.chat(profile, [{"role": "user", "content": "hi"}], PING_TOOLS))
        assert error.kind == "context"
        assert "m" in error.remedy
        assert stub.count("m") == 1


def test_401_remedy_names_env_var():
    with cw_testlib.StubLLM({"m": [cw_testlib.http_error(401)]}) as stub:
        profile = stub.profile("m", api_key_env="MY_KEY")
        os.environ["MY_KEY"] = "secret"
        try:
            error = _expect_llm_error(lambda: cw_llm.chat(profile, [{"role": "user", "content": "hi"}], PING_TOOLS))
        finally:
            del os.environ["MY_KEY"]
        assert error.kind == "auth"
        assert "MY_KEY" in error.remedy


def test_404_remedy_names_profile():
    with cw_testlib.StubLLM({"m": [cw_testlib.http_error(404)]}) as stub:
        profile = stub.profile("m", name="m")
        error = _expect_llm_error(lambda: cw_llm.chat(profile, [{"role": "user", "content": "hi"}], PING_TOOLS))
        assert error.kind == "notfound"
        assert "m" in error.remedy


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


def test_retry_after_sleep_holds_no_semaphore_slot():
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

            assert results["b"] < 1.5, results
            assert results["a"] >= 2.0, results
    finally:
        cw_llm.configure(4)


def test_peak_in_flight_matches_configured_concurrency():
    cw_llm.configure(2)
    try:
        with cw_testlib.StubLLM({"m": [cw_testlib.tool_call("ping", {})]}, delay=0.2) as stub:
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


def test_claude_call_argv_no_positional_stdin_and_cwd():
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [cw_testlib.claude_result(result="hi")]}
        with cw_testlib.fake_claude(tmp, script) as fc:
            cwd_dir = Path(tmp) / "work"
            cwd_dir.mkdir()
            cw_llm.claude_call({"model": "sonnet"}, "sys prompt", "the prompt",
                                cwd=cwd_dir, tools="Read,Grep")
            entry = fc.log()[-1]
            assert entry["cwd"] == str(cwd_dir)
            assert entry["stdin"] == "the prompt"
            argv = entry["argv"]
            assert "--tools=Read,Grep" in argv
            assert "--model=sonnet" in argv
            assert "--system-prompt=sys prompt" in argv
            assert "--output-format" in argv and "json" in argv
            assert "the prompt" not in argv  # prompt travels on stdin, never as a positional


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
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [cw_testlib.claude_result(result="ok")]}
        with cw_testlib.fake_claude(tmp, script) as fc:
            big = "x" * 200_000
            cw_llm.claude_call({"model": "sonnet"}, "sys", big, cwd=Path(tmp), tools="")
            assert fc.log()[-1]["stdin"] == big


def test_claude_call_text_mode_has_no_schema_flag():
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [cw_testlib.claude_result(result="the answer")]}
        with cw_testlib.fake_claude(tmp, script) as fc:
            out, _usage = cw_llm.claude_call({"model": "sonnet"}, "sys", "go", cwd=Path(tmp), tools="")
            assert out["result"] == "the answer"
            assert not any(a.startswith("--json-schema") for a in fc.log()[-1]["argv"])


def test_run_claude_tools_feedback_then_done_runs_two_processes():
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
    with cw_testlib.temp_home() as tmp:
        usage = {"input_tokens": 10, "output_tokens": 5,
                  "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3}
        script = {"sonnet": [cw_testlib.claude_result(result="ok", usage=usage)]}
        with cw_testlib.fake_claude(tmp, script):
            profile = {"model": "sonnet"}
            _out, got = cw_llm.claude_call(profile, "sys", "go", cwd=Path(tmp), tools="")
            assert got == {"prompt_tokens": 15, "completion_tokens": 5, "estimated": False}
            assert cw_llm.cost_usd(profile, got) is None


def test_claude_call_error_kind_auth_401():
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [cw_testlib.claude_result(is_error=True, result="no",
                                                         api_error_status=401)]}
        with cw_testlib.fake_claude(tmp, script):
            error = _expect_llm_error(
                lambda: cw_llm.claude_call({"model": "sonnet"}, "sys", "go", cwd=Path(tmp), tools=""))
            assert error.kind == "auth"


def test_claude_call_error_kind_rate_429():
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [cw_testlib.claude_result(is_error=True, result="rate limit hit",
                                                         api_error_status=429)]}
        with cw_testlib.fake_claude(tmp, script):
            error = _expect_llm_error(
                lambda: cw_llm.claude_call({"model": "sonnet"}, "sys", "go", cwd=Path(tmp), tools=""))
            assert error.kind == "rate"
            assert error.remedy is not None


def test_claude_call_error_kind_context_prompt_too_long():
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [cw_testlib.claude_result(is_error=True, result="Prompt is too long")]}
        with cw_testlib.fake_claude(tmp, script):
            error = _expect_llm_error(
                lambda: cw_llm.claude_call({"model": "sonnet"}, "sys", "go", cwd=Path(tmp), tools=""))
            assert error.kind == "context"
            assert error.remedy is not None and "sonnet" in error.remedy


def test_claude_call_error_kind_notfound_from_stderr_marker():
    with cw_testlib.temp_home() as tmp:
        entry = cw_testlib.claude_result(is_error=True, result="bad model")
        entry["stderr"] = "[claude-code:unrecognized_model] {}"
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}):
            error = _expect_llm_error(
                lambda: cw_llm.claude_call({"model": "sonnet", "name": "r"}, "sys", "go",
                                            cwd=Path(tmp), tools=""))
            assert error.kind == "notfound"
            assert "r" in error.remedy


def test_claude_call_non_json_stdout_is_protocol():
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [cw_testlib.claude_raw("not json", exit=1, stderr="boom")]}
        with cw_testlib.fake_claude(tmp, script):
            error = _expect_llm_error(
                lambda: cw_llm.claude_call({"model": "sonnet"}, "sys", "go", cwd=Path(tmp), tools=""))
            assert error.kind == "protocol"


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


def test_claude_call_unknown_error_message_truncated_to_300():
    with cw_testlib.temp_home() as tmp:
        long_text = "z" * 500
        script = {"sonnet": [cw_testlib.claude_result(is_error=True, result=long_text)]}
        with cw_testlib.fake_claude(tmp, script):
            error = _expect_llm_error(
                lambda: cw_llm.claude_call({"model": "sonnet"}, "sys", "go", cwd=Path(tmp), tools=""))
            assert error.kind == "error"
            assert len(str(error)) == 300


def test_claude_call_timeout_kills_the_process_group():
    # The fake claude writes its log entry only after its own delay elapses, so a child killed
    # mid-delay never lands in fc.log(); watch for the spawned "sleep 30" by pid instead.
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [cw_testlib.claude_delayed(
            2, cw_testlib.claude_result(result="late"), spawn_child=True)]}
        with cw_testlib.fake_claude(tmp, script):
            seen = {}

            def watch():
                deadline = time.time() + 2
                while time.time() < deadline and "pid" not in seen:
                    out = subprocess.run(["pgrep", "-f", "^sleep 30$"], capture_output=True, text=True)
                    pids = [p for p in out.stdout.split() if p]
                    if pids:
                        seen["pid"] = pids[-1]
                        return
                    time.sleep(0.02)

            watcher = threading.Thread(target=watch)
            watcher.start()
            start = time.time()
            error = _expect_llm_error(
                lambda: cw_llm.claude_call({"model": "sonnet"}, "sys", "go", cwd=Path(tmp),
                                            tools="", timeout=0.3))
            elapsed = time.time() - start
            watcher.join(timeout=2)
            assert error.kind == "timeout"
            assert elapsed < 5
            assert "pid" in seen, "never observed the spawned child"
            time.sleep(0.2)
            result = subprocess.run(["kill", "-0", seen["pid"]])
            assert result.returncode != 0  # child died with the group


def test_claude_call_concurrency_configure_one_serializes_calls():
    cw_llm.configure(1)
    try:
        with cw_testlib.temp_home() as tmp:
            script = {
                "a": [cw_testlib.claude_delayed(0.3, cw_testlib.claude_result(result="a"))],
                "b": [cw_testlib.claude_delayed(0.3, cw_testlib.claude_result(result="b"))],
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
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [cw_testlib.claude_result(structured={"ok": True})]}
        with cw_testlib.fake_claude(tmp, script):
            cw_llm.check({"kind": "claude-code", "model": "sonnet"})


def test_check_claude_code_not_logged_in():
    with cw_testlib.temp_home() as tmp:
        with cw_testlib.fake_claude(tmp, {}, auth={"loggedIn": False, "authMethod": None,
                                                     "apiProvider": None}):
            error = _expect_llm_error(
                lambda: cw_llm.check({"kind": "claude-code", "model": "sonnet"}))
            assert error.kind == "auth"


def test_check_claude_code_null_structured_output():
    with cw_testlib.temp_home() as tmp:
        script = {"sonnet": [cw_testlib.claude_result(structured=None)]}
        with cw_testlib.fake_claude(tmp, script):
            error = _expect_llm_error(
                lambda: cw_llm.check({"kind": "claude-code", "model": "sonnet"}))
            assert error.kind == "no_tools"


if __name__ == "__main__":
    tests = [
        test_server_error_retries_three_times_then_fails,
        test_context_length_400_does_not_retry,
        test_401_remedy_names_env_var,
        test_404_remedy_names_profile,
        test_closed_port_gives_connect,
        test_header_env_expansion_and_refusal_when_unset,
        test_token_cap_raises_on_second_round,
        test_rounds_cap_raises,
        test_retry_after_sleep_holds_no_semaphore_slot,
        test_peak_in_flight_matches_configured_concurrency,
        test_missing_usage_gives_estimated,
        test_run_tools_unknown_tool_then_bad_json_then_done,
        test_run_tools_nudges_until_a_tool_call_arrives,
        test_run_tools_nudge_none_returns_text,
        test_check_raises_no_tools_on_text_reply,
        test_claude_call_argv_no_positional_stdin_and_cwd,
        test_claude_env_drops_session_vars_keeps_anthropic_and_home,
        test_claude_call_large_prompt_survives_stdin,
        test_claude_call_text_mode_has_no_schema_flag,
        test_run_claude_tools_feedback_then_done_runs_two_processes,
        test_claude_call_usage_mapping_and_cost_none,
        test_claude_call_error_kind_auth_401,
        test_claude_call_error_kind_rate_429,
        test_claude_call_error_kind_context_prompt_too_long,
        test_claude_call_error_kind_notfound_from_stderr_marker,
        test_claude_call_non_json_stdout_is_protocol,
        test_claude_call_missing_binary_is_config,
        test_claude_call_missing_cwd_is_config,
        test_claude_call_unknown_error_message_truncated_to_300,
        test_claude_call_timeout_kills_the_process_group,
        test_claude_call_concurrency_configure_one_serializes_calls,
        test_check_claude_code_ok,
        test_check_claude_code_not_logged_in,
        test_check_claude_code_null_structured_output,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
