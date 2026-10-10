#!/usr/bin/env python3
"""Self-check for symdelta.py. Pure-logic tests exercise the delta arithmetic directly (no
git/go needed); the rest build a throwaway git repo under pytest's tmp_path. The end-to-end Go
and TypeScript tests skip through cw_testlib when the extractor or language server is missing
or unusable on the machine."""

import contextlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "extractors" / "lsp"))
import cw_testlib  # noqa: E402
import symdelta  # noqa: E402
import lsp_client  # noqa: E402
from cw_testlib import commit_all, git, init_repo, make_repo, orphan_baseline, write_file  # noqa: E402

SCRIPT_PATH = Path(__file__).parent / "symdelta.py"


def _run_symdelta(repo, base, head):
    cmd = [sys.executable, str(SCRIPT_PATH), "--repo", str(repo), "--base", base, "--head", head]
    return subprocess.run(cmd, capture_output=True, text=True)


def _head_only_repo(tmp_path, files):
    """One commit holding `files`; returns (repo, head_sha)."""
    cw_testlib.require_git()
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    for rel_path, content in files.items():
        write_file(repo, rel_path, content)
    return repo, commit_all(repo, "head")


def _raises(exc):
    def fail(*args, **kwargs):
        raise exc
    return fail


def _assert_no_worktree_leak(repo):
    worktrees = git(repo, "worktree", "list").strip().splitlines()
    assert len(worktrees) == 1, f"the extractor's worktrees leaked into the repo: {worktrees}"


# ---- pure-logic tests: rename-map parsing ----------------------------------------------


def test_parse_rename_map():
    rows = [
        ("brace with an empty side collapses the slash",
         " rename corelib/ratelimit/{token_bucket => }/token_bucket.go (87%)\n",
         {"corelib/ratelimit/token_bucket.go": "corelib/ratelimit/token_bucket/token_bucket.go"}),
        ("brace with a non-empty side",
         " rename corelib/ratelimit/{internal => ratelimit_config}/config.go (99%)\n",
         {"corelib/ratelimit/ratelimit_config/config.go": "corelib/ratelimit/internal/config.go"}),
        ("plain rename with no common prefix",
         " rename a/foo.go => b/bar.go (65%)\n",
         {"b/bar.go": "a/foo.go"}),
        ("non-rename summary lines are ignored",
         " a/foo.go | 3 +--\n 1 file changed, 1 insertion(+), 2 deletions(-)\n",
         {}),
    ]
    for label, summary, want in rows:
        assert symdelta.parse_rename_map(summary) == want, label


# ---- pure-logic tests: rename pairing + move collapse ----------------------------------


def _edge(from_file, from_sym, to_file, to_sym):
    return {"FromFile": from_file, "FromSym": from_sym, "ToFile": to_file, "ToSym": to_sym}


def test_compute_new_gone():
    rows = [
        # Same call, file only renamed (no directory change): must not show as new+gone.
        ("renamed head path is canonicalised",
         [_edge("a/x.go", "Caller", "a/y.go", "Callee")],
         [_edge("a/x2.go", "Caller", "a/y.go", "Callee")],
         {"a/x2.go": "a/x.go"},
         [], []),
        ("real change is kept",
         [_edge("a/x.go", "Caller", "a/y.go", "Callee")],
         [_edge("a/x.go", "Caller", "a/z.go", "NewCallee")],
         {},
         [["a/x.go", "Caller", "a/z.go", "NewCallee"]],
         [["a/x.go", "Caller", "a/y.go", "Callee"]]),
    ]
    for label, base, head, rename_map, want_new, want_gone in rows:
        new, gone = symdelta.compute_new_gone(base, head, rename_map)
        assert new == want_new and gone == want_gone, label


def test_compute_new_gone_carries_head_path_for_new_symbol_in_renamed_file():
    # A symbol carried over unchanged through a rename must not surface as new/gone at all; a
    # genuinely new symbol added in the same renamed file must be identified by its HEAD package,
    # not the pre-rename one -- the defect this guards emitted `pkg/internal:NewFunc` even though
    # `pkg/internal` no longer exists at head.
    base = [_edge("pkg/internal/foo.go", "Caller", "pkg/internal/foo.go", "Callee")]
    head = [
        _edge("pkg/foo.go", "Caller", "pkg/foo.go", "Callee"),
        _edge("pkg/foo.go", "NewFunc", "pkg/foo.go", "Callee"),
    ]
    rename_map = {"pkg/foo.go": "pkg/internal/foo.go"}

    new, gone = symdelta.compute_new_gone(base, head, rename_map)
    assert gone == []
    assert new == [["pkg/foo.go", "NewFunc", "pkg/foo.go", "Callee"]]

    nodes, _, _, _ = symdelta.build_graph(new, gone)
    symbol_ids = {n["id"] for n in nodes if n["kind"] == "symbol"}
    assert {"pkg:NewFunc", "pkg:Callee"} <= symbol_ids
    assert "pkg/internal:NewFunc" not in symbol_ids


def test_move_collapse_tallies_only_the_caller_side_move():
    # Caller "Helper" moved from pkg/internal to pkg (same callee, same call) -> collapsed and
    # tallied. A second pair only differs on the callee's package -> collapsed but NOT tallied,
    # matching the reference implementation (moved[] reports caller moves only).
    new = [
        ["pkg/a.go", "Helper", "pkg/b.go", "Target"],
        ["pkg/c.go", "Other", "pkg/d.go", "Moved"],
    ]
    gone = [
        ["pkg/internal/a.go", "Helper", "pkg/b.go", "Target"],
        ["pkg/c.go", "Other", "pkg/internal/d.go", "Moved"],
    ]
    final_new, final_gone, moved, moved_pairs = symdelta.move_collapse(new, gone)
    assert final_new == [] and final_gone == []
    assert moved == [{"from": "pkg/internal", "to": "pkg", "callsites": 1}]
    assert ("pkg/internal:Helper", "pkg:Helper", "pkg/a.go") in moved_pairs
    assert ("pkg/internal:Moved", "pkg:Moved", "pkg/d.go") in moved_pairs


def test_move_collapse_does_not_cross_match_unrelated_same_name_edges():
    # Two callers named "Config.String" in unrelated packages both call something named "Check"
    # -- a bare-name collision. Only one pair is a real move (b/internal -> b, same callee); the
    # other new/gone entries are two unrelated edges that just happen to share the same
    # (FromSym, ToSym) pair. Matching on bare names alone would cross-pair them, fabricating a
    # bogus move and dropping both real edges -- the unrelated ones must survive untouched.
    new = [
        ["a/config.go", "Config.String", "a/check.go", "Check"],
        ["b/config.go", "Config.String", "b/helper.go", "Check"],
    ]
    gone = [
        ["b/internal/config.go", "Config.String", "b/helper.go", "Check"],
        ["a/other.go", "Config.String", "a/oldcheck.go", "Check"],
    ]
    final_new, final_gone, moved, moved_pairs = symdelta.move_collapse(new, gone)
    assert final_new == [["a/config.go", "Config.String", "a/check.go", "Check"]]
    assert final_gone == [["a/other.go", "Config.String", "a/oldcheck.go", "Check"]]
    assert moved == [{"from": "b/internal", "to": "b", "callsites": 1}]
    assert moved_pairs == [("b/internal:Config.String", "b:Config.String", "b/config.go")]


def _assert_no_dangling_edges(nodes, edges):
    node_ids = {n["id"] for n in nodes}
    for e in edges:
        assert e["source"] in node_ids, f"edge {e['id']} source {e['source']!r} has no node"
        assert e["target"] in node_ids, f"edge {e['id']} target {e['target']!r} has no node"


def test_build_graph_merges_a_symbol_that_moved_package_and_changed_its_calls():
    # Helper moves from pkg/internal to pkg: one call (-> Target) is unchanged by the move and
    # collapses away entirely; a second call (-> NewTarget) is genuinely new, and the old call
    # (-> OldTarget) is genuinely removed. Helper must land as ONE node, at its head package
    # "pkg", state "changed" -- not one `new` node at pkg and one `gone` node at pkg/internal.
    new = [
        ["pkg/a.go", "Helper", "pkg/b.go", "Target"],
        ["pkg/a.go", "Helper", "pkg/c.go", "NewTarget"],
    ]
    gone = [
        ["pkg/internal/a.go", "Helper", "pkg/b.go", "Target"],
        ["pkg/internal/a.go", "Helper", "pkg/d.go", "OldTarget"],
    ]
    final_new, final_gone, moved, moved_pairs = symdelta.move_collapse(new, gone)
    assert moved == [{"from": "pkg/internal", "to": "pkg", "callsites": 1}]

    nodes, edges, _, counts = symdelta.build_graph(final_new, final_gone, {}, moved_pairs)
    _assert_no_dangling_edges(nodes, edges)

    symbol_nodes = {n["id"]: n for n in nodes if n["kind"] == "symbol"}
    assert "pkg/internal:Helper" not in symbol_nodes
    assert symbol_nodes["pkg:Helper"]["state"] == "changed"
    assert symbol_nodes["pkg:Helper"]["parent"] == "pkg"
    assert symbol_nodes["pkg:NewTarget"]["state"] == "new"
    assert symbol_nodes["pkg:OldTarget"]["state"] == "gone"
    assert counts["symbols"] == 3

    sources = {e["source"] for e in edges}
    assert sources == {"pkg:Helper"}  # both edges now point out of the merged head-side id


