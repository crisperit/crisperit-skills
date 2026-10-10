#!/usr/bin/env python3
"""Self-check for cw_server.py. Assert-based, no framework; also collected by pytest. Every
Daemon runs in-process on an ephemeral port and is always stopped in a finally block, so no
test leaks a thread or a bound socket."""

import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_ask  # noqa: E402
import cw_server  # noqa: E402
import cw_store  # noqa: E402
import cw_testlib  # noqa: E402

KEY = "repo-abcdef"
WID = "cmp-01234567"

# test_cw_task, test_cw_handover and test_cw_sibling reach these through `import test_cw_server as srv`.
running_daemon = cw_testlib.running_daemon
_request = cw_testlib.request
_rpc = cw_testlib.rpc
_sse_connect = cw_testlib.sse_connect
_sse_read_until = cw_testlib.sse_read_until


class FakeRunner:
    """Enough of cw_run.py's public interface (section 5 of the phase 2 spec) for the daemon
    tests: a run() that the test controls the pace of, via an Event."""

    def __init__(self):
        self.run_count = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def prepare_walkthrough(self, params, is_running=lambda d: False):
        d = cw_store.walkthrough_dir(params["key"], params["wid"], create=True)
        meta = cw_store.read_meta(d)
        reused = is_running(d)
        if meta is None:
            meta = {"id": params["wid"], "key": params["key"], "status": "building", "steps": {},
                     "usage": {}, "batches_done": [], "batches": 0, "page": "partial", "rev": 0,
                     "gate": [], "error": None, "remedy": None, "slug": "slug"}
            cw_store.write_json(d / "meta.json", meta)
        return d, meta, reused

    def run(self, d, on_event):
        self.run_count += 1
        self.started.set()
        self.release.wait(timeout=5)
        cw_store.update_meta(d, lambda m: m.update(status="done"))
        on_event("step", {"name": "run", "status": "done"})


def _set_meta(d, **fields):
    """Bypass cw_store.update_meta's own updated_at stamping, for tests that need to
    backdate a walkthrough."""
    meta = cw_store.read_meta(d)
    meta.update(fields)
    cw_store.write_json(d / "meta.json", meta)


def _make_walkthrough(home, *, key=KEY, wid=WID, page="partial", status="done"):
    d = cw_store.walkthrough_dir(key, wid, create=True)
    meta = {
        "id": wid, "key": key, "status": status, "slug": "my-slug", "page": page, "rev": 1,
        "steps": {}, "usage": {"analysis": {"calls": 1, "prompt_tokens": 10, "completion_tokens": 5,
                                             "cost_usd": 0.01}},
        "batches_done": [1], "batches": 2, "gate": [], "error": None, "remedy": None,
        "created_at": cw_store.now_iso(), "updated_at": cw_store.now_iso(), "target": "main...HEAD",
        "repo": str(home), "base": "a", "head": "b", "route": "fanout",
    }
    cw_store.write_json(d / "meta.json", meta)
    (d / "partial.html").write_text("<html><head><title>t</title></head><body>partial</body></html>")
    (d / "my-slug.html").write_text("<html><head><title>t</title></head><body>final</body></html>")
    return d, meta


# -- guards -------------------------------------------------------------

def test_guards_host_token_origin_and_page_token():
    # /health only checks Host; the token and Origin guards apply to /api/ and the page route.
    with cw_testlib.temp_home() as home:
        _make_walkthrough(home)
        with running_daemon() as daemon:
            port = daemon.port
            body = json.dumps({"tool": "walkthrough_list", "args": {}})
            page = f"/walkthrough/{KEY}/{WID}/"
            rows = [
                ("health wrong host", dict(method="GET", path="/health", host="evil.example:9999"), 403),
                ("health 127.0.0.1", dict(method="GET", path="/health"), 200),
                ("health localhost", dict(method="GET", path="/health", host=f"localhost:{port}"), 200),
                ("rpc wrong token", dict(method="POST", path="/api/rpc", token="wrong", body=body), 403),
                ("rpc empty token", dict(method="POST", path="/api/rpc", token="", body=body), 403),
                ("rpc no token", dict(method="POST", path="/api/rpc", body=body), 403),
                ("rpc good token", dict(method="POST", path="/api/rpc", token=daemon.token, body=body), 200),
                ("rpc attacker origin", dict(method="POST", path="/api/rpc", token=daemon.token, body=body,
                                             origin="http://attacker.example"), 403),
                ("rpc own origin", dict(method="POST", path="/api/rpc", token=daemon.token, body=body,
                                        origin=f"http://127.0.0.1:{port}"), 200),
                ("page no token", dict(method="GET", path=page), 403),
                ("page header token but no ?k=", dict(method="GET", path=page, token=daemon.token), 403),
                ("page wrong ?k=", dict(method="GET", path=f"{page}?k=wrong"), 403),
                ("page good ?k=", dict(method="GET", path=f"{page}?k={daemon.token}"), 200),
            ]
            for name, kwargs, want in rows:
                status, _ = _request(daemon, kwargs.pop("method"), kwargs.pop("path"), **kwargs)
                assert status == want, (name, status)


def test_traversal_in_id_or_key_gives_400():
    with cw_testlib.temp_home():
        with running_daemon() as daemon:
            # `..` as the key segment matches _PAGE_RE, so walkthrough_dir's guard is what answers
            status, _ = _request(daemon, "GET", f"/walkthrough/../{WID}/?k={daemon.token}")
            assert status == 400, status
            status, body = _rpc(daemon, "walkthrough_get", {"id": "../../etc/passwd", "key": KEY})
            assert status == 200, status
            assert body["ok"] is False


