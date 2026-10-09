#!/usr/bin/env python3
"""Phase 4: answer a question a reviewer asks about a selection on the live walkthrough page.

`validate_anchor` and `build_prompt` are synchronous and raise `cw_store.CWError` on bad input,
so the server can reject a request before it ever reaches a model. `answer` is the background
half the daemon runs under its per-thread ask lock: it never raises, win or lose it writes
a final turn record to `qa.jsonl` and reports it through `on_event` as a "thread" event.

Stdlib only except for talking to a model backend through cw_llm.
"""

import contextlib
import json
import os
import re
import shutil
import signal
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_llm  # noqa: E402
import cw_mcp  # noqa: E402
import cw_run  # noqa: E402
import cw_store  # noqa: E402
import notes  # noqa: E402
from validate_analysis import HUNK_PREFIX, parse_hunks  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent

PROMPT_CAP = 16000
HUNK_WINDOW = 40
QA_PAIRS = 5
QUESTION_MAX = 2000
BLOCK_RE = re.compile(r"[A-Za-z0-9:_.|-]{1,200}")
DELTA_INTERVAL_S = 0.1
DETAIL_MAX = 200
MCP_PREFIX = "mcp__cw__"
THREAD_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def validate_anchor(anchor):
    if not isinstance(anchor, dict):
        raise cw_store.CWError("bad anchor")
    quote = anchor.get("quote")
    if not isinstance(quote, str) or not quote:
        raise cw_store.CWError("bad anchor: quote required")
    kind = anchor.get("kind")

    if kind == "line":
        path = anchor.get("path")
        side = anchor.get("side")
        line = anchor.get("line")
        end_line = anchor.get("end_line")
        hunk_id = anchor.get("hunk_id")
        if not isinstance(path, str) or not path or "\x00" in path:
            raise cw_store.CWError("bad anchor: path required")
        if side not in ("LEFT", "RIGHT"):
            raise cw_store.CWError("bad anchor: side must be LEFT or RIGHT")
        if not isinstance(line, int) or isinstance(line, bool) or line <= 0:
            raise cw_store.CWError("bad anchor: line must be a positive integer")
        if end_line is not None and (
            not isinstance(end_line, int) or isinstance(end_line, bool) or end_line < line
        ):
            raise cw_store.CWError("bad anchor: end_line must be an integer >= line")
        if hunk_id is not None and not isinstance(hunk_id, str):
            raise cw_store.CWError("bad anchor: hunk_id must be a string")
        return {"kind": "line", "path": path, "side": side, "line": line,
                "end_line": end_line, "hunk_id": hunk_id, "quote": quote}

    if kind == "section":
        section = anchor.get("section")
        if not isinstance(section, str) or not section:
            raise cw_store.CWError("bad anchor: section required")
        return {"kind": "section", "section": section, "quote": quote}

    if kind == "block":
        block = anchor.get("block")
        section = anchor.get("section")
        if not isinstance(block, str) or not BLOCK_RE.fullmatch(block):
            raise cw_store.CWError("bad anchor: block key invalid")
        if section is not None and not isinstance(section, str):
            raise cw_store.CWError("bad anchor: section must be a string")
        return {"kind": "block", "block": block, "section": section, "quote": quote}

    if kind == "thread":
        note_id = anchor.get("note_id")
        if not isinstance(note_id, str) or not BLOCK_RE.fullmatch(note_id):
            raise cw_store.CWError("bad anchor: note_id invalid")
        return {"kind": "thread", "note_id": note_id, "quote": quote}

    raise cw_store.CWError("bad anchor: kind must be line, section, block or thread")


def _find_hunk(entry, anchor):
    """The hunk the frontend's hunk_id names, or else the one whose body actually covers
    anchor's line on anchor's side -- a stale hunk_id (the diff moved under the page) falls
    back to the same line search parse_hunks itself would do."""
    hunk_id = anchor.get("hunk_id")
    if hunk_id:
        prefix = hunk_id.split("\t", 1)[-1]
        for hunk in entry["hunks"]:
            if hunk["prefix"] == prefix:
                return hunk

    line, side = anchor["line"], anchor["side"]
    for hunk in entry["hunks"]:
        for kind, old_line, new_line, _raw in notes._walk_hunk_lines({"hunks": [hunk]}):
            if side == "LEFT" and kind != "a" and old_line == line:
                return hunk
            if side == "RIGHT" and kind != "d" and new_line == line:
                return hunk
    return None


