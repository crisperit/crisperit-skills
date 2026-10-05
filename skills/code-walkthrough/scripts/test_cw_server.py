#!/usr/bin/env python3
"""Self-check for cw_server.py. Assert-based, no framework; also collected by pytest. Every
Daemon runs in-process on an ephemeral port and is always stopped in a finally block, so no
test leaks a thread or a bound socket."""

import contextlib
import http.client
import json
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_server  # noqa: E402
import cw_store  # noqa: E402
import cw_testlib  # noqa: E402

KEY = "repo-abcdef"
WID = "cmp-01234567"


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


@contextlib.contextmanager
def running_daemon(idle_s=None):
    daemon = cw_server.Daemon(idle_s=idle_s, write_server_json=False)
    daemon.start()
    try:
        yield daemon
    finally:
        daemon.stop()


def _request(daemon, method, path, *, host=None, origin=None, token=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{daemon.port}")
        if origin is not None:
            conn.putheader("Origin", origin)
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


_UNSET = object()


def _rpc(daemon, tool, args, *, token=_UNSET):
    status, data = _request(daemon, "POST", "/api/rpc", token=daemon.token if token is _UNSET else token,
                             body=json.dumps({"tool": tool, "args": args}))
    return status, (json.loads(data) if data else None)


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

def test_host_guard_rejects_wrong_host():
    with cw_testlib.temp_home():
        with running_daemon() as daemon:
            status, _ = _request(daemon, "GET", "/health", host="evil.example:9999")
            assert status == 403, status
            status, _ = _request(daemon, "GET", "/health")
            assert status == 200, status
            status, _ = _request(daemon, "GET", "/health", host=f"localhost:{daemon.port}")
            assert status == 200, status


def test_token_guard_rejects_missing_or_wrong_token():
    with cw_testlib.temp_home() as home:
        _make_walkthrough(home)
        with running_daemon() as daemon:
            status, _ = _rpc(daemon, "walkthrough_list", {}, token="wrong")
            assert status == 403, status
            status, _ = _rpc(daemon, "walkthrough_list", {}, token=None)
            assert status == 403, status
            status, body = _rpc(daemon, "walkthrough_list", {})
            assert status == 200 and body["ok"], body


def test_origin_guard_rejects_mismatched_origin():
    # /health only checks Host (per the route table); the Origin guard applies to /api/ and
    # the page route, so exercise it on /api/rpc instead.
    with cw_testlib.temp_home():
        with running_daemon() as daemon:
            status, _ = _request(daemon, "POST", "/api/rpc", token=daemon.token,
                                  origin="http://attacker.example", body=json.dumps({"tool": "walkthrough_list", "args": {}}))
            assert status == 403, status
            status, _ = _request(daemon, "POST", "/api/rpc", token=daemon.token,
                                  origin=f"http://127.0.0.1:{daemon.port}",
                                  body=json.dumps({"tool": "walkthrough_list", "args": {}}))
            assert status == 200, status


def test_traversal_in_id_or_key_gives_400():
    with cw_testlib.temp_home():
        with running_daemon() as daemon:
            status, _ = _request(daemon, "GET", f"/walkthrough/../../etc/{WID}/?k={daemon.token}")
            assert status in (400, 404), status
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


def test_page_route_requires_query_token_not_header():
    with cw_testlib.temp_home() as home:
        _make_walkthrough(home)
        with running_daemon() as daemon:
            status, _ = _request(daemon, "GET", f"/walkthrough/{KEY}/{WID}/")
            assert status == 403, status


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

def _sse_read_until(sock, needle, timeout=5):
    sock.settimeout(timeout)
    buf = b""
    deadline = time.monotonic() + timeout
    while needle not in buf and time.monotonic() < deadline:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
    return buf


def _sse_connect(daemon, key, wid):
    sock = socket.create_connection(("127.0.0.1", daemon.port), timeout=5)
    req = (
        f"GET /api/walkthrough/{key}/{wid}/events?k={daemon.token} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{daemon.port}\r\nConnection: keep-alive\r\n\r\n"
    )
    sock.sendall(req.encode())
    return sock


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
        with running_daemon() as daemon:
            status, body = _rpc(daemon, "walkthrough_list", {"limit": 20})
            reply = body
            ids = [w["id"] for w in reply["result"]["walkthroughs"]]
            assert ids[:2] == ["cmp-89abcdef", "cmp-01234567"], ids


# -- startup housekeeping ---------------------------------------------------

def test_mark_interrupted_flips_building_status():
    with cw_testlib.temp_home() as home:
        d, meta = _make_walkthrough(home, status="building")
        cw_server._mark_interrupted()
        assert cw_store.read_meta(d)["status"] == "interrupted"


def test_prune_removes_old_without_drafts_keeps_with_drafts():
    with cw_testlib.temp_home() as home:
        old_no_drafts, _ = _make_walkthrough(home, key="repo-abcdef", wid="cmp-01234567")
        _set_meta(old_no_drafts, updated_at="2000-01-01T00:00:00Z", repo=None)
        old_with_drafts, _ = _make_walkthrough(home, key="repo-abcdef", wid="cmp-89abcdef")
        _set_meta(old_with_drafts, updated_at="2000-01-01T00:00:00Z", repo=None)
        cw_store.write_json(old_with_drafts / "page-notes.json", {"notes": [{"id": "n1"}]})

        import os
        os.environ["CW_PRUNE_DAYS"] = "30"
        try:
            cw_server._prune()
        finally:
            os.environ.pop("CW_PRUNE_DAYS", None)

        assert not old_no_drafts.exists()
        assert old_with_drafts.exists()


def test_prune_state_json_requires_both_local_origin_and_draft_state():
    # A note with origin "local" that has since been posted is not a draft any more: it must
    # not keep the walkthrough alive past CW_PRUNE_DAYS. An actual local draft still should.
    with cw_testlib.temp_home() as home:
        posted, _ = _make_walkthrough(home, key="repo-abcdef", wid="cmp-01234567")
        _set_meta(posted, updated_at="2000-01-01T00:00:00Z", repo=None)
        cw_store.write_json(posted / "state.json", {"notes": [{"id": "n1", "origin": "local", "state": "posted"}]})

        draft, _ = _make_walkthrough(home, key="repo-abcdef", wid="cmp-89abcdef")
        _set_meta(draft, updated_at="2000-01-01T00:00:00Z", repo=None)
        cw_store.write_json(draft / "state.json", {"notes": [{"id": "n2", "origin": "local", "state": "draft"}]})

        import os
        os.environ["CW_PRUNE_DAYS"] = "30"
        try:
            cw_server._prune()
        finally:
            os.environ.pop("CW_PRUNE_DAYS", None)

        assert not posted.exists()
        assert draft.exists()


if __name__ == "__main__":
    tests = [
        test_host_guard_rejects_wrong_host,
        test_token_guard_rejects_missing_or_wrong_token,
        test_origin_guard_rejects_mismatched_origin,
        test_traversal_in_id_or_key_gives_400,
        test_injection_round_trip,
        test_hostile_draft_shows_up_only_as_lt,
        test_page_route_requires_query_token_not_header,
        test_page_not_built_yet_gives_503,
        test_put_notes_filters_and_413,
        test_reject_closes_connection_instead_of_leaking_body_into_next_request,
        test_drafts_survive_restart_on_new_port,
        test_sse_snapshot_and_broadcast_to_two_clients,
        test_second_start_does_not_spawn_second_run,
        test_wait_s_returns_on_remedy_step_once_only,
        test_walkthrough_list_newest_first_and_filtered,
        test_mark_interrupted_flips_building_status,
        test_prune_removes_old_without_drafts_keeps_with_drafts,
        test_prune_state_json_requires_both_local_origin_and_draft_state,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
