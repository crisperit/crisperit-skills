#!/usr/bin/env python3
"""Batch-2 integration self-check (phase2-spec.md section 1): the real cw_run.py orchestration
driven through the real in-process cw_server.Daemon, over HTTP and SSE, with cw_testlib.StubLLM
standing in for the model backend -- no fakes, no mocks of our own modules. Assert-based, no
framework; also collected by pytest.
"""

import http.client
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_llm  # noqa: E402
import cw_mcp  # noqa: E402
import cw_run  # noqa: E402
import cw_server  # noqa: E402
import cw_store  # noqa: E402
import cw_testlib  # noqa: E402

cw_llm.BACKOFF_S = [0, 0]

SCRIPTS_DIR = cw_run.SCRIPTS_DIR
DELAY = 0.5


def _passing_fragment(seed):
    """A blank note (fine for a lone fragment-gate check, EMPTY_NOTE_FLOOR skips fragment=True)
    would trip the floor once every batch's fragment is merged into the whole-diff analysis, so
    this gives every hunk a real one -- short enough that MIN_IDENTIFIER_LEN drops its words,
    which keeps the note/changed-lines identifier-overlap check out of the picture too."""
    return {"files": [
        {"path": f["path"], "role": "does a thing",
         "hunks": [{"header": h["header"], "note": "explains this hunk"} for h in f["hunks"]]}
        for f in seed["files"]
    ]}


def _stub_reply(body, _n):
    """One callable script shared by both roles (StubLLM dispatches by model only when the
    script is a dict; a callable sees every request, so it switches on model itself): builds a
    fragment from whatever seed this batch's own user message carries, rather than a fixed
    canned reply, since the two batches in this diff touch different files."""
    model = body.get("model")
    user_text = cw_testlib.last_user_text(body)
    if model == "analysis-m":
        seed = cw_testlib.first_json_block(user_text)
        return cw_testlib.delayed(DELAY, cw_testlib.tool_call("submit_fragment", _passing_fragment(seed)))
    if model == "prose-m":
        return cw_testlib.delayed(DELAY, cw_testlib.tool_call(
            "submit_prose", {"overview": "o", "verdict": "v", "flow_mermaid": ""}))
    raise RuntimeError(f"unexpected model {model!r}")


def _http(daemon, method, path, *, host=None, origin=None, token=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=10)
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


def _rpc(daemon, tool, args):
    status, data = _http(daemon, "POST", "/api/rpc", token=daemon.token,
                          body=json.dumps({"tool": tool, "args": args}))
    return status, (json.loads(data) if data else None)


def _extract_cw_live(html):
    """(blob_start, blob_end, parsed) for the `<script>window.CW_LIVE={...};</script>` the
    daemon injects right before `</head>` -- blob_start/blob_end bound the whole tag, so
    `html[:blob_start] + html[blob_end:]` reconstructs the un-injected page exactly."""
    start = html.index("<script>window.CW_LIVE=")
    end = html.index("</script>", start) + len("</script>")
    inner = html[start + len("<script>window.CW_LIVE="):end - len(";</script>")]
    return start, end, json.loads(inner)


def _sse_connect(daemon, key, wid):
    sock = socket.create_connection(("127.0.0.1", daemon.port), timeout=10)
    req = (f"GET /api/walkthrough/{key}/{wid}/events?k={daemon.token} HTTP/1.1\r\n"
           f"Host: 127.0.0.1:{daemon.port}\r\nConnection: keep-alive\r\n\r\n")
    sock.sendall(req.encode())
    return sock


def _sse_read_events(sock, *, stop_names, timeout=20):
    """Parse `event: x\\ndata: y\\n\\n` frames off the wire until a `step` event names one of
    `stop_names` (the run's own terminal states), or the deadline passes."""
    sock.settimeout(0.5)
    buf = b""
    events = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        except socket.timeout:
            pass
        while b"\n\n" in buf:
            frame, buf = buf.split(b"\n\n", 1)
            lines = frame.decode(errors="replace").splitlines()
            ev_name, data = None, None
            for line in lines:
                if line.startswith("event: "):
                    ev_name = line[len("event: "):]
                elif line.startswith("data: "):
                    data = json.loads(line[len("data: "):])
            if ev_name is not None:
                events.append((ev_name, data))
                if ev_name == "step" and isinstance(data, dict) and data.get("name") == "run" \
                        and data.get("status") in stop_names:
                    return events
    return events


# ---------------------------------------------------------------------------
# Happy path: two-batch fanout diff, real daemon, real cw_run, StubLLM
# ---------------------------------------------------------------------------

