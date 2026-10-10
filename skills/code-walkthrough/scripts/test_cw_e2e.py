#!/usr/bin/env python3
"""Batch-2 integration self-check (phase2-spec.md section 1): the real cw_run.py orchestration
driven through the real in-process cw_server.Daemon, over HTTP and SSE, with cw_testlib.StubLLM
standing in for the model backend -- no fakes, no mocks of our own modules -- plus the tests that
spawn a real detached cw_server.py process. conftest.py marks this file `e2e`. Assert-based, no
framework; also collected by pytest.
"""

import contextlib
import functools
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_mcp  # noqa: E402
import cw_run  # noqa: E402
import cw_server  # noqa: E402
import cw_store  # noqa: E402
import cw_testlib  # noqa: E402

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


_http = functools.partial(cw_testlib.request, timeout=10)


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


_sse_connect = functools.partial(cw_testlib.sse_connect, timeout=10)


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
                if cw_server._MERMAID_PLACEHOLDER in on_disk:
                    payload = cw_server._mermaid_payload()
                    assert payload in stripped and cw_server._MERMAID_PLACEHOLDER not in stripped
                    stripped = stripped.replace(payload, cw_server._MERMAID_PLACEHOLDER)
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
    cw_testlib.require_posix()
    with cw_testlib.temp_home(), _stopped_afterward():
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
            proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Real detached daemon (cw_mcp.ensure_server spawns cw_server.py serve)
# ---------------------------------------------------------------------------

def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait(cond, timeout=5, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return cond()


@contextlib.contextmanager
def _stopped_afterward():
    """Whatever daemon this test starts, make sure it is gone before the test ends."""
    try:
        yield
    finally:
        info = cw_store.read_json(cw_store.server_json_path())
        if info and _pid_alive(info.get("pid")):
            try:
                cw_mcp.cmd_stop()
            except Exception:
                pass
        if info and _pid_alive(info.get("pid")):
            try:
                os.kill(info["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_ensure_server_race_then_stop():
    cw_testlib.require_posix()
    with cw_testlib.temp_home(), _stopped_afterward():
        assert cw_mcp.cmd_stop() == 0  # "not running", nothing started yet
        results = []
        threads = [threading.Thread(target=lambda: results.append(cw_mcp.ensure_server())) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert len(results) == 2
        assert results[0]["pid"] == results[1]["pid"]
        pid = results[0]["pid"]
        assert _pid_alive(pid)

        assert cw_mcp.cmd_stop() == 0
        assert _wait(lambda: not _pid_alive(pid), timeout=5)


def test_idle_exit_within_five_seconds():
    cw_testlib.require_posix()
    with cw_testlib.temp_home():
        os.environ["CW_IDLE_S"] = "1"
        try:
            with _stopped_afterward():
                info = cw_mcp.ensure_server()
                assert _wait(lambda: not _pid_alive(info["pid"]), timeout=5)
        finally:
            os.environ.pop("CW_IDLE_S", None)
