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
import cw_mcp  # noqa: E402
import cw_run  # noqa: E402
import cw_server  # noqa: E402
import cw_store  # noqa: E402
import cw_testlib  # noqa: E402

cw_llm.BACKOFF_S = [0, 0]

ANCHOR_LINE = {"kind": "line", "path": "foo.py", "side": "RIGHT", "line": 2,
               "end_line": None, "hunk_id": "foo.py\t@@ -1,3 +1,3 @@", "quote": "X"}
ANCHOR_SECTION = {"kind": "section", "section": "Overview", "quote": "q"}
ANCHOR_BLOCK = {"kind": "block", "block": "overview:p1", "section": "Overview", "quote": "q"}


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


def _post_comment(daemon, key, wid, anchor, text, thread_id=None, *, token=None):
    body = {"anchor": anchor, "text": text}
    if thread_id:
        body["thread_id"] = thread_id
    return _request(daemon, "POST", f"/api/walkthrough/{key}/{wid}/comment",
                     token=daemon.token if token is None else token, body=json.dumps(body))


def _wait_final(d, count, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        turns = cw_ask.read_qa(d)
        if len(turns) >= count and all(t["status"] != "pending" for t in turns):
            break
        time.sleep(0.05)
    return cw_ask.read_qa(d)


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
                    more = _sse_read_until(sock, b'"status": "ok"')
                    assert b"event: thread" in more, more
                    assert b'"kind": "turn"' in more and b'"status": "pending"' in more, more
                    assert b'"status": "ok"' in more, more
                finally:
                    sock.close()

        qa = cw_ask.read_qa(d)
        assert len(qa) == 1
        assert qa[0]["status"] == "ok"
        assert qa[0]["answer"]
        meta = cw_store.read_meta(d)
        assert meta["usage"]["thread"]["calls"] == 2


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
                    "qid": f"q-{i}", "thread_id": "t1", "started_at": "t", "finished_at": "t", "anchor": {},
                    "comment": f"question-{i}", "question": f"question-{i}", "status": "ok", "answer": f"answer-{i}",
                    "error": None, "remedy": None, "profile": "k", "model": "ask-m", "usage": {},
                })

            anchor = cw_ask.validate_anchor(dict(ANCHOR_LINE))
            prompt = cw_ask.build_prompt(d, anchor, "why is X here?", "t1")

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


# Same-thread comments run one after the other, different threads side by side
# ---------------------------------------------------------------------------

def test_same_thread_comments_run_sequentially():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        def ask_handler(body, n):
            return cw_testlib.delayed(0.5, cw_testlib.text("ans"))

        with cw_testlib.StubLLM(_combined_script(ask_handler)) as stub:
            d = _build_done(home, tmp, stub)
            key, wid = d.parent.name, d.name
            with running_daemon() as daemon:
                status1, raw1 = _post_comment(daemon, key, wid, ANCHOR_BLOCK, "q1")
                assert status1 == 202
                tid = json.loads(raw1)["thread_id"]
                status2, raw2 = _post_comment(daemon, key, wid, ANCHOR_BLOCK, "q2", tid)
                assert status2 == 202
                assert json.loads(raw2)["thread_id"] == tid
                records = _wait_final(d, 2)

            assert len(records) == 2, records
            first, second = records
            assert first["finished_at"] <= second["started_at"]
            assert stub.peak_in_flight == 1


def test_different_threads_run_concurrently():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        def ask_handler(body, n):
            return cw_testlib.delayed(0.8, cw_testlib.text("ans"))

        with cw_testlib.StubLLM(_combined_script(ask_handler)) as stub:
            d = _build_done(home, tmp, stub)
            key, wid = d.parent.name, d.name
            with running_daemon() as daemon:
                assert _post_comment(daemon, key, wid, ANCHOR_BLOCK, "q1")[0] == 202
                assert _post_comment(daemon, key, wid, ANCHOR_BLOCK, "q2")[0] == 202
                records = _wait_final(d, 2)

            assert len(records) == 2 and records[0]["thread_id"] != records[1]["thread_id"]
            assert stub.peak_in_flight == 2


def test_comment_is_persisted_pending_before_turn_starts():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        release = threading.Event()

        def ask_handler(body, n):
            release.wait(timeout=5)
            return cw_testlib.text("ans")

        with cw_testlib.StubLLM(_combined_script(ask_handler)) as stub:
            d = _build_done(home, tmp, stub)
            key, wid = d.parent.name, d.name
            with running_daemon() as daemon:
                try:
                    status, raw = _post_comment(daemon, key, wid, ANCHOR_BLOCK, "hold on")
                    assert status == 202
                    qid = json.loads(raw)["qid"]
                    turns = cw_ask.read_qa(d)
                    assert [(t["qid"], t["status"], t["comment"], t["question"]) for t in turns] == [
                        (qid, "pending", "hold on", "hold on")]
                    assert turns[0]["thread_id"] == qid
                    status, raw = _request(daemon, "GET", f"/api/walkthrough/{key}/{wid}/qa",
                                            token=daemon.token)
                    assert json.loads(raw) == {"turns": turns, "resolved": [], "outcomes": []}
                finally:
                    release.set()
                records = _wait_final(d, 1)
            assert records[0]["status"] == "ok" and records[0]["answer"] == "ans"


# ---------------------------------------------------------------------------
# Anchors, per-thread history, folding, resolve
# ---------------------------------------------------------------------------

def test_block_anchor_accepted_and_bad_key_refused():
    anchor = cw_ask.validate_anchor(dict(ANCHOR_BLOCK))
    assert anchor == {"kind": "block", "block": "overview:p1", "section": "Overview", "quote": "q"}
    assert cw_ask.validate_anchor({"kind": "block", "block": "a.b|c-d_e", "quote": "q"})["section"] is None
    for bad in ({"kind": "block", "block": "has space", "quote": "q"},
                {"kind": "block", "block": "", "quote": "q"},
                {"kind": "block", "block": "x" * 201, "quote": "q"},
                {"kind": "block", "quote": "q"},
                {"kind": "block", "block": "ok", "quote": ""},
                {"kind": "block", "block": "ok", "section": 3, "quote": "q"}):
        try:
            cw_ask.validate_anchor(bad)
            raise AssertionError(f"expected CWError for {bad}")
        except cw_store.CWError as e:
            assert str(e).startswith("bad anchor")


def _turn(qid, comment, answer, thread_id=None, status="ok"):
    record = {"qid": qid, "anchor": {}, "question": comment, "status": status, "answer": answer}
    if thread_id:
        record.update(thread_id=thread_id, comment=comment)
    return record


