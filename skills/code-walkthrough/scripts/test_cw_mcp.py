#!/usr/bin/env python3
"""Self-check for cw_mcp.py. Assert-based, no framework; also collected by pytest. The tests that
spawn a real detached cw_server.py daemon live in test_cw_e2e.py."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_mcp  # noqa: E402
import cw_server  # noqa: E402
import cw_store  # noqa: E402
import cw_testlib  # noqa: E402

SCRIPTS_DIR = str(Path(__file__).parent)


@contextlib.contextmanager
def _no_claude_on_path():
    """A PATH with no `claude` binary anywhere on it, for the "not on PATH" branch."""
    with tempfile.TemporaryDirectory() as empty_bin:
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = empty_bin
        try:
            yield
        finally:
            os.environ["PATH"] = old_path


def test_handshake_does_not_start_the_daemon():
    with cw_testlib.temp_home() as home:
        proc = subprocess.Popen(
            [sys.executable, str(Path(SCRIPTS_DIR) / "cw_mcp.py"), "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            request = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18"}}
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            reply = json.loads(line)
            assert reply["id"] == 1
            assert reply["result"]["protocolVersion"] == "2025-06-18"
            assert reply["result"]["serverInfo"]["name"] == "code-walkthrough"
            assert not (home / "server.json").exists()

            # a notification (no id) must not get a reply
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            proc.stdin.flush()

            request = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
            reply = json.loads(proc.stdout.readline())
            names = [t["name"] for t in reply["result"]["tools"]]
            assert names == ["walkthrough_start", "walkthrough_get", "walkthrough_list", "walkthrough_reply"]
            start = reply["result"]["tools"][0]["inputSchema"]["properties"]
            assert start["fresh"]["type"] == "boolean"

            request = {"jsonrpc": "2.0", "id": 3, "method": "nope"}
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
            reply = json.loads(proc.stdout.readline())
            assert reply["error"]["code"] == -32601
        finally:
            proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)


def _cmd_setup():
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        assert cw_mcp.cmd_setup("print") == 0
    return err.getvalue()


def test_setup_writes_config():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.fake_claude(tmp, {}):
            err = _cmd_setup()
        config = cw_store.read_json(home / "config.json")
        assert config["profiles"]["claude"]["kind"] == "claude-code"
        assert config["profiles"]["claude"]["model"] == "sonnet"
        assert config["roles"]["analysis"] == "claude"
        assert "claude-code template" in err

    with cw_testlib.temp_home() as home, _no_claude_on_path():
        err = _cmd_setup()
        config = cw_store.read_json(home / "config.json")
        assert "proxy" in config["profiles"]
        assert "kind" not in config["profiles"]["proxy"]
        assert "proxy template" in err

    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        sentinel = {"profiles": {"mine": {"base_url": "http://x", "model": "m"}}, "roles": {}}
        cw_store.write_json(home / "config.json", sentinel)
        with cw_testlib.fake_claude(tmp, {}):
            _cmd_setup()
        assert cw_store.read_json(home / "config.json") == sentinel


def test_check():
    cases = [
        ("ok", {"sonnet": [cw_testlib.claude_result(structured={"ok": True})]}, None, 0,
         ["ok analysis (claude/sonnet)"]),
        ("not logged in", {}, {"loggedIn": False}, 1, ["FAIL analysis", "remedy:"]),
    ]
    for name, script, auth, rc, wanted in cases:
        with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
            cw_testlib.write_config(
                home, {"claude": {"kind": "claude-code", "model": "sonnet"}}, {"analysis": "claude"},
            )
            with cw_testlib.fake_claude(tmp, script, auth=auth):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    assert cw_mcp.cmd_check() == rc, name
                for text in wanted:
                    assert text in out.getvalue(), (name, out.getvalue())


def test_setup_under_plugin_install_writes_config_and_skips_snippet():
    with cw_testlib.temp_home() as home:
        with tempfile.TemporaryDirectory() as tmp:
            with cw_testlib.fake_claude(tmp, {}):
                plugin_dir = cw_mcp.SCRIPTS_DIR
                cw_mcp.SCRIPTS_DIR = Path("/some/path/.claude/plugins/cache/code-walkthrough/scripts")
                try:
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        assert cw_mcp.cmd_setup("print") == 0
                    assert out.getvalue() == ""
                    assert "already registered by the plugin install" in err.getvalue()
                    assert cw_store.read_json(home / "config.json") is not None
                finally:
                    cw_mcp.SCRIPTS_DIR = plugin_dir


def _anchor_doc(*ids):
    return {"qid": "q1", "thread_id": "t1", "anchor": {},
            "resolvable": [{"id": i, "path": "a.py", "line": 1, "author": "x", "body": "b"} for i in ids]}


def test_check_outcome_cases():
    doc = _anchor_doc("7")
    ok = {"thread": "7", "why": " settled "}
    assert cw_mcp.check_outcome(doc, "propose_resolve", ok) is None
    assert "unknown tool" in cw_mcp.check_outcome(doc, "nope", ok)
    for why in (None, "", "   ", 5):
        assert "why is required" in cw_mcp.check_outcome(doc, "propose_resolve", {"thread": "7", "why": why})
    assert "500" in cw_mcp.check_outcome(doc, "propose_resolve", {"thread": "7", "why": "x" * 501})
    assert cw_mcp.check_outcome(doc, "propose_resolve", {"thread": "7", "why": "x" * 500}) is None
    assert "not a review thread" in cw_mcp.check_outcome(doc, "propose_resolve", {"thread": "9", "why": "w"})
    assert "already suggested" in cw_mcp.check_outcome(doc, "propose_resolve", ok, already={"resolve:7"})
    for bad in (["7"], {"a": 1}, 7):
        assert "thread must be a string" in cw_mcp.check_outcome(doc, "propose_resolve", {"thread": bad, "why": "w"})


def _page_doc(kind="block"):
    anchor = {"kind": "line", "path": "a.py", "line": 1} if kind == "line" else {
        "kind": kind, kind: "overview:p1", "section": "Overview"}
    return {"qid": "q1", "thread_id": "t1", "anchor": anchor, "resolvable": []}


def _edit(**over):
    args = {"op": "insert_after", "target": "overview:p1", "block": {"type": "mermaid", "text": "flowchart TD\nA-->B"}}
    return {**args, **over}


def test_check_page_edit_cases():
    doc = _page_doc()
    check = lambda args, d=doc, **kw: cw_mcp.check_outcome(d, "propose_page_edit", args, **kw)
    assert check(_edit()) is None
    assert check(_edit(op="replace", block={"type": "list", "text": "- a"})) is None
    assert check(_edit(target="Overview"), _page_doc("section")) is None
    assert "op must be" in check(_edit(op="delete"))
    assert "block.type" in check(_edit(block={"type": "html", "text": "<b>x</b>"}))
    assert "block.text is required" in check(_edit(block={"type": "prose", "text": "  "}))
    assert "block must be an object" in check(_edit(block="x"))
    assert "4000" in check(_edit(block={"type": "prose", "text": "x" * 4001}))
    assert check(_edit(block={"type": "prose", "text": "x" * 4000})) is None
    assert check(_edit(block={"type": "mermaid", "text": "x" * 6000})) is None
    assert "6000" in check(_edit(block={"type": "mermaid", "text": "x" * 6001}))
    assert "target must be overview:p1" in check(_edit(target="other:p9"))
    assert "reply instead" in check(_edit(), _page_doc("line"))
    mixed = {**_page_doc(), "anchors": [_page_doc("line")["anchor"], _page_doc()["anchor"]]}
    assert check(_edit(), mixed) is None
    assert "target must be overview:p1" in check(_edit(target="a.py"), mixed)
    task = {"title": "t", "steps": ["s"], "files": ["a.py"]}
    assert cw_mcp.check_outcome(mixed, "propose_task", task) is None
    assert "diff lines" in cw_mcp.check_outcome(_page_doc(), "propose_task", task)
    assert "already proposed a page edit" in check(_edit(), already={"page_edit"})
    assert cw_mcp.check_outcome(doc, "propose_resolve", {"thread": "7", "why": "w"}, already={"page_edit"}) is not None


class _Rpc:
    def __init__(self, *argv):
        self.proc = subprocess.Popen(
            [sys.executable, str(Path(SCRIPTS_DIR) / "cw_mcp.py"), *argv],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.n = 0

    def call(self, method, params=None):
        self.n += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method,
                                          "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def close(self):
        self.proc.stdin.close()
        try:
            return self.proc.wait(timeout=5)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()


def _propose(rpc, thread, why="settled"):
    return rpc.call("tools/call", {"name": "propose_resolve", "arguments": {"thread": thread, "why": why}})["result"]


def test_outcomes_server_accepts_and_rejects():
    with cw_testlib.temp_home(), tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "turns").mkdir()
        (d / "turns" / "q1.anchor.json").write_text(json.dumps(_anchor_doc("7")))
        rpc = _Rpc("outcomes", "--dir", str(d), "--qid", "q1")
        try:
            assert [t["name"] for t in rpc.call("tools/list")["result"]["tools"]] == [
                "propose_resolve", "propose_page_edit", "propose_github_draft", "propose_task", "post_draft_comments"]
            rpc.proc.stdin.write("garbage{\n")
            rpc.proc.stdin.flush()
            assert rpc.call("ping")["result"] == {}
            good = _propose(rpc, "7")
            assert good["isError"] is False
            lines = (d / "turns" / "q1.outcomes.jsonl").read_text().splitlines()
            assert len(lines) == 1
            rec = json.loads(lines[0])
            assert rec["oid"].startswith("o-") and rec["arguments"] == {"thread": "7", "why": "settled"}
            assert _propose(rpc, "7")["isError"] is True
            assert _propose(rpc, "9")["isError"] is True
            assert _propose(rpc, ["x"], why="y")["isError"] is True
            assert rpc.call("ping")["result"] == {}
            assert _propose(rpc, "7", why="")["isError"] is True
            assert len((d / "turns" / "q1.outcomes.jsonl").read_text().splitlines()) == 1
        finally:
            assert rpc.close() == 0


def test_outcome_server_lists_tools_by_mode():
    review = ["propose_resolve", "propose_page_edit", "propose_github_draft", "propose_task", "post_draft_comments"]
    full = ["propose_resolve", "propose_page_edit", "propose_github_draft", "post_draft_comments",
            "reload_walkthrough"]
    assert [t["name"] for t in cw_mcp.outcome_tools()] == review
    assert [t["name"] for t in cw_mcp.outcome_tools("full")] == full
    assert "--mode" not in cw_mcp.outcome_mcp_config("/d", "q1")["mcpServers"]["cw"]["args"]
    assert cw_mcp.outcome_mcp_config("/d", "q1", "full")["mcpServers"]["cw"]["args"][-2:] == ["--mode", "full"]
    with cw_testlib.temp_home(), tempfile.TemporaryDirectory() as tmp:
        for extra, names in (([], review), (["--mode", "review"], review), (["--mode", "full"], full)):
            rpc = _Rpc("outcomes", "--dir", tmp, "--qid", "q1", *extra)
            try:
                assert [t["name"] for t in rpc.call("tools/list")["result"]["tools"]] == names
            finally:
                assert rpc.close() == 0


def test_outcomes_server_page_edit_is_stored_stripped_and_once_per_turn():
    with cw_testlib.temp_home(), tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "turns").mkdir()
        (d / "turns" / "q1.anchor.json").write_text(json.dumps(_page_doc()))
        rpc = _Rpc("outcomes", "--dir", str(d), "--qid", "q1")
        try:
            def call(args):
                return rpc.call("tools/call", {"name": "propose_page_edit", "arguments": args})["result"]

            assert call(_edit(target="x:p1"))["isError"] is True
            assert not (d / "turns" / "q1.outcomes.jsonl").exists()
            good = call(_edit(block={"type": "prose", "text": "  hello \n"}))
            assert good["isError"] is False
            rec = json.loads((d / "turns" / "q1.outcomes.jsonl").read_text())
            assert rec["name"] == "propose_page_edit"
            assert rec["arguments"] == {"op": "insert_after", "target": "overview:p1",
                                        "block": {"type": "prose", "text": "hello"}}
            again = call(_edit())
            assert again["isError"] is True and "already proposed" in again["content"][0]["text"]
            assert len((d / "turns" / "q1.outcomes.jsonl").read_text().splitlines()) == 1
        finally:
            assert rpc.close() == 0


def test_outcomes_answers_each_request_id_once(monkeypatch, tmp_path):
    written = []

    def write_then_raise(obj):
        written.append(obj)
        raise BrokenPipeError("gone")

    monkeypatch.setattr(cw_mcp, "_write_response", write_then_raise)
    monkeypatch.setattr(cw_mcp, "_log_file", lambda m: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 5, "method": "ping"}) + "\n"))
    assert cw_mcp.cmd_outcomes(tmp_path, "q1") == 0
    assert [w["id"] for w in written] == [5]


def test_outcomes_missing_anchor_is_an_error():
    with cw_testlib.temp_home(), tempfile.TemporaryDirectory() as tmp:
        rpc = _Rpc("outcomes", "--dir", tmp, "--qid", "q1")
        try:
            res = _propose(rpc, "7")
        finally:
            rpc.close()
        assert res["isError"] is True
        assert "unavailable" in res["content"][0]["text"]


COMMENT = "  needs a guard\n```py\nx = 1\n```\n<b>now</b> \U0001F680\n"
REPLYABLE = [{"id": "gh-9", "path": "a.py", "line": 3, "author": "r", "body": "b"}]
ANCHORS = {
    "line": {"kind": "line", "path": "a.py", "line": 3, "side": "RIGHT", "end_line": None, "hunk_id": "h"},
    "thread": {"kind": "thread", "note_id": "gh-9"},
    "block": {"kind": "block", "block": "overview:p1", "section": "Overview"},
    "section": {"kind": "section", "section": "Overview"},
}


def _draft_doc(kind="line", replyable=REPLYABLE):
    return {"qid": "q1", "thread_id": "t1", "anchor": ANCHORS[kind], "comment": COMMENT,
            "resolvable": [], "replyable": replyable}


NEW = {"kind": "new"}
REPLY = {"kind": "reply", "note_id": "gh-9"}


def test_check_github_draft_cases():
    check = lambda args, kind="line", **kw: cw_mcp.check_outcome(_draft_doc(kind), "propose_github_draft", args, **kw)
    assert check({"body": "b", "target": NEW}) is None
    assert check({"body": "b", "target": REPLY}) is None
    assert check({"body": "b", "target": REPLY}, "thread") is None
    for kind in ("block", "section"):
        for target in (NEW, REPLY):
            assert "reply instead" in check({"body": "b", "target": target}, kind)
    assert "not on a diff line, so a new review comment cannot be drafted" in check({"body": "b", "target": NEW}, "thread")
    assert "not a review thread" in check({"body": "b", "target": {"kind": "reply", "note_id": "gh-1"}})
    assert "not a review thread" in check({"body": "b", "target": {"kind": "reply", "note_id": ["gh-9"]}})
    assert "target.kind" in check({"body": "b", "target": {"kind": "edit"}})
    assert "target.kind" in check({"body": "b", "target": "new"})
    for body in (None, "", "  \n", 5):
        assert "body is required" in check({"body": body, "target": NEW})
    assert "4000" in check({"body": "x" * 4001, "target": NEW})
    assert check({"body": "x" * 4000, "target": NEW}) is None
    assert "boolean" in check({"body": "b", "target": NEW, "verbatim": "yes"})
    two = {**_draft_doc(), "anchors": [ANCHORS["line"], {**ANCHORS["line"], "line": 4}]}
    assert "several diff-line references" in cw_mcp.check_outcome(two, "propose_github_draft", {"body": "b", "target": NEW})
    mixed = {**_draft_doc(), "anchors": [ANCHORS["block"], ANCHORS["line"]]}
    assert cw_mcp.check_outcome(mixed, "propose_github_draft", {"body": "b", "target": NEW}) is None
    assert "already drafted" in check({"body": "b", "target": NEW}, already={"github_draft"})
    assert check({"body": "b", "target": NEW}, already={"page_edit", "resolve:7"}) is None


def test_github_draft_payload_copies_target_and_verbatim_is_byte_for_byte():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)

        def accept(qid, doc, args):
            cw_store.write_json(d / "turns" / f"{qid}.anchor.json", doc)
            assert cw_mcp.accept_outcome(d, qid, "propose_github_draft", args)[0]
            return json.loads((d / "turns" / f"{qid}.outcomes.jsonl").read_text().splitlines()[-1])["arguments"]

        (d / "turns").mkdir()
        new = accept("q1", _draft_doc(), {"body": "  reworded  ", "target": NEW})
        assert new == {"body": "reworded", "original": COMMENT, "verbatim": False,
                       "target": {"kind": "new", "path": "a.py", "line": 3, "side": "RIGHT",
                                  "end_line": None, "hunk_id": "h"}}
        verbatim = accept("q2", _draft_doc(), {"body": "something else entirely", "target": NEW, "verbatim": True})
        assert verbatim["body"] == COMMENT == verbatim["original"] and verbatim["verbatim"] is True
        triage = accept("q4", {**_draft_doc(), "source": "triage"}, {"body": "b", "target": NEW, "verbatim": True})
        assert triage["original"] == "" and triage["verbatim"] is False
        reply = accept("q3", _draft_doc("thread"), {"body": "b", "target": REPLY, "verbatim": True})
        assert reply["body"] == COMMENT
        assert reply["target"] == {"kind": "reply", "note_id": "gh-9", "path": "a.py", "line": 3}
        ok, text = cw_mcp.accept_outcome(d, "q3", "propose_github_draft", {"body": "b", "target": REPLY})
        assert not ok and "already drafted" in text


def test_free_comment_refuses_every_proposal_tool():
    free = {**_draft_doc(), "anchor": {"kind": "free"}, "anchors": []}
    calls = {"propose_task": {"title": "t", "steps": ["s"], "files": ["a.py"]},
             "propose_page_edit": _edit(),
             "propose_github_draft": {"body": "b", "target": NEW}}
    for name, args in calls.items():
        assert "no reference" in cw_mcp.check_outcome(free, name, args), name
    doc = {**free, "dir": _diff_dir()}
    assert cw_mcp.check_outcome(doc, "post_draft_comments", {"comments": [_dc()]}) is None


def test_github_draft_original_has_no_ref_fences():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "turns").mkdir()
        doc = {**_draft_doc(), "comment": "why?\n\n```ref a.py:3\nx = 1\n```\n\nthanks"}
        cw_store.write_json(d / "turns" / "q1.anchor.json", doc)
        assert cw_mcp.accept_outcome(d, "q1", "propose_github_draft", {"body": "b", "target": NEW})[0]
        rec = json.loads((d / "turns" / "q1.outcomes.jsonl").read_text().splitlines()[-1])
        assert "```" not in rec["arguments"]["original"] and "why?" in rec["arguments"]["original"]


def test_ensure_server_replaces_a_daemon_on_older_code_unless_it_is_busy():
    spawned = []
    old = None
    new = None
    real_spawn, real_wait, real_probe = cw_mcp._spawn_daemon, cw_mcp._wait_pid_exit, cw_mcp._probe_health

    def spawn():
        nonlocal new
        spawned.append(1)
        old._serve_thread.join(5)
        new = cw_server.Daemon(idle_s=None, write_server_json=True).start()

    with cw_testlib.temp_home():
        old = cw_server.Daemon(idle_s=None, write_server_json=True).start()
        cw_mcp._spawn_daemon, cw_mcp._wait_pid_exit = spawn, lambda pid, timeout=5: None
        try:
            assert cw_mcp.ensure_server()["port"] == old.port
            assert not spawned

            cw_mcp._probe_health = lambda port: {**real_probe(port), "code": "older-install"}
            old._asks = 1
            assert cw_mcp.ensure_server()["port"] == old.port
            assert not spawned and not old._stopped.is_set()

            old._asks = 0
            assert cw_mcp.ensure_server()["port"] == new.port
            assert spawned and old._stopped.is_set()
        finally:
            cw_mcp._spawn_daemon, cw_mcp._wait_pid_exit, cw_mcp._probe_health = real_spawn, real_wait, real_probe
            old.stop()
            if new:
                new.stop()


DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,3 +1,4 @@
 one
-two
+TWO
+three
 four
@@ -20,2 +21,2 @@
 x
-y
+Y
"""
_DIFF_DIRS = []


