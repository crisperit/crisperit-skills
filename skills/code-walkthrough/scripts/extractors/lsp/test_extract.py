#!/usr/bin/env python3
"""Self-check for extract.py. Pure-logic tests exercise symbol naming, path filtering and CLI
argument parsing directly (no language server needed); the end-to-end test drives a real
typescript-language-server against a two-file fixture and is skipped when the tool isn't on
PATH, the same convention test_symdelta.py uses for its Go end-to-end test."""

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import extract  # noqa: E402
from lsp_client import LSPClient  # noqa: E402

ROOT = "/repo"


def test_symbol_name_qualifies_a_method_with_its_container():
    item = {"kind": extract.KIND_METHOD, "name": "method", "detail": "MyClass"}
    assert extract._symbol_name(item) == "MyClass.method"


def test_symbol_name_leaves_a_free_function_bare():
    item = {"kind": extract.KIND_FUNCTION, "name": "helper", "detail": ""}
    assert extract._symbol_name(item) == "helper"


def test_symbol_name_ignores_detail_on_a_non_method_kind():
    # A Function item should never gain a container prefix even if `detail` is set to
    # something unrelated (e.g. a module name some servers put there).
    item = {"kind": extract.KIND_FUNCTION, "name": "helper", "detail": "some/module"}
    assert extract._symbol_name(item) == "helper"


def test_uri_to_relpath_resolves_inside_root():
    assert extract._uri_to_relpath(f"file://{ROOT}/src/a.ts", ROOT) == "src/a.ts"


def test_uri_to_relpath_rejects_outside_root():
    assert extract._uri_to_relpath("file:///elsewhere/a.ts", ROOT) is None


def test_uri_to_relpath_rejects_node_modules():
    # A dependency's own .d.ts (TS lib internals like Array.push) is noise, not application
    # call-graph, even though it lives inside root on disk.
    assert extract._uri_to_relpath(f"file://{ROOT}/node_modules/typescript/lib/lib.es5.d.ts", ROOT) is None


def test_flatten_symbols_keeps_only_method_and_function_kinds_at_any_depth():
    doc_syms = [
        {
            "name": "MyClass", "kind": 5,  # Class -- not queried directly
            "children": [
                {"name": "method", "kind": extract.KIND_METHOD, "children": []},
                {"name": "prop", "kind": 7, "children": []},  # Property, e.g. an object-literal key
            ],
        },
        {"name": "helper", "kind": extract.KIND_FUNCTION},
    ]
    flat = extract._flatten_symbols(doc_syms)
    assert {s["name"] for s in flat} == {"method", "helper"}


def test_parse_args_reads_a_files_list():
    with tempfile.TemporaryDirectory() as tmp:
        list_path = Path(tmp) / "files.txt"
        list_path.write_text("a.ts\n\nb.ts\n")
        root, rel_files = extract._parse_args(["/repo", "--files-list", str(list_path)])
        assert root == "/repo"
        assert rel_files == ["a.ts", "b.ts"]


def test_parse_args_reads_positional_files():
    root, rel_files = extract._parse_args(["/repo", "a.ts", "b.ts"])
    assert root == "/repo"
    assert rel_files == ["a.ts", "b.ts"]


def test_parse_args_with_no_files_is_a_check_only_invocation():
    root, rel_files = extract._parse_args(["/repo"])
    assert root == "/repo" and rel_files == []


def test_shutdown_kills_a_process_that_ignores_terminate():
    # A process that traps and ignores SIGTERM must still be gone once shutdown() returns --
    # proves the terminate()-then-wait()-then-kill() escalation actually reaps it, rather than
    # leaving a hung tsserver orphaned once the caller's worktree is deleted out from under it.
    script = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(30)\n"
    client = LSPClient([sys.executable, "-c", script], cwd=".")
    start = time.time()
    client.shutdown()
    elapsed = time.time() - start
    assert client.proc.poll() is not None
    assert elapsed < 25  # bounded by shutdown()'s own timeouts, not the process's 30s sleep