def test_prompt_history_is_per_thread_and_ignores_unfinished_turns():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(_combined_script(lambda body, n: cw_testlib.text("x"))) as stub:
            d = _build_done(home, tmp, stub)
            cw_ask._append_qa(d, _turn("q-a1", "mine-1", "ans-mine-1", "t-a"))
            cw_ask._append_qa(d, _turn("q-b1", "theirs-1", "ans-theirs", "t-b"))
            cw_ask._append_qa(d, _turn("q-a2", "mine-2", None, "t-a", status="pending"))
            cw_ask._append_qa(d, _turn("q-a3", "mine-err", None, "t-a", status="error"))
            anchor = cw_ask.validate_anchor(dict(ANCHOR_BLOCK))

            prompt = cw_ask.build_prompt(d, anchor, "next", "t-a")
            assert "Section: Overview" in prompt
            assert "mine-1" in prompt and "ans-mine-1" in prompt
            assert "theirs-1" not in prompt
            assert "mine-2" not in prompt and "mine-err" not in prompt
            assert "Previous Q&A" not in cw_ask.build_prompt(d, anchor, "next")


def test_old_record_without_thread_id_folds_as_its_own_thread():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(_combined_script(lambda body, n: cw_testlib.text("x"))) as stub:
            d = _build_done(home, tmp, stub)
            cw_ask._append_qa(d, _turn("q-old", "legacy", "old answer"))
            cw_ask._append_qa(d, {"qid": "q-new", "thread_id": "q-new", "comment": "c",
                                  "question": "c", "status": "pending"})
            cw_ask._append_qa(d, {"qid": "q-new", "thread_id": "q-new", "comment": "c",
                                  "question": "c", "status": "ok", "answer": "a"})
            turns = cw_ask.read_qa(d)
            assert [t["qid"] for t in turns] == ["q-old", "q-new"]
            assert turns[0]["thread_id"] == "q-old" and turns[0]["comment"] == "legacy"
            assert turns[1]["status"] == "ok"
            anchor = cw_ask.validate_anchor(dict(ANCHOR_BLOCK))
            assert "legacy" in cw_ask.build_prompt(d, anchor, "more", "q-old")
            assert "legacy" not in cw_ask.build_prompt(d, anchor, "more", "q-new")


def test_read_threads_folds_resolve_events_and_readers_skip_them():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(_combined_script(lambda body, n: cw_testlib.text("x"))) as stub:
            d = _build_done(home, tmp, stub)
            cw_ask._append_qa(d, _turn("q-1", "c1", "a1", "q-1"))
            cw_ask._append_qa(d, _turn("q-2", "c2", "a2", "q-1"))
            cw_ask._append_qa(d, _turn("q-3", "c3", "a3", "q-3"))
            cw_ask.append_resolve(d, "q-1", True)
            cw_ask.append_resolve(d, "q-3", True)
            cw_ask.append_resolve(d, "q-3", False)
            threads = cw_ask.read_threads(d)
            assert [t["qid"] for t in threads["q-1"]["turns"]] == ["q-1", "q-2"]
            assert threads["q-1"]["resolved"] is True
            assert threads["q-3"]["resolved"] is False
            assert all("type" not in t for t in cw_ask.read_qa(d))
            assert len(cw_ask.read_qa(d)) == 3


def test_resolve_route_guards_and_effect():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(_combined_script(lambda body, n: cw_testlib.text("x"))) as stub:
            d = _build_done(home, tmp, stub)
            key, wid = d.parent.name, d.name
            cw_ask._append_qa(d, _turn("q-1", "c1", "a1", "q-1"))
            path = f"/api/walkthrough/{key}/{wid}/threads/q-1/resolve"
            body = json.dumps({"resolved": True})
            with running_daemon() as daemon:
                sock = _sse_connect(daemon, key, wid)
                try:
                    _sse_read_until(sock, b"event: snapshot")
                    assert _request(daemon, "POST", path, body=body)[0] == 403
                    assert _request(daemon, "POST", path, token="wrong", body=body)[0] == 403
                    assert _request(daemon, "POST", path, token=daemon.token, host="evil.example:9",
                                     body=body)[0] == 403
                    assert _request(daemon, "POST", path, token=daemon.token,
                                     origin="http://attacker.example", body=body)[0] == 403
                    assert cw_ask.read_threads(d)["q-1"]["resolved"] is False

                    bad = f"/api/walkthrough/{key}/{wid}/threads/bad.id/resolve"
                    assert _request(daemon, "POST", bad, token=daemon.token, body=body)[0] == 400
                    missing = f"/api/walkthrough/{key}/{wid}/threads/q-nope/resolve"
                    assert _request(daemon, "POST", missing, token=daemon.token, body=body)[0] == 404
                    assert _request(daemon, "POST", path, token=daemon.token,
                                     body=json.dumps({"resolved": "yes"}))[0] == 400

                    status, raw = _request(daemon, "POST", path, token=daemon.token,
                                            origin=f"http://127.0.0.1:{daemon.port}", body=body)
                    assert (status, json.loads(raw)) == (200, {"ok": True})
                    events = _sse_read_until(sock, b'"kind": "resolve"')
                    assert b'"thread_id": "q-1"' in events and b'"resolved": true' in events, events
                finally:
                    sock.close()
                status, raw = _request(daemon, "GET", f"/api/walkthrough/{key}/{wid}/qa", token=daemon.token)
                assert json.loads(raw)["resolved"] == ["q-1"]


def test_stale_pending_becomes_error_but_in_flight_stays_pending():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        release = threading.Event()

        def ask_handler(body, n):
            release.wait(timeout=5)
            return cw_testlib.text("ans")

        with cw_testlib.StubLLM(_combined_script(ask_handler)) as stub:
            d = _build_done(home, tmp, stub)
            key, wid = d.parent.name, d.name
            cw_ask._append_qa(d, {"qid": "q-dead", "thread_id": "q-dead", "anchor": ANCHOR_BLOCK,
                                  "comment": "c", "question": "c", "started_at": "t0", "status": "pending"})
            with running_daemon() as daemon:
                try:
                    status, raw = _post_comment(daemon, key, wid, ANCHOR_BLOCK, "live")
                    live = json.loads(raw)["qid"]
                    status, raw = _request(daemon, "GET", f"/api/walkthrough/{key}/{wid}/qa",
                                            token=daemon.token)
                    by_qid = {t["qid"]: t for t in json.loads(raw)["turns"]}
                    assert by_qid[live]["status"] == "pending"
                    dead = by_qid["q-dead"]
                    assert (dead["status"], dead["error"], dead["remedy"]) == (
                        "error", "turn was interrupted", "send the comment again")
                    assert dead["anchor"] == ANCHOR_BLOCK and dead["started_at"] == "t0" and dead["finished_at"]
                finally:
                    release.set()
                _wait_final(d, 2)


