#!/usr/bin/env python3
"""Self-check for structure.py's generic (non-Python) parse path. Assert-based, no framework,
same throwaway-git-repo style as test_structure.py."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_PATH = Path(__file__).parent / "structure.py"

sys.path.insert(0, str(Path(__file__).parent))
from sections import render  # noqa: E402
import structure as structure_mod  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Structure Test",
    "GIT_AUTHOR_EMAIL": "structure-test@example.com",
    "GIT_COMMITTER_NAME": "Structure Test",
    "GIT_COMMITTER_EMAIL": "structure-test@example.com",
}


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env={**os.environ, **GIT_ENV},
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


def _write(repo, rel_path, content):
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _run(repo, base, head, files, no_tree_sitter=False):
    cmd = [sys.executable, str(SCRIPT_PATH), "--repo", str(repo), "--base", base, "--head", head,
           "--files", *files]
    env = dict(os.environ)
    if no_tree_sitter:
        env["VISUAL_DIFF_NO_TREE_SITTER"] = "1"
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"structure.py failed: {result.stderr}"
    return json.loads(result.stdout)


def _build_multilang_repo(repo):
    """Each non-Python file starts empty of structures and gains its classes/functions only
    at head, so build_mermaid's "always draw added/removed" rule guarantees they're on the
    diagram regardless of whether any edge happens to touch them."""
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "app.ts", "export const version = 1;\n")
    _write(repo, "main.go", "package main\n")
    _write(repo, "lib.rs", "// nothing yet\n")
    _write(repo, "mod.py", "def old():\n    pass\n")
    base = _commit(repo, "base")

    _write(
        repo, "app.ts",
        "class Uploader {\n  go() {\n    return 1;\n  }\n  extra() {\n    return 2;\n  }\n}\n"
        "function helper() {\n  return 1;\n}\n",
    )
    _write(
        repo, "main.go",
        "package main\n\ntype Server struct{}\n\nfunc (s *Server) Run() {\n\tdoWork()\n}\n\n"
        "func doWork() {}\n",
    )
    _write(
        repo, "lib.rs",
        "struct Exporter {}\n\nimpl Exporter {\n    fn export(&self) {\n        helper();\n"
        "    }\n}\n\nfn helper() {}\n",
    )
    _write(repo, "mod.py", "def old():\n    pass\n\n\ndef fresh():\n    pass\n")
    head = _commit(repo, "head")
    return base, head


def _tree_sitter_reachable():
    sys.path.insert(0, str(Path(__file__).parent))
    from structure import _get_parser  # noqa: E402
    return _get_parser("typescript", "typescript") is not None


def test_classes_and_functions_from_three_languages_appear():
    if not _tree_sitter_reachable():
        print("SKIP test_classes_and_functions_from_three_languages_appear: "
              "no tree-sitter grammar reachable in this environment")
        return
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        base, head = _build_multilang_repo(repo)

        result = _run(repo, base, head, ["app.ts", "main.go", "lib.rs", "mod.py"])

        mermaid = result["mermaid"]
        assert "Uploader (new)" in mermaid
        # Server has no members captured (an empty Go struct), so it renders with call
        # parens just like a bare function would -- a pre-existing quirk of members_of(),
        # not specific to the generic path.
        assert "Server() (new)" in mermaid
        assert "Run()" in mermaid
        assert "doWork()" in mermaid
        assert "Exporter (new)" in mermaid
        assert "export()" in mermaid
        assert "fresh() (new)" in mermaid  # the Python ast path, unaffected


def test_generic_files_are_not_marked_unsupported():
    if not _tree_sitter_reachable():
        print("SKIP test_generic_files_are_not_marked_unsupported: "
              "no tree-sitter grammar reachable in this environment")
        return
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        base, head = _build_multilang_repo(repo)

        result = _run(repo, base, head, ["app.ts", "main.go", "lib.rs", "mod.py"])

        assert result["skipped"]["unsupported"] == []
        assert "caveat" not in result  # dropped: the fixed legend text covers this
        assert "generic tree-sitter heuristic" in render("structure", result, "md")


def test_python_path_is_unaffected_by_the_generic_path():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        base, head = _build_multilang_repo(repo)

        result = _run(repo, base, head, ["mod.py"])

        assert result["structures_added"] == [["mod.py", "fresh"]]
        assert result["skipped"]["unsupported"] == []
        assert "caveat" not in result


def test_no_parser_reachable_falls_back_to_skipped_unsupported():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        base, head = _build_multilang_repo(repo)

        result = _run(
            repo, base, head, ["app.ts", "main.go", "lib.rs", "mod.py"], no_tree_sitter=True
        )

        assert set(result["skipped"]["unsupported"]) == {"app.ts", "main.go", "lib.rs"}
        assert result["structures_added"] == [["mod.py", "fresh"]]  # python path unaffected


def test_deeply_nested_expression_is_skipped_not_crashed():
    """Guards against a long chained expression (e.g. minified/generated code) blowing the
    recursion limit and crashing the whole run with no JSON on stdout; it should land in
    skipped.unsupported like any other file the generic path can't handle."""
    if not _tree_sitter_reachable():
        print("SKIP test_deeply_nested_expression_is_skipped_not_crashed: "
              "no tree-sitter grammar reachable in this environment")
        return
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, "deep.ts", "export const x = 1;\n")
        base = _commit(repo, "base")
        chain = "1" + "".join("+1" for _ in range(3000))
        _write(repo, "deep.ts", f"export const x = {chain};\n")
        head = _commit(repo, "head")

        result = _run(repo, base, head, ["deep.ts"])

        assert "deep.ts" in result["skipped"]["unsupported"]