# -- page serving and injection -----------------------------------------

def test_injection_round_trip():
    with cw_testlib.temp_home() as home:
        d, meta = _make_walkthrough(home, status="done", page="final")
        with running_daemon() as daemon:
            status, body = _request(daemon, "GET", f"/walkthrough/{KEY}/{WID}/?k={daemon.token}")
            assert status == 200, status
            html = body.decode()
            assert "window.CW_LIVE" in html
            stripped = html.replace(
                html[html.index("<script>window.CW_LIVE="):html.index("</script>") + len("</script>")], ""
            )
            on_disk = (d / "my-slug.html").read_text()
            assert stripped == on_disk


def test_page_route_mermaid():
    with cw_testlib.temp_home() as home:
        d, meta = _make_walkthrough(home, status="done", page="final")
        raw = "<html><head></head><body><script><!-- MERMAID_JS --></script></body></html>"
        spliced = "<html><head></head><body><script>var m=1;</script></body></html>"
        with running_daemon() as daemon:
            url = f"/walkthrough/{KEY}/{WID}/?k={daemon.token}"
            (d / "my-slug.html").write_text(raw)
            status, body = _request(daemon, "GET", url)
            assert status == 200, status
            html = body.decode()
            assert "<!-- MERMAID_JS -->" not in html
            assert "mermaid" in html and len(html) > 1_000_000
            assert (d / "my-slug.html").read_text() == raw

            (d / "my-slug.html").write_text(spliced)
            status, body = _request(daemon, "GET", url)
            assert status == 200, status
            assert len(body) < 10_000
            assert b"<script>var m=1;</script>" in body


def test_hostile_draft_shows_up_only_as_lt():
    with cw_testlib.temp_home() as home:
        d, meta = _make_walkthrough(home)
        cw_store.write_json(d / "page-notes.json", {"notes": [
            {"id": "n1", "origin": "local", "state": "draft", "text": "<script>alert(1)</script>"}
        ]})
        with running_daemon() as daemon:
            status, body = _request(daemon, "GET", f"/walkthrough/{KEY}/{WID}/?k={daemon.token}")
            assert status == 200, status
            html = body.decode()
            assert "<script>alert" not in html
            assert "\\u003cscript>alert(1)\\u003c/script>" in html


def test_page_not_built_yet_gives_503():
    with cw_testlib.temp_home() as home:
        d, meta = _make_walkthrough(home, page="final")
        (d / "my-slug.html").unlink()
        with running_daemon() as daemon:
            status, body = _request(daemon, "GET", f"/walkthrough/{KEY}/{WID}/?k={daemon.token}")
            assert status == 503, status
            assert b"page not built yet" in body


# -- notes PUT ------------------------------------------------------------

def test_put_notes_filters_and_413():
    with cw_testlib.temp_home() as home:
        d, meta = _make_walkthrough(home)
        with running_daemon() as daemon:
            notes = [
                {"id": "ok-1", "origin": "local", "state": "draft", "text": "keep"},
                {"id": "ok-2", "origin": "github", "state": "draft", "text": "drop, wrong origin"},
                {"id": "ok-3", "origin": "local", "state": "posted", "text": "drop, wrong state"},
                {"id": "bad id!", "origin": "local", "state": "draft", "text": "drop, bad id"},
            ]
            status, body = _request(daemon, "PUT", f"/api/walkthrough/{KEY}/{WID}/notes",
                                     token=daemon.token, body=json.dumps({"notes": notes}))
            assert status == 200, body
            reply = json.loads(body)
            assert reply == {"ok": True, "count": 1}
            saved = cw_store.read_json(d / "page-notes.json")
            assert [n["id"] for n in saved["notes"]] == ["ok-1"]

            big = json.dumps({"notes": [{"id": "x", "origin": "local", "state": "draft",
                                          "text": "y" * 1_100_000}]})
            status, _ = _request(daemon, "PUT", f"/api/walkthrough/{KEY}/{WID}/notes",
                                  token=daemon.token, body=big)
            assert status == 413, status


def test_reject_closes_connection_instead_of_leaking_body_into_next_request():
    # Regression: an early-reject 403 used to leave the PUT body unread on the keep-alive
    # socket, so Chrome's pooled connection fed it to the server as the next request line
    # (a stale-token notes PUT followed by a page GET came back 400/501 instead of 200).
    with cw_testlib.temp_home() as home:
        _make_walkthrough(home)
        with running_daemon() as daemon:
            put_body = json.dumps({"notes": []})
            req = (
                f"PUT /api/walkthrough/{KEY}/{WID}/notes HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{daemon.port}\r\n"
                "X-CW-Token: wrong\r\n"
                f"Content-Length: {len(put_body)}\r\n"
                "Content-Type: application/json\r\n"
                "Connection: keep-alive\r\n\r\n"
                f"{put_body}"
                f"GET /health HTTP/1.1\r\nHost: 127.0.0.1:{daemon.port}\r\n\r\n"
            )
            sock = socket.create_connection(("127.0.0.1", daemon.port), timeout=5)
            try:
                sock.sendall(req.encode())
                sock.settimeout(5)
                buf = b""
                try:
                    while True:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                except socket.timeout:
                    pass
            finally:
                sock.close()
            assert buf.startswith(b"HTTP/1.1 403"), buf
            assert b"Connection: close" in buf, buf
            # Exactly one response: the leftover body+GET bytes were never parsed as a
            # second request (which would show up as a 400 or 501 status line).
            assert buf.count(b"HTTP/1.1") == 1, buf


# -- restart on a new port ------------------------------------------------

