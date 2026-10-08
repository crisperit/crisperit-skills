#!/usr/bin/env python3
"""Self-check for phase 5: post to GitHub from the page (phase345-spec.md, "Phase 5",
implementer A). Assert-based, no framework; also collected by pytest. In-process
cw_server.Daemon, real cw_run.py/notes.py, cw_testlib.fake_gh standing in for `gh`,
cw_testlib.StubLLM standing in for the model backend -- no fakes of our own modules besides
those two external boundaries, same convention as test_cw_pr.py.
"""

import contextlib
import http.client
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_llm  # noqa: E402
import cw_run  # noqa: E402
import cw_server  # noqa: E402
import cw_store  # noqa: E402
import cw_testlib  # noqa: E402
import notes  # noqa: E402

cw_llm.BACKOFF_S = [0, 0]

NO_PR_MSG = "No PR for this comparison -- use Copy for agent instead."
HEAD_NOT_PUSHED_MSG = "The compared commit has not been pushed yet -- use Copy for agent instead."


def _pr_repo(tmp):
    """A one-line-change repo with a GitHub origin and a remote-tracking ref at head, so
    links.py's head_pushed check (git branch -r --contains) is true -- the one precondition
    posting needs beyond having a PR number at all."""
    repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\nb\nc\n"}, {"foo.py": "a\nX\nc\n"})
    cw_testlib.git(repo, "remote", "add", "origin", "https://github.com/o/r.git")
    cw_testlib.git(repo, "update-ref", "refs/remotes/origin/feat", head)
    return repo, base, head


def _small_route_reply(body):
    seed = cw_testlib.first_json_block(cw_testlib.last_user_text(body))
    analysis = {
        "files": [{"path": f["path"], "role": "does a thing",
                   "hunks": [{"header": h["header"], "note": "explains it"} for h in f["hunks"]]}
                  for f in seed["files"]],
        "overview": "o", "verdict": "v", "flow_mermaid": "",
    }
    return cw_testlib.tool_call("submit_analysis", analysis)


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


def _write_config(home, stub):
    cw_testlib.write_config(
        home, {"a": stub.profile("analysis-m"), "p": stub.profile("prose-m")},
        {"analysis": "a", "prose": "p"},
    )


def _build_done_pr(home, tmp, stub, pr=7):
    repo, base, head = _pr_repo(tmp)
    _write_config(home, stub)
    d, meta, _reused = cw_run.prepare_walkthrough(
        {"repo": str(repo), "base": base, "head": head, "target": "t", "slug": "t", "pr": pr})
    status = cw_run.run(d, lambda ev, data: None)
    assert status == "done", status
    return repo, base, head, d


def _build_done_no_pr(home, tmp, stub):
    """A walkthrough built with no `pr` at all, so the directory-level meta's `pr`/`gh_repo`
    are None -- the precondition that crashed sync_pr before the fix (finding 1), distinct
    from the state.json-level pr=None case the other 409 test mutates after the fact."""
    repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\nb\nc\n"}, {"foo.py": "a\nX\nc\n"})
    _write_config(home, stub)
    d, meta, _reused = cw_run.prepare_walkthrough(
        {"repo": str(repo), "base": base, "head": head, "target": "t", "slug": "t"})
    status = cw_run.run(d, lambda ev, data: None)
    assert status == "done", status
    return repo, base, head, d


def _add_notes(d, extra_notes):
    state_path = d / "state.json"
    state = json.loads(state_path.read_text())
    state["notes"] = list(state.get("notes", [])) + list(extra_notes)
    state_path.write_text(json.dumps(state))
    return state_path


@contextlib.contextmanager
def running_daemon(idle_s=None):
    daemon = cw_server.Daemon(idle_s=idle_s, write_server_json=False)
    daemon.start()
    try:
        yield daemon
    finally:
        daemon.stop()


def _request(daemon, method, path, *, token=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{daemon.port}")
        if token is not None:
            conn.putheader("X-CW-Token", token)
        data = body.encode() if isinstance(body, str) else body
        if data is not None:
            conn.putheader("Content-Length", str(len(data)))
            conn.putheader("Content-Type", "application/json")
        conn.endheaders(data)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


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

def test_preview_lists_exactly_what_notes_py_considers_ready():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[]):
            repo, base, head, d = _build_done_pr(home, tmp, stub)

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

