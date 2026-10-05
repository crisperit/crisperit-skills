#!/usr/bin/env python3
"""The daemon: a localhost-only HTTP server that serves the live walkthrough page, streams
run events over SSE, accepts page-note PUTs, and answers the three RPC tools cw_mcp.py
exposes over MCP. One daemon per machine, arbitrated by cw_store.acquire_daemon_lock().

Usage: cw_server.py serve

Stdlib only. Binds 127.0.0.1 only; see references/live.md for the localhost guard.
"""

import hmac
import json
import os
import queue
import re
import secrets
import shutil
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
import cw_store  # noqa: E402
from notes import VALID_ID_RE  # noqa: E402  same id check a page write is subject to

HOST = "127.0.0.1"
MAX_BODY = 1_000_000

START_LOCK = threading.Lock()

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
    return {
        "v": 1, "key": key, "id": wid, "token": token,
        "api": f"/api/walkthrough/{key}/{wid}",
        "rev": meta.get("rev", 0), "page": meta.get("page"),
        "status": meta.get("status"), "error": meta.get("error"), "remedy": meta.get("remedy"),
        "steps": _steps_public(meta),
        "batches": {"done": len(meta.get("batches_done") or []), "total": meta.get("batches", 0)},
        "usage": meta.get("usage", {}), "total": cw_store.usage_total(meta),
        "drafts": drafts,
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
_QA_RE = re.compile(r"^/api/walkthrough/([^/]+)/([^/]+)/qa$")


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
            # The body is still unread on the wire; keep-alive would feed its tail to the
            # next request line.
            self.close_connection = True
            self._send_json(413, {"error": "payload too large"})
            return None, True
        return (self.rfile.read(length) if length else b""), False

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
            return self._tracked(lambda: self._route_ask(m.group(1), m.group(2)))
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
        injected = html.replace("</head>", f"<script>window.CW_LIVE={blob};</script></head>", 1)
        self._send_html(200, injected)

    def _route_events(self, key, wid, qs):
        if not self._check_host() or not self._check_token_query(qs) or not self._check_origin():
            return
        d = self._resolve_dir(key, wid, json_errors=False)
        if d is None:
            return
        daemon = self.server.cw_daemon
        meta = cw_store.read_meta(d) or {}
        live = _live_payload(key, wid, meta, d, daemon.token, daemon.port)
        snapshot = _snapshot_payload(live)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

        q = daemon.hub.register(key, wid)
        try:
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
        notes = payload.get("notes")
        if not isinstance(notes, list):
            self._send_json(400, {"error": "notes must be a list"})
            return
        kept = [
            n for n in notes
            if isinstance(n, dict) and isinstance(n.get("id"), str) and VALID_ID_RE.fullmatch(n["id"])
            and n.get("origin") == "local" and n.get("state") == "draft"
        ]
        cw_store.write_json(d / "page-notes.json", {"notes": kept})
        self._send_json(200, {"ok": True, "count": len(kept)})

    def _route_qa(self, key, wid):
        if not self._check_host() or not self._check_token_header() or not self._check_origin():
            return
        d = self._resolve_dir(key, wid, json_errors=True)
        if d is None:
            return
        self._send_json(200, {"qa": cw_ask.read_qa(d)})

    def _route_ask(self, key, wid):
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
        question = payload.get("question")
        if not isinstance(question, str) or not (1 <= len(question) <= cw_ask.QUESTION_MAX):
            self._send_json(400, {"error": "question must be 1-2000 characters",
                                   "remedy": "shorten the question"})
            return
        try:
            cw_ask.build_prompt(d, anchor, question)
        except cw_store.CWError as e:
            self._send_json(400, {"error": str(e), "remedy": e.remedy})
            return
        qid = "q-" + secrets.token_hex(4)
        self.server.cw_daemon.start_ask(key, wid, d, qid, anchor, question)
        self._send_json(202, {"qid": qid})

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
        self.token = secrets.token_urlsafe(32)
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

    def ask_lock(self, d):
        """Per-walkthrough-directory lock so a second ask on the same walkthrough waits
        rather than running alongside the first (one in-flight ask per walkthrough)."""
        key = str(d)
        with self._ask_locks_guard:
            return self._ask_locks.setdefault(key, threading.Lock())

    def start_ask(self, key, wid, d, qid, anchor, question):
        with self._inflight_lock:
            self._asks += 1
        self._idle_since = None

        def _target():
            try:
                with self.ask_lock(d):
                    cw_ask.answer(d, qid, anchor, question,
                                  on_event=lambda ev, data: self.hub.emit(key, wid, ev, data))
            finally:
                with self._inflight_lock:
                    self._asks -= 1

        threading.Thread(target=_target, daemon=True).start()

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
    shutil.rmtree(d, ignore_errors=True)


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