def test_drafts_survive_restart_on_new_port():
    with cw_testlib.temp_home() as home:
        d, meta = _make_walkthrough(home)
        cw_store.write_json(d / "page-notes.json", {"notes": [
            {"id": "n1", "origin": "local", "state": "draft", "text": "still here"}
        ]})
        daemon = cw_server.Daemon(write_server_json=True)
        daemon.start()
        held_port = daemon.port
        daemon.stop()

        # Hold the old port so the next daemon must fall back to a fresh one.
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", held_port))
        blocker.listen(1)
        try:
            daemon2 = cw_server.Daemon(write_server_json=True)
            daemon2.start()
            try:
                assert daemon2.port != held_port
                status, body = _request(daemon2, "GET", f"/walkthrough/{KEY}/{WID}/?k={daemon2.token}")
                assert status == 200, status
                assert "still here" in body.decode()
            finally:
                daemon2.stop()
        finally:
            blocker.close()


# -- SSE -------------------------------------------------------------------

def test_sse_snapshot_and_broadcast_to_two_clients():
    with cw_testlib.temp_home() as home:
        _make_walkthrough(home)
        with running_daemon() as daemon:
            sock_a = _sse_connect(daemon, KEY, WID)
            sock_b = _sse_connect(daemon, KEY, WID)
            try:
                buf_a = _sse_read_until(sock_a, b"event: snapshot")
                buf_b = _sse_read_until(sock_b, b"event: snapshot")
                assert b"event: snapshot" in buf_a, buf_a
                assert b"event: snapshot" in buf_b, buf_b

                # wait for both tabs to register before broadcasting
                deadline = time.monotonic() + 5
                while daemon.hub.client_count() < 2 and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert daemon.hub.client_count() == 2

                daemon.hub.emit(KEY, WID, "step", {"name": "run", "status": "ok"})
                more_a = _sse_read_until(sock_a, b"event: step")
                more_b = _sse_read_until(sock_b, b"event: step")
                assert b"event: step" in more_a, more_a
                assert b"event: step" in more_b, more_b
            finally:
                sock_a.close()
                sock_b.close()


# -- RPC: start/get/list ----------------------------------------------------

def test_second_start_does_not_spawn_second_run():
    with cw_testlib.temp_home():
        fake = FakeRunner()
        cw_server.runner = fake
        try:
            with running_daemon() as daemon:
                params = {"key": KEY, "wid": WID}
                status1, body1 = _rpc(daemon, "walkthrough_start", params)
                assert status1 == 200 and body1["ok"], body1
                assert fake.started.wait(timeout=5)

                status2, body2 = _rpc(daemon, "walkthrough_start", params)
                assert status2 == 200 and body2["ok"], body2

                time.sleep(0.2)
                assert fake.run_count == 1
                fake.release.set()
                d = cw_store.walkthrough_dir(KEY, WID)
                deadline = time.monotonic() + 5
                while (cw_store.read_meta(d) or {}).get("status") != "done" and time.monotonic() < deadline:
                    time.sleep(0.02)
        finally:
            cw_server.runner = None


def test_wait_s_returns_on_remedy_step_once_only():
    with cw_testlib.temp_home() as home:
        d, meta = _make_walkthrough(home, status="building")
        meta["steps"] = {"symdelta": {"status": "remedy", "remedy": "run npm ci", "remedy_reported": False}}
        cw_store.write_json(d / "meta.json", meta)
        with running_daemon() as daemon:
            status, body = _rpc(daemon, "walkthrough_get", {"id": WID, "key": KEY, "wait_s": 5})
            reply = body
            assert reply["ok"], reply
            assert reply["result"]["status"] == "building"

            saved = cw_store.read_meta(d)
            assert saved["steps"]["symdelta"]["remedy_reported"] is True

            start = time.monotonic()
            status, body = _rpc(daemon, "walkthrough_get", {"id": WID, "key": KEY, "wait_s": 1})
            elapsed = time.monotonic() - start
            assert elapsed >= 0.9, elapsed  # the already-reported remedy no longer short-circuits


def test_walkthrough_list_newest_first_and_filtered():
    with cw_testlib.temp_home() as home:
        d1, _m1 = _make_walkthrough(home, key="repo-abcdef", wid="cmp-01234567")
        _set_meta(d1, updated_at="2020-01-01T00:00:00Z")
        d2, _m2 = _make_walkthrough(home, key="repo-abcdef", wid="cmp-89abcdef")
        _set_meta(d2, updated_at="2024-01-01T00:00:00Z")
        other_repo = str(home / "other-repo")
        other_key = cw_store.repo_key(other_repo)
        d3, _m3 = _make_walkthrough(home, key=other_key, wid="cmp-00000003")
        _set_meta(d3, updated_at="2022-01-01T00:00:00Z")
        with running_daemon() as daemon:
            def listed(**args):
                status, body = _rpc(daemon, "walkthrough_list", args)
                assert status == 200 and body["ok"], body
                return [w["id"] for w in body["result"]["walkthroughs"]]

            assert listed(limit=20) == ["cmp-89abcdef", "cmp-00000003", "cmp-01234567"]
            assert listed(limit=1) == ["cmp-89abcdef"]
            assert listed(repo=other_repo) == ["cmp-00000003"]


