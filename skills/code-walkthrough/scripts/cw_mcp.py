#!/usr/bin/env python3
"""stdio MCP bridge: speaks newline-delimited JSON-RPC 2.0 to the agent harness, and HTTP to
cw_server.py (spawning it lazily on the first tool call). Also the CLI for setup/check/stop.

Usage:
  cw_mcp.py mcp                       run the stdio MCP server (default when no args)
  cw_mcp.py setup [--agent claude|print]
  cw_mcp.py outcomes --dir D --qid Q  per-turn outcome server (spawned by the daemon)
  cw_mcp.py runner --dir D --qid Q --worktree W --timeout S  code task run_check server
  cw_mcp.py check
  cw_mcp.py stop

Stdlib only. Logging goes to stderr; stdout carries only JSON-RPC replies.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_check  # noqa: E402
import cw_llm  # noqa: E402
import cw_store  # noqa: E402
import notes  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent
KNOWN_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

_STDOUT_LOCK = threading.Lock()

TOOLS = [
    {
        "name": "walkthrough_start",
        "description": ("Start or resume a live code-walkthrough run for a diff and return its URL. repo is an "
                        "absolute path of a local checkout, or owner/name. If owner/name is not cloned on this "
                        "machine the call fails and the error says so: ASK the user before passing clone: true, "
                        "which clones the repo (blobless) into the walkthrough cache."),
        "inputSchema": {
            "type": "object", "required": ["repo", "base", "head", "target", "slug"],
            "properties": {
                "repo": {"type": "string"}, "base": {"type": "string"}, "head": {"type": "string"},
                "clone": {"type": "boolean"}, "target": {"type": "string"}, "slug": {"type": "string"}, "pr": {"type": "integer"},
                "explain": {"type": "boolean"}, "paths": {"type": "array", "items": {"type": "string"}},
                "title": {"type": "string"}, "diff_file": {"type": "string"},
                "fresh": {"type": "boolean", "description": ("Regenerate everything from scratch, keeping user threads, "
                                                             "notes and state. Use only when the user explicitly reruns.")},
            },
        },
    },
    {
        "name": "walkthrough_get",
        "description": "Get the status, summary and usage of a walkthrough run, optionally waiting for it to finish.",
        "inputSchema": {
            "type": "object", "required": ["id"],
            "properties": {
                "id": {"type": "string"}, "key": {"type": "string"},
                "parts": {"type": "array", "items": {"enum": ["meta", "summary", "qa", "files", "notes", "threads"]}},
                "wait_s": {"type": "integer", "minimum": 0, "maximum": 600},
            },
        },
    },
    {
        "name": "walkthrough_list",
        "description": "List recent walkthroughs, newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}, "limit": {"type": "integer", "default": 20}},
        },
    },
    {
        "name": "walkthrough_reply",
        "description": ("Post the result of work you did in your own session into a walkthrough thread, "
                        "optionally completing a task the user handed to you. It only adds a reply; "
                        "nothing is published. The text is stored in the thread, shown on the page and "
                        "included in later thread prompts sent to the configured model backend, so it "
                        "must not contain secrets."),
        "inputSchema": {
            "type": "object", "required": ["id", "thread_id", "text"],
            "properties": {
                "id": {"type": "string"}, "key": {"type": "string"}, "thread_id": {"type": "string"},
                "oid": {"type": "string", "description": "a handed task's oid, to mark it done"},
                "text": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
        },
    },
]


def _log(message):
    print(message, file=sys.stderr, flush=True)


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _wait_pid_exit(pid, timeout=5):
    deadline = time.monotonic() + timeout
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)


def _probe_health(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _post(info, path, body=None, timeout=5):
    data = json.dumps(body or {}).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}{path}", data=data, method="POST",
        headers={"X-CW-Token": info["token"], "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read())


def _spawn_daemon():
    log_path = cw_store.log_path()
    log_f = open(log_path, "a")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPTS_DIR / "cw_server.py"), "serve"],
            start_new_session=True, stdin=subprocess.DEVNULL, stdout=log_f, stderr=subprocess.STDOUT,
            close_fds=True, cwd=str(cw_store.home()),
        )
    finally:
        log_f.close()
    # Reap the detached daemon on exit; otherwise it is a zombie that os.kill(pid, 0) still
    # sees as alive until this process exits.
    threading.Thread(target=proc.wait, daemon=True).start()


def ensure_server():
    """server.json names a live pid on our protocol and code: return it. A live pid on a different
    protocol gets shut down and waited out; so does one on different code (one predating the
    "code" field counts as different), unless it is mid-turn or mid-run, in which case it is left
    alone and replaced on the first call after it goes idle. Otherwise spawn and poll
    (CW_START_TIMEOUT)."""
    info = cw_store.read_json(cw_store.server_json_path())
    if info:
        health = _probe_health(info.get("port"))
        if (health and health.get("protocol") == cw_store.PROTOCOL
                and (health.get("code") == cw_store.code_id() or health.get("busy"))):
            return info
        if health is not None:
            try:
                _post(info, "/api/shutdown")
            except Exception:
                pass
            _wait_pid_exit(info.get("pid"))

    _spawn_daemon()
    timeout = float(os.environ.get("CW_START_TIMEOUT", 10))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = cw_store.read_json(cw_store.server_json_path())
        if info and _probe_health(info.get("port")):
            return info
        time.sleep(0.1)
    raise cw_store.CWError("daemon did not start", remedy=f"see {cw_store.log_path()}")


def rpc(tool, args, *, timeout=300):
    info = ensure_server()
    try:
        return _post(info, "/api/rpc", {"tool": tool, "args": args}, timeout=timeout)
    except (urllib.error.URLError, ConnectionRefusedError, ConnectionResetError):
        info = ensure_server()
        return _post(info, "/api/rpc", {"tool": tool, "args": args}, timeout=timeout)


OUTCOME_TOOLS = [
    {
        "name": "propose_resolve",
        "description": ("Suggest that an existing review thread on this comment's lines looks settled and "
                        "could be resolved. This only records a suggestion; the user decides."),
        "inputSchema": {
            "type": "object", "required": ["thread", "why"],
            "properties": {"thread": {"type": "string"}, "why": {"type": "string", "maxLength": 500}},
        },
    },
]


PAGE_EDIT_OPS = ("insert_after", "replace")
PAGE_EDIT_CAPS = {"prose": 4000, "list": 4000, "mermaid": 6000}

OUTCOME_TOOLS.append({
    "name": "propose_page_edit",
    "description": ("Change what the page shows next to the commented block: insert a new block after it or "
                    "replace it. The edit applies at once and the user can undo it. Prose and list blocks are "
                    "markdown-ish text, mermaid blocks are flowchart or sequence diagram source; never HTML."),
    "inputSchema": {
        "type": "object", "required": ["op", "target", "block"],
        "properties": {
            "op": {"enum": list(PAGE_EDIT_OPS)},
            "target": {"type": "string", "description": "the Block key from the message"},
            "block": {
                "type": "object", "required": ["type", "text"],
                "properties": {"type": {"enum": list(PAGE_EDIT_CAPS)}, "text": {"type": "string"}},
            },
        },
    },
})


GITHUB_DRAFT_CAP = 4000

OUTCOME_TOOLS.append({
    "name": "propose_github_draft",
    "description": ("Draft a GitHub review comment for the user: a new comment on the diff line they commented "
                    "on, or a reply in a review thread listed in the message. A draft is only a proposal; the "
                    "user keeps it as a local draft, edits it, or dismisses it, and nothing reaches GitHub "
                    "from this call. Set verbatim when the user asked to post their own words as they wrote "
                    "them."),
    "inputSchema": {
        "type": "object", "required": ["body", "target"],
        "properties": {
            "body": {"type": "string", "maxLength": GITHUB_DRAFT_CAP},
            "target": {
                "type": "object", "required": ["kind"],
                "properties": {"kind": {"enum": ["new", "reply"]},
                               "note_id": {"type": "string", "description": "the thread id, for kind reply"}},
            },
            "verbatim": {"type": "boolean"},
        },
    },
})


TASK_LIMITS = {"title": 120, "steps": 12, "step": 300, "files": 20}

OUTCOME_TOOLS.append({
    "name": "propose_task",
    "description": ("Record a plan for a code change the comment asks for. Nothing runs from this call: the "
                    "user reviews the plan and presses Run, and only then a separate agent makes the change "
                    "on a local branch that is never pushed. Do not paste code or patches."),
    "inputSchema": {
        "type": "object", "required": ["title", "steps"],
        "properties": {
            "title": {"type": "string", "maxLength": TASK_LIMITS["title"]},
            "steps": {"type": "array", "minItems": 1, "maxItems": TASK_LIMITS["steps"],
                      "items": {"type": "string", "maxLength": TASK_LIMITS["step"]}},
            "files": {"type": "array", "maxItems": TASK_LIMITS["files"],
                      "items": {"type": "string"}, "description": "repo-relative paths the change touches"},
        },
    },
})


DRAFT_COMMENTS_MAX = 30
DRAFT_BODY_CAP = 4000

OUTCOME_TOOLS.append({
    "name": "post_draft_comments",
    "description": ("Put review comments on diff lines as local drafts on the page, for the user to edit, "
                    "keep or remove. Nothing reaches GitHub until the user submits the review. Each comment "
                    "needs a path in the diff and a line (and optional end_line) inside one contiguous run "
                    "of diff lines on that side. suggestion is replacement code for RIGHT-side lines only. "
                    "One call per turn; any invalid comment rejects the whole call."),
    "inputSchema": {
        "type": "object", "required": ["comments"],
        "properties": {"comments": {
            "type": "array", "minItems": 1, "maxItems": DRAFT_COMMENTS_MAX,
            "items": {
                "type": "object", "required": ["path", "line", "body"],
                "properties": {
                    "path": {"type": "string"}, "line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                    "side": {"enum": ["RIGHT", "LEFT"]},
                    "body": {"type": "string", "maxLength": DRAFT_BODY_CAP},
                    "suggestion": {"type": "string", "maxLength": DRAFT_BODY_CAP},
                },
            },
        }},
    },
})


def task_plan_error(args):
    """Validates a code-task plan; shared by propose_task and the user's edit of a proposal."""
    title = args.get("title")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= TASK_LIMITS["title"]:
        return f"title must be 1 to {TASK_LIMITS['title']} characters"
    steps = args.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= TASK_LIMITS["steps"]:
        return f"steps must be a list of 1 to {TASK_LIMITS['steps']} strings"
    if any(not isinstance(s, str) or not 1 <= len(s.strip()) <= TASK_LIMITS["step"] for s in steps):
        return f"each step must be 1 to {TASK_LIMITS['step']} characters"
    files = args.get("files", [])
    if not isinstance(files, list) or len(files) > TASK_LIMITS["files"]:
        return f"files must be a list of at most {TASK_LIMITS['files']} paths"
    for f in files:
        if (not isinstance(f, str) or not f or "\x00" in f or f.startswith("/")
                or ".." in f.split("/")):
            return f"file {f!r} must be a repo-relative path without .."
    return None