def test_follow_up_keeps_first_turn_anchor():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(_combined_script(lambda body, n: cw_testlib.text("ans"))) as stub:
            d = _build_done(home, tmp, stub)
            key, wid = d.parent.name, d.name
            with running_daemon() as daemon:
                tid = json.loads(_post_comment(daemon, key, wid, ANCHOR_BLOCK, "q1")[1])["thread_id"]
                _wait_final(d, 1)
                assert _post_comment(daemon, key, wid, ANCHOR_SECTION, "q2", tid)[0] == 202
                records = _wait_final(d, 2)
            assert [r["anchor"] for r in records] == [ANCHOR_BLOCK, ANCHOR_BLOCK]


def test_comment_on_resolved_thread_reopens_it_and_emits_resolve():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(_combined_script(lambda body, n: cw_testlib.text("ans"))) as stub:
            d = _build_done(home, tmp, stub)
            key, wid = d.parent.name, d.name
            cw_ask._append_qa(d, _turn("q-1", "c1", "a1", "q-1"))
            cw_ask.append_resolve(d, "q-1", True)
            with running_daemon() as daemon:
                sock = _sse_connect(daemon, key, wid)
                try:
                    _sse_read_until(sock, b"event: snapshot")
                    assert _post_comment(daemon, key, wid, ANCHOR_BLOCK, "again", "q-1")[0] == 202
                    events = _sse_read_until(sock, b'"resolved": false')
                    assert b'"kind": "resolve"' in events and b'"thread_id": "q-1"' in events, events
                finally:
                    sock.close()
                _wait_final(d, 2)
            assert cw_ask.read_threads(d)["q-1"]["resolved"] is False


def test_comment_route_guards_and_unknown_thread():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(_combined_script(lambda body, n: cw_testlib.text("x"))) as stub:
            d = _build_done(home, tmp, stub)
            key, wid = d.parent.name, d.name
            with running_daemon() as daemon:
                assert _post_comment(daemon, key, wid, ANCHOR_BLOCK, "c", token="")[0] == 403
                assert _post_comment(daemon, key, wid, {"kind": "block", "block": "no good", "quote": "q"},
                                      "c")[0] == 400
                assert _post_comment(daemon, key, wid, ANCHOR_BLOCK, "")[0] == 400
                assert _post_comment(daemon, key, wid, ANCHOR_BLOCK, "x" * 2001)[0] == 400
                assert _post_comment(daemon, key, wid, ANCHOR_BLOCK, "c", "q-nope")[0] == 404
                assert _post_comment(daemon, key, wid, ANCHOR_BLOCK, "c", "bad id")[0] == 400
                status, _ = _request(daemon, "POST", f"/api/walkthrough/{key}/{wid}/comment",
                                      token=daemon.token, origin="http://attacker.example",
                                      body=json.dumps({"anchor": ANCHOR_BLOCK, "text": "c"}))
                assert status == 403
                assert cw_ask.read_qa(d) == []


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
                full = json.loads(raw)["turns"]
                assert any(r["qid"] == "q-abc" and r["profile"] == "k" and r["thread_id"] == "q-abc"
                           and r["comment"] == "hi" for r in full)


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

def _claude_done(home, tmp, roles=None, profiles=None):
    with cw_testlib.StubLLM(_combined_script(lambda body, n: _small_route_reply(body))) as stub:
        d = _build_done(home, tmp, stub)
    cw_testlib.write_config(
        home, profiles or {"k": {"kind": "claude-code", "model": "sonnet"}}, roles or {"ask": "k"})
    return d


def _cturn(d, qid, comment, thread_id=None, live=None, anchor=None, on_event=None):
    events = []
    record = cw_ask.answer(
        d, qid, cw_ask.validate_anchor(dict(anchor or ANCHOR_SECTION)), comment,
        on_event=lambda kind, data: (events.append((kind, data)), on_event and on_event(kind, data)),
        thread_id=thread_id, live=live)
    return record, events


def test_ask_on_claude_code_sums_usage_and_omits_json_schema():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        usage = {"input_tokens": 7, "output_tokens": 3,
                 "cache_creation_input_tokens": 2, "cache_read_input_tokens": 1}
        with cw_testlib.fake_claude(tmp, {"sonnet": [cw_testlib.claude_stream_entry(
                ["It assigns X ", "on line 2."], session_id="s-1", usage=usage)]}) as fc:
            record, _events = _cturn(d, "q-claude", "why?")

        assert record["status"] == "ok"
        assert record["answer"] == "It assigns X on line 2."
        assert record["session_id"] == "s-1"
        assert record["usage"]["prompt_tokens"] == 7 + 2 + 1
        assert record["usage"]["completion_tokens"] == 3
        assert record["usage"]["cost_usd"] is None
        entry = fc.log()[-1]
        assert not any(a.startswith("--json-schema") for a in entry["argv"])
        assert "--safe-mode" not in entry["argv"]
        assert any(a.startswith("--session-id") or a == "--session-id" for a in entry["argv"])
        assert Path(entry["cwd"]).resolve() == (d / "head").resolve()
        assert (d / "ctx" / "analysis.json").is_file() and (d / "ctx" / "raw.diff").is_file()
        assert cw_store.read_json(d / "meta.json")["usage"]["thread"]["calls"] == 1


def test_claude_deltas_are_ordered_and_coalesced():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        chunks = [f"w{i} " for i in range(40)]
        with cw_testlib.fake_claude(tmp, {"sonnet": [
                cw_testlib.claude_stream_entry(chunks, delay=0.005)]}):
            live = {}
            record, events = _cturn(d, "q1", "why?", live=live)
        deltas = [data for kind, data in events if kind == "delta"]
        assert 0 < len(deltas) < len(chunks), len(deltas)
        assert [x["seq"] for x in deltas] == list(range(1, len(deltas) + 1))
        assert "".join(x["text"] for x in deltas) == "".join(chunks)
        assert all(x["qid"] == "q1" and x["thread_id"] == "q1" for x in deltas)
        assert live["text"] == record["answer"] == "".join(chunks)
        assert events[-1][0] == "thread"


