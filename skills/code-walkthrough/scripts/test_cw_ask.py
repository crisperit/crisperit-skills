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
                    assert json.loads(raw) == {"turns": turns, "resolved": []}
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
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