def test_edit_or_added_resolve_between_preview_and_post_gives_409():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[]) as gh:
            repo, base, head, d = _build_done_pr(home, tmp, stub)
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

def test_retry_after_timeout_does_not_double_post():
    os.environ["CW_GH_TIMEOUT"] = "1"
    try:
        with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
            with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                    cw_testlib.fake_gh(tmp, comments=[], threads=[], sleep={"NewThread": 3}) as gh:
                repo, base, head, d = _build_done_pr(home, tmp, stub)
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
    finally:
        os.environ.pop("CW_GH_TIMEOUT", None)


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


def test_submit_review_opens_once_delivers_all_submits_once_then_resolves():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[]) as gh:
            repo, base, head, d = _build_done_pr(home, tmp, stub)
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
                                    body="ship it", submit=False)
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


def test_empty_preview_never_opens_a_review():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[]) as gh:
            repo, base, head, d = _build_done_pr(home, tmp, stub)
            key, wid = d.parent.name, d.name
            with running_daemon() as daemon:
                pre = _preview_ok(daemon, key, wid)
                assert pre["notes"] == [] and pre["resolves"] == [] and pre["in_review"] == []
                status, raw = _post(daemon, key, wid, pre["nonce"], [], submit=True)
                assert status == 200, (status, raw)
                assert json.loads(raw)["submitted"] is False
            assert _ops(gh) == []


def test_invalid_event_and_empty_request_changes_body_are_400_before_any_gh_call():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[]) as gh:
            repo, base, head, d = _build_done_pr(home, tmp, stub)
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


def test_existing_pending_review_is_reused_and_its_comment_count_previewed():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[], pending=2, pr_author="someone") as gh:
            repo, base, head, d = _build_done_pr(home, tmp, stub)
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


def test_partial_failure_keeps_delivered_in_review_runs_no_resolve_and_retry_sends_only_failed():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[], fail_nth={"NewThread": [2]}) as gh:
            repo, base, head, d = _build_done_pr(home, tmp, stub)
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


def test_submit_only_submits_the_pending_review_without_redelivering():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[], fail_nth={"NewThread": [2]}) as gh:
            repo, base, head, d = _build_done_pr(home, tmp, stub)
            _add_notes(d, [_note(id=f"n-{i}", body=f"body {i}", order=i) for i in (1, 2)])
            key, wid = d.parent.name, d.name
            with running_daemon() as daemon:
                pre = _preview_ok(daemon, key, wid)
                status, raw = _post(daemon, key, wid, pre["nonce"], [])
                assert json.loads(raw)["partial"] is True
                status, raw = _post(daemon, key, wid, "stale-nonce-ignored", [], submit_only=True,
                                    event="COMMENT", body="partial")
                assert status == 200, (status, raw)
                assert json.loads(raw)["submitted"] is True
            assert gh.count("NewThread") == 2 and gh.count("SubmitReview") == 1
            states = {n["id"]: n["state"] for n in json.loads((d / "state.json").read_text())["notes"]}
            assert (states["n-1"], states["n-2"]) == ("posted", "draft")


def test_retry_after_a_lost_response_does_not_deliver_twice():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[], lose_response_ops=("NewThread",)) as gh:
            repo, base, head, d = _build_done_pr(home, tmp, stub)
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


def test_ids_narrow_the_preview_and_the_nonce_tracks_the_id_set_and_bodies():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[]) as gh:
            repo, base, head, d = _build_done_pr(home, tmp, stub)
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


def test_in_review_notes_survive_a_notes_put():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[], fail_nth={"NewThread": [2]}):
            repo, base, head, d = _build_done_pr(home, tmp, stub)
            _add_notes(d, [_note(id=f"n-{i}", body=f"body {i}", order=i) for i in (1, 2)])
            key, wid = d.parent.name, d.name
            with running_daemon() as daemon:
                pre = _preview_ok(daemon, key, wid)
                _post(daemon, key, wid, pre["nonce"], [])
                status, raw = _put_notes(daemon, key, wid, [_note(id="n-1", body="overwrite"),
                                                            _note(id="n-2", body="body 2")])
                assert status == 200 and json.loads(raw)["count"] == 1, raw
                notes_in_state = {n["id"]: n for n in json.loads((d / "state.json").read_text())["notes"]}
                assert notes_in_state["n-1"]["state"] == "in_review"
                assert notes_in_state["n-1"]["body"] == "body 1"


