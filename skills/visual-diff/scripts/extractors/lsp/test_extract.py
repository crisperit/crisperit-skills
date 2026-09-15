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
        test_end_to_end_cross_file_call_resolves,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
