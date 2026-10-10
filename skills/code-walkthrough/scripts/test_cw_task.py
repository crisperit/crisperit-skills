#!/usr/bin/env python3
"""Self-check for the propose_task outcome and cw_task.py (running a plan as a code task in a
scratch worktree). Assert-based; collected by pytest. The `claude` binary is cw_testlib's fake,
which can write files into its cwd, so the real worktree/branch/commit path runs for real."""

import contextlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_ask  # noqa: E402
import cw_llm  # noqa: E402
import cw_mcp  # noqa: E402
import cw_run  # noqa: E402
import cw_server  # noqa: E402
import cw_store  # noqa: E402
import cw_testlib  # noqa: E402
import test_cw_server as srv  # noqa: E402

COMMENT = "please make x three"
QID = "q-aaaaaaaa"
ANCHOR = {"kind": "line", "path": "a.py", "side": "RIGHT", "line": 1, "quote": "x = 2"}
PLAN = {"title": "Make x three", "steps": ["set x to 3 in a.py", "add b.txt"], "files": ["a.py"]}
SUMMARY = "Set x to 3 and added new/b.txt. Nothing could be run, no shell."
WRITES = {"a.py": "x = 3\n", "new/b.txt": "hi\n"}


def _check(args, anchor_kind="line", already=()):
    return cw_mcp.check_outcome({"anchor": {"kind": anchor_kind}}, "propose_task", args, already)


def test_propose_task_validation():
    assert _check(PLAN) is None
    assert _check({"title": "t", "steps": ["s"]}) is None
    bad = [
        ({**PLAN, "title": " "}, "title"), ({**PLAN, "title": "x" * 121}, "title"),
        ({**PLAN, "steps": []}, "steps"), ({**PLAN, "steps": ["s"] * 13}, "steps"),
        ({**PLAN, "steps": "s"}, "steps"), ({**PLAN, "steps": [""]}, "step"),
        ({**PLAN, "steps": ["x" * 301]}, "step"), ({**PLAN, "steps": [3]}, "step"),
        ({**PLAN, "files": ["f"] * 21}, "files"), ({**PLAN, "files": "a.py"}, "files"),
        ({**PLAN, "files": ["../x"]}, "relative"), ({**PLAN, "files": ["a/../b"]}, "relative"),
        ({**PLAN, "files": ["/etc/passwd"]}, "relative"), ({**PLAN, "files": ["a\x00"]}, "relative"),
        ({**PLAN, "files": [""]}, "relative"), ({**PLAN, "files": [5]}, "relative"),
    ]
    for args, word in bad:
        error = _check(args)
        assert error and word in error, (args, error)
    assert _check({"title": "t", "steps": ["s"] * 12, "files": ["..a", "d/.x"] + ["f"] * 18}) is None
    for kind in ("block", "section"):
        assert _check(PLAN, kind) == "code tasks attach to diff lines or review threads; reply in prose instead"
    assert _check(PLAN, "thread") is None
    assert _check(PLAN, already={"task"}) == "already proposed a code task in this turn"


def _turn(d, anchor=ANCHOR, comment=COMMENT, qid=QID):
    anchor = cw_ask.validate_anchor(dict(anchor))
    cw_ask._append_qa(d, {"qid": qid, "thread_id": qid, "anchor": anchor, "comment": comment,
                          "question": comment, "status": "ok", "answer": "a", "source": "user"})
    cw_ask.write_turn_anchor(d, qid, qid, anchor, comment)


def _propose(d, args=PLAN, qid=QID):
    ok, text = cw_mcp.accept_outcome(d, qid, "propose_task", args)
    cw_ask.sweep_outcomes(d, qid, qid)
    return ok, text


def test_accept_folds_one_outcome_and_refuses_a_second_in_the_turn():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _turn(d)
        ok, text = _propose(d, {**PLAN, "title": "  Make x three "})
        assert ok and "nothing runs until the user presses Run" in text
        ok, text = _propose(d)
        assert not ok and text == "already proposed a code task in this turn"
        lines = (d / "turns" / f"{QID}.outcomes.jsonl").read_text().splitlines()
        assert len(lines) == 1 and json.loads(lines[0])["name"] == "propose_task"
        (outcome,) = cw_ask.read_outcomes(d)
        assert outcome["outcome"] == "task" and outcome["state"] == "proposed"
        assert outcome["payload"] == {"title": "Make x three", "steps": PLAN["steps"], "files": ["a.py"]}


