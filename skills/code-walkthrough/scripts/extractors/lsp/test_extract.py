#!/usr/bin/env python3
"""Self-check for extract.py. Pure-logic tests exercise symbol naming, path filtering and CLI
argument parsing directly (no language server needed); the end-to-end test drives a real
typescript-language-server against a two-file fixture and skips when it is missing or cannot
load TypeScript."""

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import cw_testlib  # noqa: E402
import extract  # noqa: E402
from lsp_client import LSPClient, uri  # noqa: E402

ROOT = "/repo"


class _StubLSPClient:
    """Fakes the request/notify/shutdown surface extract_edges calls on LSPClient, keyed by LSP
    method name only -- every request for a given method gets the same canned `result` (or, for
    a response that must vary by call, a callable of `params`). Lets the zero-guard tests below
    drive extract_edges's control flow without a real language server."""

    def __init__(self, responses):
        self._responses = responses

    def request(self, method, params, timeout=30):
        resp = self._responses.get(method)
        if callable(resp):
            resp = resp(params)
        return {"result": resp}

    def notify(self, method, params):
        pass

    def shutdown(self):
        pass


@pytest.fixture
def fast_retries(monkeypatch):
    """Shrinks typescript's retry budget and kills the inter-attempt sleep so the guard tests
    run in milliseconds instead of replaying extract_edges's real warmup/retry timings."""
    monkeypatch.setitem(extract.LANGUAGES["typescript"], "first_file_retries", 1)
    monkeypatch.setattr(extract, "DOC_SYMBOL_RETRY_DELAY", 0)


def _stub_client(monkeypatch, responses):
    monkeypatch.setattr(extract, "LSPClient", lambda cmd, cwd: _StubLSPClient(responses))


def test_symbol_name():
    rows = [
        ({"kind": extract.KIND_METHOD, "name": "method", "detail": "MyClass"}, "MyClass.method"),
        ({"kind": extract.KIND_FUNCTION, "name": "helper", "detail": ""}, "helper"),
        # A Function item should never gain a container prefix even if `detail` is set to
        # something unrelated (e.g. a module name some servers put there).
        ({"kind": extract.KIND_FUNCTION, "name": "helper", "detail": "some/module"}, "helper"),
    ]
    for item, want in rows:
        assert extract._symbol_name(item) == want, item


def test_uri_to_relpath():
    py_vendor = extract.LANGUAGES["python"]["vendor_dirs"]
    rs_vendor = extract.LANGUAGES["rust"]["vendor_dirs"]
    rows = [
        ("inside root", f"file://{ROOT}/src/a.ts", (), "src/a.ts"),
        ("outside root", "file:///elsewhere/a.ts", (), None),
        # A dependency's own .d.ts (TS lib internals like Array.push) is noise, not application
        # call-graph, even though it lives inside root on disk.
        ("node_modules", f"file://{ROOT}/node_modules/typescript/lib/lib.es5.d.ts", (), None),
        ("python .venv", f"file://{ROOT}/.venv/lib/x.py", (py_vendor,), None),
        ("python source", f"file://{ROOT}/src/a.py", (py_vendor,), "src/a.py"),
        ("rust target", f"file://{ROOT}/target/debug/build/x.rs", (rs_vendor,), None),
        ("rust source", f"file://{ROOT}/src/main.rs", (rs_vendor,), "src/main.rs"),
    ]
    for label, file_uri, extra, want in rows:
        assert extract._uri_to_relpath(file_uri, ROOT, *extra) == want, label


def test_decl_span():
    sel = {"start": {"line": 4, "character": 9}, "end": {"line": 4, "character": 10}}
    rng = {"start": {"line": 2, "character": 0}, "end": {"line": 6, "character": 1}}
    rows = [
        # selectionRange is the name token; range often starts earlier (e.g. a decorator) -- start
        # must land on the name, not there.
        ("prefers selectionRange for start", {"selectionRange": sel, "range": rng}, (5, 7)),
        ("range start when selectionRange missing", {"range": rng}, (3, 7)),
        ("end falls back to start when range missing", {"selectionRange": sel}, (5, 5)),
        ("neither present", {}, (None, None)),
    ]
    for label, item, want in rows:
        assert extract._decl_span(item) == want, label


