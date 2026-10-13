import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import cw_agent  # noqa: E402
import cw_ask  # noqa: E402
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
    monkeypatch.setattr(cw_server.cw_llm, "confinement_available", lambda: False)
    live = cw_server._live_payload("k", "w", {"gh_repo": "a/b", "own_pr": True}, tmp_path, "tok", 1)
    assert live["agent"] == {"full": False, "own_pr": True}


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


# -- Full turns end to end -------------------------------------------------

def _full_fixture(tmp, home, **meta):
    import test_cw_ask as ask

    cw_testlib.require_git((2, 31))
    repo, _base, head = cw_testlib.make_repo(tmp, {"a.py": "x = 1\n"}, {"a.py": "x = 2\n"})
    d = ask._claude_done(home)
    cw_store.update_meta(d, lambda m: m.update(repo=str(repo), head=head, **{"own_pr": True, **meta}))
    return ask, d, repo


def _comment(ask, daemon, d, mode=None, thread_id=None):
    body = {"anchor": ask.ANCHOR_SECTION, "text": "fix it"}
    if mode:
        body["mode"] = mode
    if thread_id:
        body["thread_id"] = thread_id
    return cw_testlib.request(daemon, "POST", f"/api/walkthrough/{d.parent.name}/{d.name}/comment",
                              token=daemon.token, body=json.dumps(body))


def test_full_turn_edits_only_the_shared_worktree_and_records_edits_and_denials():
    denial = {"tool_name": "Bash", "tool_input": {"command": "curl evil.example"}}
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        ask, d, repo = _full_fixture(tmp, home)
        entry = cw_testlib.claude_stream_entry(["done"], writes={"a.py": "x = 3\n"}, denials=[denial])
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}) as fc, cw_testlib.running_daemon() as daemon:
            status, raw = _comment(ask, daemon, d, "full")
            assert status == 202, raw
            ask._wait_final(d, 1)
            status, raw = cw_testlib.request(
                daemon, "GET", f"/api/walkthrough/{d.parent.name}/{d.name}/qa", token=daemon.token)
        argv = fc.log()[-1]["argv"]
        assert Path(fc.log()[-1]["cwd"]).resolve() == (d / "agent").resolve()
        assert argv[argv.index("--setting-sources") + 1] == "user"
        turn = json.loads(raw)["turns"][0]
        assert turn["status"] == "ok" and turn["mode"] == "full"
        assert turn["denied"] == [{"tool": "Bash", "detail": "curl evil.example"}]
        edits = next(o for o in cw_ask.read_outcomes(d) if o["outcome"] == "edits")
        assert edits["state"] == "done" and edits["payload"]["files"] == ["a.py"]
        assert edits["payload"]["branch"] == cw_store.read_meta(d)["agent"]["branch"]
        assert (repo / "a.py").read_text() == "x = 2\n"
        assert cw_testlib.git(repo, "status", "--porcelain") == ""


def test_full_refusals():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        ask, d, _repo = _full_fixture(tmp, home, own_pr=False)
        with cw_testlib.running_daemon() as daemon:
            status, raw = _comment(ask, daemon, d, "full")
            assert (status, json.loads(raw)["error"]) == (403, "Full access is off for this walkthrough")
            assert json.loads(raw)["remedy"] == "add owner/name to trust.repos in config.json"
            assert _comment(ask, daemon, d, "root")[0] == 400
            cw_store.update_meta(d, lambda m: m.update(own_pr=True, sibling_of={"key": "k", "id": "w"}))
            assert _comment(ask, daemon, d, "full")[0] == 409
            cw_store.update_meta(d, lambda m: m.pop("sibling_of"))
            cw_testlib.write_config(home, {"k": {"kind": "openai", "model": "m", "base_url": "http://x"}},
                                    {"thread": "k"})
            status, raw = _comment(ask, daemon, d, "full")
            assert (status, json.loads(raw)["remedy"]) == (400, cw_task.REMEDY)
        assert cw_ask.read_qa(d) == []


def test_full_refused_without_confinement(monkeypatch):
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        ask, d, _repo = _full_fixture(tmp, home)
        monkeypatch.setattr(cw_server.cw_llm, "confinement_available", lambda: False)
        with cw_testlib.running_daemon() as daemon:
            status, raw = _comment(ask, daemon, d, "full")
            assert status == 400
            assert json.loads(raw)["error"] == "Full access needs bubblewrap with overlayfs, which this machine lacks"
            assert json.loads(raw)["remedy"] == "install bubblewrap (Linux only); Review mode still works"
            cw_store.update_meta(d, lambda m: m.update(own_pr=False))
            assert _comment(ask, daemon, d, "full")[0] == 403
        assert cw_ask.read_qa(d) == []


