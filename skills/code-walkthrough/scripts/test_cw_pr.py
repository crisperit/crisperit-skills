#!/usr/bin/env python3
"""Self-check for phase 3: PR targets in live mode (phase345-spec.md, "Phase 3"). Assert-based,
no framework; also collected by pytest. Real cw_run.py, real notes.py/fanout_threads.py/regen.py
subprocesses, cw_testlib.fake_gh standing in for `gh`, cw_testlib.StubLLM standing in for the
model backend -- no fakes of our own modules besides those two external boundaries.

Every test repo's commits land well after 2000-01-01, so a thread's `first_comment_at` fixture
of "2000-01-01T00:00:00Z" always keeps every commit inside its resolution window.
"""

import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_run  # noqa: E402
import cw_store  # noqa: E402
import cw_testlib  # noqa: E402


def _pr_repo(tmp):
    return cw_testlib.pr_repo(tmp, remote_tracking=False)


def _comment(gh_id, *, path="foo.py", line=2, side="RIGHT"):
    return {
        "id": gh_id, "line": line, "original_line": line, "path": path, "side": side,
        "body": "please fix", "user": {"login": "alice"}, "created_at": "2000-01-01T00:00:00Z",
        "html_url": "https://github.com/o/r/pull/7#r1", "in_reply_to_id": None,
        "diff_hunk": "@@ -1,3 +1,3 @@", "original_commit_id": "abc",
    }


def _thread_node(thread_id, gh_id, *, resolved=True):
    return {
        "id": thread_id, "isResolved": resolved,
        "resolvedBy": {"login": "bob"} if resolved else None,
        "comments": {"pageInfo": {"hasNextPage": False}, "nodes": [{"databaseId": gh_id}]},
    }


def _step_events(events):
    return [(data["name"], data["status"]) for ev, data in events if ev == "step"]


def _valid_resolution(thread_id):
    return {"thread_id": thread_id, "outcome": "conversation", "closing_message": "settled",
            "ticket": None, "commits": [], "files": [], "why": None, "confidence": "high"}


# ---------------------------------------------------------------------------
# Order: prepare, comments (2f), threads (2g), render
# ---------------------------------------------------------------------------

