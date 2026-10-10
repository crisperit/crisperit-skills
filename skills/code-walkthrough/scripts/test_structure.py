#!/usr/bin/env python3
"""Self-check for structure.py. End-to-end tests build a throwaway git repo under pytest's
tmp_path and drive the CLI directly; a couple of pure-logic tests exercise apply_cap and
_member_states directly, no git needed. Tests that need a tree-sitter grammar or the Go helper
skip via cw_testlib when the machine lacks it."""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_testlib  # noqa: E402
import structure  # noqa: E402
from cw_testlib import commit_all, git, init_repo, make_repo, orphan_baseline, write_file  # noqa: E402

SCRIPT_PATH = Path(__file__).parent / "structure.py"


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


def _run_ts(tmp_path, base_files, head_files, symdelta=None, analysis=None, **kw):
    """make_repo plus one structure.py run, for TypeScript fixtures; skips without a grammar."""
    cw_testlib.require_tree_sitter("typescript")
    repo, base, head = make_repo(tmp_path, base_files, head_files)
    symdelta_path = _write_json(repo, "symdelta.json", symdelta) if symdelta is not None else None
    analysis_path = _write_json(repo, "analysis.json", analysis) if analysis is not None else None
    return _run_structure(repo, base, head, symdelta_path=symdelta_path,
                          analysis_path=analysis_path, **kw)


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


def test_component_state_per_file_change(tmp_path):
    rows = [
        ("new top-level function",
         {"a.ts": "export function existing() { return 1; }\n"},
         {"a.ts": "export function existing() { return 1; }\n"
                  "export function fresh() { return 2; }\n"},
         ("existing", "fresh"), {"fresh": "new", "existing": "unchanged"}),
        ("modified function body",
         {"a.ts": "export function greet() { return 'hi'; }\n"
                  "export function caller() { return greet(); }\n"},
         {"a.ts": "export function greet() { return 'hello'; }\n"
                  "export function caller() { return greet(); }\n"},
         ("caller", "greet"), {"greet": "changed"}),
        # caller gets a genuine change so it's "touched" -- greet's whitespace-only reformat is
        # the thing under test, and it needs a touched neighbour to stay boxed under the
        # unreferenced-unchanged filter (see drop_unreferenced_unchanged).
        ("whitespace-only change",
         {"a.ts": "export function greet() { return 1; }\n"
                  "export function caller() { return greet(); }\n"},
         {"a.ts": "export function greet() {\n  return 1;\n}\n"
                  "export function caller() { return greet() + 1; }\n"},
         ("caller", "greet"), {"greet": "unchanged"}),
        ("removed function",
         {"a.ts": "export function gone() { return 1; }\n"
                  "export function caller() { return gone(); }\n"},
         {"a.ts": "export function caller() { return 1; }\n"},
         ("caller", "gone"), {"gone": "removed"}),
        ("unchanged callee reached by an edge from a touched caller",
         {"a.ts": "export function caller() { return callee(); }\n"
                  "export function callee() { return 1; }\n"},
         {"a.ts": "export function caller() { return callee() + 1; }\n"
                  "export function callee() { return 1; }\n"},
         ("caller", "callee"), {"callee": "unchanged"}),
    ]
    for i, (label, base_files, head_files, (src, dst), want) in enumerate(rows):
        (tmp_path / str(i)).mkdir()
        result = _run_ts(tmp_path / str(i), base_files, head_files,
                         symdelta=_edge_symdelta("a.ts", src, "a.ts", dst))
        for name, state in want.items():
            assert _component(result, name)["state"] == state, f"{label}: {name}"
        if "removed" in want.values():
            assert _component(result, "gone")["file"] == "a.ts", label
        assert result["dropped"] == 0, label


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


def test_interface_moved_into_a_differently_named_file_is_moved_not_new_and_removed(tmp_path):
    cw_testlib.require_tree_sitter("typescript")
    repo, base, head = make_repo(
        tmp_path,
        {"src/old.port.ts": "export interface WorkflowsPort {\n  list(): void;\n}\n"},
        {
            "src/old.port.ts": None,
            "src/new.port.ts": "export interface WorkflowsPort {\n"
                               "  list(): void;\n"
                               "  payloadSchemaFor(name: string): void;\n"
                               "}\n"
                               "export interface OrchestratorPort {\n  run(): void;\n}\n",
        },
    )

    # Content differs enough that git's own rename detection won't pair the two files --
    # exactly the situation build_components has to resolve by symbol name instead.
    status = git(repo, "diff", "--name-status", "-M", f"{base}...{head}")
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