def _window_rows(hunk, side, anchor_line, window=HUNK_WINDOW):
    rows = []
    for kind, old_line, new_line, raw in notes._walk_hunk_lines({"hunks": [hunk]}):
        if side == "LEFT":
            if kind == "a":
                continue
            n = old_line
        else:
            if kind == "d":
                continue
            n = new_line
        if abs(n - anchor_line) <= window:
            rows.append((n, raw))
    return rows


def _analysis(d):
    return cw_store.read_json(d / "analysis.json") or cw_store.read_json(d / "analysis.partial.json") or {}


def _role_and_note(d, path, hunk):
    analysis = _analysis(d)
    file_entry = next(
        (f for f in analysis.get("files", []) if isinstance(f, dict) and f.get("path") == path), None)
    if not file_entry:
        return ""
    role = file_entry.get("role") or ""
    note = ""
    for h in file_entry.get("hunks") or []:
        match = HUNK_PREFIX.match((h.get("header") or "").strip())
        if match and match.group(1) == hunk["prefix"]:
            note = h.get("note") or ""
            break
    lines = []
    if role:
        lines.append(f"File role: {role}")
    if note:
        lines.append(f"Hunk note: {note}")
    return "\n".join(lines)


def _overview_verdict(d):
    analysis = _analysis(d)
    overview = analysis.get("overview") or ""
    verdict = analysis.get("verdict") or ""
    parts = []
    if overview:
        parts.append(f"Overview:\n{overview}")
    if verdict:
        parts.append(f"Verdict:\n{verdict}")
    return "\n\n".join(parts)


def _qa_pairs_text(d, thread_id=None):
    if thread_id is None:
        return ""
    records = [r for r in read_qa(d) if r["thread_id"] == thread_id and r.get("status") == "ok"][-QA_PAIRS:]
    if not records:
        return ""
    pairs = [f"Q: {r['comment']}\nA: {r.get('answer', '')}" for r in records]
    return "Previous Q&A:\n" + "\n\n".join(pairs)


def _read_records(d):
    path = Path(d) / "qa.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def read_qa(d, limit=None):
    """Turn records only, one per qid (the last written wins, so a pending record is replaced
    by its final one), in order of first appearance. Pre-thread records get thread_id=qid and
    comment=question."""
    folded = {}
    for r in _read_records(d):
        if "type" in r or "qid" not in r:
            continue
        folded[r["qid"]] = r
    turns = []
    for r in folded.values():
        r = dict(r)
        r.setdefault("thread_id", r["qid"])
        r.setdefault("comment", r.get("question"))
        r.setdefault("question", r["comment"])
        turns.append(r)
    return turns[-limit:] if limit is not None else turns


def read_threads(d):
    """{thread_id: {"turns": [folded turns in order], "resolved": bool, "outcomes": [folded]}};
    resolved is the latest resolve event for the thread, False when there is none."""
    threads = {}
    for r in read_qa(d):
        threads.setdefault(r["thread_id"], {"turns": [], "resolved": False, "outcomes": []})["turns"].append(r)
    for r in _read_records(d):
        if r.get("type") == "resolve" and r.get("thread_id") in threads:
            threads[r["thread_id"]]["resolved"] = bool(r.get("resolved"))
    for o in read_outcomes(d):
        if o["thread_id"] in threads:
            threads[o["thread_id"]]["outcomes"].append(o)
    return threads


class OutcomeError(cw_store.CWError):
    """kind is not_found, conflict or invalid, for the server to map to a status."""

    def __init__(self, message, kind, remedy=None):
        super().__init__(message, remedy=remedy)
        self.kind = kind


def _state_notes(d):
    st = cw_store.read_json(Path(d) / "state.json") or {}
    return [n for n in st.get("notes", []) if isinstance(n, dict)]


def is_root_thread(n):
    return bool(n.get("origin") == "github" and n.get("gh_thread_id")
                and not (n.get("reply_to") or n.get("in_reply_to")))


def _thread_row(n):
    return {"id": n["id"], "path": n.get("path"), "line": n.get("line"),
            "author": n.get("author"), "body": (n.get("body") or "")[:500]}