def test_build_graph_merges_a_root_level_rename_into_a_subdirectory_with_a_changed_symbol():
    # main.go (root-level, sid "(root):main.Run") is renamed into pkg/main.go and Run's call
    # target also changes. sym_id() does NOT encode a ROOT_PKG symbol as a bare name -- it's
    # "main.Run", not "Run" -- so re-deriving the name from the gone id's own string used to
    # build the wrong head-side id ("pkg:main.Run" instead of the real "pkg:Run") and left the
    # move unmerged: one node per side instead of one "changed" node.
    final_new = [["pkg/main.go", "Run", "pkg/b.go", "NewCheck"]]
    final_gone = [["main.go", "Run", "b.go", "OldCheck"]]
    rename_map = {"pkg/main.go": "main.go"}

    nodes, edges, _, _ = symdelta.build_graph(final_new, final_gone, rename_map, [])
    _assert_no_dangling_edges(nodes, edges)

    symbol_nodes = {n["id"]: n for n in nodes if n["kind"] == "symbol"}
    assert "(root):main.Run" not in symbol_nodes
    assert "pkg:main.Run" not in symbol_nodes  # the phantom id the bug used to fabricate
    assert symbol_nodes["pkg:Run"]["state"] == "changed"
    assert len(symbol_nodes) == 3  # Run, NewCheck, OldCheck -- never a fourth, phantom entry


def test_build_graph_merges_via_git_rename_map_alone():
    # No edge collapses (both calls genuinely changed target), so move_collapse finds nothing --
    # the git rename map is the only evidence FilterUnusable moved packages. Still one node.
    final_new = [["corelib/ratelimit/a.go", "FilterUnusable", "corelib/ratelimit/b.go", "NewCheck"]]
    final_gone = [
        ["corelib/ratelimit/internal/a.go", "FilterUnusable", "corelib/ratelimit/internal/b.go", "OldCheck"]
    ]
    rename_map = {"corelib/ratelimit/a.go": "corelib/ratelimit/internal/a.go"}
    nodes, edges, _, _ = symdelta.build_graph(final_new, final_gone, rename_map, [])
    _assert_no_dangling_edges(nodes, edges)
    symbol_nodes = {n["id"]: n for n in nodes if n["kind"] == "symbol"}
    assert "corelib/ratelimit/internal:FilterUnusable" not in symbol_nodes
    assert symbol_nodes["corelib/ratelimit:FilterUnusable"]["state"] == "changed"


# ---- pure-logic tests: a case-only rename must merge (Go export capitalisation), but never by
# guessing through an ambiguous match ----------------------------------------------------------


def test_resolve_symbol_merges_case_fallback():
    rows = [
        # gone_file isn't itself a file git recognised as renamed (source 2 has nothing to go on),
        # but every OTHER renamed file agrees pkg/internal -> pkg, so source 3's own fallback applies.
        ("package-level match",
         {"pkg/internal:newFoo"}, {"pkg:NewFoo"},
         {"pkg/internal:newFoo": "pkg/internal/unrelated.go"},
         {"pkg/internal:newFoo": "newFoo"},
         {"pkg/other.go": "pkg/internal/other.go"},
         {"pkg/internal:newFoo": "pkg:NewFoo"}, {"pkg:NewFoo": "newFoo"}),
        # Two live candidates differ from "foobar" only by case -- picking either would be a guess,
        # so this must fall through to the exact-match-only path: build the (not actually live)
        # exact-name id rather than merge into one of the two real candidates.
        ("ambiguous case match falls through without merging",
         {"pkg/internal:foobar"}, {"pkg:fooBar", "pkg:FooBar"},
         {"pkg/internal:foobar": "pkg/internal/a.go"},
         {"pkg/internal:foobar": "foobar"},
         {"pkg/a.go": "pkg/internal/a.go"},
         {"pkg/internal:foobar": "pkg:foobar"}, {}),
    ]
    for label, sym_gone, sym_new, gone_file, gone_name, rename_map, want_merge, want_from in rows:
        merge_map, _, renamed_from = symdelta.resolve_symbol_merges(
            sym_gone, sym_new, gone_file, gone_name, rename_map, []
        )
        assert merge_map == want_merge, label
        assert renamed_from == want_from, label


def test_build_graph_merges_an_export_capitalisation_rename_across_a_package_move():
    final_new = [["pkg/a.go", "IncrementChecksTotal", "pkg/b.go", "NewCheck"]]
    final_gone = [["pkg/internal/a.go", "incrementChecksTotal", "pkg/internal/b.go", "OldCheck"]]
    rename_map = {"pkg/a.go": "pkg/internal/a.go"}

    nodes, edges, _, _ = symdelta.build_graph(final_new, final_gone, rename_map, [])
    _assert_no_dangling_edges(nodes, edges)

    symbol_nodes = {n["id"]: n for n in nodes if n["kind"] == "symbol"}
    assert "pkg/internal:incrementChecksTotal" not in symbol_nodes
    changed = symbol_nodes["pkg:IncrementChecksTotal"]
    assert changed["state"] == "changed"
    assert changed["was"] == "incrementChecksTotal"


def test_build_graph_ambiguous_case_only_candidates_are_not_merged_into():
    final_new = [
        ["pkg/a.go", "fooBar", "pkg/x.go", "Other"],
        ["pkg/b.go", "FooBar", "pkg/y.go", "Other2"],
    ]
    final_gone = [["pkg/internal/a.go", "foobar", "pkg/internal/x.go", "OldOther"]]
    rename_map = {"pkg/a.go": "pkg/internal/a.go"}

    nodes, edges, _, _ = symdelta.build_graph(final_new, final_gone, rename_map, [])
    _assert_no_dangling_edges(nodes, edges)

    symbol_nodes = {n["id"]: n for n in nodes if n["kind"] == "symbol"}
    # both stay their own, untouched "new" node -- neither absorbs the gone-side symbol.
    assert symbol_nodes["pkg:fooBar"]["state"] == "new"
    assert symbol_nodes["pkg:FooBar"]["state"] == "new"
    assert "was" not in symbol_nodes["pkg:fooBar"]
    assert "was" not in symbol_nodes["pkg:FooBar"]


def test_build_graph_genuine_deletion_with_no_rename_evidence_stays_gone():
    # No rename_map, no moved_pairs -- nothing at all suggests Removed moved anywhere, so it
    # must surface as a plain "gone" node, not get folded into anything.
    final_new = [["pkg/a.go", "Keep", "pkg/b.go", "Other"]]
    final_gone = [["pkg/old.go", "Removed", "pkg/b.go", "Other"]]

    nodes, edges, _, _ = symdelta.build_graph(final_new, final_gone, {}, [])
    _assert_no_dangling_edges(nodes, edges)

    symbol_nodes = {n["id"]: n for n in nodes if n["kind"] == "symbol"}
    assert symbol_nodes["pkg:Removed"]["state"] == "gone"
    assert "was" not in symbol_nodes["pkg:Removed"]


def test_build_graph_symbol_state_new_gone_changed():
    final_new = [["a/x.go", "OnlyNew", "a/y.go", "Shared"]]
    final_gone = [["a/x.go", "OnlyGone", "a/y.go", "Shared"]]
    nodes, edges, presets, counts = symdelta.build_graph(final_new, final_gone)
    states = {n["id"]: n["state"] for n in nodes if n["kind"] == "symbol"}
    assert states["a:OnlyNew"] == "new"
    assert states["a:OnlyGone"] == "gone"
    assert states["a:Shared"] == "changed"
    assert counts == {"symbols": 3, "packages": 1, "edges_new": 1, "edges_gone": 1}
    assert edges[0]["id"] == "e0" and edges[1]["id"] == "g0"


def test_build_graph_package_chain_reaches_the_root():
    # "a" > "a/b" > "a/b/c" is a pure pass-through chain (each ancestor has exactly one child,
    # itself a package, and no symbol of its own) -- it collapses fully into one box at the
    # deepest id, not three nested single-child boxes.
    nodes, edges, presets, counts = symdelta.build_graph(
        [["a/b/c/x.go", "Fn", "a/b/c/y.go", "Other"]], []
    )
    pkg_nodes = {n["id"]: n for n in nodes if n["kind"] == "pkg"}
    assert set(pkg_nodes) == {"a/b/c"}
    assert pkg_nodes["a/b/c"]["parent"] is None
    assert pkg_nodes["a/b/c"]["label"] == "a/b/c"
    assert pkg_nodes["a/b/c"]["depth"] == 0
    assert presets["symbols"] == ["a/b/c"]


def test_drop_test_edges_removes_an_edge_touching_a_test_file_on_either_end():
    edges = [
        ["a/x_test.go", "TestFoo", "a/y.go", "Bar"],
        ["a/y.go", "Bar", "a/z_test.go", "TestBaz"],
        ["a/y.go", "Bar", "a/z.go", "Baz"],
    ]
    assert symdelta._drop_test_edges(edges) == [["a/y.go", "Bar", "a/z.go", "Baz"]]


# ---- pure-logic tests: pass-through package chains collapse into one box ----------------


def test_build_graph_does_not_collapse_a_package_with_two_children():
    # "src" has two package children ("src/a", "src/b") -- neither is a lone child, so "src"
    # stays a real box and both children stay uncollapsed too.
    final_new = [["src/a/x.go", "Fn1", "src/b/y.go", "Fn2"]]
    nodes, edges, _, _ = symdelta.build_graph(final_new, [])
    pkg_nodes = {n["id"]: n for n in nodes if n["kind"] == "pkg"}
    assert set(pkg_nodes) == {"src", "src/a", "src/b"}
    assert pkg_nodes["src/a"]["parent"] == "src" and pkg_nodes["src/a"]["label"] == "a"
    assert pkg_nodes["src/b"]["parent"] == "src" and pkg_nodes["src/b"]["label"] == "b"