def test_failed_walkthrough_get_shows_gate_and_serves_partial():
    with cw_testlib.temp_home() as home:
        d, _ = _make_walkthrough(home, status="failed", page="partial")
        gate = ["analysis.json: missing key 'overview'"]
        _set_meta(d, gate=gate, remedy="fix the analysis")
        with running_daemon() as daemon:
            status, body = _rpc(daemon, "walkthrough_get", {"id": WID, "key": KEY})
            assert status == 200 and body["ok"], body
            assert body["result"]["status"] == "failed"
            assert body["result"]["summary"]["gate"] == gate, body["result"]
            status, page = _request(daemon, "GET", f"/walkthrough/{KEY}/{WID}/?k={daemon.token}")
            assert status == 200, status
            assert b"partial" in page and b"final" not in page


# -- startup housekeeping ---------------------------------------------------

def test_mark_interrupted_flips_building_status():
    with cw_testlib.temp_home() as home:
        d, meta = _make_walkthrough(home, status="building")
        cw_server._mark_interrupted()
        assert cw_store.read_meta(d)["status"] == "interrupted"


def test_prune(monkeypatch):
    # (state.json notes, page-notes.json notes, updated_at, expect_kept). A note with origin
    # "local" that has since been posted is not a draft: it must not keep an old walkthrough alive.
    old, recent = "2000-01-01T00:00:00Z", cw_store.now_iso()
    local = lambda state: [{"id": "n1", "origin": "local", "state": state}]
    rows = [
        ("old, no notes", None, None, old, False),
        ("old, page notes", None, [{"id": "n1"}], old, True),
        ("old, state local posted", local("posted"), None, old, False),
        ("old, state local draft", local("draft"), None, old, True),
        ("recent, no drafts", None, None, recent, True),
    ]
    monkeypatch.setenv("CW_PRUNE_DAYS", "30")
    with cw_testlib.temp_home() as home:
        dirs = []
        for i, (_name, state_notes, page_notes, updated_at, _kept) in enumerate(rows):
            d, _ = _make_walkthrough(home, key=KEY, wid=f"cmp-{i:08x}")
            _set_meta(d, updated_at=updated_at, repo=None)
            if state_notes:
                cw_store.write_json(d / "state.json", {"notes": state_notes})
            if page_notes:
                cw_store.write_json(d / "page-notes.json", {"notes": page_notes})
            dirs.append(d)
        cw_server._prune()
        for (name, *_rest, kept), d in zip(rows, dirs):
            assert d.exists() is kept, name


# -- in-flight turn buffers ----------------------------------------------------

def _sse_events(buf):
    events = []
    for block in buf.decode().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "event" in lines and "data" in lines:
            events.append((lines["event"], json.loads(lines["data"])))
    return events


def _sse_until_final(sock, timeout=10):
    """Read the stream until a non-pending thread turn arrives; returns the parsed events."""
    sock.settimeout(1)
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            break
        buf += chunk
        if any(ev == "thread" and d.get("kind") == "turn" and d["record"]["status"] != "pending"
               for ev, d in _sse_events(buf)):
            break
    return _sse_events(buf)


def _reduce(events):
    """What the page does: a snapshot replaces the buffers, a delta applies once per seq."""
    turns = {}
    for ev, data in events:
        if ev == "snapshot":
            turns = {t["qid"]: {"seq": t["seq"], "text": t["text"]} for t in data.get("turns", [])}
        elif ev == "delta":
            t = turns.setdefault(data["qid"], {"seq": 0, "text": ""})
            if data["seq"] > t["seq"]:
                t["seq"], t["text"] = data["seq"], t["text"] + data["text"]
    return {qid: t["text"] for qid, t in turns.items()}


def _streamed_turn_fixture(tmp, home, chunks, delay):
    import test_cw_ask as ask

    d = ask._claude_done(home)
    entry = cw_testlib.claude_stream_entry(chunks, delay=delay)
    tool = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "foo.py"}}]}}
    entry["stream"].insert(3, tool)
    return ask, d, entry


def test_snapshot_mid_turn_carries_buffer_and_late_client_converges():
    chunks = [f"w{i} " for i in range(10)]
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        ask, d, entry = _streamed_turn_fixture(tmp, home, chunks, 0.05)
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}), running_daemon() as daemon:
            key, wid = d.parent.name, d.name
            sock_a = _sse_connect(daemon, key, wid)
            sock_b = None
            try:
                _sse_read_until(sock_a, b"event: snapshot")
                status, raw = ask._post_comment(daemon, key, wid, ask.ANCHOR_SECTION, "why?")
                assert status == 202, raw
                qid = json.loads(raw)["qid"]
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    mid = daemon.inflight(key, wid)
                    if mid and mid[0]["seq"] >= 3:
                        break
                    time.sleep(0.02)
                assert mid and mid[0]["qid"] == qid and mid[0]["thread_id"] == qid, mid
                assert set(mid[0]) == {"qid", "thread_id", "seq", "text", "progress"}
                assert daemon.inflight(key, "cmp-ffffffff") == []

                sock_b = _sse_connect(daemon, key, wid)
                events_b = _sse_until_final(sock_b)
                events_a = _sse_until_final(sock_a)
            finally:
                sock_a.close()
                if sock_b:
                    sock_b.close()

            snap = next(data for ev, data in events_b if ev == "snapshot")
            assert [t["qid"] for t in snap["turns"]] == [qid]
            assert snap["turns"][0]["seq"] >= 3 and snap["turns"][0]["text"]

            kinds_a = [ev for ev, _ in events_a]
            assert "delta" in kinds_a and "progress" in kinds_a
            progress = next(data for ev, data in events_a if ev == "progress")
            assert progress["tool"] == "Read" and progress["qid"] == qid

            full = "".join(chunks)
            assert _reduce(events_a)[qid] == full
            assert _reduce(events_b)[qid] == full
            assert daemon.inflight(key, wid) == []


