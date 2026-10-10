#!/usr/bin/env python3
"""Self-check for phase 5: post to GitHub from the page (phase345-spec.md, "Phase 5",
implementer A). Assert-based, no framework; also collected by pytest. In-process
cw_server.Daemon, real cw_run.py/notes.py, cw_testlib.fake_gh standing in for `gh`,
cw_testlib.StubLLM standing in for the model backend -- no fakes of our own modules besides
those two external boundaries, same convention as test_cw_pr.py.
"""

import atexit
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections import namedtuple
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import cw_ask  # noqa: E402
import cw_run  # noqa: E402
import cw_server  # noqa: E402
import cw_store  # noqa: E402
import cw_testlib  # noqa: E402
import notes  # noqa: E402
from cw_testlib import request as _request  # noqa: E402
from cw_testlib import running_daemon  # noqa: E402

NO_PR_MSG = "No PR for this comparison -- use Copy for agent instead."
HEAD_NOT_PUSHED_MSG = "The compared commit has not been pushed yet -- use Copy for agent instead."


def _note(**overrides):
    note = {
        "id": "n-1", "origin": "local", "target": "pr", "state": "draft",
        "path": "foo.py", "line": 2, "side": "RIGHT", "hunk_id": None,
        "anchor_text": None, "anchor_line": 2, "stale": False,
        "body": "a note", "order": 1, "author": "", "created_at": "2000-01-01T00:00:00Z",
        "gh_id": None, "gh_url": None, "reply_to": None,
    }
    note.update(overrides)
    return note


# Every test below needs a finished walkthrough, which costs ~2s to build. Build each kind once per
# process, snapshot the home dir, and restore the snapshot over the same path before each test: the
# walkthrough's meta.json holds absolute paths, so it has to come back where it was built. The build
# runs from a function-scoped fixture so conftest's autouse patches (no language servers, zero LLM
# backoff) are active. Posting, previewing and publishing never call the model, so the dead stub the
# snapshot's config points at is never reached.
Built = namedtuple("Built", "repo base head d")
_SNAPSHOTS = {}

GH_COMMENT = {"id": 77, "node_id": "N77", "path": "foo.py", "line": 2, "original_line": 2,
              "side": "RIGHT", "body": "same body", "user": {"login": "me"},
              "html_url": "u", "created_at": "2000-01-01T00:00:00Z",
              "in_reply_to_id": None, "commit_id": "x", "original_commit_id": "x"}


