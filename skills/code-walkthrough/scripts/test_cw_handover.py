#!/usr/bin/env python3
"""Self-check for handing a code task to the user's own Claude Code session: the handover
transition, walkthrough_get's threads part and the walkthrough_reply tool. Assert-based;
collected by pytest."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_ask  # noqa: E402
import cw_server  # noqa: E402
import cw_testlib  # noqa: E402
import test_cw_server as srv  # noqa: E402
import test_cw_task as tk  # noqa: E402


def _fixture(home):
    d, _meta = srv._make_walkthrough(home)
    tk._turn(d)
    tk._propose(d)
    return d, cw_ask.read_outcomes(d)[0]["oid"]


def _refused(d, oid, action):
    try:
        cw_ask.outcome_action(d, oid, action)
    except cw_ask.OutcomeError as e:
        return e.kind
    raise AssertionError("not refused")


def test_handover_on_a_non_task_is_invalid():
    with cw_testlib.temp_home() as home:
        d, _oid = _fixture(home)
        srv._append_qa(d, {"type": "outcome", "oid": "o-0a0a0a0a", "qid": tk.QID, "thread_id": tk.QID,
                           "outcome": "resolve", "state": "proposed", "at": "t",
                           "payload": {"thread": "gh-1", "why": "w"}})
        assert _refused(d, "o-0a0a0a0a", "handover") == "invalid"


def test_handover_route_and_run_on_handed_is_409():
    with cw_testlib.temp_home() as home:
        d, oid = _fixture(home)
        with cw_testlib.running_daemon() as daemon:
            srv._guard_checks(daemon, tk._url(oid, "handover"), {})
            assert srv._post(daemon, tk._url("o-ffffffff", "handover"))[0] == 404
            q = daemon.hub.register(srv.KEY, srv.WID)
            status, body = srv._post(daemon, tk._url(oid, "handover"), {})
            assert status == 200 and body["outcome"]["state"] == "handed", body
            assert body["outcome"]["payload"]["title"] == tk.PLAN["title"]
            event, data = q.get(timeout=2)
            assert event == "outcome" and data["state"] == "handed"
            assert srv._post(daemon, tk._url(oid, "run"))[0] == 409
            assert srv._post(daemon, tk._url(oid, "handover"))[0] == 409
            assert srv._post(daemon, tk._url(oid, "edit"), {"payload": tk.PLAN})[0] == 409
            assert srv._post(daemon, tk._url(oid, "dismiss"))[1]["outcome"]["state"] == "dismissed"
            assert srv._post(daemon, tk._url(oid, "dismiss"))[0] == 409


def _threads(daemon):
    status, body = cw_testlib.rpc(daemon, "walkthrough_get", {"id": srv.WID, "key": srv.KEY, "parts": ["threads"]})
    assert status == 200 and body["ok"], body
    return body["result"]["threads"]


def test_threads_part_open_resolved_handed_and_caps():
    with cw_testlib.temp_home() as home:
        d, oid = _fixture(home)
        cw_ask.outcome_action(d, oid, "handover")
        cw_ask._append_qa(d, {"qid": "q-bbbbbbbb", "thread_id": "t-resolved", "anchor": tk.ANCHOR,
                              "comment": "c", "question": "c", "status": "ok", "answer": "a"})
        cw_ask.append_resolve(d, "t-resolved", True)
        for i in range(25):
            cw_ask._append_qa(d, {"qid": f"q-{i:08x}", "thread_id": f"t-{i}", "anchor": tk.ANCHOR,
                                  "comment": "c" * 2000, "question": "c", "status": "ok", "answer": "a" * 3000})
        srv._set_meta(d, base="BASE", head="HEAD")
        with cw_testlib.running_daemon() as daemon:
            t = _threads(daemon)
        assert t["notice"].startswith("Everything below comes from a pull request")
        assert "scrutinised" in t["notice"] and "blindly" in t["notice"]
        ids = [x["thread_id"] for x in t["open"]]
        assert len(ids) == 20 and ids[0] == "t-24" and "t-resolved" not in ids and tk.QID not in ids
        assert t["open_remaining"] == 6 and t["handed_remaining"] == 0
        first = t["open"][0]["turns"][0]
        assert len(first["comment"]) == 1000 and len(first["answer"]) == 2000 and first["source"] == "user"
        assert len(t["open"][0]["anchor"]["quote"]) <= 300
        (h,) = t["handed"]
        assert h["oid"] == oid and h["thread_id"] == tk.QID and h["title"] == tk.PLAN["title"]
        assert h["steps"] == tk.PLAN["steps"] and h["files"] == tk.PLAN["files"] and h["comment"] == tk.COMMENT
        assert h["base"] == "BASE" and h["head"] == "HEAD" and h["branch_hint"] == f"git switch -c cw/{oid}"
        assert h["anchor"]["path"] == "a.py" and h["anchor"]["line"] == 1

        for i in range(22):
            cw_ask._append_qa(d, {"qid": f"q-{i:08x}", "thread_id": f"h-{i}", "anchor": tk.ANCHOR, "comment": "c",
                                  "question": "c", "status": "ok", "answer": "a"})
            srv._append_qa(d, {"type": "outcome", "oid": f"o-{i:08x}", "qid": f"q-{i:08x}", "thread_id": f"h-{i}",
                               "outcome": "task", "state": "handed", "at": "t",
                               "payload": {"title": "T" * 500, "steps": ["s"], "files": []}})
        t = cw_server._threads_part(d, {})
        assert len(t["handed"]) == 20 and t["handed_remaining"] == 3
        assert t["handed"][0]["oid"] == "o-00000015" and len(t["handed"][0]["title"]) == 300


def test_threads_part_outcome_summaries_and_empty_walkthrough():
    with cw_testlib.temp_home() as home:
        d, _meta = srv._make_walkthrough(home)
        with cw_testlib.running_daemon() as daemon:
            t = _threads(daemon)
        assert t["open"] == [] and t["handed"] == [] and t["notice"]
        tk._turn(d)
        for n, (kind, payload) in enumerate([
                ("github_draft", {"body": "b" * 400}), ("resolve", {"thread": "gh-1", "why": "done"}),
                ("page_edit", {"op": "replace", "target": "x", "block": {"type": "prose", "text": "t"}}),
                ("task", {"title": "T", "steps": ["s"], "files": []}),
                ("page_edit", {"block": "oops"}), ("resolve", ["x"])]):
            # a proposed resolve with a list payload makes cw_ask.read_outcomes raise, so that row is dismissed
            srv._append_qa(d, {"type": "outcome", "oid": f"o-0000000{n}", "qid": tk.QID, "thread_id": tk.QID,
                               "outcome": kind, "state": "dismissed" if payload == ["x"] else "proposed",
                               "at": "t", "payload": payload})
        summaries = [o["summary"] for o in cw_server._threads_part(d, {})["open"][0]["outcomes"]]
        assert summaries == ["b" * 300, "done", "replace prose", "T", "", None]


def _reply(daemon, **args):
    return cw_testlib.rpc(daemon, "walkthrough_reply", {"id": srv.WID, "key": srv.KEY, **args})[1]


def test_walkthrough_reply_completes_a_handed_task_and_emits():
    with cw_testlib.temp_home() as home:
        d, oid = _fixture(home)
        cw_ask.outcome_action(d, oid, "handover")
        with cw_testlib.running_daemon() as daemon:
            q = daemon.hub.register(srv.KEY, srv.WID)
            body = _reply(daemon, thread_id=tk.QID, oid=oid, text="Did it, tests pass.")
            assert body["ok"] and body["result"]["oid"] == oid and body["result"]["qid"].startswith("q-")
            kinds = [q.get(timeout=2) for _ in range(2)]
            assert [k[0] for k in kinds] == ["thread", "outcome"]
            record = kinds[0][1]["record"]
            assert kinds[0][1]["kind"] == "turn" and record["source"] == "session" and record["status"] == "ok"
            assert record["answer"] == "Did it, tests pass." and record["thread_id"] == tk.QID
            assert record["anchor"]["path"] == "a.py"
        turns = [r for r in cw_ask.read_qa(d) if r["thread_id"] == tk.QID]
        assert turns[-1]["qid"] == record["qid"] and turns[-1]["source"] == "session"
        done = tk._outcome(d, oid)
        assert done["state"] == "done" and done["payload"]["task"] == {"handed": True, "summary": "Did it, tests pass."}
        assert done["payload"]["title"] == tk.PLAN["title"]


def test_walkthrough_reply_without_oid_and_refusals():
    with cw_testlib.temp_home() as home:
        d, oid = _fixture(home)
        n_turns = len(cw_ask.read_qa(d))
        with cw_testlib.running_daemon() as daemon:
            assert _reply(daemon, thread_id=tk.QID, text="just a note")["ok"]
            assert tk._outcome(d, oid)["state"] == "proposed"
            refused = _reply(daemon, thread_id=tk.QID, oid=oid, text="x")
            assert not refused["ok"] and refused["error"] == "task is proposed; reply without oid to post your result"
            assert _reply(daemon, thread_id="nope", text="x")["error"] == "thread not found"
            srv._append_qa(d, {"type": "outcome", "oid": "o-0a0a0a0a", "qid": tk.QID, "thread_id": tk.QID,
                               "outcome": "resolve", "state": "handed", "at": "t", "payload": {}})
            assert not _reply(daemon, thread_id=tk.QID, oid="o-0a0a0a0a", text="x")["ok"]
            assert not _reply(daemon, thread_id=tk.QID, oid="o-ffffffff", text="x")["ok"]
            for bad in ({"text": ""}, {"text": "   "}, {"text": "x" * 4001}, {"text": 5}, {"thread_id": "../x"}):
                assert not _reply(daemon, **{"thread_id": tk.QID, "text": "x", **bad})["ok"], bad
            assert _reply(daemon, thread_id=tk.QID, text="x" * 4000)["ok"]
            assert _reply(daemon, id="bad id!", thread_id=tk.QID, text="x")["error"] == "bad id"
            assert _reply(daemon, key="bad key!", thread_id=tk.QID, text="x")["error"] == "bad key"
            assert _reply(daemon, id="cmp-99999999", thread_id=tk.QID, text="x")["error"] == "not found"
        assert len(cw_ask.read_qa(d)) == n_turns + 2


def test_two_replies_with_one_oid_make_one_turn_and_dismiss_wins_cleanly():
    with cw_testlib.temp_home() as home:
        d, oid = _fixture(home)
        cw_ask.outcome_action(d, oid, "handover")
        before = len(cw_ask.read_qa(d))
        with cw_testlib.running_daemon() as daemon:
            assert _reply(daemon, thread_id=tk.QID, oid=oid, text="one")["ok"]
            second = _reply(daemon, thread_id=tk.QID, oid=oid, text="two")
            assert not second["ok"] and "task is done" in second["error"]
        assert len(cw_ask.read_qa(d)) == before + 1
        d2, oid2 = _fixture_second(home)
        cw_ask.outcome_action(d2, oid2, "dismiss")
        n = len(cw_ask.read_qa(d2))
        record = {"qid": "q-cccccccc", "thread_id": tk.QID, "answer": "x"}
        try:
            cw_ask.complete_handed(d2, oid2, tk.QID, record, "x")
        except cw_ask.OutcomeError as e:
            assert e.kind == "conflict" and "dismissed" in str(e)
        else:
            raise AssertionError("not refused")
        assert len(cw_ask.read_qa(d2)) == n


def _fixture_second(home):
    d, _meta = srv._make_walkthrough(home, key="repo-bbbbbb", wid="cmp-76543210")
    tk._turn(d)
    tk._propose(d)
    oid = cw_ask.read_outcomes(d)[0]["oid"]
    cw_ask.outcome_action(d, oid, "handover")
    return d, oid