def _refs(anchor_doc):
    return anchor_doc.get("anchors") or [anchor_doc.get("anchor") or {}]


def _check_task(anchor_doc, args, already):
    if not any(r.get("kind") in ("line", "thread") for r in _refs(anchor_doc)):
        return "code tasks attach to diff lines or review threads; reply in prose instead"
    error = task_plan_error(args)
    if error:
        return error
    if "task" in already:
        return "already proposed a code task in this turn"
    return None


def _task_payload(args, anchor_doc=None):
    return {"title": args["title"].strip(), "steps": [s.strip() for s in args["steps"]],
            "files": list(args.get("files") or [])}


def _check_resolve(anchor_doc, args, already):
    why = args.get("why")
    if not isinstance(why, str) or not why.strip():
        return "why is required"
    if len(why.strip()) > 500:
        return "why must be at most 500 characters"
    thread = args.get("thread")
    if not isinstance(thread, str):
        return "thread must be a string"
    if thread not in {t["id"] for t in anchor_doc.get("resolvable") or []}:
        return f"thread {thread} is not a review thread on this comment's lines; reply instead"
    if f"resolve:{thread}" in already:
        return "already suggested in this turn"
    return None


def _check_page_edit(anchor_doc, args, already):
    keys = [r[r["kind"]] for r in _refs(anchor_doc) if r.get("kind") in ("block", "section")]
    if not keys:
        return "page edits attach to prose blocks, not diff lines; reply instead"
    if args.get("op") not in PAGE_EDIT_OPS:
        return "op must be insert_after or replace"
    block = args.get("block")
    if not isinstance(block, dict):
        return "block must be an object with type and text"
    cap = PAGE_EDIT_CAPS.get(block.get("type"))
    if cap is None:
        return "block.type must be prose, list or mermaid"
    text = block.get("text")
    if not isinstance(text, str) or not text.strip():
        return "block.text is required"
    if len(text.strip()) > cap:
        return f"block.text must be at most {cap} characters for {block['type']}"
    if args.get("target") not in keys:
        if len(keys) == 1:
            return f"target must be {keys[0]}, this comment's block key"
        return f"target must be one of {', '.join(keys)}, this comment's block keys"
    if "page_edit" in already:
        return "already proposed a page edit in this turn"
    return None


