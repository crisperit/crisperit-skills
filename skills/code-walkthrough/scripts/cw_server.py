#!/usr/bin/env python3
"""The daemon: a localhost-only HTTP server that serves the live walkthrough page, streams
run events over SSE, accepts page-note PUTs, and answers the three RPC tools cw_mcp.py
exposes over MCP. One daemon per machine, arbitrated by cw_store.acquire_daemon_lock().

Usage: cw_server.py serve

Stdlib only. Binds 127.0.0.1 only; see references/live.md for the localhost guard.
"""

import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_ask  # noqa: E402
import cw_llm  # noqa: E402
import cw_run  # noqa: E402  direct, not the runner seam: /post's gh-facing calls (notes.py
import cw_store  # noqa: E402
import cw_task  # noqa: E402
import notes  # noqa: E402
import splice_assets  # noqa: E402
from notes import VALID_ID_RE  # noqa: E402  same id check a page write is subject to

HOST = "127.0.0.1"
MAX_BODY = 1_000_000

START_LOCK = threading.Lock()

_MERMAID_PLACEHOLDER = "<!-- MERMAID_JS -->"
_mermaid_js = None


def _mermaid_payload():
    global _mermaid_js
    if _mermaid_js is None:
        _mermaid_js = splice_assets.mermaid_payload(Path(__file__).resolve().parent.parent)
    return _mermaid_js


runner = None  # test seam: inject a fake with prepare_walkthrough/run; defaults to cw_run


def _runner():
    import importlib
    return runner or importlib.import_module("cw_run")


class _Hub:
    """Per-walkthrough SSE fanout: one Queue per connected tab."""

    def __init__(self):
        self._lock = threading.Lock()
        self._queues = {}

    def register(self, key, wid):
        q = queue.Queue()
        with self._lock:
            self._queues.setdefault((key, wid), []).append(q)
        return q

    def unregister(self, key, wid, q):
        with self._lock:
            queues = self._queues.get((key, wid))
            if queues and q in queues:
                queues.remove(q)
            if queues == []:
                del self._queues[(key, wid)]

    def emit(self, key, wid, event, data):
        with self._lock:
            queues = list(self._queues.get((key, wid), []))
        for q in queues:
            q.put((event, data))

    def client_count(self):
        with self._lock:
            return sum(len(v) for v in self._queues.values())


def _page_name(meta):
    if meta.get("page") == "final":
        return f"{meta.get('slug')}.html"
    return "partial.html"


def _steps_public(meta):
    return {
        name: {"status": step.get("status"), "error": step.get("error"), "remedy": step.get("remedy")}
        for name, step in (meta.get("steps") or {}).items()
    }


def _live_payload(key, wid, meta, d, token, port):
    drafts_doc = cw_store.read_json(d / "page-notes.json", default=None)
    drafts = drafts_doc.get("notes") if isinstance(drafts_doc, dict) else None
    sib = meta.get("sibling_of")
    sibling = None
    if sib:
        sibling = {
            "parent_url": f"http://{HOST}:{port}/walkthrough/{sib['key']}/{sib['id']}/?k={token}",
            "title": sib.get("title"), "branch": sib.get("branch"), "sha": meta.get("head"),
            "task_id": sib.get("task_id"), "changed_files": sib.get("changed_files") or [],
            "prose_carried": bool(sib.get("prose_carried")),
        }
    return {
        "v": 1, "key": key, "id": wid, "token": token,
        "api": f"/api/walkthrough/{key}/{wid}",
        "rev": meta.get("rev", 0), "page": meta.get("page"),
        "status": meta.get("status"), "error": meta.get("error"), "remedy": meta.get("remedy"),
        "steps": _steps_public(meta),
        "batches": {"done": len(meta.get("batches_done") or []), "total": meta.get("batches", 0)},
        "usage": meta.get("usage", {}), "total": cw_store.usage_total(meta),
        "drafts": drafts, "sibling": sibling,
    }


def _snapshot_payload(live):
    return {k: v for k, v in live.items() if k not in ("v", "key", "id", "token", "api", "drafts")}


def _first_unreported_remedy(meta):
    for name, step in (meta.get("steps") or {}).items():
        if step.get("status") == "remedy" and not step.get("remedy_reported"):
            return name
    return None


def _tool_walkthrough_start(daemon, args):
    with START_LOCK:
        d, meta, reused = _runner().prepare_walkthrough(args, is_running=daemon.is_running)
        key, wid = meta["key"], meta["id"]
        if meta.get("status") == "building" and not daemon.is_running(d):
            daemon.start_run(key, wid, d)
    url = f"http://{HOST}:{daemon.port}/walkthrough/{key}/{wid}/?k={daemon.token}"
    return {"id": wid, "key": key, "dir": str(d), "url": url, "reused": reused, "status": meta.get("status")}


def _resolve_key(wid, key):
    if key is not None:
        return key
    matches = []
    root = cw_store.store_root()
    if root.exists():
        for key_dir in root.iterdir():
            if key_dir.is_dir() and (key_dir / wid).exists():
                matches.append(key_dir.name)
    if not matches:
        raise cw_store.CWError("not found")
    if len(matches) > 1:
        raise cw_store.CWError("ambiguous id", remedy=f"pass key, one of: {', '.join(sorted(matches))}")
    return matches[0]


def _tool_walkthrough_get(daemon, args):
    wid = args.get("id")
    if not isinstance(wid, str) or not cw_store.ID_RE.fullmatch(wid):
        raise cw_store.CWError("bad id")
    key = args.get("key")
    if key is not None and (not isinstance(key, str) or not cw_store.KEY_RE.fullmatch(key)):
        raise cw_store.CWError("bad key")
    key = _resolve_key(wid, key)
    try:
        d = cw_store.walkthrough_dir(key, wid)
    except ValueError as e:
        raise cw_store.CWError("bad key or id") from e
    meta = cw_store.read_meta(d)
    if meta is None:
        raise cw_store.CWError("not found")

    wait_s = min(int(args.get("wait_s") or 0), 600)
    deadline = time.monotonic() + wait_s
    while True:
        if meta.get("status") != "building":
            break
        remedy_name = _first_unreported_remedy(meta)
        if remedy_name is not None:
            meta = cw_store.update_meta(d, lambda m, n=remedy_name: m["steps"][n].update(remedy_reported=True))
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
        meta = cw_store.read_meta(d) or meta

    parts = args.get("parts") or ["meta", "summary", "qa"]
    result = {
        "id": wid, "key": key, "status": meta.get("status"), "error": meta.get("error"),
        "remedy": meta.get("remedy"),
        "url": f"http://{HOST}:{daemon.port}/walkthrough/{key}/{wid}/?k={daemon.token}",
    }
    analysis = cw_store.read_json(d / "analysis.json") or cw_store.read_json(d / "prose.json")
    if "meta" in parts:
        result["meta"] = {
            "target": meta.get("target"), "base": meta.get("base"), "head": meta.get("head"),
            "route": meta.get("route"), "batches": meta.get("batches"),
            "batches_done": meta.get("batches_done"), "steps": _steps_public(meta),
            "usage": meta.get("usage", {}), "total": cw_store.usage_total(meta),
            "created_at": meta.get("created_at"), "updated_at": meta.get("updated_at"), "dir": str(d),
        }
    if "summary" in parts:
        groups = analysis.get("groups") if analysis else None
        result["summary"] = {
            "verdict": analysis.get("verdict") if analysis else None,
            "groups": [g.get("title") for g in groups] if isinstance(groups, list) else None,
            "gate": meta.get("gate", []),
            "page": str(d / _page_name(meta)),
        }
    if "qa" in parts:
        daemon.finalise_stale_turns(d)
        result["qa"] = [
            {"qid": r.get("qid"), "question": r.get("question"), "status": r.get("status"),
             "answer": r.get("answer")}
            for r in cw_ask.read_qa(d)[-20:]
        ]
    if "files" in parts:
        result["files"] = [{"path": f.get("path"), "role": f.get("role")}
                            for f in (analysis or {}).get("files", []) if isinstance(f, dict)]
    if "notes" in parts:
        notes = []
        for f in (analysis or {}).get("files", []) if analysis else []:
            if not isinstance(f, dict):
                continue
            for hunk in f.get("hunks") or []:
                if isinstance(hunk, dict):
                    notes.append({"path": f.get("path"), "header": hunk.get("header"), "note": hunk.get("note")})
        result["notes"] = notes
    return result


