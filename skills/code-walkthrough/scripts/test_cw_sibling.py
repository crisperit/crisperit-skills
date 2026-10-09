#!/usr/bin/env python3
"""Self-check for the sibling revision ("Show with this change"): carry-forward in cw_run, the
show route, the publishing guard and prune in cw_server. Assert-based; also collected by
pytest. StubLLM counts every model call so "only the changed batch is re-annotated" is a
number, not a claim."""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_llm  # noqa: E402
import cw_run  # noqa: E402
import cw_server  # noqa: E402
import cw_store  # noqa: E402
import cw_testlib  # noqa: E402
import test_cw_server as srv  # noqa: E402

cw_llm.BACKOFF_S = [0, 0]

A_BASE = "".join(f"a{i}\n" for i in range(6))
B_BASE = "".join(f"b{i}\n" for i in range(6))
OID = "o-00000001"
OID_OPEN = "o-00000002"


def _reply(body, _n):
    names = cw_testlib.tool_names(body)
    if "submit_prose" in names:
        return cw_testlib.tool_call("submit_prose", {"overview": "PARENT OVERVIEW", "verdict": "PARENT VERDICT",
                                                       "flow_mermaid": ""})
    seed = cw_testlib.first_json_block(cw_testlib.last_user_text(body))
    files = [{"path": f["path"], "role": f["role"] or f"role of {f['path']}",
              "hunks": [{"header": h["header"], "note": h["note"] or "changes A2 B2 TASK4 b2 b4 a2"}
                        for h in f["hunks"]]} for f in seed["files"]]
    return cw_testlib.tool_call("submit_fragment", {"files": files})


def _setup(home, tmp, stub):
    """Parent walkthrough (two batches, one file each) run to done, plus a task commit on top of
    its head that changes only b.py. Returns (repo, base, head, task_sha, parent_dir)."""
    repo, base, head = cw_testlib.make_repo(
        tmp, {"a.py": A_BASE, "b.py": B_BASE},
        {"a.py": A_BASE.replace("a2", "A2"), "b.py": B_BASE.replace("b2", "B2")})
    (repo / "b.py").write_text(B_BASE.replace("b2", "B2").replace("b4", "TASK4"))
    cw_testlib.git(repo, "add", "-A")
    cw_testlib.git(repo, "commit", "-q", "-m", "task")
    task_sha = cw_testlib.git(repo, "rev-parse", "HEAD").strip()
    cw_testlib.write_config(
        home, {"a": stub.profile("analysis-m"), "p": stub.profile("prose-m")},
        {"analysis": "a", "prose": "p"}, small_diff_lines=0, batch_max_lines=1)
    d, meta, _ = cw_run.prepare_walkthrough({"repo": str(repo), "base": base, "head": head,
                                              "target": "main...HEAD", "slug": "par"})
    assert meta["batches"] == 2
    assert cw_run.run(d) == "done"
    return repo, base, head, task_sha, d


def _stub():
    return cw_testlib.StubLLM(lambda body, n: _reply(body, n))


def _count(stub, model):
    return sum(1 for r in stub.requests if r.get("model") == model)


def _sibling_params(repo, base, task_sha, parent_d):
    pm = cw_store.read_meta(parent_d)
    return {"repo": str(repo), "base": base, "head": task_sha, "target": "main...HEAD+t1", "slug": "par-t1",
            "sibling_of": {"key": pm["key"], "id": pm["id"], "oid": OID, "task_id": "t1", "branch": "cw/t1",
                           "title": "Fix b", "parent_head": pm["head"]}}


