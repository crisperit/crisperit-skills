#!/usr/bin/env python3
"""Self-check for cw_run.py. Assert-based; run with pytest. Uses the loopback StubLLM from
cw_testlib and real cw_llm, never a real network. conftest.py zeroes BACKOFF_S so retry tests
stay fast."""

import contextlib
import io
import json
import sys
import tempfile
import threading
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import cw_llm  # noqa: E402
import cw_run  # noqa: E402
import cw_store  # noqa: E402
import cw_testlib  # noqa: E402
import fanout  # noqa: E402


def _config(**overrides):
    cfg = dict(cw_store.DEFAULTS)
    cfg.update(overrides)
    return cfg


def _prepare_batches(tmp, repo, base, head, max_lines=1):
    """Capture + split a repo's base...head diff into a walkthrough dir's batches/, the way
    prepare_walkthrough would, without needing a model backend."""
    d = Path(tmp) / "work"
    d.mkdir(parents=True, exist_ok=True)
    diff_text = cw_testlib.git(repo, "diff", f"{base}...{head}")
    (d / "raw.diff").write_text(diff_text)
    batches_dir = d / "batches"
    batches_dir.mkdir()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fanout.do_split(types.SimpleNamespace(
            diff=str(d / "raw.diff"), out=str(batches_dir), max_lines=max_lines, max_batches=8))
    manifest = json.loads(buf.getvalue())
    (batches_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return d, manifest


def _passing_fragment(seed):
    return {"files": [
        {"path": f["path"], "role": "does a thing",
         "hunks": [{"header": h["header"], "note": ""} for h in f["hunks"]]}
        for f in seed["files"]
    ]}


# ---------------------------------------------------------------------------
# fragment gate: invented content, retry/escalate/seed, LLMError attempt-advance
# ---------------------------------------------------------------------------

def test_invented_file_or_hunk_cannot_reach_disk():
    with tempfile.TemporaryDirectory() as tmp:
        repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\nb\nc\n"}, {"foo.py": "a\nX\nc\n"})
        d, manifest = _prepare_batches(tmp, repo, base, head)
        entry = manifest[0]
        seed = json.loads(Path(entry["seed"]).read_text())
        real_header = seed["files"][0]["hunks"][0]["header"]
        evil = {"files": [
            {"path": "evil.py", "role": "r", "hunks": [{"header": "@@ -1,1 +1,1 @@", "note": "n"}]},
            {"path": "foo.py", "role": "ok", "hunks": [
                {"header": real_header, "note": "real"},
                {"header": "@@ -99,1 +99,1 @@", "note": "invented hunk"},
            ]},
        ]}
        with cw_testlib.StubLLM({"m": [cw_testlib.tool_call("submit_fragment", evil)] * 5}) as stub:
            config = _config(profiles={"a": stub.profile("m")}, roles={"analysis": "a"})
            _ok, fragment, _problems, _err = cw_run._run_fragment_conversation(d, 1, entry, config, None, "analysis")
        assert fragment is not None
        paths = [f["path"] for f in fragment["files"]]
        assert "evil.py" not in paths  # invented file dropped
        assert paths == ["foo.py"]
        headers = [h["header"] for h in fragment["files"][0]["hunks"]]
        assert "@@ -99,1 +99,1 @@" not in headers  # invented hunk dropped
        assert headers == [h["header"] for h in seed["files"][0]["hunks"]]  # only the seed's own hunks


@pytest.mark.parametrize("kind", ["http", "claude-code"])
def test_failing_fragment_gate_escalates(kind):
    cw_testlib.require_posix()
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\nb\nc\n"}, {"foo.py": "a\nX\nc\n"})
        d, manifest = _prepare_batches(tmp, repo, base, head)
        (d / "head").mkdir()
        entry = manifest[0]
        if kind == "http":
            always_bad = cw_testlib.tool_call("submit_fragment", {"files": []})
            stub = stack.enter_context(
                cw_testlib.StubLLM({"analysis-m": [always_bad] * 10, "escalate-m": [always_bad] * 10}))
            profiles = {"a": stub.profile("analysis-m"), "e": stub.profile("escalate-m")}
            first, second, count, log = "analysis-m", "escalate-m", stub.count, None
        else:
            always_bad = cw_testlib.claude_result(structured={"files": []})
            fc = stack.enter_context(
                cw_testlib.fake_claude(tmp, {"sonnet": [always_bad] * 10, "opus": [always_bad] * 10}))
            profiles = {"a": {"kind": "claude-code", "model": "sonnet"},
                        "e": {"kind": "claude-code", "model": "opus"}}
            first, second, count, log = "sonnet", "opus", fc.count, fc.log
        config = _config(profiles=profiles, roles={"analysis": "a", "escalate": "e"})
        ok = cw_run._run_batch(d, 1, entry, config, None)
        assert ok is False
        assert count(first) == 3
        assert count(second) == 3
        meta = json.loads((d / "meta.json").read_text())
        step = meta["steps"]["batch-1"]
        assert step["attempts"] == 2
        assert step["model"] == second
        assert step["status"] == "failed"
        assert not (d / "batches" / "fragment-1.json").exists()
        if log:
            assert all(Path(e["cwd"]).resolve() == (d / "head").resolve() for e in log())


def test_llm_error_counts_as_one_failed_conversation_and_moves_on():
    with tempfile.TemporaryDirectory() as tmp:
        repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\nb\nc\n"}, {"foo.py": "a\nX\nc\n"})
        d, manifest = _prepare_batches(tmp, repo, base, head)
        entry = manifest[0]
        seed = json.loads(Path(entry["seed"]).read_text())
        good = _passing_fragment(seed)
        good["files"][0]["role"] = "does the X thing"
        with cw_testlib.StubLLM({
            "bad": [cw_testlib.http_error(500)] * 3,
            "good": [cw_testlib.tool_call("submit_fragment", good)],
        }) as stub:
            config = _config(profiles={"a": stub.profile("bad"), "e": stub.profile("good")},
                              roles={"analysis": "a", "escalate": "e"})
            ok = cw_run._run_batch(d, 1, entry, config, None)
        assert ok is True
        assert stub.count("bad") == 3
        assert stub.count("good") == 1
        meta = json.loads((d / "meta.json").read_text())
        step = meta["steps"]["batch-1"]
        assert step["attempts"] == 2
        # the "bad" role's three attempts all 500 before any usage-bearing response arrives
        assert meta["usage"]["escalate"]["calls"] == 1
        assert meta["usage"]["escalate"]["prompt_tokens"] > 0


def test_a_batch_thread_that_raises_is_recorded_as_a_failed_result_not_dropped():
    # A misconfigured profile makes cw_store.role_profile raise CWError from inside the batch
    # worker thread; run()'s per-batch thread wrapper must still record `results[n] = False`
    # instead of leaving the key missing (which would make the "did any batch fail" scan blind
    # to a thread that died).
    with tempfile.TemporaryDirectory() as tmp:
        repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\nb\nc\n"}, {"foo.py": "a\nX\nc\n"})
        d, manifest = _prepare_batches(tmp, repo, base, head)
        entry = manifest[0]
        config = _config(profiles={"a": {"base_url": "", "model": ""}}, roles={"analysis": "a"})

        results = {}
        lock = threading.Lock()

        t = threading.Thread(target=cw_run._run_batch_guarded, args=(d, 1, entry, config, None, results, lock))
        t.start()
        t.join(timeout=10)
        assert results.get(1) is False


# ---------------------------------------------------------------------------
# claude-code backend: cwd=head, gate retry/escalate, mixed kinds, thread worker
# ---------------------------------------------------------------------------

def test_mixed_kinds_claude_code_analysis_fails_http_escalate_passes():
    cw_testlib.require_posix()
    with tempfile.TemporaryDirectory() as tmp:
        repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\nb\nc\n"}, {"foo.py": "a\nX\nc\n"})
        d, manifest = _prepare_batches(tmp, repo, base, head)
        (d / "head").mkdir()
        entry = manifest[0]
        seed = json.loads(Path(entry["seed"]).read_text())
        good = _passing_fragment(seed)
        good["files"][0]["role"] = "does the X thing"
        with cw_testlib.fake_claude(
                tmp, {"sonnet": [cw_testlib.claude_result(is_error=True, result="boom")]}) as fc, \
                cw_testlib.StubLLM({"escalate-m": [cw_testlib.tool_call("submit_fragment", good)]}) as stub:
            config = _config(
                profiles={"a": {"kind": "claude-code", "model": "sonnet"}, "e": stub.profile("escalate-m")},
                roles={"analysis": "a", "escalate": "e"},
            )
            ok = cw_run._run_batch(d, 1, entry, config, None)
        assert ok is True
        assert fc.count("sonnet") == 1
        assert stub.count("escalate-m") == 1
        meta = json.loads((d / "meta.json").read_text())
        step = meta["steps"]["batch-1"]
        assert step["attempts"] == 2
        assert step["model"] == "escalate-m"
        assert Path(fc.log()[-1]["cwd"]).resolve() == (d / "head").resolve()


def test_thread_worker_need_diffs_for_sends_diffs_in_second_prompt():
    cw_testlib.require_posix()
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "work"
        d.mkdir()
        (d / "head").mkdir()
        seed = {
            "thread_id": "t1",
            "thread": {"thread_id": "t1"},
            "commits": [{"sha": "abc123", "committed_at": "2024-01-01T00:00:00Z"}],
            "diffs": {},
            "cache_path_positive": str(d / "cache" / "positive.json"),
            "cache_path_null": str(d / "cache" / "null.json"),
        }
        diffs = {"abc123": "diff --git a/foo.py b/foo.py\n"}
        resolution = {
            "thread_id": "t1", "outcome": "none", "closing_message": "", "ticket": None,
            "commits": [], "files": [], "why": "no code change needed", "confidence": "high",
        }
        with cw_testlib.fake_claude(tmp, {"sonnet": [
            cw_testlib.claude_result(structured={"thread_id": "t1", "need_diffs_for": ["abc123"]}),
            cw_testlib.claude_result(structured=resolution),
        ]}) as fc:
            config = _config(profiles={"a": {"kind": "claude-code", "model": "sonnet"}},
                              roles={"analysis": "a"})
            result = cw_run._thread_worker(d, 1, seed, diffs, config, on_event=None)
        assert result == resolution
        assert (d / "resolutions" / "thread-1.json").exists()
        second_stdin = fc.log()[-1]["stdin"]
        assert "Diffs:" in second_stdin
        assert all(Path(e["cwd"]).resolve() == (d / "head").resolve() for e in fc.log())


# ---------------------------------------------------------------------------
# small-diff route
# ---------------------------------------------------------------------------

def test_small_diff_makes_one_prose_conversation_and_no_batch_calls():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\nb\nc\n"}, {"foo.py": "a\nX\nc\n"})
        with cw_testlib.StubLLM({"prose-m": [cw_testlib.tool_call("submit_analysis", {
            "files": [{"path": "foo.py", "role": "r", "hunks": []}],
            "overview": "o", "verdict": "v",
        })] * 10}) as stub:
            cw_testlib.write_config(
                home, {"a": stub.profile("analysis-m"), "p": stub.profile("prose-m")},
                {"analysis": "a", "prose": "p"}, small_diff_lines=1000000, batch_max_lines=1,
            )
            d, meta, _reused = cw_run.prepare_walkthrough({
                "repo": str(repo), "base": base, "head": head, "target": "main...HEAD", "slug": "t",
            })
            assert meta["route"] == "small"
            cw_run.run(d)
        assert stub.count("analysis-m") == 0
        assert stub.count("prose-m") >= 1


GOOD_SVG = ("<svg viewBox='0 0 100 40' role='img' aria-label='x'>"
            "<rect x='1' y='1' width='50' height='20' fill='var(--card)'/></svg>")


def _visuals_dir(home, tmp, stub, symbols=True, groups=True):
    repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\nb\nc\n"}, {"foo.py": "a\nX\nc\n"})
    cw_testlib.write_config(home, {"p": stub.profile("prose-m")}, {"analysis": "p", "prose": "p"},
                            small_diff_lines=1000000, batch_max_lines=1)
    d, meta, _reused = cw_run.prepare_walkthrough({
        "repo": str(repo), "base": base, "head": head, "target": "main...HEAD", "slug": "t"})
    analysis = {"target": "main...HEAD", "overview": "Overview text", "verdict": "v", "files": [],
                "groups": [{"title": "Core", "why": "the change", "paths": ["foo.py"]}] if groups else []}
    (d / "analysis.json").write_text(json.dumps(analysis))
    if symbols:
        (d / "symdelta.json").write_text(json.dumps({"language": "python", "nodes": [
            {"id": "foo.py:run", "label": "run", "kind": "symbol", "file": "foo.py",
             "state": "changed", "range": [2, 2]}]}))
    return d, meta


def _submit(args):
    return cw_testlib.tool_call("submit_visuals", args)


def test_visuals_pass_repairs_then_writes_visuals_json():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM({}) as stub:
            d, meta = _visuals_dir(home, tmp, stub)
            bad = {"pr": {"title": "T", "html": "<img src='x'>"}, "groups": []}
            good = {"pr": {"title": "T", "html": GOOD_SVG}, "groups": [{"index": 0, "html": "<p>g</p>"}]}
            stub.script = {"prose-m": [_submit(bad), _submit(good)]}
            assert cw_run._run_visuals(d, meta, cw_store.load_config(), None)
            gate = [m["content"] for m in stub.requests[1]["messages"] if m.get("role") == "tool"][-1]
            assert gate.startswith("gate:") and "<img> is not allowed" in gate
            assert gate.endswith("fix and call submit_visuals again")
            assert "foo.py:run | foo.py:2-2 | changed" in cw_testlib.last_user_text(stub.requests[0])
            saved = json.loads((d / "visuals.json").read_text())
            assert saved["pr"]["title"] == "T" and saved["groups"] == [{"index": 0, "html": "<p>g</p>"}]
            assert cw_store.read_meta(d)["steps"]["visuals"]["status"] == "ok"


def test_visuals_pass_prefers_the_visuals_role_over_prose():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM({}) as stub:
            d, meta = _visuals_dir(home, tmp, stub)
            cw_testlib.write_config(home, {"p": stub.profile("prose-m"), "v": stub.profile("vis-m")},
                                    {"analysis": "p", "prose": "p", "visuals": "v"})
            stub.script = {"vis-m": [_submit({"pr": None, "groups": [{"index": 0, "html": "<p>n</p>"}]})]}
            assert cw_run._run_visuals(d, meta, cw_store.load_config(), None)
            assert stub.count("vis-m") == 1 and stub.count("prose-m") == 0


def test_visuals_pass_keeps_the_clean_part_on_the_final_attempt():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM({}) as stub:
            d, meta = _visuals_dir(home, tmp, stub)
            mixed = {"pr": {"title": "T", "html": "<img src='x'>"},
                     "groups": [{"index": 0, "html": "<p>g</p>"}]}
            stub.script = {"prose-m": [_submit(mixed)]}
            assert cw_run._run_visuals(d, meta, cw_store.load_config(), None)
            saved = json.loads((d / "visuals.json").read_text())
            assert saved["pr"] is None and saved["groups"][0]["index"] == 0
            assert stub.count("prose-m") == 3


def test_visuals_pass_fails_when_nothing_clean_survives_the_final_attempt():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM({}) as stub:
            d, meta = _visuals_dir(home, tmp, stub)
            bad = {"pr": {"title": "T", "html": "<img src='x'>"}, "groups": []}
            stub.script = {"prose-m": [_submit(bad)]}
            assert not cw_run._run_visuals(d, meta, cw_store.load_config(), None)
            assert not (d / "visuals.json").exists()
            step = cw_store.read_meta(d)["steps"]["visuals"]
            assert step["status"] == "failed" and "<img> is not allowed" in step["error"]


def test_visuals_pass_skips_when_visuals_json_exists_unless_forced():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM({}) as stub:
            d, meta = _visuals_dir(home, tmp, stub)
            (d / "visuals.json").write_text('{"pr": null, "groups": []}')
            stub.script = {"prose-m": [_submit({"pr": None, "groups": [{"index": 0, "html": "<p>n</p>"}]})]}
            assert not cw_run._run_visuals(d, meta, cw_store.load_config(), None)
            assert stub.count("prose-m") == 0
            assert cw_store.read_meta(d)["steps"]["visuals"]["status"] == "skipped"
            assert cw_run._run_visuals(d, meta, cw_store.load_config(), None, force=True)
            assert json.loads((d / "visuals.json").read_text())["groups"][0]["html"] == "<p>n</p>"


def test_visuals_pass_runs_without_symbols_when_there_are_groups():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM({}) as stub:
            d, meta = _visuals_dir(home, tmp, stub, symbols=False)
            stub.script = {"prose-m": [_submit({"pr": None, "groups": [{"index": 0, "html": "<p>n</p>"}]})]}
            assert cw_run._run_visuals(d, meta, cw_store.load_config(), None)
            assert "symbols (id" not in cw_testlib.last_user_text(stub.requests[0])
            assert "0 | Core | the change | foo.py" in cw_testlib.last_user_text(stub.requests[0])


def test_visuals_llm_error_marks_the_step_failed_and_never_raises():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM({}) as stub:
            d, meta = _visuals_dir(home, tmp, stub)
            stub.script = {"prose-m": [cw_testlib.http_error(400, "nope")]}
            assert not cw_run._run_visuals(d, meta, cw_store.load_config(), None)
            assert not (d / "visuals.json").exists()
            assert cw_store.read_meta(d)["steps"]["visuals"]["status"] == "failed"


def test_visuals_pass_runs_after_symdelta_joined_and_a_failed_pass_leaves_the_run_done(monkeypatch):
    order = []
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\nb\nc\n"}, {"foo.py": "a\nX\nc\n"})
        with cw_testlib.StubLLM({}) as stub:
            cw_testlib.write_config(home, {"p": stub.profile("prose-m")}, {"analysis": "p", "prose": "p"},
                                    small_diff_lines=1000000, batch_max_lines=1)
            d, meta, _reused = cw_run.prepare_walkthrough({
                "repo": str(repo), "base": base, "head": head, "target": "main...HEAD", "slug": "t"})
            seed = json.loads((d / "batches" / "fragment-1.seed.json").read_text())
            files = [{"path": f["path"], "role": "r",
                      "hunks": [{"header": h["header"], "note": "n"} for h in f["hunks"]]}
                     for f in seed["files"]]
            stub.script = {"prose-m": [cw_testlib.tool_call("submit_analysis", {
                "files": files, "overview": "o", "verdict": "v"})]}

            def slow_symdelta(d_, meta_, on_event):
                threading.Event().wait(0.3)
                order.append("symdelta")

            def failing_visuals(d_, meta_, config, on_event, force=False):
                order.append("visuals")
                return False

            monkeypatch.setattr(cw_run, "_run_symdelta", slow_symdelta)
            monkeypatch.setattr(cw_run, "_run_visuals", failing_visuals)
            monkeypatch.setattr(cw_run, "_final_build", lambda *a, **k: True)
            assert cw_run.run(d) == "done"
        assert order == ["symdelta", "visuals"]


def test_visuals_error_inside_the_pass_leaves_the_run_done(monkeypatch):
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        with cw_testlib.StubLLM({}) as stub:
            d, meta = _visuals_dir(home, tmp, stub)
            monkeypatch.setattr(cw_run, "_visuals_user_text", lambda *a: 1 / 0)
            assert not cw_run._run_visuals(d, meta, cw_store.load_config(), None)
            assert cw_store.read_meta(d)["steps"]["visuals"]["status"] == "failed"


def test_submit_tools_no_longer_take_visuals():
    for tool in (cw_run._prose_submit_tool(), cw_run._analysis_submit_tool()):
        assert "visuals" not in tool["function"]["parameters"]["properties"]
    assert "visuals" in cw_run.STEPS and cw_run.STEPS.index("visuals") == cw_run.STEPS.index("symdelta") + 1


@pytest.mark.parametrize("route, small_diff_lines, generic", [
    ("small", 1000000, "small route failed the fragment gate"),
    ("fanout", 0, "batch 1 failed the fragment gate"),
])
def test_llm_error_reaches_run_failure(route, small_diff_lines, generic):
    cw_testlib.require_posix()
    # Regression for the real run that failed with the misleading generic "failed the fragment
    # gate" / re-run remedy while the actual cause (a bad model -> 403) only lived in
    # runs/prose-1.json. No gate problems are ever produced here (and no escalate role exists for
    # the fanout batch), so the run must carry the LLMError's own message and remedy out through
    # meta.json instead of the generic wording.
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\nb\nc\n"}, {"foo.py": "a\nX\nc\n"})
        always_403 = cw_testlib.claude_result(
            is_error=True,
            result="Failed to authenticate. API Error: 403 team not allowed to access model sonnet",
            api_error_status=403,
        )
        with cw_testlib.fake_claude(tmp, {"sonnet": [always_403] * 10}):
            cw_testlib.write_config(
                home, {"p": {"kind": "claude-code", "model": "sonnet"}}, {"analysis": "p", "prose": "p"},
                small_diff_lines=small_diff_lines, batch_max_lines=1,
            )
            d, meta, _reused = cw_run.prepare_walkthrough({
                "repo": str(repo), "base": base, "head": head, "target": "main...HEAD", "slug": "t",
            })
            assert meta["route"] == route
            status = cw_run.run(d)
        assert status == "failed"
        saved = json.loads((d / "meta.json").read_text())
        assert saved["status"] == "failed"
        assert "403" in saved["error"]
        assert saved["error"] != generic
        assert saved["remedy"] == cw_llm._CLAUDE_AUTH_REMEDY


# ---------------------------------------------------------------------------
# classifier and routing
# ---------------------------------------------------------------------------

def test_classify_prose_floor_file_fatal():
    diff_paths = {"a.py", "b.py"}
    assert cw_run._classify("missing top-level key: overview", diff_paths)[0] == "prose"
    assert cw_run._classify("overview is empty", diff_paths)[0] == "prose"
    assert cw_run._classify("verdict is empty", diff_paths)[0] == "prose"
    assert cw_run._classify("files must be a list", diff_paths)[0] == "prose"
    assert cw_run._classify("groups must be a list", diff_paths)[0] == "prose"
    assert cw_run._classify("a.py: in both group 0 and group 1", diff_paths)[0] == "prose"
    assert cw_run._classify("3/5 hunks (60%) have an empty note, above the 80% floor", diff_paths)[0] == "floor"
    assert cw_run._classify("a.py: role is empty", diff_paths) == ("file", "a.py")
    assert cw_run._classify("something unexpected happened", diff_paths)[0] == "fatal"
    assert cw_run._classify("groups[0] has no title", {"groups_helper.py"})[0] == "prose"


def test_file_problem_routes_to_its_owning_batch():
    with tempfile.TemporaryDirectory() as tmp:
        repo, base, head = cw_testlib.make_repo(
            tmp, {"a.py": "1\n2\n3\n", "b.py": "x\ny\nz\n"},
            {"a.py": "1\nA\n3\n", "b.py": "x\nB\nz\n"},
        )
        d, manifest = _prepare_batches(tmp, repo, base, head)
        assert len(manifest) == 2
        path_to_n = {p: n for n, entry in enumerate(manifest, 1) for p in entry["files"]}
        b_n = path_to_n["b.py"]
        a_n = path_to_n["a.py"]

        for n, entry in enumerate(manifest, 1):
            seed = json.loads(Path(entry["seed"]).read_text())
            (d / "batches" / f"fragment-{n}.json").write_text(json.dumps(_passing_fragment(seed)))
        (d / "prose.json").write_text(json.dumps({"target": "t", "overview": "", "verdict": ""}))

        b_seed = json.loads(Path(manifest[b_n - 1]["seed"]).read_text())
        fixed = {"files": [{"path": "b.py", "role": "fixed role", "hunks": b_seed["files"][0]["hunks"]}]}
        with cw_testlib.StubLLM({"fix-m": [cw_testlib.tool_call("submit_fragment", fixed)]}) as stub:
            config = _config(profiles={"a": stub.profile("fix-m")}, roles={"analysis": "a"})
            cw_run._route_and_finalize(d, {"target": "t"}, config, None, 2, manifest, ["b.py: role is empty"])
        assert stub.count("fix-m") == 1
        frag_a_after = json.loads((d / "batches" / f"fragment-{a_n}.json").read_text())
        assert frag_a_after["files"][0]["role"] == "does a thing"  # untouched by routing


# ---------------------------------------------------------------------------
# final gate failure
# ---------------------------------------------------------------------------

def test_final_gate_failure_gives_failed_status_with_gate_lines():
    with tempfile.TemporaryDirectory() as tmp:
        repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\nb\n"}, {"foo.py": "a\nX\n"})
        d = Path(tmp) / "work"
        d.mkdir()
        diff_text = cw_testlib.git(repo, "diff", f"{base}...{head}")
        (d / "raw.diff").write_text(diff_text)
        (d / "analysis.json").write_text(json.dumps({"files": []}))  # missing required top-level keys
        meta = {"repo": str(repo), "base": base, "head": head, "slug": "t", "paths": [], "explain": False}
        events = []
        ok = cw_run._final_build(d, meta, {}, lambda ev, data: events.append((ev, data)))
        assert ok is False
        saved = json.loads((d / "meta.json").read_text())
        assert saved["status"] == "failed"
        assert saved["gate"]
        assert saved["remedy"]
        # run() returns "failed" here without raising, so the step/run/failed event -- the
        # one SSE consumers watch to learn the run has ended -- must come from _final_build
        # itself, or it never fires at all.
        run_events = [data for ev, data in events if ev == "step" and data.get("name") == "run"]
        assert run_events and run_events[-1]["status"] == "failed"


# ---------------------------------------------------------------------------
# tool roots
# ---------------------------------------------------------------------------

def test_safe_path_refuses_every_escape():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "w"
        (d / "head").mkdir(parents=True)
        (d / "head" / "real.txt").write_text("ok")
        outside = Path(tmp) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("nope")

        for bad in ("../../etc/passwd", "/etc/passwd", "walkthrough/../../server.json"):
            try:
                cw_run.safe_path(d, bad)
                raise AssertionError(f"expected refusal for {bad!r}")
            except cw_store.CWError:
                pass

        (d / "head" / "escape").symlink_to(outside)
        try:
            cw_run.safe_path(d, "escape/secret.txt")
            raise AssertionError("expected refusal for a symlink escaping the root")
        except cw_store.CWError:
            pass


def test_grep_with_pathological_pattern_returns_within_timeout(monkeypatch):
    cw_testlib.require_git()
    monkeypatch.setattr(cw_run, "GREP_TIMEOUT", 0.3)
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "w"
        d.mkdir(parents=True)
        (d / "f.txt").write_text("a" * 50000 + "!\n")
        _tools, handlers = cw_run.read_tools(d)
        assert handlers["grep"]({"pattern": "(a+)+$", "path": "walkthrough/f.txt"}) == "grep timed out"


def test_worktree_reads_a_file_at_head_not_the_dirty_working_tree():
    with tempfile.TemporaryDirectory() as tmp:
        repo, _base, head = cw_testlib.make_repo(tmp, {"foo.py": "committed\n"}, {"foo.py": "committed\nmore\n"})
        (repo / "foo.py").write_text("DIRTY UNCOMMITTED\n")
        d = Path(tmp) / "w"
        d.mkdir()
        cw_run.add_worktree(str(repo), head, d / "head")
        _tools, handlers = cw_run.read_tools(d)
        result = handlers["read_file"]({"path": "foo.py"})
        assert "DIRTY" not in result
        assert "committed" in result


# ---------------------------------------------------------------------------
# prepare_walkthrough
# ---------------------------------------------------------------------------

def test_no_backend_configured_raises_cwerror_with_remedy():
    with cw_testlib.temp_home(), tempfile.TemporaryDirectory() as tmp:
        repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\n"}, {"foo.py": "b\n"})
        try:
            cw_run.prepare_walkthrough({"repo": str(repo), "base": base, "head": head,
                                         "target": "main...HEAD", "slug": "t"})
            raise AssertionError("expected CWError")
        except cw_store.CWError as e:
            assert e.remedy


def test_pr_target_gives_cwerror():
    # Phase 3: a PR target no longer refuses outright, but it still needs a GitHub origin to
    # compute gh_repo from -- this repo has none, so it fails the same way a non-GitHub
    # remote would (test_cw_pr.py covers the full id/sig/refresh behaviour for a real origin).
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\n"}, {"foo.py": "b\n"})
        cw_testlib.write_config(home, {"p": {"base_url": "http://x", "model": "m"}},
                                 {"analysis": "p", "prose": "p"})
        try:
            cw_run.prepare_walkthrough({"repo": str(repo), "base": base, "head": head,
                                         "target": "t", "slug": "t", "pr": 5})
            raise AssertionError("expected CWError")
        except cw_store.CWError as e:
            assert "GitHub remote" in str(e)
            assert e.remedy


def test_fresh_rebuilds_a_done_run_and_keeps_user_state():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        repo, base, head = cw_testlib.make_repo(tmp, {"foo.py": "a\n"}, {"foo.py": "b\n"})
        cw_testlib.write_config(home, {"p": {"base_url": "http://127.0.0.1:1", "model": "m"}},
                                 {"analysis": "p", "prose": "p"})
        params = {"repo": str(repo), "base": base, "head": head, "target": "t", "slug": "t"}
        d, _meta, _reused = cw_run.prepare_walkthrough(params)
        cw_store.update_meta(d, lambda m: m.update({"status": "done"}))
        gone = ("analysis.json", "visuals.json", "structure.json")
        kept = ("qa.jsonl", "page-notes.json", "threads.json", "state.json")
        for name in gone + kept:
            (d / name).write_text("{}")
        (d / "turns").mkdir(exist_ok=True)
        (d / "turns" / "1.json").write_text("{}")

        _d, _meta, reused = cw_run.prepare_walkthrough(params)
        assert reused is True
        assert all((d / name).exists() for name in gone)

        _d, meta, reused = cw_run.prepare_walkthrough({**params, "fresh": True})
        assert reused is True
        assert meta["status"] == "building"
        assert not any((d / name).exists() for name in gone)
        assert all((d / name).exists() for name in kept)
        assert (d / "turns" / "1.json").exists()


def test_reuse_keeps_a_passing_fragment_at_the_same_sig_a_new_head_clears_it():
    with cw_testlib.temp_home() as home, tempfile.TemporaryDirectory() as tmp:
        repo, base, _head = cw_testlib.make_repo(tmp, {"foo.py": "a\nb\n"}, {"foo.py": "a\nX\n"})
        cw_testlib.write_config(home, {"p": {"base_url": "http://127.0.0.1:1", "model": "m"}},
                                 {"analysis": "p", "prose": "p"}, small_diff_lines=0, batch_max_lines=1)
        # target is a fixed label, independent of base/head, so the same walkthrough id is used
        # even after head moves (a real daemon resolves base/head from the agent's refs each call).
        params = {"repo": str(repo), "base": base, "head": "HEAD", "target": "main...HEAD", "slug": "t"}

        d, _meta, reused = cw_run.prepare_walkthrough(params)
        assert reused is False
        seed = json.loads((d / "batches" / "fragment-1.seed.json").read_text())
        frag = _passing_fragment(seed)
        (d / "batches" / "fragment-1.json").write_text(json.dumps(frag))
        cw_store.update_meta(d, lambda m: m.update({"status": "failed", "batches_done": [1]}))

        d2, _meta2, reused2 = cw_run.prepare_walkthrough(params)
        assert reused2 is True
        assert d2 == d
        assert json.loads((d2 / "batches" / "fragment-1.json").read_text()) == frag

        (repo / "foo.py").write_text("a\nY\n")
        cw_testlib.git(repo, "add", "-A")
        cw_testlib.git(repo, "commit", "-q", "--amend", "-m", "head2")

        d3, _meta3, _reused3 = cw_run.prepare_walkthrough(params)
        assert d3 == d
        assert not (d3 / "batches" / "fragment-1.json").exists()


@pytest.mark.parametrize("out,rc,expected", [("abc\n", 0, True), ("zzz\n", 0, False), ("abc\n", 1, False)])
def test_head_on_github_needs_gh_to_confirm_the_head_oid(monkeypatch, out, rc, expected):
    monkeypatch.setattr(cw_run, "_gh", lambda meta, args, **kw: types.SimpleNamespace(returncode=rc, stdout=out))
    assert cw_run._head_on_github({"pr": 5, "gh_repo": "o/r", "head": "abc"}) is expected