def _build_snapshot(kind):
    root = Path(tempfile.mkdtemp(prefix="cw-post-template-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    home = root / "home"
    home.mkdir()
    old_home = os.environ.get("CODE_WALKTHROUGH_HOME")
    os.environ["CODE_WALKTHROUGH_HOME"] = str(home)
    try:
        comments = [GH_COMMENT] if kind == "pr_with_gh_comment" else []
        with cw_testlib.StubLLM(lambda body, _n: cw_testlib.small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(root, comments=comments, threads=[]):
            if kind == "no_pr":
                repo, base, head = cw_testlib.make_repo(root, {"foo.py": "a\nb\nc\n"}, {"foo.py": "a\nX\nc\n"})
                params = {}
            else:
                repo, base, head = cw_testlib.pr_repo(root)
                params = {"pr": 7}
            cw_testlib.write_route_config(home, stub, ask=False)
            d, _meta, _reused = cw_run.prepare_walkthrough(
                {"repo": str(repo), "base": base, "head": head, "target": "t", "slug": "t", **params})
            status = cw_run.run(d, lambda ev, data: None)
            assert status == "done", status
    finally:
        if old_home is None:
            os.environ.pop("CODE_WALKTHROUGH_HOME", None)
        else:
            os.environ["CODE_WALKTHROUGH_HOME"] = old_home
    snap = root / "snapshot"
    shutil.copytree(home, snap)
    return home, snap, Built(repo, base, head, d)


def _restore(kind, monkeypatch):
    if kind not in _SNAPSHOTS:
        _SNAPSHOTS[kind] = _build_snapshot(kind)
    home, snap, built = _SNAPSHOTS[kind]
    shutil.rmtree(home)
    shutil.copytree(snap, home)
    monkeypatch.setenv("CODE_WALKTHROUGH_HOME", str(home))
    return built


@pytest.fixture
def done_pr(monkeypatch):
    return _restore("pr", monkeypatch)


@pytest.fixture
def done_no_pr(monkeypatch):
    return _restore("no_pr", monkeypatch)


@pytest.fixture
def done_pr_with_gh_comment(monkeypatch):
    """Built while GitHub already held GH_COMMENT, so the note exists in state.json before the test adds its own."""
    return _restore("pr_with_gh_comment", monkeypatch)


def _add_notes(d, extra_notes):
    state_path = d / "state.json"
    state = json.loads(state_path.read_text())
    state["notes"] = list(state.get("notes", [])) + list(extra_notes)
    state_path.write_text(json.dumps(state))
    return state_path


def _preview(daemon, key, wid, resolve, **extra):
    return _request(daemon, "POST", f"/api/walkthrough/{key}/{wid}/post/preview",
                     token=daemon.token, body=json.dumps({"resolve": resolve, **extra}))


def _post(daemon, key, wid, nonce, resolve, **extra):
    return _request(daemon, "POST", f"/api/walkthrough/{key}/{wid}/post",
                     token=daemon.token,
                     body=json.dumps({"nonce": nonce, "resolve": resolve, **extra}))


def _put_notes(daemon, key, wid, notes_list):
    return _request(daemon, "PUT", f"/api/walkthrough/{key}/{wid}/notes",
                     token=daemon.token, body=json.dumps({"notes": notes_list}))


# ---------------------------------------------------------------------------
# Preview lists exactly what notes.py considers ready
# ---------------------------------------------------------------------------

def test_preview_lists_exactly_what_notes_py_considers_ready(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]):

        github_parent = _note(id="gh-501", origin="github", state="posted", gh_id=501,
                               gh_node_id="NODE_501", body="parent")
        local_draft = _note(id="n-ready", body="ready to post")
        stale_draft = _note(id="n-stale", body="stale", stale=True)
        unposted_parent = _note(id="n-unposted-parent", body="parent draft", gh_id=None)
        reply_unposted = _note(id="n-reply-unposted", body="reply", reply_to="n-unposted-parent")
        page_reply = _note(id="n-page-reply", body="page reply", reply_to=None, in_reply_to="gh-501")
        reply_to_synced = _note(id="n-reply-synced", body="reply to synced", reply_to="gh-501")
        posted_note = _note(id="n-posted", body="already posted", state="posted", gh_id=999)
        blank_draft = _note(id="n-blank", body="")

        state_path = _add_notes(d, [
            github_parent, local_draft, stale_draft, unposted_parent, reply_unposted,
            reply_to_synced, page_reply, posted_note, blank_draft,
        ])

        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            status, raw = _preview(daemon, key, wid, [])
            assert status == 200, (status, raw)
            reply = json.loads(raw)

    final_state = json.loads(state_path.read_text())
    expected = set(notes.pending_publish_ids(final_state)) - {"n-blank"}
    assert {n["id"] for n in reply["notes"]} == expected
    assert "n-blank" not in {n["id"] for n in reply["notes"]}
    page_row = next(n for n in reply["notes"] if n["id"] == "n-page-reply")
    assert page_row["reply_to"] == "gh-501"


# ---------------------------------------------------------------------------
# Edit or added resolve between preview and post gives 409
# ---------------------------------------------------------------------------

def test_edit_or_added_resolve_between_preview_and_post_gives_409(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]) as gh:
        root = _note(id="gh-501", origin="github", state="posted", gh_id=501,
                     gh_node_id="NODE_501", gh_thread_id="THREAD_1", resolved=False, body="root")
        _add_notes(d, [root])

        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            status, raw = _put_notes(daemon, key, wid, [_note(id="n-draft", body="v1")])
            assert status == 200, (status, raw)

            status, raw = _preview(daemon, key, wid, [])
            assert status == 200, (status, raw)
            nonce1 = json.loads(raw)["nonce"]

            # Edit the draft body between preview and post: the recomputed nonce no
            # longer matches the stale one from preview.
            status, raw = _put_notes(daemon, key, wid, [_note(id="n-draft", body="v2")])
            assert status == 200, (status, raw)

            status, raw = _post(daemon, key, wid, nonce1, [])
            assert status == 409, (status, raw)
            assert json.loads(raw)["error"] == "changed since preview"
            assert gh.count("NewThread") == 0

            # Re-preview to get a nonce consistent with the edited body, then add a
            # resolve the preview never saw.
            status, raw = _preview(daemon, key, wid, [])
            assert status == 200, (status, raw)
            nonce2 = json.loads(raw)["nonce"]

            status, raw = _post(daemon, key, wid, nonce2, ["gh-501"])
            assert status == 409, (status, raw)
            assert json.loads(raw)["error"] == "changed since preview"
            assert gh.count("NewThread") == 0
            assert gh.count("ResolveThread") == 0


# ---------------------------------------------------------------------------
# Retry after a timeout does not double-post
# ---------------------------------------------------------------------------

def test_retry_after_timeout_does_not_double_post(done_pr, tmp_path, monkeypatch):
    # 1s is notes.py's floor (GH_TIMEOUT is an int). It bounds every gh call from notes.py, so a
    # box too loaded to start the fake gh within 1s can fail this test.
    monkeypatch.setenv("CW_GH_TIMEOUT", "1")
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[], sleep={"NewThread": 2}) as gh:
        draft = _note(id="n-slow", body="slow post", path="foo.py", line=2, side="RIGHT")
        state_path = _add_notes(d, [draft])
        meta = cw_store.read_meta(d)

        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            status, raw = _preview(daemon, key, wid, [])
            assert status == 200, (status, raw)
            nonce = json.loads(raw)["nonce"]
            assert "n-slow" in {n["id"] for n in json.loads(raw)["notes"]}

            status, raw = _post(daemon, key, wid, nonce, [])
            assert status == 200, (status, raw)
            results = json.loads(raw)["results"]
            first = next(r for r in results if r["id"] == "n-slow")
            assert first["ok"] is False, first  # the gh call timed out

            assert gh.count("NewThread") == 1  # the fake still recorded the comment

            # Preview again: sync_pr's dedupe matches the now-visible comment to the
            # draft, so the note is gone from the ready list.
            status, raw = _preview(daemon, key, wid, [])
            assert status == 200, (status, raw)
            reply = json.loads(raw)
            assert "n-slow" not in {n["id"] for n in reply["notes"]}

            status, raw = _post(daemon, key, wid, reply["nonce"], [])
            assert status == 200, (status, raw)
            assert gh.count("NewThread") == 1  # no second thread opened

        state = json.loads(state_path.read_text())
        by_id = {n["id"]: n for n in state["notes"]}
        assert by_id["n-slow"]["state"] == "posted"

        result = cw_run._notes(meta, ["deliver", "--state", str(state_path), "--id", "n-slow"])
        assert "already posted" in result.stdout
        assert gh.count("NewThread") == 1  # no further gh call at all


# ---------------------------------------------------------------------------
# One Submit review: open once, deliver each draft, submit once, then resolve
# ---------------------------------------------------------------------------

def _ops(gh):
    return [e["op"] for e in gh.log() if e["op"] in
            ("OpenReview", "NewThread", "ReplyThread", "SubmitReview", "ResolveThread")]


def _preview_ok(daemon, key, wid, resolve=(), **extra):
    status, raw = _preview(daemon, key, wid, list(resolve), **extra)
    assert status == 200, (status, raw)
    return json.loads(raw)