def test_build_graph_does_not_collapse_a_package_with_a_symbol_child_alongside_its_pkg_child():
    # "pkg" has one package child ("pkg/inner") *and* a symbol of its own ("Keep") -- the symbol
    # child alone is enough to disqualify it, even though it has exactly one package child too.
    final_new = [["pkg/a.go", "Keep", "pkg/inner/b.go", "Fn"]]
    nodes, edges, _, _ = symdelta.build_graph(final_new, [])
    pkg_nodes = {n["id"]: n for n in nodes if n["kind"] == "pkg"}
    assert set(pkg_nodes) == {"pkg", "pkg/inner"}
    assert pkg_nodes["pkg/inner"]["parent"] == "pkg"
    assert pkg_nodes["pkg/inner"]["label"] == "inner"


def test_build_graph_depths_stay_contiguous_after_compression_for_pkg_and_symbol_nodes():
    # "src" has its own symbol ("Keep") plus one package child ("src/s2s"), so "src" itself
    # stays; "src/s2s" holds nothing but its own single package child ("src/s2s/handler") and
    # folds away. The survivor keeps its own (deepest) id, gets "src" -- the nearest surviving
    # ancestor -- as its parent, and a label that joins the elided segment back in.
    final_new = [
        ["src/other.go", "Keep", "src/s2s/handler/handle.go", "Handle"],
        ["top/x.go", "TopFn", "top/y.go", "TopFn2"],
    ]
    nodes, edges, _, _ = symdelta.build_graph(final_new, [])
    by_id = {n["id"]: n for n in nodes}
    assert "src/s2s" not in by_id
    assert by_id["src/s2s/handler"]["parent"] == "src"
    assert by_id["src/s2s/handler"]["label"] == "s2s/handler"
    assert by_id["src"]["depth"] == 0
    assert by_id["src/s2s/handler"]["depth"] == 1  # "src/s2s" folded away, not counted
    assert by_id["top"]["depth"] == 0
    assert by_id["src:Keep"]["depth"] == 1
    assert by_id["src/s2s/handler:Handle"]["depth"] == 2
    assert by_id["top:TopFn"]["depth"] == 1
    assert by_id["top:TopFn2"]["depth"] == 1


def test_build_graph_root_pkg_sentinel_is_never_folded_into_or_through():
    # "(root)" holds a symbol, not a package, so it can never satisfy the fold condition; it
    # must survive untouched even while an unrelated chain elsewhere in the same graph collapses.
    final_new = [["main.go", "Run", "src/s2s/handler/handle.go", "Handle"]]
    nodes, edges, _, _ = symdelta.build_graph(final_new, [])
    pkg_nodes = {n["id"]: n for n in nodes if n["kind"] == "pkg"}
    assert "(root)" in pkg_nodes
    root = pkg_nodes["(root)"]
    assert root["parent"] is None
    assert root["depth"] == 0
    assert root["label"] == "(root)"
    # unrelated chain still collapses fully: "src" itself has no symbol of its own here either
    assert pkg_nodes["src/s2s/handler"]["parent"] is None
    assert set(pkg_nodes) == {"(root)", "src/s2s/handler"}


# ---- pure-logic tests: root-level files get a real package, not an empty string --------


def test_root_file_ids_use_the_root_sentinel():
    assert symdelta.pkg_of("main.go") == "(root)"
    assert symdelta.pkg_of("index.ts") == "(root)"
    assert symdelta.pkg_of("a/b.go") == "a"
    sid = symdelta.sym_id("main.go", "main")
    assert sid == "(root):main.main"
    assert not sid.startswith(":")


def test_build_graph_two_root_files_with_same_symbol_name_stay_distinct():
    # "handler" declared in two unrelated root-level files must not collapse onto one node --
    # unlike a real subdirectory, the repo root has no natural grouping that guarantees distinct
    # names (Go's own package-uniqueness rule doesn't reach across separate root-level files).
    final_new = [
        ["src/caller.go", "Caller", "a.go", "handler"],
        ["src/caller.go", "Other", "b.go", "handler"],
    ]
    nodes, edges, _, _ = symdelta.build_graph(final_new, [])
    _assert_no_dangling_edges(nodes, edges)
    symbol_ids = {n["id"] for n in nodes if n["kind"] == "symbol"}
    assert {"(root):a.handler", "(root):b.handler"} <= symbol_ids
    assert not any(sid.startswith(":") for sid in symbol_ids)

    root_pkg = next(n for n in nodes if n["id"] == "(root)")
    assert root_pkg["parent"] is None
    a_handler = next(n for n in nodes if n["id"] == "(root):a.handler")
    assert a_handler["parent"] == "(root)"


# ---- pure-logic tests: decl_ranges and symbol node "range" ------------------------------


def _edge_with_range(from_file, from_sym, from_start, from_end, to_file, to_sym, to_start, to_end):
    return {
        "FromFile": from_file, "FromSym": from_sym, "FromStart": from_start, "FromEnd": from_end,
        "ToFile": to_file, "ToSym": to_sym, "ToStart": to_start, "ToEnd": to_end,
    }


def test_decl_ranges_keeps_only_positive_int_starts_and_defaults_bad_ends():
    edges = [
        _edge_with_range("a/x.go", "Good", 10, 20, "a/y.go", "Callee", 5, 5),
        {"FromFile": "a/x.go", "FromSym": "Missing", "ToFile": "a/y.go", "ToSym": "Callee"},
        _edge_with_range("a/x.go", "ZeroStart", 0, 5, "a/y.go", "Callee", 5, 5),
        _edge_with_range("a/x.go", "NegStart", -1, 5, "a/y.go", "Callee", 5, 5),
        _edge_with_range("a/x.go", "StrStart", "10", 5, "a/y.go", "Callee", 5, 5),
        _edge_with_range("a/x.go", "NoEnd", 7, None, "a/y.go", "Callee", 5, 5),
        _edge_with_range("a/x.go", "BackwardsEnd", 7, 3, "a/y.go", "Callee", 5, 5),
    ]
    ranges = symdelta.decl_ranges(edges)

    def r(sym):
        return ranges.get(("a/x.go", symdelta.sym_id("a/x.go", sym)))

    assert r("Good") == (10, 20)
    assert r("Missing") is None
    assert r("ZeroStart") is None
    assert r("NegStart") is None
    assert r("StrStart") is None  # extractor emits real ints; a string start is untrusted
    assert r("NoEnd") == (7, 7)
    assert r("BackwardsEnd") == (7, 7)


def test_decl_ranges_first_seen_wins_across_edges():
    edges = [
        _edge_with_range("a/x.go", "Caller", 10, 20, "a/y.go", "Callee", 5, 5),
        _edge_with_range("a/x.go", "Caller", 100, 200, "a/y.go", "Callee", 50, 50),
    ]
    ranges = symdelta.decl_ranges(edges)
    assert ranges[("a/x.go", symdelta.sym_id("a/x.go", "Caller"))] == (10, 20)


def test_build_graph_symbol_range_by_state():
    rows = [
        ("new symbols get the head range",
         [["pkg/a.go", "Caller", "pkg/a.go", "Callee"]], [],
         {"head_ranges": {("pkg/a.go", "pkg:Caller"): (3, 8), ("pkg/a.go", "pkg:Callee"): (10, 10)}},
         {"pkg:Caller": [3, 8], "pkg:Callee": [10, 10]}),
        ("gone symbols get the base range",
         [], [["pkg/a.go", "OldCaller", "pkg/a.go", "OldCallee"]],
         {"base_ranges": {("pkg/a.go", "pkg:OldCaller"): (1, 4), ("pkg/a.go", "pkg:OldCallee"): (6, 6)}},
         {"pkg:OldCaller": [1, 4], "pkg:OldCallee": [6, 6]}),
        # Helper exists on both sides (state "changed") -- the surfaced range is HEAD's, since only
        # a "gone" node's range is BASE-side.
        ("changed symbol uses the head range, not base",
         [["pkg/a.go", "Helper", "pkg/b.go", "NewTarget"]], [["pkg/a.go", "Helper", "pkg/c.go", "OldTarget"]],
         {"head_ranges": {("pkg/a.go", "pkg:Helper"): (20, 25)},
          "base_ranges": {("pkg/a.go", "pkg:Helper"): (1, 5)}},
         {"pkg:Helper": [20, 25]}),
        ("a symbol with no range entry gets no range key",
         [["pkg/a.go", "Caller", "pkg/a.go", "Callee"]], [], {}, {"pkg:Caller": None}),
    ]
    for label, final_new, final_gone, kwargs, want in rows:
        nodes, _, _, _ = symdelta.build_graph(final_new, final_gone, **kwargs)
        by_id = {n["id"]: n for n in nodes if n["kind"] == "symbol"}
        for sid, want_range in want.items():
            if want_range is None:
                assert "range" not in by_id[sid], label
            else:
                assert by_id[sid]["range"] == want_range, f"{label}: {sid}"