def test_task_edit_and_dismiss_transitions():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _turn(d)
        _propose(d)
        oid = cw_ask.read_outcomes(d)[0]["oid"]

        def refused(action, payload=None, target=oid):
            try:
                cw_ask.outcome_action(d, target, action, payload)
            except cw_ask.OutcomeError as e:
                return e.kind
            raise AssertionError("not refused")

        assert refused("edit", {"title": "", "steps": ["s"]}) == "invalid"
        assert refused("edit", {**PLAN, "files": ["../x"]}) == "invalid"
        assert refused("keep", {}) == "invalid" and refused("revert") == "invalid"
        assert refused("dismiss", target="o-ffffffff") == "not_found"
        edited = cw_ask.outcome_action(d, oid, "edit", {"title": " New ", "steps": ["only"]})
        assert edited["state"] == "proposed"
        assert edited["payload"] == {"title": "New", "steps": ["only"], "files": [], "edited": True}
        assert "dismissed your proposed code task" not in cw_ask.build_prompt(d, ANCHOR, "q", QID)
        assert "edited your proposed code task to: New" in cw_ask.build_prompt(d, ANCHOR, "q", QID)
        assert cw_ask.outcome_action(d, oid, "dismiss")["state"] == "dismissed"
        assert refused("dismiss") == "conflict" and refused("edit", PLAN) == "conflict"
        assert "dismissed your proposed code task: New" in cw_ask.build_prompt(d, ANCHOR, "q", QID)


def test_openai_backend_offers_propose_task():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _turn(d)
        tools, handlers = cw_ask._openai_tools(d, QID, QID, None, {})
        assert "propose_task" in [t["function"]["name"] for t in tools]
        assert "recorded as a plan" in handlers["propose_task"](PLAN)
        assert [o["outcome"] for o in cw_ask.read_outcomes(d)] == ["task"]


def test_argv_shapes():
    profile = {"model": "sonnet"}
    argv = cw_llm.task_argv(profile, "SYS")
    assert argv == [
        "claude", "-p", "--safe-mode", "--strict-mcp-config", "--restricted",
        "--tools=Read,Grep,Glob,Edit,Write", "--permission-mode",
        "acceptEdits", "--permission-prompts", "none", "--model=sonnet", "--output-format",
        "stream-json", "--verbose", "--include-partial-messages", "--no-session-persistence",
        "--system-prompt=SYS"]
    assert not any(a.startswith(("--add-dir", "--mcp-config")) for a in argv)
    thread = cw_llm.thread_argv(profile, "SYS", mcp_config={"mcpServers": {}}, add_dir="/d/ctx", session_id="u")
    assert "--tools=Read,Grep,Glob" in thread and "--permission-mode" not in thread
    assert ("--allowedTools=mcp__cw__propose_resolve,mcp__cw__propose_page_edit,"
            "mcp__cw__propose_github_draft,mcp__cw__propose_task") in thread


# -- the daemon side ----------------------------------------------------------------------------

def _setup(tmp, home, *, kind="claude-code", script=None, timeout_s=30):
    cw_testlib.require_git((2, 31))  # cw_task._check_git_link uses rev-parse --path-format=absolute
    repo, base, head = cw_testlib.make_repo(
        tmp, {"CLAUDE.md": "base rules\n", "a.py": "x = 1\n"},
        {"CLAUDE.md": "head rules\n", "a.py": "x = 2\n"})
    cw_testlib.git(repo, "config", "user.name", "Repo User")
    cw_testlib.git(repo, "config", "user.email", "repo@example.com")
    d, _meta = srv._make_walkthrough(home)
    srv._set_meta(d, repo=str(repo), base=base, head=head)
    cw_run.add_worktree(str(repo), head, d / "head", symlinks=False)
    profile = ({"kind": "claude-code", "model": "sonnet"} if kind == "claude-code"
               else {"base_url": "http://localhost:1/v1", "model": "m"})
    cw_testlib.write_config(home, {"k": profile}, {"thread": "k"}, timeout_s=timeout_s)
    _turn(d)
    _propose(d)
    return d, repo, head, cw_ask.read_outcomes(d)[0]["oid"]