def _diff_dir():
    d = tempfile.mkdtemp()
    _DIFF_DIRS.append(d)
    (Path(d) / "raw.diff").write_text(DIFF)
    return d


def _dc(**over):
    return {"path": "a.py", "line": 2, "body": "fix this", **over}


def test_check_draft_comments_cases():
    doc = {**_page_doc(), "dir": _diff_dir()}

    def check(*comments, already=()):
        return cw_mcp.check_outcome(doc, "post_draft_comments", {"comments": list(comments)}, already)

    assert check(_dc()) is None
    assert check(_dc(line=1, end_line=4), _dc(side="LEFT", line=2)) is None
    assert check(_dc(path="b.py")) == "comments[0]: b.py is not in the diff"
    assert check(_dc(), _dc(line=120)) == "comments[1]: line 120 of a.py (RIGHT) is not in the diff"
    assert "not in one contiguous" in check(_dc(line=4, end_line=21))
    assert "end_line" in check(_dc(line=3, end_line=2))
    assert "suggestion" in check(_dc(side="LEFT", line=2, suggestion="x"))
    assert "1 to 30" in check(*[_dc()] * 31)
    assert "1 to 30" in check()
    assert "body is required" in check(_dc(body="  "))
    assert "at most 4000" in check(_dc(body="x" * 4001))
    assert "at most 4000" in check(_dc(suggestion="x" * 4001))
    assert "already posted" in check(_dc(), already={"draft_comments"})

    payload = cw_mcp._draft_comments_payload({"comments": [
        _dc(body="a ```b``` c", suggestion="x = 1"), _dc(body="a ```b``` c", suggestion="x = 1"),
        _dc(line=3, end_line=3, suggestion="```\nz\n```")]})
    assert len(payload["comments"]) == 2
    assert payload["comments"][0]["body"] == "a ```b``` c\n\n```suggestion\nx = 1\n```"
    assert payload["comments"][1]["end_line"] is None
    assert payload["comments"][1]["body"].endswith("````suggestion\n```\nz\n```\n````")
    for ticks in (3, 4, 7):
        text = "`" * ticks
        body = cw_mcp._draft_comments_payload({"comments": [_dc(suggestion=text)]})["comments"][0]["body"]
        fence = "`" * (ticks + 1)
        assert body.endswith(f"{fence}suggestion\n{text}\n{fence}")