def _tool_walkthrough_list(daemon, args):
    limit = args.get("limit") or 20
    repo = args.get("repo")
    key_filter = cw_store.repo_key(repo) if repo else None
    root = cw_store.store_root()
    metas = []
    if root.exists():
        for key_dir in root.iterdir():
            if not key_dir.is_dir() or (key_filter and key_dir.name != key_filter):
                continue
            for wid_dir in key_dir.iterdir():
                meta = cw_store.read_meta(wid_dir)
                if meta is not None:
                    metas.append(meta)
    metas.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
    walkthroughs = [
        {
            "id": m.get("id"), "key": m.get("key"), "repo": m.get("repo"), "target": m.get("target"),
            "status": m.get("status"), "updated_at": m.get("updated_at"),
            "url": f"http://{HOST}:{daemon.port}/walkthrough/{m.get('key')}/{m.get('id')}/?k={daemon.token}",
        }
        for m in metas[:limit]
    ]
    return {"walkthroughs": walkthroughs}


_RPC_TOOLS = {
    "walkthrough_start": _tool_walkthrough_start,
    "walkthrough_get": _tool_walkthrough_get,
    "walkthrough_list": _tool_walkthrough_list,
}


_PAGE_RE = re.compile(r"^/walkthrough/([^/]+)/([^/]+)/?$")
_EVENTS_RE = re.compile(r"^/api/walkthrough/([^/]+)/([^/]+)/events$")
_NOTES_RE = re.compile(r"^/api/walkthrough/([^/]+)/([^/]+)/notes$")
_ASK_RE = re.compile(r"^/api/walkthrough/([^/]+)/([^/]+)/ask$")
_COMMENT_RE = re.compile(r"^/api/walkthrough/([^/]+)/([^/]+)/comment$")
_RESOLVE_RE = re.compile(r"^/api/walkthrough/([^/]+)/([^/]+)/threads/([^/]+)/resolve$")
_CANCEL_RE = re.compile(r"^/api/walkthrough/([^/]+)/([^/]+)/comment/(t?q-[0-9a-f]{8})/cancel$")
_OUTCOME_RE = re.compile(r"^/api/walkthrough/([^/]+)/([^/]+)/outcomes/(o-[0-9a-f]{8})/(dismiss|edit|keep|verbatim|revert|reapply|show|run|stop|discard)$")
_QA_RE = re.compile(r"^/api/walkthrough/([^/]+)/([^/]+)/qa$")
_POST_PREVIEW_RE = re.compile(r"^/api/walkthrough/([^/]+)/([^/]+)/post/preview$")
_POST_RE = re.compile(r"^/api/walkthrough/([^/]+)/([^/]+)/post$")
_TRIAGE_RE = re.compile(r"^/api/walkthrough/([^/]+)/([^/]+)/triage$")
_PUBLISH_ONE_RE = re.compile(r"^/api/walkthrough/([^/]+)/([^/]+)/publish-one$")
_OID_RE = re.compile(r"^o-[0-9a-f]{8}$")

THREAD_TURN_CAP = 20
TRIAGE_CAP = 30
TRIAGE_COMMENT = (
    "Triage this review thread. Judge whether it needs a code change, a reply, or can be resolved, "
    "and propose the matching outcome. Needs a code change: explain the change in your reply, do not "
    "write code. Needs a reply: propose a GitHub draft reply, written fresh and never verbatim. Safe "
    "to resolve: call propose_resolve, with an optional short reply draft. Nothing is published."
)