def test_claude_live_text_and_seq_move_with_each_delta():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        chunks = [f"w{i} " for i in range(40)]
        live, seen, bad = {}, [], []

        def hook(kind, data):
            if kind == "delta":
                seen.append(data["text"])
                if live.get("text") != "".join(seen) or live.get("seq") != data["seq"]:
                    bad.append(data["seq"])

        with cw_testlib.fake_claude(tmp, {"sonnet": [
                cw_testlib.claude_stream_entry(chunks, delay=0.005)]}):
            record, _ = _cturn(d, "q1", "why?", live=live, on_event=hook)
        assert seen and not bad, bad
        assert record["answer"] == "".join(chunks)


def test_claude_answer_joins_text_blocks_and_reports_progress():
    def block(text_):
        return [{"type": "stream_event", "event": {
                    "type": "content_block_start", "index": 0, "content_block": {"type": "text"}}},
                {"type": "stream_event", "event": {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": text_}}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": text_}]}}]

    entry = cw_testlib.claude_stream_entry(["x"], result="After.")
    entry["stream"] = (
        entry["stream"][:1] + block("Before.")
        + [{"type": "assistant", "message": {"content": [{
            "type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/a/b.py"}}]}}]
        + block("After.") + entry["stream"][-1:])
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}):
            record, events = _cturn(d, "q1", "why?")
    assert record["answer"] == "Before.\n\nAfter."
    assert "".join(x["text"] for k, x in events if k == "delta") == "Before.\n\nAfter."
    progress = [x for k, x in events if k == "progress"]
    assert [(x["tool"], x["detail"]) for x in progress] == [("Read", "/a/b.py")]


def test_claude_followup_resumes_and_failed_resume_reseeds():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        script = {"sonnet": [
            cw_testlib.claude_stream_entry(["one"], session_id="s-1"),
            cw_testlib.claude_stream_entry(["two"], session_id="s-1"),
            {"stream": [], "exit": 1, "stderr": "no conversation found"},
            cw_testlib.claude_stream_entry(["three"], session_id="s-2"),
        ]}
        with cw_testlib.fake_claude(tmp, script) as fc:
            _cturn(d, "q1", "first?")
            record, _ = _cturn(d, "q2", "second?", thread_id="q1")
            assert record["answer"] == "two"
            resumed = fc.log()[1]
            assert resumed["argv"][resumed["argv"].index("--resume") + 1] == "s-1"
            assert "Previous Q&A" not in resumed["stdin"]
            assert "second?" in resumed["stdin"]

            record, _ = _cturn(d, "q3", "third?", thread_id="q1")
        assert record["status"] == "ok" and record["answer"] == "three"
        assert record["session_id"] == "s-2"
        reseeded = fc.log()[3]
        assert "--resume" not in reseeded["argv"] and "--session-id" in reseeded["argv"]
        assert "Previous Q&A" in reseeded["stdin"]
        assert "reseeded" in cw_store.log_path().read_text()


def test_thread_role_wins_over_ask_and_falls_back():
    profiles = {"k": {"kind": "claude-code", "model": "ask-m"},
                "t": {"kind": "claude-code", "model": "thr-m"}}
    script = {"ask-m": [cw_testlib.claude_stream_entry(["from ask"])],
              "thr-m": [cw_testlib.claude_stream_entry(["from thread"])]}
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp, roles={"ask": "k", "thread": "t"}, profiles=profiles)
        with cw_testlib.fake_claude(tmp, script):
            assert _cturn(d, "q1", "why?")[0]["answer"] == "from thread"
        cw_testlib.write_config(home, profiles, {"ask": "k"})
        with cw_testlib.fake_claude(tmp, script):
            assert _cturn(d, "q2", "why?")[0]["answer"] == "from ask"


def test_claude_aborts_fast_when_outcome_server_missing():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        entry = cw_testlib.claude_stream_entry(["a", "b"], init_cw=False, delay=10)
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}):
            started = time.time()
            record, _ = _cturn(d, "q1", "why?")
        assert time.time() - started < 5
        assert record["status"] == "error"
        assert record["error"] == "the outcome server did not start"
        assert record["remedy"] == "see server.log"


def test_claude_cancel_keeps_partial_text():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        entry = cw_testlib.claude_stream_entry(["part ", "two ", "three"], delay=3)
        live = {}

        def cancel():
            deadline = time.time() + 20
            while not live.get("text") and time.time() < deadline:
                time.sleep(0.02)
            live["cancelled"] = True
            cw_ask._kill(live["proc"])

        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}):
            t = threading.Thread(target=cancel)
            t.start()
            record, events = _cturn(d, "q1", "why?", live=live)
            t.join()
        assert record["status"] == "cancelled"
        assert record["answer"] == "part "
        assert record["error"] is None


def _seed_review_threads(d, **overrides):
    note = {"id": "gh-501", "origin": "github", "gh_thread_id": "T1", "path": "foo.py", "line": 2,
            "author": "rev", "body": "please fix", "state": "posted", **overrides}
    reply = {**note, "id": "gh-502", "in_reply_to": "gh-501"}
    cw_store.write_json(d / "state.json", {"notes": [note, reply]})


def _propose(thread="$FIRST_RESOLVABLE", at=1):
    return [{"at": at, "name": "propose_resolve", "arguments": {"thread": thread, "why": "foo.py:2 now assigns X"}}]


def test_resolvable_is_for_line_anchors_over_open_root_review_threads():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        _seed_review_threads(d)
        found = cw_ask._resolvable(d, ANCHOR_LINE)
        assert [(c["id"], c["path"], c["line"], c["author"], c["body"]) for c in found] == [
            ("gh-501", "foo.py", 2, "rev", "please fix")]
        assert cw_ask._resolvable(d, ANCHOR_SECTION) == cw_ask._resolvable(d, ANCHOR_BLOCK) == []
        assert cw_ask._resolvable(d, {**ANCHOR_LINE, "line": 3}) == []
        assert len(cw_ask._resolvable(d, {**ANCHOR_LINE, "line": 1, "end_line": 2})) == 1
        _seed_review_threads(d, resolved=True)
        assert cw_ask._resolvable(d, ANCHOR_LINE) == []