def _url(oid, action):
    return f"/api/walkthrough/{srv.KEY}/{srv.WID}/outcomes/{oid}/{action}"


def _outcome(d, oid):
    return next(o for o in cw_ask.read_outcomes(d) if o["oid"] == oid)


def _wait_state(d, oid, states, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        o = _outcome(d, oid)
        if o["state"] in states:
            return o
        time.sleep(0.05)
    raise AssertionError(f"{oid} stuck: {_outcome(d, oid)}")


def _wait_idle(daemon, timeout=5):
    deadline = time.monotonic() + timeout
    while (daemon._qids or daemon._turns or daemon._asks) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not daemon._qids and not daemon._turns and daemon._asks == 0


def _branches(repo):
    return cw_testlib.git(repo, "branch", "--list", "cw/*").split()


def _worktrees(repo):
    return [line for line in cw_testlib.git(repo, "worktree", "list").splitlines() if line]


@contextlib.contextmanager
def _record_subprocess():
    calls = []
    real = subprocess.Popen

    def popen(argv, *args, **kwargs):
        calls.append(list(argv) if isinstance(argv, (list, tuple)) else argv)
        return real(argv, *args, **kwargs)

    subprocess.Popen = popen
    try:
        yield calls
    finally:
        subprocess.Popen = real


def test_task_routes_refuse_wrong_action_state_and_oid():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d, repo, _head, oid = _setup(tmp, home)
        srv._append_qa(d, {"type": "outcome", "oid": "o-0a0a0a0a", "qid": QID, "thread_id": QID,
                           "outcome": "resolve", "state": "proposed", "at": "t",
                           "payload": {"thread": "gh-1", "why": "w"}})
        with srv.running_daemon() as daemon:
            for action in ("run", "stop", "discard"):
                srv._guard_checks(daemon, _url(oid, action), {})
                assert srv._post(daemon, _url("o-ffffffff", action))[0] == 404
            assert srv._post(daemon, _url(oid, "stop"))[0] == 409
            assert srv._post(daemon, _url(oid, "discard"))[0] == 409
            assert srv._post(daemon, _url(oid, "keep"), {"note_id": "n-1"})[0] == 400
            assert srv._post(daemon, _url(oid, "edit"), {"payload": {"title": "", "steps": ["s"]}})[0] == 400
            status, body = srv._post(daemon, _url(oid, "edit"), {"payload": {**PLAN, "title": "T2"}})
            assert status == 200 and body["outcome"]["payload"]["edited"] is True
            assert srv._post(daemon, _url(oid, "dismiss"))[1]["outcome"]["state"] == "dismissed"
            assert srv._post(daemon, _url(oid, "run"))[0] == 409
            assert srv._post(daemon, _url(oid, "edit"), {"payload": PLAN})[0] == 409
            for action in ("run", "stop", "discard"):
                assert srv._post(daemon, _url("o-0a0a0a0a", action))[0] == 400
        assert not (d / "tasks").exists() and len(_worktrees(repo)) == 2


def test_run_is_refused_on_an_openai_role_and_creates_nothing():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d, repo, _head, oid = _setup(tmp, home, kind="openai")
        before = _worktrees(repo)
        with srv.running_daemon() as daemon:
            status, body = srv._post(daemon, _url(oid, "run"))
        assert status == 400 and body["remedy"] == "point roles.thread at a claude-code profile", body
        assert _outcome(d, oid)["state"] == "proposed"
        assert not (d / "tasks").exists() and _worktrees(repo) == before and _branches(repo) == []


def _run_to_done(daemon, d, oid, fc):
    status, body = srv._post(daemon, _url(oid, "run"))
    assert status == 200 and body["outcome"]["state"] == "running", (status, body)
    task = body["outcome"]["payload"]["task"]
    assert task["qid"].startswith("tq-") and task["id"].startswith("t-") and len(task["id"]) == 8
    return task, _wait_state(d, oid, ("done", "failed", "stopped"))


def test_run_commits_to_a_local_branch_and_never_pushes():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d, repo, head, oid = _setup(tmp, home)
        main_before = cw_testlib.git(repo, "rev-parse", "main").strip()
        entry = cw_testlib.claude_task_entry(SUMMARY, WRITES, tools=("Read", "Edit"))
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}) as fc, srv.running_daemon() as daemon, \
                _record_subprocess() as calls:
            sock = srv._sse_connect(daemon, srv.KEY, srv.WID)
            try:
                task, done = _run_to_done(daemon, d, oid, fc)
                buf = srv._sse_read_until(sock, b'"tool": "Edit"')
            finally:
                sock.close()
            _wait_idle(daemon)
        assert done["state"] == "done", done
        progress = [data for name, data in srv._sse_events(buf) if name == "progress"]
        assert {p["tool"] for p in progress} >= {"Read", "Edit"}
        assert all(p["qid"] == task["qid"] and p["thread_id"] == QID for p in progress)

        t, branch = task["id"], f"cw/{srv.WID}/{task['id']}"
        result = done["payload"]["task"]
        assert result["branch"] == branch and branch in _branches(repo)
        assert (d / "tasks" / t / "a.py").read_text() == "x = 3\n"
        sha = cw_testlib.git(repo, "rev-parse", branch).strip()
        assert result["sha"] == sha and result["summary"] == SUMMARY and result["qid"] == task["qid"]
        assert cw_testlib.git(repo, "rev-list", "--count", f"{head}..{branch}").strip() == "1"
        message = cw_testlib.git(repo, "log", "-1", "--format=%B", branch)
        assert message.startswith("Make x three\n\n" + SUMMARY)
        assert message.strip().endswith("Co-Authored-By: Claude <noreply@anthropic.com>")
        assert cw_testlib.git(repo, "log", "-1", "--format=%an <%ae>", branch).strip() == "Repo User <repo@example.com>"
        assert cw_testlib.git(repo, "show", f"{branch}:a.py") == "x = 3\n"
        assert sorted((f["path"], f["add"], f["del"]) for f in result["files"]) == [
            ("a.py", 1, 1), ("new/b.txt", 1, 0)]
        assert result["stat"] == "2 files, +2 -1"
        assert (d / "head" / "a.py").read_text() == "x = 2\n"
        assert cw_testlib.git(d / "head", "status", "--porcelain") == ""
        assert cw_testlib.git(repo, "rev-parse", "main").strip() == main_before

        (log,) = fc.log()
        assert Path(log["cwd"]).resolve() == (d / "tasks" / t).resolve()
        argv = log["argv"]
        assert "--restricted" in argv and "acceptEdits" in argv and "--no-session-persistence" in argv
        assert "--safe-mode" in argv and "--strict-mcp-config" in argv
        assert not any(a.startswith(("--mcp-config", "--add-dir")) or a in ("--resume", "--session-id")
                       for a in argv)
        assert "Make x three" in log["stdin"] and COMMENT in log["stdin"] and "a.py:1" in log["stdin"]
        assert "base rules" in log["stdin"] and "head rules" not in log["stdin"]

        assert calls and any("commit" in c for c in calls if isinstance(c, list))
        assert not [c for c in calls if any("push" in str(a) for a in (c if isinstance(c, list) else [c]))]