NO_PR_MSG = "No PR for this comparison -- use Copy for agent instead."
HEAD_NOT_PUSHED_MSG = "The compared commit has not been pushed yet -- use Copy for agent instead."
SIBLING_NO_PUBLISH = {"error": "publishing is off in this view", "remedy": "go back to the PR head"}
STILL_BUILDING_MSG = "walkthrough still building"
FAILED_MSG = "walkthrough did not finish"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, content_type, data):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        if status >= 400:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status, obj):
        self._send(status, "application/json", json.dumps(obj).encode())

    def _send_text(self, status, text_):
        self._send(status, "text/plain; charset=utf-8", text_.encode())

    def _send_html(self, status, html):
        self._send(status, "text/html; charset=utf-8", html.encode())

    def _check_host(self):
        daemon = self.server.cw_daemon
        host = self.headers.get("Host", "")
        if host not in (f"127.0.0.1:{daemon.port}", f"localhost:{daemon.port}"):
            self._send_json(403, {"error": "forbidden"})
            return False
        return True

    def _check_origin(self):
        daemon = self.server.cw_daemon
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        if origin not in (f"http://127.0.0.1:{daemon.port}", f"http://localhost:{daemon.port}"):
            self._send_json(403, {"error": "forbidden"})
            return False
        return True

    def _check_token_header(self):
        daemon = self.server.cw_daemon
        token = self.headers.get("X-CW-Token", "")
        if not hmac.compare_digest(token, daemon.token):
            self._send_json(403, {"error": "forbidden"})
            return False
        return True

    def _check_token_query(self, qs):
        daemon = self.server.cw_daemon
        token = (qs.get("k") or [""])[0]
        if not hmac.compare_digest(token, daemon.token):
            self._send_text(403, "forbidden")
            return False
        return True

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_BODY:
            # ponytail: drain capped at 4x MAX_BODY; a Content-Length far beyond that can
            # still race a close against the client's write. Routes here require a valid
            # token, so raise the cap if an authenticated client ever needs it.
            self._drain(min(length, MAX_BODY * 4))
            self.close_connection = True
            self._send_json(413, {"error": "payload too large"})
            return None, True
        return (self.rfile.read(length) if length else b""), False

    def _drain(self, n):
        remaining = n
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _resolve_dir(self, key, wid, *, json_errors):
        send = self._send_json if json_errors else self._send_text
        bad_key_or_id = {"error": "bad key or id"} if json_errors else "bad key or id"
        not_found = {"error": "not found"} if json_errors else "not found"
        try:
            d = cw_store.walkthrough_dir(key, wid)
        except ValueError:
            send(400, bad_key_or_id)
            return None
        if not d.exists():
            send(404, not_found)
            return None
        return d

    def _tracked(self, fn):
        daemon = self.server.cw_daemon
        daemon.enter_request()
        try:
            fn()
        except Exception as e:
            try:
                self._send_json(500, {"error": str(e)})
            except Exception:
                pass
        finally:
            daemon.exit_request()

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path == "/health":
            return self._tracked(self._route_health)
        m = _EVENTS_RE.match(path)
        if m:
            # Long-lived; tracked via hub client_count, not the request tracker, for idle exit.
            return self._route_events(m.group(1), m.group(2), qs)
        m = _QA_RE.match(path)
        if m:
            return self._tracked(lambda: self._route_qa(m.group(1), m.group(2)))
        m = _PAGE_RE.match(path)
        if m:
            return self._tracked(lambda: self._route_page(m.group(1), m.group(2), qs))
        self._tracked(lambda: self._send_text(404, "not found"))

    def do_PUT(self):
        path = urllib.parse.urlsplit(self.path).path
        m = _NOTES_RE.match(path)
        if m:
            return self._tracked(lambda: self._route_notes(m.group(1), m.group(2)))
        self._tracked(lambda: self._send_text(404, "not found"))

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/rpc":
            return self._tracked(self._route_rpc)
        if path == "/api/shutdown":
            return self._tracked(self._route_shutdown)
        m = _ASK_RE.match(path)
        if m:
            return self._tracked(lambda: self._route_comment(m.group(1), m.group(2), "question"))
        m = _COMMENT_RE.match(path)
        if m:
            return self._tracked(lambda: self._route_comment(m.group(1), m.group(2), "text"))
        m = _CANCEL_RE.match(path)
        if m:
            return self._tracked(lambda: self._route_cancel(m.group(1), m.group(2), m.group(3)))
        m = _OUTCOME_RE.match(path)
        if m:
            return self._tracked(lambda: self._route_outcome(m.group(1), m.group(2), m.group(3), m.group(4)))
        m = _RESOLVE_RE.match(path)
        if m:
            return self._tracked(lambda: self._route_resolve(m.group(1), m.group(2), m.group(3)))
        m = _POST_PREVIEW_RE.match(path)
        if m:
            return self._tracked(lambda: self._route_post_preview(m.group(1), m.group(2)))
        m = _POST_RE.match(path)
        if m:
            return self._tracked(lambda: self._route_post(m.group(1), m.group(2)))
        m = _PUBLISH_ONE_RE.match(path)
        if m:
            return self._tracked(lambda: self._route_publish_one(m.group(1), m.group(2)))
        m = _TRIAGE_RE.match(path)
        if m:
            return self._tracked(lambda: self._route_triage(m.group(1), m.group(2)))
        self._tracked(lambda: self._send_text(404, "not found"))

    def _route_health(self):
        if not self._check_host():
            return
        self._send_json(200, {"protocol": cw_store.PROTOCOL, "pid": os.getpid()})

    def _route_page(self, key, wid, qs):
        if not self._check_host() or not self._check_token_query(qs) or not self._check_origin():
            return
        d = self._resolve_dir(key, wid, json_errors=False)
        if d is None:
            return
        meta = cw_store.read_meta(d)
        if meta is None:
            self._send_text(404, "not found")
            return
        page_path = d / _page_name(meta)
        if not page_path.exists():
            self._send_text(503, "page not built yet, reload in a moment")
            return
        daemon = self.server.cw_daemon
        live = _live_payload(key, wid, meta, d, daemon.token, daemon.port)
        blob = json.dumps(live, ensure_ascii=True).replace("<", "\\u003c")
        html = page_path.read_text()
        if _MERMAID_PLACEHOLDER in html:
            html = html.replace(_MERMAID_PLACEHOLDER, _mermaid_payload())
        injected = html.replace("</head>", f"<script>window.CW_LIVE={blob};</script></head>", 1)
        self._send_html(200, injected)

    def _route_events(self, key, wid, qs):
        if not self._check_host() or not self._check_token_query(qs) or not self._check_origin():
            return
        d = self._resolve_dir(key, wid, json_errors=False)
        if d is None:
            return
        daemon = self.server.cw_daemon
        q = daemon.hub.register(key, wid)
        try:
            meta = cw_store.read_meta(d) or {}
            live = _live_payload(key, wid, meta, d, daemon.token, daemon.port)
            snapshot = _snapshot_payload(live)
            snapshot["turns"] = daemon.inflight(key, wid)

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()

            self._write_sse("snapshot", snapshot)
            last_beat = time.monotonic()
            while True:
                try:
                    event, data = q.get(timeout=1)
                    self._write_sse(event, data)
                except queue.Empty:
                    pass
                if time.monotonic() - last_beat >= daemon.beat_s:
                    self.wfile.write(b": beat\n\n")
                    self.wfile.flush()
                    last_beat = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            daemon.hub.unregister(key, wid, q)

    def _write_sse(self, event, data):
        payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        self.wfile.write(payload.encode())
        self.wfile.flush()

    def _route_notes(self, key, wid):
        if not self._check_host() or not self._check_token_header() or not self._check_origin():
            return
        raw, too_big = self._read_body()
        if too_big:
            return
        d = self._resolve_dir(key, wid, json_errors=True)
        if d is None:
            return
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            self._send_json(400, {"error": "bad json"})
            return
        with cw_store.dir_lock(d):
            self._write_page_notes(d, payload)

    def _write_page_notes(self, d, payload):
        incoming = payload.get("notes")
        if not isinstance(incoming, list):
            self._send_json(400, {"error": "notes must be a list"})
            return
        st = cw_store.read_json(d / "state.json", default={})
        posted_ids = {n.get("id") for n in (st or {}).get("notes", []) if n.get("state") in ("posted", "in_review")}
        kept = [
            n for n in incoming
            if isinstance(n, dict) and isinstance(n.get("id"), str) and VALID_ID_RE.fullmatch(n["id"])
            and n.get("origin") == "local" and n.get("state") == "draft"
            and n["id"] not in posted_ids
        ]
        cw_store.write_json(d / "page-notes.json", {"notes": kept})
        self._send_json(200, {"ok": True, "count": len(kept)})

    def _route_qa(self, key, wid):
        if not self._check_host() or not self._check_token_header() or not self._check_origin():
            return
        d = self._resolve_dir(key, wid, json_errors=True)
        if d is None:
            return
        self.server.cw_daemon.finalise_stale_turns(d)
        threads = cw_ask.read_threads(d)
        self._send_json(200, {
            "turns": cw_ask.read_qa(d),
            "resolved": [tid for tid, t in threads.items() if t["resolved"]],
            "outcomes": cw_ask.read_outcomes(d),
        })

    def _route_cancel(self, key, wid, qid):
        if not self._check_host() or not self._check_token_header() or not self._check_origin():
            return
        raw, too_big = self._read_body()
        if too_big:
            return
        if self._resolve_dir(key, wid, json_errors=True) is None:
            return
        if not self.server.cw_daemon.cancel(key, wid, qid):
            self._send_json(404, {"error": "turn not running"})
            return
        self._send_json(200, {"ok": True})

    def _route_show(self, key, wid, oid):
        if not self._check_host() or not self._check_token_header() or not self._check_origin():
            return
        raw, too_big = self._read_body()
        if too_big:
            return
        d = self._resolve_dir(key, wid, json_errors=True)
        if d is None:
            return
        o = next((o for o in cw_ask.read_outcomes(d) if o["oid"] == oid), None)
        if o is None:
            self._send_json(404, {"error": "outcome not found"})
            return
        task = (o.get("payload") or {}).get("task")
        task = task if isinstance(task, dict) else {}
        sha = task.get("sha")
        if o["outcome"] != "task" or o["state"] != "done" or not isinstance(sha, str) or not sha:
            self._send_json(409, {"error": "the task is not finished", "remedy": "wait for the task to finish"})
            return
        meta = cw_store.read_meta(d) or {}
        if meta.get("status") != "done" or meta.get("sibling_of"):
            self._send_json(409, {"error": "this walkthrough is not a finished PR view",
                                   "remedy": "open the PR walkthrough once it is done"})
            return
        task_id = re.sub(r"[^A-Za-z0-9._-]", "_", str(task.get("id") or oid))[:40]
        args = {
            "repo": meta["repo"], "base": meta["base"], "head": sha, "explain": meta.get("explain", False),
            "paths": meta.get("paths") or [], "title": meta.get("title"), "pr": None,
            "target": f"{meta['target']}+{task_id}", "slug": f"{meta['slug'][:39]}-{task_id}",
            "sibling_of": {"key": key, "id": wid, "oid": oid, "task_id": task_id,
                           "branch": task.get("branch"),
                           "title": (o.get("payload") or {}).get("title") or task.get("title"),
                           "parent_head": meta["head"]},
        }
        try:
            result = _tool_walkthrough_start(self.server.cw_daemon, args)
        except cw_store.CWError as e:
            self._send_json(409, {"error": str(e), "remedy": e.remedy})
            return
        self._send_json(200, {"ok": True, "id": result["id"], "key": result["key"],
                               "url": result["url"], "reused": result["reused"]})

    def _route_outcome(self, key, wid, oid, action):
        if action == "show":
            return self._route_show(key, wid, oid)
        if not self._check_host() or not self._check_token_header() or not self._check_origin():
            return
        raw, too_big = self._read_body()
        if too_big:
            return
        d = self._resolve_dir(key, wid, json_errors=True)
        if d is None:
            return
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            self._send_json(400, {"error": "bad json"})
            return
        payload = body.get("payload") if isinstance(body, dict) else None
        if action == "edit" and not isinstance(payload, dict):
            self._send_json(400, {"error": "payload must be an object", "remedy": "send {thread, why}"})
            return
        if action == "keep":
            payload = {"note_id": body.get("note_id") if isinstance(body, dict) else None}
        daemon = self.server.cw_daemon
        try:
            if action in ("run", "stop", "discard"):
                folded = daemon.task_action(key, wid, d, oid, action)
            else:
                folded = cw_ask.outcome_action(d, oid, action, payload,
                                               on_event=lambda ev, data: daemon.hub.emit(key, wid, ev, data))
        except cw_ask.OutcomeError as e:
            status = {"not_found": 404, "conflict": 409}.get(e.kind, 400)
            body = {"error": str(e)}
            if status == 400:
                body["remedy"] = e.remedy
            self._send_json(status, body)
            return
        self._send_json(200, {"ok": True, "outcome": folded})

    def _route_resolve(self, key, wid, tid):
        if not self._check_host() or not self._check_token_header() or not self._check_origin():
            return
        raw, too_big = self._read_body()
        if too_big:
            return
        d = self._resolve_dir(key, wid, json_errors=True)
        if d is None:
            return
        if not cw_ask.THREAD_RE.fullmatch(tid):
            self._send_json(400, {"error": "bad thread id"})
            return
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            self._send_json(400, {"error": "bad json"})
            return
        resolved = payload.get("resolved") if isinstance(payload, dict) else None
        if not isinstance(resolved, bool):
            self._send_json(400, {"error": "resolved must be a boolean"})
            return
        if tid not in cw_ask.read_threads(d):
            self._send_json(404, {"error": "thread not found"})
            return
        daemon = self.server.cw_daemon
        cw_ask.append_resolve(d, tid, resolved, on_event=lambda ev, data: daemon.hub.emit(key, wid, ev, data))
        self._send_json(200, {"ok": True})

    def _route_comment(self, key, wid, text_key):
        if not self._check_host() or not self._check_token_header() or not self._check_origin():
            return
        raw, too_big = self._read_body()
        if too_big:
            return
        d = self._resolve_dir(key, wid, json_errors=True)
        if d is None:
            return
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            self._send_json(400, {"error": "bad json"})
            return
        try:
            anchor = cw_ask.validate_anchor(payload.get("anchor"))
        except cw_store.CWError as e:
            self._send_json(400, {"error": str(e), "remedy": e.remedy})
            return
        if anchor["kind"] == "thread" and not any(
                n.get("id") == anchor["note_id"] and cw_ask.is_root_thread(n) for n in cw_ask._state_notes(d)):
            self._send_json(400, {"error": "bad anchor: not a GitHub review thread on this PR",
                                   "remedy": "reload the page and comment on a thread again"})
            return
        text = payload.get(text_key)
        if not isinstance(text, str) or not (1 <= len(text) <= cw_ask.QUESTION_MAX):
            self._send_json(400, {"error": f"{text_key} must be 1-2000 characters",
                                   "remedy": f"shorten the {text_key}"})
            return
        thread_id = payload.get("thread_id")
        if thread_id is not None:
            if not isinstance(thread_id, str) or not cw_ask.THREAD_RE.fullmatch(thread_id):
                self._send_json(400, {"error": "bad thread id"})
                return
            thread = cw_ask.read_threads(d).get(thread_id)
            if thread is None:
                self._send_json(404, {"error": "thread not found"})
                return
            live_turns = sum(1 for t in thread["turns"] if t.get("status") in ("ok", "pending"))
            if live_turns >= THREAD_TURN_CAP:
                self._send_json(409, {"error": f"thread has {THREAD_TURN_CAP} turns",
                                       "remedy": "start a new thread"})
                return
            anchor = thread["turns"][0].get("anchor") or anchor
        try:
            cw_ask.build_prompt(d, anchor, text, thread_id)
        except cw_store.CWError as e:
            self._send_json(400, {"error": str(e), "remedy": e.remedy})
            return
        if thread_id is not None and thread["resolved"]:
            cw_ask.append_resolve(d, thread_id, False,
                                  on_event=lambda ev, data: self.server.cw_daemon.hub.emit(key, wid, ev, data))
        qid = "q-" + secrets.token_hex(4)
        thread_id = thread_id or qid
        self.server.cw_daemon.start_ask(key, wid, d, qid, thread_id, anchor, text)
        self._send_json(202, {"qid": qid, "thread_id": thread_id})

    def _route_triage(self, key, wid):
        if not self._check_host() or not self._check_token_header() or not self._check_origin():
            return
        raw, too_big = self._read_body()
        if too_big:
            return
        d = self._resolve_dir(key, wid, json_errors=True)
        if d is None:
            return
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            self._send_json(400, {"error": "bad json"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "body must be an object"})
            return
        daemon = self.server.cw_daemon
        if daemon.is_running(d):
            self._send_json(409, {"error": STILL_BUILDING_MSG})
            return
        with cw_store.dir_lock(d):
            st = cw_store.read_json(d / "state.json")
            if not isinstance(st, dict):
                self._send_json(409, {"error": STILL_BUILDING_MSG})
                return
            if (st.get("meta") or {}).get("pr") is None:
                self._send_json(409, {"error": NO_PR_MSG})
                return
            triaged = set()
            for t in cw_ask.read_threads(d).values():
                a = t["turns"][0].get("anchor") or {}
                if a.get("kind") == "thread" and (
                        t["turns"][0].get("source") == "triage"
                        or any(x.get("status") == "pending" for x in t["turns"])):
                    triaged.add(a.get("note_id"))
            roots = [n for n in cw_ask._state_notes(d) if cw_ask.is_root_thread(n) and not n.get("resolved")]
            skipped = sum(1 for n in roots if n["id"] in triaged)
            todo = [n for n in roots if n["id"] not in triaged]
            started = []
            for n in todo[:TRIAGE_CAP]:
                qid = "q-" + secrets.token_hex(4)
                anchor = {"kind": "thread", "note_id": n["id"], "quote": (n.get("body") or "")[:200]}
                daemon.start_ask(key, wid, d, qid, qid, anchor, TRIAGE_COMMENT, source="triage")
                started.append({"note_id": n["id"], "qid": qid, "thread_id": qid})
        self._send_json(200, {"started": len(started), "skipped": skipped,
                               "remaining": len(todo) - len(started), "threads": started})

    def _post_check_meta(self, d):
        """Guard 1, shared by /post/preview and /post: posting only ever makes sense from a
        built, final page -- daemon.is_running(d) already turned away a page still building,
        this turns away a partial preview page, which has no links/pr wiring yet."""
        meta = cw_store.read_meta(d)
        if meta is None:
            return None, {"error": STILL_BUILDING_MSG}
        if meta.get("status") in ("failed", "interrupted"):
            return None, {"error": FAILED_MSG, "remedy": "re-run /code-walkthrough"}
        if meta.get("status") != "done" or meta.get("page") != "final":
            return None, {"error": STILL_BUILDING_MSG}
        if meta.get("sibling_of"):
            return None, SIBLING_NO_PUBLISH
        return meta, None

    def _post_check_state(self, d):
        """Guard 2: today's exact refusal strings (canGhCommand, assets/diff-review-template.html)
        for the two cases a page post can never succeed in -- no PR on this comparison, or a
        head GitHub cannot see yet."""
        st = cw_store.read_json(d / "state.json")
        if not isinstance(st, dict):
            return None, {"error": STILL_BUILDING_MSG}
        st_meta = st.get("meta") or {}
        if st_meta.get("pr") is None:
            return None, {"error": NO_PR_MSG}
        if not st_meta.get("head_pushed"):
            return None, {"error": HEAD_NOT_PUSHED_MSG}
        return st, None

    @staticmethod
    def _fold_page_notes(d, st):
        page_notes_path = d / "page-notes.json"
        if not page_notes_path.exists():
            return st
        result = cw_run._notes(cw_store.read_meta(d), [
            "import", "--state", str(d / "state.json"), "--file", str(page_notes_path),
            "--replace-local-drafts",
        ])
        if result.returncode != 0:
            raise cw_store.CWError("notes.py import failed: " + result.stderr.strip()[-500:])
        return cw_store.read_json(d / "state.json")

    def _post_items(self, d, st, resolve_ids, ids=None):
        """Steps 3-4 shared by preview and post: fold page-notes.json into state.json first
        (an edit made just before clicking Post must count), then list exactly what
        notes.pending_publish_ids calls ready -- the same in-process predicate /post
        recomputes and compares against the nonce -- and which requested root ids still
        point at an unresolved thread."""
        st = self._fold_page_notes(d, st)
        by_id = {n["id"]: n for n in st.get("notes", [])}
        notes_items = []
        for note_id in notes.pending_publish_ids(st):
            note = by_id[note_id]
            body = note.get("body") or ""
            if not body.strip() or (ids is not None and note_id not in ids):
                continue
            notes_items.append({
                "id": note_id, "path": note.get("path"), "line": note.get("line"),
                "side": note.get("side"), "body": body,
                "reply_to": note.get("reply_to") or note.get("in_reply_to"),
            })
        resolves_items = []
        for root_id in resolve_ids or []:
            note = by_id.get(root_id)
            if note is None:
                continue
            thread_id = note.get("gh_thread_id")
            if not thread_id or note.get("resolved"):
                continue
            resolves_items.append({
                "root_id": root_id, "thread_id": thread_id, "path": note.get("path"),
                "line": note.get("line"), "body": note.get("body"),
            })
        return st, notes_items, resolves_items

    def _post_nonce(self, notes_items, resolves_items):
        """A change under us between preview and post -- a draft body edited, a resolve added
        or removed -- must invalidate the preview rather than silently posting something the
        user never actually saw; a hash over exactly what preview returned catches either."""
        payload = {
            "notes": [[n["id"], n["body"]] for n in notes_items],
            "resolve": sorted(r["thread_id"] for r in resolves_items),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _route_post_preview(self, key, wid):
        if not self._check_host() or not self._check_token_header() or not self._check_origin():
            return
        raw, too_big = self._read_body()
        if too_big:
            return
        d = self._resolve_dir(key, wid, json_errors=True)
        if d is None:
            return
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            self._send_json(400, {"error": "bad json"})
            return
        if self.server.cw_daemon.is_running(d):
            self._send_json(409, {"error": STILL_BUILDING_MSG})
            return
        with cw_store.dir_lock(d):
            meta, err = self._post_check_meta(d)
            if err:
                self._send_json(409, err)
                return
            st, err = self._post_check_state(d)
            if err:
                self._send_json(409, err)
                return
            ids = self._post_ids(payload)
            if ids is False:
                self._send_json(400, {"error": "ids must be a list of strings"})
                return
            resolve_ids = self._post_resolve(payload)
            if resolve_ids is False:
                self._send_json(400, {"error": "resolve must be a list of strings"})
                return
            st = self._post_sync(d, meta, st)
            st, reset = self._reset_stuck(d, meta, st)
            waiting = self._in_review_ids(st)
            try:
                st, notes_items, resolves_items = self._post_items(d, st, resolve_ids, ids)
            except cw_store.CWError as e:
                self._send_json(500, {"error": str(e)})
                return
            parent_err = self._post_reply_parent_error(st, ids)
            if parent_err:
                self._send_json(400, parent_err)
                return
            nonce = self._post_nonce(notes_items, resolves_items)
            info = self._review_info(meta, d) or {}
            by_id = {n["id"]: n for n in st.get("notes", [])}
            in_review = [{"id": i, "path": by_id[i].get("path"), "line": by_id[i].get("line"),
                          "body": by_id[i].get("body")} for i in waiting if i in by_id]
            own_pr = self._own_pr(meta)
        self._send_json(200, {
            "notes": notes_items, "resolves": resolves_items, "nonce": nonce,
            "review": {"pending": bool(info.get("review_id")), "comments": info.get("comments", 0)},
            "in_review": in_review, "own_pr": own_pr, "reset": reset,
        })

    @staticmethod
    def _post_ids(payload):
        """None when the caller sent no `ids` (every ready draft), the id set when it did, False
        when `ids` is malformed."""
        ids = payload.get("ids")
        if ids is None:
            return None
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            return False
        return set(ids)

    @staticmethod
    def _post_resolve(payload):
        """The `resolve` list, [] when absent, False when it is not a list of strings."""
        resolve = payload.get("resolve")
        if resolve is None:
            return []
        if not isinstance(resolve, list) or not all(isinstance(i, str) for i in resolve):
            return False
        return resolve

    @staticmethod
    def _post_reply_parent_error(st, ids):
        """With `ids`, a selected reply draft whose parent is itself a local draft needs that
        parent selected too: the reply can only be delivered into the parent's thread."""
        if ids is None:
            return None
        by_id = {n["id"]: n for n in st.get("notes", [])}
        for note_id in ids:
            note = by_id.get(note_id)
            parent = by_id.get((note or {}).get("reply_to") or "")
            if (note and note.get("state") == "draft" and parent is not None
                    and parent.get("origin") == "local" and parent.get("state") == "draft"
                    and parent["id"] not in ids):
                return {"error": f"{note_id} replies to the draft {parent['id']}, which is not selected",
                        "remedy": "a reply needs its parent comment: tick it too"}
        return None

    def _reset_stuck(self, d, meta, st):
        """Notes in_review whose pending review no longer exists on GitHub (submitted or
        discarded in the web UI) go back to draft so they can be delivered again. Runs after
        sync, whose dedupe already turned the ones submitted elsewhere into posted. A failed
        review-info lookup resets nothing."""
        if not self._in_review_ids(st):
            return st, []
        info = self._review_info(meta, d)
        if info is None or info.get("review_id"):
            return st, []
        reset = notes.reset_in_review(st)
        cw_store.write_json(d / "state.json", st)
        return st, reset

    @staticmethod
    def _in_review_ids(st):
        return [n["id"] for n in st.get("notes", []) if n.get("state") == "in_review"]

    def _post_sync(self, d, meta, st):
        if meta.get("pr") and meta.get("gh_repo"):
            _runner().sync_pr(d, meta)
            refreshed = cw_store.read_json(d / "state.json")
            if isinstance(refreshed, dict):
                return refreshed
        return st

    def _review_info(self, meta, d):
        result = cw_run._notes(meta, ["review-info", "--state", str(d / "state.json")])
        if result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout)
        except ValueError:
            return None

    def _own_pr(self, meta):
        """True/False when the PR author is / is not the gh viewer; None when either lookup fails."""
        try:
            viewer = cw_run._gh(meta, ["api", "user"])
            author = cw_run._gh(meta, ["pr", "view", str(meta["pr"]), "--repo", meta["gh_repo"],
                                        "--json", "author"])
            if viewer.returncode != 0 or author.returncode != 0:
                return None
            login = json.loads(viewer.stdout).get("login")
            pr_author = (json.loads(author.stdout).get("author") or {}).get("login")
        except (cw_store.CWError, ValueError, AttributeError, OSError):
            return None
        return None if not login or not pr_author else login == pr_author

    def _route_post(self, key, wid):
        if not self._check_host() or not self._check_token_header() or not self._check_origin():
            return
        raw, too_big = self._read_body()
        if too_big:
            return
        d = self._resolve_dir(key, wid, json_errors=True)
        if d is None:
            return
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            self._send_json(400, {"error": "bad json"})
            return
        event = payload.get("event") or "COMMENT"
        review_body = payload.get("body") or ""
        ids = self._post_ids(payload)
        if event not in ("COMMENT", "APPROVE", "REQUEST_CHANGES"):
            self._send_json(400, {"error": "event must be COMMENT, APPROVE or REQUEST_CHANGES"})
            return
        if not isinstance(review_body, str):
            self._send_json(400, {"error": "body must be a string"})
            return
        if event == "REQUEST_CHANGES" and not review_body.strip():
            self._send_json(400, {"error": "Request changes needs a review body"})
            return
        if ids is False:
            self._send_json(400, {"error": "ids must be a list of strings"})
            return
        resolve_ids = self._post_resolve(payload)
        if resolve_ids is False:
            self._send_json(400, {"error": "resolve must be a list of strings"})
            return
        submit_only = payload.get("submit_only") is True
        daemon = self.server.cw_daemon
        if daemon.is_running(d):
            self._send_json(409, {"error": STILL_BUILDING_MSG})
            return
        with cw_store.dir_lock(d):
            meta, err = self._post_check_meta(d)
            if err:
                self._send_json(409, err)
                return
            st, err = self._post_check_state(d)
            if err:
                self._send_json(409, err)
                return
            if not submit_only:
                st = self._post_sync(d, meta, st)
            st, reset = self._reset_stuck(d, meta, st)
            waiting = set(self._in_review_ids(st))
            try:
                st, notes_items, resolves_items = self._post_items(d, st, resolve_ids, ids)
            except cw_store.CWError as e:
                self._send_json(500, {"error": str(e)})
                return
            parent_err = self._post_reply_parent_error(st, ids)
            if parent_err:
                self._send_json(400, parent_err)
                return
            if submit_only:
                notes_items = []
            if not notes_items and not waiting and (event != "COMMENT" or review_body.strip()):
                if not (self._review_info(meta, d) or {}).get("review_id"):
                    self._send_json(400, {
                        "error": "nothing to submit: there are no comments in the review",
                        "remedy": "approve or request changes on GitHub, or add a comment first"})
                    return
            if not submit_only:
                nonce = self._post_nonce(notes_items, resolves_items)
                if nonce != payload.get("nonce"):
                    self._send_json(409, {"error": "changed since preview", "remedy": "review the list again"})
                    return

            results = []
            state_path = str(d / "state.json")

            def record(entry):
                results.append(entry)
                daemon.hub.emit(key, wid, "posted", entry)

            def err_of(result):
                return None if result.returncode == 0 else result.stderr.strip()[-500:]

            for item in notes_items:
                result = cw_run._notes(meta, ["deliver", "--state", state_path, "--id", item["id"]])
                ok = result.returncode == 0
                record({"kind": "note", "id": item["id"], "ok": ok,
                        "state": "in_review" if ok else None, "error": err_of(result)})

            delivered_ids = {r["id"] for r in results if r["ok"]}
            failed = [{"id": r["id"], "error": r["error"]} for r in results if not r["ok"]]
            if not submit_only and delivered_ids:
                self._drop_from_page_notes(d, delivered_ids)

            def partial(failures):
                total = len(self._in_review_ids(cw_store.read_json(d / "state.json") or {}))
                body = {"ok": False, "partial": True, "results": results,
                        "delivered": len(delivered_ids), "failed": failures,
                        "in_review": total, "reset": reset}
                meta2, render_error = self._render_page(d, meta, key, wid)
                if render_error:
                    body["render_error"] = render_error
                else:
                    daemon.hub.emit(key, wid, "rebuilt",
                                    {"rev": meta2["rev"], "page": "final", "fragments": []})
                self._send_json(200, body)

            if failed:
                partial(failed)
                return

            submitted = False
            if not (submit_only or delivered_ids) and waiting:
                has_review = bool((self._review_info(meta, d) or {}).get("review_id"))
            else:
                has_review = True
            if (submit_only or delivered_ids or waiting) and has_review:
                body_path = d / "review-body.txt"
                body_path.write_text(review_body)
                result = cw_run._notes(meta, ["submit", "--state", state_path, "--event", event,
                                              "--body-file", str(body_path)])
                record({"kind": "submit", "id": None, "ok": result.returncode == 0,
                        "event": event, "error": err_of(result)})
                if result.returncode != 0:
                    partial([{"id": "submit", "error": err_of(result)}])
                    return
                submitted = True

            for item in resolves_items:
                result = cw_run._notes(meta, ["resolve", "--state", state_path,
                                              "--thread-id", item["thread_id"]])
                record({"kind": "resolve", "id": item["thread_id"], "ok": result.returncode == 0,
                        "error": err_of(result)})

            meta2, render_error = self._render_page(d, meta, key, wid)
            if render_error:
                self._send_json(200, {"ok": False, "results": results, "render_error": render_error})
                return

        daemon.hub.emit(key, wid, "rebuilt", {"rev": meta2["rev"], "page": "final", "fragments": []})
        self._send_json(200, {"ok": True, "results": results, "submitted": submitted, "reset": reset})

    def _route_publish_one(self, key, wid):
        if not self._check_host() or not self._check_token_header() or not self._check_origin():
            return
        raw, too_big = self._read_body()
        if too_big:
            return
        d = self._resolve_dir(key, wid, json_errors=True)
        if d is None:
            return
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            self._send_json(400, {"error": "bad json"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "body must be an object"})
            return
        note_id, resolve_id, oid = payload.get("id"), payload.get("resolve"), payload.get("oid")
        body_sha = payload.get("body_sha")
        if (note_id is None) == (resolve_id is None) or not isinstance(note_id or resolve_id, str):
            self._send_json(400, {"error": "send exactly one of id or resolve, as a string"})
            return
        if note_id is not None and not isinstance(body_sha, str):
            self._send_json(400, {"error": "body_sha is required"})
            return
        if oid is not None and not (isinstance(oid, str) and _OID_RE.fullmatch(oid)):
            self._send_json(400, {"error": "bad oid"})
            return
        daemon = self.server.cw_daemon
        if daemon.is_running(d):
            self._send_json(409, {"error": STILL_BUILDING_MSG})
            return
        emit = lambda ev, data: daemon.hub.emit(key, wid, ev, data)  # noqa: E731
        with cw_store.dir_lock(d):
            meta, err = self._post_check_meta(d)
            if err:
                self._send_json(409, err)
                return
            st, err = self._post_check_state(d)
            if err:
                self._send_json(409, err)
                return
            st = self._post_sync(d, meta, st)
            st, _reset = self._reset_stuck(d, meta, st)
            try:
                st = self._fold_page_notes(d, st)
            except cw_store.CWError as e:
                self._send_json(500, {"error": str(e)})
                return
            by_id = {n["id"]: n for n in st.get("notes", [])}
            target_id = note_id if note_id is not None else resolve_id
            note = by_id.get(target_id)
            if note is None:
                self._send_json(404, {"error": "note not found"})
                return
            outcome = None
            if oid is not None:
                outcome = next((o for o in cw_ask.read_outcomes(d) if o["oid"] == oid), None)
                if outcome is None:
                    self._send_json(404, {"error": "outcome not found"})
                    return
                if note_id is not None:
                    tied = (outcome["outcome"] == "github_draft" and outcome["state"] == "kept"
                            and (outcome["payload"] or {}).get("note_id") == note_id)
                else:
                    tied = (outcome["outcome"] == "resolve" and outcome["state"] == "proposed"
                            and (outcome["payload"] or {}).get("thread") == resolve_id)
                if not tied:
                    self._send_json(409, {"error": "that suggestion is no longer open",
                                           "remedy": "reload the page"})
                    return
            if note_id is None:
                done = self._publish_resolve(d, meta, note, resolve_id)
            else:
                done = self._publish_note(d, meta, by_id, note, body_sha)
            if done.get("status"):
                self._send_json(done.pop("status"), done)
                return
            if outcome is not None:
                cw_ask.mark_outcome(d, oid, "done" if note_id is None else "published", on_event=emit)
            if note_id is not None:
                self._drop_from_page_notes(d, {note_id})
                emit("posted", {"kind": "note", "id": note_id, "ok": True, "state": "posted"})
            else:
                emit("posted", {"kind": "resolve", "id": note.get("gh_thread_id"), "ok": True})
            meta2, render_error = self._render_page(d, meta, key, wid)
        if render_error:
            done["render_error"] = render_error
        else:
            emit("rebuilt", {"rev": meta2["rev"], "page": "final", "fragments": []})
        self._send_json(200, {"ok": True, **done})

    def _publish_resolve(self, d, meta, note, resolve_id):
        if not cw_ask.is_root_thread(note):
            return {"status": 409, "error": "that is not a GitHub review thread"}
        if note.get("resolved"):
            return {}
        result = cw_run._notes(meta, ["resolve", "--state", str(d / "state.json"),
                                      "--thread-id", note["gh_thread_id"]])
        if result.returncode != 0:
            return {"status": 502, "error": result.stderr.strip()[-500:] or "gh failed",
                    "remedy": "check gh auth status, then try again"}
        return {}

    def _publish_note(self, d, meta, by_id, note, body_sha):
        if note.get("state") != "draft" or note.get("origin") != "local":
            return {"status": 409, "error": "only a local draft can be published",
                    "remedy": "reload the page"}
        body = note.get("body") or ""
        if hashlib.sha256(body.encode()).hexdigest() != body_sha:
            return {"status": 409, "error": "changed since you looked",
                    "remedy": "check the text and publish again"}
        if not body.strip():
            return {"status": 400, "error": "the draft is empty"}
        if note.get("reply_to") or note.get("in_reply_to"):
            root = notes.thread_root(by_id, note)
            if root is None or root.get("gh_id") is None or root.get("state") != "posted":
                return {"status": 409, "error": "the comment this replies to is not on GitHub yet",
                        "remedy": "publish the parent first"}
        elif note.get("stale"):
            return {"status": 409, "error": "this draft's line no longer matches the diff",
                    "remedy": "reply in a thread, or comment on a current line"}
        info = self._review_info(meta, d)
        if info is None:
            return {"status": 502, "error": "could not check for a pending review", "remedy": "try again"}
        if info.get("review_id"):
            return {"status": 409, "error": "you have a pending review on this PR",
                    "remedy": "Submit review, or finish it on GitHub"}
        result = cw_run._notes(meta, ["publish", "--state", str(d / "state.json"), "--id", note["id"]])
        if result.returncode != 0:
            error = result.stderr.strip()[-500:] or "gh failed"
            # A lost response can leave the comment on GitHub with the draft still local: sync's
            # dedupe turns such a draft into a posted note.
            self._post_sync(d, meta, cw_store.read_json(d / "state.json"))
            synced = next((n for n in (cw_store.read_json(d / "state.json") or {}).get("notes", [])
                           if n["id"] == note["id"]), {})
            if synced.get("state") == "posted":
                return {"id": note["id"], "gh_url": synced.get("gh_url")}
            unclear = "timed out" in error or ("gh api failed" in error and not re.search(r"\b4\d\d\b", error))
            if unclear:
                error = "the comment may already be on GitHub; check the PR before retrying"
            return {"status": 502, "error": error, "remedy": "check gh auth status, then try again"}
        try:
            info = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            info = {}
        return {"id": note["id"], "gh_url": info.get("gh_url")}

    def _render_page(self, d, meta, key, wid):
        """Re-render the final page and bump rev. (meta2, None) on success; (None, stderr tail)
        after marking the walkthrough failed when the render gate rejects."""
        render_args = ["render", "--dir", str(d), "--slug", meta["slug"]]
        if meta.get("title"):
            render_args += ["--title", meta["title"]]
        render_result = subprocess.run(
            [sys.executable, str(cw_run.SCRIPTS_DIR / "pipeline.py"), *render_args],
            capture_output=True, text=True)
        if render_result.returncode != 0:
            gate_lines = [line for line in render_result.stderr.splitlines() if line.strip()]
            cw_store.update_meta(d, lambda m: m.update({"status": "failed", "gate": gate_lines}))
            self.server.cw_daemon.hub.emit(key, wid, "step",
                                           {"name": "render", "status": "failed", "error": gate_lines})
            return None, render_result.stderr.strip()[-500:]
        return cw_store.update_meta(d, lambda m: m.update(
            {"page": "final", "rev": m.get("rev", 0) + 1})), None

    @staticmethod
    def _drop_from_page_notes(d, delivered_ids):
        page_notes = cw_store.read_json(d / "page-notes.json")
        if isinstance(page_notes, dict):
            remaining = [n for n in page_notes.get("notes", []) if n.get("id") not in delivered_ids]
            cw_store.write_json(d / "page-notes.json", {"notes": remaining})

    def _route_rpc(self):
        if not self._check_host() or not self._check_token_header() or not self._check_origin():
            return
        raw, too_big = self._read_body()
        if too_big:
            return
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            self._send_json(400, {"error": "bad json"})
            return
        tool = payload.get("tool")
        args = payload.get("args") or {}
        handler = _RPC_TOOLS.get(tool)
        if handler is None:
            self._send_json(200, {"ok": False, "error": f"unknown tool {tool}", "remedy": None})
            return
        try:
            result = handler(self.server.cw_daemon, args)
            self._send_json(200, {"ok": True, "result": result})
        except cw_store.CWError as e:
            self._send_json(200, {"ok": False, "error": str(e), "remedy": e.remedy})

    def _route_shutdown(self):
        if not self._check_host() or not self._check_token_header() or not self._check_origin():
            return
        self._send_json(200, {"ok": True})
        threading.Thread(target=self.server.cw_daemon.stop, daemon=True).start()