def test_two_batch_run_through_the_real_daemon():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        # Plain .txt files, not .py/.go/.ts/.rs: symdelta's LANG_EXTENSIONS doesn't recognise
        # them, so it takes its fast "no supported files changed" path instead of spinning up
        # a real pyright/tsserver LSP session, which this diff is too trivial to need and
        # which can take far longer than this test's budget to initialize.
        repo, base, head = cw_testlib.make_repo(
            tmp, {"a.txt": "1\n2\n3\n", "b.txt": "x\ny\nz\n"},
            {"a.txt": "1\nA\n3\n", "b.txt": "x\nB\nz\n"},
        )
        key = cw_store.repo_key(str(repo.resolve()))
        wid = cw_store.walkthrough_id("main...HEAD")
        with cw_testlib.StubLLM(_stub_reply) as stub:
            cw_testlib.write_config(
                home, {"a": stub.profile("analysis-m"), "p": stub.profile("prose-m")},
                {"analysis": "a", "prose": "p"}, small_diff_lines=0, batch_max_lines=1,
            )
            daemon = cw_server.Daemon(write_server_json=False)
            daemon.start()
            try:
                # Start the RPC call on its own thread and connect SSE the moment the
                # walkthrough dir exists, well before `run()`'s own background thread can
                # reach a batch call (each gated behind DELAY): a connect timed off the RPC
                # call's *return* would race run() and could miss its early events.
                start_result = {}

                def _start():
                    status, body = _rpc(daemon, "walkthrough_start", {
                        "repo": str(repo), "base": base, "head": head,
                        "target": "main...HEAD", "slug": "two-batch",
                    })
                    start_result["status"], start_result["body"] = status, body

                start_thread = threading.Thread(target=_start, daemon=True)
                start_thread.start()

                d = cw_store.walkthrough_dir(key, wid)
                deadline = time.monotonic() + 10
                while not d.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert d.exists(), "walkthrough dir never appeared"
                sock = _sse_connect(daemon, key, wid)

                start_thread.join(timeout=15)
                assert not start_thread.is_alive(), "walkthrough_start RPC never returned"
                status, body = start_result["status"], start_result["body"]
                assert status == 200 and body["ok"], body
                result = body["result"]
                assert (result["key"], result["id"]) == (key, wid)
                assert result["status"] == "building"

                # Skeleton already served before any worker could have replied: StubLLM's
                # replies are all delayed by DELAY, and prepare_walkthrough's synchronous
                # skeleton render happened before this RPC call even returned.
                status, page_bytes = _http(daemon, "GET", f"/walkthrough/{key}/{wid}/?k={daemon.token}")
                assert status == 200, status
                page_html = page_bytes.decode()
                live_blob_start, live_blob_end, live = _extract_cw_live(page_html)
                assert live["status"] == "building"

                # served page minus the injection equals the on-disk partial.html
                stripped = page_html[:live_blob_start] + page_html[live_blob_end:]
                on_disk = (d / "partial.html").read_text()
                assert stripped == on_disk

                # the CSP meta tag precedes the injected script, i.e. still the first
                # meaningful thing in <head>
                csp_idx = page_html.index('http-equiv="Content-Security-Policy"')
                assert csp_idx < live_blob_start

                try:
                    events = _sse_read_events(sock, stop_names=("done", "failed"))
                finally:
                    sock.close()

                run_step = next(d2 for ev, d2 in events if ev == "step" and d2.get("name") == "run")
                assert run_step["status"] == "done", events

                # every passing fragment (batches 1 and 2) is covered by a later `rebuilt`
                for n in (1, 2):
                    ok_idx = next(
                        i for i, (ev, d2) in enumerate(events)
                        if ev == "step" and d2.get("name") == f"batch-{n}" and d2.get("status") == "ok"
                    )
                    assert any(
                        ev == "rebuilt" and n in (d2.get("fragments") or [])
                        for ev, d2 in events[ok_idx + 1:]
                    ), (n, events)

                # final page passes validate_analysis.py --rendered
                meta = cw_store.read_meta(d)
                assert meta["status"] == "done"
                final_html = d / f"{meta['slug']}.html"
                assert final_html.exists()
                result_v = subprocess.run(
                    [sys.executable, str(SCRIPTS_DIR / "validate_analysis.py"),
                     "--diff", str(d / "raw.diff"), "--analysis", str(d / "analysis.json"),
                     "--rendered", str(final_html)],
                    capture_output=True, text=True,
                )
                assert result_v.returncode == 0, result_v.stderr

                # usage totals in walkthrough_get and in the CW_LIVE injection
                status, body = _rpc(daemon, "walkthrough_get", {"id": wid, "key": key, "parts": ["meta"]})
                assert status == 200 and body["ok"], body
                usage_meta = body["result"]["meta"]
                assert usage_meta["usage"]["analysis"]["calls"] >= 1
                assert usage_meta["usage"]["prose"]["calls"] >= 1
                assert usage_meta["total"]["prompt_tokens"] > 0

                status, final_page = _http(daemon, "GET", f"/walkthrough/{key}/{wid}/?k={daemon.token}")
                assert status == 200
                _, _, live_final = _extract_cw_live(final_page.decode())
                assert live_final["total"]["prompt_tokens"] > 0
                assert live_final["page"] == "final"
            finally:
                daemon.stop()


