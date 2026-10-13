import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import cw_agent  # noqa: E402
import cw_server  # noqa: E402
import cw_store  # noqa: E402
import cw_task  # noqa: E402
import cw_testlib  # noqa: E402


def test_trust_levels(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("trust must not spawn a process")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    meta = {"gh_repo": "Acme/Widgets"}
    assert cw_agent.trust(meta, {}) is False
    assert cw_agent.trust(meta, {"trust": {"repos": [], "orgs": []}}) is False
    assert cw_agent.trust(meta, {"trust": {"repos": ["acme/widgets"]}}) is True
    assert cw_agent.trust(meta, {"trust": {"orgs": ["ACME"]}}) is True
    assert cw_agent.trust(meta, {"trust": {"repos": ["acme/other"], "orgs": ["other"]}}) is False
    assert cw_agent.trust({**meta, "own_pr": True}, {}) is True
    assert cw_agent.trust({**meta, "own_pr": False}, {}) is False
    sibling = {**meta, "own_pr": True, "sibling_of": {"key": "k"}}
    assert cw_agent.trust(sibling, {"trust": {"repos": ["acme/widgets"]}}) is False


def test_live_payload_agent_defaults_off(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_WALKTHROUGH_HOME", str(tmp_path / "home"))
    live = cw_server._live_payload("k", "w", {"gh_repo": "a/b"}, tmp_path, "tok", 1)
    assert live["agent"] == {"full": False, "own_pr": None}
    live = cw_server._live_payload("k", "w", {"gh_repo": "a/b", "own_pr": True}, tmp_path, "tok", 1)
    assert live["agent"] == {"full": True, "own_pr": True}


def test_repo_name_reads_origin_for_non_pr_targets(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "git@github.com:Acme/Widgets.git"],
                   check=True)
    meta = {"repo": str(tmp_path)}
    assert cw_agent.repo_name(meta) == "Acme/Widgets"
    assert cw_agent.repo_name({**meta, "gh_repo": "x/y"}) == "x/y"
    assert cw_agent.trust(meta, {"trust": {"repos": ["acme/widgets"]}}) is True
    assert cw_agent.trust(meta, {}) is False


def _agent_fixture(tmp):
    cw_testlib.require_git((2, 31))
    repo, _base, head = cw_testlib.make_repo(tmp, {"a.py": "x = 1\n"}, {"a.py": "x = 2\n"})
    d = Path(tmp) / "wd"
    d.mkdir()
    marker = Path(tmp) / "hook-ran"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    hook.chmod(0o755)
    meta = {"id": "cmp-12345678", "repo": str(repo), "head": head}
    cw_store.write_json(d / "meta.json", meta)
    return d, repo, meta, marker


def test_worktree_is_lazy_shared_and_hardened():
    with tempfile.TemporaryDirectory() as tmp:
        d, repo, meta, marker = _agent_fixture(tmp)
        os.symlink("a.py", repo / "link")
        cw_testlib.git(repo, "add", "link")
        cw_testlib.git(repo, "commit", "-q", "--no-verify", "-m", "link")
        meta["head"] = cw_testlib.git(repo, "rev-parse", "HEAD").strip()
        assert not (d / "agent").exists() and "agent" not in meta
        wt = cw_agent.ensure_worktree(d, meta)
        assert wt == d / "agent" and cw_agent.ensure_worktree(d, meta) == wt
        assert meta["agent"]["branch"] == "cw/cmp-12345678/agent"
        assert cw_store.read_meta(d)["agent"] == meta["agent"]
        assert not (wt / "link").is_symlink()
        (wt / "a.py").write_text("x = 3\n")
        assert cw_agent.commit(d, meta, "t", "q-1")["files"] == ["a.py"]
        assert not marker.exists()


def test_commit_is_none_when_clean_and_counts_agent_made_commits():
    with tempfile.TemporaryDirectory() as tmp:
        d, repo, meta, _marker = _agent_fixture(tmp)
        wt = cw_agent.ensure_worktree(d, meta)
        assert cw_agent.commit(d, meta, "t", "q-1") is None
        (wt / "a.py").write_text("x = 3\n")
        (wt / "b.txt").write_text("hi\n")
        first = cw_agent.commit(d, meta, "t", "q-1")
        assert first["parent"] == meta["head"] and first["branch"] == meta["agent"]["branch"]
        assert first["stat"] == {"a.py": "+1 -1", "b.txt": "+1 -0"} and meta["agent"]["sha"] == first["sha"]
        assert cw_store.read_meta(d)["agent"]["sha"] == first["sha"]
        assert cw_agent.commit(d, meta, "t", "q-2") is None
        (wt / "c.txt").write_text("c\n")
        cw_testlib.git(wt, "add", "-A")
        cw_testlib.git(wt, "commit", "-q", "--no-verify", "-m", "by the agent")
        second = cw_agent.commit(d, meta, "t", "q-3")
        assert second["parent"] == first["sha"] and second["files"] == ["c.txt"]


def test_commit_refuses_a_repointed_link_or_a_branch_switch():
    with tempfile.TemporaryDirectory() as tmp:
        d, repo, meta, _marker = _agent_fixture(tmp)
        wt = cw_agent.ensure_worktree(d, meta)
        (wt / "a.py").write_text("x = 3\n")
        cw_testlib.git(wt, "checkout", "-q", "-b", "elsewhere")
        with pytest.raises(cw_agent.BranchChanged):
            cw_agent.commit(d, meta, "t", "q-1")
        cw_testlib.git(wt, "checkout", "-q", meta["agent"]["branch"])
        link = (wt / ".git").read_text()
        (wt / ".git").write_text(f"gitdir: {tmp}/elsewhere\n")
        with pytest.raises(cw_task.GitLinkChanged):
            cw_agent.commit(d, meta, "t", "q-1")
        (wt / ".git").write_text(link)
        assert cw_agent.commit(d, meta, "t", "q-1")["files"] == ["a.py"]