def _root_threads(d, anchor, include_resolved):
    if anchor.get("kind") == "thread":
        roots = [n for n in _state_notes(d) if n.get("id") == anchor["note_id"] and is_root_thread(n)]
    elif anchor.get("kind") == "line":
        first = anchor["line"]
        last = anchor.get("end_line") or first
        roots = [n for n in _state_notes(d)
                 if is_root_thread(n) and n.get("path") == anchor["path"]
                 and isinstance(n.get("line"), int) and first <= n["line"] <= last]
    else:
        return []
    return [_thread_row(n) for n in roots if include_resolved or not n.get("resolved")]


def _resolvable(d, anchor):
    return _root_threads(d, anchor, include_resolved=False)


def _replyable(d, anchor):
    return _root_threads(d, anchor, include_resolved=True)


def write_turn_anchor(d, qid, thread_id, anchor, comment=None):
    path = Path(d) / "turns" / f"{qid}.anchor.json"
    path.parent.mkdir(exist_ok=True)
    cw_store.write_json(path, {"qid": qid, "thread_id": thread_id, "anchor": anchor, "comment": comment,
                               "resolvable": _resolvable(d, anchor), "replyable": _replyable(d, anchor)})


def _folded_outcome(r):
    return {"oid": r["oid"], "qid": r.get("qid"), "thread_id": r.get("thread_id"),
            "outcome": r.get("outcome"), "payload": r.get("payload"), "state": r.get("state")}


def read_outcomes(d):
    folded = {}
    for r in _read_records(d):
        if r.get("type") == "outcome" and "oid" in r:
            folded[r["oid"]] = _folded_outcome(r)
        elif r.get("type") == "state" and r.get("oid") in folded:
            o = folded[r["oid"]]
            o["state"] = r.get("state")
            if "payload" in r:
                o["payload"] = r["payload"]
    resolved = {n.get("id") for n in _state_notes(d) if n.get("resolved")}
    for o in folded.values():
        if o["state"] == "proposed" and o["outcome"] == "resolve" and o["payload"]["thread"] in resolved:
            o["state"] = "done"
    return list(folded.values())


def sweep_outcomes(d, qid, thread_id, on_event=None):
    try:
        lines = (Path(d) / "turns" / f"{qid}.outcomes.jsonl").read_text().splitlines()
    except OSError:
        return
    known = {r.get("oid") for r in _read_records(d) if r.get("type") == "outcome"}
    for line in lines:
        try:
            rec = json.loads(line)
            oid, kind = rec["oid"], cw_mcp.OUTCOME_KINDS[rec["name"]]
            payload = rec["arguments"]
            if not isinstance(payload, dict):
                continue
        except (ValueError, KeyError, TypeError):
            continue
        if oid in known:
            continue
        known.add(oid)
        record = {"type": "outcome", "oid": oid, "qid": qid, "thread_id": thread_id,
                  "outcome": kind["outcome"], "payload": payload, "state": kind["state"],
                  "at": cw_store.now_iso()}
        _append_qa(d, record)
        if on_event:
            on_event("outcome", _folded_outcome(record))


_OUTCOME_LOCK = threading.Lock()


def outcome_action(d, oid, action, payload=None, on_event=None):
    with _OUTCOME_LOCK:
        return _outcome_action(d, oid, action, payload, on_event)


_TRANSITIONS = {
    ("resolve", "dismiss"): ("proposed", "dismissed"),
    ("resolve", "edit"): ("proposed", "proposed"),
    ("github_draft", "dismiss"): ("proposed", "dismissed"),
    ("github_draft", "edit"): ("proposed", "proposed"),
    ("github_draft", "keep"): ("proposed", "kept"),
    ("github_draft", "verbatim"): ("proposed", "proposed"),
    ("page_edit", "revert"): ("applied", "reverted"),
    ("page_edit", "reapply"): ("reverted", "applied"),
}