class Daemon:
    """In-process server. `serve()` is the CLI wrapper tests do not need to go through."""

    def __init__(self, idle_s=None, write_server_json=True):
        self.idle_s = idle_s
        self.write_server_json = write_server_json
        self.beat_s = float(os.environ.get("CW_BEAT_S", 15))
        self.token = cw_store.load_or_create_token()
        self.hub = _Hub()
        self.port = None

        self._server = None
        self._serve_thread = None
        self._watchdog = None
        self._stopped = threading.Event()

        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self._run_threads = set()
        self._run_threads_lock = threading.Lock()
        self._idle_since = None

        self._asks = 0
        self._ask_locks = {}
        self._ask_locks_guard = threading.Lock()
        self._qids = set()
        self._qids_lock = threading.Lock()
        self._turns = {}

    def start(self):
        old = cw_store.read_json(cw_store.server_json_path())
        prev_port = old.get("port") if isinstance(old, dict) else None
        server = None
        if prev_port:
            try:
                server = ThreadingHTTPServer((HOST, prev_port), _Handler)
            except OSError:
                server = None
        if server is None:
            server = ThreadingHTTPServer((HOST, 0), _Handler)
        server.daemon_threads = True
        server.cw_daemon = self
        self._server = server
        self.port = server.server_port

        if self.write_server_json:
            cw_store.write_json(
                cw_store.server_json_path(),
                {"pid": os.getpid(), "port": self.port, "token": self.token,
                 "protocol": cw_store.PROTOCOL, "started_at": cw_store.now_iso()},
                mode=0o600,
            )

        self._serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._serve_thread.start()

        if self.idle_s is not None:
            self._watchdog = threading.Thread(target=self._watch_idle, daemon=True)
            self._watchdog.start()
        return self

    def wait_stopped(self):
        self._stopped.wait()

    def stop(self):
        if self._stopped.is_set():
            return
        self._stopped.set()
        with self._qids_lock:
            lives = [t["live"] for t in self._turns.values()]
        for live in lives:
            live["cancelled"] = True
        procs = [live.get("proc") for live in lives]
        for proc in procs:
            if proc is not None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        if self.write_server_json:
            info = cw_store.read_json(cw_store.server_json_path())
            if isinstance(info, dict) and info.get("pid") == os.getpid():
                try:
                    cw_store.server_json_path().unlink()
                except FileNotFoundError:
                    pass
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def enter_request(self):
        with self._inflight_lock:
            self._inflight += 1
        self._idle_since = None

    def exit_request(self):
        with self._inflight_lock:
            self._inflight -= 1

    def is_running(self, d):
        with self._run_threads_lock:
            return str(d) in self._run_threads

    def start_run(self, key, wid, d):
        with self._run_threads_lock:
            self._run_threads.add(str(d))
        self._idle_since = None

        def _target():
            try:
                def on_event(event, data):
                    self.hub.emit(key, wid, event, data)
                    self._idle_since = None
                _runner().run(d, on_event)
            except Exception as e:
                remedy = getattr(e, "remedy", None)
                cw_store.update_meta(d, lambda m: m.update(status="failed", error=str(e), remedy=remedy))
                self.hub.emit(key, wid, "step", {"name": "run", "status": "failed", "error": str(e),
                                                  "remedy": remedy, "batches": None})
            finally:
                with self._run_threads_lock:
                    self._run_threads.discard(str(d))

        threading.Thread(target=_target, daemon=True).start()

    def ask_lock(self, d, thread_id):
        """Per-(walkthrough dir, thread) lock: turns of one thread queue behind each other,
        different threads run side by side."""
        key = (str(d), thread_id)
        with self._ask_locks_guard:
            return self._ask_locks.setdefault(key, threading.Lock())

    def finalise_stale_turns(self, d):
        with self._qids_lock:
            cw_ask.finalise_stale(d, self._qids)

    def inflight(self, key, wid):
        """Buffers of the running turns of one walkthrough, for the SSE snapshot."""
        with self._qids_lock:
            turns = [(qid, t) for qid, t in self._turns.items() if t["key"] == key and t["wid"] == wid]
        out = []
        for qid, t in turns:
            live = t["live"]
            with live["lock"]:
                seq, text, progress = live["seq"], live["text"], live["progress"]
            out.append({"qid": qid, "thread_id": t["thread_id"], "seq": seq, "text": text, "progress": progress})
        return out

    def cancel(self, key, wid, qid):
        with self._qids_lock:
            t = self._turns.get(qid)
            if t is None or t["key"] != key or t["wid"] != wid:
                return False
            live = t["live"]
            live["cancelled"] = True
            proc = live.get("proc")
        if proc is not None:
            def _kill(sig):
                try:
                    os.killpg(proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            _kill(signal.SIGTERM)

            def _escalate():
                if proc.poll() is None:
                    _kill(signal.SIGKILL)
            timer = threading.Timer(3, _escalate)
            timer.daemon = True
            timer.start()
        return True

    def start_ask(self, key, wid, d, qid, thread_id, anchor, question, source="user"):
        with self._inflight_lock:
            self._asks += 1
        live = {"text": "", "seq": 0, "progress": None, "proc": None, "cancelled": False,
                "lock": threading.Lock()}
        with self._qids_lock:
            self._qids.add(qid)
            self._turns[qid] = {"live": live, "key": key, "wid": wid, "thread_id": thread_id}
        self._idle_since = None
        emit = lambda ev, data: self.hub.emit(key, wid, ev, data)  # noqa: E731
        try:
            cw_ask.begin_turn(d, qid, anchor, question, thread_id, on_event=emit, source=source)
        except BaseException:
            with self._inflight_lock:
                self._asks -= 1
            with self._qids_lock:
                self._qids.discard(qid)
                self._turns.pop(qid, None)
            raise

        def _target():
            try:
                with self.ask_lock(d, thread_id):
                    if live["cancelled"] or self._stopped.is_set():
                        now = cw_store.now_iso()
                        record = {"qid": qid, "thread_id": thread_id, "started_at": now, "finished_at": now,
                                  "anchor": anchor, "comment": question, "question": question,
                                  "source": source, "status": "cancelled", "answer": "", "error": None, "remedy": None,
                                  "profile": None, "model": None, "session_id": None,
                                  "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": None}}
                        cw_ask._append_qa(d, record)
                        emit("thread", {"kind": "turn", "record": record})
                        return
                    cw_ask.answer(d, qid, anchor, question, on_event=emit, thread_id=thread_id, live=live, source=source)
            finally:
                with self._inflight_lock:
                    self._asks -= 1
                with self._qids_lock:
                    self._qids.discard(qid)
                    self._turns.pop(qid, None)

        threading.Thread(target=_target, daemon=True).start()

    def task_action(self, key, wid, d, oid, action):
        emit = lambda ev, data: self.hub.emit(key, wid, ev, data)  # noqa: E731
        meta = cw_store.read_meta(d) or {}
        if action == "discard":
            return cw_task.discard(d, meta, oid, emit)
        if action == "stop":
            current = cw_task._task_outcome(d, oid, "stop")
            qid = ((current["payload"] or {}).get("task") or {}).get("qid")
            if current["state"] != "running" or not self.cancel(key, wid, qid):
                raise cw_ask.OutcomeError("task is not running", "conflict")
            return current
        return self.start_task(key, wid, d, meta, oid, emit)

    def start_task(self, key, wid, d, meta, oid, emit):
        current, profile = cw_task.check_runnable(d, oid)
        config = cw_store.load_config()
        qid, t = "tq-" + secrets.token_hex(4), "t-" + secrets.token_hex(3)
        live = {"text": "", "seq": 0, "progress": None, "proc": None, "cancelled": False,
                "lock": threading.Lock()}
        with self._inflight_lock:
            self._asks += 1
        with self._qids_lock:
            self._qids.add(qid)
            self._turns[qid] = {"live": live, "key": key, "wid": wid, "thread_id": current["thread_id"]}
        self._idle_since = None

        def release():
            with self._inflight_lock:
                self._asks -= 1
            with self._qids_lock:
                self._qids.discard(qid)
                self._turns.pop(qid, None)

        try:
            folded = cw_task.begin(d, meta, current, t, qid, emit)
        except BaseException:
            release()
            raise

        def _target():
            try:
                cw_task.run_turn(d, meta, current, folded["payload"]["task"], live, emit, config, profile)
            finally:
                release()

        threading.Thread(target=_target, daemon=True).start()
        return folded

    def _is_idle_now(self):
        with self._inflight_lock:
            inflight = self._inflight
            asks = self._asks
        with self._run_threads_lock:
            running = len(self._run_threads)
        return inflight == 0 and asks == 0 and running == 0 and self.hub.client_count() == 0

    def _watch_idle(self):
        while not self._stopped.is_set():
            time.sleep(1)
            if not self._is_idle_now():
                self._idle_since = None
                continue
            if self._idle_since is None:
                self._idle_since = time.monotonic()
            elif time.monotonic() - self._idle_since >= self.idle_s:
                self.stop()
                return