def test_modes_keep_separate_sessions():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        ask, d, _repo = _full_fixture(tmp, home)
        script = {"sonnet": [cw_testlib.claude_stream_entry(["r1"], session_id=f"s-{n}") for n in "rfRF"]}
        with cw_testlib.fake_claude(tmp, script) as fc, cw_testlib.running_daemon() as daemon:
            tid = None
            for n, mode in enumerate(["review", "full", "review", "full"], 1):
                status, raw = _comment(ask, daemon, d, mode, tid)
                assert status == 202, raw
                tid = tid or json.loads(raw)["thread_id"]
                ask._wait_final(d, n)
        flags = [("--resume" in e["argv"] and e["argv"][e["argv"].index("--resume") + 1]) for e in fc.log()]
        assert flags == [False, False, "s-r", "s-f"]


def test_old_records_read_as_review(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_WALKTHROUGH_HOME", str(tmp_path))
    (tmp_path / "qa.jsonl").write_text(json.dumps({"qid": "q-1", "question": "why", "status": "ok"}) + "\n")
    assert cw_ask.read_qa(tmp_path)[0]["mode"] == "review"


def _full_turn_with_edits(ask, daemon, d, tmp):
    status, raw = _comment(ask, daemon, d, "full")
    assert status == 202, raw
    ask._wait_final(d, 1)
    return next(o for o in cw_ask.read_outcomes(d) if o["outcome"] == "edits")


def _discard(daemon, d, oid):
    return cw_testlib.request(daemon, "POST", f"/api/walkthrough/{d.parent.name}/{d.name}/outcomes/{oid}/discard",
                              token=daemon.token, body="{}")


def test_discard_all_removes_worktree_branch_and_marks_edits_discarded():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        ask, d, repo = _full_fixture(tmp, home)
        entry = cw_testlib.claude_stream_entry(["done"], writes={"a.py": "x = 3\n"})
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}), cw_testlib.running_daemon() as daemon:
            edits = _full_turn_with_edits(ask, daemon, d, tmp)
            branch = cw_store.read_meta(d)["agent"]["branch"]
            assert (d / "agent").exists()
            status, raw = _discard(daemon, d, edits["oid"])
        assert status == 200, raw
        assert json.loads(raw)["outcome"]["state"] == "discarded"
        assert not (d / "agent").exists() and "agent" not in cw_store.read_meta(d)
        assert cw_testlib.git(repo, "branch", "--list", branch).strip() == ""
        assert cw_testlib.git(repo, "status", "--porcelain") == "" and (repo / "a.py").read_text() == "x = 2\n"


def test_discard_refused_while_a_full_turn_runs():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        ask, d, repo = _full_fixture(tmp, home)
        entry = cw_testlib.claude_stream_entry(["done"], writes={"a.py": "x = 3\n"})
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}), cw_testlib.running_daemon() as daemon:
            edits = _full_turn_with_edits(ask, daemon, d, tmp)
            lock = daemon.edit_lock(d)
            lock.acquire()
            try:
                status, raw = _discard(daemon, d, edits["oid"])
            finally:
                lock.release()
        assert status == 409 and "Full turn is running" in json.loads(raw)["error"]
        assert (d / "agent").exists() and "agent" in cw_store.read_meta(d)
        assert next(o for o in cw_ask.read_outcomes(d) if o["oid"] == edits["oid"])["state"] == "done"


def test_ensure_worktree_readopts_or_recreates_a_leftover_directory():
    with tempfile.TemporaryDirectory() as tmp:
        d, repo, meta, _marker = _agent_fixture(tmp)
        wt = cw_agent.ensure_worktree(d, meta)
        (wt / "a.py").write_text("x = 3\n")
        first = cw_agent.commit(d, meta, "t", "q-1")
        for m in (meta, ):
            m.pop("agent")
        cw_store.update_meta(d, lambda m: m.pop("agent"))
        assert cw_agent.ensure_worktree(d, meta) == wt
        assert meta["agent"]["sha"] == first["sha"] and cw_store.read_meta(d)["agent"]["sha"] == first["sha"]
        meta.pop("agent")
        cw_testlib.git(wt, "checkout", "-q", "-b", "elsewhere")
        assert cw_agent.ensure_worktree(d, meta) == wt
        assert cw_testlib.git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == "cw/cmp-12345678/agent"