def test_events_registers_before_building_snapshot():
    with cw_testlib.temp_home() as home:
        _make_walkthrough(home)
        with running_daemon() as daemon:
            real = daemon.inflight

            def inflight_then_emit(key, wid):
                turns = real(key, wid)
                daemon.hub.emit(key, wid, "delta", {"qid": "q-0", "thread_id": "q-0", "seq": 1, "text": "x"})
                return turns

            daemon.inflight = inflight_then_emit
            sock = _sse_connect(daemon, KEY, WID)
            try:
                buf = _sse_read_until(sock, b"event: delta")
            finally:
                sock.close()
            kinds = [ev for ev, _ in _sse_events(buf)]
            assert kinds[:2] == ["snapshot", "delta"], kinds


def test_stop_kills_in_flight_turn_group():
    chunks = [f"w{i} " for i in range(40)]
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        ask, d, entry = _streamed_turn_fixture(tmp, home, chunks, 0.15)
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}) as fc:
            daemon = cw_server.Daemon(idle_s=None, write_server_json=False).start()
            try:
                status, raw = ask._post_comment(daemon, d.parent.name, d.name, ask.ANCHOR_SECTION, "why?")
                assert status == 202, raw
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not (
                        fc.starts() and daemon.inflight(d.parent.name, d.name)
                        and daemon.inflight(d.parent.name, d.name)[0]["seq"] >= 1):
                    time.sleep(0.02)
                start = fc.starts()[0]
                os.kill(start["pid"], 0)
            finally:
                daemon.stop()

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not _pgid_gone(start["pgid"]):
                time.sleep(0.05)
            assert _pgid_gone(start["pgid"])
            while time.monotonic() < deadline + 5 and daemon._asks:
                time.sleep(0.05)


# -- cancel, outcomes, turn cap ------------------------------------------------

def _post(daemon, path, body=None, **kw):
    kw.setdefault("token", daemon.token)
    status, raw = _request(daemon, "POST", path, body=json.dumps({} if body is None else body), **kw)
    return status, (json.loads(raw) if raw else None)


def _append_qa(d, *records):
    with open(d / "qa.jsonl", "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _guard_checks(daemon, path, body):
    assert _post(daemon, path, body, host="evil.example:9999")[0] == 403
    assert _post(daemon, path, body, token="")[0] == 403
    assert _post(daemon, path, body, origin="http://attacker.example")[0] == 403


def _wait_status(d, qid, want, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        turn = next((t for t in cw_ask.read_qa(d) if t["qid"] == qid), None)
        if turn and turn["status"] == want:
            return turn
        time.sleep(0.02)
    raise AssertionError(f"{qid} never reached {want}: {cw_ask.read_qa(d)}")


def _pgid_gone(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    return False


def test_cancel_route_guards_and_unknown_qid():
    with cw_testlib.temp_home() as home:
        _make_walkthrough(home)
        with running_daemon() as daemon:
            path = f"/api/walkthrough/{KEY}/{WID}/comment/q-0123abcd/cancel"
            _guard_checks(daemon, path, {})
            status, body = _post(daemon, path)
            assert status == 404 and body == {"error": "turn not running"}, (status, body)


def test_cancel_running_streamed_turn_keeps_partial_and_kills_group():
    chunks = [f"w{i} " for i in range(40)]
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        ask, d, entry = _streamed_turn_fixture(tmp, home, chunks, 0.15)
        key, wid = d.parent.name, d.name
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}) as fc, running_daemon() as daemon:
            status, raw = ask._post_comment(daemon, key, wid, ask.ANCHOR_SECTION, "why?")
            assert status == 202, raw
            qid = json.loads(raw)["qid"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (
                    fc.starts() and daemon.inflight(key, wid) and daemon.inflight(key, wid)[0]["seq"] >= 2):
                time.sleep(0.02)
            start = fc.starts()[0]
            status, body = _post(daemon, f"/api/walkthrough/{key}/{wid}/comment/{qid}/cancel")
            assert (status, body) == (200, {"ok": True}), (status, body)
            turn = _wait_status(d, qid, "cancelled")
            assert turn["answer"].startswith("w0 w1"), turn["answer"]
            assert "w39" not in turn["answer"]
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not _pgid_gone(start["pgid"]):
                time.sleep(0.05)
            assert _pgid_gone(start["pgid"])
            status, _ = _post(daemon, f"/api/walkthrough/{key}/{wid}/comment/{qid}/cancel")
            assert status == 404


def test_cancel_queued_turn_never_spawns():
    chunks = [f"w{i} " for i in range(40)]
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        ask, d, entry = _streamed_turn_fixture(tmp, home, chunks, 0.15)
        key, wid = d.parent.name, d.name
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry, entry]}) as fc, running_daemon() as daemon:
            status, raw = ask._post_comment(daemon, key, wid, ask.ANCHOR_SECTION, "first")
            first = json.loads(raw)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not fc.starts():
                time.sleep(0.02)
            status, raw = ask._post_comment(daemon, key, wid, ask.ANCHOR_SECTION, "second", first["thread_id"])
            assert status == 202, raw
            second = json.loads(raw)["qid"]
            base = f"/api/walkthrough/{key}/{wid}/comment"
            assert _post(daemon, f"{base}/{second}/cancel")[0] == 200
            assert _post(daemon, f"{base}/{first['qid']}/cancel")[0] == 200
            turn = _wait_status(d, second, "cancelled", timeout=5)
            assert turn["answer"] == "" and turn["thread_id"] == first["thread_id"]
            assert {"error", "remedy", "profile", "model", "session_id", "usage"} <= set(turn)
            assert turn["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": None}
            _wait_status(d, first["qid"], "cancelled", timeout=5)
            assert len(fc.starts()) == 1


