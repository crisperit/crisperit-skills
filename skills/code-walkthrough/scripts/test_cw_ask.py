#!/usr/bin/env python3
"""Self-check for phase 4: ask from the page (phase345-spec.md, "Phase 4", implementer A).
Assert-based, no framework; also collected by pytest. In-process cw_server.Daemon, real
cw_run.py/cw_ask.py, cw_testlib.StubLLM standing in for the model backend -- no fakes of our
own modules besides that one external boundary."""

import contextlib
import http.client
import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_ask  # noqa: E402
import cw_llm  # noqa: E402
import cw_run  # noqa: E402
import cw_server  # noqa: E402
import cw_store  # noqa: E402
import cw_testlib  # noqa: E402

cw_llm.BACKOFF_S = [0, 0]

ANCHOR_LINE = {"kind": "line", "path": "foo.py", "side": "RIGHT", "line": 2,
               "end_line": None, "hunk_id": "foo.py\t@@ -1,3 +1,3 @@", "quote": "X"}
ANCHOR_SECTION = {"kind": "section", "section": "Overview", "quote": "q"}


def _small_route_reply(body):
    seed = cw_testlib.first_json_block(cw_testlib.last_user_text(body))
    analysis = {
        "files": [{"path": f["path"], "role": "does a thing",
                   "hunks": [{"header": h["header"], "note": "explains it"} for h in f["hunks"]]}
                  for f in seed["files"]],
        "overview": "o", "verdict": "v", "flow_mermaid": "",
    }
    return cw_testlib.tool_call("submit_analysis", analysis)


def _combined_script(ask_handler):
    def script(body, n):
        model = body.get("model")
        if model == "prose-m":
            return _small_route_reply(body)
        if model == "ask-m":
            return ask_handler(body, n)
        raise RuntimeError(f"unexpected model {model!r}")
    return script


def _write_config(home, stub, **limits):
    cw_testlib.write_config(
        home,
        {"a": stub.profile("analysis-m"), "p": stub.profile("prose-m"), "k": stub.profile("ask-m")},
        {"analysis": "a", "prose": "p", "ask": "k"},
        **limits,
    )


def _build_done(home, tmp, stub, **limits):
    """A one-line-change repo, built end to end on the small route, with no PR -- enough of a
    walkthrough for cw_ask to read raw.diff and analysis.json from."""
    repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\nb\nc\n"}, {"foo.py": "a\nX\nc\n"})
    _write_config(home, stub, **limits)
    d, _meta, _reused = cw_run.prepare_walkthrough(
        {"repo": str(repo), "base": base, "head": head, "target": "t", "slug": "t"})
    status = cw_run.run(d, lambda ev, data: None)
    assert status == "done", status
    return d


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


def _post_ask(daemon, key, wid, anchor, question, *, token=None):
    return _request(daemon, "POST", f"/api/walkthrough/{key}/{wid}/ask",
                     token=daemon.token if token is None else token,
                     body=json.dumps({"anchor": anchor, "question": question}))


def _rpc(daemon, tool, args):
    status, data = _request(daemon, "POST", "/api/rpc", token=daemon.token,
                             body=json.dumps({"tool": tool, "args": args}))
    return status, (json.loads(data) if data else None)


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


# ---------------------------------------------------------------------------
# An ask produces a qa.jsonl record and an SSE answer
# ---------------------------------------------------------------------------

def test_ask_gives_qa_record_and_sse_answer():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        def ask_handler(body, n):
            if n == 0:
                return cw_testlib.tool_call("read_file", {"path": "foo.py"})
            return cw_testlib.text("It assigns X on line 2.")

        with cw_testlib.StubLLM(_combined_script(ask_handler)) as stub:
            d = _build_done(home, tmp, stub)
            key, wid = d.parent.name, d.name
            with running_daemon() as daemon:
                sock = _sse_connect(daemon, key, wid)
                try:
                    snap = _sse_read_until(sock, b"event: snapshot")
                    assert b"event: snapshot" in snap, snap
                    status, raw = _post_ask(daemon, key, wid, ANCHOR_LINE, "why X?")
                    assert status == 202, (status, raw)
                    assert json.loads(raw)["qid"].startswith("q-")
                    more = _sse_read_until(sock, b"event: answer")
                    assert b"event: answer" in more, more
                finally:
                    sock.close()

        qa = cw_ask.read_qa(d)
        assert len(qa) == 1
        assert qa[0]["status"] == "ok"
        assert qa[0]["answer"]
        meta = cw_store.read_meta(d)
        assert meta["usage"]["ask"]["calls"] == 2


# ---------------------------------------------------------------------------
# Oversized selection refused, quote never cut
# ---------------------------------------------------------------------------