def test_build_graph_merge_target_gets_range_keyed_on_its_head_file():
    # Helper moves from pkg/internal to pkg: one call is unchanged (collapses away), a second is
    # genuinely new, so Helper survives as one "changed" node at its head package (see
    # test_build_graph_merges_a_symbol_that_moved_package_and_changed_its_calls). Its range must
    # come from head_ranges keyed on the HEAD file/sid the merge resolves to, not the pre-move one.
    new = [
        ["pkg/a.go", "Helper", "pkg/b.go", "Target"],
        ["pkg/a.go", "Helper", "pkg/c.go", "NewTarget"],
    ]
    gone = [
        ["pkg/internal/a.go", "Helper", "pkg/b.go", "Target"],
        ["pkg/internal/a.go", "Helper", "pkg/d.go", "OldTarget"],
    ]
    final_new, final_gone, moved, moved_pairs = symdelta.move_collapse(new, gone)
    head_ranges = {("pkg/a.go", "pkg:Helper"): (2, 9)}
    nodes, _, _, _ = symdelta.build_graph(
        final_new, final_gone, moved_pairs=moved_pairs, head_ranges=head_ranges
    )
    helper = next(n for n in nodes if n["id"] == "pkg:Helper")
    assert helper["state"] == "changed"
    assert helper["range"] == [2, 9]


def test_build_graph_same_sid_from_two_files_uses_the_first_files_range():
    # sym_id's own docstring accepts a same-package, same-name collision across two files as a
    # display simplification: both collapse onto one sid/one node, and sym_file keeps the first
    # file seen. The range map keys on (file, sid), not sid alone, so the lookup for that node
    # must land on the first file's range, never the second file's.
    final_new = [
        ["pkg/one.go", "String", "pkg/callee.go", "Callee"],
        ["pkg/two.go", "String", "pkg/callee.go", "Callee"],
    ]
    head_ranges = {
        ("pkg/one.go", "pkg:String"): (1, 2),
        ("pkg/two.go", "pkg:String"): (100, 200),
    }
    nodes, _, _, _ = symdelta.build_graph(final_new, [], head_ranges=head_ranges)
    string_node = next(n for n in nodes if n["id"] == "pkg:String")
    assert string_node["file"] == "pkg/one.go"
    assert string_node["range"] == [1, 2]


# ---- pure-logic tests: CACHE_DIR's fallback chain ---------------------------------------


def test_resolve_cache_dir_xdg_cache_home_wins(tmp_path):
    # An explicit XDG_CACHE_HOME is a decision, not a guess; silently relocating it, even when it
    # is unwritable, would hide a misconfiguration behind a cache that keeps getting rebuilt.
    (tmp_path / "with-dot-cache" / ".cache").mkdir(parents=True)
    rows = [
        ("xdg set", tmp_path, "/xdg/cache"),
        ("xdg set and ~/.cache exists", tmp_path / "with-dot-cache", "/xdg/cache"),
        ("xdg not writable", Path("/nonexistent"), "/proc"),
    ]
    for label, home, xdg in rows:
        result = symdelta._resolve_cache_dir({"XDG_CACHE_HOME": xdg}, home)
        assert result == Path(xdg) / "code-walkthrough", label


def test_resolve_cache_dir_falls_back_to_dot_cache_when_xdg_unset(tmp_path):
    result = symdelta._resolve_cache_dir({}, tmp_path)

    assert result == tmp_path / ".cache" / "code-walkthrough"


def test_resolve_cache_dir_falls_back_to_tmp_when_dot_cache_is_not_writable(tmp_path):
    # The agent-sandbox case: ~/.cache is read-only, so the extractor would never build and the
    # whole symbols section would go missing over a cache directory.
    cw_testlib.require_non_root()
    home = tmp_path / "home"
    (home / ".cache").mkdir(parents=True)
    (home / ".cache").chmod(0o555)
    try:
        result = symdelta._resolve_cache_dir({}, home, tmp="/scratch")
    finally:
        (home / ".cache").chmod(0o755)

    assert result == Path("/scratch") / "code-walkthrough"


# ---- pure-logic tests: detect_language's tie-break is honest and deterministic ----------


def test_detect_language_tie_break_is_honest_and_deterministic(tmp_path):
    # "aaa.ts" sorts before "zzz.go" in git's own diff listing, so an insertion-order tie
    # break would have picked typescript; the fix must be independent of that ordering.
    repo, base, head = make_repo(
        tmp_path,
        {"go.mod": "module example.com/tie\n\ngo 1.21\n",
         "aaa.ts": "export function a() {}\n",
         "zzz.go": "package main\n\nfunc Caller() {}\n"},
        {"aaa.ts": "export function a() { console.log(1); }\n",
         "zzz.go": "package main\n\nfunc Caller() {}\nfunc Other() {}\n"},
    )

    lang, reason = symdelta.detect_language(repo, base, head)
    assert lang == "go"
    assert "tied at 1 changed files each" in reason
    assert "not a real majority" in reason


def test_detect_language_routes_by_extension(tmp_path):
    rows = [
        ("python", "a.py", "def a():\n    pass\n", "def a():\n    return 1\n"),
        ("rust", "a.rs", "fn a() {}\n", "fn a() { let _ = 1; }\n"),
    ]
    for want, name, before, after in rows:
        (tmp_path / want).mkdir()
        repo, base, head = make_repo(tmp_path / want, {name: before}, {name: after})

        lang, reason = symdelta.detect_language(repo, base, head)
        assert lang == want and reason is None, want


def test_detect_language_works_against_an_orphan_baseline_with_no_merge_base(tmp_path):
    # Three-dot diff needs a merge base; an orphan baseline has none, so this used to fail with
    # "fatal: ...: no merge base" before detect_language switched to two-dot on a resolved base.
    repo, head = _head_only_repo(tmp_path, {"a.py": "def a():\n    pass\n"})

    lang, reason = symdelta.detect_language(repo, orphan_baseline(repo), head)
    assert lang == "python" and reason is None


def test_lsp_language_tables_all_cover_typescript_python_and_rust():
    # WORKTREE_PREP and ANALYSERS dispatch every LSP-tier language, or a newly-added one would
    # silently fall through in one but not another (e.g. routed by LANG_EXTENSIONS but with no
    # ANALYSERS entry).
    for lang in ("typescript", "python", "rust"):
        assert lang in symdelta.WORKTREE_PREP
        assert lang in symdelta.ANALYSERS
    assert set(symdelta.LANG_EXTENSIONS.values()) >= {"typescript", "python", "rust", "go"}

    # DEPENDENCY_FILES_BY_LANG/DEPENDENCY_REASON guard only typescript and python: rust
    # worktrees never share a Cargo.lock or target/ between base and head (see WORKTREE_PREP),
    # so a Cargo change can't leak into the other side's resolution the way a shared
    # node_modules or resolver can.
    for lang in ("typescript", "python"):
        assert lang in symdelta.DEPENDENCY_FILES_BY_LANG
        assert lang in symdelta.DEPENDENCY_REASON
    assert "rust" not in symdelta.DEPENDENCY_FILES_BY_LANG
    assert "rust" not in symdelta.DEPENDENCY_REASON


# ---- pure-logic + wiring tests: refuse a TS diff when node_modules would lie ------------


def test_dependency_files_changed(tmp_path):
    rows = [
        ("package.json changed", None,
         {"package.json": '{"version": "1.0.0"}\n'}, {"package.json": '{"version": "1.0.1"}\n'}, True),
        ("unrelated change", None,
         {"src/a.ts": "export function a() {}\n", "package.json": '{"version": "1.0.0"}\n'},
         {"src/a.ts": "export function a() { return 1; }\n"}, False),
        ("yarn.lock", None,
         {"yarn.lock": "# yarn lockfile v1\n"}, {"yarn.lock": "# yarn lockfile v1\nleft-pad@1.0.0:\n"}, True),
        ("pnpm-lock.yaml", None,
         {"pnpm-lock.yaml": "lockfileVersion: '6.0'\n"},
         {"pnpm-lock.yaml": "lockfileVersion: '6.0'\ndependencies: {}\n"}, True),
        ("pyproject.toml for python", "python",
         {"pyproject.toml": '[project]\nname = "x"\nversion = "0.1.0"\n'},
         {"pyproject.toml": '[project]\nname = "x"\nversion = "0.2.0"\n'}, True),
        ("python ignores a changed ts lockfile", "python",
         {"package.json": '{"version": "1.0.0"}\n', "a.py": "def a():\n    pass\n"},
         {"package.json": '{"version": "1.0.1"}\n'}, False),
        # Rust has no entry in DEPENDENCY_FILES_BY_LANG: each worktree resolves against its own
        # Cargo.lock and target/, so a Cargo change on one side can't affect the other's resolution.
        ("Cargo.lock for rust", "rust",
         {"Cargo.lock": "# auto-generated\nversion = 3\n"}, {"Cargo.lock": "# auto-generated\nversion = 4\n"}, False),
    ]
    for i, (label, lang, base_files, head_files, want) in enumerate(rows):
        (tmp_path / str(i)).mkdir()
        repo, base, head = make_repo(tmp_path / str(i), base_files, head_files)
        args = (lang,) if lang else ()
        assert symdelta.dependency_files_changed(repo, base, head, *args) is want, label


# ---- pure-logic tests: TypeScript dependency compatibility decided from declared specs -----