OID = "o-0a1b2c3d"
OQID = "q-11223344"


def _outcome_fixture(home):
    d, _ = _make_walkthrough(home)
    (d / "turns").mkdir(exist_ok=True)
    cw_store.write_json(d / "turns" / f"{OQID}.anchor.json",
                        {"qid": OQID, "thread_id": OQID, "anchor": {},
                         "resolvable": [{"id": "gh-1", "path": "a.py", "line": 3, "author": "r", "body": "b"}]})
    _append_qa(d, {"qid": OQID, "thread_id": OQID, "status": "ok", "answer": "a", "comment": "c", "question": "c"},
               {"type": "outcome", "oid": OID, "qid": OQID, "thread_id": OQID, "outcome": "resolve",
                "payload": {"thread": "gh-1", "why": "fixed at a.py:3"}, "state": "proposed", "at": "t"})
    return d


def test_outcome_routes_guards_404_dismiss_and_conflict():
    with cw_testlib.temp_home() as home:
        d = _outcome_fixture(home)
        with running_daemon() as daemon:
            base = f"/api/walkthrough/{KEY}/{WID}/outcomes"
            _guard_checks(daemon, f"{base}/{OID}/dismiss", {})
            _guard_checks(daemon, f"{base}/{OID}/edit", {"payload": {"thread": "gh-1", "why": "x"}})
            assert _post(daemon, f"{base}/o-ffffffff/dismiss")[0] == 404
            assert _get_qa(daemon)["outcomes"][0]["state"] == "proposed"
            sock = _sse_connect(daemon, KEY, WID)
            try:
                status, body = _post(daemon, f"{base}/{OID}/dismiss")
                assert status == 200 and body["outcome"]["state"] == "dismissed", (status, body)
                buf = _sse_read_until(sock, b"event: outcome")
            finally:
                sock.close()
            ev = next(data for name, data in _sse_events(buf) if name == "outcome")
            assert ev["oid"] == OID and ev["state"] == "dismissed"
            assert _get_qa(daemon)["outcomes"][0]["state"] == "dismissed"
            status, body = _post(daemon, f"{base}/{OID}/dismiss")
            assert (status, body) == (409, {"error": "outcome is not open"})
            assert _post(daemon, f"{base}/{OID}/edit", {"payload": {"thread": "gh-1", "why": "x"}})[0] == 409


def test_outcome_edit_ok_and_bad_edit_gives_validator_text():
    with cw_testlib.temp_home() as home:
        _outcome_fixture(home)
        with running_daemon() as daemon:
            path = f"/api/walkthrough/{KEY}/{WID}/outcomes/{OID}/edit"
            status, body = _post(daemon, path, {"payload": {"thread": "gh-9", "why": "x"}})
            assert status == 400 and "gh-9 is not a review thread" in body["error"] and body["remedy"], body
            status, body = _post(daemon, path, {"payload": {"thread": "gh-1", "why": "  "}})
            assert status == 400 and body["error"] == "why is required", body
            assert _post(daemon, path, {})[0] == 400
            status, body = _post(daemon, path, {"payload": {"thread": "gh-1", "why": " better "}})
            assert status == 200 and body["outcome"]["payload"] == {"thread": "gh-1", "why": "better"}
            assert body["outcome"]["state"] == "proposed"


def test_page_edit_revert_reapply_routes():
    with cw_testlib.temp_home() as home:
        d = _outcome_fixture(home)
        _append_qa(d, {"type": "outcome", "oid": "o-0000feed", "qid": OQID, "thread_id": OQID,
                       "outcome": "page_edit", "state": "applied", "at": "t",
                       "payload": {"op": "replace", "target": "b:p1", "block": {"type": "prose", "text": "x"}}})
        pe = "o-0000feed"
        with running_daemon() as daemon:
            base = f"/api/walkthrough/{KEY}/{WID}/outcomes"
            _guard_checks(daemon, f"{base}/{pe}/revert", {})
            _guard_checks(daemon, f"{base}/{pe}/reapply", {})
            assert _post(daemon, f"{base}/o-ffffffff/revert")[0] == 404
            assert _post(daemon, f"{base}/{pe}/reapply")[0] == 409
            assert _post(daemon, f"{base}/{pe}/dismiss")[0] == 400
            assert _post(daemon, f"{base}/{pe}/edit", {"payload": {"thread": "gh-1", "why": "x"}})[0] == 400
            assert _post(daemon, f"{base}/{OID}/revert")[0] == 400
            sock = _sse_connect(daemon, KEY, WID)
            try:
                status, body = _post(daemon, f"{base}/{pe}/revert")
                assert status == 200 and body["outcome"]["state"] == "reverted", (status, body)
                buf = _sse_read_until(sock, b"event: outcome")
            finally:
                sock.close()
            ev = next(data for name, data in _sse_events(buf) if name == "outcome")
            assert ev["oid"] == pe and ev["state"] == "reverted" and ev["outcome"] == "page_edit"
            assert _post(daemon, f"{base}/{pe}/revert")[0] == 409
            status, body = _post(daemon, f"{base}/{pe}/reapply")
            assert status == 200 and body["outcome"]["state"] == "applied"


def _get_qa(daemon):
    status, raw = _request(daemon, "GET", f"/api/walkthrough/{KEY}/{WID}/qa", token=daemon.token)
    assert status == 200, raw
    return json.loads(raw)