# ---------------------------------------------------------------------------
# No backend, through MCP tools/call
# ---------------------------------------------------------------------------

def test_no_backend_through_mcp_tools_call_gives_iserror_with_remedy():
    with cw_testlib.temp_home():
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPTS_DIR / "cw_mcp.py"), "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\nb\n"}, {"foo.py": "a\nX\n"})
                request = {
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "walkthrough_start", "arguments": {
                        "repo": str(repo), "base": base, "head": head,
                        "target": "main...HEAD", "slug": "no-backend",
                    }},
                }
                proc.stdin.write(json.dumps(request) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
                reply = json.loads(line)
                assert reply["id"] == 1
                result = reply["result"]
                assert result["isError"] is True, result
                payload = json.loads(result["content"][0]["text"])
                assert payload.get("remedy"), payload
        finally:
            try:
                cw_mcp.cmd_stop()
            except Exception:
                pass
            proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Final gate failure
# ---------------------------------------------------------------------------

def test_final_gate_failure_gives_gate_lines_through_walkthrough_get_and_serves_partial():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\nb\n"}, {"foo.py": "a\nX\n"})
        key = cw_store.repo_key(str(repo))
        wid = cw_store.walkthrough_id("main...HEAD")
        d = cw_store.walkthrough_dir(key, wid, create=True)
        diff_text = cw_testlib.git(repo, "diff", f"{base}...{head}")
        (d / "raw.diff").write_text(diff_text)
        (d / "partial.html").write_text("<html><head><title>t</title></head><body>skeleton</body></html>")
        (d / "analysis.json").write_text(json.dumps({"files": []}))  # missing required top-level keys
        meta = {
            "id": wid, "key": key, "status": "building", "repo": str(repo), "base": base, "head": head,
            "slug": "t", "page": "partial", "rev": 0, "steps": {}, "usage": {}, "batches_done": [],
            "batches": 0, "gate": [], "error": None, "remedy": None, "paths": [], "explain": False,
            "created_at": cw_store.now_iso(), "updated_at": cw_store.now_iso(), "target": "main...HEAD",
            "route": "small",
        }
        cw_store.write_json(d / "meta.json", meta)

        daemon = cw_server.Daemon(write_server_json=False)
        daemon.start()
        try:
            # SSE connected before the failure fires: a client watching for the documented
            # "name run marks the end" contract must see it over the wire, not just via a
            # separate meta.json/RPC poll (run() returns "failed" here without raising).
            sock = _sse_connect(daemon, key, wid)
            try:
                ok = cw_run._final_build(d, meta, lambda ev, data: daemon.hub.emit(key, wid, ev, data))
                assert ok is False
                events = _sse_read_events(sock, stop_names=("done", "failed"))
            finally:
                sock.close()
            run_events = [data for ev, data in events if ev == "step" and data.get("name") == "run"]
            assert run_events and run_events[-1]["status"] == "failed", events

            status, body = _rpc(daemon, "walkthrough_get", {"id": wid, "key": key})
            assert status == 200 and body["ok"], body
            result = body["result"]
            assert result["status"] == "failed"
            assert result["summary"]["gate"], result

            status, page_bytes = _http(daemon, "GET", f"/walkthrough/{key}/{wid}/?k={daemon.token}")
            assert status == 200, status
            assert b"skeleton" in page_bytes  # partial page served, never promoted to final
        finally:
            daemon.stop()


if __name__ == "__main__":
    tests = [
        test_two_batch_run_through_the_real_daemon,
        test_no_backend_through_mcp_tools_call_gives_iserror_with_remedy,
        test_final_gate_failure_gives_gate_lines_through_walkthrough_get_and_serves_partial,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