def test_proposed_resolve_lands_as_outcome_before_the_final_thread_event():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        _seed_review_threads(d)
        entry = cw_testlib.claude_stream_entry(["fixed"], mcp_calls=_propose())
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}) as fc:
            record, events = _cturn(d, "q1", "ok?", anchor=ANCHOR_LINE)
        assert record["status"] == "ok"
        kinds = [k for k, _ in events]
        assert kinds.count("outcome") == 1 and kinds.index("outcome") < kinds.index("thread")
        outcome = dict(events)["outcome"]
        assert outcome["payload"]["thread"] == "gh-501" and outcome["state"] == "proposed"
        assert "gh-501, foo.py:2, rev" in fc.log()[-1]["stdin"]
        assert cw_ask.read_threads(d)["q1"]["outcomes"] == [outcome]
        assert [r for r in cw_ask._read_records(d) if r.get("type") == "outcome"][0]["qid"] == "q1"
        cw_ask.sweep_outcomes(d, "q1", "q1")
        assert len(cw_ask.read_outcomes(d)) == 1


def test_resolve_for_a_non_candidate_thread_is_refused_and_records_nothing():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        _seed_review_threads(d)
        entry = cw_testlib.claude_stream_entry(["no"], mcp_calls=_propose("gh-999"))
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}):
            record, events = _cturn(d, "q1", "ok?", anchor=ANCHOR_LINE)
        assert record["status"] == "ok"
        assert cw_ask.read_outcomes(d) == [] and "outcome" not in [k for k, _ in events]
        assert not (d / "turns" / "q1.outcomes.jsonl").exists()


def test_openai_turn_records_propose_resolve_and_keeps_looping():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        def ask_handler(body, n):
            if n == 0:
                return cw_testlib.tool_call("propose_resolve", {"thread": "gh-501", "why": "foo.py:2 ok"})
            return cw_testlib.text("Looks settled.")

        with cw_testlib.StubLLM(_combined_script(ask_handler)) as stub:
            d = _build_done(home, tmp, stub)
            _seed_review_threads(d)
            record, events = _cturn(d, "q1", "ok?", anchor=ANCHOR_LINE)
        assert record["status"] == "ok" and record["answer"] == "Looks settled."
        assert [o["payload"]["thread"] for o in cw_ask.read_outcomes(d)] == ["gh-501"]
        kinds = [k for k, _ in events]
        assert kinds.index("outcome") < kinds.index("thread") and "delta" not in kinds
        assert ("progress", "propose_resolve") in [(k, x["tool"]) for k, x in events if k == "progress"]


def test_dismissed_suggestion_is_fed_into_the_next_turn():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        _seed_review_threads(d)
        script = {"sonnet": [cw_testlib.claude_stream_entry(["one"], mcp_calls=_propose()),
                             cw_testlib.claude_stream_entry(["two"]),
                             cw_testlib.claude_stream_entry(["three"])]}
        with cw_testlib.fake_claude(tmp, script) as fc:
            _cturn(d, "q1", "first?", anchor=ANCHOR_LINE)
            oid = cw_ask.read_outcomes(d)[0]["oid"]
            cw_ask.outcome_action(d, oid, "dismiss")
            _cturn(d, "q2", "second?", thread_id="q1", anchor=ANCHOR_LINE)
            _cturn(d, "q3", "third?", thread_id="q1", anchor=ANCHOR_LINE)
        second, third = fc.log()[1]["stdin"], fc.log()[2]["stdin"]
        assert "dismissed your suggestion to resolve foo.py:2: foo.py:2 now assigns X" in second
        assert "gh-501, foo.py:2, rev" in second
        assert "dismissed" not in third


def test_text_is_flushed_before_a_following_tool_event():
    entry = cw_testlib.claude_stream_entry(["a", "b"])
    entry["stream"].insert(4, {"type": "assistant", "message": {"content": [{
        "type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/x"}}]}})
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}):
            _record, events = _cturn(d, "q1", "why?")
    kinds = [k for k, _ in events]
    assert kinds[:3] == ["delta", "delta", "progress"], kinds
    assert [x["text"] for k, x in events if k == "delta"] == ["a", "b"]


def test_cancel_after_result_completes_and_books_usage():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        live = {}
        entry = cw_testlib.claude_stream_entry(["done"])
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}):
            record, _ = _cturn(d, "q1", "why?", live=live,
                               on_event=lambda k, _x: live.update(cancelled=True) if k == "delta" else None)
        assert record["status"] == "ok" and record["answer"] == "done"
        assert record["usage"]["prompt_tokens"] == 10 and record["usage"]["completion_tokens"] == 5


def test_cancel_without_result_keeps_the_buffered_tail():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        entry = cw_testlib.claude_stream_entry(["a", "b"])
        entry["stream"] = entry["stream"][:4] + [{"_sleep": 30}]
        live = {}

        def cancel():
            deadline = time.time() + 20
            while not live.get("text") and time.time() < deadline:
                time.sleep(0.02)
            time.sleep(0.5)
            live["cancelled"] = True
            cw_ask._kill(live["proc"])

        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}):
            t = threading.Thread(target=cancel)
            t.start()
            record, _ = _cturn(d, "q1", "why?", live=live)
            t.join()
        assert record["status"] == "cancelled" and record["answer"] == "ab"
        assert record["usage"]["prompt_tokens"] == 0


def test_openai_cancel_stops_at_the_next_tool_boundary():
    live = {}

    def ask_handler(body, n):
        live["cancelled"] = True
        return cw_testlib.tool_call("read_file", {"path": "foo.py"})

    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(_combined_script(ask_handler)) as stub:
            d = _build_done(home, tmp, stub)
            before = stub.count("ask-m")
            record, _ = _cturn(d, "q1", "why?", live=live)
            assert stub.count("ask-m") - before == 1
        assert record["status"] == "cancelled" and record["answer"] == "" and record["error"] is None


def test_openai_late_cancel_keeps_a_fully_generated_answer():
    live = {}

    def ask_handler(body, n):
        if n == 0:
            return cw_testlib.tool_call("read_file", {"path": "foo.py"})
        live["cancelled"] = True
        return cw_testlib.text("the full answer")

    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(_combined_script(ask_handler)) as stub:
            d = _build_done(home, tmp, stub)
            record, _ = _cturn(d, "q1", "why?", live=live)
        assert record["status"] == "ok" and record["answer"] == "the full answer"