def test_ts_dependency_incompatible(tmp_path):
    left_pad = '{"dependencies": {"left-pad": "1.0.0"}}\n'
    rows = [
        ("additions only", {"package.json": left_pad},
         {"package.json": '{"dependencies": {"left-pad": "1.0.0", "chalk": "5.0.0"}}\n'}, False, None),
        ("a dependency is removed", {"package.json": left_pad},
         {"package.json": '{"dependencies": {}}\n'}, True, "removed: left-pad"),
        ("a version spec changed", {"package.json": left_pad},
         {"package.json": '{"dependencies": {"left-pad": "2.0.0"}}\n'}, True, "version changed: left-pad"),
        ("lockfile-only change", {"package.json": left_pad, "package-lock.json": '{"lockfileVersion": 2}\n'},
         {"package-lock.json": '{"lockfileVersion": 3}\n'}, False, None),
        ("package.json missing at base", {"src/a.ts": "export function a() {}\n"},
         {"package.json": left_pad}, True, "missing or unparseable"),
        ("package.json is unparseable", {"package.json": left_pad},
         {"package.json": "{not valid json\n"}, True, "missing or unparseable"),
        # A dict.update() merge across sections would let devDependencies silently overwrite
        # dependencies' newer spec here, hiding a real version bump behind section iteration order.
        ("sections disagree on the spec", {"package.json": left_pad},
         {"package.json": '{"dependencies": {"left-pad": "2.0.0"}, "devDependencies": {"left-pad": "1.0.0"}}\n'},
         True, "version changed: left-pad"),
        ("same spec appears in two sections", {"package.json": left_pad},
         {"package.json": '{"dependencies": {"left-pad": "1.0.0"}, "devDependencies": {"left-pad": "1.0.0"}}\n'},
         False, None),
    ]
    for i, (label, base_files, head_files, want_flag, want_reason) in enumerate(rows):
        (tmp_path / str(i)).mkdir()
        repo, base, head = make_repo(tmp_path / str(i), base_files, head_files)
        incompatible, reason = symdelta.ts_dependency_incompatible(repo, base, head)
        assert incompatible is want_flag, label
        if want_reason is None:
            assert reason is None, label
        else:
            assert want_reason in reason, label


# ---- wiring tests: link_node_modules must not symlink a relative target --------------------


def test_link_node_modules_resolves_a_relative_repo_path(tmp_path, monkeypatch):
    # A relative repo, embedded verbatim in the symlink target, would point the worktree's
    # node_modules at itself instead of the real one -- worktree and repo live in different dirs.
    repo = tmp_path / "repo"
    (repo / "node_modules").mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    monkeypatch.chdir(repo)
    symdelta.link_node_modules(".", worktree)

    dst = worktree / "node_modules"
    assert dst.exists()
    assert os.path.isabs(os.readlink(dst))


# ---- wiring tests: node_modules coverage preflight, before any worktree gets created -------


def test_check_node_modules_coverage(tmp_path):
    def lock(version):
        return json.dumps({"packages": {"node_modules/left-pad": {"version": version}}}) + "\n"

    left_pad_caret2 = '{"dependencies": {"left-pad": "^2.0.0"}}\n'
    stale_install = {"node_modules/left-pad/package.json": '{"version": "1.0.0"}\n'}
    # (label, committed files, files/dirs added after the commit (None = empty dir), ok, reason must name)
    rows = [
        ("every dependency resolves", {"package.json": '{"dependencies": {"left-pad": "1.0.0"}}\n'},
         {"node_modules/left-pad": None}, True, ()),
        ("node_modules missing and nothing declared", {"src/a.ts": "export function a() {}\n"},
         {}, True, ()),
        # node_modules exists but never got the newly-declared scoped package -- the stale
        # node_modules case this preflight exists to catch.
        ("a missing scoped package", {"package.json": '{"dependencies": {"@restatedev/restate-sdk": "1.0.0"}}\n'},
         {"node_modules": None}, False, ("@restatedev/restate-sdk",)),
        ("the reason is capped at five names",
         {"package.json": json.dumps({"dependencies": {f"pkg-{i}": "1.0.0" for i in range(7)}}) + "\n"},
         {"node_modules": None}, False, ("and 2 more",)),
        ("a missing peer dependency", {"package.json": '{"peerDependencies": {"react": "18.0.0"}}\n'},
         {"node_modules": None}, False, ("react",)),
        ("a missing optional dependency is ignored",
         {"package.json": '{"optionalDependencies": {"fsevents": "2.0.0"}}\n'},
         {"node_modules": None}, True, ()),
        ("installed version matches the lockfile",
         {"package.json": '{"dependencies": {"left-pad": "^1.0.0"}}\n', "package-lock.json": lock("1.3.0")},
         {"node_modules/left-pad/package.json": '{"version": "1.3.0"}\n'}, True, ()),
        # node_modules/left-pad exists (satisfies the bare presence check) but is still at the
        # version the lockfile no longer resolves to.
        ("a stale installed version",
         {"package.json": left_pad_caret2, "package-lock.json": lock("2.0.0")},
         stale_install, False, ("left-pad",)),
        # with no lockfile to compare against at all, presence is all this preflight can check
        ("presence only when there is no lockfile", {"package.json": left_pad_caret2}, stale_install, True, ()),
        ("presence only when the lockfile is unparseable",
         {"package.json": left_pad_caret2, "package-lock.json": "{not valid json\n"}, stale_install, True, ()),
        ("lockfile version 1 shape",
         {"package.json": left_pad_caret2,
          "package-lock.json": json.dumps({"lockfileVersion": 1, "dependencies": {"left-pad": {"version": "1.0.0"}}}) + "\n"},
         {"node_modules/left-pad/package.json": '{"version": "2.0.0"}\n'}, False, ("left-pad",)),
    ]
    for i, (label, committed, installed, want_ok, names) in enumerate(rows):
        (tmp_path / str(i)).mkdir()
        repo, head = _head_only_repo(tmp_path / str(i), committed)
        for rel_path, content in installed.items():
            if content is None:
                (repo / rel_path).mkdir(parents=True)
            else:
                write_file(repo, rel_path, content)

        ok, reason = symdelta.check_node_modules_coverage(repo, head)
        assert ok is want_ok, label
        if want_ok:
            assert reason is None, label
        for name in names:
            assert name in reason, f"{label}: {name}"


# ---- wiring tests: analyse_typescript's null results carry the right remedy (or none) -----


def test_analyse_typescript_refuses_when_a_dependency_is_removed(tmp_path):
    # Must short-circuit before any typescript-language-server check, so this needs no tool on
    # PATH and is never skipped.
    repo, base, head = make_repo(
        tmp_path,
        {"package.json": '{"dependencies": {"left-pad": "1.0.0"}}\n', "src/a.ts": "export function a() {}\n"},
        {"package.json": '{"dependencies": {}}\n', "src/a.ts": "export function a() { return 1; }\n"},
    )

    result = symdelta.analyse_typescript(repo, base, head)
    assert result["language"] is None
    assert "left-pad" in result["reason"]
    assert "remedy" not in result


def test_analyse_typescript_does_not_refuse_on_dependency_additions_only(tmp_path, monkeypatch):
    # Change 1's whole point: an added-only dependency must not trip the gate that a removed or
    # changed one does. The tooling check is stubbed to fail, so reaching it (reason "boom"
    # instead of a dependency reason) proves the additions-only gate let it through.
    monkeypatch.setattr(symdelta, "check_typescript_tooling", lambda repo, lang="typescript": (False, "boom"))
    repo, base, head = make_repo(
        tmp_path,
        {"package.json": '{"dependencies": {}}\n', "src/a.ts": "export function a() {}\n"},
        {"package.json": '{"dependencies": {"left-pad": "1.0.0"}}\n',
         "src/a.ts": "export function a() { return 1; }\n"},
    )
    (repo / "node_modules" / "left-pad").mkdir(parents=True)

    result = symdelta.analyse_typescript(repo, base, head)
    assert result["language"] is None
    assert result["reason"] == "boom"


def test_analyse_typescript_refuses_with_npm_ci_remedy_when_node_modules_is_stale(tmp_path):
    # Also needs no tool on PATH: the node_modules preflight runs before check_typescript_tooling.
    repo, base, head = make_repo(
        tmp_path,
        {"package.json": '{"dependencies": {}}\n', "src/a.ts": "export function a() {}\n"},
        {"package.json": '{"dependencies": {"left-pad": "1.0.0"}}\n',
         "src/a.ts": "export function a() { return 1; }\n"},
    )
    (repo / "node_modules").mkdir()  # exists, but never got left-pad

    result = symdelta.analyse_typescript(repo, base, head)
    assert result["language"] is None
    assert "left-pad" in result["reason"]
    assert result["remedy"] == "npm ci"


def test_analyse_lsp_attaches_language_server_remedy_when_tooling_check_fails(tmp_path, monkeypatch):
    # LANGUAGE_SERVER_REMEDY's `npm i -g` would be a no-op on a binary that's already installed;
    # a PATH-trap reason carries its own fix, and that must win, with the fix pulled out of the
    # reason so it isn't shown twice.
    path_trap_reason = ('pyright-langserver is installed in /opt/npm/bin but that directory is '
                        'not on PATH; add it, e.g. export PATH="/opt/npm/bin:$PATH"')
    repo, base, head = make_repo(
        tmp_path, {"a.py": "def a():\n    pass\n"}, {"a.py": "def a():\n    return 1\n"}
    )
    rows = [
        ("plain reason gets the language-server remedy", "boom",
         {"language": None, "reason": "boom", "remedy": symdelta.LANGUAGE_SERVER_REMEDY["python"]}),
        ("path-trap reason carries its own remedy", path_trap_reason,
         {"language": None,
          "reason": 'pyright-langserver is installed in /opt/npm/bin but that directory is not on PATH',
          "remedy": 'export PATH="/opt/npm/bin:$PATH"'}),
    ]
    for label, tooling_reason, want in rows:
        monkeypatch.setattr(
            symdelta, "check_typescript_tooling", lambda repo, lang="typescript": (False, tooling_reason)
        )
        assert symdelta.analyse_python(repo, base, head) == want, label