def _outcome_action(d, oid, action, payload, on_event):
    d = Path(d)
    outcomes = read_outcomes(d)
    current = next((o for o in outcomes if o["oid"] == oid), None)
    if current is None:
        raise OutcomeError("outcome not found", "not_found")
    transition = _TRANSITIONS.get((current["outcome"], action))
    if transition is None:
        raise OutcomeError(f"unknown action {action!r}", "invalid")
    from_state, to_state = transition
    if current["state"] != from_state:
        raise OutcomeError("outcome is not open" if from_state == "proposed" else
                           f"outcome is {current['state']}, not {from_state}", "conflict")
    event = {"type": "state", "oid": oid, "by": "user", "at": cw_store.now_iso(), "state": to_state}
    if current["outcome"] == "github_draft" and action in ("edit", "keep", "verbatim"):
        event["payload"] = _github_draft_payload(current["payload"], action, payload)
    elif action == "edit":
        anchor_doc = cw_store.read_json(d / "turns" / f"{current['qid']}.anchor.json") or {}
        already = {f"resolve:{o['payload']['thread']}" for o in outcomes
                   if o["outcome"] == "resolve" and o["qid"] == current["qid"] and o["oid"] != oid}
        error = cw_mcp.check_outcome(anchor_doc, "propose_resolve", payload, already)
        if error:
            raise OutcomeError(error, "invalid", remedy="fix the suggestion and try again")
        event["payload"] = {"thread": payload["thread"], "why": payload["why"].strip()}
    _append_qa(d, event)
    folded = next(o for o in read_outcomes(d) if o["oid"] == oid)
    if on_event:
        on_event("outcome", folded)
    return folded


def _github_draft_payload(current, action, payload):
    if action == "edit":
        body = (payload or {}).get("body")
        if not isinstance(body, str) or not body.strip() or len(body) > cw_mcp.GITHUB_DRAFT_CAP:
            raise OutcomeError(f"body must be 1-{cw_mcp.GITHUB_DRAFT_CAP} characters", "invalid",
                               remedy="fix the draft and try again")
        return {**current, "body": body, "edited": True, "verbatim": False}
    if action == "keep":
        note_id = (payload or {}).get("note_id")
        if not isinstance(note_id, str) or not notes.VALID_ID_RE.fullmatch(note_id):
            raise OutcomeError("note_id must be a note id", "invalid", remedy="send {note_id}")
        return {**current, "note_id": note_id}
    return {**current, "body": current.get("original") or current["body"], "verbatim": True, "edited": False}


def mark_outcome(d, oid, state, on_event=None):
    """Writes a state event that no /outcomes action owns (publish-one's published and done)."""
    with _OUTCOME_LOCK:
        _append_qa(d, {"type": "state", "oid": oid, "by": "user", "at": cw_store.now_iso(), "state": state})
        folded = next(o for o in read_outcomes(d) if o["oid"] == oid)
    if on_event:
        on_event("outcome", folded)
    return folded


def _note_loc(d, note_id):
    for n in _state_notes(d):
        if n.get("id") == note_id:
            return f"{n.get('path')}:{n.get('line')}"
    return note_id


def _outcome_updates_text(d, thread_id, since_last_turn=True):
    by_oid = {o["oid"]: o for o in read_outcomes(d) if o["thread_id"] == thread_id}
    lines = []
    for r in _read_records(d):
        if "type" not in r:
            if (since_last_turn and r.get("thread_id", r.get("qid")) == thread_id
                    and r.get("finished_at")):
                lines = []
            continue
        o = by_oid.get(r.get("oid"))
        if r["type"] != "state" or o is None:
            continue
        payload = r.get("payload") or o["payload"]
        if o["outcome"] == "page_edit":
            verb = "undid" if r.get("state") == "reverted" else "redid"
            where = "after block" if payload["op"] == "insert_after" else "of block"
            lines.append(f"The user {verb} your page edit ({payload['op']} {where} {payload['target']})")
            continue
        if o["outcome"] == "github_draft":
            where = f"{payload['target'].get('path')}:{payload['target'].get('line')}"
            if r.get("state") == "dismissed":
                lines.append(f"The user dismissed your draft GitHub comment on {where}")
            elif payload.get("verbatim"):
                lines.append(f"The user replaced your draft GitHub comment on {where} with their own words, "
                             "unchanged")
            elif r.get("state") == "proposed":
                lines.append(f"The user edited your draft GitHub comment on {where} to: {payload['body']}")
            continue
        if r.get("state") not in ("dismissed", "proposed"):
            continue
        verb = "dismissed" if r.get("state") == "dismissed" else "edited"
        lines.append(f"The user {verb} your suggestion to resolve "
                     f"{_note_loc(d, payload['thread'])}: {payload['why']}")
    return "\n".join(lines)


def _candidates_text(d, anchor):
    found = _resolvable(d, anchor)
    if not found:
        return ""
    rows = [f"- {c['id']}, {c['path']}:{c['line']}, {c['author']}: {c['body'][:300]}" for c in found]
    return "Review threads on these lines you may propose to resolve:\n" + "\n".join(rows)