def test_parse_args(tmp_path):
    list_path = tmp_path / "files.txt"
    list_path.write_text("a.ts\n\nb.ts\n")
    rows = [
        ("files list", ["/repo", "--files-list", str(list_path)], ["a.ts", "b.ts"]),
        ("positional files", ["/repo", "a.ts", "b.ts"], ["a.ts", "b.ts"]),
        ("no files is a check-only invocation", ["/repo"], []),
    ]
    for label, argv, want in rows:
        root, rel_files = extract._parse_args(argv)
        assert root == "/repo" and rel_files == want, label


def test_shutdown_kills_a_process_that_ignores_terminate(monkeypatch, tmp_path):
    # A process that traps and ignores SIGTERM must still be gone once shutdown() returns --
    # proves the terminate()-then-wait()-then-kill() escalation actually reaps it, rather than
    # leaving a hung tsserver orphaned once the caller's worktree is deleted out from under it.
    # The child never reads stdin, so shutdown()'s "shutdown" request would wait its full 5s and
    # the post-terminate wait another 5s; both are shortened here instead of in the product.
    ready = tmp_path / "ready"
    script = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(ready)!r}, 'w').close()\n"
        "time.sleep(30)\n"
    )
    client = LSPClient([sys.executable, "-c", script], cwd=".")
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists(), "child never installed its SIGTERM handler"

    real_wait = client.proc.wait
    timed_out = []

    def short_wait(timeout=None):
        try:
            return real_wait(timeout=min(timeout, 0.3))
        except subprocess.TimeoutExpired:
            timed_out.append(timeout)
            raise

    monkeypatch.setattr(client, "request", lambda *a, **kw: {})
    monkeypatch.setattr(client.proc, "wait", short_wait)
    start = time.monotonic()
    client.shutdown()
    elapsed = time.monotonic() - start
    assert timed_out, "terminate alone reaped the child, so the kill escalation was never exercised"
    assert client.proc.poll() is not None
    assert elapsed < 5


def test_check_language_server_reports_path_trap_when_binary_found_but_off_path(monkeypatch, tmp_path):
    # A binary sitting in the known install dir but missing from PATH is a different failure
    # than one that was never installed -- reinstalling would be a no-op, so the reason must
    # name the directory instead of just saying "not found".
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "typescript-language-server").write_text("#!/bin/sh\n")
    monkeypatch.setattr(extract.shutil, "which", lambda tool: None)
    monkeypatch.setitem(extract.KNOWN_INSTALL_DIRS, "typescript", lambda: str(bin_dir))

    ok, reason = extract.check_language_server("/repo", "typescript")

    assert not ok
    assert str(bin_dir) in reason
    assert "not on PATH" in reason


def test_check_language_server_reports_plain_not_found_when_nothing_hits_the_known_dir(monkeypatch):
    # rust has no KNOWN_INSTALL_DIRS entry: rustup's own layout has no single well-known
    # directory the way npm's global bin dir does, so it keeps the plain "not found" reason.
    monkeypatch.setattr(extract.shutil, "which", lambda tool: None)

    ok, reason = extract.check_language_server("/repo", "rust")

    assert not ok
    assert reason == "rust-analyzer not found on PATH"


def test_check_language_server_explains_missing_node_modules_when_callhierarchy_missing(monkeypatch, tmp_path):
    # The reason must name node_modules, not just repeat "does not advertise
    # callHierarchyProvider" -- that alone reads as a server bug and hides the real, fixable cause.
    monkeypatch.setattr(extract.shutil, "which", lambda tool: f"/usr/local/bin/{tool}")
    _stub_client(monkeypatch, {"initialize": {"capabilities": {}}})

    ok, reason = extract.check_language_server(tmp_path, "typescript")

    assert not ok
    assert "does not advertise callHierarchyProvider" in reason
    assert f"{tmp_path}/node_modules is missing or a broken link" in reason