# ---------------------------------------------------------------------------
# No PR, or an unpushed head, blocks posting with today's exact message
# ---------------------------------------------------------------------------

def test_no_pr_or_unpushed_head_blocks_posting_with_exact_messages():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[]):
            repo, base, head, d = _build_done_pr(home, tmp, stub)
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
# deliver/submit/resolve run in the repo toplevel without GH_TOKEN/GITHUB_TOKEN
# ---------------------------------------------------------------------------

def test_gh_calls_run_in_toplevel_with_no_tokens():
    os.environ["GH_TOKEN"] = "x"
    os.environ["GITHUB_TOKEN"] = "y"
    try:
        with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
            with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                    cw_testlib.fake_gh(tmp, comments=[], threads=[]) as gh:
                repo, base, head, d = _build_done_pr(home, tmp, stub)
                root = _note(id="gh-501", origin="github", state="posted", gh_id=501,
                             gh_node_id="NODE_501", gh_thread_id="THREAD_1", resolved=False)
                draft = _note(id="n-1", body="body", path="foo.py", line=2, side="RIGHT")
                _add_notes(d, [root, draft])

                key, wid = d.parent.name, d.name
                with running_daemon() as daemon:
                    status, raw = _preview(daemon, key, wid, ["gh-501"])
                    assert status == 200, (status, raw)
                    nonce = json.loads(raw)["nonce"]
                    status, raw = _post(daemon, key, wid, nonce, ["gh-501"])
                    assert status == 200, (status, raw)

                log = gh.log()
                assert log
                toplevel = str(repo.resolve())
                for entry in log:
                    assert entry["cwd"] == toplevel
                    assert entry["gh_token"] is False
                    assert entry["github_token"] is False
    finally:
        os.environ.pop("GH_TOKEN", None)
        os.environ.pop("GITHUB_TOKEN", None)


# ---------------------------------------------------------------------------
# After a post, page-notes.json lacks the delivered id; a PUT carrying a
# posted id drops it
# ---------------------------------------------------------------------------

def test_page_notes_json_rewritten_and_put_drops_a_posted_id():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[]):
            repo, base, head, d = _build_done_pr(home, tmp, stub)
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

def test_post_preview_and_post_409_on_a_genuinely_non_pr_walkthrough():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[]) as gh:
            repo, base, head, d = _build_done_no_pr(home, tmp, stub)
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

def test_render_failure_after_post_reports_ok_false_with_render_error():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[]):
            repo, base, head, d = _build_done_pr(home, tmp, stub)
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

def test_failed_or_interrupted_meta_gives_its_own_message():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[]):
            repo, base, head, d = _build_done_pr(home, tmp, stub)
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

def test_approve_or_body_with_nothing_in_the_review_is_400_without_any_mutation():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[]) as gh:
            repo, base, head, d = _build_done_pr(home, tmp, stub)
            key, wid = d.parent.name, d.name
            with running_daemon() as daemon:
                pre = _preview_ok(daemon, key, wid)
                for extra in ({"event": "APPROVE"}, {"event": "COMMENT", "body": "hi"},
                              {"event": "REQUEST_CHANGES", "body": "no"}):
                    status, raw = _post(daemon, key, wid, pre["nonce"], [], **extra)
                    assert status == 400, (extra, status, raw)
                    assert "nothing to submit" in json.loads(raw)["error"]
                    assert json.loads(raw)["remedy"]
                status, raw = _post(daemon, key, wid, pre["nonce"], [])
                assert status == 200 and json.loads(raw)["submitted"] is False
            assert _ops(gh) == []


def test_approve_and_request_changes_with_drafts_submit_with_that_event():
    for event, body in (("APPROVE", ""), ("REQUEST_CHANGES", "fix it")):
        with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
            with cw_testlib.StubLLM(lambda b, _n: _small_route_reply(b)) as stub, \
                    cw_testlib.fake_gh(tmp, comments=[], threads=[]) as gh:
                repo, base, head, d = _build_done_pr(home, tmp, stub)
                _add_notes(d, [_note(id="n-1", body="b")])
                key, wid = d.parent.name, d.name
                with running_daemon() as daemon:
                    pre = _preview_ok(daemon, key, wid)
                    status, raw = _post(daemon, key, wid, pre["nonce"], [], event=event, body=body)
                    assert status == 200 and json.loads(raw)["submitted"] is True, raw
                submit = next(e for e in gh.log() if e["op"] == "SubmitReview")
                assert submit["variables"]["event"] == event