def test_claude_reseed_replaces_live_text_bumps_seq_and_books_failed_usage():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        failed = cw_testlib.claude_stream_entry(["stale ", "text"], session_id="s-x")
        failed["stream"] = failed["stream"][1:]
        failed.update(exit=1, stderr="no conversation found")
        script = {"sonnet": [
            cw_testlib.claude_stream_entry(["one"], session_id="s-1"),
            failed,
            cw_testlib.claude_stream_entry(["fresh"], session_id="s-2"),
        ]}
        with cw_testlib.fake_claude(tmp, script):
            _cturn(d, "q1", "first?")
            live, deltas = {}, []
            record, _ = _cturn(
                d, "q2", "second?", thread_id="q1", live=live,
                on_event=lambda k, x: deltas.append(x["seq"]) if k == "delta" else None)
        assert record["answer"] == "fresh" and live["text"] == "fresh"
        assert max(deltas) >= 3 and live["seq"] > max(deltas)
        assert record["usage"]["prompt_tokens"] == 20


def test_system_prompt_ctx_note_only_on_the_claude_path():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        with cw_testlib.fake_claude(tmp, {"sonnet": [cw_testlib.claude_stream_entry(["x"])]}) as fc:
            _cturn(d, "q1", "why?")
        argv = fc.log()[-1]["argv"]
        system = argv[argv.index("--system-prompt") + 1] if "--system-prompt" in argv else " ".join(argv)
        assert str((d / "ctx").resolve()) in system and "{ctx_note}" not in system

    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM(_combined_script(lambda body, n: cw_testlib.text("fine"))) as stub:
            d = _build_done(home, tmp, stub)
            _cturn(d, "q1", "why?")
            content = [r for r in stub.requests if r.get("model") == "ask-m"][-1]["messages"][0]["content"]
        assert "analysis.json" not in content and "{ctx_note}" not in content and "\n\n\n" not in content


def test_reseeded_prompt_lists_earlier_dismissed_suggestion():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        _seed_review_threads(d)
        script = {"sonnet": [cw_testlib.claude_stream_entry(["one"], mcp_calls=_propose()),
                             {"stream": [], "exit": 1, "stderr": "no conversation found"},
                             cw_testlib.claude_stream_entry(["two"], session_id="s-2")]}
        with cw_testlib.fake_claude(tmp, script) as fc:
            _cturn(d, "q1", "first?", anchor=ANCHOR_LINE)
            cw_ask.outcome_action(d, cw_ask.read_outcomes(d)[0]["oid"], "dismiss")
            _cturn(d, "q2", "second?", thread_id="q1", anchor=ANCHOR_LINE)
        assert "dismissed your suggestion to resolve foo.py:2" in fc.log()[2]["stdin"]


def test_finalise_stale_keeps_outcomes_accepted_before_the_kill():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        _seed_review_threads(d)
        cw_ask.begin_turn(d, "q-dead", ANCHOR_LINE, "c")
        cw_ask.write_turn_anchor(d, "q-dead", "q-dead", ANCHOR_LINE)
        ok, _text = cw_mcp.accept_outcome(d, "q-dead", "propose_resolve", {"thread": "gh-501", "why": "w"})
        assert ok
        cw_ask.finalise_stale(d, set())
        assert [(o["qid"], o["thread_id"], o["state"]) for o in cw_ask.read_outcomes(d)] == [
            ("q-dead", "q-dead", "proposed")]
        assert cw_ask.read_qa(d)[0]["status"] == "error"


def test_outcome_action_states_and_done_folding():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        _seed_review_threads(d)
        cw_ask.write_turn_anchor(d, "q1", "q1", ANCHOR_LINE)
        cw_mcp.accept_outcome(d, "q1", "propose_resolve", {"thread": "gh-501", "why": "w"})
        cw_ask.sweep_outcomes(d, "q1", "q1")
        oid = cw_ask.read_outcomes(d)[0]["oid"]
        events = []
        on_event = lambda kind, data: events.append((kind, data))

        def refused(action, payload=None, target=oid):
            try:
                cw_ask.outcome_action(d, target, action, payload)
            except cw_ask.OutcomeError as e:
                return e.kind, str(e)
            raise AssertionError("not refused")

        assert refused("dismiss", target="o-00000000")[0] == "not_found"
        kind, message = refused("edit", {"thread": "gh-999", "why": "w"})
        assert kind == "invalid" and "gh-999" in message
        assert refused("edit", {"thread": "gh-501", "why": " "})[0] == "invalid"
        edited = cw_ask.outcome_action(d, oid, "edit", {"thread": "gh-501", "why": " better "}, on_event)
        assert edited["state"] == "proposed" and edited["payload"]["why"] == "better"
        dismissed = cw_ask.outcome_action(d, oid, "dismiss", on_event=on_event)
        assert dismissed["state"] == "dismissed" and [k for k, _ in events] == ["outcome", "outcome"]
        assert refused("dismiss") == ("conflict", "outcome is not open")
        assert cw_ask.read_outcomes(d) == [dismissed]

        cw_ask.write_turn_anchor(d, "q2", "q1", ANCHOR_LINE)
        cw_mcp.accept_outcome(d, "q2", "propose_resolve", {"thread": "gh-501", "why": "again"})
        _seed_review_threads(d, resolved=True)
        cw_ask.sweep_outcomes(d, "q2", "q1")
        assert [o["state"] for o in cw_ask.read_outcomes(d)] == ["dismissed", "done"]


def _page_edit_call(at=1, **over):
    args = {"op": "insert_after", "target": "overview:p1", "block": {"type": "prose", "text": " A note. "}, **over}
    return [{"at": at, "name": "propose_page_edit", "arguments": args}]


def test_page_edit_applies_at_creation_and_revert_reapply_fold():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        entry = cw_testlib.claude_stream_entry(["added a note"], mcp_calls=_page_edit_call())
        with cw_testlib.fake_claude(tmp, {"sonnet": [entry]}) as fc:
            record, events = _cturn(d, "q1", "add a note", anchor=ANCHOR_BLOCK)
        assert "Block key: overview:p1" in fc.log()[-1]["stdin"]
        kinds = [k for k, _ in events]
        assert kinds.index("outcome") < kinds.index("thread")
        outcome = dict(events)["outcome"]
        assert outcome["outcome"] == "page_edit" and outcome["state"] == "applied"
        assert outcome["payload"] == {"op": "insert_after", "target": "overview:p1",
                                      "block": {"type": "prose", "text": "A note."}}
        oid = outcome["oid"]
        seen = []
        on_event = lambda kind, data: seen.append(data["state"])

        def refused(action):
            try:
                cw_ask.outcome_action(d, oid, action)
            except cw_ask.OutcomeError as e:
                return e.kind
            raise AssertionError("not refused")

        assert refused("reapply") == "conflict"
        assert refused("dismiss") == "invalid" and refused("edit") == "invalid"
        assert cw_ask.outcome_action(d, oid, "revert", on_event=on_event)["state"] == "reverted"
        assert refused("revert") == "conflict"
        assert cw_ask.outcome_action(d, oid, "reapply", on_event=on_event)["state"] == "applied"
        assert seen == ["reverted", "applied"]
        assert cw_ask.read_threads(d)["q1"]["outcomes"][0]["state"] == "applied"