def test_submit_review_opens_once_delivers_all_submits_once_then_resolves(done_pr, tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setenv("GITHUB_TOKEN", "y")
    d = done_pr.d
    repo = done_pr.repo
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]) as gh:
        root = _note(id="gh-501", origin="github", state="posted", gh_id=501,
                     gh_node_id="NODE_501", gh_thread_id="THREAD_1", resolved=False, body="root")
        _add_notes(d, [root, *[_note(id=f"n-{i}", body=f"body {i}", order=i) for i in (1, 2, 3)]])
        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            pre = _preview_ok(daemon, key, wid, ["gh-501"])
            assert pre["review"] == {"pending": False, "comments": 0}
            assert pre["in_review"] == [] and pre["own_pr"] is True
            assert {n["id"] for n in pre["notes"]} == {"n-1", "n-2", "n-3"}
            gh_ops_before = len(gh.log())

            status, raw = _post(daemon, key, wid, pre["nonce"], ["gh-501"], event="APPROVE",
                                body="ship it")
            assert status == 200, (status, raw)
            reply = json.loads(raw)
            assert reply["ok"] is True and reply["submitted"] is True
            note_results = [r for r in reply["results"] if r["kind"] == "note"]
            assert [r["state"] for r in note_results] == ["in_review"] * 3

        ops = [e["op"] for e in gh.log()[gh_ops_before:] if e["op"] in
               ("OpenReview", "NewThread", "SubmitReview", "ResolveThread")]
        assert ops == ["OpenReview", "NewThread", "NewThread", "NewThread", "SubmitReview",
                       "ResolveThread"], ops
        submit = next(e for e in gh.log() if e["op"] == "SubmitReview")
        assert submit["variables"]["event"] == "APPROVE" and submit["variables"]["body"] == "ship it"
        states = {n["id"]: n["state"] for n in json.loads((d / "state.json").read_text())["notes"]}
        assert [states[f"n-{i}"] for i in (1, 2, 3)] == ["posted"] * 3

        # wrong account: every gh call runs in the repo toplevel with neither token forwarded
        log = gh.log()
        assert log
        toplevel = str(repo.resolve())
        for entry in log:
            assert entry["cwd"] == toplevel
            assert entry["gh_token"] is False
            assert entry["github_token"] is False


def test_invalid_event_and_empty_request_changes_body_are_400_before_any_gh_call(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]) as gh:
        _add_notes(d, [_note(id="n-1", body="b")])
        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            pre = _preview_ok(daemon, key, wid)
            n_calls = len(gh.log())
            for extra in ({"event": "REQUEST_CHANGES", "body": "  \n"}, {"event": "REQUEST_CHANGES"},
                          {"event": "DISMISS"}):
                status, raw = _post(daemon, key, wid, pre["nonce"], [], **extra)
                assert status == 400, (extra, status, raw)
            assert len(gh.log()) == n_calls


def test_existing_pending_review_is_reused_and_its_comment_count_previewed(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[], pending=2, pr_author="someone") as gh:
        _add_notes(d, [_note(id="n-1", body="b")])
        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            pre = _preview_ok(daemon, key, wid)
            assert pre["review"] == {"pending": True, "comments": 2}
            assert pre["own_pr"] is False
            status, raw = _post(daemon, key, wid, pre["nonce"], [])
            assert status == 200, (status, raw)
            assert json.loads(raw)["submitted"] is True
        assert gh.count("OpenReview") == 0 and gh.count("SubmitReview") == 1


def test_partial_failure_keeps_delivered_in_review_runs_no_resolve_and_retry_sends_only_failed(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[], fail_nth={"NewThread": [2]}) as gh:
        root = _note(id="gh-501", origin="github", state="posted", gh_id=501,
                     gh_node_id="NODE_501", gh_thread_id="THREAD_1", resolved=False, body="root")
        _add_notes(d, [root, *[_note(id=f"n-{i}", body=f"body {i}", order=i) for i in (1, 2, 3)]])
        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            pre = _preview_ok(daemon, key, wid, ["gh-501"])
            status, raw = _post(daemon, key, wid, pre["nonce"], ["gh-501"])
            assert status == 200, (status, raw)
            reply = json.loads(raw)
            assert reply["ok"] is False and reply["partial"] is True
            assert reply["delivered"] == 2 and reply["in_review"] == 2
            assert [f["id"] for f in reply["failed"]] == ["n-2"] and reply["failed"][0]["error"]
            assert [(r["id"], r["ok"]) for r in reply["results"]] == [
                ("n-1", True), ("n-2", False), ("n-3", True)]
            assert gh.count("SubmitReview") == 0 and gh.count("ResolveThread") == 0
            states = {n["id"]: n["state"] for n in json.loads((d / "state.json").read_text())["notes"]}
            assert (states["n-1"], states["n-2"], states["n-3"]) == ("in_review", "draft", "in_review")

            pre2 = _preview_ok(daemon, key, wid, ["gh-501"])
            assert [n["id"] for n in pre2["notes"]] == ["n-2"]
            assert {i["id"] for i in pre2["in_review"]} == {"n-1", "n-3"}
            assert pre2["review"]["comments"] == 2
            status, raw = _post(daemon, key, wid, pre2["nonce"], ["gh-501"])
            assert status == 200 and json.loads(raw)["ok"] is True, raw
        assert gh.count("NewThread") == 4  # three first tries (one rejected) + the one retry
        assert gh.count("OpenReview") == 1 and gh.count("SubmitReview") == 1
        assert gh.count("ResolveThread") == 1