def test_check_language_server_reports_path_trap_when_binary_found_but_off_path():
    # A binary sitting in the known install dir but missing from PATH is a different failure
    # than one that was never installed -- reinstalling would be a no-op, so the reason must
    # name the directory instead of just saying "not found".
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp) / "bin"
        bin_dir.mkdir()
        (bin_dir / "typescript-language-server").write_text("#!/bin/sh\n")

        original_which = extract.shutil.which
        original_locate = extract.KNOWN_INSTALL_DIRS["typescript"]
        extract.shutil.which = lambda tool: None
        extract.KNOWN_INSTALL_DIRS["typescript"] = lambda: str(bin_dir)
        try:
            ok, reason = extract.check_language_server("/repo", "typescript")
        finally:
            extract.shutil.which = original_which
            extract.KNOWN_INSTALL_DIRS["typescript"] = original_locate

        assert not ok
        assert str(bin_dir) in reason
        assert "not on PATH" in reason


def test_check_language_server_reports_plain_not_found_when_nothing_hits_the_known_dir():
    # rust has no KNOWN_INSTALL_DIRS entry: rustup's own layout has no single well-known
    # directory the way npm's global bin dir does, so it keeps the plain "not found" reason.
    original_which = extract.shutil.which
    extract.shutil.which = lambda tool: None
    try:
        ok, reason = extract.check_language_server("/repo", "rust")
    finally:
        extract.shutil.which = original_which

    assert not ok
    assert reason == "rust-analyzer not found on PATH"


def test_check_language_server_explains_missing_node_modules_when_callhierarchy_missing():
    # The reason must name node_modules, not just repeat "does not advertise
    # callHierarchyProvider" -- that alone reads as a server bug and hides the real, fixable cause.
    original_which = extract.shutil.which
    original_client = extract.LSPClient
    extract.shutil.which = lambda tool: f"/usr/local/bin/{tool}"
    extract.LSPClient = lambda cmd, cwd: _StubLSPClient({"initialize": {"capabilities": {}}})
    try:
        with tempfile.TemporaryDirectory() as tmp:
            ok, reason = extract.check_language_server(tmp, "typescript")
    finally:
        extract.shutil.which = original_which
        extract.LSPClient = original_client

    assert not ok
    assert "does not advertise callHierarchyProvider" in reason
    assert f"{tmp}/node_modules is missing or a broken link" in reason


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


def _patch_lsp_profile_for_fast_retries():
    """Shrinks typescript's retry budget and kills the inter-attempt sleep so the guard tests
    below run in milliseconds instead of replaying extract_edges's real warmup/retry timings.
    Returns the (first_file_retries, DOC_SYMBOL_RETRY_DELAY) originals to restore."""
    original_retries = extract.LANGUAGES["typescript"]["first_file_retries"]
    original_delay = extract.DOC_SYMBOL_RETRY_DELAY
    extract.LANGUAGES["typescript"]["first_file_retries"] = 1
    extract.DOC_SYMBOL_RETRY_DELAY = 0
    return original_retries, original_delay


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


def test_extract_edges_raises_when_no_file_yields_any_symbols():
    # The server answers every request but documentSymbol always comes back empty -- up but
    # indexing nothing (wrong root, missing tsconfig, never warmed).
    original_client = extract.LSPClient
    original_retries, original_delay = _patch_lsp_profile_for_fast_retries()
    extract.LSPClient = lambda cmd, cwd: _StubLSPClient({"textDocument/documentSymbol": None})
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.ts").write_text("export function a() {}\n")
            (root / "b.ts").write_text("export function b() {}\n")
            try:
                extract.extract_edges(root, ["a.ts", "b.ts"])
                assert False, "expected RuntimeError"
            except RuntimeError as exc:
                assert "typescript-language-server" in str(exc)
                assert "2 file(s)" in str(exc)
    finally:
        extract.LSPClient = original_client
        extract.LANGUAGES["typescript"]["first_file_retries"] = original_retries
        extract.DOC_SYMBOL_RETRY_DELAY = original_delay


