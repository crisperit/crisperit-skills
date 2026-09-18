#!/usr/bin/env python3
"""Draft -> posted lifecycle for state.json's notes[], targeted at a GitHub PR.

  python3 notes.py sync --state state.json  (< gh api .../pulls/<n>/comments --paginate)
  python3 notes.py sync-threads --state state.json  (< gh api graphql for REVIEW_THREADS_QUERY)
  python3 notes.py import --state state.json [--file payload.json]  (< pasted JSON on stdin)
  python3 notes.py deliver --state state.json --id <local> [--diff raw.diff]
  python3 notes.py submit --state state.json --event COMMENT|APPROVE|REQUEST_CHANGES \
      --body-file <f>
  python3 notes.py reanchor --state state.json --diff raw.diff

  Superseded, kept for one release (see "GitHub delivery" below):
  python3 notes.py payloads --state state.json --commit-id <sha> --out <dir>
  python3 notes.py promote --state state.json --id <local> --gh-id <id> --gh-url <url>

notes.py never calls git. `gh` is the one subprocess it runs, and only for `deliver` and
`submit`, always `gh api graphql` with the request body on stdin (see "GitHub delivery").
Everything else here is a pure state transformer over state.json's notes[] array (schema:
docs/plans/human-review/PLAN.md, "The state document").

Lifecycle is draft -> posted. A record is never deleted and never rewritten in place by any
other step; a failed post leaves the note a draft, so nothing is lost.

sync ingests `gh api repos/<owner>/<repo>/pulls/<n>/comments --paginate` output from stdin.
--paginate concatenates one JSON array per page rather than merging them, so stdin may hold
several back-to-back JSON documents; read_paginated_json handles that without a `jq -s`
round trip. `line === null` is GitHub's outdated-comment signal (empirically confirmed, see
state.md): the comment is kept with `line = original_line` and `stale = true`.
`position`/`original_position` are never stored, they're unreliable. `diff_hunk` is stored
verbatim -- the hunk as it stood when the comment was written, for rendering the outdated
snippet a stale comment refers to. `in_reply_to_id` resolves to the parent's local id when
the parent is already in state; otherwise the raw gh id is kept on `reply_to_gh_id` and
re-tried on every sync, since `--paginate` does not guarantee parent-before-child order. A
reply always inherits its parent's `line`/`side`.

sync-threads is a second, additive sync pass over a different `gh` shape: the REST payload
`sync` reads has no review-thread node id, so resolving a conversation (a GraphQL-only
operation, `resolveReviewThread`) needs this pass to learn each thread's id before the page can
ever show a Resolve button. Run it after `sync`, from the query's own response on stdin:

  gh api graphql -f query='REVIEW_THREADS_QUERY' -F owner=<owner> -F repo=<repo> -F number=<n> | \
    python3 notes.py sync-threads --state state.json

REVIEW_THREADS_QUERY (below, next to the delivery mutations) walks
`repository.pullRequest.reviewThreads.nodes` for `id`, `isResolved`, and each thread's
`comments.nodes[].databaseId`, then sync_threads maps every thread onto the notes whose gh_id
(the REST-style integer `sync` already stored) matches one of those databaseIds, setting
`gh_thread_id` and `resolved` on each. Idempotent like `sync`: matched by gh_id, so replaying
the same response twice is a no-op the second time. `first: 100` on both connections rather than
`--paginate`, since a nested connection (comments inside each thread) has its own cursor that
--paginate's outer-cursor convention does not reach; a PR with over 100 threads or over 100
comments in one thread needs a second call, not attempted here.

import merges a page's Copy for agent payload into state.json: the page is a plain local
file with no server to post a click to, so pasting the copied JSON here is the only way a
marked note reaches state.json. It goes through merge_state with is_page_origin=True, the
same deny-list a page write is always subject to (PAGE_DENIED_NOTE_FIELDS and VALID_ID_RE): a
pasted payload is a snapshot of localStorage and can be stale, and without that deny-list a
note state.json already has as posted would flip back to draft, sending the next deliver to
open a second thread on the same line. A note brand new to state.json gets the deny-list's
fields defaulted by merge_state itself (state "draft", origin "local", etc). Its "ready to
post" list is exactly pending_publish_ids restricted to the notes this call touched, not a
second definition of ready.

GitHub delivery (deliver/submit) goes through a GraphQL pending review instead of the old
REST payloads/promote pair, because REST has no way to append a comment to an
already-created review and secondary rate limits punish one-comment-per-click. `deliver`
opens the pending review lazily on its first call -- its id is looked up fresh every time
rather than stored, so a review submitted from the web UI mid-session can never strand us on
a stale id -- then either replies into an existing thread (`addPullRequestReviewComment`,
when the note has a `reply_to`/`in_reply_to` whose parent already carries a `gh_node_id`) or
opens a new one (`addPullRequestReviewThread`, after resolving the anchor -- see
`postable_ranges`/`resolve` below). A null thread/comment back from a reply attempt means the
web UI deleted that thread mid-session, not an error: `deliver` falls back to opening a fresh
thread instead. The GitHub post and the local state update happen in the same call, so a
crash between the two can never lose the record of a comment that landed. `submit` publishes
the pending review with `submitPullRequestReview`. Every `gh` call sends its query and
variables as one JSON document on stdin via `gh api graphql --input -`: a note body is
authored by a browser page, and interpolating that text into a command line would be shell
injection with an extra step. Notes carry `gh_thread_id` and `gh_node_id` beside the existing
`gh_id` (the REST-style integer, requested via `databaseId`), since a reply addresses a
GraphQL node rather than a REST id.

postable_ranges/resolve (anchor resolution) exist because GitHub's GraphQL mutations return
200 with a null thread and no error for a line outside any hunk -- the comment is silently
discarded rather than rejected. `deliver` resolves every anchor against the diff before
sending: unchanged when the line is still in a hunk, the nearest in-hunk line on the same
side when one is within `NEAREST_LINE_LIMIT`, otherwise a file-level thread. Either moved
case appends a short note to the body saying so. Never called from the page, only from
`deliver`, since the page's own diff can be stale by the time a comment actually posts.

payloads/promote are the REST-era pair, kept working for one release: payloads emits one
JSON object per pending draft note (see pending_publish_ids below): {body, commit_id, path,
line, side} for a top-level comment, {body, in_reply_to} for a reply. `side` is copied
verbatim from the record and never re-derived here -- posting a LEFT line as RIGHT lands the
comment on unrelated code.

reanchor rebinds a note's `line` near its old position after the diff has moved, by matching
`anchor_text` (the diff line's verbatim text, captured at wire time) against the new diff.
The search never crosses sides: a LEFT note only searches removed lines, a RIGHT note only
added and context lines, and a rebind never changes `side`. No match, or a too-trivial
anchor, means `stale: true` and the old `line`/`anchor_line` are left alone -- never guessed.

Stdlib only, no network beyond `gh`.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from validate_analysis import parse_hunks  # noqa: E402  the single owner of diff parsing

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
SYNCED_ORDER = 9999
REANCHOR_WINDOW = 25
# Matches everywhere in real code, so an exact/whitespace hit on one of these proves nothing
# about *which* line moved where. Refuse to rebind rather than guess.
TRIVIAL_ANCHORS = frozenset({"}", "});", "return", ")", "],"})
MIN_ANCHOR_LEN = 8

# Phase 2: how far from its original line a comment may relocate before it gives up on a
# specific line and falls back to a file-level thread.
NEAREST_LINE_LIMIT = 10

# merge_state drops a note whose id fails this rather than merging it, since a pasted
# payload's ids are unvalidated browser input.
VALID_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# state, gh_id, gh_url, reply_to and origin are the agent's alone to set; a page write to
# merge_state withholds all five.
PAGE_DENIED_NOTE_FIELDS = frozenset({"state", "gh_id", "gh_url", "reply_to", "origin"})

GH_TIMEOUT = 30  # seconds; a hung `gh` must not wedge deliver/submit forever

# All four operations are named so a test double can dispatch on the query text alone --
# the variables never appear on argv, so a name is the only thing a fake `gh` can switch on.
PENDING_REVIEW_QUERY = """
query PendingReview($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      id
      reviews(states: PENDING, first: 1) { nodes { id } }
    }
  }
}
"""

OPEN_REVIEW_MUTATION = """
mutation OpenReview($pullRequestId: ID!) {
  addPullRequestReview(input: {pullRequestId: $pullRequestId}) {
    pullRequestReview { id }
  }
}
"""

NEW_THREAD_MUTATION = """
mutation NewThread($reviewId: ID!, $path: String!, $body: String!, $line: Int,
                    $side: DiffSide, $subjectType: PullRequestReviewThreadSubjectType) {
  addPullRequestReviewThread(input: {
    pullRequestReviewId: $reviewId, path: $path, body: $body, line: $line, side: $side,
    subjectType: $subjectType
  }) {
    thread { id comments(first: 1) { nodes { id databaseId } } }
  }
}
"""

REPLY_MUTATION = """
mutation ReplyThread($reviewId: ID!, $inReplyTo: ID!, $body: String!) {
  addPullRequestReviewComment(input: {
    pullRequestReviewId: $reviewId, inReplyTo: $inReplyTo, body: $body
  }) {
    comment { id databaseId }
  }
}
"""

SUBMIT_MUTATION = """
mutation SubmitReview($reviewId: ID!, $event: PullRequestReviewEvent!, $body: String!) {
  submitPullRequestReview(input: {pullRequestReviewId: $reviewId, event: $event, body: $body}) {
    pullRequestReview { id state }
  }
}
"""

# A read, not a delivery mutation, so it is never run through _gh_graphql/deliver's machinery --
# see sync-threads in the module docstring for why it exists and the pagination trade-off.
REVIEW_THREADS_QUERY = """
query ReviewThreads($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          id
          isResolved
          comments(first: 100) {
            pageInfo { hasNextPage }
            nodes { databaseId }
          }
        }
      }
    }
  }
}
"""


def _load_state(path):
    return json.loads(Path(path).read_text())


def _save_state(path, state):
    """Temp file in the same directory then os.replace -- a plain write_text left a window
    where a crash mid-write could truncate state.json, or leave a delivered note showing as
    draft (see do_deliver)."""
    path = Path(path)
    tmp = tempfile.NamedTemporaryFile(mode="w", dir=str(path.parent), delete=False, suffix=".tmp")
    try:
        json.dump(state, tmp, indent=2)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, path)
    except Exception:
        os.unlink(tmp.name)
        raise


def _index_by_id(notes):
    return {note["id"]: note for note in notes}


def _index_by_gh_id(notes):
    return {note["gh_id"]: note for note in notes if note.get("gh_id") is not None}


def merge_state(current, partial, is_page_origin=False):
    """Keyed merge, not whole-document replace: every field in the schema has exactly one
    writer, so two writes to different notes always commute. meta is merged key by key,
    hunks[]/files[]/groups[] replace wholesale when present and are left alone otherwise.
    notes[] is upserted by id, merged field by field onto the existing record rather than
    replacing the whole object, so a partial note write can't erase fields it didn't send.
    is_page_origin additionally withholds PAGE_DENIED_NOTE_FIELDS -- state, gh_id, gh_url,
    reply_to, origin, which only the agent may set -- and blocks touching a note whose stored
    origin is already "github" at all. A freshly created note gets those five fields' schema
    defaults (state "draft", origin "local", gh_id/gh_url/reply_to None) via setdefault. The
    one real conflict left, two edits to the same field, is last-write-wins by design. A note
    whose id fails VALID_ID_RE is dropped rather than merged, and never raises -- the id is
    unvalidated browser input.
    """
    merged = dict(current)
    if "meta" in partial:
        meta = dict(merged.get("meta", {}))
        meta.update(partial["meta"])
        merged["meta"] = meta
    for key in ("hunks", "files", "groups"):
        if key in partial:
            merged[key] = partial[key]
    if "notes" in partial:
        existing = merged.get("notes", [])
        by_id = {note["id"]: dict(note) for note in existing}
        order = [note["id"] for note in existing]
        for note in partial["notes"]:
            note_id = note.get("id")
            if not isinstance(note_id, str) or not VALID_ID_RE.fullmatch(note_id):
                continue  # not a safe id: drop the record rather than merge it
            stored = by_id.get(note_id)
            if is_page_origin and stored is not None and stored.get("origin") == "github":
                continue
            if stored is None:
                stored = {}
                order.append(note_id)
            for field, value in note.items():
                if is_page_origin and field in PAGE_DENIED_NOTE_FIELDS:
                    continue
                stored[field] = value
            # A page write can't set these fields (PAGE_DENIED_NOTE_FIELDS), so a note it
            # creates needs the defaults here. origin is "local" because a page-created note
            # is local by construction -- a github-origin note only ever enters via notes.py
            # sync, an agent write, so it is never filtered above.
            stored.setdefault("state", "draft")
            stored.setdefault("origin", "local")
            stored.setdefault("gh_id", None)
            stored.setdefault("gh_url", None)
            stored.setdefault("reply_to", None)
            by_id[note_id] = stored
        merged["notes"] = [by_id[note_id] for note_id in order]
    return merged


def read_paginated_json(stream):
    """Flatten one or more concatenated JSON documents (gh api --paginate output) into a
    single list of comment dicts. --paginate writes each page's raw array back to back, not
    a single merged array, so a plain json.loads would fail on more than one page.
    """
    text = stream.read()
    decoder = json.JSONDecoder()
    values = []
    idx, length = 0, len(text)
    while idx < length:
        while idx < length and text[idx] in " \t\r\n":
            idx += 1
        if idx >= length:
            break
        value, idx = decoder.raw_decode(text, idx)
        values.append(value)
    comments = []
    for value in values:
        comments.extend(value) if isinstance(value, list) else comments.append(value)
    return comments


def _dedupe_drafts(notes):
    """A draft that already matches a github-origin comment on the same (path, line, side,
    body) is treated as already posted rather than posted again. `author` is deliberately
    not part of the match: page-authored notes are always written with author:'', so
    comparing it would never fire.
    """
    github_notes = [note for note in notes if note.get("origin") == "github"]
    for note in notes:
        if note.get("origin") != "local" or note.get("state") != "draft":
            continue
        for gh_note in github_notes:
            if (
                gh_note.get("path") == note.get("path")
                and gh_note.get("line") == note.get("line")
                and gh_note.get("side") == note.get("side")
                and gh_note.get("body") == note.get("body")
            ):
                note["state"] = "posted"
                note["gh_id"] = gh_note.get("gh_id")
                note["gh_url"] = gh_note.get("gh_url")
                break


def sync_comments(state, comments):
    """Merge GitHub review comments into state["notes"], in place. Matched by gh_id, so
    running this twice with the same comments is a no-op the second time.
    """
    notes = state.setdefault("notes", [])
    by_gh_id = _index_by_gh_id(notes)

    for comment in comments:
        gh_id = comment["id"]
        line = comment.get("line")
        original_line = comment.get("original_line")
        stale = line is None
        if stale:
            line = original_line
        path = comment.get("path")
        side = comment.get("side")
        body = comment.get("body")
        author = (comment.get("user") or {}).get("login")
        created_at = comment.get("created_at")
        gh_url = comment.get("html_url")
        in_reply_to_id = comment.get("in_reply_to_id")
        diff_hunk = comment.get("diff_hunk")

        existing = by_gh_id.get(gh_id)
        if existing is not None:
            existing.update(
                path=path, line=line, side=side, stale=stale, body=body, author=author,
                created_at=created_at, gh_url=gh_url,
                anchor_line=line, diff_hunk=diff_hunk,
            )
            if existing.get("reply_to") is None:
                existing["reply_to_gh_id"] = in_reply_to_id
        else:
            new_note = {
                "id": f"gh-{gh_id}", "origin": "github", "target": "pr", "state": "posted",
                "path": path, "line": line, "side": side, "hunk_id": None,
                "anchor_text": None, "anchor_line": line, "stale": stale, "body": body,
                "order": SYNCED_ORDER, "author": author,
                "created_at": created_at, "gh_id": gh_id, "gh_url": gh_url, "reply_to": None,
                "reply_to_gh_id": in_reply_to_id, "diff_hunk": diff_hunk,
            }
            notes.append(new_note)
            by_gh_id[gh_id] = new_note

    # A second pass over every note (not just this batch), since --paginate gives no
    # ordering guarantee between a reply and the comment it replies to.
    for note in notes:
        if note.get("reply_to") is None and note.get("reply_to_gh_id") is not None:
            parent = by_gh_id.get(note["reply_to_gh_id"])
            if parent is not None:
                note["reply_to"] = parent["id"]
                note["reply_to_gh_id"] = None
                # Replies never anchor independently: they carry the parent's location.
                note["line"] = parent["line"]
                note["side"] = parent["side"]

    _dedupe_drafts(notes)
    return state


def sync_threads(state, threads):
    """Merge GraphQL review-thread nodes (REVIEW_THREADS_QUERY's shape) into state["notes"],
    in place: every note whose gh_id matches one of a thread's comments' databaseId gets that
    thread's id and resolved status. Matched by gh_id like sync_comments, so replaying the same
    threads is a no-op the second time. A thread with no matching note (nothing from `sync` yet)
    contributes nothing rather than raising.
    """
    notes = state.setdefault("notes", [])
    by_gh_id = _index_by_gh_id(notes)
    for thread in threads:
        thread_id = thread.get("id")
        resolved = bool(thread.get("isResolved"))
        comment_ids = [c.get("databaseId") for c in (thread.get("comments") or {}).get("nodes") or []]
        for gh_id in comment_ids:
            note = by_gh_id.get(gh_id)
            if note is not None:
                note["gh_thread_id"] = thread_id
                note["resolved"] = resolved
    return state


def payloads_for(state, commit_id):
    """Return [(note_id, payload)] for every local draft note, in notes[] order. A stale
    top-level draft is skipped: its `line` is a coordinate in the file as it was, not as it
    is, so posting it would land the comment on unrelated code. A stale reply is still
    emitted -- it posts via `in_reply_to`, not a line, so its own `stale` doesn't matter.
    """
    notes = state.get("notes", [])
    by_id = _index_by_id(notes)
    out = []
    for note in notes:
        if note.get("state") != "draft":
            continue
        reply_to = note.get("reply_to") or note.get("in_reply_to")
        if reply_to:
            parent = by_id.get(reply_to)
            if parent is None or parent.get("gh_id") is None:
                continue  # parent isn't posted yet, nothing to reply to
            out.append((note["id"], {"body": note["body"], "in_reply_to": parent["gh_id"]}))
        else:
            if note.get("stale"):
                continue
            out.append((note["id"], {
                "body": note["body"], "commit_id": commit_id, "path": note["path"],
                "line": note["line"], "side": note["side"],
            }))
    return out


def pending_publish_ids(state):
    """Ids of the notes `payloads`/`import` would emit right now. Same predicate as
    payloads_for, so the two can never disagree on what is ready to post."""
    return [note_id for note_id, _ in payloads_for(state, "")]


def promote_note(notes, note_id, gh_id, gh_url):
    """Mutate the one note matching note_id to posted; every other note is untouched."""
    for note in notes:
        if note["id"] == note_id:
            try:
                gh_id = int(gh_id)
            except (TypeError, ValueError):
                pass
            note["state"] = "posted"
            note["gh_id"] = gh_id
            note["gh_url"] = gh_url
            return True
    return False


def _code(raw_line):
    """The line's content with its leading +/-/space diff marker stripped, then trimmed."""
    return raw_line[1:].strip() if raw_line else ""


def is_trivial_anchor(anchor_text):
    code = _code(anchor_text)
    return len(code) < MIN_ANCHOR_LEN or code in TRIVIAL_ANCHORS


def _walk_hunk_lines(entry):
    """Yield (kind, old_line, new_line, raw) for every body line of every hunk in one
    parse_hunks() file entry, each line's position given before that side's counter advances.
    The one place that tracks both sides' running line numbers, shared by diff_anchors and
    postable_ranges below, which otherwise duplicated this walk line for line.
    """
    for hunk in entry["hunks"]:
        match = HUNK_HEADER.match(hunk["prefix"])
        old_line, new_line = int(match.group(1)), int(match.group(2))
        for kind, raw in hunk["lines"]:
            yield kind, old_line, new_line, raw
            if kind != "a":
                old_line += 1
            if kind != "d":
                new_line += 1


def diff_anchors(diff_text):
    """Return {path: {"LEFT": [(line, raw_text)], "RIGHT": [(line, raw_text)]}}.

    LEFT candidates are removed lines only; RIGHT candidates are added and context lines.
    Context never appears on LEFT: re-anchoring never changes a note's side, so a LEFT note
    must only ever be able to bind to a line that still exists on the old side.
    """
    _, files = parse_hunks(diff_text)
    anchors = {}
    for path, entry in files.items():
        left, right = [], []
        for kind, old_line, new_line, raw in _walk_hunk_lines(entry):
            if kind == "d":
                left.append((old_line, raw))
            else:
                right.append((new_line, raw))
        anchors[path] = {"LEFT": left, "RIGHT": right}
    return anchors


def _nearest_match(candidates, anchor_line, matches):
    best_line, best_dist = None, None
    for line, text in candidates:
        dist = abs(line - anchor_line)
        if dist > REANCHOR_WINDOW or not matches(text):
            continue
        if best_dist is None or dist < best_dist or (dist == best_dist and line < best_line):
            best_line, best_dist = line, dist
    return best_line


def reanchor_note(note, anchors):
    """Rebind one note's line near its stored anchor_line, or mark it stale. Mutates in
    place. A note with no anchor_text (a synced GitHub comment) has nothing to rebind on
    and is left alone.
    """
    anchor_text = note.get("anchor_text")
    if anchor_text is None:
        return
    if is_trivial_anchor(anchor_text):
        note["stale"] = True
        return

    candidates = anchors.get(note["path"], {}).get(note["side"], [])
    anchor_line = note["anchor_line"]

    match = _nearest_match(candidates, anchor_line, lambda text: text == anchor_text)
    if match is None:
        target = _code(anchor_text)
        match = _nearest_match(candidates, anchor_line, lambda text: _code(text) == target)

    if match is None:
        note["stale"] = True
        return
    note["line"] = match
    note["anchor_line"] = match
    note["stale"] = False


def reanchor_state(state, diff_text):
    anchors = diff_anchors(diff_text)
    for note in state.get("notes", []):
        reanchor_note(note, anchors)
    return state


def _line_ranges(lines):
    """Sorted line numbers collapsed into contiguous (start, end) runs, so a run interrupted
    by a line of the other kind (e.g. an added line splitting two removed runs) reports as
    two ranges rather than one that wrongly spans the gap."""
    if not lines:
        return []
    ordered = sorted(lines)
    runs = [[ordered[0], ordered[0]]]
    for line in ordered[1:]:
        if line == runs[-1][1] + 1:
            runs[-1][1] = line
        else:
            runs.append([line, line])
    return [tuple(run) for run in runs]


def postable_ranges(diff_text):
    """{(path, side): [(start, end), ...]} of the lines GitHub will actually attach a review
    thread to. LEFT is removed lines only, RIGHT is added and context lines -- the same split
    diff_anchors already uses above, since GitHub attributes a context line's comment to its
    new-side line number only, never its old-side one.
    """
    _, files = parse_hunks(diff_text)
    ranges = {}
    for path, entry in files.items():
        left_lines, right_lines = [], []
        for kind, old_line, new_line, _raw in _walk_hunk_lines(entry):
            if kind == "d":
                left_lines.append(old_line)
            else:
                right_lines.append(new_line)
        ranges[(path, "LEFT")] = _line_ranges(left_lines)
        ranges[(path, "RIGHT")] = _line_ranges(right_lines)
    return ranges


def _nearest_line(line, ranges):
    """(nearest line, distance) across every range, or (None, None) when there are none.
    A range's nearest point to `line` is one of its own endpoints once `line` falls outside
    it -- clamping is enough, no need to scan every line in the range.
    """
    best_line, best_dist = None, None
    for start, end in ranges:
        candidate = min(max(line, start), end)
        dist = abs(candidate - line)
        if best_dist is None or dist < best_dist or (dist == best_dist and candidate < best_line):
            best_line, best_dist = candidate, dist
    return best_line, best_dist


def resolve(path, line, side, ranges):
    """(resolved_line, resolved_side, anchor_status, note_suffix) for one note's post target.
    A line still inside a hunk is unchanged. Outside a hunk but within NEAREST_LINE_LIMIT of
    one, GitHub still 200s with a null thread and drops the comment silently, so this rebinds
    to the nearest in-hunk line on the same side instead ("shifted") and says so in the body.
    Past that window, or a file with no hunks at all, becomes a file-level thread
    ("file_level") with the same kind of note -- there is no single line left to defend.
    """
    side_ranges = ranges.get((path, side), [])
    if any(start <= line <= end for start, end in side_ranges):
        return line, side, "anchored", ""

    nearest, dist = _nearest_line(line, side_ranges)
    if nearest is not None and dist <= NEAREST_LINE_LIMIT:
        note = (f"\n\n_(anchor moved: originally line {line}, relocated to the nearest "
                f"line still in the diff, {nearest})_")
        return nearest, side, "shifted", note

    note = (f"\n\n_(anchor moved: line {line} is no longer in the diff and no line within "
            f"{NEAREST_LINE_LIMIT} lines matched; posted as a file-level comment)_")
    return None, side, "file_level", note


def _gh_graphql(query, variables):
    """Run one `gh api graphql` request, query and variables together as the entire stdin
    body -- never as a command-line argument. A note's body is authored by a browser page;
    putting it on argv would be shell injection with an extra step.
    """
    payload = json.dumps({"query": query, "variables": variables})
    try:
        result = subprocess.run(
            ["gh", "api", "graphql", "--input", "-"],
            input=payload, capture_output=True, text=True, timeout=GH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("gh api graphql timed out")
    if result.returncode != 0:
        raise RuntimeError(f"gh api graphql failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _repo_owner_name(state):
    repo = (state.get("meta") or {}).get("repo") or ""
    if "/" not in repo:
        raise RuntimeError(f"state.json meta.repo is not \"owner/repo\": {repo!r}")
    owner, name = repo.split("/", 1)
    return owner, name


def _ensure_pending_review(state, gh_run):
    """The pending review's node id, looked up fresh on every call rather than cached on
    state -- a review submitted from the GitHub web UI mid-session would otherwise strand a
    stored id on a review that no longer exists. Opens one only when none is found.
    """
    owner, name = _repo_owner_name(state)
    number = (state.get("meta") or {}).get("pr")
    resp = gh_run(PENDING_REVIEW_QUERY, {"owner": owner, "repo": name, "number": number})
    pull_request = resp["data"]["repository"]["pullRequest"]
    nodes = (pull_request.get("reviews") or {}).get("nodes") or []
    if nodes:
        return nodes[0]["id"]
    resp = gh_run(OPEN_REVIEW_MUTATION, {"pullRequestId": pull_request["id"]})
    return resp["data"]["addPullRequestReview"]["pullRequestReview"]["id"]


def deliver_note(state, note, diff_text, gh_run):
    """Post one note through the pending review and record the result on it, in place --
    the GitHub call and the state mutation happen together so a crash between the two can
    never lose the record of a comment that landed.
    """
    reply_to = note.get("reply_to") or note.get("in_reply_to")
    parent = None
    if reply_to:
        parent = _index_by_id(state.get("notes", [])).get(reply_to)
        if parent is None or not parent.get("gh_node_id"):
            raise RuntimeError(f"reply target {reply_to!r} has not been delivered yet")

    review_id = _ensure_pending_review(state, gh_run)
    comment = None

    if parent is not None:
        resp = gh_run(REPLY_MUTATION, {
            "reviewId": review_id, "inReplyTo": parent["gh_node_id"], "body": note["body"],
        })
        comment = ((resp.get("data") or {}).get("addPullRequestReviewComment") or {}).get("comment")
        if comment is not None:
            note["gh_id"] = comment.get("databaseId")
            note["gh_node_id"] = comment.get("id")
            note["gh_thread_id"] = parent.get("gh_thread_id")
            note["anchor_status"] = parent.get("anchor_status", "anchored")
        # else: the parent's thread was deleted from the web UI mid-session -- fall through
        # and open a fresh thread instead of treating a null node as an error.

    if comment is None:
        ranges = postable_ranges(diff_text) if diff_text else {}
        line, side, anchor_status, note_suffix = resolve(
            note["path"], note["line"], note["side"], ranges
        )
        resp = gh_run(NEW_THREAD_MUTATION, {
            "reviewId": review_id, "path": note["path"], "body": note["body"] + note_suffix,
            "line": line, "side": side if line is not None else None,
            "subjectType": "LINE" if line is not None else "FILE",
        })
        thread = ((resp.get("data") or {}).get("addPullRequestReviewThread") or {}).get("thread")
        if thread is None:
            raise RuntimeError(f"GitHub rejected the anchor for note {note['id']!r}")
        thread_comment = ((thread.get("comments") or {}).get("nodes") or [{}])[0]
        note["gh_thread_id"] = thread["id"]
        note["gh_node_id"] = thread_comment.get("id")
        note["gh_id"] = thread_comment.get("databaseId")
        note["anchor_status"] = anchor_status

    note["state"] = "posted"
    return note


def submit_review(state, event, body, gh_run):
    review_id = _ensure_pending_review(state, gh_run)
    resp = gh_run(SUBMIT_MUTATION, {"reviewId": review_id, "event": event, "body": body})
    return ((resp.get("data") or {}).get("submitPullRequestReview") or {}).get("pullRequestReview")


def do_sync(args):
    state = _load_state(args.state)
    comments = read_paginated_json(sys.stdin)
    sync_comments(state, comments)
    _save_state(args.state, state)
    print(f"synced {len(comments)} comment(s), {len(state.get('notes', []))} note(s) total")
    return 0


def do_sync_threads(args):
    """Reads one gh api graphql response (REVIEW_THREADS_QUERY's shape) from stdin -- a
    transform over already-fetched state, same as do_sync, never calling `gh` itself. Warns on
    stderr, rather than paginating, when either connection hit its 100-item ceiling: threads or
    comments past it never get a gh_thread_id, so their Resolve-conversation button can't appear.
    """
    state = _load_state(args.state)
    payload = json.loads(sys.stdin.read())
    pull_request = (((payload.get("data") or {}).get("repository") or {})
                     .get("pullRequest") or {})
    review_threads = pull_request.get("reviewThreads") or {}
    threads = review_threads.get("nodes") or []

    if (review_threads.get("pageInfo") or {}).get("hasNextPage"):
        print(
            "warning: PR has more than 100 review threads; threads beyond the first 100 were "
            "not fetched and cannot be resolved from the page",
            file=sys.stderr,
        )
    for thread in threads:
        comments = thread.get("comments") or {}
        if (comments.get("pageInfo") or {}).get("hasNextPage"):
            print(
                f"warning: thread {thread.get('id')!r} has more than 100 comments; comments "
                "beyond the first 100 were not synced and cannot be resolved from the page",
                file=sys.stderr,
            )

    sync_threads(state, threads)
    _save_state(args.state, state)
    print(f"synced {len(threads)} thread(s)")
    return 0


def do_import(args):
    """Merge a pasted copy-for-agent payload into state.json."""
    state = _load_state(args.state)
    raw = Path(args.file).read_text() if args.file else sys.stdin.read()
    partial = json.loads(raw)

    before = {note["id"]: dict(note) for note in state.get("notes", [])}
    merged = merge_state(state, partial, is_page_origin=True)
    merged_by_id = {note["id"]: note for note in merged.get("notes", [])}

    added, updated = [], []
    for note in partial.get("notes", []):
        note_id = note.get("id")
        if not isinstance(note_id, str) or note_id not in merged_by_id:
            continue  # merge_state dropped it: a bad id, or a locked github-origin note
        if note_id not in before:
            added.append(note_id)
        elif merged_by_id[note_id] != before[note_id]:
            updated.append(note_id)

    _save_state(args.state, merged)

    touched = set(added) | set(updated)
    postable = set(pending_publish_ids(merged))
    ready = [note_id for note_id in touched if note_id in postable]
    print(f"imported: {len(added)} added, {len(updated)} updated")
    print("ready to post: " + (", ".join(ready) if ready else "none"))
    return 0


def do_payloads(args):
    state = _load_state(args.state)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    pairs = payloads_for(state, args.commit_id)
    for note_id, payload in pairs:
        (out_dir / f"{note_id}.json").write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {len(pairs)} payload(s) to {out_dir}")
    return 0


def do_promote(args):
    state = _load_state(args.state)
    if not promote_note(state.get("notes", []), args.id, args.gh_id, args.gh_url):
        print(f"no note with id {args.id}", file=sys.stderr)
        return 1
    _save_state(args.state, state)
    print(f"promoted {args.id}")
    return 0


def do_reanchor(args):
    state = _load_state(args.state)
    diff_text = Path(args.diff).read_text(errors="replace")
    reanchor_state(state, diff_text)
    _save_state(args.state, state)
    return 0


def do_deliver(args):
    state = _load_state(args.state)
    note = _index_by_id(state.get("notes", [])).get(args.id)
    if note is None:
        print(f"no note with id {args.id}", file=sys.stderr)
        return 1
    diff_path = Path(args.diff) if args.diff else Path(args.state).parent / "raw.diff"
    diff_text = diff_path.read_text(errors="replace") if diff_path.exists() else ""
    deliver_note(state, note, diff_text, _gh_graphql)
    # Persisted the instant the GitHub post lands, before any further bookkeeping -- a crash
    # after this line can never leave state.json still showing a posted note as a draft, which
    # is what made a retry re-post it as a duplicate thread.
    _save_state(args.state, state)
    print(f"delivered {args.id}")
    return 0


def do_submit(args):
    state = _load_state(args.state)
    body = Path(args.body_file).read_text()
    submit_review(state, args.event, body, _gh_graphql)
    print("submitted")
    return 0


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)

    sync_parser = sub.add_parser("sync")
    sync_parser.add_argument("--state", required=True)
    sync_parser.set_defaults(func=do_sync)

    sync_threads_parser = sub.add_parser("sync-threads")
    sync_threads_parser.add_argument("--state", required=True)
    sync_threads_parser.set_defaults(func=do_sync_threads)

    import_parser = sub.add_parser("import")
    import_parser.add_argument("--state", required=True)
    import_parser.add_argument("--file", help="read the payload from a file instead of stdin")
    import_parser.set_defaults(func=do_import)

    payloads_parser = sub.add_parser("payloads")
    payloads_parser.add_argument("--state", required=True)
    payloads_parser.add_argument("--commit-id", required=True)
    payloads_parser.add_argument("--out", required=True)
    payloads_parser.set_defaults(func=do_payloads)

    promote_parser = sub.add_parser("promote")
    promote_parser.add_argument("--state", required=True)
    promote_parser.add_argument("--id", required=True)
    promote_parser.add_argument("--gh-id", required=True)
    promote_parser.add_argument("--gh-url", required=True)
    promote_parser.set_defaults(func=do_promote)

    reanchor_parser = sub.add_parser("reanchor")
    reanchor_parser.add_argument("--state", required=True)
    reanchor_parser.add_argument("--diff", required=True)
    reanchor_parser.set_defaults(func=do_reanchor)

    deliver_parser = sub.add_parser("deliver")
    deliver_parser.add_argument("--state", required=True)
    deliver_parser.add_argument("--id", required=True)
    deliver_parser.add_argument(
        "--diff", help="defaults to raw.diff beside --state, the scratchpad convention"
    )
    deliver_parser.set_defaults(func=do_deliver)

    submit_parser = sub.add_parser("submit")
    submit_parser.add_argument("--state", required=True)
    submit_parser.add_argument("--event", required=True,
                                choices=["COMMENT", "APPROVE", "REQUEST_CHANGES"])
    submit_parser.add_argument("--body-file", required=True)
    submit_parser.set_defaults(func=do_submit)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