_FUNC_SYMBOL = {
    "name": "f",
    "kind": extract.KIND_FUNCTION,
    "selectionRange": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
    "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 10}},
}

_CALL_ITEM = {
    "name": "f",
    "kind": extract.KIND_FUNCTION,
    "uri": "file:///repo/a.ts",
    "range": _FUNC_SYMBOL["range"],
    "selectionRange": _FUNC_SYMBOL["selectionRange"],
}


def test_extract_edges_raises_when_no_file_yields_any_symbols(monkeypatch, tmp_path, fast_retries):
    # The server answers every request but documentSymbol always comes back empty -- up but
    # indexing nothing (wrong root, missing tsconfig, never warmed).
    _stub_client(monkeypatch, {"textDocument/documentSymbol": None})
    (tmp_path / "a.ts").write_text("export function a() {}\n")
    (tmp_path / "b.ts").write_text("export function b() {}\n")

    with pytest.raises(RuntimeError) as exc:
        extract.extract_edges(tmp_path, ["a.ts", "b.ts"])

    assert "typescript-language-server" in str(exc.value)
    assert "2 file(s)" in str(exc.value)


def test_extract_edges_raises_when_every_call_hierarchy_lookup_comes_back_empty(monkeypatch, tmp_path, fast_retries):
    # documentSymbol resolves a real function in both files, but prepareCallHierarchy exhausts
    # its retry budget for every one of them -- the case the retries exist to survive, not this.
    _stub_client(monkeypatch, {
        "textDocument/documentSymbol": [_FUNC_SYMBOL],
        "textDocument/prepareCallHierarchy": None,
    })
    (tmp_path / "a.ts").write_text("export function f() {}\n")
    (tmp_path / "b.ts").write_text("export function f() {}\n")

    with pytest.raises(RuntimeError) as exc:
        extract.extract_edges(tmp_path, ["a.ts", "b.ts"])

    assert "2 symbol(s) tried" in str(exc.value)


def test_extract_edges_returns_empty_without_raising_when_calls_just_dont_resolve(monkeypatch, tmp_path, fast_retries):
    # Symbols and call hierarchy both resolve fine; the calls themselves are empty. A diff whose
    # changed symbols genuinely call nothing new is normal and must not be treated as a failure.
    _stub_client(monkeypatch, {
        "textDocument/documentSymbol": [_FUNC_SYMBOL],
        "textDocument/prepareCallHierarchy": [_CALL_ITEM],
        "callHierarchy/outgoingCalls": [],
        "callHierarchy/incomingCalls": [],
    })
    (tmp_path / "a.ts").write_text("export function f() {}\n")

    assert extract.extract_edges(tmp_path, ["a.ts"]) == []


def test_extract_edges_returns_empty_without_raising_when_existing_is_empty(monkeypatch, tmp_path):
    # None of rel_files exist on disk at this ref -- the ordinary base side of an all-new diff.
    _stub_client(monkeypatch, {})

    assert extract.extract_edges(tmp_path, ["missing.ts"]) == []


