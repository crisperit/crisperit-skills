#!/usr/bin/env python3
"""Self-check for cw_server.py. Assert-based, no framework; also collected by pytest. Every
Daemon runs in-process on an ephemeral port and is always stopped in a finally block, so no
test leaks a thread or a bound socket."""

import contextlib
import http.client
import json
import os
import socket
import sys
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


# -- token persistence ------------------------------------------------------

def test_token_stable_across_restarts():
    with cw_testlib.temp_home():
        with running_daemon() as daemon1:
            token1 = daemon1.token
        with running_daemon() as daemon2:
            token2 = daemon2.token
        assert token1 == token2


def test_token_file_is_mode_0600():
    with cw_testlib.temp_home():
        with running_daemon():
            mode = cw_store.token_path().stat().st_mode
            assert (mode & 0o777) == 0o600


def test_corrupt_token_file_replaced_with_fresh_valid_token():
    with cw_testlib.temp_home():
        cw_store.token_path().write_text("")
        with running_daemon() as daemon:
            token = daemon.token
            assert cw_store.TOKEN_RE.fullmatch(token)
            assert cw_store.token_path().read_text().strip() == token


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


# -- direct GitHub draft path -------------------------------------------------

EXACT_BODY = "  lead spaces\n`code` <b>x</b> \U0001F600 tail\n"


def test_direct_draft_and_reply_draft_make_no_model_call_and_keep_exact_body():
    import tempfile
    import test_cw_post as post  # shared PR-walkthrough fixtures

    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(lambda body, _n: post._small_route_reply(body)) as stub, \
                cw_testlib.fake_claude(tmp, {}) as fc, \
                cw_testlib.fake_gh(tmp, comments=[], threads=[]):
            repo, base, head, d = post._build_done_pr(home, tmp, stub)
            root = post._note(id="gh-501", origin="github", state="posted", gh_id=501,
                              gh_node_id="NODE_501", gh_thread_id="THREAD_1", body="root")
            post._add_notes(d, [root])
            baseline = len(stub.requests)
            qa = d / "qa.jsonl"
            qa_before = qa.read_text() if qa.exists() else ""

            direct = post._note(id="n-direct", body=EXACT_BODY)
            reply = post._note(id="n-reply", body=EXACT_BODY, reply_to="gh-501", in_reply_to="gh-501")
            key, wid = d.parent.name, d.name
            with post.running_daemon() as daemon:
                status, raw = post._put_notes(daemon, key, wid, [direct, reply])
                assert status == 200, (status, raw)
                saved = {n["id"]: n for n in cw_store.read_json(d / "page-notes.json")["notes"]}
                assert saved["n-direct"]["body"] == EXACT_BODY
                assert saved["n-reply"]["body"] == EXACT_BODY
                status, raw = post._preview(daemon, key, wid, [])
                assert status == 200, (status, raw)

            state = {n["id"]: n for n in cw_store.read_json(d / "state.json")["notes"]}
            assert state["n-direct"]["body"] == EXACT_BODY
            assert state["n-direct"]["origin"] == "local" and state["n-direct"]["state"] == "draft"
            assert state["n-reply"]["body"] == EXACT_BODY
            # PAGE_DENIED_NOTE_FIELDS strips reply_to; in_reply_to is the durable link.
            assert state["n-reply"]["reply_to"] is None
            assert state["n-reply"]["in_reply_to"] == "gh-501"
            assert len(stub.requests) == baseline
            assert fc.log() == []
            assert (qa.read_text() if qa.exists() else "") == qa_before


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

    d = ask._claude_done(home, tmp)
    entry = cw_testlib.claude_stream_entry(chunks, delay=delay)
    tool = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "foo.py"}}]}}
    entry["stream"].insert(3, tool)
    return ask, d, entry


def test_snapshot_mid_turn_carries_buffer_and_late_client_converges():
    import tempfile

    chunks = [f"w{i} " for i in range(10)]
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        ask, d, entry = _streamed_turn_fixture(tmp, home, chunks, 0.15)
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
    import os
    import tempfile

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

            def gone():
                try:
                    os.killpg(start["pgid"], 0)
                except ProcessLookupError:
                    return True
                return False

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not gone():
                time.sleep(0.05)
            assert gone()
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
    import tempfile

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
    import tempfile

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