def test_submit_only_submits_the_pending_review_without_redelivering(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[], fail_nth={"NewThread": [2]}) as gh:
        _add_notes(d, [_note(id=f"n-{i}", body=f"body {i}", order=i) for i in (1, 2)])
        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            pre = _preview_ok(daemon, key, wid)
            status, raw = _post(daemon, key, wid, pre["nonce"], [])
            assert json.loads(raw)["partial"] is True

            status, raw = _put_notes(daemon, key, wid, [_note(id="n-1", body="overwrite"),
                                                        _note(id="n-2", body="body 2")])
            assert status == 200 and json.loads(raw)["count"] == 1, raw
            notes_in_state = {n["id"]: n for n in json.loads((d / "state.json").read_text())["notes"]}
            assert notes_in_state["n-1"]["state"] == "in_review"
            assert notes_in_state["n-1"]["body"] == "body 1"

            status, raw = _post(daemon, key, wid, "stale-nonce-ignored", [], submit_only=True,
                                event="COMMENT", body="partial")
            assert status == 200, (status, raw)
            assert json.loads(raw)["submitted"] is True
        assert gh.count("NewThread") == 2 and gh.count("SubmitReview") == 1
        states = {n["id"]: n["state"] for n in json.loads((d / "state.json").read_text())["notes"]}
        assert (states["n-1"], states["n-2"]) == ("posted", "draft")


def test_retry_after_a_lost_response_does_not_deliver_twice(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[], lose_response_ops=("NewThread",)) as gh:
        _add_notes(d, [_note(id="n-1", body="lost body")])
        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            pre = _preview_ok(daemon, key, wid)
            status, raw = _post(daemon, key, wid, pre["nonce"], [])
            assert json.loads(raw)["partial"] is True
            assert gh.count("NewThread") == 1

            pre2 = _preview_ok(daemon, key, wid)
            assert pre2["notes"] == []
            status, raw = _post(daemon, key, wid, pre2["nonce"], [])
            assert status == 200, (status, raw)
            assert gh.count("NewThread") == 1
            # the comment is still in the user's pending review, reachable via submit_only
            assert pre2["review"] == {"pending": True, "comments": 1}
            assert gh.count("SubmitReview") == 0


def test_ids_narrow_the_preview_and_the_nonce_tracks_the_id_set_and_bodies(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]) as gh:
        _add_notes(d, [_note(id=f"n-{i}", body=f"body {i}", order=i) for i in (1, 2)])
        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            both = _preview_ok(daemon, key, wid)
            one = _preview_ok(daemon, key, wid, ids=["n-1"])
            assert [n["id"] for n in one["notes"]] == ["n-1"]
            assert one["nonce"] != both["nonce"]

            status, raw = _post(daemon, key, wid, both["nonce"], [], ids=["n-1"])
            assert status == 409
            _put_notes(daemon, key, wid, [_note(id="n-1", body="edited")])
            assert _preview_ok(daemon, key, wid, ids=["n-1"])["nonce"] != one["nonce"]

            status, raw = _post(daemon, key, wid, _preview_ok(daemon, key, wid, ids=["n-1"])["nonce"],
                                [], ids=["n-1"])
            assert status == 200, (status, raw)
        assert gh.count("NewThread") == 1


# ---------------------------------------------------------------------------
# No PR, or an unpushed head, blocks posting with today's exact message
# ---------------------------------------------------------------------------

def test_no_pr_or_unpushed_head_blocks_posting_with_exact_messages(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]):
        state_path = d / "state.json"
        key, wid = d.parent.name, d.name

        state = json.loads(state_path.read_text())
        assert state["meta"]["pr"] == 7
        assert state["meta"]["head_pushed"] is True

        with running_daemon() as daemon:
            state["meta"]["pr"] = None
            state_path.write_text(json.dumps(state))
            status, raw = _preview(daemon, key, wid, [])
            assert status == 409, (status, raw)
            assert json.loads(raw)["error"] == NO_PR_MSG

            state["meta"]["pr"] = 7
            state["meta"]["head_pushed"] = False
            state_path.write_text(json.dumps(state))
            status, raw = _preview(daemon, key, wid, [])
            assert status == 409, (status, raw)
            assert json.loads(raw)["error"] == HEAD_NOT_PUSHED_MSG


# ---------------------------------------------------------------------------
# After a post, page-notes.json lacks the delivered id; a PUT carrying a
# posted id drops it
# ---------------------------------------------------------------------------

def test_page_notes_json_rewritten_and_put_drops_a_posted_id(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]):
        key, wid = d.parent.name, d.name

        with running_daemon() as daemon:
            status, raw = _put_notes(daemon, key, wid, [_note(id="n-1", body="body")])
            assert status == 200, (status, raw)
            page_notes = json.loads((d / "page-notes.json").read_text())
            assert any(n["id"] == "n-1" for n in page_notes["notes"])

            status, raw = _preview(daemon, key, wid, [])
            assert status == 200, (status, raw)
            nonce = json.loads(raw)["nonce"]
            status, raw = _post(daemon, key, wid, nonce, [])
            assert status == 200, (status, raw)

            page_notes = json.loads((d / "page-notes.json").read_text())
            assert not any(n["id"] == "n-1" for n in page_notes.get("notes", []))

            status, raw = _put_notes(daemon, key, wid, [_note(id="n-1", body="body")])
            assert status == 200, (status, raw)
            assert json.loads(raw)["count"] == 0
            page_notes = json.loads((d / "page-notes.json").read_text())
            assert page_notes["notes"] == []


# ---------------------------------------------------------------------------
# A genuinely non-PR walkthrough (dir-level meta.pr/gh_repo both None) 409s
# instead of crashing sync_pr with an AttributeError and dropping the
# connection
# ---------------------------------------------------------------------------

def test_post_preview_and_post_409_on_a_genuinely_non_pr_walkthrough(done_no_pr, tmp_path):
    d = done_no_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]) as gh:
        meta = cw_store.read_meta(d)
        assert meta.get("pr") is None
        assert meta.get("gh_repo") is None

        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            status, raw = _preview(daemon, key, wid, [])
            assert status == 409, (status, raw)
            assert json.loads(raw)["error"] == NO_PR_MSG

            status, raw = _post(daemon, key, wid, "whatever", [])
            assert status == 409, (status, raw)
            assert json.loads(raw)["error"] == NO_PR_MSG

        assert gh.count("rest-comments") == 0  # sync_pr never ran


# ---------------------------------------------------------------------------
# A render failure after a successful post reports ok: false with the
# render_error, instead of a hardcoded "ok: true"
# ---------------------------------------------------------------------------