def test_oversized_selection_refused_quote_never_cut():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(_combined_script(lambda body, n: cw_testlib.text("x"))) as stub:
            d = _build_done(home, tmp, stub)

            big_quote = "q" * 17000
            anchor = cw_ask.validate_anchor({"kind": "section", "section": "Overview", "quote": big_quote})
            try:
                cw_ask.build_prompt(d, anchor, "why?")
                raise AssertionError("expected CWError")
            except cw_store.CWError as e:
                assert e.remedy == "select a smaller range"

            key, wid = d.parent.name, d.name
            with running_daemon() as daemon:
                status, raw = _post_ask(daemon, key, wid,
                                         {"kind": "section", "section": "Overview", "quote": big_quote}, "why?")
                assert status == 400, (status, raw)
                assert json.loads(raw)["remedy"] == "select a smaller range"

            analysis = json.loads((d / "analysis.json").read_text())
            analysis["overview"] = "o" * 5000
            (d / "analysis.json").write_text(json.dumps(analysis))

            quote = "q" * 15000
            anchor2 = cw_ask.validate_anchor({"kind": "section", "section": "Overview", "quote": quote})
            prompt = cw_ask.build_prompt(d, anchor2, "why?")
            assert len(prompt) <= cw_ask.PROMPT_CAP
            assert quote in prompt


# ---------------------------------------------------------------------------
# Prompt holds hunk, notes and at most 5 prior pairs under 16k, in order
# ---------------------------------------------------------------------------

def test_prompt_order_hunk_notes_and_pair_limit():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(_combined_script(lambda body, n: cw_testlib.text("x"))) as stub:
            d = _build_done(home, tmp, stub)

            for i in range(7):
                cw_ask._append_qa(d, {
                    "qid": f"q-{i}", "started_at": "t", "finished_at": "t", "anchor": {},
                    "question": f"question-{i}", "status": "ok", "answer": f"answer-{i}",
                    "error": None, "remedy": None, "profile": "k", "model": "ask-m", "usage": {},
                })

            anchor = cw_ask.validate_anchor(dict(ANCHOR_LINE))
            prompt = cw_ask.build_prompt(d, anchor, "why is X here?")

            assert len(prompt) < cw_ask.PROMPT_CAP
            assert "question-0" not in prompt
            assert "question-1" not in prompt
            for i in range(2, 7):
                assert f"question-{i}" in prompt

            i_question = prompt.index("why is X here?")
            i_hunk = prompt.index("Diff context")
            i_note = prompt.index("Hunk note")
            i_verdict = prompt.index("Verdict:")
            i_first_pair = prompt.index("question-2")
            assert i_question < i_hunk < i_note < i_verdict < i_first_pair


# ---------------------------------------------------------------------------
# answer() never raises, even when the ask profile itself is malformed
# ---------------------------------------------------------------------------

def test_answer_never_raises_on_a_malformed_ask_profile():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(_combined_script(lambda body, n: cw_testlib.text("x"))) as stub:
            d = _build_done(home, tmp, stub)
            config_path = cw_store.config_path()
            config = json.loads(config_path.read_text())
            config["profiles"]["k"] = {"model": "ask-m"}  # missing base_url: role_profile raises
            config_path.write_text(json.dumps(config))

            anchor = cw_ask.validate_anchor(dict(ANCHOR_SECTION))
            record = cw_ask.answer(d, "q-bad-profile", anchor, "why?", on_event=None)
            assert record["status"] == "error"
            assert record["error"]


# ---------------------------------------------------------------------------
# Two asks on one walkthrough run one after the other
# ---------------------------------------------------------------------------

def test_two_asks_run_sequentially():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        def ask_handler(body, n):
            return cw_testlib.delayed(0.5, cw_testlib.text("ans"))

        with cw_testlib.StubLLM(_combined_script(ask_handler)) as stub:
            d = _build_done(home, tmp, stub)
            key, wid = d.parent.name, d.name
            with running_daemon() as daemon:
                status1, _ = _post_ask(daemon, key, wid, ANCHOR_SECTION, "q1")
                assert status1 == 202
                status2, _ = _post_ask(daemon, key, wid, ANCHOR_SECTION, "q2")
                assert status2 == 202

                deadline = time.monotonic() + 10
                while len(cw_ask.read_qa(d)) < 2 and time.monotonic() < deadline:
                    time.sleep(0.05)

            records = cw_ask.read_qa(d)
            assert len(records) == 2, records
            first, second = records[0], records[1]
            assert first["finished_at"] <= second["started_at"]


# ---------------------------------------------------------------------------
# A hung endpoint is cut at timeout_s; the record is error with a remedy
# ---------------------------------------------------------------------------

def test_hung_endpoint_cut_at_timeout():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        def ask_handler(body, n):
            return cw_testlib.delayed(3, cw_testlib.text("ans"))

        with cw_testlib.StubLLM(_combined_script(ask_handler)) as stub:
            d = _build_done(home, tmp, stub, timeout_s=1)
            anchor = cw_ask.validate_anchor(dict(ANCHOR_SECTION))
            record = cw_ask.answer(d, "q-test", anchor, "why?", on_event=None)
            assert record["status"] == "error"
            assert "timeout_s" in (record["remedy"] or "")


# ---------------------------------------------------------------------------
# Guards: no token 403, bad anchor 400, oversize 413; walkthrough_get surfaces qa
# ---------------------------------------------------------------------------