def test_ts_class_extends_is_an_extends_edge():
    if not _tree_sitter_reachable():
        print("SKIP test_ts_class_extends_is_an_extends_edge: "
              "no tree-sitter grammar reachable in this environment")
        return
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, "base.ts", "export class Base {\n  run() {}\n}\n")
        _write(repo, "child.ts", "export class Child {}\n")
        base = _commit(repo, "base")
        _write(
            repo, "child.ts",
            "import { Base } from './base';\n\nexport class Child extends Base {}\n",
        )
        head = _commit(repo, "head")

        result = _run(repo, base, head, ["child.ts"])

        added = {(tuple(e["from"]), tuple(e["to"]), e["kind"]) for e in result["added"]}
        assert (("child.ts", "Child"), ("base.ts", "Base"), "extends") in added
        assert "==>|extends|" in result["mermaid"]


def test_ts_class_implements_is_an_implements_edge():
    if not _tree_sitter_reachable():
        print("SKIP test_ts_class_implements_is_an_implements_edge: "
              "no tree-sitter grammar reachable in this environment")
        return
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, "port.ts", "export interface Port {\n  run(): void;\n}\n")
        _write(repo, "adapter.ts", "export class Adapter {\n  run() {}\n}\n")
        base = _commit(repo, "base")
        _write(
            repo, "adapter.ts",
            "import { Port } from './port';\n\n"
            "export class Adapter implements Port {\n  run() {}\n}\n",
        )
        head = _commit(repo, "head")

        result = _run(repo, base, head, ["adapter.ts"])

        added = {(tuple(e["from"]), tuple(e["to"]), e["kind"]) for e in result["added"]}
        assert (("adapter.ts", "Adapter"), ("port.ts", "Port"), "implements") in added
        assert "|implements|" in result["mermaid"]