def test_git_refuses_push_and_anything_off_the_allowlist():
    import cw_task
    for args in (["push"], ["-c", "a=b", "push", "origin"], ["fetch"], ["remote", "add", "x", "y"], []):
        try:
            cw_task._git(".", args)
        except ValueError:
            continue
        raise AssertionError(args)
    with tempfile.TemporaryDirectory() as tmp:
        cw_testlib.require_git()
        cw_testlib.init_repo(tmp)
        assert cw_task._git(tmp, ["-c", "a.b=c", "rev-parse", "--git-dir"]).returncode == 0


def test_a_repointed_git_link_fails_the_task_before_any_commit():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d, repo, _head, oid = _setup(tmp, home)
        entry = cw_testlib.claude_task_entry(SUMMARY, {**WRITES, ".git": f"gitdir: {tmp}/elsewhere\n"})
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}) as fc, srv.running_daemon() as daemon:
            _task, failed = _run_to_done(daemon, d, oid, fc)
        assert failed["state"] == "failed"
        assert failed["payload"]["task"]["error"] == "the worktree's git link was changed"
        assert _branches(repo) == [] and cw_testlib.git(repo, "log", "--all", "--format=%s").count("\n") == 2


def test_cancellation_wins_over_a_completed_result():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d, repo, _head, oid = _setup(tmp, home)
        entry = cw_testlib.claude_task_entry(SUMMARY, WRITES, delay=0.3)
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}) as fc, srv.running_daemon() as daemon:
            status, body = srv._post(daemon, _url(oid, "run"))
            task = body["outcome"]["payload"]["task"]
            deadline = time.monotonic() + 10
            while not fc.starts() and time.monotonic() < deadline:
                time.sleep(0.02)
            daemon._turns[task["qid"]]["live"]["cancelled"] = True
            assert _wait_state(d, oid, ("stopped", "done", "failed"))["state"] == "stopped"
        assert _branches(repo) == [] and (d / "tasks" / task["id"]).is_dir()