def test_page_edit_undo_and_redo_reach_the_next_prompt():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        d = _claude_done(home, tmp)
        script = {"sonnet": [cw_testlib.claude_stream_entry(["one"], mcp_calls=_page_edit_call()),
                             cw_testlib.claude_stream_entry(["two"]),
                             cw_testlib.claude_stream_entry(["three"])]}
        with cw_testlib.fake_claude(tmp, script) as fc:
            _cturn(d, "q1", "add", anchor=ANCHOR_BLOCK)
            oid = cw_ask.read_outcomes(d)[0]["oid"]
            cw_ask.outcome_action(d, oid, "revert")
            _cturn(d, "q2", "again?", thread_id="q1", anchor=ANCHOR_BLOCK)
            cw_ask.outcome_action(d, oid, "reapply")
            _cturn(d, "q3", "ok", thread_id="q1", anchor=ANCHOR_BLOCK)
        second, third = fc.log()[1]["stdin"], fc.log()[2]["stdin"]
        assert "The user undid your page edit (insert_after after block overview:p1)" in second
        assert "Block key: overview:p1" in second
        assert "The user redid your page edit" in third and "undid" not in third


def test_page_edit_on_a_line_anchor_is_refused_and_openai_path_registers_the_tool():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        def ask_handler(body, n):
            if n == 0:
                return cw_testlib.tool_call("propose_page_edit", {
                    "op": "replace", "target": "overview:p1", "block": {"type": "list", "text": "- x"}})
            return cw_testlib.text("done")

        with cw_testlib.StubLLM(_combined_script(ask_handler)) as stub:
            d = _build_done(home, tmp, stub)
            record, events = _cturn(d, "q1", "list it", anchor=ANCHOR_BLOCK)
            names = [t["function"]["name"] for t in
                     [r for r in stub.requests if r.get("model") == "ask-m"][-1]["tools"]]
            line_record, line_events = _cturn(d, "q2", "x", anchor=ANCHOR_LINE)
        assert {"propose_resolve", "propose_page_edit", "propose_github_draft"} <= set(names)
        assert record["status"] == "ok" and [o["state"] for o in cw_ask.read_outcomes(d)] == ["applied"]
        assert "outcome" in [k for k, _ in events]
        assert "outcome" not in [k for k, _ in line_events]


ANCHOR_THREAD = {"kind": "thread", "note_id": "gh-501", "quote": "please fix"}
DRAFT_COMMENT = "  you should guard this\n```py\nx = 1\n```\n<b>now</b> \U0001F680\n"


def _thread_state(d):
    root = {"id": "gh-501", "origin": "github", "gh_thread_id": "T1", "path": "foo.py", "line": 2,
            "author": "rev", "body": "please fix", "state": "posted", "created_at": "2026-01-01T00:00:01Z",
            "diff_hunk": "@@ -1,3 +1,3 @@\n a\n-b\n+X"}
    other = {**root, "id": "gh-600", "gh_thread_id": "T2", "line": 3, "resolved": True, "body": "old nit"}
    reply = {**root, "id": "gh-502", "reply_to": "gh-501", "author": "me", "body": "done in abc",
             "created_at": "2026-01-01T00:00:02Z", "diff_hunk": None}
    draft = {**reply, "id": "n-draft", "origin": "local", "state": "draft", "body": "SECRET DRAFT"}
    cw_store.write_json(Path(d) / "state.json", {"notes": [reply, root, other, draft]})


def test_thread_anchor_validation_and_turn_anchor_doc():
    assert cw_ask.validate_anchor(dict(ANCHOR_THREAD)) == ANCHOR_THREAD
    for bad in ({"kind": "thread", "quote": "q"}, {"kind": "thread", "note_id": 5, "quote": "q"},
                {"kind": "thread", "note_id": "a b", "quote": "q"}, {"kind": "thread", "note_id": "gh-1"}):
        try:
            cw_ask.validate_anchor(bad)
        except cw_store.CWError:
            continue
        raise AssertionError(bad)
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _thread_state(d)

        def doc(anchor):
            cw_ask.write_turn_anchor(d, "q1", "q1", anchor, DRAFT_COMMENT)
            return cw_store.read_json(d / "turns" / "q1.anchor.json")

        thread = doc(ANCHOR_THREAD)
        assert thread["comment"] == DRAFT_COMMENT
        assert [r["id"] for r in thread["replyable"]] == ["gh-501"] == [r["id"] for r in thread["resolvable"]]
        assert thread["replyable"][0].keys() == {"id", "path", "line", "author", "body"}
        assert doc({**ANCHOR_THREAD, "note_id": "gh-600"})["resolvable"] == []
        assert [r["id"] for r in doc({**ANCHOR_THREAD, "note_id": "gh-600"})["replyable"]] == ["gh-600"]
        assert doc({**ANCHOR_THREAD, "note_id": "gh-502"})["replyable"] == []
        line = doc({**ANCHOR_LINE, "end_line": 3})
        assert sorted(r["id"] for r in line["replyable"]) == ["gh-501", "gh-600"]
        assert [r["id"] for r in line["resolvable"]] == ["gh-501"]
        assert doc(ANCHOR_BLOCK)["replyable"] == [] and doc(ANCHOR_SECTION)["replyable"] == []


def test_thread_anchor_prompt_carries_the_thread_in_order_and_its_hunk_but_no_drafts():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _thread_state(d)
        prompt = cw_ask.build_prompt(d, dict(ANCHOR_THREAD), "is this fixed?", "q1")
        assert "gh-501" in prompt and "@@ -1,3 +1,3 @@\n a\n-b\n+X" in prompt
        assert prompt.index("rev: please fix") < prompt.index("me: done in abc")
        assert "SECRET DRAFT" not in prompt and "old nit" not in prompt


def _draft_turn(d, qid, anchor, comment, args, thread_id=None):
    cw_ask.write_turn_anchor(d, qid, thread_id or qid, anchor, comment)
    ok, text = cw_mcp.accept_outcome(d, qid, "propose_github_draft", args)
    assert ok, text
    cw_ask.sweep_outcomes(d, qid, thread_id or qid)
    return next(o for o in cw_ask.read_outcomes(d) if o["qid"] == qid)