def test_outcomes_dispatch_rejects_tools_outside_the_mode(tmp_path):
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "propose_task", "arguments": {}}}
    called = []
    resp = cw_mcp._outcomes_dispatch(tmp_path, "q1", req, cw_mcp.outcome_tools("full"),
                                     lambda n, a: called.append(n) or (True, "ok"))
    assert resp["result"]["isError"] is True and not called


def test_accept_draft_comments_once_per_turn_on_free_thread():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "turns").mkdir()
        (d / "raw.diff").write_text(DIFF)
        cw_store.write_json(d / "turns" / "q1.anchor.json", {**_draft_doc(), "anchor": {"kind": "free"}, "anchors": []})
        assert cw_mcp.accept_outcome(d, "q1", "post_draft_comments", {"comments": [_dc()]})[0]
        ok, msg = cw_mcp.accept_outcome(d, "q1", "post_draft_comments", {"comments": [_dc()]})
        assert not ok and "already posted" in msg
        rec = json.loads((d / "turns" / "q1.outcomes.jsonl").read_text())
        assert rec["arguments"] == {"comments": [{"path": "a.py", "line": 2, "end_line": None, "side": "RIGHT",
                                                  "body": "fix this"}]}


def test_reload_walkthrough_is_full_only_and_once_per_turn():
    assert "reload_walkthrough" not in [t["name"] for t in cw_mcp.outcome_tools("review")]
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "turns").mkdir()
        cw_store.write_json(d / "turns" / "q1.anchor.json", {**_draft_doc(), "anchor": {"kind": "free"}, "anchors": []})
        ok, msg = cw_mcp.accept_outcome(d, "q1", "reload_walkthrough", {})
        assert not ok and "only available in Full mode" in msg
        assert not (d / "turns" / "q1.outcomes.jsonl").exists()
        cw_store.write_json(d / "turns" / "q1.anchor.json",
                            {**_draft_doc(), "mode": "full", "anchor": {"kind": "free"}, "anchors": []})
        assert cw_mcp.accept_outcome(d, "q1", "reload_walkthrough", {})[0]
        ok, msg = cw_mcp.accept_outcome(d, "q1", "reload_walkthrough", {})
        assert not ok and "already requested" in msg
        assert json.loads((d / "turns" / "q1.outcomes.jsonl").read_text())["arguments"] == {}