def _reply_targets_text(d, anchor):
    if anchor.get("kind") != "line":
        return ""
    found = _replyable(d, anchor)
    if not found:
        return ""
    rows = [f"- {c['id']}, {c['path']}:{c['line']}, {c['author']}: {c['body'][:300]}" for c in found]
    return "Review threads on these lines you may reply to with propose_github_draft:\n" + "\n".join(rows)


THREAD_TEXT_CAP = 8000
THREAD_COMMENT_CAP = 2000


def _thread_text(d, anchor):
    if anchor.get("kind") != "thread":
        return ""
    state_notes = _state_notes(d)
    root = next((n for n in state_notes if n["id"] == anchor["note_id"]), None)
    if root is None:
        return ""
    by_id = {n["id"]: n for n in state_notes}

    def root_id(n):
        seen = set()
        while (n.get("reply_to") or n.get("in_reply_to")) in by_id and n["id"] not in seen:
            seen.add(n["id"])
            n = by_id[n.get("reply_to") or n.get("in_reply_to")]
        return n["id"]

    comments = sorted((n for n in state_notes if root_id(n) == root["id"] and n.get("state") != "draft"),
                      key=lambda n: n.get("created_at") or "")
    lines = [f"Review thread {root['id']} on {root.get('path')}:{root.get('line')}"
             f"{' (resolved)' if root.get('resolved') else ''}"]
    if root.get("diff_hunk"):
        lines.append("Diff hunk:\n" + root["diff_hunk"][:THREAD_COMMENT_CAP])
    lines.append("Comments, oldest first:")
    lines += [f"{n.get('author') or 'unknown'}: {(n.get('body') or '')[:THREAD_COMMENT_CAP]}" for n in comments]
    return "\n".join(lines)[:THREAD_TEXT_CAP]


def _ensure_ctx(d):
    """Copies, not symlinks: the model reads them through --add-dir and the analysis can be
    replaced under it. A copy is refreshed only when its source is newer."""
    ctx = d / "ctx"
    ctx.mkdir(exist_ok=True)
    analysis = d / "analysis.json"
    if not analysis.exists():
        analysis = d / "analysis.partial.json"
    for source, name in ((analysis, "analysis.json"), (d / "raw.diff", "raw.diff")):
        dest = ctx / name
        if source.exists() and (not dest.exists() or source.stat().st_mtime > dest.stat().st_mtime):
            shutil.copyfile(source, dest)
    return ctx


_CTX_NOTE = ("The walkthrough's own files sit in `{ctx_dir}`: `analysis.json` (the notes shown on the page) "
             "and `raw.diff` (the full diff). Read them with the Read, Grep and Glob tools by absolute path "
             "when the diff and notes in the message do not answer the comment.")


def _system_prompt(ctx_dir=None):
    text = (SKILL_DIR / "prompts" / "comment.md").read_text()
    note = _CTX_NOTE.replace("{ctx_dir}", str(ctx_dir)) if ctx_dir else ""
    return text.replace("{ctx_note}\n", note + "\n" if note else "")


def _thread_session(d, thread_id):
    for r in reversed(read_qa(d)):
        if r["thread_id"] == thread_id and r.get("session_id"):
            return r["session_id"]
    return None


def build_prompt(d, anchor, question, thread_id=None, resumed=False):
    d = Path(d)
    block_key = f"Block key: {anchor[anchor['kind']]}" if anchor["kind"] in ("section", "block") else ""
    if resumed:
        extras = [block_key, _candidates_text(d, anchor), _reply_targets_text(d, anchor),
                  _outcome_updates_text(d, thread_id)]
        return "\n\n".join([f"Question:\n{question}", *filter(None, extras)])
    quote = anchor["quote"]
    part1 = f"Question:\n{question}"
    if anchor["kind"] in ("section", "block") and anchor.get("section"):
        part1 += f"\n\nSection: {anchor['section']}"
    if block_key:
        part1 += f"\n\n{block_key}"
    part1 += f"\n\nSelected text:\n{quote}"

    hunk = None
    part2 = ""
    if anchor["kind"] == "line":
        raw_path = d / "raw.diff"
        raw_diff = raw_path.read_text(errors="replace") if raw_path.exists() else ""
        _order, files = parse_hunks(raw_diff)
        entry = files.get(anchor["path"])
        if entry:
            hunk = _find_hunk(entry, anchor)
            if hunk:
                rows = _window_rows(hunk, anchor["side"], anchor["line"])
                if rows:
                    part2 = "Diff context:\n" + "\n".join(f"{n}\t{raw}" for n, raw in rows)

    combined = part1 + ("\n\n" + part2 if part2 else "")
    if len(combined) > PROMPT_CAP:
        raise cw_store.CWError("selection too large", remedy="select a smaller range")

    parts = [part1]
    if part2:
        parts.append(part2)
    if hunk is not None:
        role_note = _role_and_note(d, anchor["path"], hunk)
        if role_note:
            parts.append(role_note)
    overview_verdict = _overview_verdict(d)
    if overview_verdict:
        parts.append(overview_verdict)
    thread_text = _thread_text(d, anchor)
    if thread_text:
        parts.append(thread_text)
    candidates = _candidates_text(d, anchor)
    if candidates:
        parts.append(candidates)
    reply_targets = _reply_targets_text(d, anchor)
    if reply_targets:
        parts.append(reply_targets)
    qa_text = _qa_pairs_text(d, thread_id)
    if qa_text:
        parts.append(qa_text)
    if thread_id is not None:
        updates = _outcome_updates_text(d, thread_id, since_last_turn=False)
        if updates:
            parts.append(updates)

    return "\n\n".join(parts)[:PROMPT_CAP]


