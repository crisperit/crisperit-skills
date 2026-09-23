#!/usr/bin/env python3
"""Self-check for structure.py. End-to-end tests build a throwaway git repo under
tempfile.TemporaryDirectory() and drive the CLI directly, matching test_symdelta.py's style;
a couple of pure-logic tests exercise apply_cap and _member_states directly, no git needed.
The Go case is skipped when `go` isn't on PATH."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import structure  # noqa: E402

SCRIPT_PATH = Path(__file__).parent / "structure.py"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Structure Test",
    "GIT_AUTHOR_EMAIL": "structure-test@example.com",
    "GIT_COMMITTER_NAME": "Structure Test",
    "GIT_COMMITTER_EMAIL": "structure-test@example.com",
}


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        env={**os.environ, **GIT_ENV},
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


def _write(repo, rel_path, content):
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _init_repo(repo):
    _git(repo, "init", "-q", "-b", "main")


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _write_json(repo, name, payload):
    path = repo / name
    path.write_text(json.dumps(payload))
    return path


def _run_structure(repo, base, head, symdelta_path=None, analysis_path=None, env=None,
                    paths=None):
    out_path = repo / "structure.json"
    if symdelta_path is None:
        symdelta_path = _write_json(repo, "symdelta.json", {"nodes": [], "edges": []})
    cmd = [
        sys.executable, str(SCRIPT_PATH), "--repo", str(repo), "--base", base, "--head", head,
        "--symdelta", str(symdelta_path), "--out", str(out_path),
    ]
    if analysis_path:
        cmd += ["--analysis", str(analysis_path)]
    if paths:
        cmd += ["--paths", *paths]
    result = subprocess.run(
        cmd, capture_output=True, text=True, env={**os.environ, **(env or {})}
    )
    assert result.returncode == 0, result.stderr
    return json.loads(out_path.read_text())


def _orphan_baseline(repo):
    """A commit with no parent, pointing at git's well-known empty tree -- the "explain an
    existing feature" mode's baseline when there is no real base commit. Shares no history with
    any ref in the repo, so `git merge-base` fails against it."""
    result = subprocess.run(
        ["git", "-C", str(repo), "commit-tree",
         "4b825dc642cb6eb9a060e54bf8d69288fbee4904", "-m", "empty baseline"],
        input="", capture_output=True, text=True, env={**os.environ, **GIT_ENV},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _component(result, name):
    matches = [c for c in result["components"] if c["name"] == name]
    assert len(matches) == 1, f"expected exactly one component named {name!r}, got {matches}"
    return matches[0]


def _edge_symdelta(src_file, src_name, dst_file, dst_name):
    """A minimal symdelta payload with one call edge src_name -> dst_name. Most fixtures below
    predate the also_touched split (structure.drop test_components/split_also_touched) and test
    symbol-diff state, not which components draw a box, so this exists purely to give a tested
    component a call edge -- keeping it "boxed" and its state inspectable via _component --
    rather than moved to also_touched, which carries no state at all."""
    return {
        "nodes": [
            {"id": "n:src", "label": src_name, "kind": "symbol", "file": src_file},
            {"id": "n:dst", "label": dst_name, "kind": "symbol", "file": dst_file},
        ],
        "edges": [{"id": "e0", "source": "n:src", "target": "n:dst"}],
    }


# ---- new / changed / removed / unchanged, TypeScript ---------------------------------------


def test_new_top_level_function_is_new():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.ts", "export function existing() { return 1; }\n")
        base = _commit(repo, "base")
        _write(
            repo, "a.ts",
            "export function existing() { return 1; }\n"
            "export function fresh() { return 2; }\n",
        )
        head = _commit(repo, "head")

        # existing->fresh keeps both boxed (state-inspectable) instead of also_touched -- see
        # _edge_symdelta.
        symdelta_path = _write_json(
            repo, "symdelta.json", _edge_symdelta("a.ts", "existing", "a.ts", "fresh")
        )
        result = _run_structure(repo, base, head, symdelta_path=symdelta_path)
        assert _component(result, "fresh")["state"] == "new"
        assert _component(result, "existing")["state"] == "unchanged"


def test_modified_function_body_is_changed():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(
            repo, "a.ts",
            "export function greet() { return 'hi'; }\n"
            "export function caller() { return greet(); }\n",
        )
        base = _commit(repo, "base")
        _write(
            repo, "a.ts",
            "export function greet() { return 'hello'; }\n"
            "export function caller() { return greet(); }\n",
        )
        head = _commit(repo, "head")

        symdelta_path = _write_json(
            repo, "symdelta.json", _edge_symdelta("a.ts", "caller", "a.ts", "greet")
        )
        result = _run_structure(repo, base, head, symdelta_path=symdelta_path)
        assert _component(result, "greet")["state"] == "changed"


def test_whitespace_only_change_is_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(
            repo, "a.ts",
            "export function greet() { return 1; }\n"
            "export function caller() { return greet(); }\n",
        )
        base = _commit(repo, "base")
        _write(
            repo, "a.ts",
            "export function greet() {\n  return 1;\n}\n"
            "export function caller() { return greet(); }\n",
        )
        head = _commit(repo, "head")

        symdelta_path = _write_json(
            repo, "symdelta.json", _edge_symdelta("a.ts", "caller", "a.ts", "greet")
        )
        result = _run_structure(repo, base, head, symdelta_path=symdelta_path)
        assert _component(result, "greet")["state"] == "unchanged"


def test_removed_function_is_removed():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(
            repo, "a.ts",
            "export function gone() { return 1; }\n"
            "export function caller() { return gone(); }\n",
        )
        base = _commit(repo, "base")
        _write(repo, "a.ts", "export function caller() { return 1; }\n")
        head = _commit(repo, "head")

        symdelta_path = _write_json(
            repo, "symdelta.json", _edge_symdelta("a.ts", "caller", "a.ts", "gone")
        )
        result = _run_structure(repo, base, head, symdelta_path=symdelta_path)
        comp = _component(result, "gone")
        assert comp["state"] == "removed"
        assert comp["file"] == "a.ts"


# ---- matched-entry order: deterministic, not PYTHONHASHSEED-dependent ----------------------


def test_build_components_orders_matched_entries_deterministically():
    # Both files are unchanged, so both land in the `matched` set build_components must order
    # deterministically (sorted, not raw set order) or a downstream collision resolution
    # (_implements_edges_by_id) would pick a different winner run to run.
    comp = {"kind": "function", "hash": "h", "members": {}, "extends": [], "implements": []}
    base_modules = {"b.ts": {"Beta": comp}, "a.ts": {"Alpha": comp}}
    head_modules = {"b.ts": {"Beta": comp}, "a.ts": {"Alpha": comp}}
    components, _ = structure.build_components(base_modules, head_modules)
    assert list(components) == sorted(components)


# ---- moved: same name, different file, no git rename detected (the WorkflowsPort case) -----


def test_interface_moved_into_a_differently_named_file_is_moved_not_new_and_removed():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "src/old.port.ts", "export interface WorkflowsPort {\n  list(): void;\n}\n")
        base = _commit(repo, "base")
        _git(repo, "rm", "-q", "src/old.port.ts")
        _write(
            repo, "src/new.port.ts",
            "export interface WorkflowsPort {\n"
            "  list(): void;\n"
            "  payloadSchemaFor(name: string): void;\n"
            "}\n"
            "export interface OrchestratorPort {\n  run(): void;\n}\n",
        )
        head = _commit(repo, "head")

        # Content differs enough that git's own rename detection won't pair the two files --
        # exactly the situation build_components has to resolve by symbol name instead.
        status = _git(repo, "diff", "--name-status", "-M", f"{base}...{head}")
        assert not status.splitlines()[0].startswith("R")

        symdelta_path = _write_json(
            repo, "symdelta.json",
            _edge_symdelta("src/new.port.ts", "WorkflowsPort", "src/new.port.ts", "OrchestratorPort"),
        )
        result = _run_structure(repo, base, head, symdelta_path=symdelta_path)
        comp = _component(result, "WorkflowsPort")
        assert comp["state"] == "moved"
        assert comp["file"] == "src/new.port.ts"
        member_states = {m["name"]: m["state"] for m in comp["members"]}
        assert member_states == {"list": "unchanged", "payloadSchemaFor": "new"}
        # The new file's second interface is genuinely new, not a second "moved" guess
        # consuming the already-matched base-side name.
        assert _component(result, "OrchestratorPort")["state"] == "new"


def test_coincidentally_same_named_function_in_an_unrelated_file_is_removed_and_new():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(
            repo, "old.ts",
            "export function anchor() { return Validate(); }\n"
            "export function Validate() { return 1; }\n",
        )
        _write(repo, "brandnew.ts", "export function unrelatedPlaceholder() { return 0; }\n")
        base = _commit(repo, "base")
        _git(repo, "rm", "-q", "old.ts")
        _write(
            repo, "brandnew.ts",
            "export function unrelatedPlaceholder() { return 0; }\n"
            "export function Validate() { return 999; }\n",
        )
        head = _commit(repo, "head")

        # A call edge on each side keeps both Validates boxed rather than also_touched.
        symdelta_path = _write_json(repo, "symdelta.json", {
            "nodes": [
                {"id": "n:anchor", "label": "anchor", "kind": "symbol", "file": "old.ts"},
                {"id": "n:v-old", "label": "Validate", "kind": "symbol", "file": "old.ts"},
                {"id": "n:up", "label": "unrelatedPlaceholder", "kind": "symbol", "file": "brandnew.ts"},
                {"id": "n:v-new", "label": "Validate", "kind": "symbol", "file": "brandnew.ts"},
            ],
            "edges": [
                {"id": "e0", "source": "n:anchor", "target": "n:v-old"},
                {"id": "e1", "source": "n:up", "target": "n:v-new"},
            ],
        })
        result = _run_structure(repo, base, head, symdelta_path=symdelta_path)
        removed = [c for c in result["components"] if c["name"] == "Validate" and c["file"] == "old.ts"]
        new = [c for c in result["components"] if c["name"] == "Validate" and c["file"] == "brandnew.ts"]
        assert len(removed) == 1 and removed[0]["state"] == "removed"
        assert len(new) == 1 and new[0]["state"] == "new"
        assert not any(c["name"] == "Validate" and c["state"] == "moved" for c in result["components"])


# ---- a symbol that only gains a new caller must never read as new (the defineTool case) ----


def test_function_unchanged_but_gains_a_caller_is_not_new():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        # shared.ts must itself be part of the diff (structure.py only parses changed files,
        # per spec) but defineTool's own body must not change -- an unrelated addition next
        # to it is what a real "gains a caller elsewhere" diff usually also touches.
        _write(
            repo, "shared.ts",
            "export function defineTool(x) { return x; }\n"
            "export function unrelated() { return 0; }\n",
        )
        _write(repo, "caller.ts", "export function existingCaller() { return 1; }\n")
        base = _commit(repo, "base")
        _write(
            repo, "shared.ts",
            "export function defineTool(x) { return x; }\n"
            "export function unrelated() { return 1; }\n",
        )
        _write(
            repo, "caller.ts",
            "import { defineTool } from './shared';\n"
            "export function existingCaller() { return 1; }\n"
            "export function newCaller() { return defineTool(1); }\n",
        )
        head = _commit(repo, "head")

        symdelta_payload = {
            "nodes": [
                {"id": "caller:newCaller", "label": "newCaller", "kind": "symbol",
                 "file": "caller.ts", "state": "new"},
                {"id": "shared:defineTool", "label": "defineTool", "kind": "symbol",
                 "file": "shared.ts", "state": "new"},
            ],
            "edges": [
                {"id": "e0", "source": "caller:newCaller", "target": "shared:defineTool",
                 "state": "new"},
            ],
        }
        symdelta_path = _write_json(repo, "symdelta.json", symdelta_payload)
        result = _run_structure(repo, base, head, symdelta_path=symdelta_path)

        define_tool = _component(result, "defineTool")
        assert define_tool["state"] == "unchanged"
        edges = [e for e in result["edges"] if e["to"] == define_tool["id"]]
        assert edges and edges[0]["from"] == _component(result, "newCaller")["id"]


def test_function_body_changed_and_gains_a_caller_is_changed_not_new():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(
            repo, "shared.ts",
            "export function defineTool(x) { return x; }\n"
            "export function other() { return defineTool(0); }\n",
        )
        base = _commit(repo, "base")
        _write(
            repo, "shared.ts",
            "export function defineTool(x) { return x * 2; }\n"
            "export function other() { return defineTool(0); }\n",
        )
        head = _commit(repo, "head")

        symdelta_path = _write_json(
            repo, "symdelta.json", _edge_symdelta("shared.ts", "other", "shared.ts", "defineTool")
        )
        result = _run_structure(repo, base, head, symdelta_path=symdelta_path)
        assert _component(result, "defineTool")["state"] == "changed"


# ---- local closures never surface as members or components ---------------------------------


def test_local_closure_is_never_a_member_or_component():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(
            repo, "a.ts",
            "export function helper() { return outer(); }\n"
            "export function outer() {\n  const inner = () => 1;\n  return inner();\n}\n",
        )
        base = _commit(repo, "base")
        _write(
            repo, "a.ts",
            "export function helper() { return outer(); }\n"
            "export function outer() {\n"
            "  const inner = () => 1;\n"
            "  function nested() { return 2; }\n"
            "  return inner() + nested();\n"
            "}\n",
        )
        head = _commit(repo, "head")

        symdelta_path = _write_json(
            repo, "symdelta.json", _edge_symdelta("a.ts", "helper", "a.ts", "outer")
        )
        result = _run_structure(repo, base, head, symdelta_path=symdelta_path)
        names = {c["name"] for c in result["components"]}
        assert "inner" not in names and "nested" not in names
        assert _component(result, "outer")["members"] == []


# ---- heritage --------------------------------------------------------------------------------


def test_implements_is_recorded_from_ts_class_heritage():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.ts", "export interface Base {\n  a(): void;\n}\n")
        base = _commit(repo, "base")
        _write(
            repo, "a.ts",
            "export interface Base {\n  a(): void;\n}\n"
            "export class Impl implements Base {\n  a(){}\n}\n",
        )
        head = _commit(repo, "head")

        result = _run_structure(repo, base, head)
        impl = _component(result, "Impl")
        entries = [i for i in result["implements"] if i["from"] == impl["id"]]
        assert entries == [{"from": impl["id"], "to": "Base", "kind": "implements"}]


# ---- grouping via analysis.json --------------------------------------------------------------


def test_group_index_matches_analysis_groups():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.ts", "export function a1() { return 1; }\n")
        _write(repo, "b.ts", "export function b1() { return 1; }\n")
        base = _commit(repo, "base")
        _write(repo, "a.ts", "export function a1() { return 2; }\n")
        _write(repo, "b.ts", "export function b1() { return 2; }\n")
        head = _commit(repo, "head")

        analysis_path = _write_json(
            repo, "analysis.json", {"groups": [{"paths": ["a.ts"]}, {"paths": ["b.ts"]}]}
        )

        symdelta_path = _write_json(
            repo, "symdelta.json", _edge_symdelta("a.ts", "a1", "b.ts", "b1")
        )
        result = _run_structure(
            repo, base, head, symdelta_path=symdelta_path, analysis_path=analysis_path
        )
        assert _component(result, "a1")["group"] == 0
        assert _component(result, "b1")["group"] == 1


def test_group_is_null_without_an_analysis_file():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(
            repo, "a.ts",
            "export function helper() { return a1(); }\n"
            "export function a1() { return 1; }\n",
        )
        base = _commit(repo, "base")
        _write(
            repo, "a.ts",
            "export function helper() { return a1(); }\n"
            "export function a1() { return 2; }\n",
        )
        head = _commit(repo, "head")

        symdelta_path = _write_json(
            repo, "symdelta.json", _edge_symdelta("a.ts", "helper", "a.ts", "a1")
        )
        result = _run_structure(repo, base, head, symdelta_path=symdelta_path)
        assert _component(result, "a1")["group"] is None


def test_group_titles_pure_logic():
    groups = [{"title": "Auth"}, {"title": "  "}, {}, {"title": "Billing"}]
    assert structure.group_titles(groups) == [
        {"index": 0, "title": "Auth"}, {"index": 3, "title": "Billing"},
    ]
    assert structure.group_titles([]) == []
    assert structure.group_titles(None) == []


def test_group_titles_surface_at_the_top_level_when_present():
    # sections.py labels its filter chips from this -- see render_structure's own chip test.
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.ts", "export function a1() { return 1; }\n")
        base = _commit(repo, "base")
        _write(repo, "a.ts", "export function a1() { return 2; }\n")
        head = _commit(repo, "head")

        analysis_path = _write_json(
            repo, "analysis.json", {"groups": [{"title": "Auth", "paths": ["a.ts"]}]}
        )

        result = _run_structure(repo, base, head, analysis_path=analysis_path)
        assert result["groups"] == [{"index": 0, "title": "Auth"}]


def test_no_groups_key_when_no_group_has_a_title():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.ts", "export function a1() { return 1; }\n")
        base = _commit(repo, "base")
        _write(repo, "a.ts", "export function a1() { return 2; }\n")
        head = _commit(repo, "head")

        analysis_path = _write_json(repo, "analysis.json", {"groups": [{"paths": ["a.ts"]}]})

        result = _run_structure(repo, base, head, analysis_path=analysis_path)
        assert "groups" not in result


# ---- unsupported language -> language null, same pattern as symdelta ------------------------


def test_unsupported_language_returns_null_language():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "README.md", "# hello\n")
        base = _commit(repo, "base")
        _write(repo, "README.md", "# hello world\n")
        head = _commit(repo, "head")

        result = _run_structure(repo, base, head)
        assert result["language"] is None
        assert result["components"] == []
        assert "reason" in result


def test_missing_tree_sitter_falls_back_to_null_language():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.ts", "export function a1() { return 1; }\n")
        base = _commit(repo, "base")
        _write(repo, "a.ts", "export function a1() { return 2; }\n")
        head = _commit(repo, "head")

        result = _run_structure(repo, base, head, env={"STRUCTURE_NO_TREE_SITTER": "1"})
        assert result["language"] is None
        assert "reason" in result


# ---- size cap: pure logic, no git needed -----------------------------------------------------


def test_size_cap_keeps_top_degree_components_and_reports_dropped():
    components = {}
    for i in range(40):
        cid = f"file{i}.ts:Comp{i}"
        components[cid] = {
            "id": cid, "name": f"Comp{i}", "kind": "function", "file": f"file{i}.ts",
            "state": "unchanged", "members": [], "group": None,
        }
    # The first 5 components get real call-edge degree, so the cap must keep them.
    hot_ids = [f"file{i}.ts:Comp{i}" for i in range(5)]
    edges = []
    for a, b in zip(hot_ids, hot_ids[1:]):
        edges.append({"from": a, "to": b})
        edges.append({"from": b, "to": a})

    kept, kept_edges, dropped = structure.apply_cap(components, edges, cap=30)

    assert len(kept) == 30
    assert dropped == 10
    kept_ids = {c["id"] for c in kept}
    assert set(hot_ids).issubset(kept_ids)
    assert all(e["from"] in kept_ids and e["to"] in kept_ids for e in kept_edges)


def test_size_cap_is_a_noop_under_the_cap():
    components = {"a:X": {"id": "a:X", "group": None}}
    kept, kept_edges, dropped = structure.apply_cap(components, [], cap=30)
    assert kept == [components["a:X"]]
    assert dropped == 0


# ---- _member_states: pure logic --------------------------------------------------------------


def test_member_states_pure_logic():
    states = structure._member_states(
        {"a": "h1", "b": "h2"}, {"a": "h1", "b": "h2x", "c": "h3"}
    )
    lookup = {s["name"]: s["state"] for s in states}
    assert lookup == {"a": "unchanged", "b": "changed", "c": "new"}
    assert structure._member_states({"a": "h1"}, {}) == [{"name": "a", "state": "removed"}]


# ---- Go, via the tiny stdlib-only helper program ----------------------------------------------


def test_go_struct_and_interface_states():
    if shutil.which("go") is None:
        print("skip (no `go` on PATH): test_go_struct_and_interface_states")
        return
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "go.mod", "module example.com/structuretest\n\ngo 1.21\n")
        _write(
            repo, "pkg/a.go",
            "package pkg\n\n"
            "type Greeter struct{}\n\n"
            'func (g *Greeter) Greet() string { return "hi" }\n\n'
            "func TopLevel() int { return 1 }\n",
        )
        base = _commit(repo, "base")
        _write(
            repo, "pkg/a.go",
            "package pkg\n\n"
            "type Greeter struct{}\n\n"
            'func (g *Greeter) Greet() string { return "hello" }\n\n'
            "func TopLevel() int { return 1 }\n\n"
            "func NewFunc() int { return 2 }\n",
        )
        head = _commit(repo, "head")

        # NewFunc -> TopLevel -> Greeter keeps all three boxed rather than also_touched.
        symdelta_path = _write_json(repo, "symdelta.json", {
            "nodes": [
                {"id": "n:new", "label": "NewFunc", "kind": "symbol", "file": "pkg/a.go"},
                {"id": "n:top", "label": "TopLevel", "kind": "symbol", "file": "pkg/a.go"},
                {"id": "n:greeter", "label": "Greeter", "kind": "symbol", "file": "pkg/a.go"},
            ],
            "edges": [
                {"id": "e0", "source": "n:new", "target": "n:top"},
                {"id": "e1", "source": "n:top", "target": "n:greeter"},
            ],
        })
        result = _run_structure(repo, base, head, symdelta_path=symdelta_path)
        assert result["language"] == "go"
        assert _component(result, "TopLevel")["state"] == "unchanged"
        assert _component(result, "NewFunc")["state"] == "new"
        greeter = _component(result, "Greeter")
        assert greeter["state"] == "changed"
        member_states = {m["name"]: m["state"] for m in greeter["members"]}
        assert member_states == {"Greet": "changed"}


def test_go_generic_receiver_method_is_a_member_and_a_new_one_marks_the_struct_changed():
    if shutil.which("go") is None:
        print(
            "skip (no `go` on PATH): "
            "test_go_generic_receiver_method_is_a_member_and_a_new_one_marks_the_struct_changed"
        )
        return
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "go.mod", "module example.com/structuretest\n\ngo 1.21\n")
        _write(
            repo, "pkg/box.go",
            "package pkg\n\n"
            "type Box[T any] struct {\n\tval T\n}\n\n"
            "func (b *Box[T]) Get() T { return b.val }\n\n"
            "func Use() int {\n\tb := &Box[int]{}\n\treturn b.Get()\n}\n",
        )
        base = _commit(repo, "base")
        _write(
            repo, "pkg/box.go",
            "package pkg\n\n"
            "type Box[T any] struct {\n\tval T\n}\n\n"
            "func (b *Box[T]) Get() T { return b.val }\n\n"
            "func (b *Box[T]) Set(v T) { b.val = v }\n\n"
            "func Use() int {\n\tb := &Box[int]{}\n\treturn b.Get()\n}\n",
        )
        head = _commit(repo, "head")

        symdelta_path = _write_json(
            repo, "symdelta.json", _edge_symdelta("pkg/box.go", "Use", "pkg/box.go", "Box")
        )
        result = _run_structure(repo, base, head, symdelta_path=symdelta_path)
        box = _component(result, "Box")
        # Before the fix, recvTypeName returned "" for a Box[T] receiver and Get() vanished
        # entirely instead of showing up as a member.
        member_states = {m["name"]: m["state"] for m in box["members"]}
        assert member_states == {"Get": "unchanged", "Set": "new"}
        assert box["state"] == "changed"


# ---- test files: dropped before the cap, never a component or an also_touched entry ---------


def test_test_components_are_dropped_before_the_cap():
    components = {}
    for i in range(29):
        cid = f"file{i}.ts:Comp{i}"
        components[cid] = {"id": cid, "name": f"Comp{i}", "file": f"file{i}.ts"}
    components["src/a.test.ts:TestComp"] = {
        "id": "src/a.test.ts:TestComp", "name": "TestComp", "file": "src/a.test.ts",
    }
    edges = [{"from": "src/a.test.ts:TestComp", "to": "file0.ts:Comp0"}]

    kept, kept_edges, kept_implements = structure.drop_test_components(components, edges, [])

    assert "src/a.test.ts:TestComp" not in kept
    assert len(kept) == 29
    assert kept_edges == []


def test_a_test_file_component_never_takes_a_cap_slot_or_an_also_touched_entry():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "src/real.ts", "export function real() { return 1; }\n")
        _write(repo, "src/real.test.ts", "export function testHelper() { return 1; }\n")
        base = _commit(repo, "base")
        _write(repo, "src/real.ts", "export function real() { return 2; }\n")
        _write(repo, "src/real.test.ts", "export function testHelper() { return 2; }\n")
        head = _commit(repo, "head")

        result = _run_structure(repo, base, head)
        names = {c["name"] for c in result["components"]}
        # Neither function calls the other, so "real" (a real component) is expected to land in
        # also_touched, not components -- the point here is that "testHelper" (a test-file
        # component) appears in neither.
        assert "real" in names or "real" in result["also_touched"]
        assert "testHelper" not in names
        assert "testHelper" not in result["also_touched"]


# ---- also_touched: an edgeless component draws no box, only a note --------------------------


def test_split_also_touched_pure_logic():
    a = {"id": "f:A", "name": "A"}
    b = {"id": "f:B", "name": "B"}
    c = {"id": "f:C", "name": "C"}
    boxed, also_touched = structure.split_also_touched(
        [a, b, c], [{"from": "f:A", "to": "f:B"}], []
    )
    assert {x["id"] for x in boxed} == {"f:A", "f:B"}
    assert [x["name"] for x in also_touched] == ["C"]


def test_edgeless_component_moves_to_also_touched_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.ts", "export function a1() { return 1; }\n")
        _write(repo, "b.ts", "export function b1() { return 1; }\n")
        base = _commit(repo, "base")
        _write(repo, "a.ts", "export function a1() { return 2; }\n")
        _write(repo, "b.ts", "export function b1() { return 2; }\n")
        head = _commit(repo, "head")

        # No symdelta edges tying a1 and b1 together -- both stay isolated.
        result = _run_structure(repo, base, head)
        assert {c["name"] for c in result["components"]} == set()
        assert sorted(result["also_touched"]) == ["a1", "b1"]


# ---- row: barycenter ordering within a column, deterministic --------------------------------


def test_barycenter_row_order_reduces_crossings_and_is_deterministic():
    # Column 0: A, B (name order). Column 1: X, Y. A calls Y, B calls X -- name order alone
    # would cross those two lines; barycenter ordering should swap column 1 to Y-then-X so each
    # call runs in a parallel, non-crossing line instead.
    a = {"id": "f:A", "name": "A", "column": 0}
    b = {"id": "f:B", "name": "B", "column": 0}
    x = {"id": "f:X", "name": "X", "column": 1}
    y = {"id": "f:Y", "name": "Y", "column": 1}
    components = [a, b, x, y]
    edges = [{"from": "f:A", "to": "f:Y"}, {"from": "f:B", "to": "f:X"}]

    structure.assign_rows(components, edges, [])
    assert (a["row"], b["row"]) == (0, 1)
    assert (y["row"], x["row"]) == (0, 1)

    # Same input, run again: the same rows come back, not whatever order dict iteration gave.
    structure.assign_rows(components, edges, [])
    assert (a["row"], b["row"], x["row"], y["row"]) == (0, 1, 1, 0)


# ---- explain mode: orphan baseline, --paths scoping ------------------------------------------


def test_orphan_base_with_no_merge_base_works():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.ts", "export function existing() { return 1; }\n")
        _commit(repo, "base")
        _write(
            repo, "a.ts",
            "export function existing() { return 1; }\n"
            "export function fresh() { return 2; }\n",
        )
        head = _commit(repo, "head")
        orphan = _orphan_baseline(repo)

        # Three-dot needs a merge base; the orphan shares no history with head, so this used to
        # die with "fatal: ...: no merge base" before changed_files() switched to two-dot.
        symdelta_path = _write_json(
            repo, "symdelta.json", _edge_symdelta("a.ts", "existing", "a.ts", "fresh")
        )
        result = _run_structure(repo, orphan, head, symdelta_path=symdelta_path)
        assert _component(result, "existing")["state"] == "new"
        assert _component(result, "fresh")["state"] == "new"


def test_paths_scopes_the_component_set():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(
            repo, "src/a/foo.ts",
            "export function helper() { return foo(); }\n"
            "export function foo() { return 1; }\n",
        )
        _write(repo, "src/b/bar.ts", "export function bar() { return 2; }\n")
        head = _commit(repo, "head")
        orphan = _orphan_baseline(repo)

        symdelta_path = _write_json(
            repo, "symdelta.json", _edge_symdelta("src/a/foo.ts", "helper", "src/a/foo.ts", "foo")
        )
        # Against the orphan base every file counts as changed, so without --paths this pulls in
        # the whole repo. Scoped to src/a, src/b's component must not appear.
        result = _run_structure(repo, orphan, head, symdelta_path=symdelta_path, paths=["src/a"])
        assert _component(result, "foo")["state"] == "new"
        assert [c for c in result["components"] if c["name"] == "bar"] == []


def test_bad_ref_fails_with_nonzero_exit():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.ts", "export function a1() { return 1; }\n")
        base = _commit(repo, "base")
        symdelta_path = _write_json(repo, "symdelta.json", {"nodes": [], "edges": []})
        out_path = repo / "structure.json"
        cmd = [
            sys.executable, str(SCRIPT_PATH), "--repo", str(repo), "--base", base,
            "--head", "not-a-real-ref", "--symdelta", str(symdelta_path), "--out", str(out_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        assert result.returncode != 0


if __name__ == "__main__":
    tests = [
        test_new_top_level_function_is_new,
        test_modified_function_body_is_changed,
        test_whitespace_only_change_is_unchanged,
        test_removed_function_is_removed,
        test_build_components_orders_matched_entries_deterministically,
        test_interface_moved_into_a_differently_named_file_is_moved_not_new_and_removed,
        test_coincidentally_same_named_function_in_an_unrelated_file_is_removed_and_new,
        test_function_unchanged_but_gains_a_caller_is_not_new,
        test_function_body_changed_and_gains_a_caller_is_changed_not_new,
        test_local_closure_is_never_a_member_or_component,
        test_implements_is_recorded_from_ts_class_heritage,
        test_group_index_matches_analysis_groups,
        test_group_is_null_without_an_analysis_file,
        test_group_titles_pure_logic,
        test_group_titles_surface_at_the_top_level_when_present,
        test_no_groups_key_when_no_group_has_a_title,
        test_unsupported_language_returns_null_language,
        test_missing_tree_sitter_falls_back_to_null_language,
        test_size_cap_keeps_top_degree_components_and_reports_dropped,
        test_size_cap_is_a_noop_under_the_cap,
        test_member_states_pure_logic,
        test_go_struct_and_interface_states,
        test_go_generic_receiver_method_is_a_member_and_a_new_one_marks_the_struct_changed,
        test_test_components_are_dropped_before_the_cap,
        test_a_test_file_component_never_takes_a_cap_slot_or_an_also_touched_entry,
        test_split_also_touched_pure_logic,
        test_edgeless_component_moves_to_also_touched_end_to_end,
        test_barycenter_row_order_reduces_crossings_and_is_deterministic,
        test_orphan_base_with_no_merge_base_works,
        test_paths_scopes_the_component_set,
        test_bad_ref_fails_with_nonzero_exit,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