def test_render_failure_after_post_reports_ok_false_with_render_error(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]):
        draft = _note(id="n-1", body="body", path="foo.py", line=2, side="RIGHT")
        _add_notes(d, [draft])

        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            status, raw = _preview(daemon, key, wid, [])
            assert status == 200, (status, raw)
            nonce = json.loads(raw)["nonce"]

            (d / "analysis.json").unlink()  # force pipeline.py render to fail

            status, raw = _post(daemon, key, wid, nonce, [])
            assert status == 200, (status, raw)
            body = json.loads(raw)
            assert body["ok"] is False
            assert body.get("render_error")
            assert any(r["id"] == "n-1" and r["ok"] for r in body["results"])


# ---------------------------------------------------------------------------
# A failed or interrupted build gets its own message, not "still building"
# ---------------------------------------------------------------------------

def test_failed_or_interrupted_meta_gives_its_own_message(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]):
        key, wid = d.parent.name, d.name

        for bad_status in ("failed", "interrupted"):
            cw_store.update_meta(d, lambda m, s=bad_status: m.update(status=s))
            with running_daemon() as daemon:
                status, raw = _preview(daemon, key, wid, [])
                assert status == 409, (status, raw)
                body = json.loads(raw)
                assert body["error"] == cw_server.FAILED_MSG, (bad_status, body)
                assert body["error"] != cw_server.STILL_BUILDING_MSG
                assert body.get("remedy")


# ---------------------------------------------------------------------------
# Review-fix: empty approve, stuck in_review, partial consistency, payload validation
# ---------------------------------------------------------------------------

def test_approve_or_body_with_nothing_in_the_review_is_400_without_any_mutation(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]) as gh:
        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            pre = _preview_ok(daemon, key, wid)
            assert pre["notes"] == [] and pre["resolves"] == [] and pre["in_review"] == []
            for extra in ({"event": "APPROVE"}, {"event": "COMMENT", "body": "hi"},
                          {"event": "REQUEST_CHANGES", "body": "no"}):
                status, raw = _post(daemon, key, wid, pre["nonce"], [], **extra)
                assert status == 400, (extra, status, raw)
                assert "nothing to submit" in json.loads(raw)["error"]
                assert json.loads(raw)["remedy"]
            status, raw = _post(daemon, key, wid, pre["nonce"], [])
            assert status == 200 and json.loads(raw)["submitted"] is False
        assert _ops(gh) == []


@pytest.mark.parametrize("event, body", [("APPROVE", ""), ("REQUEST_CHANGES", "fix it")])
def test_approve_and_request_changes_with_drafts_submit_with_that_event(done_pr, tmp_path, event, body):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]) as gh:
        _add_notes(d, [_note(id="n-1", body="b")])
        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            pre = _preview_ok(daemon, key, wid)
            status, raw = _post(daemon, key, wid, pre["nonce"], [], event=event, body=body)
            assert status == 200 and json.loads(raw)["submitted"] is True, raw
        submit = next(e for e in gh.log() if e["op"] == "SubmitReview")
        assert submit["variables"]["event"] == event


def test_submit_failure_after_deliveries_is_partial_with_no_resolves_and_notes_stay_in_review(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[], fail_ops=("SubmitReview",)) as gh:
        root = _note(id="gh-501", origin="github", state="posted", gh_id=501,
                     gh_node_id="NODE_501", gh_thread_id="THREAD_1", resolved=False, body="root")
        _add_notes(d, [root, _note(id="n-1", body="b")])
        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            pre = _preview_ok(daemon, key, wid, ["gh-501"])
            status, raw = _post(daemon, key, wid, pre["nonce"], ["gh-501"])
            reply = json.loads(raw)
            assert status == 200 and reply["partial"] is True and reply["ok"] is False
            assert [f["id"] for f in reply["failed"]] == ["submit"]
            assert reply["delivered"] == 1 and reply["in_review"] == 1
        assert gh.count("ResolveThread") == 0
        states = {n["id"]: n["state"] for n in json.loads((d / "state.json").read_text())["notes"]}
        assert states["n-1"] == "in_review"


def test_in_review_with_the_pending_review_gone_is_reset_and_deliverable_again(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]) as gh:
        _add_notes(d, [_note(id="n-1", body="b", state="in_review", gh_id=9,
                             gh_node_id="N9", gh_thread_id="T9", gh_url="u")])
        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            pre = _preview_ok(daemon, key, wid)
            assert pre["reset"] == ["n-1"] and pre["in_review"] == []
            assert [n["id"] for n in pre["notes"]] == ["n-1"]
            status, raw = _post(daemon, key, wid, pre["nonce"], [])
            reply = json.loads(raw)
            assert status == 200 and reply["ok"] is True and reply["submitted"] is True, raw
            assert reply["reset"] == []
        assert gh.count("NewThread") == 1 and gh.count("SubmitReview") == 1
        note = next(n for n in json.loads((d / "state.json").read_text())["notes"] if n["id"] == "n-1")
        assert note["state"] == "posted" and note["gh_thread_id"] != "T9"


def test_in_review_note_already_submitted_elsewhere_becomes_posted_not_reset(done_pr_with_gh_comment, tmp_path):
    d = done_pr_with_gh_comment.d
    with cw_testlib.fake_gh(tmp_path, comments=[GH_COMMENT], threads=[]):
        _add_notes(d, [_note(id="n-1", body="same body", state="in_review")])
        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            pre = _preview_ok(daemon, key, wid)
            assert pre["reset"] == [] and pre["notes"] == [] and pre["in_review"] == []
        note = next(n for n in json.loads((d / "state.json").read_text())["notes"] if n["id"] == "n-1")
        assert note["state"] == "posted"