def test_discard_removes_worktree_and_branch():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d, repo, _head, oid = _setup(tmp, home)
        entry = cw_testlib.claude_task_entry(SUMMARY, WRITES)
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}) as fc, srv.running_daemon() as daemon:
            task, done = _run_to_done(daemon, d, oid, fc)
            assert done["state"] == "done" and _branches(repo)
            status, body = srv._post(daemon, _url(oid, "discard"))
            assert status == 200 and body["outcome"]["state"] == "discarded", body
            assert not (d / "tasks" / task["id"]).exists() and _branches(repo) == []
            assert len(_worktrees(repo)) == 2
            assert srv._post(daemon, _url(oid, "discard"))[0] == 409
            assert srv._post(daemon, _url(oid, "run"))[0] == 409
            assert srv._post(daemon, _url(oid, "stop"))[0] == 409
        assert (d / "head" / "a.py").read_text() == "x = 2\n"


def test_a_run_that_changes_nothing_or_exits_badly_fails_and_commits_nothing():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d, repo, _head, oid = _setup(tmp, home)
        script = [cw_testlib.claude_task_entry("looked, nothing to do"),
                  cw_testlib.claude_task_entry("crashed", {"a.py": "x = 9\n"}, exit=1)]
        with cw_testlib.fake_claude(tmp, {"sonnet": script}) as fc, srv.running_daemon() as daemon:
            task, failed = _run_to_done(daemon, d, oid, fc)
            assert failed["state"] == "failed"
            assert failed["payload"]["task"]["error"] == "the agent made no changes"
            assert (d / "tasks" / task["id"]).is_dir() and _branches(repo) == []
            assert srv._post(daemon, _url(oid, "run"))[0] == 409

            _turn(d, qid="q-bbbbbbbb")
            _propose(d, PLAN, qid="q-bbbbbbbb")
            second = next(o for o in cw_ask.read_outcomes(d) if o["qid"] == "q-bbbbbbbb")["oid"]
            _task, failed = _run_to_done(daemon, d, second, fc)
            assert failed["state"] == "failed" and failed["payload"]["task"]["error"]
            assert _branches(repo) == []


