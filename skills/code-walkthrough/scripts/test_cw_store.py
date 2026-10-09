#!/usr/bin/env python3
"""Self-check for cw_store.py. Assert-based, no framework; also collected by pytest."""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_store  # noqa: E402
import cw_testlib  # noqa: E402

SCRIPTS_DIR = str(Path(__file__).parent)


def test_walkthrough_dir_rejects_dotdot_and_bad_id():
    with cw_testlib.temp_home():
        bad = [("..", "cmp-01234567"), ("../x", "cmp-01234567"), ("myrepo-abcdef", "not-an-id")]
        for key, wid in bad:
            try:
                cw_store.walkthrough_dir(key, wid)
            except ValueError:
                continue
            raise AssertionError(f"expected ValueError for {key!r}/{wid!r}")


def test_walkthrough_dir_accepts_valid_ids():
    with cw_testlib.temp_home():
        d = cw_store.walkthrough_dir("myrepo-abcdef", "cmp-01234567", create=True)
        assert d.is_dir()
        assert d.name == "cmp-01234567"
        assert cw_store.store_root().resolve() in d.parents


def test_repo_key_sanitizes():
    key = cw_store.repo_key("/home/me/My Repo!!")
    assert key.startswith("My_Repo__-"), key
    digest = key.split("-")[-1]
    assert len(digest) == 6
    assert all(c in "0123456789abcdef" for c in digest)


def test_write_json_mode():
    with cw_testlib.temp_home() as home:
        path = home / "x.json"
        cw_store.write_json(path, {"a": 1}, mode=0o600)
        assert (path.stat().st_mode & 0o777) == 0o600
        assert cw_store.read_json(path) == {"a": 1}


def test_acquire_daemon_lock_second_in_child_process_returns_none():
    with cw_testlib.temp_home() as home:
        held = cw_store.acquire_daemon_lock()
        assert held is not None
        try:
            script = (
                f"import sys; sys.path.insert(0, {SCRIPTS_DIR!r}); "
                "import cw_store; print(cw_store.acquire_daemon_lock())"
            )
            env = {**os.environ, "CODE_WALKTHROUGH_HOME": str(home)}
            result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
            assert result.stdout.strip() == "None", result.stdout + result.stderr
        finally:
            held.close()


def test_dead_pid_lock_file_with_no_flock_is_acquired():
    with cw_testlib.temp_home():
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        dead_pid = proc.pid
        proc.wait()
        cw_store.lock_path().write_text(str(dead_pid))

        f = cw_store.acquire_daemon_lock()
        assert f is not None
        f.close()


def test_role_profile_escalate_falls_back_to_prose():
    config = {
        "profiles": {"p": {"base_url": "http://x", "model": "m"}},
        "roles": {"prose": "p"},
    }
    profile = cw_store.role_profile(config, "escalate")
    assert profile["name"] == "p"
    assert profile["base_url"] == "http://x"
    assert cw_store.role_profile(config, "analysis") is None


def test_role_profile_thread_falls_back_to_ask():
    config = {
        "profiles": {"p": {"base_url": "http://x", "model": "m"}},
        "roles": {"ask": "p"},
    }
    assert cw_store.role_profile(config, "thread")["name"] == "p"
    config["roles"]["thread"] = "q"
    config["profiles"]["q"] = {"base_url": "http://y", "model": "n"}
    assert cw_store.role_profile(config, "thread")["name"] == "q"


def test_load_config_defaults():
    with cw_testlib.temp_home():
        config = cw_store.load_config()
        assert config == cw_store.DEFAULTS
        assert config is not cw_store.DEFAULTS


def test_role_profile_claude_code_without_base_url_passes():
    config = {
        "profiles": {"c": {"kind": "claude-code", "model": "sonnet"}},
        "roles": {"analysis": "c"},
    }
    profile = cw_store.role_profile(config, "analysis")
    assert profile["name"] == "c"
    assert profile["model"] == "sonnet"
    assert "base_url" not in profile


def test_role_profile_unknown_kind_refused_with_remedy():
    with cw_testlib.temp_home():
        config = {
            "profiles": {"c": {"kind": "bogus", "model": "sonnet"}},
            "roles": {"analysis": "c"},
        }
        try:
            cw_store.role_profile(config, "analysis")
        except cw_store.CWError as e:
            assert "unknown kind" in str(e)
            assert e.remedy and "openai or claude-code" in e.remedy
        else:
            raise AssertionError("expected CWError for unknown kind")


if __name__ == "__main__":
    tests = [
        test_walkthrough_dir_rejects_dotdot_and_bad_id,
        test_walkthrough_dir_accepts_valid_ids,
        test_repo_key_sanitizes,
        test_write_json_mode,
        test_acquire_daemon_lock_second_in_child_process_returns_none,
        test_dead_pid_lock_file_with_no_flock_is_acquired,
        test_role_profile_escalate_falls_back_to_prose,
        test_load_config_defaults,
        test_role_profile_claude_code_without_base_url_passes,
        test_role_profile_unknown_kind_refused_with_remedy,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