def test_ids_without_a_draft_parent_and_bad_resolve_payloads_are_400(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]) as gh:
        _add_notes(d, [_note(id="n-parent", body="p", order=1),
                       _note(id="n-reply", body="r", order=2, reply_to="n-parent")])
        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            for call in (lambda **kw: _preview(daemon, key, wid, [], **kw),
                         lambda **kw: _post(daemon, key, wid, "x", [], **kw)):
                status, raw = call(ids=["n-reply"])
                assert status == 400, (status, raw)
                assert "parent" in json.loads(raw)["remedy"]
            for bad in ("n-1", ["n-1", 3], {"a": 1}):
                status, raw = _preview(daemon, key, wid, bad)
                assert status == 400, (bad, status, raw)
                status, raw = _post(daemon, key, wid, "x", bad)
                assert status == 400, (bad, status, raw)
        assert _ops(gh) == []


def test_partial_result_rerenders_and_cleans_page_notes(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[], fail_nth={"NewThread": [2]}) as gh:
        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            status, raw = _put_notes(daemon, key, wid, [_note(id="n-1", body="one", order=1),
                                                        _note(id="n-2", body="two", order=2)])
            assert status == 200, (status, raw)
            rev_before = cw_store.read_meta(d)["rev"]
            q = daemon.hub.register(key, wid)
            pre = _preview_ok(daemon, key, wid)
            status, raw = _post(daemon, key, wid, pre["nonce"], [])
            reply = json.loads(raw)
            assert reply["partial"] is True and reply["delivered"] == 1
            assert cw_store.read_meta(d)["rev"] == rev_before + 1
            events = []
            while not q.empty():
                events.append(q.get_nowait())
            assert any("rebuilt" in str(e) for e in events), events
            daemon.hub.unregister(key, wid, q)
            remaining = {n["id"] for n in json.loads((d / "page-notes.json").read_text())["notes"]}
            assert remaining == {"n-2"}
        assert gh.count("SubmitReview") == 0


# ---------------------------------------------------------------------------
# publish-one: one draft straight to GitHub as its own review, or one resolve
# ---------------------------------------------------------------------------

BODY = "  keep the spaces\n```py\nx = 1\n```\n<b>now</b> \U0001F680\n"


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _publish(daemon, d, **body):
    return _request(daemon, "POST", f"/api/walkthrough/{d.parent.name}/{d.name}/publish-one",
                     token=daemon.token, body=json.dumps(body))


def _rest_calls(gh):
    return [e for e in gh.log() if e["op"] in ("rest-post-comment", "rest-post-reply")]


def _qa(d, *records):
    with open(d / "qa.jsonl", "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _thread_notes():
    root = _note(id="gh-501", origin="github", state="posted", gh_id=501, gh_node_id="N501",
                 gh_thread_id="T1", resolved=False, body="root", author="rev")
    reply = _note(id="gh-502", origin="github", state="posted", gh_id=502, gh_node_id="N502",
                  gh_thread_id="T1", resolved=False, body="reply", reply_to="gh-501", author="rev")
    return [root, reply]


def _outcome(oid, kind, state, payload):
    return {"type": "outcome", "oid": oid, "qid": "q-00000001", "thread_id": "q-00000001",
            "outcome": kind, "state": state, "payload": payload, "at": "t"}


def test_publish_one_sends_a_single_rest_call_with_the_exact_body_and_posts_the_note(done_pr, tmp_path):
    d = done_pr.d
    head = done_pr.head
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]) as gh:
        _add_notes(d, [_note(id="n-1", body=BODY, end_line=None), _note(id="n-2", body="other", line=3)])
        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            pre = _preview_ok(daemon, key, wid)
            assert {n["id"] for n in pre["notes"]} == {"n-1", "n-2"}
            q = daemon.hub.register(key, wid)
            status, raw = _publish(daemon, d, id="n-1", body_sha=_sha(BODY))
            assert status == 200, (status, raw)
            reply = json.loads(raw)
            assert reply["ok"] is True and reply["id"] == "n-1"
            assert reply["gh_url"].endswith("discussion_r2001")
            events = []
            while not q.empty():
                events.append(q.get_nowait())
            daemon.hub.unregister(key, wid, q)
            assert ("posted", {"kind": "note", "id": "n-1", "ok": True, "state": "posted"}) in events
            assert "rebuilt" in [e for e, _ in events]

            calls = _rest_calls(gh)
            assert len(calls) == 1 and calls[0]["op"] == "rest-post-comment"
            assert calls[0]["argv"][:2] == ["api", "repos/o/r/pulls/7/comments"]
            assert not any("keep the spaces" in a for a in calls[0]["argv"])
            assert calls[0]["variables"]["body"] == {
                "body": BODY, "commit_id": head, "path": "foo.py", "line": 2, "side": "RIGHT"}
            assert gh.count("OpenReview") == 0 and gh.count("NewThread") == 0

            state = json.loads((d / "state.json").read_text())
            posted = next(n for n in state["notes"] if n["id"] == "n-1")
            assert posted["state"] == "posted" and posted["gh_id"] == 2001
            assert posted["gh_url"] == reply["gh_url"]
            assert notes.pending_publish_ids(state) == ["n-2"]

            status, raw = _post(daemon, key, wid, pre["nonce"], [])
            assert status == 409 and json.loads(raw)["error"] == "changed since preview", (status, raw)
            assert gh.count("NewThread") == 0

            status, raw = _publish(daemon, d, id="n-1", body_sha=_sha(BODY))
            assert status == 409, (status, raw)
            assert len(_rest_calls(gh)) == 1