def test_submit_failure_after_deliveries_is_partial_with_no_resolves_and_notes_stay_in_review():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[], fail_ops=("SubmitReview",)) as gh:
            repo, base, head, d = _build_done_pr(home, tmp, stub)
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


def test_in_review_with_the_pending_review_gone_is_reset_and_deliverable_again():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[]) as gh:
            repo, base, head, d = _build_done_pr(home, tmp, stub)
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


def test_in_review_note_already_submitted_elsewhere_becomes_posted_not_reset():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        gh_comment = {"id": 77, "node_id": "N77", "path": "foo.py", "line": 2, "original_line": 2,
                      "side": "RIGHT", "body": "same body", "user": {"login": "me"},
                      "html_url": "u", "created_at": "2000-01-01T00:00:00Z",
                      "in_reply_to_id": None, "commit_id": "x", "original_commit_id": "x"}
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[gh_comment], threads=[]) as gh:
            repo, base, head, d = _build_done_pr(home, tmp, stub)
            _add_notes(d, [_note(id="n-1", body="same body", state="in_review")])
            key, wid = d.parent.name, d.name
            with running_daemon() as daemon:
                pre = _preview_ok(daemon, key, wid)
                assert pre["reset"] == [] and pre["notes"] == [] and pre["in_review"] == []
            note = next(n for n in json.loads((d / "state.json").read_text())["notes"] if n["id"] == "n-1")
            assert note["state"] == "posted"


def test_ids_without_a_draft_parent_and_bad_resolve_payloads_are_400():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[]) as gh:
            repo, base, head, d = _build_done_pr(home, tmp, stub)
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


def test_partial_result_rerenders_and_cleans_page_notes():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: _small_route_reply(body)) as stub, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[], fail_nth={"NewThread": [2]}) as gh:
            repo, base, head, d = _build_done_pr(home, tmp, stub)
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


if __name__ == "__main__":
    tests = [
        test_preview_lists_exactly_what_notes_py_considers_ready,
        test_edit_or_added_resolve_between_preview_and_post_gives_409,
        test_retry_after_timeout_does_not_double_post,
        test_submit_review_opens_once_delivers_all_submits_once_then_resolves,
        test_empty_preview_never_opens_a_review,
        test_invalid_event_and_empty_request_changes_body_are_400_before_any_gh_call,
        test_existing_pending_review_is_reused_and_its_comment_count_previewed,
        test_partial_failure_keeps_delivered_in_review_runs_no_resolve_and_retry_sends_only_failed,
        test_submit_only_submits_the_pending_review_without_redelivering,
        test_retry_after_a_lost_response_does_not_deliver_twice,
        test_ids_narrow_the_preview_and_the_nonce_tracks_the_id_set_and_bodies,
        test_in_review_notes_survive_a_notes_put,
        test_no_pr_or_unpushed_head_blocks_posting_with_exact_messages,
        test_gh_calls_run_in_toplevel_with_no_tokens,
        test_page_notes_json_rewritten_and_put_drops_a_posted_id,
        test_post_preview_and_post_409_on_a_genuinely_non_pr_walkthrough,
        test_render_failure_after_post_reports_ok_false_with_render_error,
        test_failed_or_interrupted_meta_gives_its_own_message,
        test_approve_or_body_with_nothing_in_the_review_is_400_without_any_mutation,
        test_approve_and_request_changes_with_drafts_submit_with_that_event,
        test_submit_failure_after_deliveries_is_partial_with_no_resolves_and_notes_stay_in_review,
        test_in_review_with_the_pending_review_gone_is_reset_and_deliverable_again,
        test_in_review_note_already_submitted_elsewhere_becomes_posted_not_reset,
        test_ids_without_a_draft_parent_and_bad_resolve_payloads_are_400,
        test_partial_result_rerenders_and_cleans_page_notes,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