def test_thread_turn_cap():
    with cw_testlib.temp_home() as home:
        anchor = {"kind": "section", "section": "Overview", "quote": "q"}
        ok_turn = lambda i: {"qid": f"q-{i:08x}", "thread_id": "q-00000000", "anchor": anchor, "status": "ok",
                             "answer": "a", "comment": "c", "question": "c"}
        full, _ = _make_walkthrough(home)
        _append_qa(full, *(ok_turn(i) for i in range(20)))
        # only ok and pending turns count: error and cancelled ones leave room for a 20th
        room, _ = _make_walkthrough(home, wid="cmp-89abcdef")
        _append_qa(room, *(ok_turn(i) for i in range(19)),
                   *({**ok_turn(i), "status": st, "answer": ""}
                     for i, st in ((100, "error"), (101, "cancelled"), (102, "error"))))
        body = {"anchor": anchor, "text": "more", "thread_id": "q-00000000"}
        with running_daemon() as daemon:
            status, reply = _post(daemon, f"/api/walkthrough/{KEY}/{WID}/comment", body)
            assert status == 409, (status, reply)
            assert reply == {"error": "thread has 20 turns", "remedy": "start a new thread"}
            assert len(cw_ask.read_qa(full)) == 20

            status, reply = _post(daemon, f"/api/walkthrough/{KEY}/cmp-89abcdef/comment", body)
            assert status == 202, (status, reply)
            _wait_turns_done(room)


def test_stopped_daemon_never_spawns_a_queued_turn():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        ask, d, entry = _streamed_turn_fixture(tmp, home, ["a "], 0)
        key, wid = d.parent.name, d.name
        tid = "q-00000000"
        _append_qa(d, {"qid": tid, "thread_id": tid, "anchor": ask.ANCHOR_SECTION, "status": "ok",
                       "answer": "a", "comment": "c", "question": "c"})
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}) as fc, running_daemon() as daemon:
            lock = daemon.ask_lock(d, tid)
            with lock:
                status, raw = ask._post_comment(daemon, key, wid, ask.ANCHOR_SECTION, "next", tid)
                assert status == 202, raw
                qid = json.loads(raw)["qid"]
                daemon.stop()
            _wait_status(d, qid, "cancelled", timeout=5)
            assert fc.starts() == []


def test_inflight_text_and_seq_are_consistent_under_the_live_lock():
    daemon = cw_server.Daemon(write_server_json=False)
    live = {"text": "", "seq": 0, "progress": None, "lock": threading.Lock()}
    daemon._turns["q-1"] = {"live": live, "key": KEY, "wid": WID, "thread_id": "q-1"}
    done = threading.Event()

    def writer():
        while not done.is_set():
            with live["lock"]:
                live["seq"] += 1
                time.sleep(0)
                live["text"] += "x"
            time.sleep(0.0002)

    th = threading.Thread(target=writer)
    th.start()
    try:
        for _ in range(300):
            (snap,) = daemon.inflight(KEY, WID)
            assert len(snap["text"]) == snap["seq"]
    finally:
        done.set()
        th.join()


GD_OID = "o-0000d0d0"


def _github_draft_fixture(home):
    d = _outcome_fixture(home)
    _append_qa(d, {"type": "outcome", "oid": GD_OID, "qid": OQID, "thread_id": OQID, "outcome": "github_draft",
                   "state": "proposed", "at": "t",
                   "payload": {"body": "b", "original": " orig\n", "verbatim": False,
                               "target": {"kind": "new", "path": "a.py", "line": 3}}})
    return d


def test_github_draft_routes_edit_verbatim_keep_dismiss_and_wrong_action():
    with cw_testlib.temp_home() as home:
        _github_draft_fixture(home)
        with running_daemon() as daemon:
            base = f"/api/walkthrough/{KEY}/{WID}/outcomes"
            for action, body in (("keep", {"note_id": "n-1"}), ("verbatim", {}), ("edit", {"payload": {"body": "x"}})):
                _guard_checks(daemon, f"{base}/{GD_OID}/{action}", body)
            assert _post(daemon, f"{base}/{GD_OID}/revert")[0] == 400
            assert _post(daemon, f"{base}/{OID}/keep", {"note_id": "n-1"})[0] == 400
            assert _post(daemon, f"{base}/{OID}/verbatim")[0] == 400
            assert _post(daemon, f"{base}/o-ffffffff/keep", {"note_id": "n-1"})[0] == 404
            assert _post(daemon, f"{base}/{GD_OID}/edit", {"payload": {"body": " "}})[0] == 400
            assert _post(daemon, f"{base}/{GD_OID}/keep", {})[0] == 400
            status, body = _post(daemon, f"{base}/{GD_OID}/edit", {"payload": {"body": " mine "}})
            assert status == 200 and body["outcome"]["payload"]["body"] == " mine "
            status, body = _post(daemon, f"{base}/{GD_OID}/verbatim")
            assert status == 200 and body["outcome"]["payload"]["body"] == " orig\n"
            status, body = _post(daemon, f"{base}/{GD_OID}/keep", {"note_id": "n-1"})
            assert status == 200 and body["outcome"]["state"] == "kept"
            assert body["outcome"]["payload"]["note_id"] == "n-1"
            for action, payload in (("dismiss", {}), ("keep", {"note_id": "n-2"}), ("verbatim", {}),
                                    ("edit", {"payload": {"body": "x"}})):
                assert _post(daemon, f"{base}/{GD_OID}/{action}", payload)[0] == 409


