#!/usr/bin/env python3
"""Self-check for cw_testlib.fake_claude. Assert-based, no framework; also collected by pytest.
Proves the fake itself behaves before any batch-2 module relies on it: argv/model-keyed entries,
stdin capture, env_keys, auth status, and a spawn_child pid surviving in the log."""

import os
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


if __name__ == "__main__":
    tests = [
        test_model_keyed_entries_advance_per_call,
        test_stdin_and_argv_and_env_keys_logged,
        test_claude_raw_gives_non_json_stdout,
        test_auth_status_reports_configured_login,
        test_spawn_child_pid_is_logged_and_dies_with_group,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