def test_publish_one_reply_is_addressed_to_the_top_level_root_and_needs_a_posted_parent(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]) as gh:
        _add_notes(d, [*_thread_notes(),
                       _note(id="n-r", body="reply body", reply_to="gh-502"),
                       _note(id="n-p", body="parent draft", line=3),
                       _note(id="n-c", body="child of a draft", line=3, reply_to="n-p")])
        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            status, raw = _publish(daemon, d, id="n-c", body_sha=_sha("child of a draft"))
            assert status == 409, (status, raw)
            assert json.loads(raw)["remedy"] == "publish the parent first"
            assert _rest_calls(gh) == []

            status, raw = _publish(daemon, d, id="n-r", body_sha=_sha("reply body"))
            assert status == 200, (status, raw)
            (call,) = _rest_calls(gh)
            assert call["op"] == "rest-post-reply"
            assert call["argv"][1] == "repos/o/r/pulls/7/comments/501/replies"
            assert call["variables"]["body"] == {"body": "reply body"}
            posted = next(n for n in json.loads((d / "state.json").read_text())["notes"] if n["id"] == "n-r")
            assert posted["state"] == "posted" and posted["gh_thread_id"] == "T1"

            assert _publish(daemon, d, id="n-p", body_sha=_sha("parent draft"))[0] == 200
            status, raw = _publish(daemon, d, id="n-c", body_sha=_sha("child of a draft"))
            assert status == 200, (status, raw)
            paths = [c["argv"][1] for c in _rest_calls(gh)]
            assert paths[-1].endswith("/comments/2002/replies"), paths


def test_publish_one_refusals_make_no_write_call(done_pr, tmp_path):
    d = done_pr.d
    head = done_pr.head
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[], pending=1) as gh:
        _add_notes(d, [*_thread_notes(), _note(id="n-1", body="hello"), _note(id="n-s", body="s", stale=True)])
        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            good = {"id": "n-1", "body_sha": _sha("hello")}
            assert _publish(daemon, d, id="n-1", body_sha=_sha("hello "))[0] == 409
            status, raw = _publish(daemon, d, id="n-1", body_sha=_sha("old text"))
            assert status == 409
            assert json.loads(raw) == {"error": "changed since you looked",
                                       "remedy": "check the text and publish again"}
            assert _publish(daemon, d, id="nope", body_sha="x")[0] == 404
            assert _publish(daemon, d, id="gh-501", body_sha=_sha("root"))[0] == 409
            assert _publish(daemon, d, id="n-s", body_sha=_sha("s"))[0] == 409
            assert _publish(daemon, d, **good, oid="o-ffffffff")[0] == 404
            for bad in ({}, {"id": "n-1"}, {"id": "n-1", "resolve": "gh-501", "body_sha": "x"},
                        {**good, "oid": "bad"}, {"id": 5, "body_sha": "x"}):
                assert _publish(daemon, d, **bad)[0] == 400, bad
            assert _request(daemon, "POST", f"/api/walkthrough/{key}/{wid}/publish-one",
                            body=json.dumps(good))[0] == 403

            status, raw = _publish(daemon, d, **good)
            assert status == 409, (status, raw)
            assert json.loads(raw) == {"error": "you have a pending review on this PR",
                                       "remedy": "Submit review, or finish it on GitHub"}
            assert _rest_calls(gh) == []

            cw_store.update_meta(d, lambda m: m.update(sibling_of="deadbeef"))
            before = len(gh.log())
            status, raw = _publish(daemon, d, **good)
            assert status == 409 and json.loads(raw) == {
                "error": "publishing is off in this view", "remedy": "go back to the PR head"}
            assert len(gh.log()) == before
    assert next(n for n in json.loads((d / "state.json").read_text())["notes"]
                if n["id"] == "n-1")["state"] == "draft"


def test_publish_one_gh_failure_is_502_and_leaves_the_draft_and_outcome_alone(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[], fail_ops=["rest-post-comment"]) as gh:
        _add_notes(d, [_note(id="n-1", body="hello")])
        _qa(d, _outcome("o-0000aaaa", "github_draft", "kept", {"body": "hello", "note_id": "n-1"}))
        with running_daemon() as daemon:
            status, raw = _publish(daemon, d, id="n-1", body_sha=_sha("hello"), oid="o-0000aaaa")
            assert status == 502, (status, raw)
            reply = json.loads(raw)
            assert reply["error"] == "the comment may already be on GitHub; check the PR before retrying"
            assert reply["remedy"]
            assert len(_rest_calls(gh)) == 1
    note = next(n for n in json.loads((d / "state.json").read_text())["notes"] if n["id"] == "n-1")
    assert note["state"] == "draft" and note["gh_id"] is None
    assert [o["state"] for o in cw_ask.read_outcomes(d)] == ["kept"]


def test_publish_one_marks_the_draft_outcome_published_and_needs_it_open(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]) as gh:
        _add_notes(d, [_note(id="n-1", body="hello")])
        _qa(d, _outcome("o-0000aaaa", "github_draft", "kept", {"body": "hello", "note_id": "n-1"}),
            _outcome("o-0000bbbb", "github_draft", "dismissed", {"body": "hello", "note_id": "n-1"}),
            _outcome("o-0000cccc", "resolve", "proposed", {"thread": "gh-501", "why": "w"}),
            _outcome("o-0000dddd", "github_draft", "kept", {"body": "hello", "note_id": "n-other"}),
            _outcome("o-0000eeee", "github_draft", "proposed", {"body": "hello", "note_id": "n-1"}))
        _add_notes(d, [*_thread_notes(), _note(id="n-other", body="x", line=3)])
        with running_daemon() as daemon:
            for oid in ("o-0000bbbb", "o-0000cccc", "o-0000dddd", "o-0000eeee"):
                assert _publish(daemon, d, id="n-1", body_sha=_sha("hello"), oid=oid)[0] == 409
            assert _publish(daemon, d, resolve="gh-501", oid="o-0000aaaa")[0] == 409
            assert gh.count("ResolveThread") == 0
            assert _rest_calls(gh) == []
            status, raw = _publish(daemon, d, id="n-1", body_sha=_sha("hello"), oid="o-0000aaaa")
            assert status == 200, (status, raw)
    states = {o["oid"]: o["state"] for o in cw_ask.read_outcomes(d)}
    assert states["o-0000aaaa"] == "published" and states["o-0000bbbb"] == "dismissed"


