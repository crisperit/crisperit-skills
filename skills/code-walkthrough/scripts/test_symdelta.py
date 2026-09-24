#!/usr/bin/env python3
"""Self-check for symdelta.py. Pure-logic tests exercise the delta arithmetic directly (no
git/go needed); the rest build a throwaway git repo under tempfile.TemporaryDirectory() and
leave nothing behind. The end-to-end Go test is skipped when `go` isn't on PATH."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "extractors" / "lsp"))
import symdelta  # noqa: E402
import lsp_client  # noqa: E402

SCRIPT_PATH = Path(__file__).parent / "symdelta.py"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Symdelta Test",
    "GIT_AUTHOR_EMAIL": "symdelta-test@example.com",
    "GIT_COMMITTER_NAME": "Symdelta Test",
    "GIT_COMMITTER_EMAIL": "symdelta-test@example.com",
}


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
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


def _run_symdelta(repo, base, head):
    cmd = [sys.executable, str(SCRIPT_PATH), "--repo", str(repo), "--base", base, "--head", head]
    return subprocess.run(cmd, capture_output=True, text=True)


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


# ---- pure-logic tests: rename-map parsing ----------------------------------------------


def test_brace_rename_with_empty_side_collapses_the_slash():
    summary = " rename corelib/ratelimit/{token_bucket => }/token_bucket.go (87%)\n"
    rename_map = symdelta.parse_rename_map(summary)
    assert rename_map == {"corelib/ratelimit/token_bucket.go": "corelib/ratelimit/token_bucket/token_bucket.go"}


def test_brace_rename_with_non_empty_side():
    summary = " rename corelib/ratelimit/{internal => ratelimit_config}/config.go (99%)\n"
    rename_map = symdelta.parse_rename_map(summary)
    assert rename_map == {
        "corelib/ratelimit/ratelimit_config/config.go": "corelib/ratelimit/internal/config.go"
    }


def test_plain_rename_with_no_common_prefix():
    summary = " rename a/foo.go => b/bar.go (65%)\n"
    rename_map = symdelta.parse_rename_map(summary)
    assert rename_map == {"b/bar.go": "a/foo.go"}


def test_non_rename_summary_lines_are_ignored():
    summary = " a/foo.go | 3 +--\n 1 file changed, 1 insertion(+), 2 deletions(-)\n"
    assert symdelta.parse_rename_map(summary) == {}


# ---- pure-logic tests: rename pairing + move collapse ----------------------------------


def _edge(from_file, from_sym, to_file, to_sym):
    return {"FromFile": from_file, "FromSym": from_sym, "ToFile": to_file, "ToSym": to_sym}


def test_compute_new_gone_canonicalises_renamed_head_paths():
    # Same call, file only renamed (no directory change): must not show as new+gone.
    base = [_edge("a/x.go", "Caller", "a/y.go", "Callee")]
    head = [_edge("a/x2.go", "Caller", "a/y.go", "Callee")]
    rename_map = {"a/x2.go": "a/x.go"}
    new, gone = symdelta.compute_new_gone(base, head, rename_map)
    assert new == [] and gone == []


def test_compute_new_gone_keeps_real_changes():
    base = [_edge("a/x.go", "Caller", "a/y.go", "Callee")]
    head = [_edge("a/x.go", "Caller", "a/z.go", "NewCallee")]
    new, gone = symdelta.compute_new_gone(base, head, {})
    assert new == [["a/x.go", "Caller", "a/z.go", "NewCallee"]]
    assert gone == [["a/x.go", "Caller", "a/y.go", "Callee"]]


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


def test_move_collapse_leaves_unrelated_bare_name_collisions_alone_when_no_match():
    # Different (FromSym, ToSym) pairs never collide even if the target symbol name repeats
    # across packages -- collapsing only ever fires on an exact (FromSym, ToSym) match.
    new = [["a/x.go", "Caller", "a/y.go", "String"]]
    gone = [["b/x.go", "OtherCaller", "b/y.go", "String"]]
    final_new, final_gone, moved, moved_pairs = symdelta.move_collapse(new, gone)
    assert final_new == new and final_gone == gone and moved == [] and moved_pairs == []


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


def test_no_dangling_edges_across_new_gone_and_changed_states():
    final_new = [
        ["a/x.go", "OnlyNew", "a/y.go", "Shared"],
        ["pkg/a.go", "Helper", "pkg/c.go", "NewTarget"],
    ]
    final_gone = [
        ["a/x.go", "OnlyGone", "a/y.go", "Shared"],
        ["pkg/internal/a.go", "Helper", "pkg/d.go", "OldTarget"],
    ]
    moved_pairs = [("pkg/internal:Helper", "pkg:Helper", "pkg/a.go")]
    nodes, edges, _, _ = symdelta.build_graph(final_new, final_gone, {}, moved_pairs)
    _assert_no_dangling_edges(nodes, edges)


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


def test_resolve_symbol_merges_case_insensitive_fallback_via_file_rename():
    # incrementChecksTotal (unexported, internal/) becomes IncrementChecksTotal (exported, moved
    # out of internal/) -- the dominant real case this fallback exists for.
    sym_gone = {"pkg/internal:incrementChecksTotal"}
    sym_new = {"pkg:IncrementChecksTotal", "pkg:NewCheck"}
    gone_file = {"pkg/internal:incrementChecksTotal": "pkg/internal/a.go"}
    gone_name = {"pkg/internal:incrementChecksTotal": "incrementChecksTotal"}
    rename_map = {"pkg/a.go": "pkg/internal/a.go"}

    merge_map, _, renamed_from = symdelta.resolve_symbol_merges(
        sym_gone, sym_new, gone_file, gone_name, rename_map, []
    )

    assert merge_map == {"pkg/internal:incrementChecksTotal": "pkg:IncrementChecksTotal"}
    assert renamed_from == {"pkg:IncrementChecksTotal": "incrementChecksTotal"}


def test_resolve_symbol_merges_case_insensitive_fallback_via_package_level_match():
    # gone_file isn't itself a file git recognised as renamed (source 2 has nothing to go on),
    # but every OTHER renamed file agrees pkg/internal -> pkg, so source 3's own fallback applies.
    sym_gone = {"pkg/internal:newFoo"}
    sym_new = {"pkg:NewFoo"}
    gone_file = {"pkg/internal:newFoo": "pkg/internal/unrelated.go"}
    gone_name = {"pkg/internal:newFoo": "newFoo"}
    rename_map = {"pkg/other.go": "pkg/internal/other.go"}

    merge_map, _, renamed_from = symdelta.resolve_symbol_merges(
        sym_gone, sym_new, gone_file, gone_name, rename_map, []
    )

    assert merge_map == {"pkg/internal:newFoo": "pkg:NewFoo"}
    assert renamed_from == {"pkg:NewFoo": "newFoo"}


def test_resolve_symbol_merges_ambiguous_case_match_falls_through_without_merging():
    # Two live candidates differ from "foobar" only by case -- picking either would be a guess,
    # so this must fall through to the exact-match-only path: build the (not actually live)
    # exact-name id rather than merge into one of the two real candidates.
    sym_gone = {"pkg/internal:foobar"}
    sym_new = {"pkg:fooBar", "pkg:FooBar"}
    gone_file = {"pkg/internal:foobar": "pkg/internal/a.go"}
    gone_name = {"pkg/internal:foobar": "foobar"}
    rename_map = {"pkg/a.go": "pkg/internal/a.go"}

    merge_map, _, renamed_from = symdelta.resolve_symbol_merges(
        sym_gone, sym_new, gone_file, gone_name, rename_map, []
    )

    assert merge_map == {"pkg/internal:foobar": "pkg:foobar"}
    assert renamed_from == {}


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


def test_build_graph_nodes_and_edges_carry_no_isTest_field():
    nodes, edges, _, _ = symdelta.build_graph(
        [["a/x_test.go", "TestFoo", "a/y.go", "Bar"]], []
    )
    assert all("isTest" not in n for n in nodes)
    assert all("isTest" not in e for e in edges)


# ---- pure-logic tests: pass-through package chains collapse into one box ----------------


def test_build_graph_collapses_a_two_deep_passthrough_chain_under_a_real_grandparent():
    # "src" has its own symbol ("Keep") plus one package child ("src/s2s"), so "src" itself
    # stays; "src/s2s" holds nothing but its own single package child ("src/s2s/handler") and
    # folds away. The survivor keeps its own (deepest) id, gets "src" -- the nearest surviving
    # ancestor -- as its parent, and a label that joins the elided segment back in.
    final_new = [["src/other.go", "Keep", "src/s2s/handler/handle.go", "Handle"]]
    nodes, edges, _, _ = symdelta.build_graph(final_new, [])
    pkg_nodes = {n["id"]: n for n in nodes if n["kind"] == "pkg"}
    assert set(pkg_nodes) == {"src", "src/s2s/handler"}
    assert pkg_nodes["src/s2s/handler"]["parent"] == "src"
    assert pkg_nodes["src/s2s/handler"]["label"] == "s2s/handler"


def test_build_graph_collapses_a_three_deep_chain_fully_into_one_box():
    # "auction" > "model" > "request" each hold nothing but the next single package -- the whole
    # chain must collapse into ONE box, not stop one level short.
    final_new = [["auction/model/request/a.go", "Fn", "auction/model/request/b.go", "Other"]]
    nodes, edges, _, _ = symdelta.build_graph(final_new, [])
    pkg_nodes = {n["id"]: n for n in nodes if n["kind"] == "pkg"}
    assert set(pkg_nodes) == {"auction/model/request"}
    assert pkg_nodes["auction/model/request"]["parent"] is None
    assert pkg_nodes["auction/model/request"]["label"] == "auction/model/request"
    assert pkg_nodes["auction/model/request"]["depth"] == 0


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
    final_new = [
        ["src/other.go", "Keep", "src/s2s/handler/handle.go", "Handle"],
        ["top/x.go", "TopFn", "top/y.go", "TopFn2"],
    ]
    nodes, edges, _, _ = symdelta.build_graph(final_new, [])
    by_id = {n["id"]: n for n in nodes}
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


def test_pkg_of_root_file_uses_sentinel_not_empty_string():
    assert symdelta.pkg_of("main.go") == "(root)"
    assert symdelta.pkg_of("index.ts") == "(root)"
    assert symdelta.pkg_of("a/b.go") == "a"


def test_sym_id_root_file_is_qualified_and_never_starts_with_a_bare_colon():
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


def test_build_graph_attaches_head_range_to_new_symbols():
    final_new = [["pkg/a.go", "Caller", "pkg/a.go", "Callee"]]
    head_ranges = {
        ("pkg/a.go", "pkg:Caller"): (3, 8),
        ("pkg/a.go", "pkg:Callee"): (10, 10),
    }
    nodes, _, _, _ = symdelta.build_graph(final_new, [], head_ranges=head_ranges)
    by_id = {n["id"]: n for n in nodes if n["kind"] == "symbol"}
    assert by_id["pkg:Caller"]["range"] == [3, 8]
    assert by_id["pkg:Callee"]["range"] == [10, 10]


def test_build_graph_attaches_base_range_to_gone_symbols():
    final_gone = [["pkg/a.go", "OldCaller", "pkg/a.go", "OldCallee"]]
    base_ranges = {
        ("pkg/a.go", "pkg:OldCaller"): (1, 4),
        ("pkg/a.go", "pkg:OldCallee"): (6, 6),
    }
    nodes, _, _, _ = symdelta.build_graph([], final_gone, base_ranges=base_ranges)
    by_id = {n["id"]: n for n in nodes if n["kind"] == "symbol"}
    assert by_id["pkg:OldCaller"]["range"] == [1, 4]
    assert by_id["pkg:OldCallee"]["range"] == [6, 6]


def test_build_graph_changed_symbol_uses_head_range_not_base():
    # Helper exists on both sides (state "changed") -- the surfaced range is HEAD's, since only
    # a "gone" node's range is BASE-side.
    final_new = [["pkg/a.go", "Helper", "pkg/b.go", "NewTarget"]]
    final_gone = [["pkg/a.go", "Helper", "pkg/c.go", "OldTarget"]]
    head_ranges = {("pkg/a.go", "pkg:Helper"): (20, 25)}
    base_ranges = {("pkg/a.go", "pkg:Helper"): (1, 5)}
    nodes, _, _, _ = symdelta.build_graph(
        final_new, final_gone, head_ranges=head_ranges, base_ranges=base_ranges
    )
    helper = next(n for n in nodes if n["id"] == "pkg:Helper")
    assert helper["state"] == "changed"
    assert helper["range"] == [20, 25]


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


def test_build_graph_symbol_with_no_range_entry_gets_no_range_key():
    nodes, _, _, _ = symdelta.build_graph([["pkg/a.go", "Caller", "pkg/a.go", "Callee"]], [])
    caller = next(n for n in nodes if n["id"] == "pkg:Caller")
    assert "range" not in caller


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


def test_resolve_cache_dir_uses_xdg_cache_home_when_set():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        result = symdelta._resolve_cache_dir({"XDG_CACHE_HOME": "/xdg/cache"}, home)

        assert result == Path("/xdg/cache") / "code-walkthrough"


def test_resolve_cache_dir_falls_back_to_dot_cache_when_xdg_unset():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)

        result = symdelta._resolve_cache_dir({}, home)

        assert result == home / ".cache" / "code-walkthrough"


def test_resolve_cache_dir_xdg_cache_home_wins_even_when_dot_cache_exists():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / ".cache").mkdir()

        result = symdelta._resolve_cache_dir({"XDG_CACHE_HOME": "/xdg/cache"}, home)

        assert result == Path("/xdg/cache") / "code-walkthrough"


def test_resolve_cache_dir_falls_back_to_tmp_when_dot_cache_is_not_writable():
    # The agent-sandbox case: ~/.cache is read-only, so the extractor would never build and the
    # whole symbols section would go missing over a cache directory.
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        (home / ".cache").mkdir(parents=True)
        (home / ".cache").chmod(0o555)
        try:
            result = symdelta._resolve_cache_dir({}, home, tmp="/scratch")
        finally:
            (home / ".cache").chmod(0o755)

        assert result == Path("/scratch") / "code-walkthrough"


def test_resolve_cache_dir_xdg_cache_home_wins_even_when_it_is_not_writable():
    # An explicit XDG_CACHE_HOME is a decision, not a guess; silently relocating it would hide
    # a misconfiguration behind a cache that keeps getting rebuilt.
    result = symdelta._resolve_cache_dir({"XDG_CACHE_HOME": "/proc"}, Path("/nonexistent"))

    assert result == Path("/proc") / "code-walkthrough"


# ---- pure-logic tests: detect_language's tie-break is honest and deterministic ----------


def test_detect_language_tie_break_is_honest_and_deterministic():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "go.mod", "module example.com/tie\n\ngo 1.21\n")
        # "aaa.ts" sorts before "zzz.go" in git's own diff listing, so an insertion-order tie
        # break would have picked typescript; the fix must be independent of that ordering.
        _write(repo, "aaa.ts", "export function a() {}\n")
        _write(repo, "zzz.go", "package main\n\nfunc Caller() {}\n")
        base = _commit(repo, "base")
        _write(repo, "aaa.ts", "export function a() { console.log(1); }\n")
        _write(repo, "zzz.go", "package main\n\nfunc Caller() {}\nfunc Other() {}\n")
        head = _commit(repo, "head")

        lang, reason = symdelta.detect_language(repo, base, head)
        assert lang == "go"
        assert "tied at 1 changed files each" in reason
        assert "not a real majority" in reason


def test_detect_language_routes_py_files_to_python():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.py", "def a():\n    pass\n")
        base = _commit(repo, "base")
        _write(repo, "a.py", "def a():\n    return 1\n")
        head = _commit(repo, "head")

        lang, reason = symdelta.detect_language(repo, base, head)
        assert lang == "python" and reason is None


def test_detect_language_works_against_an_orphan_baseline_with_no_merge_base():
    # Three-dot diff needs a merge base; an orphan baseline has none, so this used to fail with
    # "fatal: ...: no merge base" before detect_language switched to two-dot on a resolved base.
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.py", "def a():\n    pass\n")
        head = _commit(repo, "head")
        orphan = _orphan_baseline(repo)

        lang, reason = symdelta.detect_language(repo, orphan, head)
        assert lang == "python" and reason is None


def test_detect_language_routes_rs_files_to_rust():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.rs", "fn a() {}\n")
        base = _commit(repo, "base")
        _write(repo, "a.rs", "fn a() { let _ = 1; }\n")
        head = _commit(repo, "head")

        lang, reason = symdelta.detect_language(repo, base, head)
        assert lang == "rust" and reason is None


def test_detect_language_tie_break_between_python_and_rust_is_alphabetical():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.py", "def a():\n    pass\n")
        _write(repo, "a.rs", "fn a() {}\n")
        base = _commit(repo, "base")
        _write(repo, "a.py", "def a():\n    return 1\n")
        _write(repo, "a.rs", "fn a() { let _ = 1; }\n")
        head = _commit(repo, "head")

        lang, reason = symdelta.detect_language(repo, base, head)
        assert lang == "python"
        assert "tied at 1 changed files each" in reason


def test_lsp_language_tables_all_cover_typescript_python_and_rust():
    # The four per-language dispatch tables symdelta.py's LSP tier relies on must all name the
    # same three languages, or a newly-added one would silently fall through in one but not
    # another (e.g. routed by LANG_EXTENSIONS but with no ANALYSERS entry).
    for lang in ("typescript", "python", "rust"):
        assert lang in symdelta.DEPENDENCY_FILES_BY_LANG
        assert lang in symdelta.DEPENDENCY_REASON
        assert lang in symdelta.WORKTREE_PREP
        assert lang in symdelta.ANALYSERS
    assert set(symdelta.LANG_EXTENSIONS.values()) >= {"typescript", "python", "rust", "go"}


# ---- pure-logic + wiring tests: refuse a TS diff when node_modules would lie ------------


def test_dependency_files_changed_true_when_package_json_changed():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"version": "1.0.0"}\n')
        base = _commit(repo, "base")
        _write(repo, "package.json", '{"version": "1.0.1"}\n')
        head = _commit(repo, "head")
        assert symdelta.dependency_files_changed(repo, base, head) is True


def test_dependency_files_changed_false_for_an_unrelated_change():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "src/a.ts", "export function a() {}\n")
        _write(repo, "package.json", '{"version": "1.0.0"}\n')
        base = _commit(repo, "base")
        _write(repo, "src/a.ts", "export function a() { return 1; }\n")
        head = _commit(repo, "head")
        assert symdelta.dependency_files_changed(repo, base, head) is False


def test_dependency_files_changed_true_for_yarn_lock():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "yarn.lock", "# yarn lockfile v1\n")
        base = _commit(repo, "base")
        _write(repo, "yarn.lock", "# yarn lockfile v1\nleft-pad@1.0.0:\n")
        head = _commit(repo, "head")
        assert symdelta.dependency_files_changed(repo, base, head) is True


def test_dependency_files_changed_true_for_pnpm_lock():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "pnpm-lock.yaml", "lockfileVersion: '6.0'\n")
        base = _commit(repo, "base")
        _write(repo, "pnpm-lock.yaml", "lockfileVersion: '6.0'\ndependencies: {}\n")
        head = _commit(repo, "head")
        assert symdelta.dependency_files_changed(repo, base, head) is True


def test_dependency_files_changed_true_for_pyproject_toml():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "pyproject.toml", "[project]\nname = \"x\"\nversion = \"0.1.0\"\n")
        base = _commit(repo, "base")
        _write(repo, "pyproject.toml", "[project]\nname = \"x\"\nversion = \"0.2.0\"\n")
        head = _commit(repo, "head")
        assert symdelta.dependency_files_changed(repo, base, head, "python") is True


def test_dependency_files_changed_false_for_python_when_only_ts_lockfile_changed():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"version": "1.0.0"}\n')
        _write(repo, "a.py", "def a():\n    pass\n")
        base = _commit(repo, "base")
        _write(repo, "package.json", '{"version": "1.0.1"}\n')
        head = _commit(repo, "head")
        assert symdelta.dependency_files_changed(repo, base, head, "python") is False


def test_dependency_files_changed_true_for_cargo_lock():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "Cargo.lock", "# auto-generated\nversion = 3\n")
        base = _commit(repo, "base")
        _write(repo, "Cargo.lock", "# auto-generated\nversion = 4\n")
        head = _commit(repo, "head")
        assert symdelta.dependency_files_changed(repo, base, head, "rust") is True


# ---- pure-logic tests: TypeScript dependency compatibility decided from declared specs -----


def test_ts_dependency_incompatible_false_for_additions_only():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"dependencies": {"left-pad": "1.0.0"}}\n')
        base = _commit(repo, "base")
        _write(repo, "package.json", '{"dependencies": {"left-pad": "1.0.0", "chalk": "5.0.0"}}\n')
        head = _commit(repo, "head")
        assert symdelta.ts_dependency_incompatible(repo, base, head) == (False, None)


def test_ts_dependency_incompatible_true_when_a_dependency_is_removed():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"dependencies": {"left-pad": "1.0.0"}}\n')
        base = _commit(repo, "base")
        _write(repo, "package.json", '{"dependencies": {}}\n')
        head = _commit(repo, "head")
        incompatible, reason = symdelta.ts_dependency_incompatible(repo, base, head)
        assert incompatible is True
        assert "removed: left-pad" in reason


def test_ts_dependency_incompatible_true_when_a_version_spec_changed():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"dependencies": {"left-pad": "1.0.0"}}\n')
        base = _commit(repo, "base")
        _write(repo, "package.json", '{"dependencies": {"left-pad": "2.0.0"}}\n')
        head = _commit(repo, "head")
        incompatible, reason = symdelta.ts_dependency_incompatible(repo, base, head)
        assert incompatible is True
        assert "version changed: left-pad" in reason


def test_ts_dependency_incompatible_false_for_lockfile_only_change():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"dependencies": {"left-pad": "1.0.0"}}\n')
        _write(repo, "package-lock.json", '{"lockfileVersion": 2}\n')
        base = _commit(repo, "base")
        _write(repo, "package-lock.json", '{"lockfileVersion": 3}\n')
        head = _commit(repo, "head")
        assert symdelta.ts_dependency_incompatible(repo, base, head) == (False, None)


def test_ts_dependency_incompatible_true_when_package_json_missing_at_base():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "src/a.ts", "export function a() {}\n")
        base = _commit(repo, "base")
        _write(repo, "package.json", '{"dependencies": {"left-pad": "1.0.0"}}\n')
        head = _commit(repo, "head")
        incompatible, reason = symdelta.ts_dependency_incompatible(repo, base, head)
        assert incompatible is True
        assert "missing or unparseable" in reason


def test_ts_dependency_incompatible_true_when_package_json_is_unparseable():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"dependencies": {"left-pad": "1.0.0"}}\n')
        base = _commit(repo, "base")
        _write(repo, "package.json", "{not valid json\n")
        head = _commit(repo, "head")
        incompatible, reason = symdelta.ts_dependency_incompatible(repo, base, head)
        assert incompatible is True
        assert "missing or unparseable" in reason


def test_ts_dependency_incompatible_true_when_sections_disagree_on_spec():
    # A dict.update() merge across sections would let devDependencies silently overwrite
    # dependencies' newer spec here, hiding a real version bump behind section iteration order.
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"dependencies": {"left-pad": "1.0.0"}}\n')
        base = _commit(repo, "base")
        _write(
            repo, "package.json",
            '{"dependencies": {"left-pad": "2.0.0"}, "devDependencies": {"left-pad": "1.0.0"}}\n',
        )
        head = _commit(repo, "head")
        incompatible, reason = symdelta.ts_dependency_incompatible(repo, base, head)
        assert incompatible is True
        assert "version changed: left-pad" in reason


def test_ts_dependency_incompatible_false_when_same_spec_appears_in_two_sections():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"dependencies": {"left-pad": "1.0.0"}}\n')
        base = _commit(repo, "base")
        _write(
            repo, "package.json",
            '{"dependencies": {"left-pad": "1.0.0"}, "devDependencies": {"left-pad": "1.0.0"}}\n',
        )
        head = _commit(repo, "head")
        assert symdelta.ts_dependency_incompatible(repo, base, head) == (False, None)


# ---- wiring tests: link_node_modules must not symlink a relative target --------------------


def test_link_node_modules_resolves_a_relative_repo_path():
    # A relative repo, embedded verbatim in the symlink target, would point the worktree's
    # node_modules at itself instead of the real one -- worktree and repo live in different dirs.
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        (repo / "node_modules").mkdir(parents=True)
        worktree = Path(tmp) / "worktree"
        worktree.mkdir()

        cwd = os.getcwd()
        os.chdir(repo)
        try:
            symdelta.link_node_modules(".", worktree)
        finally:
            os.chdir(cwd)

        dst = worktree / "node_modules"
        assert dst.exists()
        assert os.path.isabs(os.readlink(dst))


# ---- wiring tests: node_modules coverage preflight, before any worktree gets created -------


def test_check_node_modules_coverage_true_when_every_dependency_resolves():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"dependencies": {"left-pad": "1.0.0"}}\n')
        head = _commit(repo, "head")
        (repo / "node_modules" / "left-pad").mkdir(parents=True)

        assert symdelta.check_node_modules_coverage(repo, head) == (True, None)


def test_check_node_modules_coverage_true_when_node_modules_missing_and_nothing_declared():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "src/a.ts", "export function a() {}\n")
        head = _commit(repo, "head")

        assert symdelta.check_node_modules_coverage(repo, head) == (True, None)


def test_check_node_modules_coverage_catches_a_missing_scoped_package():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(
            repo, "package.json",
            '{"dependencies": {"@restatedev/restate-sdk": "1.0.0"}}\n',
        )
        head = _commit(repo, "head")
        # node_modules exists but never got the newly-declared scoped package -- the stale
        # node_modules case this preflight exists to catch.
        (repo / "node_modules").mkdir()

        ok, reason = symdelta.check_node_modules_coverage(repo, head)
        assert ok is False
        assert "@restatedev/restate-sdk" in reason


def test_check_node_modules_coverage_caps_the_reason_at_five_names():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        deps = {f"pkg-{i}": "1.0.0" for i in range(7)}
        _write(repo, "package.json", json.dumps({"dependencies": deps}) + "\n")
        head = _commit(repo, "head")
        (repo / "node_modules").mkdir()

        ok, reason = symdelta.check_node_modules_coverage(repo, head)
        assert ok is False
        assert "and 2 more" in reason


def test_check_node_modules_coverage_catches_a_missing_peer_dependency():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"peerDependencies": {"react": "18.0.0"}}\n')
        head = _commit(repo, "head")
        (repo / "node_modules").mkdir()

        ok, reason = symdelta.check_node_modules_coverage(repo, head)
        assert ok is False
        assert "react" in reason


def test_check_node_modules_coverage_ignores_a_missing_optional_dependency():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"optionalDependencies": {"fsevents": "2.0.0"}}\n')
        head = _commit(repo, "head")
        (repo / "node_modules").mkdir()

        assert symdelta.check_node_modules_coverage(repo, head) == (True, None)


def test_check_node_modules_coverage_true_when_installed_version_matches_lockfile():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"dependencies": {"left-pad": "^1.0.0"}}\n')
        _write(
            repo, "package-lock.json",
            json.dumps({"packages": {"node_modules/left-pad": {"version": "1.3.0"}}}) + "\n",
        )
        head = _commit(repo, "head")
        _write(repo, "node_modules/left-pad/package.json", '{"version": "1.3.0"}\n')

        assert symdelta.check_node_modules_coverage(repo, head) == (True, None)


def test_check_node_modules_coverage_catches_a_stale_installed_version():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"dependencies": {"left-pad": "^2.0.0"}}\n')
        _write(
            repo, "package-lock.json",
            json.dumps({"packages": {"node_modules/left-pad": {"version": "2.0.0"}}}) + "\n",
        )
        head = _commit(repo, "head")
        # node_modules/left-pad exists (satisfies the bare presence check) but is still at the
        # version the lockfile no longer resolves to.
        _write(repo, "node_modules/left-pad/package.json", '{"version": "1.0.0"}\n')

        ok, reason = symdelta.check_node_modules_coverage(repo, head)
        assert ok is False
        assert "left-pad" in reason


def test_check_node_modules_coverage_presence_only_when_no_lockfile():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"dependencies": {"left-pad": "^2.0.0"}}\n')
        head = _commit(repo, "head")
        # present, but at a version a lockfile (if one existed) might disagree with -- with no
        # lockfile to compare against at all, presence is all this preflight can check.
        _write(repo, "node_modules/left-pad/package.json", '{"version": "1.0.0"}\n')

        assert symdelta.check_node_modules_coverage(repo, head) == (True, None)


def test_check_node_modules_coverage_presence_only_when_lockfile_is_unparseable():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"dependencies": {"left-pad": "^2.0.0"}}\n')
        _write(repo, "package-lock.json", "{not valid json\n")
        head = _commit(repo, "head")
        _write(repo, "node_modules/left-pad/package.json", '{"version": "1.0.0"}\n')

        assert symdelta.check_node_modules_coverage(repo, head) == (True, None)


def test_check_node_modules_coverage_handles_lockfile_version_1_shape():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"dependencies": {"left-pad": "^2.0.0"}}\n')
        _write(
            repo, "package-lock.json",
            json.dumps(
                {"lockfileVersion": 1, "dependencies": {"left-pad": {"version": "1.0.0"}}}
            ) + "\n",
        )
        head = _commit(repo, "head")
        _write(repo, "node_modules/left-pad/package.json", '{"version": "2.0.0"}\n')

        ok, reason = symdelta.check_node_modules_coverage(repo, head)
        assert ok is False
        assert "left-pad" in reason


# ---- wiring tests: analyse_typescript's null results carry the right remedy (or none) -----


def test_analyse_typescript_refuses_when_a_dependency_is_removed():
    # Must short-circuit before any typescript-language-server check, so this needs no tool on
    # PATH and is never skipped.
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"dependencies": {"left-pad": "1.0.0"}}\n')
        _write(repo, "src/a.ts", "export function a() {}\n")
        base = _commit(repo, "base")
        _write(repo, "package.json", '{"dependencies": {}}\n')
        _write(repo, "src/a.ts", "export function a() { return 1; }\n")
        head = _commit(repo, "head")

        result = symdelta.analyse_typescript(repo, base, head)
        assert result["language"] is None
        assert "left-pad" in result["reason"]
        assert "remedy" not in result


def test_analyse_typescript_does_not_refuse_on_dependency_additions_only():
    # Change 1's whole point: an added-only dependency must not trip the gate that a removed or
    # changed one does. Whatever happens next needs a real typescript-language-server, so this
    # only pins down that the dependency reason specifically is never given.
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"dependencies": {}}\n')
        _write(repo, "src/a.ts", "export function a() {}\n")
        base = _commit(repo, "base")
        _write(repo, "package.json", '{"dependencies": {"left-pad": "1.0.0"}}\n')
        _write(repo, "src/a.ts", "export function a() { return 1; }\n")
        head = _commit(repo, "head")
        (repo / "node_modules" / "left-pad").mkdir(parents=True)

        result = symdelta.analyse_typescript(repo, base, head)
        if result["language"] is None:
            assert "dependency incompatibility" not in result["reason"]


def test_analyse_typescript_refuses_with_npm_ci_remedy_when_node_modules_is_stale():
    # Also needs no tool on PATH: the node_modules preflight runs before check_typescript_tooling.
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"dependencies": {}}\n')
        _write(repo, "src/a.ts", "export function a() {}\n")
        base = _commit(repo, "base")
        _write(repo, "package.json", '{"dependencies": {"left-pad": "1.0.0"}}\n')
        _write(repo, "src/a.ts", "export function a() { return 1; }\n")
        head = _commit(repo, "head")
        (repo / "node_modules").mkdir()  # exists, but never got left-pad

        result = symdelta.analyse_typescript(repo, base, head)
        assert result["language"] is None
        assert "left-pad" in result["reason"]
        assert result["remedy"] == "npm ci"


def test_analyse_typescript_refuses_with_npm_ci_remedy_when_installed_version_is_stale():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "package.json", '{"dependencies": {}}\n')
        _write(repo, "src/a.ts", "export function a() {}\n")
        base = _commit(repo, "base")
        _write(repo, "package.json", '{"dependencies": {"left-pad": "^2.0.0"}}\n')
        _write(repo, "src/a.ts", "export function a() { return 1; }\n")
        _write(
            repo, "package-lock.json",
            json.dumps({"packages": {"node_modules/left-pad": {"version": "2.0.0"}}}) + "\n",
        )
        head = _commit(repo, "head")
        _write(repo, "node_modules/left-pad/package.json", '{"version": "1.0.0"}\n')

        result = symdelta.analyse_typescript(repo, base, head)
        assert result["language"] is None
        assert "left-pad" in result["reason"]
        assert result["remedy"] == "npm ci"


def test_analyse_lsp_attaches_language_server_remedy_when_tooling_check_fails():
    original_check = symdelta.check_typescript_tooling
    symdelta.check_typescript_tooling = lambda repo, lang="typescript": (False, "boom")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            _write(repo, "a.py", "def a():\n    pass\n")
            base = _commit(repo, "base")
            _write(repo, "a.py", "def a():\n    return 1\n")
            head = _commit(repo, "head")

            result = symdelta.analyse_python(repo, base, head)
            assert result == {
                "language": None,
                "reason": "boom",
                "remedy": symdelta.LANGUAGE_SERVER_REMEDY["python"],
            }
    finally:
        symdelta.check_typescript_tooling = original_check


def test_split_path_trap_pulls_the_remedy_out_of_the_reason():
    # The remedy must not stay inside `reason` too -- callers show both fields together, and
    # leaving it in both places would print the same `export PATH=...` line twice.
    reason = ('pyright-langserver is installed in /opt/npm/bin but that directory is not on '
              'PATH; add it, e.g. export PATH="/opt/npm/bin:$PATH"')
    trimmed, remedy = symdelta._split_path_trap(reason)
    assert trimmed == 'pyright-langserver is installed in /opt/npm/bin but that directory is not on PATH'
    assert remedy == 'export PATH="/opt/npm/bin:$PATH"'


def test_split_path_trap_returns_the_reason_unchanged_with_no_remedy_for_a_plain_reason():
    reason = "pyright-langserver not found on PATH"
    assert symdelta._split_path_trap(reason) == (reason, None)


def test_analyse_lsp_uses_the_path_trap_remedy_instead_of_a_reinstall():
    # LANGUAGE_SERVER_REMEDY's `npm i -g` would be a no-op on a binary that's already installed;
    # a PATH-trap reason carries its own fix, and that must win, with the fix pulled out of the
    # reason so it isn't shown twice.
    path_trap_reason = ('pyright-langserver is installed in /opt/npm/bin but that directory is '
                         'not on PATH; add it, e.g. export PATH="/opt/npm/bin:$PATH"')
    original_check = symdelta.check_typescript_tooling
    symdelta.check_typescript_tooling = lambda repo, lang="typescript": (False, path_trap_reason)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            _write(repo, "a.py", "def a():\n    pass\n")
            base = _commit(repo, "base")
            _write(repo, "a.py", "def a():\n    return 1\n")
            head = _commit(repo, "head")

            result = symdelta.analyse_python(repo, base, head)
            assert result == {
                "language": None,
                "reason": 'pyright-langserver is installed in /opt/npm/bin but that directory '
                          'is not on PATH',
                "remedy": 'export PATH="/opt/npm/bin:$PATH"',
            }
    finally:
        symdelta.check_typescript_tooling = original_check


# ---- --doctor: no diff, base or head needed ---------------------------------------------


def test_doctor_report_is_ok_for_every_language_when_everything_checks_out():
    original_which = symdelta.shutil.which
    original_build = symdelta.build_extractor
    original_check = symdelta.check_language_server
    symdelta.shutil.which = lambda tool: "/usr/bin/go" if tool == "go" else None
    symdelta.build_extractor = lambda: None
    symdelta.check_language_server = lambda repo, lang: (True, None)
    try:
        report = symdelta.doctor_report()
    finally:
        symdelta.shutil.which = original_which
        symdelta.build_extractor = original_build
        symdelta.check_language_server = original_check

    lines = report.splitlines()
    assert lines[0] == "go: toolchain OK, extractor builds"
    assert any(line.startswith("typescript: OK") for line in lines)
    assert any(line.startswith("python: OK") for line in lines)
    assert any(line.startswith("rust: OK") for line in lines)


def test_doctor_report_names_the_go_toolchain_as_missing():
    original_which = symdelta.shutil.which
    symdelta.shutil.which = lambda tool: None
    try:
        report = symdelta.doctor_report()
    finally:
        symdelta.shutil.which = original_which

    assert report.splitlines()[0] == "go: toolchain not found on PATH"


def test_doctor_report_prefers_the_path_trap_remedy_over_language_server_remedy():
    original_which = symdelta.shutil.which
    original_check = symdelta.check_language_server
    symdelta.shutil.which = lambda tool: None

    def fake_check(repo, lang):
        if lang == "typescript":
            return False, ('typescript-language-server is installed in /opt/npm/bin but that '
                            'directory is not on PATH; add it, e.g. '
                            'export PATH="/opt/npm/bin:$PATH"')
        return False, f"{lang} tool not found on PATH"

    symdelta.check_language_server = fake_check
    try:
        report = symdelta.doctor_report()
    finally:
        symdelta.shutil.which = original_which
        symdelta.check_language_server = original_check

    by_lang = {line.split(":", 1)[0]: line for line in report.splitlines()}
    assert 'export PATH="/opt/npm/bin:$PATH"' in by_lang["typescript"]
    # And named exactly once -- check_language_server's reason already carries the fix, so
    # printing the split-out remedy too must not repeat that same PATH line.
    assert by_lang["typescript"].count('export PATH="/opt/npm/bin:$PATH"') == 1
    assert symdelta.LANGUAGE_SERVER_REMEDY["python"] in by_lang["python"]


def test_doctor_report_survives_a_present_but_broken_binary():
    # shutil.which finds it, but spawning it still raises OSError (e.g. not executable) --
    # check_language_server only guards the initialize round-trip, not the spawn itself, so
    # doctor_report must catch this rather than losing the whole report to one bad language.
    original_which = symdelta.shutil.which
    original_check = symdelta.check_language_server
    symdelta.shutil.which = lambda tool: None

    def fake_check(repo, lang):
        if lang == "typescript":
            raise OSError("Exec format error")
        return True, None

    symdelta.check_language_server = fake_check
    try:
        report = symdelta.doctor_report()
    finally:
        symdelta.shutil.which = original_which
        symdelta.check_language_server = original_check

    by_lang = {line.split(":", 1)[0]: line for line in report.splitlines()}
    assert "failed to start" in by_lang["typescript"]
    assert by_lang["python"].strip().endswith("OK (pyright-langserver advertises callHierarchy)")


def test_main_doctor_flag_needs_no_repo_base_or_head():
    original_argv = sys.argv
    original_doctor_report = symdelta.doctor_report
    symdelta.doctor_report = lambda: "stubbed doctor output"
    sys.argv = ["symdelta.py", "--doctor"]
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = symdelta.main()
    finally:
        sys.argv = original_argv
        symdelta.doctor_report = original_doctor_report

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


def test_check_typescript_tooling_raises_runtime_error_on_timeout():
    original_popen = symdelta.subprocess.Popen
    symdelta.subprocess.Popen = lambda cmd, **kwargs: _FakeTimingOutProc(cmd)
    try:
        try:
            symdelta.check_typescript_tooling("/tmp/whatever")
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "60" in str(exc)
    finally:
        symdelta.subprocess.Popen = original_popen


def test_run_ts_extractor_raises_runtime_error_on_timeout_and_still_cleans_up_the_files_list():
    original_popen = symdelta.subprocess.Popen
    seen_path = {}

    def fake_popen(cmd, **kwargs):
        seen_path["path"] = cmd[-1]
        return _FakeTimingOutProc(cmd)

    symdelta.subprocess.Popen = fake_popen
    try:
        try:
            symdelta.run_ts_extractor("/tmp/worktree", ["a.ts"])
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "600" in str(exc)
        assert not os.path.exists(seen_path["path"])
    finally:
        symdelta.subprocess.Popen = original_popen


def test_run_in_process_group_handles_getpgid_race_without_crashing():
    # ProcessLookupError here means only the leader died, not the group -- hangs_after_kill=True
    # simulates a surviving descendant (tsserver) still holding the pipes open, so this also
    # covers the follow-up communicate() staying bounded instead of hanging.
    original_popen = symdelta.subprocess.Popen
    original_getpgid = symdelta.os.getpgid
    symdelta.subprocess.Popen = lambda cmd, **kwargs: _FakeTimingOutProc(cmd, hangs_after_kill=True)

    def fake_getpgid(pid):
        raise ProcessLookupError()

    symdelta.os.getpgid = fake_getpgid
    try:
        try:
            symdelta.run_in_process_group(["ignored"], timeout=1, what="test command")
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "test command" in str(exc) and "1" in str(exc)
    finally:
        symdelta.subprocess.Popen = original_popen
        symdelta.os.getpgid = original_getpgid


def test_run_in_process_group_kills_the_whole_group_not_just_the_direct_child():
    # A hung extract.py that spawned its own child (tsserver, in production) must not leave that
    # child orphaned when the outer timeout fires. This spawns a real grandchild process that
    # outlives a plain SIGKILL of the direct child, then asserts killpg took it down too.
    if shutil.which("sleep") is None:
        print(
            "skip (no `sleep` on PATH): "
            "test_run_in_process_group_kills_the_whole_group_not_just_the_direct_child"
        )
        return

    with tempfile.TemporaryDirectory() as tmp:
        pidfile = Path(tmp) / "child.pid"
        script = Path(tmp) / "spawn_and_hang.py"
        script.write_text(
            "import subprocess, sys, time\n"
            "p = subprocess.Popen(['sleep', '100'])\n"
            f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
            "time.sleep(100)\n"
        )

        try:
            symdelta.run_in_process_group(
                [sys.executable, str(script)], timeout=2, what="test command"
            )
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "test command" in str(exc) and "2" in str(exc)

        time.sleep(1)  # let the killed grandchild actually get reaped
        child_pid = int(pidfile.read_text().strip())
        try:
            os.kill(child_pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        assert not alive, "the sleep grandchild survived -- only the direct child was killed"


def test_build_extractor_raises_runtime_error_on_timeout():
    original_run = symdelta.subprocess.run
    original_cache_dir = symdelta.CACHE_DIR
    original_bin = symdelta.EXTRACTOR_BIN

    def fake_run(*args, **kwargs):
        raise symdelta.subprocess.TimeoutExpired(cmd="go build", timeout=600)

    with tempfile.TemporaryDirectory() as tmp:
        # a fresh, nonexistent EXTRACTOR_BIN forces build_extractor past its "reuse an existing
        # binary" short-circuit and into the subprocess.run call this test targets
        symdelta.CACHE_DIR = Path(tmp) / "cache"
        symdelta.EXTRACTOR_BIN = symdelta.CACHE_DIR / "symdelta-go-extractor"
        symdelta.subprocess.run = fake_run
        try:
            try:
                symdelta.build_extractor()
                assert False, "expected RuntimeError"
            except RuntimeError as exc:
                assert "600" in str(exc)
        finally:
            symdelta.subprocess.run = original_run
            symdelta.CACHE_DIR = original_cache_dir
            symdelta.EXTRACTOR_BIN = original_bin


def test_run_extractor_raises_runtime_error_on_timeout():
    original_run = symdelta.subprocess.run

    def fake_run(*args, **kwargs):
        raise symdelta.subprocess.TimeoutExpired(cmd="extractor", timeout=600)

    symdelta.subprocess.run = fake_run
    try:
        try:
            symdelta.run_extractor("/tmp/some-worktree")
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "600" in str(exc) and "/tmp/some-worktree" in str(exc)
    finally:
        symdelta.subprocess.run = original_run


def test_run_extractor_omits_goflags_when_go_work_exists_and_sets_it_otherwise():
    # -mod=mod is illegal once Go is in workspace mode ("go: -mod may only be set to readonly
    # or vendor when in workspace mode"), so this must track go.work's presence, not be constant.
    original_run = symdelta.subprocess.run
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    symdelta.subprocess.run = fake_run
    try:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            (worktree / "go.work").write_text("go 1.21\n\nuse ./a\n")
            symdelta.run_extractor(worktree)
            assert "GOFLAGS" not in captured["env"]

        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            symdelta.run_extractor(worktree)
            assert captured["env"]["GOFLAGS"] == "-mod=mod"
    finally:
        symdelta.subprocess.run = original_run


def test_lsp_client_shutdown_swallows_timeout_from_an_unreapable_process():
    # A process that survives SIGKILL must not raise out of shutdown() -- it's always called
    # from a `finally` in extract.py, so an unguarded wait() there would shadow whatever
    # original exception was propagating.
    client = lsp_client.LSPClient.__new__(lsp_client.LSPClient)
    client.request = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no real server"))
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


def test_no_supported_files_short_circuits_with_null_language():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "readme.md", "hello\n")
        base = _commit(repo, "base")
        _write(repo, "readme.md", "hello world\n")
        head = _commit(repo, "head")

        result = _run_symdelta(repo, base, head)

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload == {
            "language": None,
            "reason": f"no supported files (.go, .py, .rs, .ts, .tsx) changed between {base} and {head}",
        }


def test_detect_language_picks_the_majority_on_a_mixed_diff():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "go.mod", "module example.com/mixed\n\ngo 1.21\n")
        _write(repo, "pkg/a.go", "package pkg\n\nfunc Caller() {}\n")
        _write(repo, "src/a.ts", "export function a() {}\n")
        base = _commit(repo, "base")
        _write(repo, "pkg/a.go", "package pkg\n\nfunc Caller() {}\nfunc Other() {}\n")
        _write(repo, "src/a.ts", "export function a() { console.log(1); }\n")
        _write(repo, "src/b.ts", "export function b() {}\n")
        head = _commit(repo, "head")

        lang, reason = symdelta.detect_language(repo, base, head)
        assert lang == "typescript"
        assert "2 typescript" in reason and "1 go" in reason


def test_ts_files_by_side_excludes_added_from_base_and_deleted_from_head():
    entries = [
        ("A", "src/new.ts", "src/new.ts"),
        ("D", "src/old.ts", "src/old.ts"),
        ("M", "src/both.ts", "src/both.ts"),
        ("R100", "src/before.ts", "src/after.ts"),
        ("M", "readme.md", "readme.md"),
    ]
    base_files, head_files = symdelta.ts_files_by_side(entries)
    assert sorted(base_files) == ["src/before.ts", "src/both.ts", "src/old.ts"]
    assert sorted(head_files) == ["src/after.ts", "src/both.ts", "src/new.ts"]


def test_ts_files_by_side_filters_by_given_extensions():
    entries = [
        ("A", "src/new.py", "src/new.py"),
        ("D", "src/old.rs", "src/old.rs"),
        ("M", "src/both.ts", "src/both.ts"),
    ]
    base_files, head_files = symdelta.ts_files_by_side(entries, (".py",))
    assert base_files == [] and head_files == ["src/new.py"]

    base_files, head_files = symdelta.ts_files_by_side(entries, (".rs",))
    assert base_files == ["src/old.rs"] and head_files == []


def test_bad_ref_fails_with_nonzero_exit():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "readme.md", "hello\n")
        _commit(repo, "base")

        result = _run_symdelta(repo, "not-a-real-ref", "HEAD")

        assert result.returncode == 1
        assert "bad ref" in result.stderr


def test_analyse_does_not_crash_on_an_orphan_baseline():
    # End-to-end version of the orphan-baseline bug above: it used to fail every git-diff call
    # in analyse()'s pipeline with a three-dot "no merge base" error. A missing language-server
    # tool is a legitimate, separate reason to bail out; a merge-base crash is not.
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.py", "def a():\n    pass\n")
        head = _commit(repo, "head")
        orphan = _orphan_baseline(repo)

        result = _run_symdelta(repo, orphan, head)

        assert result.returncode == 0, result.stderr
        assert "no merge base" not in result.stderr
        payload = json.loads(result.stdout)
        assert "language" in payload


def test_is_empty_base_matches_the_baseline_by_tree_not_by_commit():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.py", "def a():\n    pass\n")
        head = _commit(repo, "head")

        assert symdelta.is_empty_base(repo, _orphan_baseline(repo))
        assert not symdelta.is_empty_base(repo, head)


def test_the_empty_baseline_gets_no_worktree_and_no_extractor_run():
    # A checkout of the empty tree has no go.mod, no package.json and no source, so every
    # extractor used to fail on it and take the whole symbols section down with it.
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.py", "def a():\n    pass\n")
        head = _commit(repo, "head")
        orphan = _orphan_baseline(repo)
        seen = []

        def base_extractor(path):
            seen.append(path)
            raise AssertionError("the base extractor must not run against the empty baseline")

        def head_extractor(path):
            assert (path / "a.py").exists()
            return ["edge"]

        base_edges, head_edges = symdelta._run_in_worktrees(
            repo, orphan, head, base_extractor, head_extractor
        )

        assert seen == []
        assert base_edges == []
        assert head_edges == ["edge"]


def test_a_real_base_still_gets_its_own_worktree():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.py", "def a():\n    pass\n")
        base = _commit(repo, "base")
        _write(repo, "b.py", "def b():\n    pass\n")
        head = _commit(repo, "head")

        both = symdelta._run_in_worktrees(
            repo, base, head,
            lambda p: [(p / "a.py").exists(), (p / "b.py").exists()],
            lambda p: [(p / "a.py").exists(), (p / "b.py").exists()],
        )

        assert both == ([True, False], [True, True])


def test_end_to_end_ts_repo_reports_a_cross_file_call():
    if shutil.which("typescript-language-server") is None:
        print("skip (no typescript-language-server on PATH): test_end_to_end_ts_repo_reports_a_cross_file_call")
        return

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "tsconfig.json", '{"compilerOptions": {"target": "es2020", "module": "commonjs"}}\n')
        _write(repo, "src/b.ts", "export function callee() {\n    return 1;\n}\n")
        _write(repo, "src/a.ts", 'import { callee } from "./b";\n\nexport function caller() {\n    return 0;\n}\n')
        base = _commit(repo, "base")
        _write(
            repo, "src/a.ts",
            'import { callee } from "./b";\n\nexport function caller() {\n    return callee();\n}\n',
        )
        head = _commit(repo, "head")

        result = _run_symdelta(repo, base, head)

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["language"] == "typescript"
        assert payload["counts"]["edges_new"] == 1
        assert any(
            e["source"] == "src:caller" and e["target"] == "src:callee" for e in payload["edges"]
        )
        # the extractor's own worktrees must not leak into the repo's worktree list
        worktrees = _git(repo, "worktree", "list")
        assert worktrees.strip().splitlines() == [worktrees.strip().splitlines()[0]]


def test_analyse_go_returns_null_language_when_the_extractor_fails():
    # A RuntimeError out of run_extractor (e.g. the silent-zero guard tripping) used to
    # propagate to main() and exit 1 with symdelta.json never written at all, taking the whole
    # symbols section down with no note. build_extractor failures (toolchain/setup problems)
    # must still raise, so only run_extractor is faked here.
    original_build = symdelta.build_extractor
    original_run = symdelta.run_extractor
    symdelta.build_extractor = lambda: None
    symdelta.run_extractor = lambda worktree_path: (_ for _ in ()).throw(
        RuntimeError("extractor: loaded 3 packages, 0 in-repo with type info")
    )
    try:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            _write(repo, "go.mod", "module example.com/x\n\ngo 1.21\n")
            _write(repo, "pkg/a.go", "package pkg\n\nfunc A() {}\n")
            base = _commit(repo, "base")
            _write(repo, "pkg/a.go", "package pkg\n\nfunc A() {}\n\nfunc B() {}\n")
            head = _commit(repo, "head")

            result = symdelta.analyse_go(repo, base, head)
            assert result == {
                "language": None,
                "reason": "extractor: loaded 3 packages, 0 in-repo with type info",
            }
    finally:
        symdelta.build_extractor = original_build
        symdelta.run_extractor = original_run


def test_analyse_lsp_returns_null_language_when_the_extractor_fails():
    # A RuntimeError out of run_ts_extractor (e.g. extract_edges's own zero guards tripping)
    # used to propagate to main() and exit 1 with symdelta.json never written at all. No remedy
    # here, unlike analyse_go's counterpart -- analyse_lsp's own comment explains why.
    original_check = symdelta.check_typescript_tooling
    original_run = symdelta.run_ts_extractor
    symdelta.check_typescript_tooling = lambda repo, lang="typescript": (True, None)
    symdelta.run_ts_extractor = lambda worktree_path, rel_files, lang="typescript": (
        _ for _ in ()
    ).throw(RuntimeError("LSP extractor failed on /tmp/x: python: 0 of 2 files yielded any symbols"))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            _write(repo, "a.py", "def a():\n    pass\n")
            base = _commit(repo, "base")
            _write(repo, "a.py", "def a():\n    return 1\n")
            head = _commit(repo, "head")

            result = symdelta.analyse_python(repo, base, head)
            assert result == {
                "language": None,
                "reason": "LSP extractor failed on /tmp/x: python: 0 of 2 files yielded any symbols",
            }
            assert "remedy" not in result
    finally:
        symdelta.check_typescript_tooling = original_check
        symdelta.run_ts_extractor = original_run


def test_llm_head_edges_malformed_line_errors_with_the_line_number():
    with tempfile.TemporaryDirectory() as tmp:
        edges_path = Path(tmp) / "bad.jsonl"
        edges_path.write_text('{"FromFile": "a.go", "FromSym": "A", "ToFile": "a.go"}\n')
        try:
            symdelta._load_llm_edges(str(edges_path))
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert f"{edges_path}:1:" in str(exc)


def test_llm_head_edges_produces_a_graph_with_llm_resolver():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "pkg/a.go", "package pkg\n\nfunc Caller() {}\n\nfunc Callee() {}\n")
        base = _commit(repo, "base")
        _write(repo, "pkg/a.go", "package pkg\n\nfunc Caller() {\n\tCallee()\n}\n\nfunc Callee() {}\n")
        head = _commit(repo, "head")

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


def test_llm_head_edges_falls_back_to_unknown_language_when_nothing_is_detected():
    # The whole point of this tier is that the mechanical one declined; detect_language finding
    # no supported extension at all must not refuse the LLM path too.
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "readme.md", "hello\n")
        base = _commit(repo, "base")
        _write(repo, "readme.md", "hello world\n")
        head = _commit(repo, "head")

        edges_path = repo / "llm-edges.jsonl"
        edges_path.write_text(json.dumps({
            "FromFile": "a", "FromSym": "A", "ToFile": "b", "ToSym": "B",
        }) + "\n")

        result = symdelta.analyse(repo, base, head, llm_head_edges=str(edges_path))
        assert result["language"] == "unknown"
        assert result["resolver"] == "llm"


def test_llm_tier_symbol_nodes_carry_no_range():
    # The LLM tier's wire shape is validated against _LLM_EDGE_FIELDS, which has no *Start/*End
    # keys at all -- position-less by schema, so its nodes must never carry a fabricated range.
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "pkg/a.go", "package pkg\n\nfunc Caller() {}\n\nfunc Callee() {}\n")
        base = _commit(repo, "base")
        _write(repo, "pkg/a.go", "package pkg\n\nfunc Caller() {\n\tCallee()\n}\n\nfunc Callee() {}\n")
        head = _commit(repo, "head")

        edges_path = repo / "llm-edges.jsonl"
        edges_path.write_text(json.dumps({
            "FromFile": "pkg/a.go", "FromSym": "Caller", "ToFile": "pkg/a.go", "ToSym": "Callee",
        }) + "\n")

        result = symdelta.analyse(repo, base, head, llm_head_edges=str(edges_path))
        symbol_nodes = [n for n in result["nodes"] if n["kind"] == "symbol"]
        assert symbol_nodes  # sanity: this tier did produce symbol nodes
        assert all("range" not in n for n in symbol_nodes)


def test_end_to_end_go_repo_reports_added_and_removed_symbols():
    if shutil.which("go") is None:
        print("skip (no `go` on PATH): test_end_to_end_go_repo_reports_added_and_removed_symbols")
        return

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        # The vendored extractor (extractors/go/main.go) filters packages by the module path it
        # reads from go.mod, so an unrelated module name proves that filter isn't hardcoded to
        # this repo.
        _write(repo, "go.mod", "module example.com/some-other-module\n\ngo 1.21\n")
        _write(
            repo,
            "pkg/a.go",
            "package pkg\n\nfunc Caller() {\n\tCallee()\n}\n\nfunc Callee() {}\n",
        )
        base = _commit(repo, "base")
        _write(
            repo,
            "pkg/a.go",
            "package pkg\n\nfunc Caller() {\n\tCallee()\n\tNewCallee()\n}\n\n"
            "func Callee() {}\n\nfunc NewCallee() {}\n",
        )
        head = _commit(repo, "head")

        result = _run_symdelta(repo, base, head)

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["language"] == "go"
        assert payload["counts"]["edges_new"] == 1
        assert payload["counts"]["edges_gone"] == 0
        assert any(
            e["source"] == "pkg:Caller" and e["target"] == "pkg:NewCallee"
            for e in payload["edges"]
        )
        # the extractor's own worktrees must not leak into the repo's worktree list
        worktrees = _git(repo, "worktree", "list")
        assert worktrees.strip().splitlines() == [worktrees.strip().splitlines()[0]]


def test_end_to_end_go_repo_reports_symbol_ranges_for_new_changed_and_gone_symbols():
    # Depends on the Go extractor (extractors/go/main.go) emitting FromStart/FromEnd/ToStart/
    # ToEnd on the wire; a failure here with an empty/absent "range" means the extractor isn't
    # emitting positions yet, not a bug in this test.
    if shutil.which("go") is None:
        print(
            "skip (no `go` on PATH): "
            "test_end_to_end_go_repo_reports_symbol_ranges_for_new_changed_and_gone_symbols"
        )
        return

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "go.mod", "module example.com/range-module\n\ngo 1.21\n")
        _write(
            repo,
            "pkg/a.go",
            "package pkg\n\nfunc Caller() {\n\tCallee()\n\tOldCallee()\n}\n\n"
            "func Callee() {}\n\nfunc OldCallee() {}\n",
        )
        base = _commit(repo, "base")
        _write(
            repo,
            "pkg/a.go",
            "package pkg\n\nfunc Caller() {\n\tCallee()\n\tNewCallee()\n}\n\n"
            "func Callee() {}\n\nfunc NewCallee() {}\n",
        )
        head = _commit(repo, "head")

        result = _run_symdelta(repo, base, head)

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        nodes = {n["id"]: n for n in payload["nodes"] if n["kind"] == "symbol"}

        # Caller: "func Caller() {" on line 3 of the HEAD fixture above, closing "}" on line 6.
        assert nodes["pkg:Caller"]["range"] == [3, 6]
        # NewCallee: single-line "func NewCallee() {}" on line 10 of the HEAD fixture.
        assert nodes["pkg:NewCallee"]["range"] == [10, 10]
        # OldCallee only exists at BASE, on line 10 of the BASE fixture -- its range must be the
        # BASE-side declaration, not absent and not HEAD's (it has none at HEAD).
        assert nodes["pkg:OldCallee"]["state"] == "gone"
        assert nodes["pkg:OldCallee"]["range"] == [10, 10]


def test_end_to_end_go_repo_skips_bodyless_funcs_without_panicking():
    # Assembly-backed / //go:linkname funcs have no Go body; extracting one used to call
    # ast.Inspect(nil, ...) and panic, aborting the whole extraction.
    if shutil.which("go") is None:
        print("skip (no `go` on PATH): test_end_to_end_go_repo_skips_bodyless_funcs_without_panicking")
        return

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "go.mod", "module example.com/bodyless-module\n\ngo 1.21\n")
        _write(
            repo,
            "pkg/a.go",
            "package pkg\n\nfunc Caller() {\n\tCallee()\n}\n\nfunc Callee() {}\n\n"
            "func asmFunc() uint64\n",
        )
        _write(
            repo,
            "pkg/a_amd64.s",
            "#include \"textflag.h\"\n\n"
            "TEXT ·asmFunc(SB), NOSPLIT, $0-8\n\tMOVQ $42, ret+0(FP)\n\tRET\n",
        )
        base = _commit(repo, "base")
        _write(
            repo,
            "pkg/a.go",
            "package pkg\n\nfunc Caller() {\n\tCallee()\n\tNewCallee()\n}\n\n"
            "func Callee() {}\n\nfunc NewCallee() {}\n\n"
            "func asmFunc() uint64\n",
        )
        head = _commit(repo, "head")

        result = _run_symdelta(repo, base, head)

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["language"] == "go"
        assert payload["counts"]["edges_new"] == 1
        assert any(
            e["source"] == "pkg:Caller" and e["target"] == "pkg:NewCallee"
            for e in payload["edges"]
        )


def test_end_to_end_go_repo_root_level_symbol_uses_the_root_sentinel():
    if shutil.which("go") is None:
        print("skip (no `go` on PATH): test_end_to_end_go_repo_root_level_symbol_uses_the_root_sentinel")
        return

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "go.mod", "module example.com/root-go\n\ngo 1.21\n")
        _write(repo, "main.go", "package main\n\nfunc main() {\n\tCallee()\n}\n\nfunc Callee() {}\n")
        base = _commit(repo, "base")
        _write(
            repo, "main.go",
            "package main\n\nfunc main() {\n\tCallee()\n\tNewCallee()\n}\n\n"
            "func Callee() {}\n\nfunc NewCallee() {}\n",
        )
        head = _commit(repo, "head")

        result = _run_symdelta(repo, base, head)

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["language"] == "go"
        symbol_ids = {n["id"] for n in payload["nodes"] if n["kind"] == "symbol"}
        assert "(root):main.NewCallee" in symbol_ids
        assert not any(sid.startswith(":") for sid in symbol_ids)
        pkg_ids = {n["id"] for n in payload["nodes"] if n["kind"] == "pkg"}
        assert "(root)" in pkg_ids


def test_end_to_end_ts_repo_root_level_symbol_uses_the_root_sentinel():
    if shutil.which("typescript-language-server") is None:
        print("skip (no typescript-language-server on PATH): test_end_to_end_ts_repo_root_level_symbol_uses_the_root_sentinel")
        return

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "tsconfig.json", '{"compilerOptions": {"target": "es2020", "module": "commonjs"}}\n')
        _write(
            repo, "index.ts",
            "export function callee() {\n    return 1;\n}\n\nexport function caller() {\n    return 0;\n}\n",
        )
        base = _commit(repo, "base")
        _write(
            repo, "index.ts",
            "export function callee() {\n    return 1;\n}\n\n"
            "export function caller() {\n    return callee();\n}\n",
        )
        head = _commit(repo, "head")

        result = _run_symdelta(repo, base, head)

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["language"] == "typescript"
        symbol_ids = {n["id"] for n in payload["nodes"] if n["kind"] == "symbol"}
        assert "(root):index.caller" in symbol_ids
        assert "(root):index.callee" in symbol_ids
        assert not any(sid.startswith(":") for sid in symbol_ids)


def test_end_to_end_ts_repo_two_root_files_with_same_function_name_stay_distinct():
    if shutil.which("typescript-language-server") is None:
        print(
            "skip (no typescript-language-server on PATH): "
            "test_end_to_end_ts_repo_two_root_files_with_same_function_name_stay_distinct"
        )
        return

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "tsconfig.json", '{"compilerOptions": {"target": "es2020", "module": "commonjs"}}\n')
        _write(repo, "a.ts", "export function run() {\n    return 1;\n}\n")
        _write(repo, "b.ts", "export function run() {\n    return 2;\n}\n")
        _write(
            repo, "entry.ts",
            'import { run as runA } from "./a";\nimport { run as runB } from "./b";\n\n'
            'export function caller() {\n    return 0;\n}\n',
        )
        base = _commit(repo, "base")
        _write(
            repo, "entry.ts",
            'import { run as runA } from "./a";\nimport { run as runB } from "./b";\n\n'
            'export function caller() {\n    return runA() + runB();\n}\n',
        )
        head = _commit(repo, "head")

        result = _run_symdelta(repo, base, head)

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        symbol_ids = {n["id"] for n in payload["nodes"] if n["kind"] == "symbol"}
        # Same function name ("run"), two unrelated root files -- must not collapse onto one id.
        assert "(root):a.run" in symbol_ids
        assert "(root):b.run" in symbol_ids


if __name__ == "__main__":
    tests = [
        test_brace_rename_with_empty_side_collapses_the_slash,
        test_brace_rename_with_non_empty_side,
        test_plain_rename_with_no_common_prefix,
        test_non_rename_summary_lines_are_ignored,
        test_compute_new_gone_canonicalises_renamed_head_paths,
        test_compute_new_gone_keeps_real_changes,
        test_compute_new_gone_carries_head_path_for_new_symbol_in_renamed_file,
        test_move_collapse_tallies_only_the_caller_side_move,
        test_move_collapse_leaves_unrelated_bare_name_collisions_alone_when_no_match,
        test_move_collapse_does_not_cross_match_unrelated_same_name_edges,
        test_no_dangling_edges_across_new_gone_and_changed_states,
        test_build_graph_merges_a_symbol_that_moved_package_and_changed_its_calls,
        test_build_graph_merges_a_root_level_rename_into_a_subdirectory_with_a_changed_symbol,
        test_build_graph_merges_via_git_rename_map_alone,
        test_resolve_symbol_merges_case_insensitive_fallback_via_file_rename,
        test_resolve_symbol_merges_case_insensitive_fallback_via_package_level_match,
        test_resolve_symbol_merges_ambiguous_case_match_falls_through_without_merging,
        test_build_graph_merges_an_export_capitalisation_rename_across_a_package_move,
        test_build_graph_ambiguous_case_only_candidates_are_not_merged_into,
        test_build_graph_genuine_deletion_with_no_rename_evidence_stays_gone,
        test_build_graph_symbol_state_new_gone_changed,
        test_build_graph_package_chain_reaches_the_root,
        test_drop_test_edges_removes_an_edge_touching_a_test_file_on_either_end,
        test_build_graph_nodes_and_edges_carry_no_isTest_field,
        test_build_graph_collapses_a_two_deep_passthrough_chain_under_a_real_grandparent,
        test_build_graph_collapses_a_three_deep_chain_fully_into_one_box,
        test_build_graph_does_not_collapse_a_package_with_two_children,
        test_build_graph_does_not_collapse_a_package_with_a_symbol_child_alongside_its_pkg_child,
        test_build_graph_depths_stay_contiguous_after_compression_for_pkg_and_symbol_nodes,
        test_build_graph_root_pkg_sentinel_is_never_folded_into_or_through,
        test_pkg_of_root_file_uses_sentinel_not_empty_string,
        test_sym_id_root_file_is_qualified_and_never_starts_with_a_bare_colon,
        test_build_graph_two_root_files_with_same_symbol_name_stay_distinct,
        test_decl_ranges_keeps_only_positive_int_starts_and_defaults_bad_ends,
        test_decl_ranges_first_seen_wins_across_edges,
        test_build_graph_attaches_head_range_to_new_symbols,
        test_build_graph_attaches_base_range_to_gone_symbols,
        test_build_graph_changed_symbol_uses_head_range_not_base,
        test_build_graph_merge_target_gets_range_keyed_on_its_head_file,
        test_build_graph_symbol_with_no_range_entry_gets_no_range_key,
        test_build_graph_same_sid_from_two_files_uses_the_first_files_range,
        test_detect_language_tie_break_is_honest_and_deterministic,
        test_detect_language_routes_py_files_to_python,
        test_detect_language_works_against_an_orphan_baseline_with_no_merge_base,
        test_detect_language_routes_rs_files_to_rust,
        test_detect_language_tie_break_between_python_and_rust_is_alphabetical,
        test_lsp_language_tables_all_cover_typescript_python_and_rust,
        test_dependency_files_changed_true_when_package_json_changed,
        test_dependency_files_changed_false_for_an_unrelated_change,
        test_dependency_files_changed_true_for_yarn_lock,
        test_dependency_files_changed_true_for_pnpm_lock,
        test_dependency_files_changed_true_for_pyproject_toml,
        test_dependency_files_changed_false_for_python_when_only_ts_lockfile_changed,
        test_dependency_files_changed_true_for_cargo_lock,
        test_ts_dependency_incompatible_false_for_additions_only,
        test_ts_dependency_incompatible_true_when_a_dependency_is_removed,
        test_ts_dependency_incompatible_true_when_a_version_spec_changed,
        test_ts_dependency_incompatible_false_for_lockfile_only_change,
        test_ts_dependency_incompatible_true_when_package_json_missing_at_base,
        test_ts_dependency_incompatible_true_when_package_json_is_unparseable,
        test_ts_dependency_incompatible_true_when_sections_disagree_on_spec,
        test_ts_dependency_incompatible_false_when_same_spec_appears_in_two_sections,
        test_link_node_modules_resolves_a_relative_repo_path,
        test_check_node_modules_coverage_true_when_every_dependency_resolves,
        test_check_node_modules_coverage_true_when_node_modules_missing_and_nothing_declared,
        test_check_node_modules_coverage_catches_a_missing_scoped_package,
        test_check_node_modules_coverage_caps_the_reason_at_five_names,
        test_check_node_modules_coverage_catches_a_missing_peer_dependency,
        test_check_node_modules_coverage_ignores_a_missing_optional_dependency,
        test_check_node_modules_coverage_true_when_installed_version_matches_lockfile,
        test_check_node_modules_coverage_catches_a_stale_installed_version,
        test_check_node_modules_coverage_presence_only_when_no_lockfile,
        test_check_node_modules_coverage_presence_only_when_lockfile_is_unparseable,
        test_check_node_modules_coverage_handles_lockfile_version_1_shape,
        test_analyse_typescript_refuses_when_a_dependency_is_removed,
        test_analyse_typescript_does_not_refuse_on_dependency_additions_only,
        test_analyse_typescript_refuses_with_npm_ci_remedy_when_node_modules_is_stale,
        test_analyse_typescript_refuses_with_npm_ci_remedy_when_installed_version_is_stale,
        test_analyse_lsp_attaches_language_server_remedy_when_tooling_check_fails,
        test_split_path_trap_pulls_the_remedy_out_of_the_reason,
        test_split_path_trap_returns_the_reason_unchanged_with_no_remedy_for_a_plain_reason,
        test_analyse_lsp_uses_the_path_trap_remedy_instead_of_a_reinstall,
        test_doctor_report_is_ok_for_every_language_when_everything_checks_out,
        test_doctor_report_names_the_go_toolchain_as_missing,
        test_doctor_report_prefers_the_path_trap_remedy_over_language_server_remedy,
        test_doctor_report_survives_a_present_but_broken_binary,
        test_main_doctor_flag_needs_no_repo_base_or_head,
        test_check_typescript_tooling_raises_runtime_error_on_timeout,
        test_run_ts_extractor_raises_runtime_error_on_timeout_and_still_cleans_up_the_files_list,
        test_run_in_process_group_handles_getpgid_race_without_crashing,
        test_run_in_process_group_kills_the_whole_group_not_just_the_direct_child,
        test_build_extractor_raises_runtime_error_on_timeout,
        test_run_extractor_raises_runtime_error_on_timeout,
        test_run_extractor_omits_goflags_when_go_work_exists_and_sets_it_otherwise,
        test_resolve_cache_dir_uses_xdg_cache_home_when_set,
        test_resolve_cache_dir_falls_back_to_dot_cache_when_xdg_unset,
        test_resolve_cache_dir_xdg_cache_home_wins_even_when_dot_cache_exists,
        test_lsp_client_shutdown_swallows_timeout_from_an_unreapable_process,
        test_no_supported_files_short_circuits_with_null_language,
        test_detect_language_picks_the_majority_on_a_mixed_diff,
        test_ts_files_by_side_excludes_added_from_base_and_deleted_from_head,
        test_ts_files_by_side_filters_by_given_extensions,
        test_bad_ref_fails_with_nonzero_exit,
        test_analyse_does_not_crash_on_an_orphan_baseline,
        test_analyse_go_returns_null_language_when_the_extractor_fails,
        test_analyse_lsp_returns_null_language_when_the_extractor_fails,
        test_llm_head_edges_malformed_line_errors_with_the_line_number,
        test_llm_head_edges_produces_a_graph_with_llm_resolver,
        test_llm_head_edges_falls_back_to_unknown_language_when_nothing_is_detected,
        test_llm_tier_symbol_nodes_carry_no_range,
        test_end_to_end_ts_repo_reports_a_cross_file_call,
        test_end_to_end_go_repo_reports_added_and_removed_symbols,
        test_end_to_end_go_repo_reports_symbol_ranges_for_new_changed_and_gone_symbols,
        test_end_to_end_go_repo_skips_bodyless_funcs_without_panicking,
        test_end_to_end_go_repo_root_level_symbol_uses_the_root_sentinel,
        test_end_to_end_ts_repo_root_level_symbol_uses_the_root_sentinel,
        test_end_to_end_ts_repo_two_root_files_with_same_function_name_stay_distinct,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