def _iter_walkthrough_dirs():
    root = cw_store.store_root()
    if not root.exists():
        return
    for key_dir in root.iterdir():
        if not key_dir.is_dir():
            continue
        for wid_dir in key_dir.iterdir():
            if wid_dir.is_dir():
                yield wid_dir


def _mark_interrupted():
    for d in _iter_walkthrough_dirs():
        meta = cw_store.read_meta(d)
        if meta and meta.get("status") == "building":
            cw_store.update_meta(d, lambda m: m.update(status="interrupted"))
        if meta:
            cw_ask.fail_interrupted_tasks(d, set())


def _has_local_drafts(d):
    page_notes = cw_store.read_json(d / "page-notes.json")
    if page_notes and page_notes.get("notes"):
        return True
    state = cw_store.read_json(d / "state.json")
    if state and any(n.get("origin") == "local" and n.get("state") == "draft"
                      for n in (state.get("notes") or [])):
        return True
    return False


def _remove_walkthrough(meta, d):
    repo = meta.get("repo")
    head = d / "head"
    if repo and head.exists():
        result = subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", str(head)],
                                 capture_output=True, text=True)
        if result.returncode != 0:
            shutil.rmtree(head, ignore_errors=True)
            subprocess.run(["git", "-C", repo, "worktree", "prune"], capture_output=True, text=True)
    cw_task.remove_all(d, meta)
    shutil.rmtree(d, ignore_errors=True)
    for sd in list(_iter_walkthrough_dirs()):
        smeta = cw_store.read_meta(sd)
        sib = (smeta or {}).get("sibling_of") or {}
        if sib.get("key") == meta.get("key") and sib.get("id") == meta.get("id") and sd != d:
            _remove_walkthrough(smeta, sd)


def _prune():
    days = int(os.environ.get("CW_PRUNE_DAYS", 30))
    cutoff = time.time() - days * 86400
    for d in list(_iter_walkthrough_dirs()):
        meta = cw_store.read_meta(d)
        if meta is None or _has_local_drafts(d):
            continue
        try:
            ts = datetime.strptime(meta.get("updated_at", ""), "%Y-%m-%dT%H:%M:%SZ") \
                .replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if ts >= cutoff:
            continue
        _remove_walkthrough(meta, d)
    root = cw_store.store_root()
    if root.exists():
        for key_dir in root.iterdir():
            if key_dir.is_dir() and not any(key_dir.iterdir()):
                key_dir.rmdir()


def serve():
    lock = cw_store.acquire_daemon_lock()
    if lock is None:
        return 0
    try:
        config = cw_store.load_config()
        cw_llm.configure(config.get("max_concurrency", 4))
        _mark_interrupted()
        _prune()
        idle_s = int(os.environ.get("CW_IDLE_S", 2700))
        daemon = Daemon(idle_s=idle_s, write_server_json=True)
        daemon.start()
        daemon.wait_stopped()
    finally:
        lock.close()
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "serve":
        return serve()
    print("usage: cw_server.py serve", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