def test_publish_one_resolve_resolves_the_thread_and_marks_the_outcome_done(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]) as gh:
        _add_notes(d, _thread_notes())
        _qa(d, _outcome("o-0000cccc", "resolve", "proposed", {"thread": "gh-501", "why": "w"}))
        with running_daemon() as daemon:
            assert _publish(daemon, d, resolve="gh-502")[0] == 409
            assert _publish(daemon, d, resolve="nope")[0] == 404
            assert gh.count("ResolveThread") == 0
            status, raw = _publish(daemon, d, resolve="gh-501", oid="o-0000cccc")
            assert status == 200 and json.loads(raw) == {"ok": True}, (status, raw)
            assert gh.count("ResolveThread") == 1
            assert [o["state"] for o in cw_ask.read_outcomes(d)] == ["done"]
            assert gh.count("OpenReview") == 0 and _rest_calls(gh) == []
    notes_by_id = {n["id"]: n for n in json.loads((d / "state.json").read_text())["notes"]}
    assert notes_by_id["gh-501"]["resolved"] is True


def test_unpublished_draft_outcomes_stay_out_of_the_preview(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]):
        _add_notes(d, [_note(id="n-real", body="real draft")])
        _qa(d, _outcome("o-0000aaaa", "github_draft", "proposed", {
                "body": "proposal", "original": "o", "verbatim": False,
                "target": {"kind": "new", "path": "foo.py", "line": 2, "side": "RIGHT"}}),
            _outcome("o-0000bbbb", "github_draft", "kept", {
                "body": "kept one", "note_id": "n-kept", "target": {"kind": "new"}}))
        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            pre = _preview_ok(daemon, key, wid)
    assert [n["id"] for n in pre["notes"]] == ["n-real"]
    assert "proposal" not in json.dumps(pre) and "kept one" not in json.dumps(pre)
    assert notes.pending_publish_ids(json.loads((d / "state.json").read_text())) == ["n-real"]


def test_publish_one_fails_closed_when_the_pending_review_lookup_fails(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[], fail_ops=["PendingReview"]) as gh:
        _add_notes(d, [_note(id="n-1", body="hello")])
        with running_daemon() as daemon:
            status, raw = _publish(daemon, d, id="n-1", body_sha=_sha("hello"))
            assert status == 502, (status, raw)
            assert json.loads(raw) == {"error": "could not check for a pending review", "remedy": "try again"}
            assert _rest_calls(gh) == []


def test_publish_one_lost_response_resyncs_and_reports_ok_without_a_second_post(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[], lose_response_ops=["rest-post-comment"]) as gh:
        _add_notes(d, [_note(id="n-1", body="hello")])
        with running_daemon() as daemon:
            status, raw = _publish(daemon, d, id="n-1", body_sha=_sha("hello"))
            assert status == 200 and json.loads(raw)["ok"] is True, (status, raw)
            assert len(_rest_calls(gh)) == 1
    note = next(n for n in json.loads((d / "state.json").read_text())["notes"] if n["id"] == "n-1")
    assert note["state"] == "posted" and note["gh_id"] == 2001


def test_publish_one_without_raw_diff_refuses_a_top_level_draft_before_the_post(done_pr, tmp_path):
    d = done_pr.d
    with cw_testlib.fake_gh(tmp_path, comments=[], threads=[]) as gh:
        _add_notes(d, [_note(id="n-1", body="hello")])
        (d / "raw.diff").unlink()
        with running_daemon() as daemon:
            status, raw = _publish(daemon, d, id="n-1", body_sha=_sha("hello"))
            assert status == 502 and "raw.diff" in json.loads(raw)["error"], (status, raw)
            assert _rest_calls(gh) == []


EXACT_BODY = "  lead spaces\n`code` <b>x</b> \U0001F600 tail\n"


def test_direct_draft_and_reply_draft_make_no_model_call_and_keep_exact_body(done_pr, tmp_path):
    d = done_pr.d
    home = Path(os.environ["CODE_WALKTHROUGH_HOME"])
    with cw_testlib.StubLLM(lambda body, _n: cw_testlib.small_route_reply(body)) as stub, \
            cw_testlib.fake_claude(tmp_path, {}) as fc, \
            cw_testlib.fake_gh(tmp_path, comments=[], threads=[]):
        # the snapshot's config points at a dead stub; point it at a live one so a model call would be counted
        cw_testlib.write_route_config(home, stub, ask=False)
        root = _note(id="gh-501", origin="github", state="posted", gh_id=501,
                     gh_node_id="NODE_501", gh_thread_id="THREAD_1", body="root")
        _add_notes(d, [root])
        baseline = len(stub.requests)
        qa = d / "qa.jsonl"
        qa_before = qa.read_text() if qa.exists() else ""

        direct = _note(id="n-direct", body=EXACT_BODY)
        reply = _note(id="n-reply", body=EXACT_BODY, reply_to="gh-501", in_reply_to="gh-501")
        key, wid = d.parent.name, d.name
        with running_daemon() as daemon:
            status, raw = _put_notes(daemon, key, wid, [direct, reply])
            assert status == 200, (status, raw)
            saved = {n["id"]: n for n in cw_store.read_json(d / "page-notes.json")["notes"]}
            assert saved["n-direct"]["body"] == EXACT_BODY
            assert saved["n-reply"]["body"] == EXACT_BODY
            status, raw = _preview(daemon, key, wid, [])
            assert status == 200, (status, raw)

        state = {n["id"]: n for n in cw_store.read_json(d / "state.json")["notes"]}
        assert state["n-direct"]["body"] == EXACT_BODY
        assert state["n-direct"]["origin"] == "local" and state["n-direct"]["state"] == "draft"
        assert state["n-reply"]["body"] == EXACT_BODY
        # PAGE_DENIED_NOTE_FIELDS strips reply_to; in_reply_to is the durable link.
        assert state["n-reply"]["reply_to"] is None
        assert state["n-reply"]["in_reply_to"] == "gh-501"
        assert len(stub.requests) == baseline
        assert fc.log() == []
        assert (qa.read_text() if qa.exists() else "") == qa_before