def test_stop_mid_turn_marks_stopped_keeps_the_worktree_then_discard():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d, repo, _head, oid = _setup(tmp, home)
        entry = cw_testlib.claude_task_entry(SUMMARY, WRITES, delay=20)
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}) as fc, srv.running_daemon() as daemon:
            status, body = srv._post(daemon, _url(oid, "run"))
            assert status == 200
            task = body["outcome"]["payload"]["task"]
            deadline = time.monotonic() + 10
            while not fc.starts() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert fc.starts() and daemon.inflight(srv.KEY, srv.WID)[0]["qid"] == task["qid"]
            status, _ = srv._post(daemon, f"/api/walkthrough/{srv.KEY}/{srv.WID}/comment/{task['qid']}/cancel")
            assert status == 200
            stopped = _wait_state(d, oid, ("stopped", "done", "failed"))
            assert stopped["state"] == "stopped", stopped
            assert (d / "tasks" / task["id"]).is_dir() and _branches(repo) == []
            _wait_idle(daemon)
            assert srv._post(daemon, f"/api/walkthrough/{srv.KEY}/{srv.WID}/comment/{task['qid']}/cancel")[0] == 404
            status, body = srv._post(daemon, _url(oid, "discard"))
            assert status == 200 and body["outcome"]["state"] == "discarded"
            assert not (d / "tasks" / task["id"]).exists()


def test_stop_action_cancels_the_running_turn():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d, _repo, _head, oid = _setup(tmp, home)
        entry = cw_testlib.claude_task_entry(SUMMARY, WRITES, delay=20)
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}) as fc, srv.running_daemon() as daemon:
            assert srv._post(daemon, _url(oid, "run"))[0] == 200
            assert srv._post(daemon, _url(oid, "stop"))[0] == 200
            assert _wait_state(d, oid, ("stopped", "done", "failed"))["state"] == "stopped"
            assert srv._post(daemon, _url(oid, "stop"))[0] == 409


def test_prune_removes_every_task_worktree_and_branch():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d, repo, _head, oid = _setup(tmp, home)
        entry = cw_testlib.claude_task_entry(SUMMARY, WRITES)
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}) as fc, srv.running_daemon() as daemon:
            _task, done = _run_to_done(daemon, d, oid, fc)
        assert done["state"] == "done" and _branches(repo) and len(_worktrees(repo)) == 3
        cw_server._remove_walkthrough(cw_store.read_meta(d), d)
        assert not d.exists() and _branches(repo) == [] and len(_worktrees(repo)) == 1


def _running_fixture(d):
    _turn(d)
    _propose(d)
    oid = cw_ask.read_outcomes(d)[0]["oid"]
    task = {"id": "t-abcdef", "qid": "tq-deadbeef", "branch": f"cw/{srv.WID}/t-abcdef"}
    cw_ask.mark_outcome(d, oid, "running", payload={**PLAN, "task": task})
    return oid


def test_a_running_task_without_a_live_turn_becomes_failed():
    with cw_testlib.temp_home() as home:
        d, _ = srv._make_walkthrough(home)
        oid = _running_fixture(d)
        cw_ask.finalise_stale(d, {"tq-deadbeef"})
        assert _outcome(d, oid)["state"] == "running"
        cw_ask.finalise_stale(d, set())
        failed = _outcome(d, oid)
        assert failed["state"] == "failed" and failed["payload"]["task"]["error"] == "interrupted"
        assert failed["payload"]["task"]["branch"] == f"cw/{srv.WID}/t-abcdef"


def test_daemon_start_and_qa_read_fail_interrupted_tasks():
    with cw_testlib.temp_home() as home:
        d, _ = srv._make_walkthrough(home)
        oid = _running_fixture(d)
        cw_server._mark_interrupted()
        assert _outcome(d, oid)["state"] == "failed"
        d2, _ = srv._make_walkthrough(home, key="repo-bbbbbb", wid="cmp-76543210")
        oid2 = _running_fixture(d2)
        with srv.running_daemon() as daemon:
            status, raw = srv._request(daemon, "GET", "/api/walkthrough/repo-bbbbbb/cmp-76543210/qa",
                                       token=daemon.token)
            assert status == 200
        assert _outcome(d2, oid2)["state"] == "failed"


def test_task_prompt_asset_exists_and_names_the_limits():
    text = (Path(__file__).parent.parent / "prompts" / "task.md").read_text()
    assert "no shell" in text and "push" not in text
    comment = " ".join((Path(__file__).parent.parent / "prompts" / "comment.md").read_text().split())
    assert "`propose_task`" in comment and "nothing runs until the user presses Run" in comment