def test_order_prepare_comments_threads_render(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setenv("GITHUB_TOKEN", "y")
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        repo, base, head = _pr_repo(tmp)

        def script(body, _n):
            model = body.get("model")
            if model == "prose-m":
                return cw_testlib.small_route_reply(body)
            if model == "analysis-m":
                return cw_testlib.tool_call("submit_resolution", _valid_resolution("T1"))
            raise RuntimeError(f"unexpected model {model!r}")

        with cw_testlib.StubLLM(script) as stub, cw_testlib.fake_gh(
            tmp, comments=[_comment(101)], threads=[_thread_node("T1", 101)],
        ) as gh:
            cw_testlib.write_route_config(home, stub, ask=False)
            d, meta, _reused = cw_run.prepare_walkthrough(
                {"repo": str(repo), "base": base, "head": head, "target": "t", "slug": "t", "pr": 7})
            events = []
            status = cw_run.run(d, lambda ev, data: events.append((ev, data)))

        assert status == "done"
        steps = _step_events(events)
        order = [name for name, _status in steps]
        assert order.index("prepare") < order.index("comments") < order.index("threads") < order.index("render")
        assert ("prepare", "ok") in steps
        assert ("comments", "ok") in steps
        assert ("threads", "ok") in steps
        assert ("render", "ok") in steps

        state = json.loads((d / "state.json").read_text())
        assert any(n["id"] == "gh-101" for n in state["notes"])
        assert "T1" in state["resolutions"]
        assert meta["id"] == cw_store.walkthrough_id("t", 7, "o/r")

        log = gh.log()
        assert log
        toplevel = str(repo.resolve())
        for entry in log:
            assert entry["cwd"] == toplevel
            assert entry["gh_token"] is False
            assert entry["github_token"] is False


# ---------------------------------------------------------------------------
# No resolved threads: no worker call, step skipped
# ---------------------------------------------------------------------------

def test_no_resolved_threads_means_no_worker_call():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        repo, base, head = _pr_repo(tmp)
        with cw_testlib.StubLLM(lambda body, _n: cw_testlib.small_route_reply(body)) as stub, cw_testlib.fake_gh(
            tmp, comments=[_comment(101)], threads=[_thread_node("T1", 101, resolved=False)],
        ):
            cw_testlib.write_route_config(home, stub, ask=False)
            d, meta, _reused = cw_run.prepare_walkthrough(
                {"repo": str(repo), "base": base, "head": head, "target": "t", "slug": "t", "pr": 7})
            events = []
            status = cw_run.run(d, lambda ev, data: events.append((ev, data)))

        assert status == "done"
        assert stub.count("analysis-m") == 0
        steps = dict(_step_events(events))
        assert steps["threads"] == "skipped"


# ---------------------------------------------------------------------------
# need_diffs_for: exactly one more call, out-of-window sha rejected
# ---------------------------------------------------------------------------

def test_need_diffs_for_gives_diffs_then_resolves():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        repo, base, head = _pr_repo(tmp)
        bogus_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

        script = {
            "analysis-m": [
                cw_testlib.tool_call("submit_resolution", {
                    "thread_id": "T1", "need_diffs_for": [head, bogus_sha]}),
                cw_testlib.tool_call("submit_resolution", {
                    "thread_id": "T1", "outcome": "commits", "closing_message": None,
                    "ticket": None, "commits": [head], "files": [], "why": "fixed it",
                    "confidence": "high"}),
            ],
        }

        def dispatch(body, n):
            model = body.get("model")
            if model == "prose-m":
                return cw_testlib.small_route_reply(body)
            return script[model][min(n, len(script[model]) - 1)]

        with cw_testlib.StubLLM(dispatch) as stub, cw_testlib.fake_gh(
            tmp, comments=[_comment(101)], threads=[_thread_node("T1", 101)],
        ):
            cw_testlib.write_route_config(home, stub, ask=False)
            d, meta, _reused = cw_run.prepare_walkthrough(
                {"repo": str(repo), "base": base, "head": head, "target": "t", "slug": "t", "pr": 7})
            status = cw_run.run(d, lambda ev, data: None)

        assert status == "done"
        assert stub.count("analysis-m") == 2
        transcript = json.loads((d / "runs" / "analysis-1.json").read_text())
        tool_msgs = [m["content"] for m in transcript["messages"] if m.get("role") == "tool"]
        assert any(isinstance(c, str) and "```diff" in c and head in c for c in tool_msgs)
        assert any(isinstance(c, str) and bogus_sha in c and "not in this thread's window" in c
                   for c in tool_msgs)
        state = json.loads((d / "state.json").read_text())
        assert state["resolutions"]["T1"]["outcome"] == "commits"


# ---------------------------------------------------------------------------
# Bad sha / javascript: ticket -> gated out, no chip; three retries each
# ---------------------------------------------------------------------------

def test_bad_sha_or_javascript_ticket_dropped_from_merge():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        repo, base, head = _pr_repo(tmp)
        counts = {"A": 0, "B": 0, "C": 0}
        lock = threading.Lock()

        def dispatch(body, _n):
            model = body.get("model")
            if model == "prose-m":
                return cw_testlib.small_route_reply(body)
            seed = cw_testlib.first_json_block(cw_testlib.last_user_text(body))
            thread_id = seed["thread_id"]
            with lock:
                counts[thread_id] += 1
            if thread_id == "A":
                return cw_testlib.tool_call("submit_resolution", {
                    "thread_id": "A", "outcome": "commits", "closing_message": None,
                    "ticket": None, "commits": ["deadbeef"], "files": [], "why": "x",
                    "confidence": "low"})
            if thread_id == "B":
                return cw_testlib.tool_call("submit_resolution", {
                    "thread_id": "B", "outcome": "deferred", "closing_message": None,
                    "ticket": "javascript:alert(1)", "commits": [], "files": [], "why": None,
                    "confidence": "low"})
            return cw_testlib.tool_call("submit_resolution", _valid_resolution("C"))

        with cw_testlib.StubLLM(dispatch) as stub, cw_testlib.fake_gh(
            tmp,
            comments=[_comment(201), _comment(202), _comment(203)],
            threads=[_thread_node("A", 201), _thread_node("B", 202), _thread_node("C", 203)],
        ):
            cw_testlib.write_route_config(home, stub, ask=False)
            d, meta, _reused = cw_run.prepare_walkthrough(
                {"repo": str(repo), "base": base, "head": head, "target": "t", "slug": "t", "pr": 7})
            status = cw_run.run(d, lambda ev, data: None)

        assert status == "done"
        state = json.loads((d / "state.json").read_text())
        assert list(state["resolutions"].keys()) == ["C"]
        assert counts == {"A": 3, "B": 3, "C": 1}


# ---------------------------------------------------------------------------
# Cache: a re-run makes zero thread calls; refresh path makes zero model calls
# ---------------------------------------------------------------------------

def test_cache_hit_then_refresh_make_no_model_calls():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        repo, base, head = _pr_repo(tmp)

        def dispatch(body, _n):
            model = body.get("model")
            if model == "prose-m":
                return cw_testlib.small_route_reply(body)
            return cw_testlib.tool_call("submit_resolution", _valid_resolution("T1"))

        with cw_testlib.StubLLM(dispatch) as stub, cw_testlib.fake_gh(
            tmp, comments=[_comment(101)], threads=[_thread_node("T1", 101)],
        ):
            cw_testlib.write_route_config(home, stub, ask=False)
            params = {"repo": str(repo), "base": base, "head": head, "target": "t", "slug": "t", "pr": 7}
            d, meta, _reused = cw_run.prepare_walkthrough(params)
            status = cw_run.run(d, lambda ev, data: None)
            assert status == "done"
            assert any((d / "cache").glob("*.json"))

            analysis_n1 = stub.count("analysis-m")
            prose_n1 = stub.count("prose-m")

            # Re-run the resolve step directly: everything is now cached, so this adds no
            # model calls at all.
            meta = cw_store.read_meta(d)
            cw_run._resolve_threads(d, meta, cw_store.load_config(), None)
            assert stub.count("analysis-m") == analysis_n1

            # Refresh path: a second walkthrough_start on a done PR goes to "building", and
            # run() takes the refresh short-circuit straight to _final_build -- no batch or
            # prose calls either.
            d2, meta2, reused2 = cw_run.prepare_walkthrough(params)
            assert reused2 is True
            assert d2 == d
            assert meta2["status"] == "building"
            status2 = cw_run.run(d, lambda ev, data: None)
            assert status2 == "done"
            assert stub.count("analysis-m") == analysis_n1
            assert stub.count("prose-m") == prose_n1


# ---------------------------------------------------------------------------
# gh failure: step failed with a remedy, run still finishes done
# ---------------------------------------------------------------------------

def test_gh_failure_fails_the_comments_step_with_a_remedy():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        repo, base, head = _pr_repo(tmp)
        with cw_testlib.StubLLM(lambda body, _n: cw_testlib.small_route_reply(body)) as stub, cw_testlib.fake_gh(
            tmp, comments=[_comment(101)], threads=[_thread_node("T1", 101)],
            fail_ops=("rest-comments",),
        ):
            cw_testlib.write_route_config(home, stub, ask=False)
            d, meta, _reused = cw_run.prepare_walkthrough(
                {"repo": str(repo), "base": base, "head": head, "target": "t", "slug": "t", "pr": 7})
            status = cw_run.run(d, lambda ev, data: None)

        assert status == "done"
        meta = cw_store.read_meta(d)
        comments_step = meta["steps"]["comments"]
        assert comments_step["status"] == "failed"
        assert "gh auth login" in comments_step["remedy"]


def test_non_github_origin_raises_cwerror_with_remedy():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\n"}, {"foo.py": "b\n"})
        cw_testlib.write_config(home, {"p": {"base_url": "http://x", "model": "m"}},
                                 {"analysis": "p", "prose": "p"})
        try:
            cw_run.prepare_walkthrough({"repo": str(repo), "base": base, "head": head,
                                         "target": "t", "slug": "t", "pr": 5})
            raise AssertionError("expected CWError")
        except cw_store.CWError as e:
            assert "GitHub remote" in str(e)
            assert e.remedy
