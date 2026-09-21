#!/usr/bin/env python3
"""Self-check for notes.py. Assert-based, no framework."""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import notes  # noqa: E402
from notes import (  # noqa: E402
    apply_resolutions,
    deliver_note,
    diff_anchors,
    do_apply_resolutions,
    do_deliver,
    do_import,
    do_payloads,
    do_promote,
    do_reanchor,
    do_resolved_threads,
    do_submit,
    do_sync,
    do_sync_threads,
    is_trivial_anchor,
    merge_state,
    payloads_for,
    pending_publish_ids,
    postable_ranges,
    promote_note,
    read_paginated_json,
    reanchor_note,
    resolve,
    resolved_threads,
    submit_review,
    sync_comments,
    sync_threads,
)

class _Args:
    """Stand-in for the argparse.Namespace the do_* functions read their options from."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _note(**overrides):
    note = {
        "id": "n-1", "origin": "local", "target": "pr", "state": "draft",
        "path": "src/app.py", "line": 10, "side": "RIGHT", "hunk_id": None,
        "anchor_text": "+    value = 1", "anchor_line": 10, "stale": False,
        "body": "a note", "order": 1, "author": "crisperit", "created_at": "2026-09-16T00:00:00Z",
        "gh_id": None, "gh_url": None, "reply_to": None,
    }
    note.update(overrides)
    return note


def _comment(**overrides):
    comment = {
        "id": 1, "line": 5, "original_line": 5, "position": 1, "original_position": 1,
        "side": "RIGHT", "path": "src/app.py", "body": "a comment",
        "user": {"login": "crisperit"}, "created_at": "2026-09-16T00:00:00Z",
        "html_url": "https://github.com/o/r/pull/1#discussion_r1", "in_reply_to_id": None,
    }
    comment.update(overrides)
    return comment


def _thread(**overrides):
    thread = {"id": "THREAD_1", "isResolved": False, "comments": {"nodes": [{"databaseId": 1}]}}
    thread.update(overrides)
    return thread


def _hunk_diff(path, header, lines):
    body = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
    body += header + "\n" + "\n".join(lines) + "\n"
    return body


# ---------------------------------------------------------------------------
# Phase 4: sync, payloads, promote
# ---------------------------------------------------------------------------

def test_reply_attaches_to_parent_even_when_it_appears_first():
    state = {"notes": []}
    parent = _comment(id=10, line=5, original_line=5, side="RIGHT", body="please fix",
                       created_at="t1", html_url="url10")
    reply = _comment(id=20, line=5, original_line=5, side="RIGHT", body="I agree",
                      user={"login": "bob"}, created_at="t2", html_url="url20",
                      in_reply_to_id=10)
    sync_comments(state, [reply, parent])

    by_gh_id = {n["gh_id"]: n for n in state["notes"]}
    assert by_gh_id[20]["reply_to"] == by_gh_id[10]["id"]
    assert by_gh_id[20]["reply_to_gh_id"] is None
    assert by_gh_id[20]["line"] == by_gh_id[10]["line"]
    assert by_gh_id[20]["side"] == by_gh_id[10]["side"]


def test_outdated_comment_lands_on_original_line_and_is_stale():
    state = {"notes": []}
    comment = _comment(id=30, line=None, original_line=42, side="LEFT", path="b.py",
                        body="dead code", created_at="t", html_url="url30")
    sync_comments(state, [comment])

    note = state["notes"][0]
    assert note["line"] == 42
    assert note["anchor_line"] == 42
    assert note["stale"] is True
    assert "position" not in note and "original_position" not in note


def test_sync_twice_is_idempotent():
    state = {"notes": []}
    comments = [
        _comment(id=1, line=5, side="RIGHT", body="one"),
        _comment(id=2, line=8, side="LEFT", body="two", user={"login": "bob"}),
    ]
    sync_comments(state, comments)
    first = json.dumps(state, sort_keys=True)
    sync_comments(state, comments)
    second = json.dumps(state, sort_keys=True)
    assert first == second
    assert len(state["notes"]) == 2


def test_sync_via_stdin_handles_paginated_concatenated_arrays():
    # gh api --paginate writes each page's raw array back to back, not one merged array.
    page1 = json.dumps([_comment(id=1, body="a")])
    page2 = json.dumps([_comment(id=2, body="b", user={"login": "bob"})])
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        state_path.write_text(json.dumps({"notes": []}))
        real_stdin = sys.stdin
        sys.stdin = io.StringIO(page1 + page2)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                do_sync(_Args(state=str(state_path)))
        finally:
            sys.stdin = real_stdin
        state = json.loads(state_path.read_text())
    assert {n["gh_id"] for n in state["notes"]} == {1, 2}


def test_dedupe_promotes_a_matching_draft_without_reposting():
    draft = _note(id="n-1", origin="local", state="draft", path="a.py", line=5, side="RIGHT",
                  body="nice catch", author="crisperit")
    state = {"notes": [draft]}
    comment = _comment(id=555, line=5, original_line=5, side="RIGHT", path="a.py",
                        body="nice catch", user={"login": "crisperit"}, html_url="url555")
    sync_comments(state, [comment])

    by_id = {n["id"]: n for n in state["notes"]}
    assert by_id["n-1"]["state"] == "posted"
    assert by_id["n-1"]["gh_id"] == 555
    assert by_id["n-1"]["gh_url"] == "url555"


def test_dedupe_matches_a_local_draft_despite_its_empty_author():
    # Page-authored notes are always written with author:'' -- the match must not rely on it.
    draft = _note(id="n-1", origin="local", state="draft", path="a.py", line=5, side="RIGHT",
                  body="nice catch", author="")
    state = {"notes": [draft]}
    comment = _comment(id=556, line=5, original_line=5, side="RIGHT", path="a.py",
                        body="nice catch", user={"login": "crisperit"}, html_url="url556")
    sync_comments(state, [comment])

    by_id = {n["id"]: n for n in state["notes"]}
    assert by_id["n-1"]["state"] == "posted"
    assert by_id["n-1"]["gh_id"] == 556


def test_sync_stores_diff_hunk_verbatim_on_a_new_note():
    state = {"notes": []}
    comment = _comment(id=40, diff_hunk="@@ -1,2 +1,2 @@\n-old\n+new")
    sync_comments(state, [comment])
    assert state["notes"][0]["diff_hunk"] == "@@ -1,2 +1,2 @@\n-old\n+new"


def test_second_sync_of_same_comment_leaves_diff_hunk_unchanged():
    state = {"notes": []}
    comment = _comment(id=41, diff_hunk="@@ -1,2 +1,2 @@\n-old\n+new")
    sync_comments(state, [comment])
    sync_comments(state, [comment])
    assert state["notes"][0]["diff_hunk"] == "@@ -1,2 +1,2 @@\n-old\n+new"


def test_sync_stores_original_commit_id_on_a_new_note():
    state = {"notes": []}
    comment = _comment(id=42, original_commit_id="abc123")
    sync_comments(state, [comment])
    assert state["notes"][0]["original_commit_id"] == "abc123"


def test_sync_stores_none_original_commit_id_when_absent():
    state = {"notes": []}
    comment = _comment(id=43)
    assert "original_commit_id" not in comment
    sync_comments(state, [comment])
    assert state["notes"][0]["original_commit_id"] is None


def test_second_sync_of_same_comment_leaves_original_commit_id_unchanged():
    state = {"notes": []}
    comment = _comment(id=44, original_commit_id="sha1")
    sync_comments(state, [comment])
    sync_comments(state, [comment])
    assert state["notes"][0]["original_commit_id"] == "sha1"


def test_local_draft_with_no_diff_hunk_round_trips_unharmed():
    draft = _note(id="n-1", origin="local", state="draft")
    assert "diff_hunk" not in draft
    state = {"notes": [draft]}
    sync_comments(state, [])
    by_id = {n["id"]: n for n in state["notes"]}
    assert "diff_hunk" not in by_id["n-1"]


# ---------------------------------------------------------------------------
# sync-threads (the additive GraphQL pass that resolves a conversation)
# ---------------------------------------------------------------------------

def test_sync_threads_sets_thread_id_and_resolved_on_the_matching_note():
    note = _note(id="n-1", gh_id=100)
    state = {"notes": [note]}
    sync_threads(state, [_thread(id="THREAD_1", isResolved=True,
                                  comments={"nodes": [{"databaseId": 100}]})])
    assert note["gh_thread_id"] == "THREAD_1"
    assert note["resolved"] is True


def test_sync_threads_maps_one_thread_onto_several_comments():
    root = _note(id="n-1", gh_id=100)
    reply = _note(id="n-2", gh_id=101, reply_to="n-1")
    state = {"notes": [root, reply]}
    sync_threads(state, [_thread(id="THREAD_1", isResolved=False,
                                  comments={"nodes": [{"databaseId": 100}, {"databaseId": 101}]})])
    assert root["gh_thread_id"] == reply["gh_thread_id"] == "THREAD_1"
    assert root["resolved"] is False and reply["resolved"] is False


def test_sync_threads_stores_resolved_by_login_on_every_note_in_the_thread():
    root = _note(id="n-1", gh_id=100)
    reply = _note(id="n-2", gh_id=101, reply_to="n-1")
    state = {"notes": [root, reply]}
    sync_threads(state, [_thread(id="THREAD_1", isResolved=True, resolvedBy={"login": "alice"},
                                  comments={"nodes": [{"databaseId": 100}, {"databaseId": 101}]})])
    assert root["resolved_by"] == reply["resolved_by"] == "alice"


def test_sync_threads_resolved_by_is_none_when_the_field_is_absent():
    note = _note(id="n-1", gh_id=100)
    state = {"notes": [note]}
    sync_threads(state, [_thread(id="THREAD_1", isResolved=True,
                                  comments={"nodes": [{"databaseId": 100}]})])
    assert note["resolved_by"] is None


def test_sync_threads_unresolve_clears_resolved_and_resolved_by():
    note = _note(id="n-1", gh_id=100)
    state = {"notes": [note]}
    sync_threads(state, [_thread(id="THREAD_1", isResolved=True, resolvedBy={"login": "alice"},
                                  comments={"nodes": [{"databaseId": 100}]})])
    assert note["resolved"] is True and note["resolved_by"] == "alice"

    sync_threads(state, [_thread(id="THREAD_1", isResolved=False,
                                  comments={"nodes": [{"databaseId": 100}]})])
    assert note["resolved"] is False
    assert note["resolved_by"] is None


def test_sync_threads_idempotent_with_resolved_by():
    note = _note(id="n-1", gh_id=100)
    state = {"notes": [note]}
    threads = [_thread(id="THREAD_1", isResolved=True, resolvedBy={"login": "alice"},
                        comments={"nodes": [{"databaseId": 100}]})]
    sync_threads(state, threads)
    first = json.dumps(state, sort_keys=True)
    sync_threads(state, threads)
    second = json.dumps(state, sort_keys=True)
    assert first == second


def test_sync_threads_leaves_a_comment_with_no_matching_note_alone():
    note = _note(id="n-1", gh_id=100)
    state = {"notes": [note]}
    sync_threads(state, [_thread(id="THREAD_1", isResolved=True,
                                  comments={"nodes": [{"databaseId": 999}]})])
    assert "gh_thread_id" not in note
    assert "resolved" not in note


def test_sync_threads_is_idempotent():
    note = _note(id="n-1", gh_id=100)
    state = {"notes": [note]}
    threads = [_thread(id="THREAD_1", isResolved=True,
                        comments={"nodes": [{"databaseId": 100}]})]
    sync_threads(state, threads)
    first = json.dumps(state, sort_keys=True)
    sync_threads(state, threads)
    second = json.dumps(state, sort_keys=True)
    assert first == second


def test_do_sync_threads_reads_the_graphql_response_shape_from_stdin():
    note = _note(id="n-1", gh_id=100)
    with tempfile.TemporaryDirectory() as tmp:
        state_path = _write_state(tmp, {"notes": [note]})
        payload = json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": [_thread(id="THREAD_1", isResolved=True,
                               comments={"nodes": [{"databaseId": 100}]})],
        }}}}})
        real_stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                do_sync_threads(_Args(state=state_path))
        finally:
            sys.stdin = real_stdin
        result = json.loads(Path(state_path).read_text())
    assert result["notes"][0]["gh_thread_id"] == "THREAD_1"
    assert result["notes"][0]["resolved"] is True


def test_do_sync_threads_warns_on_stderr_when_either_connection_is_truncated():
    note = _note(id="n-1", gh_id=100)
    with tempfile.TemporaryDirectory() as tmp:
        state_path = _write_state(tmp, {"notes": [note]})
        payload = json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": True},
            "nodes": [_thread(id="THREAD_1", isResolved=True,
                               comments={"nodes": [{"databaseId": 100}],
                                         "pageInfo": {"hasNextPage": True}})],
        }}}}})
        real_stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()) as err:
                do_sync_threads(_Args(state=state_path))
        finally:
            sys.stdin = real_stdin
    warnings = err.getvalue()
    assert "more than 100 review threads" in warnings
    assert "THREAD_1" in warnings and "more than 100 comments" in warnings


# ---------------------------------------------------------------------------
# resolved_threads / apply_resolutions (fanout_threads.py's input and output)
# ---------------------------------------------------------------------------

def test_resolved_threads_groups_several_notes_into_one_thread():
    root = _note(id="n-1", gh_thread_id="T1", resolved=True, resolved_by="alice",
                 path="src/auth.py", line=40, body="extract this",
                 created_at="2026-09-01T10:00:00Z")
    reply1 = _note(id="n-2", reply_to="n-1", gh_thread_id="T1", resolved=True,
                    resolved_by="alice", author="bob", body="done",
                    created_at="2026-09-02T09:00:00Z")
    reply2 = _note(id="n-3", reply_to="n-1", gh_thread_id="T1", resolved=True,
                    resolved_by="alice", author="alice", body="thanks",
                    created_at="2026-09-03T09:00:00Z")
    state = {"notes": [root, reply1, reply2]}

    result = resolved_threads(state)

    assert len(result["threads"]) == 1
    thread = result["threads"][0]
    assert thread["thread_id"] == "T1"
    assert thread["path"] == "src/auth.py" and thread["line"] == 40
    assert thread["body"] == "extract this" and thread["resolved_by"] == "alice"
    assert [r["body"] for r in thread["replies"]] == ["done", "thanks"]


def test_resolved_threads_finds_root_even_when_it_is_not_first():
    reply = _note(id="n-2", reply_to="n-1", gh_thread_id="T1", resolved=True,
                   body="reply body", created_at="2026-09-02T09:00:00Z")
    root = _note(id="n-1", gh_thread_id="T1", resolved=True, path="a.py", line=1,
                 body="root body", created_at="2026-09-01T10:00:00Z")
    state = {"notes": [reply, root]}  # root appears second in notes[]

    thread = resolved_threads(state)["threads"][0]

    assert thread["body"] == "root body"
    assert thread["replies"][0]["body"] == "reply body"


def test_resolved_threads_excludes_unresolved_threads():
    root = _note(id="n-1", gh_thread_id="T1", resolved=False, path="a.py", line=1, body="x")
    state = {"notes": [root]}

    assert resolved_threads(state)["threads"] == []


def test_resolved_threads_last_comment_id_from_raw_payload():
    root = _note(id="n-1", gh_thread_id="T1", resolved=True, gh_id=100, path="a.py",
                 line=1, body="x")
    state = {"notes": [root]}
    payload = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
        {"id": "T1", "comments": {"nodes": [{"databaseId": 100}, {"databaseId": 999}]}},
    ]}}}}}

    thread = resolved_threads(state, payload)["threads"][0]

    # 999 only exists in the raw payload, not on any note -- proves the join is payload-first.
    assert thread["last_comment_id"] == 999


def test_resolved_threads_last_comment_id_falls_back_to_notes_gh_id_without_payload():
    root = _note(id="n-1", gh_thread_id="T1", resolved=True, gh_id=100, path="a.py",
                 line=1, body="x")
    reply = _note(id="n-2", reply_to="n-1", gh_thread_id="T1", resolved=True, gh_id=150,
                  created_at="t2", body="y")
    state = {"notes": [root, reply]}

    thread = resolved_threads(state)["threads"][0]

    assert thread["last_comment_id"] == 150


def test_apply_resolutions_merges_by_thread_id():
    state = {}

    apply_resolutions(state, {"T1": {"outcome": "none", "why": None}})

    assert state["resolutions"] == {"T1": {"outcome": "none", "why": None}}


def test_apply_resolutions_last_write_wins_on_same_thread_id():
    state = {"resolutions": {"T1": {"outcome": "none"}}}

    apply_resolutions(state, {"T1": {"outcome": "commits", "commits": ["abc"]}})

    assert state["resolutions"]["T1"]["outcome"] == "commits"


def test_do_resolved_threads_writes_contract_to_out_file():
    root = _note(id="n-1", gh_thread_id="T1", resolved=True, path="a.py", line=1, body="x")
    with tempfile.TemporaryDirectory() as tmp:
        state_path = _write_state(tmp, {"notes": [root]})
        out_path = Path(tmp) / "threads.json"
        with contextlib.redirect_stdout(io.StringIO()):
            do_resolved_threads(_Args(state=state_path, payload=None, out=str(out_path)))
        result = json.loads(out_path.read_text())
    assert result["threads"][0]["thread_id"] == "T1"


def test_do_apply_resolutions_reads_stdin_and_saves_state():
    with tempfile.TemporaryDirectory() as tmp:
        state_path = _write_state(tmp, {"notes": []})
        real_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps({"T1": {"outcome": "none"}}))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                do_apply_resolutions(_Args(state=state_path))
        finally:
            sys.stdin = real_stdin
        result = json.loads(Path(state_path).read_text())
    assert result["resolutions"] == {"T1": {"outcome": "none"}}


def test_payloads_emits_for_a_plain_draft_with_no_publish_requested_flag():
    # publish_requested is no longer a gate: any non-stale local draft is ready to post.
    draft = _note(id="n-1", state="draft", path="a.py", line=5, side="RIGHT", body="fix this")
    assert "publish_requested" not in draft
    state = {"notes": [draft]}
    pairs = dict(payloads_for(state, "abc123"))
    assert pairs["n-1"] == {"body": "fix this", "commit_id": "abc123", "path": "a.py",
                             "line": 5, "side": "RIGHT"}


def test_payloads_emits_exact_payload_for_a_draft():
    draft = _note(id="n-1", state="draft", path="a.py", line=5, side="RIGHT", body="fix this")
    state = {"notes": [draft]}
    pairs = dict(payloads_for(state, "sha123"))
    assert pairs["n-1"] == {"body": "fix this", "commit_id": "sha123", "path": "a.py",
                             "line": 5, "side": "RIGHT"}


def test_payloads_emits_nothing_for_a_stale_draft():
    stale = _note(id="n-stale", state="draft", path="a.py", line=5, side="RIGHT",
                  body="fix this", stale=True)
    state = {"notes": [stale]}
    assert payloads_for(state, "abc123") == []


def test_payloads_still_emits_a_non_stale_draft_alongside_a_stale_one():
    stale = _note(id="n-stale", state="draft", path="a.py", line=5, side="RIGHT",
                  body="stale one", stale=True)
    fresh = _note(id="n-fresh", state="draft", path="a.py", line=8, side="RIGHT",
                  body="fresh one", stale=False)
    state = {"notes": [stale, fresh]}
    pairs = dict(payloads_for(state, "abc123"))
    assert "n-stale" not in pairs
    assert pairs["n-fresh"] == {"body": "fresh one", "commit_id": "abc123", "path": "a.py",
                                 "line": 8, "side": "RIGHT"}


def test_payloads_still_emits_a_stale_reply_since_it_posts_by_in_reply_to_not_line():
    parent = _note(id="n-parent", state="posted", gh_id=999, path="a.py", line=5, side="RIGHT")
    stale_reply = _note(id="n-reply", state="draft", reply_to="n-parent", body="agreed",
                        path="a.py", line=5, side="RIGHT", stale=True)
    state = {"notes": [parent, stale_reply]}
    pairs = dict(payloads_for(state, "abc123"))
    assert pairs["n-reply"] == {"body": "agreed", "in_reply_to": 999}


def test_reply_still_needs_a_posted_parent():
    parent_draft = _note(id="n-parent", state="draft")  # no gh_id yet
    reply = _note(id="n-reply", state="draft", reply_to="n-parent", body="agreed")
    state = {"notes": [parent_draft, reply]}
    pairs = dict(payloads_for(state, "abc123"))
    assert "n-reply" not in pairs


def test_posted_note_is_always_skipped():
    note = _note(id="n-1", state="posted")
    state = {"notes": [note]}
    assert payloads_for(state, "abc123") == []


def test_payloads_emits_side_verbatim_for_left_and_right():
    left = _note(id="n-left", path="a.py", line=5, side="LEFT", body="left note")
    right = _note(id="n-right", path="a.py", line=8, side="RIGHT", body="right note")
    state = {"notes": [left, right]}

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "payloads"
        with contextlib.redirect_stdout(io.StringIO()):
            do_payloads(_Args(state=_write_state(tmp, state), commit_id="abc123", out=str(out_dir)))
        left_payload = json.loads((out_dir / "n-left.json").read_text())
        right_payload = json.loads((out_dir / "n-right.json").read_text())

    assert left_payload == {"body": "left note", "commit_id": "abc123", "path": "a.py",
                             "line": 5, "side": "LEFT"}
    assert right_payload == {"body": "right note", "commit_id": "abc123", "path": "a.py",
                              "line": 8, "side": "RIGHT"}


def test_payloads_emits_body_and_in_reply_to_only_for_a_reply():
    parent = _note(id="n-parent", state="posted", gh_id=999, path="a.py", line=5, side="RIGHT")
    reply = _note(id="n-reply", state="draft", reply_to="n-parent", body="agreed",
                  path="a.py", line=5, side="RIGHT")
    state = {"notes": [parent, reply]}

    pairs = dict(payloads_for(state, "abc123"))
    assert pairs["n-reply"] == {"body": "agreed", "in_reply_to": 999}
    assert "n-parent" not in pairs  # not a draft, no payload


def test_pending_publish_ids_agrees_with_payloads_for_over_a_mixed_state():
    plain_draft = _note(id="n-draft", state="draft", path="a.py", line=5, side="RIGHT",
                        body="fix this")
    posted = _note(id="n-posted", state="posted", gh_id=1)
    stale_top = _note(id="n-stale-top", state="draft", path="a.py", line=7, side="RIGHT",
                      body="stale top", stale=True)
    parent = _note(id="n-parent", state="posted", gh_id=999, path="a.py", line=8, side="RIGHT")
    stale_reply = _note(id="n-stale-reply", state="draft", reply_to="n-parent", body="agreed",
                        path="a.py", line=8, side="RIGHT", stale=True)
    state = {"notes": [plain_draft, posted, stale_top, parent, stale_reply]}

    expected = {note_id for note_id, _ in payloads_for(state, "sha")}
    assert set(pending_publish_ids(state)) == expected
    assert expected == {"n-draft", "n-stale-reply"}


# ---------------------------------------------------------------------------
# Phase 2: anchor resolution
# ---------------------------------------------------------------------------

def test_postable_ranges_splits_left_removed_only_from_right_added_plus_context():
    diff = _hunk_diff("a.py", "@@ -5,3 +5,2 @@", [" context", "-removed", " more"])
    ranges = postable_ranges(diff)
    assert ranges[("a.py", "LEFT")] == [(6, 6)]
    assert ranges[("a.py", "RIGHT")] == [(5, 6)]


def test_resolve_table_driven_anchor_cases():
    diff = _hunk_diff("src/app.py", "@@ -1,3 +10,4 @@",
                       [" context", "+added1", "+added2", " context2"])
    diff += "diff --git a/other.py b/other.py\nindex aaa1111..bbb2222 100644\n"
    ranges = postable_ranges(diff)

    # (path, line, side) -> (expected resolved line, expected anchor_status, note expected?)
    cases = [
        (("src/app.py", 11, "RIGHT"), (11, "anchored", False)),   # inside the hunk
        (("src/app.py", 16, "RIGHT"), (13, "shifted", True)),     # 3 lines past the hunk
        (("src/app.py", 23, "RIGHT"), (13, "shifted", True)),     # exactly NEAREST_LINE_LIMIT (10) past
        (("src/app.py", 53, "RIGHT"), (None, "file_level", True)),  # 40 lines past the hunk
        (("other.py", 5, "RIGHT"), (None, "file_level", True)),   # no hunks in this file at all
    ]
    for (path, line, side), (expect_line, expect_status, expect_note) in cases:
        resolved_line, resolved_side, status, note = resolve(path, line, side, ranges)
        assert resolved_line == expect_line, (path, line)
        assert resolved_side == side
        assert status == expect_status, (path, line)
        assert bool(note) == expect_note, (path, line)
        if expect_note:
            assert str(line) in note


def test_promote_moves_one_note_and_leaves_the_other_untouched():
    a = _note(id="n-a", state="draft")
    b = _note(id="n-b", state="draft", body="untouched")
    state = {"notes": [a, b]}

    with tempfile.TemporaryDirectory() as tmp:
        state_path = _write_state(tmp, state)
        with contextlib.redirect_stdout(io.StringIO()):
            do_promote(_Args(state=state_path, id="n-a", gh_id="42", gh_url="https://x/42"))
        result = json.loads(Path(state_path).read_text())

    by_id = {n["id"]: n for n in result["notes"]}
    assert by_id["n-a"]["state"] == "posted"
    assert by_id["n-a"]["gh_id"] == 42
    assert by_id["n-a"]["gh_url"] == "https://x/42"
    assert by_id["n-b"] == b  # completely untouched


def test_promote_pure_function_reports_failure_for_an_unknown_id():
    notes = [_note(id="n-a")]
    assert promote_note(notes, "does-not-exist", 1, "url") is False


# ---------------------------------------------------------------------------
# Phase 5: reanchor
# ---------------------------------------------------------------------------

def test_reanchor_rebinds_when_the_anchor_moved_within_the_window():
    diff = _hunk_diff("src/app.py", "@@ -1,3 +16,4 @@",
                       [" context line", "+    def hello():", " more context"])
    note = _note(anchor_text="+    def hello():", anchor_line=11, line=11, side="RIGHT")
    anchors = diff_anchors(diff)
    reanchor_note(note, anchors)

    assert note["line"] == 17  # moved 6 lines down from 11
    assert note["anchor_line"] == 17
    assert note["stale"] is False


def test_reanchor_goes_stale_when_the_anchor_moved_outside_the_window():
    diff = _hunk_diff("src/app.py", "@@ -1,3 +16,4 @@",
                       [" context line", "+    def hello():", " more context"])
    note = _note(anchor_text="+    def hello():", anchor_line=77, line=77, side="RIGHT")
    anchors = diff_anchors(diff)
    reanchor_note(note, anchors)

    assert note["stale"] is True
    assert note["line"] == 77  # unchanged, never guessed
    assert note["anchor_line"] == 77


def test_reanchor_refuses_to_rebind_a_trivial_anchor():
    diff = _hunk_diff("src/app.py", "@@ -1,2 +1,2 @@", [" x", "-}"])
    note = _note(anchor_text="-}", anchor_line=2, line=2, side="LEFT")
    anchors = diff_anchors(diff)
    reanchor_note(note, anchors)

    assert note["stale"] is True
    assert is_trivial_anchor("-}") is True
    assert is_trivial_anchor("+    if host not in allowed:") is False


def test_reanchor_left_note_never_binds_to_an_added_line():
    # The exact same code (minus the diff marker) exists only as an added line, never as a
    # removed one -- a LEFT note must not be fooled into binding to it.
    diff = _hunk_diff("src/app.py", "@@ -8,2 +8,3 @@", [" context", "+    old_value = 5", " more"])
    note = _note(anchor_text="-    old_value = 5", anchor_line=9, line=9, side="LEFT")
    anchors = diff_anchors(diff)

    assert anchors["src/app.py"]["LEFT"] == []  # no removed lines at all in this hunk
    reanchor_note(note, anchors)

    assert note["stale"] is True
    assert note["line"] == 9


def test_reanchor_keeps_side_gh_id_and_body_on_a_successful_rebind():
    diff = _hunk_diff("src/app.py", "@@ -1,3 +16,4 @@",
                       [" context line", "+    def hello():", " more context"])
    note = _note(anchor_text="+    def hello():", anchor_line=11, line=11, side="RIGHT",
                 gh_id=123, gh_url="https://x/123", body="original text")
    anchors = diff_anchors(diff)
    reanchor_note(note, anchors)

    assert note["side"] == "RIGHT"
    assert note["gh_id"] == 123
    assert note["gh_url"] == "https://x/123"
    assert note["body"] == "original text"
    assert note["line"] == 17


def test_reanchor_leaves_a_synced_github_note_alone():
    # A synced GitHub comment has no anchor_text -- reanchor has nothing to rebind on it.
    note = _note(origin="github", anchor_text=None, line=5, anchor_line=5, side="RIGHT",
                 stale=False)
    reanchor_note(note, {})
    assert note["line"] == 5
    assert note["stale"] is False


def test_do_reanchor_round_trips_through_files():
    diff = _hunk_diff("src/app.py", "@@ -1,3 +16,4 @@",
                       [" context line", "+    def hello():", " more context"])
    note = _note(anchor_text="+    def hello():", anchor_line=11, line=11, side="RIGHT")
    state = {"notes": [note]}

    with tempfile.TemporaryDirectory() as tmp:
        state_path = _write_state(tmp, state)
        diff_path = Path(tmp) / "raw.diff"
        diff_path.write_text(diff)
        with contextlib.redirect_stdout(io.StringIO()):
            do_reanchor(_Args(state=state_path, diff=str(diff_path)))
        result = json.loads(Path(state_path).read_text())

    assert result["notes"][0]["line"] == 17


def test_read_paginated_json_flattens_concatenated_pages():
    text = json.dumps([{"id": 1}]) + json.dumps([{"id": 2}, {"id": 3}])
    comments = read_paginated_json(io.StringIO(text))
    assert [c["id"] for c in comments] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Phase 1: GraphQL delivery
# ---------------------------------------------------------------------------

# A fake `gh` on PATH, dispatching on the named GraphQL operation in the query text (never
# on the variables -- those never appear on argv, so a name is the only thing left to switch
# on for dispatch). The log records argv and the parsed variables dict together, so a test can
# still assert nothing lands on argv while also checking what was actually sent -- dispatching
# on the query alone proved nothing about a wrong path/line/side/body reaching the mutation.
# State persists to GH_FAKE_STATE across invocations, since each call is a fresh subprocess:
# that's what lets one test assert a pending review was opened exactly once across three
# separate `deliver` calls.
FAKE_GH_SCRIPT = '''#!/usr/bin/env python3
import json, os, sys

stdin_data = sys.stdin.read()
payload = json.loads(stdin_data) if stdin_data else {}
query = payload.get("query", "")
variables = payload.get("variables", {})

with open(os.environ["GH_FAKE_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "variables": variables}) + "\\n")

state_path = os.environ["GH_FAKE_STATE"]
try:
    state = json.loads(open(state_path).read())
except (OSError, ValueError):
    state = {"review_id": None, "opens": 0, "threads": 0, "comments": 0}


def respond(data):
    print(json.dumps({"data": data}))


if "query PendingReview" in query:
    nodes = [{"id": state["review_id"]}] if state["review_id"] else []
    respond({"repository": {"pullRequest": {"id": "PR_node", "reviews": {"nodes": nodes}}}})
elif "mutation OpenReview" in query:
    state["opens"] += 1
    state["review_id"] = "REVIEW_node"
    respond({"addPullRequestReview": {"pullRequestReview": {"id": state["review_id"]}}})
elif "mutation NewThread" in query:
    state["threads"] += 1
    tid, cid = f"THREAD_{state['threads']}", f"COMMENT_{state['threads']}"
    respond({"addPullRequestReviewThread": {
        "thread": {"id": tid, "comments": {"nodes": [{"id": cid,
                                                        "databaseId": 1000 + state["threads"]}]}}
    }})
elif "mutation ReplyThread" in query:
    state["comments"] += 1
    cid = f"REPLY_{state['comments']}"
    respond({"addPullRequestReviewComment": {
        "comment": {"id": cid, "databaseId": 2000 + state["comments"]}
    }})
elif "mutation SubmitReview" in query:
    respond({"submitPullRequestReview": {"pullRequestReview": {"id": state.get("review_id"),
                                                                "state": "COMMENTED"}}})
else:
    respond({})

with open(state_path, "w") as f:
    json.dump(state, f)
'''


@contextlib.contextmanager
def _fake_gh_on_path(tmp_path):
    gh_path = tmp_path / "gh"
    gh_path.write_text(FAKE_GH_SCRIPT)
    gh_path.chmod(0o755)
    log_path, gh_state_path = tmp_path / "gh.log", tmp_path / "gh_state.json"
    saved_env = {k: os.environ.get(k) for k in ("PATH", "GH_FAKE_LOG", "GH_FAKE_STATE")}
    os.environ["PATH"] = f"{tmp_path}{os.pathsep}{saved_env['PATH'] or ''}"
    os.environ["GH_FAKE_LOG"] = str(log_path)
    os.environ["GH_FAKE_STATE"] = str(gh_state_path)
    try:
        yield log_path, gh_state_path
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_deliver_opens_one_pending_review_across_three_calls_and_a_reply_uses_the_comment_mutation():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        diff = _hunk_diff("a.py", "@@ -1,2 +1,3 @@", [" context", "+added", " more"])
        (tmp_path / "raw.diff").write_text(diff)

        note1 = _note(id="n-1", path="a.py", line=1, side="RIGHT", body="first")
        note2 = _note(id="n-2", path="a.py", line=1, side="RIGHT", body="second")
        reply = _note(id="n-3", path="a.py", line=1, side="RIGHT", body="a reply",
                     reply_to="n-1")
        state_path = _write_state(tmp, {
            "meta": {"repo": "o/r", "pr": 42}, "notes": [note1, note2, reply], "requests": [],
        })

        with _fake_gh_on_path(tmp_path) as (log_path, gh_state_path):
            with contextlib.redirect_stdout(io.StringIO()):
                for note_id in ("n-1", "n-2", "n-3"):
                    assert do_deliver(_Args(state=state_path, id=note_id, diff=None)) == 0
            gh_state = json.loads(gh_state_path.read_text())
            calls = [json.loads(line) for line in log_path.read_text().splitlines()]
        result = json.loads(Path(state_path).read_text())

    assert gh_state["opens"] == 1  # one pending review across all three delivers
    assert gh_state["threads"] == 2  # n-1, n-2 -- both top-level
    assert gh_state["comments"] == 1  # n-3 -- a reply, never a new thread

    for body_text in ("first", "second", "a reply"):
        for call in calls:
            assert not any(body_text in arg for arg in call["argv"]), (body_text, call)

    # The bodies never reach argv (above), but they must still reach the right mutation's
    # variables -- dispatching the fake on the query name alone wouldn't catch a swapped body.
    thread_bodies = [c["variables"]["body"] for c in calls if "subjectType" in c["variables"]]
    assert thread_bodies == ["first", "second"]
    reply_variables = next(c["variables"] for c in calls if "inReplyTo" in c["variables"])
    assert reply_variables == {"reviewId": "REVIEW_node", "inReplyTo": "COMMENT_1",
                                "body": "a reply"}

    by_id = {n["id"]: n for n in result["notes"]}
    assert by_id["n-1"]["state"] == by_id["n-2"]["state"] == by_id["n-3"]["state"] == "posted"
    assert by_id["n-3"]["gh_node_id"] == "REPLY_1"


def test_deliver_refuses_a_reply_whose_parent_has_not_been_delivered_yet():
    parent = _note(id="n-1", body="first")  # no gh_node_id: never delivered
    reply = _note(id="n-2", body="a reply", reply_to="n-1")
    state = {"meta": {"repo": "o/r", "pr": 1}, "notes": [parent, reply]}
    try:
        deliver_note(state, reply, "", gh_run=lambda *_: (_ for _ in ()).throw(AssertionError))
    except RuntimeError as exc:
        assert "n-1" in str(exc)
    else:
        raise AssertionError("expected a RuntimeError")


def _pending_review_response():
    return {"data": {"repository": {"pullRequest": {
        "id": "PR_node", "reviews": {"nodes": [{"id": "REVIEW_node"}]}}}}}


def test_deliver_reply_falls_back_to_a_new_thread_when_the_comment_field_is_outright_null():
    # GraphQL null propagation: the parent's thread was deleted from the web UI mid-session,
    # so addPullRequestReviewComment itself comes back null, not {"comment": null}. The old
    # `.get("addPullRequestReviewComment", {})` default never applies to a present-but-None
    # key, so this used to crash with AttributeError instead of opening a fresh thread.
    parent = _note(id="n-1", body="first", path="a.py", line=1, side="RIGHT",
                   gh_node_id="PARENT_node")
    reply = _note(id="n-2", body="a reply", path="a.py", line=1, side="RIGHT", reply_to="n-1")
    state = {"meta": {"repo": "o/r", "pr": 1}, "notes": [parent, reply]}
    diff = _hunk_diff("a.py", "@@ -1,2 +1,3 @@", [" context", "+added", " more"])

    def gh_run(query, _variables):
        if "PendingReview" in query:
            return _pending_review_response()
        if "ReplyThread" in query:
            return {"data": {"addPullRequestReviewComment": None}}
        if "NewThread" in query:
            return {"data": {"addPullRequestReviewThread": {
                "thread": {"id": "THREAD_1",
                           "comments": {"nodes": [{"id": "COMMENT_1", "databaseId": 1}]}}}}}
        raise AssertionError(query)

    deliver_note(state, reply, diff, gh_run)
    assert reply["state"] == "posted"
    assert reply["gh_thread_id"] == "THREAD_1"


def test_deliver_raises_cleanly_when_the_new_thread_field_is_outright_null():
    # Same null-propagation shape as the reply case above, but there is no thread to fall back
    # to here -- GitHub rejected the anchor outright, so the existing "thread is None" handling
    # should raise its RuntimeError, not crash with AttributeError first.
    note = _note(id="n-1", body="first", path="a.py", line=1, side="RIGHT")
    state = {"meta": {"repo": "o/r", "pr": 1}, "notes": [note]}
    diff = _hunk_diff("a.py", "@@ -1,2 +1,3 @@", [" context", "+added", " more"])

    def gh_run(query, _variables):
        if "PendingReview" in query:
            return _pending_review_response()
        if "NewThread" in query:
            return {"data": {"addPullRequestReviewThread": None}}
        raise AssertionError(query)

    try:
        deliver_note(state, note, diff, gh_run)
    except RuntimeError as exc:
        assert "n-1" in str(exc)
    else:
        raise AssertionError("expected a RuntimeError")


def test_submit_review_returns_none_when_the_submit_field_is_outright_null():
    # Same null-propagation shape as deliver_note's two sites: submitPullRequestReview can come
    # back outright null rather than {"pullRequestReview": null}, which used to crash the old
    # `.get("submitPullRequestReview", {})` default with AttributeError.
    state = {"meta": {"repo": "o/r", "pr": 1}}

    def gh_run(query, _variables):
        if "PendingReview" in query:
            return _pending_review_response()
        if "SubmitReview" in query:
            return {"data": {"submitPullRequestReview": None}}
        raise AssertionError(query)

    assert submit_review(state, "COMMENT", "lgtm", gh_run) is None


def test_do_submit_raises_instead_of_reporting_success_when_the_submit_is_rejected():
    # Regression: do_submit used to discard submit_review's return value and always print
    # "submitted", even on the null-review case proven reachable above.
    real_gh_graphql = notes._gh_graphql

    def fake_gh_graphql(query, _variables):
        if "PendingReview" in query:
            return _pending_review_response()
        if "SubmitReview" in query:
            return {"data": {"submitPullRequestReview": None}}
        raise AssertionError(query)

    notes._gh_graphql = fake_gh_graphql
    try:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = _write_state(tmp, {"meta": {"repo": "o/r", "pr": 1}, "notes": []})
            body_path = Path(tmp) / "body.txt"
            body_path.write_text("lgtm")
            try:
                do_submit(_Args(state=state_path, event="COMMENT", body_file=str(body_path)))
            except RuntimeError:
                pass
            else:
                raise AssertionError("expected a RuntimeError")
    finally:
        notes._gh_graphql = real_gh_graphql


def test_deliver_sends_the_relocated_line_to_the_mutation_when_the_anchor_has_shifted():
    # notes.py:592's variables, not just its argv, are what deliver actually sends -- this
    # checks the resolved (shifted) line reaches the mutation, not the note's stale original.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        diff = _hunk_diff("a.py", "@@ -1,2 +1,3 @@", [" context", "+added", " more"])
        (tmp_path / "raw.diff").write_text(diff)

        # RIGHT range for a.py is (1, 3); line 13 is exactly NEAREST_LINE_LIMIT (10) past it,
        # so it should resolve (shift) to line 3, never post as line 13.
        note = _note(id="n-1", path="a.py", line=13, side="RIGHT", body="hello")
        state_path = _write_state(tmp, {
            "meta": {"repo": "o/r", "pr": 1}, "notes": [note], "requests": [],
        })

        with _fake_gh_on_path(tmp_path) as (log_path, _gh_state_path):
            with contextlib.redirect_stdout(io.StringIO()):
                assert do_deliver(_Args(state=state_path, id="n-1", diff=None)) == 0
            calls = [json.loads(line) for line in log_path.read_text().splitlines()]

    thread_call = next(c["variables"] for c in calls if "subjectType" in c["variables"])
    assert thread_call["line"] == 3
    assert thread_call["side"] == "RIGHT"
    assert "originally line 13" in thread_call["body"]


def test_do_deliver_skips_a_note_already_posted_and_does_not_double_post():
    # The natural response to a gh timeout is "run deliver again" -- that must not re-post a
    # note whose first attempt actually landed.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        diff = _hunk_diff("a.py", "@@ -1,2 +1,3 @@", [" context", "+added", " more"])
        (tmp_path / "raw.diff").write_text(diff)
        note = _note(id="n-1", path="a.py", line=1, side="RIGHT", body="first")
        state_path = _write_state(tmp, {
            "meta": {"repo": "o/r", "pr": 1}, "notes": [note], "requests": [],
        })

        with _fake_gh_on_path(tmp_path) as (log_path, _gh_state_path):
            with contextlib.redirect_stdout(io.StringIO()):
                assert do_deliver(_Args(state=state_path, id="n-1", diff=None)) == 0
            calls_after_first = log_path.read_text()
            with contextlib.redirect_stdout(io.StringIO()) as out:
                assert do_deliver(_Args(state=state_path, id="n-1", diff=None)) == 0
            calls_after_second = log_path.read_text()

    assert calls_after_first == calls_after_second  # no gh call at all on the retry
    assert "already posted" in out.getvalue()


# ---------------------------------------------------------------------------
# merge_state (the page-origin deny-list `import` relies on)
# ---------------------------------------------------------------------------

def test_merge_state_upserts_notes_merges_meta_replaces_lists():
    current = {
        "meta": {"a": 1, "b": 2},
        "notes": [{"id": "n1", "body": "x"}],
        "hunks": [{"id": "h1"}],
    }
    partial = {
        "meta": {"b": 3, "c": 4},
        "notes": [{"id": "n1", "body": "y"}, {"id": "n2", "body": "z"}],
    }

    merged = merge_state(current, partial)

    assert merged["meta"] == {"a": 1, "b": 3, "c": 4}
    assert [n["id"] for n in merged["notes"]] == ["n1", "n2"]
    assert merged["notes"][0]["body"] == "y"
    assert merged["hunks"] == [{"id": "h1"}]


def test_merge_state_page_created_note_gets_schema_defaults():
    current = {"notes": []}
    partial = {"notes": [{"id": "n-new", "body": "hi", "path": "a.py", "line": 5,
                          "side": "RIGHT"}]}

    merged = merge_state(current, partial, is_page_origin=True)

    note = merged["notes"][0]
    assert note["state"] == "draft"
    assert note["origin"] == "local"
    assert note["gh_id"] is None
    assert note["gh_url"] is None
    assert note["reply_to"] is None


def test_merge_state_agent_created_note_state_is_not_overridden_by_default():
    current = {"notes": []}
    partial = {"notes": [{"id": "n-new", "body": "hi", "state": "posted", "origin": "github"}]}

    merged = merge_state(current, partial)

    note = merged["notes"][0]
    assert note["state"] == "posted"
    assert note["origin"] == "github"


def test_merge_state_drops_a_note_with_an_unsafe_id_instead_of_raising():
    current = {"notes": []}
    partial = {"notes": [
        {"id": "n1; curl attacker.example", "body": "evil"},
        {"id": "n2", "body": "fine"},
    ]}

    merged = merge_state(current, partial)

    assert [n["id"] for n in merged["notes"]] == ["n2"]


def test_page_origin_write_cannot_set_gh_id_or_state():
    current = {"notes": [{"id": "n-1", "state": "draft", "origin": "local"}]}
    partial = {"notes": [{"id": "n-1", "state": "posted", "gh_id": 999}]}

    merged = merge_state(current, partial, is_page_origin=True)

    note = merged["notes"][0]
    assert note["state"] == "draft"
    assert note.get("gh_id") is None


def test_page_origin_write_cannot_touch_a_github_origin_note():
    current = {"notes": [
        {"id": "gh-1", "origin": "github", "body": "original", "state": "posted"},
    ]}
    partial = {"notes": [{"id": "gh-1", "body": "tampered"}]}

    merged = merge_state(current, partial, is_page_origin=True)

    note = merged["notes"][0]
    assert note["body"] == "original"


def test_agent_origin_write_can_set_state_and_gh_id():
    current = {"notes": [{"id": "n-1", "state": "draft", "origin": "local"}]}
    partial = {"notes": [{"id": "n-1", "state": "posted", "gh_id": 42}]}

    merged = merge_state(current, partial)  # is_page_origin=False: an agent write

    note = merged["notes"][0]
    assert note["state"] == "posted"
    assert note["gh_id"] == 42


def test_partial_note_write_preserves_stored_origin_and_gh_fields():
    current = {"notes": [
        {"id": "n-1", "body": "first", "origin": "local", "gh_id": 7,
         "gh_url": "https://x", "state": "posted"},
    ]}
    partial = {"notes": [{"id": "n-1", "body": "edited"}]}

    merged = merge_state(current, partial, is_page_origin=True)

    note = merged["notes"][0]
    assert note["body"] == "edited"
    assert note["origin"] == "local"
    assert note["gh_id"] == 7
    assert note["gh_url"] == "https://x"


def test_page_origin_write_drops_a_new_note_missing_path():
    current = {"notes": []}
    partial = {"notes": [{"id": "n-new", "body": "hi", "state": "draft"}]}

    merged = merge_state(current, partial, is_page_origin=True)

    assert merged["notes"] == []


def test_page_origin_write_drops_a_new_note_with_a_null_line():
    current = {"notes": []}
    partial = {"notes": [{"id": "n-new", "body": "hi", "path": "a.py", "line": None,
                          "side": "RIGHT"}]}

    merged = merge_state(current, partial, is_page_origin=True)

    assert merged["notes"] == []


def test_page_origin_write_drops_a_new_note_with_a_string_line():
    current = {"notes": []}
    partial = {"notes": [{"id": "n-new", "body": "hi", "path": "a.py", "line": "5",
                          "side": "RIGHT"}]}

    merged = merge_state(current, partial, is_page_origin=True)

    assert merged["notes"] == []


def test_page_origin_partial_write_onto_an_existing_note_still_merges_body_only():
    current = {"notes": [
        {"id": "n-1", "body": "first", "path": "a.py", "line": 5, "side": "RIGHT",
         "origin": "local", "state": "draft"},
    ]}
    partial = {"notes": [{"id": "n-1", "body": "edited"}]}

    merged = merge_state(current, partial, is_page_origin=True)

    note = merged["notes"][0]
    assert note["body"] == "edited"
    assert note["path"] == "a.py" and note["line"] == 5 and note["side"] == "RIGHT"


# ---------------------------------------------------------------------------
# import (copy-for-agent payload)
# ---------------------------------------------------------------------------

def test_import_merges_new_notes_into_a_state_without_them():
    new_note = _note(id="n-new", state="draft")
    with tempfile.TemporaryDirectory() as tmp:
        state_path = _write_state(tmp, {"notes": []})
        payload_path = Path(tmp) / "payload.json"
        payload_path.write_text(json.dumps({"notes": [new_note]}))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            do_import(_Args(state=state_path, file=str(payload_path)))
        result = json.loads(Path(state_path).read_text())
    assert [n["id"] for n in result["notes"]] == ["n-new"]
    assert "1 added, 0 updated" in out.getvalue()
    assert "n-new" in out.getvalue()


def test_a_brand_new_imported_note_is_visible_to_pending_publish_ids_and_payloads_for():
    # Regression: a brand-new import used to leave state.json with no "state" key at all
    # (merge_state's deny-list withholds it, with no prior value to fall back on), invisible
    # to pending_publish_ids/payloads_for, which both require state=="draft" verbatim.
    new_note = _note(id="n-new", state="draft", path="a.py", line=5, side="RIGHT")
    del new_note["state"]
    with tempfile.TemporaryDirectory() as tmp:
        state_path = _write_state(tmp, {"notes": []})
        payload_path = Path(tmp) / "payload.json"
        payload_path.write_text(json.dumps({"notes": [new_note]}))
        with contextlib.redirect_stdout(io.StringIO()):
            do_import(_Args(state=state_path, file=str(payload_path)))
        result = json.loads(Path(state_path).read_text())

    assert result["notes"][0]["state"] == "draft"
    assert pending_publish_ids(result) == ["n-new"]
    assert dict(payloads_for(result, "sha"))["n-new"] == {
        "body": new_note["body"], "commit_id": "sha", "path": "a.py", "line": 5, "side": "RIGHT",
    }


def test_page_created_reply_survives_import_with_threading_intact():
    # Regression: reply_to is in PAGE_DENIED_NOTE_FIELDS, so do_import's is_page_origin=True
    # merge strips it from a pasted page payload; only in_reply_to (the field the page's
    # wireReplyBox sets alongside it) used to survive, and payloads_for read only reply_to --
    # so the reply came out as a brand-new top-level comment instead of a reply.
    parent = _note(id="n-parent", state="posted", gh_id=999, path="a.py", line=5, side="RIGHT")
    reply = _note(id="n-reply", state="draft", path="a.py", line=5, side="RIGHT", body="agreed")
    reply["reply_to"] = "n-parent"
    reply["in_reply_to"] = "n-parent"
    with tempfile.TemporaryDirectory() as tmp:
        state_path = _write_state(tmp, {"notes": [parent]})
        payload_path = Path(tmp) / "payload.json"
        payload_path.write_text(json.dumps({"notes": [reply]}))
        with contextlib.redirect_stdout(io.StringIO()):
            do_import(_Args(state=state_path, file=str(payload_path)))
        result = json.loads(Path(state_path).read_text())

    imported = next(n for n in result["notes"] if n["id"] == "n-reply")
    assert imported.get("reply_to") is None  # stripped by the deny-list, as intended
    assert imported.get("in_reply_to") == "n-parent"  # the fallback payloads_for now shares
    assert dict(payloads_for(result, "sha"))["n-reply"] == {"body": "agreed", "in_reply_to": 999}
    assert pending_publish_ids(result) == ["n-reply"]


def test_import_of_a_posted_note_does_not_flip_it_back_to_draft():
    # Duplicate-thread guard: a stale pasted draft must never flip an already-posted note
    # back to draft or erase its gh_id/gh_url -- see the import section of notes.py's
    # docstring for the full mechanism.
    posted = _note(id="n-1", state="posted", origin="local", gh_id=999, gh_url="https://x/1")
    stale_draft = _note(id="n-1", state="draft")
    with tempfile.TemporaryDirectory() as tmp:
        state_path = _write_state(tmp, {"notes": [posted]})
        payload_path = Path(tmp) / "payload.json"
        payload_path.write_text(json.dumps({"notes": [stale_draft]}))
        with contextlib.redirect_stdout(io.StringIO()):
            do_import(_Args(state=state_path, file=str(payload_path)))
        result = json.loads(Path(state_path).read_text())
    note = result["notes"][0]
    assert note["state"] == "posted"
    assert note["gh_id"] == 999
    assert note["gh_url"] == "https://x/1"


def test_import_of_a_malformed_page_note_does_not_brick_a_later_import():
    # Regression: a pasted note missing path/line/side used to be merged and written to
    # state.json, then pending_publish_ids (which do_import calls on every import, not just
    # this one) raised KeyError reading note["path"] on every import from then on.
    with tempfile.TemporaryDirectory() as tmp:
        state_path = _write_state(tmp, {"notes": []})
        bad_payload_path = Path(tmp) / "bad.json"
        bad_payload_path.write_text(json.dumps(
            {"notes": [{"id": "n-bad", "body": "hi", "state": "draft"}]}
        ))
        with contextlib.redirect_stdout(io.StringIO()):
            do_import(_Args(state=state_path, file=str(bad_payload_path)))

        good_note = _note(id="n-good", state="draft", path="a.py", line=5, side="RIGHT")
        good_payload_path = Path(tmp) / "good.json"
        good_payload_path.write_text(json.dumps({"notes": [good_note]}))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            do_import(_Args(state=state_path, file=str(good_payload_path)))
        result = json.loads(Path(state_path).read_text())

    assert [n["id"] for n in result["notes"]] == ["n-good"]
    assert "ready to post: n-good" in out.getvalue()


def test_import_reads_from_stdin():
    new_note = _note(id="n-stdin", state="draft")
    payload = json.dumps({"notes": [new_note]})
    with tempfile.TemporaryDirectory() as tmp:
        state_path = _write_state(tmp, {"notes": []})
        real_stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                do_import(_Args(state=state_path, file=None))
        finally:
            sys.stdin = real_stdin
        result = json.loads(Path(state_path).read_text())
    assert [n["id"] for n in result["notes"]] == ["n-stdin"]


def test_import_rejects_a_note_id_failing_valid_id_re():
    bad_note = _note(id="not a safe id!", state="draft")
    with tempfile.TemporaryDirectory() as tmp:
        state_path = _write_state(tmp, {"notes": []})
        payload_path = Path(tmp) / "payload.json"
        payload_path.write_text(json.dumps({"notes": [bad_note]}))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            do_import(_Args(state=state_path, file=str(payload_path)))
        result = json.loads(Path(state_path).read_text())
    assert result["notes"] == []
    assert "0 added, 0 updated" in out.getvalue()


def _write_state(tmp_dir, state):
    path = Path(tmp_dir) / "state.json"
    path.write_text(json.dumps(state))
    return str(path)


if __name__ == "__main__":
    tests = [
        test_reply_attaches_to_parent_even_when_it_appears_first,
        test_outdated_comment_lands_on_original_line_and_is_stale,
        test_sync_twice_is_idempotent,
        test_sync_via_stdin_handles_paginated_concatenated_arrays,
        test_dedupe_promotes_a_matching_draft_without_reposting,
        test_dedupe_matches_a_local_draft_despite_its_empty_author,
        test_sync_stores_diff_hunk_verbatim_on_a_new_note,
        test_second_sync_of_same_comment_leaves_diff_hunk_unchanged,
        test_sync_stores_original_commit_id_on_a_new_note,
        test_sync_stores_none_original_commit_id_when_absent,
        test_second_sync_of_same_comment_leaves_original_commit_id_unchanged,
        test_local_draft_with_no_diff_hunk_round_trips_unharmed,
        test_sync_threads_sets_thread_id_and_resolved_on_the_matching_note,
        test_sync_threads_maps_one_thread_onto_several_comments,
        test_sync_threads_stores_resolved_by_login_on_every_note_in_the_thread,
        test_sync_threads_resolved_by_is_none_when_the_field_is_absent,
        test_sync_threads_unresolve_clears_resolved_and_resolved_by,
        test_sync_threads_idempotent_with_resolved_by,
        test_sync_threads_leaves_a_comment_with_no_matching_note_alone,
        test_sync_threads_is_idempotent,
        test_do_sync_threads_reads_the_graphql_response_shape_from_stdin,
        test_do_sync_threads_warns_on_stderr_when_either_connection_is_truncated,
        test_resolved_threads_groups_several_notes_into_one_thread,
        test_resolved_threads_finds_root_even_when_it_is_not_first,
        test_resolved_threads_excludes_unresolved_threads,
        test_resolved_threads_last_comment_id_from_raw_payload,
        test_resolved_threads_last_comment_id_falls_back_to_notes_gh_id_without_payload,
        test_apply_resolutions_merges_by_thread_id,
        test_apply_resolutions_last_write_wins_on_same_thread_id,
        test_do_resolved_threads_writes_contract_to_out_file,
        test_do_apply_resolutions_reads_stdin_and_saves_state,
        test_payloads_emits_for_a_plain_draft_with_no_publish_requested_flag,
        test_payloads_emits_exact_payload_for_a_draft,
        test_payloads_emits_nothing_for_a_stale_draft,
        test_payloads_still_emits_a_non_stale_draft_alongside_a_stale_one,
        test_payloads_still_emits_a_stale_reply_since_it_posts_by_in_reply_to_not_line,
        test_reply_still_needs_a_posted_parent,
        test_posted_note_is_always_skipped,
        test_payloads_emits_side_verbatim_for_left_and_right,
        test_payloads_emits_body_and_in_reply_to_only_for_a_reply,
        test_pending_publish_ids_agrees_with_payloads_for_over_a_mixed_state,
        test_promote_moves_one_note_and_leaves_the_other_untouched,
        test_promote_pure_function_reports_failure_for_an_unknown_id,
        test_reanchor_rebinds_when_the_anchor_moved_within_the_window,
        test_reanchor_goes_stale_when_the_anchor_moved_outside_the_window,
        test_reanchor_refuses_to_rebind_a_trivial_anchor,
        test_reanchor_left_note_never_binds_to_an_added_line,
        test_reanchor_keeps_side_gh_id_and_body_on_a_successful_rebind,
        test_reanchor_leaves_a_synced_github_note_alone,
        test_do_reanchor_round_trips_through_files,
        test_read_paginated_json_flattens_concatenated_pages,
        test_postable_ranges_splits_left_removed_only_from_right_added_plus_context,
        test_resolve_table_driven_anchor_cases,
        test_deliver_opens_one_pending_review_across_three_calls_and_a_reply_uses_the_comment_mutation,
        test_deliver_refuses_a_reply_whose_parent_has_not_been_delivered_yet,
        test_deliver_reply_falls_back_to_a_new_thread_when_the_comment_field_is_outright_null,
        test_deliver_raises_cleanly_when_the_new_thread_field_is_outright_null,
        test_submit_review_returns_none_when_the_submit_field_is_outright_null,
        test_do_submit_raises_instead_of_reporting_success_when_the_submit_is_rejected,
        test_deliver_sends_the_relocated_line_to_the_mutation_when_the_anchor_has_shifted,
        test_do_deliver_skips_a_note_already_posted_and_does_not_double_post,
        test_merge_state_upserts_notes_merges_meta_replaces_lists,
        test_merge_state_page_created_note_gets_schema_defaults,
        test_merge_state_agent_created_note_state_is_not_overridden_by_default,
        test_merge_state_drops_a_note_with_an_unsafe_id_instead_of_raising,
        test_page_origin_write_cannot_set_gh_id_or_state,
        test_page_origin_write_cannot_touch_a_github_origin_note,
        test_agent_origin_write_can_set_state_and_gh_id,
        test_partial_note_write_preserves_stored_origin_and_gh_fields,
        test_page_origin_write_drops_a_new_note_missing_path,
        test_page_origin_write_drops_a_new_note_with_a_null_line,
        test_page_origin_write_drops_a_new_note_with_a_string_line,
        test_page_origin_partial_write_onto_an_existing_note_still_merges_body_only,
        test_import_merges_new_notes_into_a_state_without_them,
        test_a_brand_new_imported_note_is_visible_to_pending_publish_ids_and_payloads_for,
        test_import_of_a_posted_note_does_not_flip_it_back_to_draft,
        test_import_of_a_malformed_page_note_does_not_brick_a_later_import,
        test_import_reads_from_stdin,
        test_import_rejects_a_note_id_failing_valid_id_re,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