def test_extract_edges_includes_start_end_positions_on_both_sides(monkeypatch, tmp_path, fast_retries):
    # Outgoing and incoming feed the same symbol's own span into opposite add_edge slots; giving
    # every item a distinct range catches a from/to swap that identical ranges would hide.
    sym = {
        "name": "f",
        "kind": extract.KIND_FUNCTION,
        "selectionRange": {"start": {"line": 10, "character": 9}, "end": {"line": 10, "character": 10}},
        "range": {"start": {"line": 10, "character": 0}, "end": {"line": 12, "character": 1}},
    }
    prepared_item = {
        "name": "f", "kind": extract.KIND_FUNCTION, "uri": uri(str(tmp_path / "a.ts")),
        "range": sym["range"], "selectionRange": sym["selectionRange"],
    }
    callee_item = {
        "name": "callee", "kind": extract.KIND_FUNCTION, "uri": uri(str(tmp_path / "b.ts")),
        "selectionRange": {"start": {"line": 0, "character": 9}, "end": {"line": 0, "character": 15}},
        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 2, "character": 1}},
    }
    caller_item = {
        "name": "caller", "kind": extract.KIND_FUNCTION, "uri": uri(str(tmp_path / "c.ts")),
        "selectionRange": {"start": {"line": 20, "character": 9}, "end": {"line": 20, "character": 15}},
        "range": {"start": {"line": 20, "character": 0}, "end": {"line": 22, "character": 1}},
    }
    _stub_client(monkeypatch, {
        "textDocument/documentSymbol": [sym],
        "textDocument/prepareCallHierarchy": [prepared_item],
        "callHierarchy/outgoingCalls": [{"to": callee_item}],
        "callHierarchy/incomingCalls": [{"from": caller_item}],
    })
    (tmp_path / "a.ts").write_text("export function f() {}\n")

    edges = extract.extract_edges(tmp_path, ["a.ts"])

    outgoing = next(e for e in edges if e["ToSym"] == "callee")
    assert (outgoing["FromStart"], outgoing["FromEnd"]) == (11, 13)
    assert (outgoing["ToStart"], outgoing["ToEnd"]) == (1, 3)

    incoming = next(e for e in edges if e["FromSym"] == "caller")
    assert (incoming["FromStart"], incoming["FromEnd"]) == (21, 23)
    assert (incoming["ToStart"], incoming["ToEnd"]) == (11, 13)


def test_extract_edges_skips_a_symbol_whose_call_hierarchy_request_times_out(monkeypatch, tmp_path, fast_retries):
    # A single symbol's outgoingCalls hanging past call_timeout (rust-analyzer's workspace-wide
    # search on a large crate graph, in practice) must skip that one symbol, not abort
    # extraction for every other symbol in the run.
    def prepare_call_hierarchy(params):
        file_uri = params["textDocument"]["uri"]
        return [{**_CALL_ITEM, "uri": file_uri}]

    def outgoing_calls(params):
        if params["item"]["uri"].endswith("a.ts"):
            raise TimeoutError("callHierarchy/outgoingCalls timed out after 15s")
        return []

    _stub_client(monkeypatch, {
        "textDocument/documentSymbol": [_FUNC_SYMBOL],
        "textDocument/prepareCallHierarchy": prepare_call_hierarchy,
        "callHierarchy/outgoingCalls": outgoing_calls,
        "callHierarchy/incomingCalls": [],
    })
    (tmp_path / "a.ts").write_text("export function f() {}\n")
    (tmp_path / "b.ts").write_text("export function f() {}\n")

    # Must not raise: a.ts's symbol is skipped, b.ts's still resolves (to no edges).
    assert extract.extract_edges(tmp_path, ["a.ts", "b.ts"]) == []


def test_end_to_end_cross_file_call_resolves(tmp_path):
    cw_testlib.require_ts_language_server()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions": {"target": "es2020", "module": "commonjs"}}\n'
    )
    (tmp_path / "a.ts").write_text(
        'import { callee } from "./b";\n\nexport function caller() {\n    return callee();\n}\n'
    )
    (tmp_path / "b.ts").write_text("export function callee() {\n    return 1;\n}\n")

    ok, reason = extract.check_language_server(tmp_path)
    assert ok, reason

    edges = extract.extract_edges(tmp_path, ["a.ts", "b.ts"])
    identities = [{k: e[k] for k in ("FromFile", "FromSym", "ToFile", "ToSym")} for e in edges]
    assert {"FromFile": "a.ts", "FromSym": "caller", "ToFile": "b.ts", "ToSym": "callee"} in identities