def test_guards_and_walkthrough_get_qa():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(_combined_script(lambda body, n: cw_testlib.text("x"))) as stub:
            d = _build_done(home, tmp, stub)
            key, wid = d.parent.name, d.name
            with running_daemon() as daemon:
                status, _ = _post_ask(daemon, key, wid, ANCHOR_SECTION, "q", token="")
                assert status == 403

                status, _ = _request(daemon, "GET", f"/api/walkthrough/{key}/{wid}/qa", token=None)
                assert status == 403

                status, raw = _post_ask(daemon, key, wid, {"kind": "nonsense"}, "q")
                assert status == 400, (status, raw)

                status, _ = _request(
                    daemon, "POST", f"/api/walkthrough/{key}/{wid}/ask",
                    token=daemon.token, body="x" * (cw_server.MAX_BODY + 1))
                assert status == 413

                cw_ask._append_qa(d, {
                    "qid": "q-abc", "started_at": "t", "finished_at": "t", "anchor": {},
                    "question": "hi", "status": "ok", "answer": "there", "error": None,
                    "remedy": None, "profile": "k", "model": "ask-m", "usage": {},
                })
                status, payload = _rpc(daemon, "walkthrough_get", {"id": wid, "key": key, "parts": ["qa"]})
                assert status == 200
                qa_list = payload["result"]["qa"]
                assert any(q["qid"] == "q-abc" and q["answer"] == "there" for q in qa_list)

                status, raw = _request(daemon, "GET", f"/api/walkthrough/{key}/{wid}/qa", token=daemon.token)
                assert status == 200
                full = json.loads(raw)["qa"]
                assert any(r["qid"] == "q-abc" and r["profile"] == "k" for r in full)


def test_idle_exit_waits_for_in_flight_ask():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        release = threading.Event()

        def ask_handler(body, n):
            release.wait(timeout=5)
            return cw_testlib.text("ans")

        with cw_testlib.StubLLM(_combined_script(ask_handler)) as stub:
            d = _build_done(home, tmp, stub)
            key, wid = d.parent.name, d.name
            daemon = cw_server.Daemon(idle_s=1, write_server_json=False)
            daemon.start()
            try:
                status, _ = _post_ask(daemon, key, wid, ANCHOR_SECTION, "why?")
                assert status == 202
                time.sleep(1.5)
                assert not daemon._stopped.is_set(), "daemon stopped while an ask was in flight"
                release.set()
                deadline = time.monotonic() + 5
                while not daemon._stopped.is_set() and time.monotonic() < deadline:
                    time.sleep(0.1)
                assert daemon._stopped.is_set()
            finally:
                release.set()
                daemon.stop()


# ---------------------------------------------------------------------------
# ask role on the claude-code backend
# ---------------------------------------------------------------------------

def test_ask_on_claude_code_sums_usage_and_omits_json_schema():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(_combined_script(lambda body, n: _small_route_reply(body))) as stub:
            cw_testlib.write_config(
                home,
                {"a": stub.profile("analysis-m"), "p": stub.profile("prose-m"),
                 "k": {"kind": "claude-code", "model": "sonnet"}},
                {"analysis": "a", "prose": "p", "ask": "k"},
            )
            repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\nb\nc\n"}, {"foo.py": "a\nX\nc\n"})
            d, _meta, _reused = cw_run.prepare_walkthrough(
                {"repo": str(repo), "base": base, "head": head, "target": "t", "slug": "t"})
            status = cw_run.run(d, lambda ev, data: None)
            assert status == "done", status

        with cw_testlib.fake_claude(tmp, {"sonnet": [cw_testlib.claude_result(
                result="It assigns X on line 2.",
                usage={"input_tokens": 7, "output_tokens": 3,
                       "cache_creation_input_tokens": 2, "cache_read_input_tokens": 1})]}) as fc:
            anchor = cw_ask.validate_anchor(dict(ANCHOR_SECTION))
            record = cw_ask.answer(d, "q-claude", anchor, "why?", on_event=None)

        assert record["status"] == "ok"
        assert record["answer"] == "It assigns X on line 2."
        assert record["usage"]["prompt_tokens"] == 7 + 2 + 1
        assert record["usage"]["completion_tokens"] == 3
        assert record["usage"]["cost_usd"] is None
        entry = fc.log()[-1]
        assert not any(a.startswith("--json-schema") for a in entry["argv"])
        assert Path(entry["cwd"]).resolve() == (d / "head").resolve()


if __name__ == "__main__":
    tests = [
        test_ask_gives_qa_record_and_sse_answer,
        test_oversized_selection_refused_quote_never_cut,
        test_prompt_order_hunk_notes_and_pair_limit,
        test_answer_never_raises_on_a_malformed_ask_profile,
        test_two_asks_run_sequentially,
        test_hung_endpoint_cut_at_timeout,
        test_guards_and_walkthrough_get_qa,
        test_idle_exit_waits_for_in_flight_ask,
        test_ask_on_claude_code_sums_usage_and_omits_json_schema,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