def test_analyse_rust_does_not_bail_on_a_cargo_lock_change(tmp_path, monkeypatch):
    # A Cargo.lock change between base and head must still let analyse_rust reach the tooling
    # check (forced here to fail for an unrelated reason) rather than bailing earlier on a
    # dependency-manifest reason. If analyse_lsp's `lang == "python"` gate ever regresses to
    # cover rust too, this fails with the dependency reason instead of "boom".
    monkeypatch.setattr(symdelta, "check_typescript_tooling", lambda repo, lang="typescript": (False, "boom"))
    repo, base, head = make_repo(
        tmp_path,
        {"Cargo.lock": "# auto-generated\nversion = 3\n", "src/a.rs": "fn a() {}\n"},
        {"Cargo.lock": "# auto-generated\nversion = 4\n", "src/a.rs": "fn a() { let _ = 1; }\n"},
    )

    result = symdelta.analyse_rust(repo, base, head)
    assert result == {
        "language": None,
        "reason": "boom",
        "remedy": symdelta.LANGUAGE_SERVER_REMEDY["rust"],
    }


# ---- --doctor: no diff, base or head needed ---------------------------------------------


def test_doctor_report_is_ok_for_every_language_when_everything_checks_out(monkeypatch):
    monkeypatch.setattr(symdelta.shutil, "which", lambda tool: "/usr/bin/go" if tool == "go" else None)
    monkeypatch.setattr(symdelta, "build_extractor", lambda: None)
    monkeypatch.setattr(symdelta, "check_language_server", lambda repo, lang: (True, None))

    lines = symdelta.doctor_report().splitlines()

    assert lines[0] == "go: toolchain OK, extractor builds"
    assert any(line.startswith("typescript: OK") for line in lines)
    assert any(line.startswith("python: OK") for line in lines)
    assert any(line.startswith("rust: OK") for line in lines)


def test_doctor_report_names_the_go_toolchain_as_missing(monkeypatch):
    monkeypatch.setattr(symdelta.shutil, "which", lambda tool: None)
    monkeypatch.setattr(symdelta, "check_language_server", lambda repo, lang: (False, "x"))

    assert symdelta.doctor_report().splitlines()[0] == "go: toolchain not found on PATH"


def test_doctor_report_prefers_the_path_trap_remedy_over_language_server_remedy(monkeypatch):
    def fake_check(repo, lang):
        if lang == "typescript":
            return False, ('typescript-language-server is installed in /opt/npm/bin but that '
                            'directory is not on PATH; add it, e.g. '
                            'export PATH="/opt/npm/bin:$PATH"')
        return False, f"{lang} tool not found on PATH"

    monkeypatch.setattr(symdelta.shutil, "which", lambda tool: None)
    monkeypatch.setattr(symdelta, "check_language_server", fake_check)

    report = symdelta.doctor_report()

    by_lang = {line.split(":", 1)[0]: line for line in report.splitlines()}
    assert 'export PATH="/opt/npm/bin:$PATH"' in by_lang["typescript"]
    # And named exactly once -- check_language_server's reason already carries the fix, so
    # printing the split-out remedy too must not repeat that same PATH line.
    assert by_lang["typescript"].count('export PATH="/opt/npm/bin:$PATH"') == 1
    assert symdelta.LANGUAGE_SERVER_REMEDY["python"] in by_lang["python"]


def test_doctor_report_survives_a_present_but_broken_binary(monkeypatch):
    # shutil.which finds it, but spawning it still raises OSError (e.g. not executable) --
    # check_language_server only guards the initialize round-trip, not the spawn itself, so
    # doctor_report must catch this rather than losing the whole report to one bad language.
    def fake_check(repo, lang):
        if lang == "typescript":
            raise OSError("Exec format error")
        return True, None

    monkeypatch.setattr(symdelta.shutil, "which", lambda tool: None)
    monkeypatch.setattr(symdelta, "check_language_server", fake_check)

    report = symdelta.doctor_report()

    by_lang = {line.split(":", 1)[0]: line for line in report.splitlines()}
    assert "failed to start" in by_lang["typescript"]
    assert by_lang["python"].strip().endswith("OK (pyright-langserver advertises callHierarchy)")


def test_main_doctor_flag_needs_no_repo_base_or_head(monkeypatch):
    monkeypatch.setattr(symdelta, "doctor_report", lambda: "stubbed doctor output")
    monkeypatch.setattr(sys, "argv", ["symdelta.py", "--doctor"])
    buf = io.StringIO()

    with contextlib.redirect_stdout(buf):
        rc = symdelta.main()

    assert rc == 0
    assert buf.getvalue().strip() == "stubbed doctor output"


# ---- pure-logic tests: a hung LSP extractor raises a clean error, not a raw traceback ---


class _FakeTimingOutProc:
    """Stands in for subprocess.Popen: communicate() times out once (as if the real process
    hung). By default it then succeeds on the follow-up call run_in_process_group makes after
    killing it, matching how a real process behaves post-SIGKILL. With hangs_after_kill=True,
    it keeps timing out on that follow-up call too, standing in for a descendant that inherited
    the same stdout/stderr pipes and is still holding them open."""

    def __init__(self, cmd, hangs_after_kill=False):
        self.pid = 999999999  # never a real pid; os.getpgid on it is expected to race/fail
        self.returncode = 0
        self._cmd = cmd
        self._calls = 0
        self._hangs_after_kill = hangs_after_kill

    def kill(self):
        pass

    def communicate(self, timeout=None):
        self._calls += 1
        if self._calls == 1 or self._hangs_after_kill:
            raise subprocess.TimeoutExpired(cmd=self._cmd, timeout=timeout)
        return "", ""


def test_tooling_timeout_surfaces_as_runtime_error(monkeypatch):
    seen_path = {}

    def fake_popen(cmd, **kwargs):
        seen_path["path"] = cmd[-1]
        return _FakeTimingOutProc(cmd)

    monkeypatch.setattr(symdelta.subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError) as exc:
        symdelta.check_typescript_tooling("/tmp/whatever")
    assert "60" in str(exc.value)

    with pytest.raises(RuntimeError) as exc:
        symdelta.run_ts_extractor("/tmp/worktree", ["a.ts"])
    assert "600" in str(exc.value)
    assert not os.path.exists(seen_path["path"]), "the files list was not cleaned up"


def test_run_in_process_group_handles_getpgid_race_without_crashing(monkeypatch):
    # ProcessLookupError here means only the leader died, not the group -- hangs_after_kill=True
    # simulates a surviving descendant (tsserver) still holding the pipes open, so this also
    # covers the follow-up communicate() staying bounded instead of hanging.
    monkeypatch.setattr(
        symdelta.subprocess, "Popen", lambda cmd, **kwargs: _FakeTimingOutProc(cmd, hangs_after_kill=True)
    )
    monkeypatch.setattr(symdelta.os, "getpgid", _raises(ProcessLookupError()))

    with pytest.raises(RuntimeError) as exc:
        symdelta.run_in_process_group(["ignored"], timeout=1, what="test command")
    assert "test command" in str(exc.value) and "1" in str(exc.value)


def test_run_in_process_group_kills_the_whole_group_not_just_the_direct_child(tmp_path):
    # A hung extract.py that spawned its own child (tsserver, in production) must not leave that
    # child orphaned when the outer timeout fires. This spawns a real grandchild process that
    # outlives a plain SIGKILL of the direct child, then asserts killpg took it down too.
    cw_testlib.require_posix()
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "spawn_and_hang.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(100)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(100)\n"
    )

    with pytest.raises(RuntimeError) as exc:
        symdelta.run_in_process_group([sys.executable, str(script)], timeout=1.5, what="test command")
    assert "test command" in str(exc.value) and "1.5" in str(exc.value)

    child_pid = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 3
    alive = True
    while alive and time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
            time.sleep(0.02)
        except ProcessLookupError:
            alive = False
    assert not alive, "the grandchild survived -- only the direct child was killed"