def _check_github_draft(anchor_doc, args, already):
    refs = _refs(anchor_doc)
    lines = [r for r in refs if r.get("kind") == "line"]
    if not lines and not any(r.get("kind") == "thread" for r in refs):
        return ("this comment is on the page text, not on a diff line or a review thread, so no GitHub "
                "comment can be drafted; reply instead")
    body = args.get("body")
    if not isinstance(body, str) or not body.strip():
        return "body is required"
    if len(body.strip()) > GITHUB_DRAFT_CAP:
        return f"body must be at most {GITHUB_DRAFT_CAP} characters"
    if "verbatim" in args and not isinstance(args["verbatim"], bool):
        return "verbatim must be a boolean"
    target = args.get("target")
    target_kind = target.get("kind") if isinstance(target, dict) else None
    if target_kind == "new":
        if len(lines) != 1:
            return (("this comment is not on a diff line" if not lines else "this comment has several diff-line references")
                    + ", so a new review comment cannot be drafted; "
                    "reply in the thread or reply to an existing review thread")
    elif target_kind == "reply":
        note_id = target.get("note_id")
        if not isinstance(note_id, str) or note_id not in {r["id"] for r in anchor_doc.get("replyable") or []}:
            return f"note_id {note_id!r} is not a review thread this comment may reply to; reply in prose instead"
    else:
        return "target.kind must be new or reply"
    if "github_draft" in already:
        return "already drafted a GitHub comment in this turn"
    return None


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _check_reload(anchor_doc, args, already):
    if anchor_doc.get("mode") != "full":
        return "reload_walkthrough is only available in Full mode"
    if "reload" in already:
        return "already requested a reload in this turn"
    return None