def test_uses_resolves_through_the_import_not_a_same_named_decoy():
    """Mirrors a real false edge: two files each define their own same-named symbol, and a
    caller that only imports one of them must not also get an edge to the other."""
    if not _tree_sitter_reachable():
        print("SKIP test_uses_resolves_through_the_import_not_a_same_named_decoy: "
              "no tree-sitter grammar reachable in this environment")
        return
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, "right.ts", "export function helper() {\n  return 1;\n}\n")
        _write(repo, "wrong.ts", "export function helper() {\n  return 2;\n}\n")
        _write(
            repo, "caller.ts",
            "import { helper } from './right';\n\nfunction go() {\n  return 0;\n}\n",
        )
        base = _commit(repo, "base")
        _write(
            repo, "caller.ts",
            "import { helper } from './right';\n\nfunction go() {\n  return helper();\n}\n",
        )
        head = _commit(repo, "head")

        result = _run(repo, base, head, ["caller.ts", "wrong.ts"])

        added = {(tuple(e["from"]), tuple(e["to"]), e["kind"]) for e in result["added"]}
        assert (("caller.ts", "go"), ("right.ts", "helper"), "uses") in added
        assert (("caller.ts", "go"), ("wrong.ts", "helper"), "uses") not in added


def test_import_to_an_unparsed_file_drops_the_edge_rather_than_guessing():
    """neighbor.ts is a coupling neighbour of the changed caller.ts (one hop) and gets
    parsed, but neighbor.ts's own import of helper from right.ts is a second hop --
    modules_for_ref never fetches it, so right.ts is never parsed. The reference must drop
    rather than fall through to the same-named decoy in wrong.ts, which *is* parsed because
    it is passed as a changed file."""
    if not _tree_sitter_reachable():
        print("SKIP test_import_to_an_unparsed_file_drops_the_edge_rather_than_guessing: "
              "no tree-sitter grammar reachable in this environment")
        return
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, "right.ts", "export function helper() {\n  return 1;\n}\n")
        _write(repo, "wrong.ts", "export function helper() {\n  return 2;\n}\n")
        _write(
            repo, "neighbor.ts",
            "import { helper } from './right';\n\nexport function useIt() {\n  return 0;\n}\n",
        )
        _write(
            repo, "caller.ts",
            "import { useIt } from './neighbor';\n\nexport function go() {\n  return useIt();\n}\n",
        )
        base = _commit(repo, "base")
        _write(
            repo, "neighbor.ts",
            "import { helper } from './right';\n\n"
            "export function useIt() {\n  return helper();\n}\n",
        )
        head = _commit(repo, "head")

        result = _run(repo, base, head, ["caller.ts", "wrong.ts"])

        added = {(tuple(e["from"]), tuple(e["to"]), e["kind"]) for e in result["added"]}
        assert (("neighbor.ts", "useIt"), ("right.ts", "helper"), "uses") not in added
        assert (("neighbor.ts", "useIt"), ("wrong.ts", "helper"), "uses") not in added


def test_import_target_parsed_but_missing_the_symbol_drops_the_edge():
    """right.ts is parsed (imported directly by caller.ts, so it's in the neighbourhood) but
    never declares `helper` -- a stale/renamed import. Must still drop, not fall through."""
    if not _tree_sitter_reachable():
        print("SKIP test_import_target_parsed_but_missing_the_symbol_drops_the_edge: "
              "no tree-sitter grammar reachable in this environment")
        return
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, "right.ts", "export const nothing = 1;\n")
        _write(repo, "wrong.ts", "export function helper() {\n  return 2;\n}\n")
        _write(
            repo, "caller.ts",
            "import { nothing } from './right';\n\nfunction go() {\n  return 0;\n}\n",
        )
        base = _commit(repo, "base")
        _write(
            repo, "caller.ts",
            "import { helper } from './right';\n\nfunction go() {\n  return helper();\n}\n",
        )
        head = _commit(repo, "head")

        result = _run(repo, base, head, ["caller.ts", "wrong.ts"])

        added = {(tuple(e["from"]), tuple(e["to"]), e["kind"]) for e in result["added"]}
        assert (("caller.ts", "go"), ("right.ts", "helper"), "uses") not in added
        assert (("caller.ts", "go"), ("wrong.ts", "helper"), "uses") not in added