def test_go_extractor_timeout_is_a_runtime_error(tmp_path, monkeypatch):
    def timeout_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=600)

    monkeypatch.setattr(symdelta.subprocess, "run", timeout_run)
    # a fresh, nonexistent EXTRACTOR_BIN forces build_extractor past its "reuse an existing
    # binary" short-circuit and into the subprocess.run call this test targets
    monkeypatch.setattr(symdelta, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(symdelta, "EXTRACTOR_BIN", tmp_path / "cache" / "symdelta-go-extractor")

    with pytest.raises(RuntimeError) as exc:
        symdelta.build_extractor()
    assert "600" in str(exc.value)

    with pytest.raises(RuntimeError) as exc:
        symdelta.run_extractor("/tmp/some-worktree")
    assert "600" in str(exc.value) and "/tmp/some-worktree" in str(exc.value)


def test_build_extractor_builds_to_a_temp_path_and_replaces_the_real_binary(tmp_path, monkeypatch):
    recorded = {}

    def fake_run(cmd, **kwargs):
        out_path = Path(cmd[cmd.index("-o") + 1])
        recorded["out_path"] = out_path
        out_path.write_text("fake binary")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(symdelta, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(symdelta, "EXTRACTOR_BIN", cache_dir / "symdelta-go-extractor")
    monkeypatch.setattr(symdelta.subprocess, "run", fake_run)

    symdelta.build_extractor()

    assert recorded["out_path"] != cache_dir / "symdelta-go-extractor"
    assert (cache_dir / "symdelta-go-extractor").exists()
    assert not recorded["out_path"].exists(), "temp build file was not replaced away"


def test_run_extractor_omits_goflags_when_go_work_exists_and_sets_it_otherwise(tmp_path, monkeypatch):
    # -mod=mod is illegal once Go is in workspace mode ("go: -mod may only be set to readonly
    # or vendor when in workspace mode"), so this must track go.work's presence, not be constant.
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(symdelta.subprocess, "run", fake_run)
    with_work = tmp_path / "with-work"
    with_work.mkdir()
    (with_work / "go.work").write_text("go 1.21\n\nuse ./a\n")
    without_work = tmp_path / "without-work"
    without_work.mkdir()

    symdelta.run_extractor(with_work)
    assert "GOFLAGS" not in captured["env"]

    symdelta.run_extractor(without_work)
    assert captured["env"]["GOFLAGS"] == "-mod=mod"


def test_lsp_client_shutdown_swallows_timeout_from_an_unreapable_process():
    # A process that survives SIGKILL must not raise out of shutdown() -- it's always called
    # from a `finally` in extract.py, so an unguarded wait() there would shadow whatever
    # original exception was propagating.
    client = lsp_client.LSPClient.__new__(lsp_client.LSPClient)
    client.request = _raises(RuntimeError("no real server"))
    client.notify = lambda *a, **kw: None

    class _NeverDies:
        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)

    client.proc = _NeverDies()
    client.shutdown()  # must not raise


# ---- wiring tests: real (throwaway) git repos ------------------------------------------


def test_no_supported_files_short_circuits_with_null_language(tmp_path):
    repo, base, head = make_repo(tmp_path, {"readme.md": "hello\n"}, {"readme.md": "hello world\n"})

    result = _run_symdelta(repo, base, head)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "language": None,
        "reason": f"no supported files (.go, .py, .rs, .ts, .tsx) changed between {base} and {head}",
    }


def test_detect_language_picks_the_majority_on_a_mixed_diff(tmp_path):
    repo, base, head = make_repo(
        tmp_path,
        {"go.mod": "module example.com/mixed\n\ngo 1.21\n",
         "pkg/a.go": "package pkg\n\nfunc Caller() {}\n",
         "src/a.ts": "export function a() {}\n"},
        {"pkg/a.go": "package pkg\n\nfunc Caller() {}\nfunc Other() {}\n",
         "src/a.ts": "export function a() { console.log(1); }\n",
         "src/b.ts": "export function b() {}\n"},
    )

    lang, reason = symdelta.detect_language(repo, base, head)
    assert lang == "typescript"
    assert "2 typescript" in reason and "1 go" in reason


def test_ts_files_by_side():
    ts_entries = [
        ("A", "src/new.ts", "src/new.ts"),
        ("D", "src/old.ts", "src/old.ts"),
        ("M", "src/both.ts", "src/both.ts"),
        ("R100", "src/before.ts", "src/after.ts"),
        ("M", "readme.md", "readme.md"),
    ]
    mixed = [
        ("A", "src/new.py", "src/new.py"),
        ("D", "src/old.rs", "src/old.rs"),
        ("M", "src/both.ts", "src/both.ts"),
    ]
    rows = [
        ("added is excluded from base, deleted from head (default extensions)", ts_entries, (),
         ["src/before.ts", "src/both.ts", "src/old.ts"], ["src/after.ts", "src/both.ts", "src/new.ts"]),
        ("python extensions", mixed, ((".py",),), [], ["src/new.py"]),
        ("rust extensions", mixed, ((".rs",),), ["src/old.rs"], []),
    ]
    for label, entries, exts, want_base, want_head in rows:
        base_files, head_files = symdelta.ts_files_by_side(entries, *exts)
        assert sorted(base_files) == want_base, label
        assert sorted(head_files) == want_head, label


def test_bad_ref_fails_with_nonzero_exit(tmp_path):
    repo, _head = _head_only_repo(tmp_path, {"readme.md": "hello\n"})

    result = _run_symdelta(repo, "not-a-real-ref", "HEAD")

    assert result.returncode == 1
    assert "bad ref" in result.stderr


def test_analyse_does_not_crash_on_an_orphan_baseline(tmp_path, monkeypatch):
    # No merge base must not break any git step of analyse()'s pipeline (resolve_base,
    # detect_language, _rename_map, ts_diff_entries, _run_in_worktrees all run `git diff
    # base..head`); it used to fail every one with a three-dot "no merge base" error. The
    # language server is not what is under test, so the preflight and extractor are stubbed.
    seen = []

    def fake_extractor(worktree_path, rel_files, lang="typescript"):
        seen.append((lang, list(rel_files), (Path(worktree_path) / rel_files[0]).exists()))
        return [{"FromFile": "a.py", "FromSym": "a", "ToFile": "a.py", "ToSym": "b"}]

    monkeypatch.setattr(symdelta, "check_typescript_tooling", lambda repo, lang="typescript": (True, None))
    monkeypatch.setattr(symdelta, "run_ts_extractor", fake_extractor)
    repo, head = _head_only_repo(tmp_path, {"a.py": "def a():\n    pass\n"})

    result = symdelta.analyse(repo, orphan_baseline(repo), head)

    assert result["language"] == "python" and result["counts"]["edges_new"] == 1
    assert seen == [("python", ["a.py"], True)]  # base side skipped, head side ran


def test_is_empty_base_matches_the_baseline_by_tree_not_by_commit(tmp_path):
    repo, head = _head_only_repo(tmp_path, {"a.py": "def a():\n    pass\n"})

    assert symdelta.is_empty_base(repo, orphan_baseline(repo))
    assert not symdelta.is_empty_base(repo, head)


def test_the_empty_baseline_gets_no_worktree_and_no_extractor_run(tmp_path):
    # A checkout of the empty tree has no go.mod, no package.json and no source, so every
    # extractor used to fail on it and take the whole symbols section down with it.
    repo, head = _head_only_repo(tmp_path, {"a.py": "def a():\n    pass\n"})
    orphan = orphan_baseline(repo)
    seen = []

    def base_extractor(path):
        seen.append(path)
        raise AssertionError("the base extractor must not run against the empty baseline")

    def head_extractor(path):
        assert (path / "a.py").exists()
        return ["edge"]

    base_edges, head_edges = symdelta._run_in_worktrees(repo, orphan, head, base_extractor, head_extractor)

    assert seen == []
    assert base_edges == []
    assert head_edges == ["edge"]


def test_a_real_base_still_gets_its_own_worktree(tmp_path):
    repo, base, head = make_repo(tmp_path, {"a.py": "def a():\n    pass\n"}, {"b.py": "def b():\n    pass\n"})

    both = symdelta._run_in_worktrees(
        repo, base, head,
        lambda p: [(p / "a.py").exists(), (p / "b.py").exists()],
        lambda p: [(p / "a.py").exists(), (p / "b.py").exists()],
    )

    assert both == ([True, False], [True, True])


def test_end_to_end_ts_repo(tmp_path):
    cw_testlib.require_ts_language_server()
    repo, base, head = make_repo(
        tmp_path,
        {
            "tsconfig.json": '{"compilerOptions": {"target": "es2020", "module": "commonjs"}}\n',
            "src/b.ts": "export function callee() {\n    return 1;\n}\n",
            "src/a.ts": 'import { callee } from "./b";\n\nexport function caller() {\n    return 0;\n}\n',
            "index.ts": "export function callee() {\n    return 1;\n}\n\n"
                        "export function caller() {\n    return 0;\n}\n",
            "a.ts": "export function run() {\n    return 1;\n}\n",
            "b.ts": "export function run() {\n    return 2;\n}\n",
            "entry.ts": 'import { run as runA } from "./a";\nimport { run as runB } from "./b";\n\n'
                        "export function caller() {\n    return 0;\n}\n",
        },
        {
            "src/a.ts": 'import { callee } from "./b";\n\nexport function caller() {\n    return callee();\n}\n',
            "index.ts": "export function callee() {\n    return 1;\n}\n\n"
                        "export function caller() {\n    return callee();\n}\n",
            "entry.ts": 'import { run as runA } from "./a";\nimport { run as runB } from "./b";\n\n'
                        "export function caller() {\n    return runA() + runB();\n}\n",
        },
    )

    result = _run_symdelta(repo, base, head)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["language"] == "typescript"
    # one new call each in src/a.ts, index.ts and (two) entry.ts
    assert payload["counts"]["edges_new"] >= 1
    assert any(e["source"] == "src:caller" and e["target"] == "src:callee" for e in payload["edges"])
    symbol_ids = {n["id"] for n in payload["nodes"] if n["kind"] == "symbol"}
    assert "(root):index.caller" in symbol_ids
    assert "(root):index.callee" in symbol_ids
    # Same function name ("run"), two unrelated root files -- must not collapse onto one id.
    assert "(root):a.run" in symbol_ids
    assert "(root):b.run" in symbol_ids
    assert not any(sid.startswith(":") for sid in symbol_ids)
    _assert_no_worktree_leak(repo)