def test_github_draft_outcome_states_and_guards():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _thread_state(d)
        outcome = _draft_turn(d, "q1", ANCHOR_LINE, DRAFT_COMMENT, {"body": "reworded", "target": {"kind": "new"}})
        oid = outcome["oid"]
        assert outcome["outcome"] == "github_draft" and outcome["state"] == "proposed"
        assert outcome["payload"]["original"] == DRAFT_COMMENT and outcome["payload"]["verbatim"] is False
        assert outcome["payload"]["target"]["path"] == "foo.py"

        def refused(action, payload=None, target=oid):
            try:
                cw_ask.outcome_action(d, target, action, payload)
            except cw_ask.OutcomeError as e:
                return e.kind
            raise AssertionError("not refused")

        assert refused("revert") == "invalid" and refused("reapply") == "invalid"
        for bad in (None, {}, {"body": "  "}, {"body": 5}, {"body": "x" * 4001}):
            assert refused("edit", bad) == "invalid"
        for bad in (None, {}, {"note_id": 5}, {"note_id": "a b"}):
            assert refused("keep", bad) == "invalid"
        edited = cw_ask.outcome_action(d, oid, "edit", {"body": "  my edit \n"})
        assert edited["state"] == "proposed" and edited["payload"]["body"] == "  my edit \n"
        assert edited["payload"]["edited"] is True and edited["payload"]["verbatim"] is False
        back = cw_ask.outcome_action(d, oid, "verbatim", {})
        assert back["payload"]["body"] == DRAFT_COMMENT and back["payload"]["verbatim"] is True
        assert back["payload"]["edited"] is False
        kept = cw_ask.outcome_action(d, oid, "keep", {"note_id": "n-77"})
        assert kept["state"] == "kept" and kept["payload"]["note_id"] == "n-77"
        assert kept["payload"]["body"] == DRAFT_COMMENT
        for action, payload in (("dismiss", None), ("edit", {"body": "x"}), ("keep", {"note_id": "n-8"}),
                                ("verbatim", {})):
            assert refused(action, payload) == "conflict"
        published = cw_ask.mark_outcome(d, oid, "published")
        assert published["state"] == "published" and published["payload"] == kept["payload"]

        second = _draft_turn(d, "q2", ANCHOR_THREAD, "c", {"body": "b", "target": {"kind": "reply", "note_id": "gh-501"}})
        assert second["payload"]["target"] == {"kind": "reply", "note_id": "gh-501", "path": "foo.py", "line": 2}
        assert cw_ask.outcome_action(d, second["oid"], "dismiss")["state"] == "dismissed"
        assert refused("keep", {"note_id": "n-9"}, target=second["oid"]) == "conflict"


def test_github_draft_dismissal_and_edit_reach_the_next_prompt():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _thread_state(d)
        one = _draft_turn(d, "q1", ANCHOR_LINE, "c", {"body": "first", "target": {"kind": "new"}}, "q1")
        cw_ask.outcome_action(d, one["oid"], "edit", {"body": "my words"})
        text = cw_ask.build_prompt(d, dict(ANCHOR_LINE), "next", "q1", resumed=True)
        assert "edited your draft GitHub comment on foo.py:2 to: my words" in text
        assert "Review threads on these lines you may reply to with propose_github_draft:" in text
        cw_ask.outcome_action(d, one["oid"], "dismiss")
        text = cw_ask.build_prompt(d, dict(ANCHOR_LINE), "next", "q1", resumed=True)
        assert "dismissed your draft GitHub comment on foo.py:2" in text
        cw_ask.mark_outcome(d, one["oid"], "published")
        assert "published" not in cw_ask.build_prompt(d, dict(ANCHOR_LINE), "next", "q1", resumed=True)


if __name__ == "__main__":
    tests = [
        test_ask_gives_qa_record_and_sse_answer,
        test_oversized_selection_refused_quote_never_cut,
        test_prompt_order_hunk_notes_and_pair_limit,
        test_answer_never_raises_on_a_malformed_ask_profile,
        test_same_thread_comments_run_sequentially,
        test_different_threads_run_concurrently,
        test_comment_is_persisted_pending_before_turn_starts,
        test_block_anchor_accepted_and_bad_key_refused,
        test_prompt_history_is_per_thread_and_ignores_unfinished_turns,
        test_old_record_without_thread_id_folds_as_its_own_thread,
        test_read_threads_folds_resolve_events_and_readers_skip_them,
        test_resolve_route_guards_and_effect,
        test_comment_route_guards_and_unknown_thread,
        test_hung_endpoint_cut_at_timeout,
        test_guards_and_walkthrough_get_qa,
        test_idle_exit_waits_for_in_flight_ask,
        test_ask_on_claude_code_sums_usage_and_omits_json_schema,
        test_claude_deltas_are_ordered_and_coalesced,
        test_claude_answer_joins_text_blocks_and_reports_progress,
        test_claude_followup_resumes_and_failed_resume_reseeds,
        test_thread_role_wins_over_ask_and_falls_back,
        test_claude_aborts_fast_when_outcome_server_missing,
        test_claude_cancel_keeps_partial_text,
        test_resolvable_is_for_line_anchors_over_open_root_review_threads,
        test_proposed_resolve_lands_as_outcome_before_the_final_thread_event,
        test_resolve_for_a_non_candidate_thread_is_refused_and_records_nothing,
        test_openai_turn_records_propose_resolve_and_keeps_looping,
        test_dismissed_suggestion_is_fed_into_the_next_turn,
        test_text_is_flushed_before_a_following_tool_event,
        test_cancel_after_result_completes_and_books_usage,
        test_cancel_without_result_keeps_the_buffered_tail,
        test_openai_cancel_stops_at_the_next_tool_boundary,
        test_system_prompt_ctx_note_only_on_the_claude_path,
        test_reseeded_prompt_lists_earlier_dismissed_suggestion,
        test_finalise_stale_keeps_outcomes_accepted_before_the_kill,
        test_outcome_action_states_and_done_folding,
        test_page_edit_applies_at_creation_and_revert_reapply_fold,
        test_page_edit_undo_and_redo_reach_the_next_prompt,
        test_page_edit_on_a_line_anchor_is_refused_and_openai_path_registers_the_tool,
        test_thread_anchor_validation_and_turn_anchor_doc,
        test_thread_anchor_prompt_carries_the_thread_in_order_and_its_hunk_but_no_drafts,
        test_github_draft_outcome_states_and_guards,
        test_github_draft_dismissal_and_edit_reach_the_next_prompt,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
