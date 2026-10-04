#!/usr/bin/env python3
"""Self-check for cw_llm.py. Assert-based, no framework; also collected by pytest. Uses the
loopback StubLLM from cw_testlib, never a real network. BACKOFF_S is zeroed so the retry tests
stay fast."""

import os
import socket
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
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