def _wait_done(d, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meta = cw_store.read_meta(d) or {}
        if meta.get("status") in ("done", "failed"):
            return meta
        time.sleep(0.1)
    raise AssertionError("run never finished")


def test_carry_forward_reannotates_only_the_changed_batch_and_keeps_parent_prose():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp, _stub() as stub:
        repo, base, _head, task_sha, pd = _setup(home, tmp, stub)
        assert _count(stub, "analysis-m") == 2 and _count(stub, "prose-m") == 1
        parent_analysis = json.loads((pd / "analysis.json").read_text())

        sd, meta, _ = cw_run.prepare_walkthrough(_sibling_params(repo, base, task_sha, pd))
        assert sd != pd
        assert cw_run.run(sd) == "done"

        assert _count(stub, "analysis-m") == 3  # batch 1 (a.py) carried, batch 2 (b.py) one call
        assert _count(stub, "prose-m") == 1
        meta = cw_store.read_meta(sd)
        assert meta["steps"]["batch-1"]["reused"] is True and meta["steps"]["batch-1"]["carried"] is True
        assert meta["steps"]["batch-2"].get("carried") is None
        assert meta["pr"] is None and meta["steps"].get("comments") is None
        assert meta["sibling_of"]["changed_files"] == ["b.py"]
        assert meta["sibling_of"]["prose_carried"] is True
        assert meta["sibling_of"]["task_id"] == "t1"
        assert meta["sibling_of"]["parent_head"] == cw_store.read_meta(pd)["head"]

        analysis = json.loads((sd / "analysis.json").read_text())
        by = lambda a: {f["path"]: f for f in a["files"]}  # noqa: E731
        assert json.dumps(by(analysis)["a.py"], sort_keys=True) == json.dumps(by(parent_analysis)["a.py"], sort_keys=True)
        assert by(analysis)["b.py"]["hunks"][0]["note"]
        assert json.loads((sd / "prose.json").read_text())["overview"] == "PARENT OVERVIEW"
        assert analysis["verdict"] == "PARENT VERDICT"

        live = cw_server._live_payload(sd.parent.name, sd.name, meta, sd, "tok", 1234)
        sib = live["sibling"]
        assert sib["changed_files"] == ["b.py"] and sib["prose_carried"] is True
        assert sib["task_id"] == "t1" and sib["branch"] == "cw/t1" and sib["title"] == "Fix b"
        assert sib["sha"] == task_sha
        assert sib["parent_url"] == f"http://127.0.0.1:1234/walkthrough/{pd.parent.name}/{pd.name}/?k=tok"
        pm = cw_store.read_meta(pd)
        assert cw_server._live_payload(pd.parent.name, pd.name, pm, pd, "tok", 1234)["sibling"] is None

        # prune: the sibling goes with its parent
        cw_server._remove_walkthrough(pm, pd)
        assert not pd.exists() and not sd.exists()


def test_bad_sibling_of_is_rejected():
    params = {"repo": "/nonexistent", "base": "x", "head": "y", "target": "t", "slug": "s",
              "sibling_of": {"key": "k", "id": "i", "task_id": "t1", "parent_head": "h"}}
    try:
        cw_run.prepare_walkthrough(params)
    except cw_store.CWError as e:
        assert "sibling_of" in str(e)
    else:
        raise AssertionError("expected CWError")


def _outcome_records(task_sha):
    task = {"id": "t1", "qid": "q-00000001", "sha": task_sha, "branch": "cw/t1", "files": ["b.py"],
            "stat": "1 file changed", "summary": "done"}
    rec = lambda oid, qid: {"type": "outcome", "oid": oid, "qid": qid, "thread_id": qid,  # noqa: E731
                            "outcome": "task", "payload": {"title": "Fix b", "task": task}, "state": "running"}
    return [rec(OID, "q-00000001"), {"type": "state", "oid": OID, "state": "done"}, rec(OID_OPEN, "q-00000002")]


def test_show_route_guards_reuse_and_publishing_is_off():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp, _stub() as stub:
        repo, base, _head, task_sha, pd = _setup(home, tmp, stub)
        srv._append_qa(pd, *_outcome_records(task_sha))
        key, wid = pd.parent.name, pd.name
        path = f"/api/walkthrough/{key}/{wid}/outcomes/{OID}/show"
        with srv.running_daemon() as daemon:
            srv._guard_checks(daemon, path, {})
            assert srv._post(daemon, f"/api/walkthrough/{key}/{wid}/outcomes/o-0000000f/show")[0] == 404
            assert srv._post(daemon, f"/api/walkthrough/{key}/{wid}/outcomes/{OID_OPEN}/show")[0] == 409
            pm = cw_store.read_meta(pd)
            srv._set_meta(pd, status="building")
            assert srv._post(daemon, path)[0] == 409
            srv._set_meta(pd, status="done")

            status, out = srv._post(daemon, path)
            assert status == 200 and out["ok"] is True and out["reused"] is False, (status, out)
            assert out["key"] == key and out["id"] != wid
            assert out["url"] == f"http://127.0.0.1:{daemon.port}/walkthrough/{key}/{out['id']}/?k={daemon.token}"
            sd = cw_store.walkthrough_dir(key, out["id"])
            smeta = _wait_done(sd)
            assert smeta["status"] == "done", smeta
            assert smeta["sibling_of"]["oid"] == OID and smeta["pr"] is None
            assert smeta["sibling_of"]["title"] == "Fix b"
            assert cw_server._live_payload(key, out["id"], smeta, sd, "t", 1)["sibling"]["title"] == "Fix b"
            assert smeta["target"] == pm["target"] + "+t1" and smeta["head"] == task_sha
            calls = _count(stub, "analysis-m")

            status, again = srv._post(daemon, path)
            assert status == 200 and again["reused"] is True and again["id"] == out["id"]
            assert _count(stub, "analysis-m") == calls

            # a sibling is not itself a parent
            sib_path = f"/api/walkthrough/{key}/{out['id']}/outcomes/{OID}/show"
            assert srv._post(daemon, sib_path)[0] == 404

            for suffix, body in (("post/preview", {}), ("post", {}), ("publish-one", {"id": "n1", "body_sha": "x"})):
                status, err = srv._post(daemon, f"/api/walkthrough/{key}/{out['id']}/{suffix}", body)
                assert status == 409, (suffix, status, err)
                assert err == {"error": "publishing is off in this view", "remedy": "go back to the PR head"}, suffix


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