def _get_qa(daemon):
    status, raw = _request(daemon, "GET", f"/api/walkthrough/{KEY}/{WID}/qa", token=daemon.token)
    assert status == 200, raw
    return json.loads(raw)


def test_thread_turn_cap_gives_409_on_the_21st_turn():
    with cw_testlib.temp_home() as home:
        d, _ = _make_walkthrough(home)
        anchor = {"kind": "section", "section": "Overview", "quote": "q"}
        _append_qa(d, *({"qid": f"q-{i:08x}", "thread_id": "q-00000000", "anchor": anchor, "status": "ok",
                         "answer": "a", "comment": "c", "question": "c"} for i in range(20)))
        with running_daemon() as daemon:
            status, body = _post(daemon, f"/api/walkthrough/{KEY}/{WID}/comment",
                                  {"anchor": anchor, "text": "more", "thread_id": "q-00000000"})
            assert status == 409, (status, body)
            assert body == {"error": "thread has 20 turns", "remedy": "start a new thread"}
            assert len(cw_ask.read_qa(d)) == 20


def test_thread_cap_counts_only_ok_and_pending_turns():
    with cw_testlib.temp_home() as home:
        d, _ = _make_walkthrough(home)
        anchor = {"kind": "section", "section": "Overview", "quote": "q"}
        turns = [{"qid": f"q-{i:08x}", "thread_id": "q-00000000", "anchor": anchor, "status": "ok",
                  "answer": "a", "comment": "c", "question": "c"} for i in range(19)]
        turns += [{"qid": f"q-{i:08x}", "thread_id": "q-00000000", "anchor": anchor, "status": st,
                   "answer": "", "comment": "c", "question": "c"}
                  for i, st in ((100, "error"), (101, "cancelled"), (102, "error"))]
        _append_qa(d, *turns)
        with running_daemon() as daemon:
            body = {"anchor": anchor, "text": "more", "thread_id": "q-00000000"}
            path = f"/api/walkthrough/{KEY}/{WID}/comment"
            assert _post(daemon, path, body)[0] != 409


def test_stopped_daemon_never_spawns_a_queued_turn():
    import tempfile

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
                live["text"] += "x"
                time.sleep(0)

    th = threading.Thread(target=writer)
    th.start()
    try:
        for _ in range(2000):
            (snap,) = daemon.inflight(KEY, WID)
            assert len(snap["text"]) == snap["seq"]
    finally:
        done.set()
        th.join()


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
        test_token_stable_across_restarts,
        test_token_file_is_mode_0600,
        test_corrupt_token_file_replaced_with_fresh_valid_token,
        test_drafts_survive_restart_on_new_port,
        test_sse_snapshot_and_broadcast_to_two_clients,
        test_second_start_does_not_spawn_second_run,
        test_wait_s_returns_on_remedy_step_once_only,
        test_walkthrough_list_newest_first_and_filtered,
        test_mark_interrupted_flips_building_status,
        test_prune_removes_old_without_drafts_keeps_with_drafts,
        test_prune_state_json_requires_both_local_origin_and_draft_state,
        test_direct_draft_and_reply_draft_make_no_model_call_and_keep_exact_body,
        test_snapshot_mid_turn_carries_buffer_and_late_client_converges,
        test_events_registers_before_building_snapshot,
        test_thread_cap_counts_only_ok_and_pending_turns,
        test_stopped_daemon_never_spawns_a_queued_turn,
        test_inflight_text_and_seq_are_consistent_under_the_live_lock,
        test_stop_kills_in_flight_turn_group,
        test_cancel_route_guards_and_unknown_qid,
        test_cancel_running_streamed_turn_keeps_partial_and_kills_group,
        test_cancel_queued_turn_never_spawns,
        test_outcome_routes_guards_404_dismiss_and_conflict,
        test_outcome_edit_ok_and_bad_edit_gives_validator_text,
        test_thread_turn_cap_gives_409_on_the_21st_turn,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