def test_analyse_go_returns_null_language_when_the_extractor_fails(tmp_path, monkeypatch):
    # A RuntimeError out of run_extractor (e.g. the silent-zero guard tripping) used to
    # propagate to main() and exit 1 with symdelta.json never written at all, taking the whole
    # symbols section down with no note. build_extractor failures (toolchain/setup problems)
    # must still raise, so only run_extractor is faked here.
    monkeypatch.setattr(symdelta, "build_extractor", lambda: None)
    monkeypatch.setattr(
        symdelta, "run_extractor",
        _raises(RuntimeError("extractor: loaded 3 packages, 0 in-repo with type info")),
    )
    repo, base, head = make_repo(
        tmp_path,
        {"go.mod": "module example.com/x\n\ngo 1.21\n", "pkg/a.go": "package pkg\n\nfunc A() {}\n"},
        {"pkg/a.go": "package pkg\n\nfunc A() {}\n\nfunc B() {}\n"},
    )

    result = symdelta.analyse_go(repo, base, head)
    assert result == {
        "language": None,
        "reason": "extractor: loaded 3 packages, 0 in-repo with type info",
    }


def test_analyse_lsp_returns_null_language_when_the_extractor_fails(tmp_path, monkeypatch):
    # A RuntimeError out of run_ts_extractor (e.g. extract_edges's own zero guards tripping)
    # used to propagate to main() and exit 1 with symdelta.json never written at all. No remedy
    # here, unlike analyse_go's counterpart -- analyse_lsp's own comment explains why.
    monkeypatch.setattr(symdelta, "check_typescript_tooling", lambda repo, lang="typescript": (True, None))
    monkeypatch.setattr(
        symdelta, "run_ts_extractor",
        _raises(RuntimeError("LSP extractor failed on /tmp/x: python: 0 of 2 files yielded any symbols")),
    )
    repo, base, head = make_repo(
        tmp_path, {"a.py": "def a():\n    pass\n"}, {"a.py": "def a():\n    return 1\n"}
    )

    result = symdelta.analyse_python(repo, base, head)
    assert result == {
        "language": None,
        "reason": "LSP extractor failed on /tmp/x: python: 0 of 2 files yielded any symbols",
    }
    assert "remedy" not in result


def test_llm_head_edges_malformed_line_errors_with_the_line_number(tmp_path):
    edges_path = tmp_path / "bad.jsonl"
    edges_path.write_text('{"FromFile": "a.go", "FromSym": "A", "ToFile": "a.go"}\n')

    with pytest.raises(RuntimeError) as exc:
        symdelta._load_llm_edges(str(edges_path))
    assert f"{edges_path}:1:" in str(exc.value)


def test_llm_head_edges_produces_a_graph_with_llm_resolver(tmp_path):
    repo, base, head = make_repo(
        tmp_path,
        {"pkg/a.go": "package pkg\n\nfunc Caller() {}\n\nfunc Callee() {}\n"},
        {"pkg/a.go": "package pkg\n\nfunc Caller() {\n\tCallee()\n}\n\nfunc Callee() {}\n"},
    )
    edges_path = repo / "llm-edges.jsonl"
    edges_path.write_text(json.dumps({
        "FromFile": "pkg/a.go", "FromSym": "Caller", "ToFile": "pkg/a.go", "ToSym": "Callee",
    }) + "\n")

    result = symdelta.analyse(repo, base, head, llm_head_edges=str(edges_path))

    assert result["language"] == "go"  # detect_language still names the real language
    assert result["resolver"] == "llm"
    assert result["counts"]["edges_new"] == 1
    assert any(
        e["source"] == "pkg:Caller" and e["target"] == "pkg:Callee" for e in result["edges"]
    )
    # The LLM tier's wire shape is validated against _LLM_EDGE_FIELDS, which has no *Start/*End
    # keys at all -- position-less by schema, so its nodes must never carry a fabricated range.
    symbol_nodes = [n for n in result["nodes"] if n["kind"] == "symbol"]
    assert symbol_nodes  # sanity: this tier did produce symbol nodes
    assert all("range" not in n for n in symbol_nodes)


def test_llm_head_edges_falls_back_to_unknown_language_when_nothing_is_detected(tmp_path):
    # The whole point of this tier is that the mechanical one declined; detect_language finding
    # no supported extension at all must not refuse the LLM path too.
    repo, base, head = make_repo(tmp_path, {"readme.md": "hello\n"}, {"readme.md": "hello world\n"})
    edges_path = repo / "llm-edges.jsonl"
    edges_path.write_text(json.dumps({
        "FromFile": "a", "FromSym": "A", "ToFile": "b", "ToSym": "B",
    }) + "\n")

    result = symdelta.analyse(repo, base, head, llm_head_edges=str(edges_path))
    assert result["language"] == "unknown"
    assert result["resolver"] == "llm"


def test_end_to_end_go_repo(tmp_path):
    cw_testlib.require_go_extractor()
    # The vendored extractor (extractors/go/main.go) filters packages by the module path it
    # reads from go.mod, so an unrelated module name proves that filter isn't hardcoded to
    # this repo. asmFunc is an assembly-backed func with no Go body; extracting one used to call
    # ast.Inspect(nil, ...) and panic, aborting the whole extraction.
    repo, base, head = make_repo(
        tmp_path,
        {
            "go.mod": "module example.com/some-other-module\n\ngo 1.21\n",
            "pkg/a.go": "package pkg\n\nfunc Caller() {\n\tCallee()\n\tOldCallee()\n}\n\n"
                        "func Callee() {}\n\nfunc OldCallee() {}\n\n"
                        "func asmFunc() uint64\n",
            "pkg/a_amd64.s": '#include "textflag.h"\n\n'
                             "TEXT ·asmFunc(SB), NOSPLIT, $0-8\n\tMOVQ $42, ret+0(FP)\n\tRET\n",
        },
        {
            "pkg/a.go": "package pkg\n\nfunc Caller() {\n\tCallee()\n\tNewCallee()\n}\n\n"
                        "func Callee() {}\n\nfunc NewCallee() {}\n\n"
                        "func asmFunc() uint64\n",
        },
    )

    result = _run_symdelta(repo, base, head)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["language"] == "go"
    assert payload["counts"]["edges_new"] == 1
    assert payload["counts"]["edges_gone"] == 1
    assert any(
        e["source"] == "pkg:Caller" and e["target"] == "pkg:NewCallee" for e in payload["edges"]
    )
    nodes = {n["id"]: n for n in payload["nodes"] if n["kind"] == "symbol"}
    # Depends on the Go extractor emitting FromStart/FromEnd/ToStart/ToEnd on the wire; an
    # empty/absent "range" here means it isn't emitting positions, not a bug in this test.
    # Caller: "func Caller() {" on line 3 of the HEAD fixture above, closing "}" on line 6.
    assert nodes["pkg:Caller"]["range"] == [3, 6]
    # NewCallee: single-line "func NewCallee() {}" on line 10 of the HEAD fixture.
    assert nodes["pkg:NewCallee"]["range"] == [10, 10]
    # OldCallee only exists at BASE, on line 10 of the BASE fixture -- its range must be the
    # BASE-side declaration, not absent and not HEAD's (it has none at HEAD).
    assert nodes["pkg:OldCallee"]["state"] == "gone"
    assert nodes["pkg:OldCallee"]["range"] == [10, 10]
    _assert_no_worktree_leak(repo)


def test_end_to_end_go_repo_root_level_symbol_uses_the_root_sentinel(tmp_path):
    cw_testlib.require_go_extractor()
    repo, base, head = make_repo(
        tmp_path,
        {"go.mod": "module example.com/root-go\n\ngo 1.21\n",
         "main.go": "package main\n\nfunc main() {\n\tCallee()\n}\n\nfunc Callee() {}\n"},
        {"main.go": "package main\n\nfunc main() {\n\tCallee()\n\tNewCallee()\n}\n\n"
                    "func Callee() {}\n\nfunc NewCallee() {}\n"},
    )

    result = _run_symdelta(repo, base, head)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["language"] == "go"
    symbol_ids = {n["id"] for n in payload["nodes"] if n["kind"] == "symbol"}
    assert "(root):main.NewCallee" in symbol_ids
    assert not any(sid.startswith(":") for sid in symbol_ids)
    pkg_ids = {n["id"] for n in payload["nodes"] if n["kind"] == "pkg"}
    assert "(root)" in pkg_ids


# ---- pure-logic tests: add_worktree disables a hostile repo's hooks ---------------------


def test_add_worktree_does_not_run_a_tracked_post_checkout_hook(tmp_path):
    cw_testlib.require_git()
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    git(repo, "config", "core.hooksPath", ".githooks")
    marker = repo / "pwned"
    hook = repo / ".githooks" / "post-checkout"
    hook.parent.mkdir()
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    head = commit_all(repo, "head")

    worktree_path = tmp_path / "wt"
    symdelta.add_worktree(repo, head, worktree_path)
    try:
        assert not marker.exists()
    finally:
        symdelta.remove_worktree(repo, worktree_path)


def test_add_worktree_with_symlinks_false_checks_out_a_regular_file(tmp_path):
    cw_testlib.require_posix()
    cw_testlib.require_git()
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    write_file(repo, "target.txt", "payload\n")
    os.symlink("target.txt", repo / "link.txt")
    head = commit_all(repo, "head")

    worktree_path = tmp_path / "wt"
    symdelta.add_worktree(repo, head, worktree_path, symlinks=False)
    try:
        checked_out = worktree_path / "link.txt"
        assert not checked_out.is_symlink()
        assert checked_out.read_text() == "target.txt"
    finally:
        symdelta.remove_worktree(repo, worktree_path)