def begin_turn(d, qid, anchor, comment, thread_id=None, on_event=None):
    """Persist the comment as a pending record before any model work, so it survives a crash
    and shows up in GET /qa while the turn is queued."""
    record = {"qid": qid, "thread_id": thread_id or qid, "anchor": anchor, "comment": comment,
              "question": comment, "started_at": cw_store.now_iso(), "status": "pending"}
    _append_qa(d, record)
    if on_event:
        on_event("thread", {"kind": "turn", "record": record})
    return record


def append_resolve(d, thread_id, resolved, on_event=None):
    _append_qa(d, {"type": "resolve", "thread_id": thread_id, "resolved": resolved,
                   "at": cw_store.now_iso()})
    if on_event:
        on_event("thread", {"kind": "resolve", "thread_id": thread_id, "resolved": resolved})


def finalise_stale(d, in_flight):
    """Close every pending turn whose qid is not in flight (the daemon died or restarted
    mid-turn) with an error record, so the page stops showing it as working."""
    for r in read_qa(d):
        if r.get("status") == "pending" and r["qid"] not in in_flight:
            _append_qa(d, {
                "qid": r["qid"], "thread_id": r["thread_id"], "anchor": r.get("anchor"),
                "comment": r["comment"], "question": r["question"],
                "started_at": r.get("started_at"), "finished_at": cw_store.now_iso(),
                "status": "error", "error": "turn was interrupted",
                "remedy": "send the comment again"})
            sweep_outcomes(d, r["qid"], r["thread_id"])


def _server_log(message):
    try:
        with open(cw_store.log_path(), "a") as f:
            f.write(f"{cw_store.now_iso()} {message}\n")
    except OSError:
        pass


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _tool_detail(tool_input):
    for key in ("file_path", "pattern", "path"):
        value = (tool_input or {}).get(key)
        if isinstance(value, str) and value:
            return value[:DETAIL_MAX]
    return ""


def _emit_progress(live, on_event, qid, thread_id, tool, detail):
    progress = {"qid": qid, "thread_id": thread_id, "tool": tool, "detail": detail}
    live["progress"] = progress
    if on_event:
        on_event("progress", progress)