def test_heritage_target_one_hop_past_the_diff_is_marked_context():
    """adapter.ts is a coupling neighbour of the changed file, not the diff itself; port.ts is
    a second hop past that, reachable only through adapter.ts's own implements clause."""
    if not _tree_sitter_reachable():
        print("SKIP test_heritage_target_one_hop_past_the_diff_is_marked_context: "
              "no tree-sitter grammar reachable in this environment")
        return
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, "port.ts", "export interface Port {\n  run(): void;\n}\n")
        _write(repo, "adapter.ts", "export class Adapter {\n  run() {}\n}\n")
        _write(
            repo, "caller.ts",
            "import { Adapter } from './adapter';\n\n"
            "export function go() {\n  return new Adapter();\n}\n",
        )
        base = _commit(repo, "base")
        _write(
            repo, "adapter.ts",
            "import { Port } from './port';\n\n"
            "export class Adapter implements Port {\n  run() {}\n}\n",
        )
        _write(
            repo, "caller.ts",
            "import { Adapter } from './adapter';\n\n"
            "export function go() {\n  return new Adapter();\n}\n",
        )
        head = _commit(repo, "head")

        result = _run(repo, base, head, ["caller.ts"])

        added = {(tuple(e["from"]), tuple(e["to"]), e["kind"]) for e in result["added"]}
        assert (("adapter.ts", "Adapter"), ("port.ts", "Port"), "implements") in added
        assert ["port.ts", "Port"] not in result["structures_added"]
        assert ["port.ts", "Port"] not in result["structures_removed"]
        assert "Port() (context)" in result["mermaid"]  # members==[] gives it call parens


def test_heritage_fanout_over_the_cap_leaves_a_note():
    """Two distinct implements targets (portA.ts, portB.ts), neither parsed yet, one hop past
    the changed file. Capping MAX_HERITAGE_REFS at 1 forces only one to be fetched, so the run
    must say some context nodes were left out rather than silently drawing fewer."""
    if not _tree_sitter_reachable():
        print("SKIP test_heritage_fanout_over_the_cap_leaves_a_note: "
              "no tree-sitter grammar reachable in this environment")
        return
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, "portA.ts", "export interface PortA {\n  run(): void;\n}\n")
        _write(repo, "portB.ts", "export interface PortB {\n  run(): void;\n}\n")
        _write(repo, "adapter.ts", "export class AdapterA {}\nexport class AdapterB {}\n")
        _write(
            repo, "caller.ts",
            "import { AdapterA } from './adapter';\n\n"
            "export function go() {\n  return new AdapterA();\n}\n",
        )
        base = _commit(repo, "base")
        _write(
            repo, "adapter.ts",
            "import { PortA } from './portA';\nimport { PortB } from './portB';\n\n"
            "export class AdapterA implements PortA {\n  run() {}\n}\n"
            "export class AdapterB implements PortB {\n  run() {}\n}\n",
        )
        head = _commit(repo, "head")

        original_cap = structure_mod.MAX_HERITAGE_REFS
        structure_mod.MAX_HERITAGE_REFS = 1
        try:
            result = structure_mod.analyse(str(repo), base, head, ["caller.ts"])
        finally:
            structure_mod.MAX_HERITAGE_REFS = original_cap

        assert "context nodes were left out" in result["note"]


if __name__ == "__main__":
    tests = [
        test_classes_and_functions_from_three_languages_appear,
        test_generic_files_are_not_marked_unsupported,
        test_python_path_is_unaffected_by_the_generic_path,
        test_no_parser_reachable_falls_back_to_skipped_unsupported,
        test_deeply_nested_expression_is_skipped_not_crashed,
        test_ts_class_extends_is_an_extends_edge,
        test_ts_class_implements_is_an_implements_edge,
        test_uses_resolves_through_the_import_not_a_same_named_decoy,
        test_import_to_an_unparsed_file_drops_the_edge_rather_than_guessing,
        test_import_target_parsed_but_missing_the_symbol_drops_the_edge,
        test_heritage_target_one_hop_past_the_diff_is_marked_context,
        test_heritage_fanout_over_the_cap_leaves_a_note,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