def test_extract_edges_raises_when_every_call_hierarchy_lookup_comes_back_empty():
    # documentSymbol resolves a real function in both files, but prepareCallHierarchy exhausts
    # its retry budget for every one of them -- the case the retries exist to survive, not this.
    original_client = extract.LSPClient
    original_retries, original_delay = _patch_lsp_profile_for_fast_retries()
    extract.LSPClient = lambda cmd, cwd: _StubLSPClient({
        "textDocument/documentSymbol": [_FUNC_SYMBOL],
        "textDocument/prepareCallHierarchy": None,
    })
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.ts").write_text("export function f() {}\n")
            (root / "b.ts").write_text("export function f() {}\n")
            try:
                extract.extract_edges(root, ["a.ts", "b.ts"])
                assert False, "expected RuntimeError"
            except RuntimeError as exc:
                assert "2 symbol(s) tried" in str(exc)
    finally:
        extract.LSPClient = original_client
        extract.LANGUAGES["typescript"]["first_file_retries"] = original_retries
        extract.DOC_SYMBOL_RETRY_DELAY = original_delay


def test_extract_edges_returns_empty_without_raising_when_calls_just_dont_resolve():
    # Symbols and call hierarchy both resolve fine; the calls themselves are empty. A diff whose
    # changed symbols genuinely call nothing new is normal and must not be treated as a failure.
    original_client = extract.LSPClient
    original_retries, original_delay = _patch_lsp_profile_for_fast_retries()
    extract.LSPClient = lambda cmd, cwd: _StubLSPClient({
        "textDocument/documentSymbol": [_FUNC_SYMBOL],
        "textDocument/prepareCallHierarchy": [_CALL_ITEM],
        "callHierarchy/outgoingCalls": [],
        "callHierarchy/incomingCalls": [],
    })
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.ts").write_text("export function f() {}\n")
            edges = extract.extract_edges(root, ["a.ts"])
            assert edges == []
    finally:
        extract.LSPClient = original_client
        extract.LANGUAGES["typescript"]["first_file_retries"] = original_retries
        extract.DOC_SYMBOL_RETRY_DELAY = original_delay


def test_extract_edges_returns_empty_without_raising_when_existing_is_empty():
    # None of rel_files exist on disk at this ref -- the ordinary base side of an all-new diff.
    original_client = extract.LSPClient
    extract.LSPClient = lambda cmd, cwd: _StubLSPClient({})
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            edges = extract.extract_edges(root, ["missing.ts"])
            assert edges == []
    finally:
        extract.LSPClient = original_client


def test_end_to_end_cross_file_call_resolves():
    if shutil.which("typescript-language-server") is None:
        print("skip (no typescript-language-server on PATH): test_end_to_end_cross_file_call_resolves")
        return

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "tsconfig.json").write_text(
            '{"compilerOptions": {"target": "es2020", "module": "commonjs"}}\n'
        )
        (root / "a.ts").write_text(
            'import { callee } from "./b";\n\nexport function caller() {\n    return callee();\n}\n'
        )
        (root / "b.ts").write_text("export function callee() {\n    return 1;\n}\n")

        ok, reason = extract.check_language_server(root)
        assert ok, reason

        edges = extract.extract_edges(root, ["a.ts", "b.ts"])
        assert {"FromFile": "a.ts", "FromSym": "caller", "ToFile": "b.ts", "ToSym": "callee"} in edges


if __name__ == "__main__":
    tests = [
        test_symbol_name_qualifies_a_method_with_its_container,
        test_symbol_name_leaves_a_free_function_bare,
        test_symbol_name_ignores_detail_on_a_non_method_kind,
        test_uri_to_relpath_resolves_inside_root,
        test_uri_to_relpath_rejects_outside_root,
        test_uri_to_relpath_rejects_node_modules,
        test_flatten_symbols_keeps_only_method_and_function_kinds_at_any_depth,
        test_parse_args_reads_a_files_list,
        test_parse_args_reads_positional_files,
        test_parse_args_with_no_files_is_a_check_only_invocation,
        test_shutdown_kills_a_process_that_ignores_terminate,
        test_check_language_server_reports_path_trap_when_binary_found_but_off_path,
        test_check_language_server_reports_plain_not_found_when_nothing_hits_the_known_dir,
        test_check_language_server_explains_missing_node_modules_when_callhierarchy_missing,
        test_extract_edges_raises_when_no_file_yields_any_symbols,
        test_extract_edges_raises_when_every_call_hierarchy_lookup_comes_back_empty,
        test_extract_edges_returns_empty_without_raising_when_calls_just_dont_resolve,
        test_extract_edges_returns_empty_without_raising_when_existing_is_empty,
        test_end_to_end_cross_file_call_resolves,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