def test_comment_with_a_thread_anchor_needs_a_github_root_thread_note():
    with cw_testlib.temp_home() as home:
        d, _ = _make_walkthrough(home)
        root = {"id": "gh-1", "origin": "github", "gh_thread_id": "T1", "path": "a.py", "line": 3}
        cw_store.write_json(d / "state.json", {"notes": [
            root, {**root, "id": "gh-2", "reply_to": "gh-1"}, {**root, "id": "n-3", "origin": "local"},
            {**root, "id": "gh-4", "gh_thread_id": None}]})
        with running_daemon() as daemon:
            for note_id in ("gh-2", "n-3", "gh-4", "gh-9"):
                status, body = _post(daemon, f"/api/walkthrough/{KEY}/{WID}/comment", {
                    "anchor": {"kind": "thread", "note_id": note_id, "quote": "q"}, "text": "hi"})
                assert status == 400 and "not a GitHub review thread" in body["error"], (note_id, status, body)
            assert cw_ask.read_qa(d) == []


def _triage_setup(home, extra_notes=(), pr=7):
    d, _ = _make_walkthrough(home)
    root = {"origin": "github", "path": "a.py", "line": 3, "body": "please fix"}
    notes = [{**root, "id": f"gh-{i}", "gh_thread_id": f"T{i}"} for i in (1, 2, 3)]
    notes += [{**root, "id": "gh-4", "gh_thread_id": "T4", "resolved": True},
              {**root, "id": "gh-5", "gh_thread_id": "T1", "reply_to": "gh-1"}, *extra_notes]
    cw_store.write_json(d / "state.json", {"meta": {"pr": pr}, "notes": notes})
    return d


def _wait_turns_done(d):
    for _ in range(100):
        if not any(r.get("status") == "pending" for r in cw_ask.read_qa(d)):
            return
        time.sleep(0.05)
    raise AssertionError("turns still pending")


def test_triage_starts_one_source_triage_turn_per_open_root_thread_and_publishes_nothing():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp, \
            cw_testlib.fake_gh(tmp, comments=[], threads=[]) as gh:
        d = _triage_setup(home)
        before = (d / "state.json").read_text()
        path = f"/api/walkthrough/{KEY}/{WID}/triage"
        with running_daemon() as daemon:
            status, body = _post(daemon, path)
            assert status == 200 and (body["started"], body["skipped"], body["remaining"]) == (3, 0, 0), body
            assert sorted(t["note_id"] for t in body["threads"]) == ["gh-1", "gh-2", "gh-3"]
            _wait_turns_done(d)
            turns = cw_ask.read_qa(d)
            assert len(turns) == 3 and all(t["source"] == "triage" for t in turns), turns
            assert all(t["comment"] == cw_server.TRIAGE_COMMENT and len(t["comment"]) < 500 for t in turns)
            by_thread = {t["thread_id"]: t["anchor"] for t in turns}
            for info in body["threads"]:
                a = by_thread[info["thread_id"]]
                assert a["kind"] == "thread" and a["note_id"] == info["note_id"] and a["quote"] == "please fix"
            assert (d / "state.json").read_text() == before
            assert gh.log() == []
            status, body = _post(daemon, path)
            assert status == 200 and (body["started"], body["skipped"]) == (0, 3), body
            assert len(cw_ask.read_qa(d)) == 3


def test_triage_cap_leaves_the_rest_remaining():
    with cw_testlib.temp_home() as home:
        d = _triage_setup(home)
        old = cw_server.TRIAGE_CAP
        cw_server.TRIAGE_CAP = 2
        try:
            with running_daemon() as daemon:
                path = f"/api/walkthrough/{KEY}/{WID}/triage"
                status, body = _post(daemon, path)
                assert (status, body["started"], body["remaining"]) == (200, 2, 1), body
                _wait_turns_done(d)
                status, body = _post(daemon, path)
                assert (status, body["started"], body["skipped"], body["remaining"]) == (200, 1, 2, 0), body
                _wait_turns_done(d)
        finally:
            cw_server.TRIAGE_CAP = old


def test_triage_guards_and_409s():
    with cw_testlib.temp_home() as home:
        d = _triage_setup(home)
        path = f"/api/walkthrough/{KEY}/{WID}/triage"
        with running_daemon() as daemon:
            assert _request(daemon, "POST", path, body="{}")[0] == 403
            assert _post(daemon, path, token="wrong")[0] == 403
            assert _post(daemon, path, origin="http://evil.example")[0] == 403
            assert _post(daemon, path, host="evil.example:9")[0] == 403
            assert cw_ask.read_qa(d) == []
            cw_store.write_json(d / "state.json", {"meta": {"pr": None}, "notes": []})
            status, body = _post(daemon, path)
            assert status == 409 and body["error"] == cw_server.NO_PR_MSG, body
            daemon.is_running = lambda _d: True
            status, body = _post(daemon, path)
            assert status == 409 and body["error"] == cw_server.STILL_BUILDING_MSG, body


def test_notes_put_works_while_idle_and_waits_for_the_walkthrough_lock():
    with cw_testlib.temp_home() as home:
        d, _ = _make_walkthrough(home)
        note = {"id": "n-1", "origin": "local", "state": "draft", "path": "a.py", "line": 1, "side": "RIGHT"}
        with running_daemon() as daemon:
            put = lambda: _request(daemon, "PUT", f"/api/walkthrough/{KEY}/{WID}/notes",
                                    token=daemon.token, body=json.dumps({"notes": [note]}))
            assert put()[0] == 200
            result = []
            with cw_store.dir_lock(d):
                t = threading.Thread(target=lambda: result.append(put()[0]))
                t.start()
                t.join(0.3)
                assert t.is_alive() and result == []
            t.join(5)
            assert result == [200]