def test_rebuild_clean_removes_the_agent_worktree_and_branch():
    import cw_run

    with tempfile.TemporaryDirectory() as tmp:
        d, repo, meta, _marker = _agent_fixture(tmp)
        cw_agent.ensure_worktree(d, meta)
        cw_run._full_clean(d, cw_store.read_meta(d), repo)
        assert not (d / "agent").exists()
        assert cw_testlib.git(repo, "branch", "--list", "cw/cmp-12345678/agent").strip() == ""
        cw_agent.ensure_worktree(d, {k: v for k, v in meta.items() if k != "agent"})


def test_commit_ignores_fsmonitor_and_filters_the_agent_configured():
    with tempfile.TemporaryDirectory() as tmp:
        d, repo, meta, _marker = _agent_fixture(tmp)
        wt = cw_agent.ensure_worktree(d, meta)
        fs, flt = Path(tmp) / "fsmonitor-ran", Path(tmp) / "filter-ran"
        cw_testlib.git(repo, "config", "core.fsmonitor", f"touch {fs}; echo")
        cw_testlib.git(repo, "config", "filter.x.clean", f"touch {flt}; cat")
        cw_testlib.git(repo, "config", "filter.x.smudge", f"touch {flt}; cat")
        (wt / ".gitattributes").write_text("* filter=x\n")
        (wt / "a.py").write_text("x = 3\n")
        assert "a.py" in cw_agent.commit(d, meta, "t", "q-1")["files"]
        assert not fs.exists() and not flt.exists()


def test_discard_edits_is_retryable_and_marks_sibling_edits_outcomes():
    with tempfile.TemporaryDirectory() as tmp:
        d, repo, meta, _marker = _agent_fixture(tmp)
        cw_agent.ensure_worktree(d, meta)
        branch = meta["agent"]["branch"]
        oids = [cw_ask.append_outcome(d, f"q-{n}", "t-1", "edits", "done", {"branch": branch, "files": []})["oid"]
                for n in (1, 2)]
        other = cw_ask.append_outcome(d, "q-3", "t-1", "edits", "done", {"branch": "cw/other/agent"})["oid"]
        state_of = lambda: {o["oid"]: o["state"] for o in cw_ask.read_outcomes(d)}  # noqa: E731
        real = cw_task.remove_worktree
        cw_task.remove_worktree = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("busy"))
        try:
            with pytest.raises(RuntimeError):
                cw_agent.discard_edits(d, oids[0])
        finally:
            cw_task.remove_worktree = real
        assert state_of()[oids[0]] == "done" and "agent" in cw_store.read_meta(d)
        cw_agent.discard_edits(d, oids[0])
        assert state_of() == {oids[0]: "discarded", oids[1]: "discarded", other: "done"}
        assert "agent" not in cw_store.read_meta(d)
        with pytest.raises(cw_store.CWError):
            cw_agent.discard_all(d, {"id": "cmp-12345678", "repo": str(repo),
                                     "agent": {"branch": "cw/other/agent"}})


def test_trust_ignores_malformed_config_and_non_github_hosts(tmp_path):
    meta = {"gh_repo": "acme/widgets"}
    for bad in ({"repos": [None, 3]}, {"repos": "acme/widgets"}, {"orgs": {"acme": 1}}, ["x"], "x"):
        assert cw_agent.trust(meta, {"trust": bad}) is False
    assert cw_agent.trust(meta, {"trust": {"repos": [None, "acme/widgets"]}}) is True
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "git@gitlab.example.com:acme/widgets.git"],
                   check=True)
    assert cw_agent.repo_name({"repo": str(tmp_path)}) is None
    subprocess.run(["git", "-C", str(tmp_path), "remote", "set-url", "origin", "git@github.com:Acme/Widgets.git"],
                   check=True)
    d = tmp_path / "wd"
    d.mkdir()
    meta = {"repo": str(tmp_path)}
    cw_store.write_json(d / "meta.json", dict(meta))
    assert cw_agent.repo_name(meta, d) == "Acme/Widgets"
    subprocess.run(["git", "-C", str(tmp_path), "remote", "remove", "origin"], check=True)
    assert cw_agent.repo_name(cw_store.read_meta(d), d) == "Acme/Widgets"


def test_commit_caps_the_file_list():
    with tempfile.TemporaryDirectory() as tmp:
        d, repo, meta, _marker = _agent_fixture(tmp)
        wt = cw_agent.ensure_worktree(d, meta)
        for n in range(cw_agent.FILES_CAP + 5):
            (wt / f"f{n}.txt").write_text("x\n")
        edits = cw_agent.commit(d, meta, "t", "q-1")
        assert len(edits["files"]) == len(edits["stat"]) == cw_agent.FILES_CAP and edits["truncated"] is True