def test_coincidentally_same_named_function_in_an_unrelated_file_is_removed_and_new(tmp_path):
    # A call edge on each side keeps both Validates boxed rather than also_touched.
    symdelta = {
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
    }
    result = _run_ts(
        tmp_path,
        {
            "old.ts": "export function anchor() { return Validate(); }\n"
                      "export function Validate() { return 1; }\n",
            "brandnew.ts": "export function unrelatedPlaceholder() { return 0; }\n",
        },
        {
            "old.ts": None,
            "brandnew.ts": "export function unrelatedPlaceholder() { return 0; }\n"
                           "export function Validate() { return 999; }\n",
        },
        symdelta=symdelta,
    )
    removed = [c for c in result["components"] if c["name"] == "Validate" and c["file"] == "old.ts"]
    new = [c for c in result["components"] if c["name"] == "Validate" and c["file"] == "brandnew.ts"]
    assert len(removed) == 1 and removed[0]["state"] == "removed"
    assert len(new) == 1 and new[0]["state"] == "new"
    assert not any(c["name"] == "Validate" and c["state"] == "moved" for c in result["components"])


# ---- a symbol that only gains a new caller must never read as new (the defineTool case) ----


def test_function_unchanged_but_gains_a_caller_is_not_new(tmp_path):
    # shared.ts must itself be part of the diff (structure.py only parses changed files,
    # per spec) but defineTool's own body must not change -- an unrelated addition next
    # to it is what a real "gains a caller elsewhere" diff usually also touches.
    symdelta = {
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
    result = _run_ts(
        tmp_path,
        {
            "shared.ts": "export function defineTool(x) { return x; }\n"
                         "export function unrelated() { return 0; }\n",
            "caller.ts": "export function existingCaller() { return 1; }\n",
        },
        {
            "shared.ts": "export function defineTool(x) { return x; }\n"
                         "export function unrelated() { return 1; }\n",
            "caller.ts": "import { defineTool } from './shared';\n"
                         "export function existingCaller() { return 1; }\n"
                         "export function newCaller() { return defineTool(1); }\n",
        },
        symdelta=symdelta,
    )

    define_tool = _component(result, "defineTool")
    assert define_tool["state"] == "unchanged"
    edges = [e for e in result["edges"] if e["to"] == define_tool["id"]]
    assert edges and edges[0]["from"] == _component(result, "newCaller")["id"]


# ---- local closures never surface as members or components ---------------------------------


def test_local_closure_is_never_a_member_or_component(tmp_path):
    result = _run_ts(
        tmp_path,
        {"a.ts": "export function helper() { return outer(); }\n"
                 "export function outer() {\n  const inner = () => 1;\n  return inner();\n}\n"},
        {"a.ts": "export function helper() { return outer(); }\n"
                 "export function outer() {\n"
                 "  const inner = () => 1;\n"
                 "  function nested() { return 2; }\n"
                 "  return inner() + nested();\n"
                 "}\n"},
        symdelta=_edge_symdelta("a.ts", "helper", "a.ts", "outer"),
    )
    names = {c["name"] for c in result["components"]}
    assert "inner" not in names and "nested" not in names
    assert _component(result, "outer")["members"] == []


# ---- heritage --------------------------------------------------------------------------------


def test_implements_is_recorded_from_ts_class_heritage(tmp_path):
    result = _run_ts(
        tmp_path,
        {"a.ts": "export interface Base {\n  a(): void;\n}\n"},
        {"a.ts": "export interface Base {\n  a(): void;\n}\n"
                 "export class Impl implements Base {\n  a(){}\n}\n"},
    )
    impl = _component(result, "Impl")
    entries = [i for i in result["implements"] if i["from"] == impl["id"]]
    assert entries == [{"from": impl["id"], "to": "Base", "kind": "implements"}]


# ---- grouping via analysis.json --------------------------------------------------------------


def test_group_index_matches_analysis_groups(tmp_path):
    result = _run_ts(
        tmp_path,
        {"a.ts": "export function a1() { return 1; }\n",
         "b.ts": "export function b1() { return 1; }\n"},
        {"a.ts": "export function a1() { return 2; }\n",
         "b.ts": "export function b1() { return 2; }\n"},
        symdelta=_edge_symdelta("a.ts", "a1", "b.ts", "b1"),
        analysis={"groups": [{"paths": ["a.ts"]}, {"paths": ["b.ts"]}]},
    )
    assert _component(result, "a1")["group"] == 0
    assert _component(result, "b1")["group"] == 1


def test_group_is_null_without_an_analysis_file(tmp_path):
    result = _run_ts(
        tmp_path,
        {"a.ts": "export function helper() { return a1(); }\n"
                 "export function a1() { return 1; }\n"},
        {"a.ts": "export function helper() { return a1(); }\n"
                 "export function a1() { return 2; }\n"},
        symdelta=_edge_symdelta("a.ts", "helper", "a.ts", "a1"),
    )
    assert _component(result, "a1")["group"] is None


def test_group_titles_pure_logic():
    groups = [{"title": "Auth"}, {"title": "  "}, {}, {"title": "Billing"}]
    assert structure.group_titles(groups) == [
        {"index": 0, "title": "Auth"}, {"index": 3, "title": "Billing"},
    ]
    assert structure.group_titles([]) == []
    assert structure.group_titles(None) == []


def test_group_titles_end_to_end(tmp_path):
    # sections.py labels its filter chips from this -- see render_structure's own chip test.
    cw_testlib.require_tree_sitter("typescript")
    repo, base, head = make_repo(
        tmp_path,
        {"a.ts": "export function a1() { return 1; }\n"},
        {"a.ts": "export function a1() { return 2; }\n"},
    )
    rows = [
        ("title present", {"groups": [{"title": "Auth", "paths": ["a.ts"]}]},
         [{"index": 0, "title": "Auth"}]),
        ("no group has a title", {"groups": [{"paths": ["a.ts"]}]}, None),
    ]
    for label, analysis, want in rows:
        analysis_path = _write_json(repo, "analysis.json", analysis)
        result = _run_structure(repo, base, head, analysis_path=analysis_path)
        if want is None:
            assert "groups" not in result, label
        else:
            assert result["groups"] == want, label


# ---- unsupported language -> language null, same pattern as symdelta ------------------------


def test_unsupported_language_returns_null_language(tmp_path):
    repo, base, head = make_repo(tmp_path, {"README.md": "# hello\n"}, {"README.md": "# hello world\n"})

    result = _run_structure(repo, base, head)

    assert result["language"] is None
    assert result["components"] == []
    assert "reason" in result


def test_missing_tree_sitter_falls_back_to_null_language(tmp_path):
    repo, base, head = make_repo(
        tmp_path,
        {"a.ts": "export function a1() { return 1; }\n"},
        {"a.ts": "export function a1() { return 2; }\n"},
    )

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


def test_size_cap_never_cuts_a_touched_component_for_an_unchanged_one():
    # touched ties fall to alphabetical id on degree 0, which used to let an unchanged component win
    # a slot a touched one needed. The second row also guards the cross-group round robin: group 0
    # has 20 touched, group 1 only 5 unchanged, and alternating groups without regard to state would
    # hand group 1's unchanged components a slot each round before group 0 ran out.
    rows = [
        ("single group", [(32, "changed", None), (3, "unchanged", None)], 30, 5),
        ("two groups", [(20, "changed", 0), (5, "unchanged", 1)], 10, 15),
    ]
    for label, parts, cap, want_dropped in rows:
        components = {}
        n = 0
        for count, state, group in parts:
            for _ in range(count):
                cid = f"file{n}.ts:Comp{n}"
                components[cid] = {"id": cid, "name": f"Comp{n}", "state": state, "group": group}
                n += 1

        kept, kept_edges, dropped = structure.apply_cap(components, [], cap=cap)

        assert len(kept) == cap, label
        assert dropped == want_dropped, label
        assert all(c["state"] != "unchanged" for c in kept), label


# ---- unreferenced unchanged components: dropped as noise, not as overflow -------------------


def test_unreferenced_unchanged_component_excluded_and_not_counted_in_dropped(tmp_path):
    # No edge touches "untouched" at all -- it's an unrelated symbol build_components still
    # parsed because it shares a changed file with "touched".
    result = _run_ts(
        tmp_path,
        {"a.ts": "export function touched() { return 1; }\n"
                 "export function untouched() { return 2; }\n"},
        {"a.ts": "export function touched() { return 99; }\n"
                 "export function untouched() { return 2; }\n"},
    )
    assert result["components"] == []
    assert result["also_touched"] == ["touched"]
    assert result["dropped"] == 0


# ---- _member_states: pure logic --------------------------------------------------------------


def test_member_states_pure_logic():
    states = structure._member_states(
        {"a": "h1", "b": "h2"}, {"a": "h1", "b": "h2x", "c": "h3"}
    )
    lookup = {s["name"]: s["state"] for s in states}
    assert lookup == {"a": "unchanged", "b": "changed", "c": "new"}
    assert structure._member_states({"a": "h1"}, {}) == [{"name": "a", "state": "removed"}]


# ---- Go, via the tiny stdlib-only helper program ----------------------------------------------


def test_go_struct_and_interface_states(tmp_path):
    cw_testlib.require_tree_sitter("go")
    repo, base, head = make_repo(
        tmp_path,
        {
            "go.mod": "module example.com/structuretest\n\ngo 1.21\n",
            "pkg/a.go": "package pkg\n\n"
                        "type Greeter struct{}\n\n"
                        'func (g *Greeter) Greet() string { return "hi" }\n\n'
                        "func TopLevel() int { return 1 }\n",
        },
        {
            "pkg/a.go": "package pkg\n\n"
                        "type Greeter struct{}\n\n"
                        'func (g *Greeter) Greet() string { return "hello" }\n\n'
                        "func TopLevel() int { return 1 }\n\n"
                        "func NewFunc() int { return 2 }\n",
        },
    )

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


def test_go_generic_receiver_method_is_a_member_and_a_new_one_marks_the_struct_changed(tmp_path):
    cw_testlib.require_tree_sitter("go")
    repo, base, head = make_repo(
        tmp_path,
        {
            "go.mod": "module example.com/structuretest\n\ngo 1.21\n",
            "pkg/box.go": "package pkg\n\n"
                          "type Box[T any] struct {\n\tval T\n}\n\n"
                          "func (b *Box[T]) Get() T { return b.val }\n\n"
                          "func Use() int {\n\tb := &Box[int]{}\n\treturn b.Get()\n}\n",
        },
        {
            "pkg/box.go": "package pkg\n\n"
                          "type Box[T any] struct {\n\tval T\n}\n\n"
                          "func (b *Box[T]) Get() T { return b.val }\n\n"
                          "func (b *Box[T]) Set(v T) { b.val = v }\n\n"
                          "func Use() int {\n\tb := &Box[int]{}\n\treturn b.Get()\n}\n",
        },
    )

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


def test_a_test_file_component_never_takes_a_cap_slot_or_an_also_touched_entry(tmp_path):
    result = _run_ts(
        tmp_path,
        {"src/real.ts": "export function real() { return 1; }\n",
         "src/real.test.ts": "export function testHelper() { return 1; }\n"},
        {"src/real.ts": "export function real() { return 2; }\n",
         "src/real.test.ts": "export function testHelper() { return 2; }\n"},
    )
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


def test_edgeless_component_moves_to_also_touched_end_to_end(tmp_path):
    # No symdelta edges tying a1 and b1 together -- both stay isolated.
    result = _run_ts(
        tmp_path,
        {"a.ts": "export function a1() { return 1; }\n",
         "b.ts": "export function b1() { return 1; }\n"},
        {"a.ts": "export function a1() { return 2; }\n",
         "b.ts": "export function b1() { return 2; }\n"},
    )
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


def test_orphan_base_with_no_merge_base_works(tmp_path):
    cw_testlib.require_tree_sitter("typescript")
    repo, _base, head = make_repo(
        tmp_path,
        {"a.ts": "export function existing() { return 1; }\n"},
        {"a.ts": "export function existing() { return 1; }\n"
                 "export function fresh() { return 2; }\n"},
    )
    orphan = orphan_baseline(repo)

    # Three-dot needs a merge base; the orphan shares no history with head, so this used to
    # die with "fatal: ...: no merge base" before changed_files() switched to two-dot.
    symdelta_path = _write_json(
        repo, "symdelta.json", _edge_symdelta("a.ts", "existing", "a.ts", "fresh")
    )
    result = _run_structure(repo, orphan, head, symdelta_path=symdelta_path)
    assert _component(result, "existing")["state"] == "new"
    assert _component(result, "fresh")["state"] == "new"


def test_paths_scopes_the_component_set(tmp_path):
    cw_testlib.require_tree_sitter("typescript")
    cw_testlib.require_git()
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    write_file(
        repo, "src/a/foo.ts",
        "export function helper() { return foo(); }\n"
        "export function foo() { return 1; }\n",
    )
    write_file(repo, "src/b/bar.ts", "export function bar() { return 2; }\n")
    head = commit_all(repo, "head")
    orphan = orphan_baseline(repo)

    symdelta_path = _write_json(
        repo, "symdelta.json", _edge_symdelta("src/a/foo.ts", "helper", "src/a/foo.ts", "foo")
    )
    # Against the orphan base every file counts as changed, so without --paths this pulls in
    # the whole repo. Scoped to src/a, src/b's component must not appear.
    result = _run_structure(repo, orphan, head, symdelta_path=symdelta_path, paths=["src/a"])
    assert _component(result, "foo")["state"] == "new"
    assert [c for c in result["components"] if c["name"] == "bar"] == []


def test_bad_ref_fails_with_nonzero_exit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cw_testlib.require_git()
    init_repo(repo)
    write_file(repo, "a.ts", "export function a1() { return 1; }\n")
    base = commit_all(repo, "base")
    symdelta_path = _write_json(repo, "symdelta.json", {"nodes": [], "edges": []})
    out_path = repo / "structure.json"
    cmd = [
        sys.executable, str(SCRIPT_PATH), "--repo", str(repo), "--base", base,
        "--head", "not-a-real-ref", "--symdelta", str(symdelta_path), "--out", str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode != 0