def _check_draft_comments(anchor_doc, args, already):
    items = args.get("comments")
    if not isinstance(items, list) or not 1 <= len(items) <= DRAFT_COMMENTS_MAX:
        return f"comments must be a list of 1 to {DRAFT_COMMENTS_MAX} comments"
    try:
        ranges = notes.postable_ranges((Path(anchor_doc["dir"]) / "raw.diff").read_text(errors="replace"))
    except (KeyError, OSError):
        return "the diff is unavailable, so no comment can be drafted"
    for i, item in enumerate(items):
        error = _draft_item_error(item, ranges)
        if error:
            return f"comments[{i}]: {error}"
    if "draft_comments" in already:
        return "already posted draft comments in this turn"
    return None


def _draft_item_error(item, ranges):
    if not isinstance(item, dict):
        return "must be an object"
    path, line, end_line = item.get("path"), item.get("line"), item.get("end_line")
    side = item.get("side", "RIGHT")
    if side not in ("RIGHT", "LEFT"):
        return "side must be RIGHT or LEFT"
    if not isinstance(path, str) or (path, side) not in ranges:
        return f"{path} is not in the diff"
    if not _is_int(line) or line < 1:
        return "line must be a positive integer"
    if end_line is not None and (not _is_int(end_line) or end_line < line):
        return "end_line must be an integer at least line"
    last = line if end_line is None else end_line
    runs = ranges[(path, side)]
    if not any(start <= line <= end for start, end in runs):
        return f"line {line} of {path} ({side}) is not in the diff"
    if not any(start <= line and last <= end for start, end in runs):
        return f"lines {line}-{last} of {path} ({side}) are not in one contiguous diff range"
    body = item.get("body")
    if not isinstance(body, str) or not body.strip():
        return "body is required"
    if len(body.strip()) > DRAFT_BODY_CAP:
        return f"body must be at most {DRAFT_BODY_CAP} characters"
    suggestion = item.get("suggestion")
    if suggestion is not None:
        if not isinstance(suggestion, str) or len(suggestion) > DRAFT_BODY_CAP:
            return f"suggestion must be a string of at most {DRAFT_BODY_CAP} characters"
        if side != "RIGHT":
            return "a suggestion needs a RIGHT-side line"
    return None