def _stream_once(argv, prompt, *, cwd, timeout, qid, thread_id, on_event, live, sweep=None):
    st = {"init": False, "session_id": None, "texts": [], "result": None, "bad_mcp": False,
          "buf": "", "last": 0.0, "sep": False, "mcp_ids": set()}

    def flush():
        if not st["buf"]:
            return
        text, st["buf"] = st["buf"], ""
        with live.get("lock") or contextlib.nullcontext():
            live["text"] = live.get("text", "") + text
            live["seq"] = seq = live.get("seq", 0) + 1
        st["last"] = time.monotonic()
        if on_event:
            on_event("delta", {"qid": qid, "thread_id": thread_id, "seq": seq, "text": text})

    def on_spawn(proc):
        live["proc"] = proc
        if live.get("cancelled"):
            _kill(proc)

    def on_line(obj):
        kind = obj.get("type")
        if kind != "stream_event":
            flush()
        if kind == "system" and obj.get("subtype") == "init":
            st["init"] = True
            st["session_id"] = obj.get("session_id")
            if not any(s.get("name") == "cw" and s.get("status") == "connected"
                       for s in obj.get("mcp_servers") or [] if isinstance(s, dict)):
                st["bad_mcp"] = True
                if live.get("proc"):
                    _kill(live["proc"])
        elif kind == "stream_event":
            event = obj.get("event") or {}
            etype = event.get("type")
            if etype == "content_block_start" and (event.get("content_block") or {}).get("type") == "text":
                st["sep"] = bool(live.get("text") or st["buf"])
            elif etype == "content_block_delta" and (event.get("delta") or {}).get("type") == "text_delta":
                chunk = event["delta"].get("text") or ""
                if st["sep"]:
                    chunk, st["sep"] = "\n\n" + chunk, False
                st["buf"] += chunk
                if time.monotonic() - st["last"] >= DELTA_INTERVAL_S:
                    flush()
        elif kind == "assistant":
            for block in (obj.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    st["texts"].append(block.get("text") or "")
                elif block.get("type") == "tool_use":
                    name = block.get("name") or ""
                    if name.startswith(MCP_PREFIX):
                        st["mcp_ids"].add(block.get("id"))
                    _emit_progress(live, on_event, qid, thread_id, name.removeprefix(MCP_PREFIX),
                                   _tool_detail(block.get("input")))
        elif kind == "user" and sweep:
            content = (obj.get("message") or {}).get("content")
            blocks = content if isinstance(content, list) else []
            if any(isinstance(b, dict) and b.get("type") == "tool_result"
                   and b.get("tool_use_id") in st["mcp_ids"] for b in blocks):
                sweep()
        elif kind == "result":
            st["result"] = obj

    try:
        st["returncode"], st["stderr"] = cw_llm.claude_stream(
            argv, prompt, cwd=cwd, timeout=timeout, on_line=on_line, on_spawn=on_spawn)
    finally:
        flush()
        live["proc"] = None
    return st


def _claude_turn(d, record, profile, config, anchor, question, on_event, live, on_usage):
    qid, thread_id = record["qid"], record["thread_id"]
    ctx = _ensure_ctx(d).resolve()
    system = _system_prompt(ctx)
    mcp_config = cw_mcp.outcome_mcp_config(d, qid)
    prior = _thread_session(d, thread_id)
    write_turn_anchor(d, qid, thread_id, anchor, question)

    def run(session_id=None, resume=None):
        argv = cw_llm.thread_argv(
            profile, system, mcp_config=mcp_config, add_dir=ctx, session_id=session_id, resume=resume)
        prompt = build_prompt(d, anchor, question, thread_id, resumed=bool(resume))
        return _stream_once(
            argv, prompt, cwd=d / "head", timeout=config["timeout_s"], qid=qid,
            thread_id=thread_id, on_event=on_event, live=live,
            sweep=lambda: sweep_outcomes(d, qid, thread_id, on_event))

    st = run(resume=prior) if prior else run(session_id=str(uuid.uuid4()))
    if prior and st["returncode"] != 0 and not st["init"] and not live.get("cancelled"):
        _server_log(f"reseeded thread {thread_id} (resume of {prior} failed)")
        if st["result"]:
            on_usage(cw_llm.claude_usage(st["result"]))
        with live.get("lock") or contextlib.nullcontext():
            live["text"] = ""
            live["seq"] = live.get("seq", 0) + 1
        st = run(session_id=str(uuid.uuid4()))

    if st["init"]:
        record["session_id"] = st["session_id"]
    result = st["result"]
    if result:
        on_usage(cw_llm.claude_usage(result))
    completed = bool(result) and not result.get("is_error")
    if live.get("cancelled") and not completed:
        record.update(status="cancelled", answer=live.get("text") or "")
        return
    if st["bad_mcp"]:
        raise cw_llm.LLMError(
            "the outcome server did not start", kind="config", remedy="see server.log")

    if result is None or result.get("is_error") or (st["returncode"] != 0 and not live.get("cancelled")):
        parsed = result or {}
        kind, remedy = cw_llm._classify_claude_error(parsed, st["stderr"], profile)
        message = (parsed.get("result") or st["stderr"] or "claude produced no result")[:300]
        raise cw_llm.LLMError(message, kind=kind, remedy=remedy)

    answer_text = "\n\n".join(t for t in st["texts"] if t) or result.get("result") or ""
    with live.get("lock") or contextlib.nullcontext():
        live["text"] = answer_text
        live["seq"] = live.get("seq", 0) + 1
    record["answer"] = answer_text


class _Cancelled(Exception):
    pass


_READ_TOOL_NAMES = {"read_file": "Read", "grep": "Grep", "list_dir": "Glob"}


def _check_cancelled(live):
    # Stop only takes effect at the next tool boundary: the HTTP chat call is not interruptible.
    if live.get("cancelled"):
        raise _Cancelled()


def _openai_tools(d, qid, thread_id, on_event, live):
    tools, handlers = cw_run.read_tools(d)

    def wrap(name, handler):
        def run(args):
            _check_cancelled(live)
            _emit_progress(live, on_event, qid, thread_id, name, _tool_detail(args))
            return handler(args)
        return run

    handlers = {name: wrap(_READ_TOOL_NAMES.get(name, name), fn) for name, fn in handlers.items()}

    def outcome_handler(name):
        def run(args):
            _check_cancelled(live)
            _emit_progress(live, on_event, qid, thread_id, name, "")
            _ok, text = cw_mcp.accept_outcome(d, qid, name, args)
            sweep_outcomes(d, qid, thread_id, on_event)
            return text
        return run

    for spec in cw_mcp.OUTCOME_TOOLS:
        tools.append({"type": "function", "function": {
            "name": spec["name"], "description": spec["description"], "parameters": spec["inputSchema"]}})
        handlers[spec["name"]] = outcome_handler(spec["name"])
    return tools, handlers


def answer(d, qid, anchor, question, on_event=None, thread_id=None, live=None):
    """Never raises: everything that can fail, including loading config and resolving the
    thread profile, runs inside the try below so a misconfigured role lands as an ordinary
    status: "error" record instead of killing the daemon's background thread silently.
    `live` is the daemon's per-turn dict (text, seq, progress, proc, cancelled)."""
    d = Path(d)
    live = live if live is not None else {}
    record = {
        "qid": qid, "thread_id": thread_id or qid, "started_at": cw_store.now_iso(),
        "finished_at": None, "anchor": anchor, "comment": question, "question": question, "status": "ok", "answer": None,
        "error": None, "remedy": None, "profile": None, "model": None, "session_id": None,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": None},
    }
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": None}

    try:
        config = cw_store.load_config()
        profile = cw_store.role_profile(config, "thread")
        if profile is None:
            record.update(status="error", error="no thread role",
                          remedy="set roles.thread or roles.ask in config.json")
        else:
            record["profile"] = profile.get("name")
            record["model"] = profile.get("model")
            usage_hook = cw_run._make_on_usage(d, "thread", on_event, config)

            def on_usage(usage):
                usage_totals["prompt_tokens"] += usage.get("prompt_tokens", 0)
                usage_totals["completion_tokens"] += usage.get("completion_tokens", 0)
                cost = cw_llm.cost_usd(profile, usage)
                if cost is not None:
                    usage_totals["cost_usd"] = (usage_totals["cost_usd"] or 0.0) + cost
                usage_hook(usage)

            if profile.get("kind") == "claude-code":
                _claude_turn(d, record, profile, config, anchor, question, on_event, live, on_usage)
            else:
                write_turn_anchor(d, qid, record["thread_id"], anchor, question)
                prompt = build_prompt(d, anchor, question, record["thread_id"])
                messages = [
                    {"role": "system", "content": _system_prompt()},
                    {"role": "user", "content": prompt},
                ]
                tools, handlers = _openai_tools(d, qid, record["thread_id"], on_event, live)
                answer_text = cw_llm.run_tools(
                    profile, messages, tools, handlers, nudge=None, max_rounds=10,
                    max_tokens=config["max_conversation_tokens"], timeout=config["timeout_s"],
                    on_usage=on_usage, cwd=d / "head",
                )
                record["status"] = "ok"
                record["answer"] = answer_text
    except _Cancelled:
        record.update(status="cancelled", answer="")
    except (cw_llm.LLMError, cw_store.CWError) as e:
        record.update(status="error", error=str(e), remedy=e.remedy)
    except Exception as e:
        record.update(status="error", error=str(e), remedy=None)

    sweep_outcomes(d, qid, record["thread_id"], on_event)
    record["finished_at"] = cw_store.now_iso()
    record["usage"] = usage_totals
    _append_qa(d, record)
    if on_event:
        on_event("thread", {"kind": "turn", "record": record})
    return record


def _append_qa(d, record):
    with open(Path(d) / "qa.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")
