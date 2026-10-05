#!/usr/bin/env python3
"""Phase 4: answer a question a reviewer asks about a selection on the live walkthrough page.

`validate_anchor` and `build_prompt` are synchronous and raise `cw_store.CWError` on bad input,
so the server can reject a request before it ever reaches a model. `answer` is the background
half the daemon runs under its per-walkthrough ask lock: it never raises, win or lose it writes
one `qa.jsonl` record and reports it through `on_event`.

Stdlib only except for talking to a model backend through cw_llm.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_llm  # noqa: E402
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

    raise cw_store.CWError("bad anchor: kind must be line or section")


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


def _qa_pairs_text(d):
    records = [r for r in read_qa(d) if r.get("status") == "ok"][-QA_PAIRS:]
    if not records:
        return ""
    pairs = [f"Q: {r.get('question', '')}\nA: {r.get('answer', '')}" for r in records]
    return "Previous Q&A:\n" + "\n\n".join(pairs)


def read_qa(d, limit=None):
    path = Path(d) / "qa.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records[-limit:] if limit is not None else records


def build_prompt(d, anchor, question):
    d = Path(d)
    quote = anchor["quote"]
    if anchor["kind"] == "section":
        part1 = f"Question:\n{question}\n\nSection: {anchor['section']}\n\nSelected text:\n{quote}"
    else:
        part1 = f"Question:\n{question}\n\nSelected text:\n{quote}"

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
    qa_text = _qa_pairs_text(d)
    if qa_text:
        parts.append(qa_text)

    return "\n\n".join(parts)[:PROMPT_CAP]


def answer(d, qid, anchor, question, on_event=None):
    """Never raises: everything that can fail, including loading config and resolving the
    ask profile, runs inside the try below so a misconfigured role lands as an ordinary
    status: "error" record instead of killing the daemon's background thread silently."""
    d = Path(d)
    record = {
        "qid": qid, "started_at": cw_store.now_iso(), "finished_at": None,
        "anchor": anchor, "question": question, "status": "ok", "answer": None,
        "error": None, "remedy": None, "profile": None, "model": None,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": None},
    }
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": None}

    try:
        config = cw_store.load_config()
        profile = cw_store.role_profile(config, "ask")
        if profile is None:
            record.update(status="error", error="no ask role", remedy="set roles.ask in config.json")
        else:
            record["profile"] = profile.get("name")
            record["model"] = profile.get("model")
            usage_hook = cw_run._make_on_usage(d, "ask", on_event, config)

            def on_usage(usage):
                usage_totals["prompt_tokens"] += usage.get("prompt_tokens", 0)
                usage_totals["completion_tokens"] += usage.get("completion_tokens", 0)
                cost = cw_llm.cost_usd(profile, usage)
                if cost is not None:
                    usage_totals["cost_usd"] = (usage_totals["cost_usd"] or 0.0) + cost
                usage_hook(usage)

            prompt = build_prompt(d, anchor, question)
            messages = [
                {"role": "system", "content": (SKILL_DIR / "prompts" / "ask.md").read_text()},
                {"role": "user", "content": prompt},
            ]
            tools, handlers = cw_run.read_tools(d)
            answer_text = cw_llm.run_tools(
                profile, messages, tools, handlers, nudge=None, max_rounds=10,
                max_tokens=config["max_conversation_tokens"], timeout=config["timeout_s"],
                on_usage=on_usage, cwd=d / "head",
            )
            record["status"] = "ok"
            record["answer"] = answer_text
    except (cw_llm.LLMError, cw_store.CWError) as e:
        record.update(status="error", error=str(e), remedy=e.remedy)
    except Exception as e:
        record.update(status="error", error=str(e), remedy=None)

    record["finished_at"] = cw_store.now_iso()
    record["usage"] = usage_totals
    _append_qa(d, record)
    if on_event:
        on_event("answer", record)
    return record


def _append_qa(d, record):
    with open(Path(d) / "qa.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")