def _draft_comments_payload(args, anchor_doc=None):
    out, seen = [], set()
    for item in args["comments"]:
        suggestion = item.get("suggestion")
        body = item["body"].strip()
        if suggestion is not None:
            fence = "`" * max(3, max((len(m) for m in re.findall(r"`+", suggestion)), default=0) + 1)
            body += f"\n\n{fence}suggestion\n{suggestion}\n{fence}"
        end_line = item.get("end_line")
        row = {"path": item["path"], "line": item["line"],
               "end_line": end_line if end_line != item["line"] else None,
               "side": item.get("side", "RIGHT"), "body": body}
        key = json.dumps(row, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(row)
    return {"comments": out}


def _resolve_payload(args, anchor_doc=None):
    return {"thread": args["thread"], "why": args["why"].strip()}


def _github_draft_payload(args, anchor_doc):
    original = "" if anchor_doc.get("source") == "triage" else anchor_doc.get("comment") or ""
    stripped = cw_store.strip_ref_fences(original)
    if stripped != original:
        original = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    verbatim = args.get("verbatim") is True and bool(original)
    if args["target"]["kind"] == "new":
        anchor = next(r for r in _refs(anchor_doc) if r.get("kind") == "line")
        target = {"kind": "new", **{k: anchor.get(k) for k in ("path", "line", "side", "end_line", "hunk_id")}}
    else:
        row = next(r for r in anchor_doc["replyable"] if r["id"] == args["target"]["note_id"])
        target = {"kind": "reply", "note_id": row["id"], "path": row["path"], "line": row["line"]}
    return {"body": original if verbatim else args["body"].strip(), "original": original,
            "verbatim": verbatim, "target": target}


def _page_edit_payload(args, anchor_doc=None):
    block = args["block"]
    return {"op": args["op"], "target": args["target"],
            "block": {"type": block["type"], "text": block["text"].strip()}}


FULL_ONLY_TOOLS = [{
    "name": "reload_walkthrough",
    "description": ("Show your edits from this turn as a new walkthrough of the agent branch, opened beside "
                    "this one. The current walkthrough is never rebuilt. Call it once, after your last edit."),
    "inputSchema": {"type": "object", "properties": {}},
}]

OUTCOME_KINDS = {
    "propose_task": {
        "outcome": "task", "state": "proposed", "check": _check_task, "payload": _task_payload,
        "key": lambda payload: "task",
        "accepted": "recorded as a plan; nothing runs until the user presses Run",
    },
    "propose_resolve": {
        "outcome": "resolve", "state": "proposed", "check": _check_resolve, "payload": _resolve_payload,
        "key": lambda payload: f"resolve:{payload['thread']}", "accepted": "recorded as a suggestion; the user decides",
    },
    "propose_github_draft": {
        "outcome": "github_draft", "state": "proposed", "check": _check_github_draft,
        "payload": _github_draft_payload, "key": lambda payload: "github_draft",
        "accepted": "drafted for the user, who keeps or dismisses it; nothing was posted to GitHub",
    },
    "propose_page_edit": {
        "outcome": "page_edit", "state": "applied", "check": _check_page_edit, "payload": _page_edit_payload,
        "key": lambda payload: "page_edit", "accepted": "applied to the page; the user can undo it",
    },
    "post_draft_comments": {
        "outcome": "draft_comments", "state": "applied", "check": _check_draft_comments,
        "payload": _draft_comments_payload, "key": lambda payload: "draft_comments",
        "accepted": "added to the page as local drafts; the user can edit or remove them and nothing was posted to GitHub",
    },
    "reload_walkthrough": {
        "outcome": "reload", "state": "requested", "check": _check_reload,
        "payload": lambda args, anchor_doc=None: {}, "key": lambda payload: "reload",
        "accepted": "requested; the walkthrough of your edits opens when this turn ends",
    },
}


def check_outcome(anchor_doc, name, args, already=()):
    kind = OUTCOME_KINDS.get(name)
    if kind is None:
        return f"unknown tool {name}"
    if name not in ("post_draft_comments", "reload_walkthrough") and all(r.get("kind") == "free" for r in _refs(anchor_doc)):
        return "this comment has no reference (no diff line, prose block or review thread), so this tool cannot be used; reply instead"
    return kind["check"](anchor_doc, args if isinstance(args, dict) else {}, already)


def accept_outcome(d, qid, name, args):
    d = Path(d)
    anchor_doc = cw_store.read_json(d / "turns" / f"{qid}.anchor.json")
    if not isinstance(anchor_doc, dict):
        return False, "outcome channel unavailable"
    path = d / "turns" / f"{qid}.outcomes.jsonl"
    already = set()
    try:
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
                already.add(OUTCOME_KINDS[rec["name"]]["key"](rec["arguments"]))
            except (ValueError, KeyError, TypeError):
                pass
    except OSError:
        pass
    error = check_outcome({**anchor_doc, "dir": str(d)}, name, args, already)
    if error:
        return False, error
    kind = OUTCOME_KINDS[name]
    record = {"oid": "o-" + uuid.uuid4().hex[:8], "name": name,
              "arguments": kind["payload"](args, anchor_doc), "raw_arguments": args, "at": cw_store.now_iso()}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return True, kind["accepted"]


def outcome_tools(mode="review"):
    if mode == "review":
        return OUTCOME_TOOLS
    if mode == "full":
        return [t for t in OUTCOME_TOOLS if t["name"] != "propose_task"] + FULL_ONLY_TOOLS
    raise ValueError(f"unknown outcome mode {mode}")


def outcome_mcp_config(d, qid, mode="review"):
    args = [str(Path(__file__).resolve()), "outcomes", "--dir", str(d), "--qid", qid]
    if mode != "review":
        args += ["--mode", mode]
    return {"mcpServers": {"cw": {"type": "stdio", "command": sys.executable, "args": args}}}


RUNNER_TOOLS = [{
    "name": "run_check",
    "description": ("Run one test, build or lint command (go test, cargo test, npm test, pytest, make test ...) "
                    "in the checkout, offline and without a shell. No git, no network, nothing outside the checkout."),
    "inputSchema": {"type": "object", "required": ["command"], "properties": {"command": {"type": "string"}}},
}]


def runner_mcp_config(d, qid, worktree, timeout):
    return {"mcpServers": {"cw": {"type": "stdio", "command": sys.executable, "args": [
        str(Path(__file__).resolve()), "runner", "--dir", str(d), "--qid", qid,
        "--worktree", str(worktree), "--timeout", str(timeout)]}}}


def checks_path(d, qid):
    return Path(d) / "turns" / f"{qid}.checks.jsonl"


def _write_response(obj):
    with _STDOUT_LOCK:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def _handle_initialize(req):
    requested = (req.get("params") or {}).get("protocolVersion")
    protocol = requested if requested in KNOWN_PROTOCOLS else KNOWN_PROTOCOLS[0]
    return {
        "protocolVersion": protocol, "capabilities": {"tools": {}},
        "serverInfo": {"name": "code-walkthrough", "version": "1"},
    }


def _handle_tools_list(_req):
    return {"tools": TOOLS}


def _handle_tools_call(req):
    params = req.get("params") or {}
    name, args = params.get("name"), params.get("arguments") or {}
    timeout = 300
    if name == "walkthrough_get":
        timeout = min(int(args.get("wait_s") or 0), 600) + 30
    try:
        reply = rpc(name, args, timeout=timeout)
    except cw_store.CWError as e:
        return {"content": [{"type": "text", "text": json.dumps({"error": str(e), "remedy": e.remedy})}],
                "isError": True}
    except Exception as e:
        return {"content": [{"type": "text", "text": json.dumps({"error": str(e), "remedy": None})}],
                "isError": True}
    if reply.get("ok"):
        return {"content": [{"type": "text", "text": json.dumps(reply["result"])}], "isError": False}
    return {"content": [{"type": "text", "text": json.dumps({"error": reply.get("error"), "remedy": reply.get("remedy")})}],
            "isError": True}


_METHODS = {"initialize": _handle_initialize, "tools/list": _handle_tools_list, "ping": lambda _req: {}}


def _dispatch(req):
    req_id = req.get("id")
    if req_id is None:
        return
    method = req.get("method")
    if method == "tools/call":
        threading.Thread(target=lambda: _write_response(
            {"jsonrpc": "2.0", "id": req_id, "result": _handle_tools_call(req)}), daemon=True).start()
        return
    handler = _METHODS.get(method)
    if handler is None:
        _write_response({"jsonrpc": "2.0", "id": req_id,
                          "error": {"code": -32601, "message": f"unknown method {method}"}})
        return
    _write_response({"jsonrpc": "2.0", "id": req_id, "result": handler(req)})


def cmd_mcp():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            _log(f"bad json-rpc line: {line!r}")
            continue
        _dispatch(req)
    return 0


def _log_file(message):
    try:
        with open(cw_store.log_path(), "a") as f:
            f.write(f"outcomes: {message}\n")
    except OSError:
        _log(message)


def _outcomes_dispatch(d, qid, req, tools=None, call=None):
    req_id = req.get("id")
    if req_id is None:
        return None
    method = req.get("method")
    if method == "initialize":
        result = _handle_initialize(req)
    elif method == "tools/list":
        result = {"tools": tools or OUTCOME_TOOLS}
    elif method == "ping":
        result = {}
    elif method == "tools/call":
        params = req.get("params") or {}
        if params.get("name") not in {t["name"] for t in tools or OUTCOME_TOOLS}:
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": f"unknown tool {params.get('name')}"}], "isError": True}}
        ok, text = (call or (lambda name, args: accept_outcome(d, qid, name, args)))(
            params.get("name"), params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": text}], "isError": not ok}
    else:
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _answer_failure(req):
    req_id = req.get("id") if isinstance(req, dict) else None
    if req_id is None:
        return None
    if req.get("method") == "tools/call":
        resp = {"result": {"content": [{"type": "text", "text": "outcome server error"}], "isError": True}}
    else:
        resp = {"error": {"code": -32603, "message": "internal error"}}
    return {"jsonrpc": "2.0", "id": req_id, **resp}


def cmd_outcomes(d, qid, tools=None, call=None):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = None
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("not an object")
            resp = _outcomes_dispatch(d, qid, req, tools, call)
        except Exception as e:
            _log_file(f"{type(e).__name__}: {e}")
            resp = _answer_failure(req)
        if resp is not None:
            try:
                _write_response(resp)
            except Exception as e:
                _log_file(f"write failed: {type(e).__name__}: {e}")
    return 0


def cmd_runner(d, qid, worktree, timeout):
    scratch = Path(d) / "turns" / f"{qid}.scratch"

    def call(name, args):
        if name != "run_check":
            return False, f"unknown tool {name}"
        return cw_check.run_check(args.get("command"), worktree, scratch, checks_path(d, qid), timeout)

    return cmd_outcomes(d, qid, RUNNER_TOOLS, call)


_CONFIG_TEMPLATE = {
    "profiles": {
        "proxy": {"base_url": "http://localhost:4000/v1", "model": "<a model your proxy routes>",
                   "api_key_env": "LITELLM_API_KEY"},
        "local": {"base_url": "http://localhost:11434/v1", "model": "<an Ollama model with tools>"},
    },
    "roles": {"analysis": "proxy", "prose": "proxy", "ask": "proxy", "escalate": "proxy"},
    "max_concurrency": 4,
    "timeout_s": 300,
    "max_conversation_tokens": 200000,
}

_CLAUDE_CODE_TEMPLATE = {
    "profiles": {"claude": {"kind": "claude-code", "model": "sonnet"}},
    "roles": {"analysis": "claude", "prose": "claude", "ask": "claude"},
    "max_concurrency": 4,
    "timeout_s": 600,
    "max_conversation_tokens": 200000,
}


def cmd_setup(agent):
    config_path = cw_store.config_path()
    if not config_path.exists():
        if shutil.which("claude"):
            cw_store.write_json(config_path, _CLAUDE_CODE_TEMPLATE)
            _log(f"wrote {config_path} (claude-code template)")
        else:
            cw_store.write_json(config_path, _CONFIG_TEMPLATE)
            _log(f"wrote {config_path} (proxy template)")

    if "/.claude/plugins/" in str(SCRIPTS_DIR):
        _log(f"MCP server already registered by the plugin install; config is at {config_path}")
        return 0

    mcp_py = str(SCRIPTS_DIR / "cw_mcp.py")
    if agent == "print":
        print(json.dumps({"mcpServers": {"code-walkthrough": {"command": sys.executable, "args": [mcp_py, "mcp"]}}}))
        return 0
    if agent == "claude":
        subprocess.run(["claude", "mcp", "remove", "-s", "user", "code-walkthrough"], capture_output=True)
        result = subprocess.run(
            ["claude", "mcp", "add", "-s", "user", "code-walkthrough", "--", sys.executable, mcp_py, "mcp"]
        )
        return result.returncode
    _log(f"unknown --agent {agent!r}")
    return 2


def cmd_check():
    config = cw_store.load_config()
    failed = False
    for role in config.get("roles", {}):
        try:
            profile = cw_store.role_profile(config, role)
        except cw_store.CWError as e:
            print(f"FAIL {role}: {e}")
            print(f"  remedy: {e.remedy}")
            failed = True
            continue
        if profile is None:
            continue
        try:
            cw_llm.check(profile)
            print(f"ok {role} ({profile['name']}/{profile['model']})")
        except cw_llm.LLMError as e:
            print(f"FAIL {role}: {e}")
            print(f"  remedy: {e.remedy}")
            failed = True
    return 1 if failed else 0


def cmd_stop():
    info = cw_store.read_json(cw_store.server_json_path())
    if not info:
        print("not running")
        return 0
    try:
        _post(info, "/api/shutdown")
    except Exception:
        pass
    _wait_pid_exit(info.get("pid"))
    if _pid_alive(info.get("pid")):
        try:
            os.kill(info["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    print("stopped")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="cw_mcp.py")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("mcp")
    setup_parser = sub.add_parser("setup")
    setup_parser.add_argument("--agent", choices=["claude", "print"], default="print")
    outcomes_parser = sub.add_parser("outcomes")
    outcomes_parser.add_argument("--dir", required=True)
    outcomes_parser.add_argument("--qid", required=True)
    outcomes_parser.add_argument("--mode", choices=["review", "full"], default="review")
    runner_parser = sub.add_parser("runner")
    runner_parser.add_argument("--dir", required=True)
    runner_parser.add_argument("--qid", required=True)
    runner_parser.add_argument("--worktree", required=True)
    runner_parser.add_argument("--timeout", type=int, default=300)
    sub.add_parser("check")
    sub.add_parser("stop")
    args = parser.parse_args(argv)

    if args.cmd in (None, "mcp"):
        return cmd_mcp()
    if args.cmd == "outcomes":
        return cmd_outcomes(args.dir, args.qid, outcome_tools(args.mode))
    if args.cmd == "runner":
        return cmd_runner(args.dir, args.qid, args.worktree, args.timeout)
    if args.cmd == "setup":
        return cmd_setup(args.agent)
    if args.cmd == "check":
        return cmd_check()
    if args.cmd == "stop":
        return cmd_stop()
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
