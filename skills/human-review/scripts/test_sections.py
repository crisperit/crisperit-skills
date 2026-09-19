#!/usr/bin/env python3
"""Self-check for sections.py. Assert-based, no framework."""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sections import (  # noqa: E402
    LABEL_WRAP_TARGET,
    LINK_COLOR_GONE,
    LINK_COLOR_NEW,
    _changed_symbol_ids,
    _connected_components,
    _edge_endpoint_ids,
    _mermaid_packages,
    _mermaid_symbols,
    _mermaid_symbols_for_level,
    _mm_escape,
    _symbols_legend,
    _symbols_legend_text,
    _symbols_orphans,
    _symbols_scope,
    render_symbols,
    render_symbols_md,
    wrap_label,
)

# ---- symdelta.py fixtures: nodes/edges shaped exactly like build_graph's own output --------

SYMDELTA = {
    "nodes": [
        {"id": "a", "label": "a", "kind": "pkg", "parent": None, "depth": 0},
        {"id": "b", "label": "b", "kind": "pkg", "parent": None, "depth": 0},
        {"id": "a:New", "label": "New", "kind": "symbol", "parent": "a", "depth": 1,
         "state": "new", "file": "a/x.go"},
        {"id": "a:Gone", "label": "Gone", "kind": "symbol", "parent": "a", "depth": 1,
         "state": "gone", "file": "a/x.go"},
        {"id": "b:Moved", "label": "Moved", "kind": "symbol", "parent": "b", "depth": 1,
         "state": "changed", "file": "b/y.go"},
    ],
    "edges": [
        {"id": "e0", "source": "a:New", "target": "b:Moved", "state": "new"},
        {"id": "g0", "source": "a:Gone", "target": "a:New", "state": "gone"},
    ],
    "moved": [{"from": "a/old", "to": "b", "callsites": 2}],
    "presets": {"packages": [], "symbols": ["a", "b"]},
    "counts": {"symbols": 3, "packages": 2, "edges_new": 1, "edges_gone": 1},
}


def test_symbols_returns_nothing_when_there_are_no_nodes():
    assert render_symbols({"nodes": []}) == ""
    assert render_symbols({}) == ""


def test_symbols_html_has_marker_legend_slider_and_three_levels():
    out = render_symbols(SYMDELTA)

    assert out.startswith("<!-- visual-diff:symbols -->")
    assert '<div class="vd-symbols">' in out
    assert '<h2 class="h2-row"><span>Changes visualization</span>' in out
    # Two levels, so a toggle: it opens on packages and its label names that, not the switch.
    assert 'id="vd-level-toggle"' in out
    assert ">packages</button>" in out
    assert 'aria-label="Detail level: packages. Activate to show symbols."' in out
    assert "aria-pressed" not in out
    assert 'data-names="' in out and "Symbols + callers" not in out
    assert "vd-level-name" not in out  # the slider's caption went with it
    assert '<div class="mermaid" data-level="1"' in out
    assert '<div class="mermaid" data-level="2" hidden' in out
    assert 'data-level="3"' not in out
    assert "flowchart LR" in out
    assert "flowchart TB" not in out


def test_symbols_html_never_emits_classdef():
    # Regression guard: classDef used to leak into the HTML page and override its own light/dark
    # node theming (see the comment above LINK_COLOR_NEW/GONE in sections.py).
    out = render_symbols(SYMDELTA)

    assert "classDef" not in out


def test_symbols_note_is_the_counts_sentence_plus_drawn_and_listed():
    # a and b are both top-level packages -- no containment relationship between them -- so
    # undrawn_count is 0 here and the note says nothing about it (see the nonzero case below).
    out = render_symbols(SYMDELTA)

    note = out.split('<p class="note">', 1)[1].split("</p>", 1)[0]
    assert note == (
        "Showing 5 packages and symbols, 2 relations between them. "
        "2 changed symbols drawn as nodes, 0 listed below."
    )


def test_symbols_note_names_the_undrawn_containment_count_when_nonzero():
    nodes = SYMDELTA["nodes"] + [
        {"id": "a/inner", "label": "inner", "kind": "pkg", "parent": "a", "depth": 1},
        {"id": "a/inner:D", "label": "D", "kind": "symbol", "parent": "a/inner", "depth": 1,
         "state": "new", "file": "a/inner/d.go"},
    ]
    edges = SYMDELTA["edges"] + [{"id": "e1", "source": "a:New", "target": "a/inner:D", "state": "new"}]
    data = {**SYMDELTA, "nodes": nodes, "edges": edges}

    out = render_symbols(data)

    note = out.split('<p class="note">', 1)[1].split("</p>", 1)[0]
    assert (
        "1 relations between a package and a package inside it are not drawn because the "
        "containment box already shows them." in note
    )
    level1 = out.split('data-level="1"', 1)[1].split(">", 1)[1].split("</div>", 1)[0]
    assert "×" not in level1


def test_symbols_moved_renders_as_prose_not_a_graph_node():
    # The `vd-moved` prose list is the sole channel for a package-level move: a package-level
    # move edge was tried on the level-1 diagram and reverted because it broke the containment
    # layout (see render_symbols).
    out = render_symbols(SYMDELTA)

    assert '<ul class="vd-moved">' in out
    assert "2 call sites moved, a/old -&gt; b" in out
    level1 = out.split('data-level="1"', 1)[1].split(">", 1)[1].split("</div>", 1)[0]
    assert "call sites moved" not in level1


def test_mermaid_packages_level1_never_draws_a_dashed_move_edge():
    # Regression guard for the reverted feature: SYMDELTA carries a non-empty "moved" tally, and
    # render_symbols must not feed it into _mermaid_packages at all, let alone as a `-.->` edge.
    out = render_symbols(SYMDELTA)

    level1 = out.split('data-level="1"', 1)[1].split(">", 1)[1].split("</div>", 1)[0]
    assert "-.->" not in level1


SYMDELTA_WITH_ORPHAN = {
    **SYMDELTA,
    "nodes": SYMDELTA["nodes"] + [
        {"id": "b:Lonely", "label": "Lonely", "kind": "symbol", "parent": "b", "depth": 1,
         "state": "new", "file": "b/z.go"},
    ],
}


def test_symbols_orphan_symbol_is_listed_instead_of_drawn():
    # b:Lonely is new (so it's a "changed" symbol that has to be shown) but touches no edge in
    # the symbols level's edge set -- exactly the disconnected-node case that blew that level
    # out to thousands of pixels wide on a real PR.
    out = render_symbols(SYMDELTA_WITH_ORPHAN)

    level2 = out.split('data-level="2" hidden', 1)[1].split("</div>", 1)[0]
    assert "Lonely" not in level2
    assert "<li>b: Lonely (new)</li>" in out
    note = out.split('<p class="note">', 1)[1].split("</p>", 1)[0]
    assert "2 changed symbols drawn as nodes, 1 listed below." in note


def test_symbols_orphans_helper_groups_by_package_and_marks_new_gone():
    nodes = [
        {"id": "a", "label": "a", "kind": "pkg", "parent": None, "depth": 0},
        {"id": "a:Zeta", "label": "Zeta", "kind": "symbol", "parent": "a", "depth": 1,
         "state": "gone", "file": "a/x.go"},
        {"id": "a:Alpha", "label": "Alpha", "kind": "symbol", "parent": "a", "depth": 1,
         "state": "new", "file": "a/x.go"},
    ]

    out = _symbols_orphans(nodes, {"a:Zeta", "a:Alpha"})

    assert out == '<ul class="vd-moved"><li>a: Alpha (new)</li><li>a: Zeta (gone)</li></ul>\n'


def test_symbols_orphans_is_empty_string_when_nothing_is_held_back():
    assert _symbols_orphans(SYMDELTA["nodes"], set()) == ""


def test_edge_endpoint_ids_collects_both_sides_of_every_edge():
    edges = [{"source": "x", "target": "y"}, {"source": "y", "target": "z"}]
    assert _edge_endpoint_ids(edges) == {"x", "y", "z"}


def test_mermaid_symbols_for_level_returns_a_placeholder_node_when_nothing_qualifies():
    # A bare "flowchart LR" would render at a degenerate near-zero viewBox that the page's own
    # JS mistakes for a failed render (see diff-review-template.html's degenerate()); this has
    # to stay a real, if trivial, diagram instead.
    text, id_map = _mermaid_symbols_for_level(SYMDELTA["nodes"], set(), [])
    assert text == 'flowchart LR\n  N0["Nothing to draw at this detail level"]'
    assert id_map == {}


def test_mermaid_symbols_for_level_draws_normally_when_something_qualifies():
    ids, edges = _symbols_scope(SYMDELTA["nodes"], SYMDELTA["edges"])

    result = _mermaid_symbols_for_level(SYMDELTA["nodes"], ids, edges)

    assert result == _mermaid_symbols(SYMDELTA["nodes"], ids, edges)


def test_symbols_all_orphaned_level_gets_a_placeholder_not_a_degenerate_diagram():
    # b:Lonely is the only changed symbol and has no edge at all, so both level 2 and level 3
    # end up with nothing to draw -- exactly the all-orphaned case the placeholder exists for.
    data = {
        "nodes": [
            {"id": "b", "label": "b", "kind": "pkg", "parent": None, "depth": 0},
            {"id": "b:Lonely", "label": "Lonely", "kind": "symbol", "parent": "b", "depth": 1,
             "state": "new", "file": "b/z.go"},
        ],
        "edges": [],
    }

    out = render_symbols(data)

    level2 = out.split('data-level="2" hidden', 1)[1].split("</div>", 1)[0]
    assert "Nothing to draw at this detail level" in level2
    assert "<li>b: Lonely (new)</li>" in out


# ---- data-ids: the node menu's file map, moved here from the deleted coupling section -------

_TWO_LINKED_SYMBOLS = [
    {"id": "a", "label": "a", "kind": "pkg", "parent": None, "depth": 0},
    {"id": "a:F", "label": "F", "kind": "symbol", "parent": "a", "depth": 1,
     "state": "new", "file": "a/f.go"},
    {"id": "a:G", "label": "G", "kind": "symbol", "parent": "a", "depth": 1,
     "state": "new", "file": "a/g.go"},
]
_ONE_EDGE = [{"id": "e0", "source": "a:F", "target": "a:G", "state": "new"}]


def test_mermaid_symbols_id_map_keys_symbol_nodes_not_package_boxes():
    text, id_map = _mermaid_symbols(_TWO_LINKED_SYMBOLS, {"a:F", "a:G"}, _ONE_EDGE)

    assert set(id_map.values()) == {"a/f.go", "a/g.go"}
    assert all(k.startswith("S") for k in id_map)  # no "G..." package box id is ever a key


def test_symbols_html_level1_data_ids_is_always_empty():
    # Package boxes carry no file of their own (see _mermaid_symbols), so the packages level's
    # map has nothing to hold even when the symbols level's does.
    out = render_symbols({"nodes": _TWO_LINKED_SYMBOLS, "edges": _ONE_EDGE})

    attr = out.split('data-level="1"', 1)[1].split('data-ids="', 1)[1].split('"', 1)[0]
    assert attr == "{}"


def test_symbols_html_level2_data_ids_maps_symbol_nodes_not_package_boxes():
    out = render_symbols({"nodes": _TWO_LINKED_SYMBOLS, "edges": _ONE_EDGE})

    attr = out.split('data-level="2"', 1)[1].split('data-ids="', 1)[1].split('"', 1)[0]
    ids = json.loads(attr.replace("&quot;", '"'))
    assert set(ids.values()) == {"a/f.go", "a/g.go"}
    assert all(k.startswith("S") for k in ids)


def test_symbols_html_data_ids_survives_a_quote_in_a_file_path():
    nodes = [
        {"id": "a", "label": "a", "kind": "pkg", "parent": None, "depth": 0},
        {"id": "a:F", "label": "F", "kind": "symbol", "parent": "a", "depth": 1,
         "state": "new", "file": 'weird"path.go'},
        {"id": "a:G", "label": "G", "kind": "symbol", "parent": "a", "depth": 1,
         "state": "new", "file": "a/g.go"},
    ]
    out = render_symbols({"nodes": nodes, "edges": _ONE_EDGE})

    attr = out.split('data-level="2"', 1)[1].split('data-ids="', 1)[1].split('"', 1)[0]
    assert '"' not in attr  # every quote became &quot;, so none can end the attribute early
    ids = json.loads(attr.replace("&quot;", '"'))
    assert 'weird"path.go' in ids.values()


def test_mm_escape_strips_parens_quotes_and_backticks():
    assert _mm_escape('Foo("bar") `baz`') == "Foobar baz"


def test_wrap_label_breaks_a_long_multiword_label_at_spaces():
    label = "NewRequest sets Request User Id to usrID for the MediaGuard lookup"

    out = wrap_label(label)

    assert " ".join(out.split("<br/>")) == label  # every word survives, none lost
    assert "MediaGuard lookup" in out  # never split across a line break


def test_wrap_label_breaks_a_single_long_camelcase_word_at_its_boundaries():
    # No '/', '_' or '.' to break on -- only camelCase boundaries are left, which is exactly the
    # fallback path this exercises.
    identifier = "filterUnusableIdentifierThatIsVeryLongIndeedYes"

    out = wrap_label(identifier)

    assert "<br/>" in out
    assert "".join(out.split("<br/>")) == identifier  # every character survives, none lost


def test_wrap_label_keeps_going_around_a_long_camelcase_word_between_short_ones():
    out = wrap_label("short filterUnusableIdentifierThatIsVeryLongIndeedYes done")

    assert "".join(out.split("<br/>")[1:-1]) == "filterUnusableIdentifierThatIsVeryLongIndeedYes"
    assert out.split("<br/>")[0] == "short"
    assert out.split("<br/>")[-1] == "done"


def test_wrap_label_keeps_going_rather_than_truncating_a_long_sentence():
    # The 25-word priority-chain sentence from the real bug report, trimmed to keep the test
    # readable. Longer than four lines at the default target -- a taller node beats losing text.
    sentence = ("Resolve priority chain IFA for app then SyncID cookie then "
                "User BuyerUID then User ID then Pubcid then IFA fallback for site")

    out = wrap_label(sentence)

    assert " ".join(out.split("<br/>")) == sentence
    assert out.count("<br/>") > 3  # ran past the nominal four lines instead of cutting anything


def test_wrap_label_empty_label_is_unchanged():
    assert wrap_label("") == ""


def test_wrap_label_breaks_a_package_path_at_slashes():
    label = "corelib/ratelimit/buckets"  # 25 chars, one over the 24-char default target

    out = wrap_label(label)

    assert "<br/>" in out
    assert "".join(out.split("<br/>")) == label  # slash stays on the end of the earlier line
    assert out.split("<br/>")[0].endswith("/")


def test_wrap_label_falls_back_to_underscore_when_a_segment_alone_is_too_long():
    # No slash makes any given piece short enough on its own, so '_' has to do the rest.
    label = "a_very_long_snake_case_package_name"

    out = wrap_label(label, target=12)

    assert "<br/>" in out
    assert "".join(out.split("<br/>")) == label
    assert all(len(line) <= 12 for line in out.split("<br/>"))


def test_wrap_label_leaves_a_short_package_path_untouched():
    assert wrap_label("corelib/ratelimit") == "corelib/ratelimit"


# ---- real Go symbol names from the node-label-clipping bug report: no '/' or '_' to break on,
# so these used to stay one 30+ char line and get clipped by mermaid's own wrappingWidth -------

_REAL_CLIPPED_LABELS = [
    "MediaGuardGRPCDecorator.Lookup",
    "distributionChannelResolverChain.Resolve",
    "NewDistributionChannelResolverChain",
    "distributionChannelResolver.canBeApplied",
]


def test_wrap_label_breaks_the_real_clipped_go_symbol_names():
    for label in _REAL_CLIPPED_LABELS:
        out = wrap_label(label)
        lines = out.split("<br/>")

        assert "".join(lines) == label  # no piece ever loses a character
        assert all(len(line) <= LABEL_WRAP_TARGET for line in lines), label


def test_wrap_label_breaks_a_dotted_selector_after_the_dot():
    out = wrap_label("MediaGuardGRPCDecorator.Lookup")

    assert out.split("<br/>")[0].endswith(".")  # dot stays on the end of the earlier line, like '/'


def test_wrap_label_keeps_an_acronym_run_together_when_camel_splitting():
    # GRPC must survive as one piece, not get shredded letter by letter, once there's no
    # separator left and camelCase boundaries are all that's left to break on.
    out = wrap_label("MediaGuardGRPCDecorator", target=12)

    assert "".join(out.split("<br/>")) == "MediaGuardGRPCDecorator"
    assert "GRPC" in out.split("<br/>")


def test_wrap_label_all_caps_word_with_no_boundary_is_returned_whole():
    # No '/', '_' or '.', and no lowercase letter to anchor a camel boundary either -- nothing
    # left to break on, so it stays one (long) line rather than being truncated.
    identifier = "ABCDEFGHIJKLMNOPQRSTUVWXYZAB"

    assert wrap_label(identifier) == identifier


def test_changed_symbol_ids_excludes_the_moved_state():
    ids = _changed_symbol_ids(SYMDELTA["nodes"])
    assert ids == {"a:New", "a:Gone"}


def test_symbols_scope_pulls_in_the_context_endpoint_of_a_changed_edge():
    # b:Moved is only "changed", so it is never the subject of an edge, but e0 reaches it from
    # a changed symbol and the reader needs to see what that call now lands on.
    ids, edges = _symbols_scope(SYMDELTA["nodes"], SYMDELTA["edges"])

    assert ids == {"a:New", "a:Gone", "b:Moved"}
    assert {e["id"] for e in edges} == {"e0", "g0"}


def test_mermaid_packages_rolls_up_cross_package_edges_with_a_count_label():
    text, undrawn = _mermaid_packages(SYMDELTA["nodes"], SYMDELTA["edges"])

    assert text.startswith("flowchart LR")
    assert '["a"]' in text and '["b"]' in text
    # a:Gone -> a:New is a same-package edge and has nothing to roll up to at this level.
    assert "|1|" in text
    assert text.count("-->") == 1
    assert undrawn == 0


def test_mermaid_packages_nests_child_packages_inside_their_parent_box():
    nodes = [
        {"id": "corelib", "label": "corelib", "kind": "pkg", "parent": None, "depth": 0},
        {"id": "corelib/ratelimit", "label": "ratelimit", "kind": "pkg", "parent": "corelib", "depth": 1},
        {"id": "corelib/ratelimit/internal", "label": "internal", "kind": "pkg",
         "parent": "corelib/ratelimit", "depth": 2},
    ]

    text, undrawn = _mermaid_packages(nodes, [])

    assert 'subgraph P0["corelib"]' in text
    assert 'subgraph P1["ratelimit"]' in text
    assert 'P2["internal"]' in text  # a leaf package is a plain node, not an empty subgraph
    lines = text.splitlines()
    top = next(i for i, l in enumerate(lines) if 'subgraph P0["corelib"]' in l)
    inner = next(i for i, l in enumerate(lines) if 'subgraph P1["ratelimit"]' in l)
    leaf = next(i for i, l in enumerate(lines) if 'P2["internal"]' in l)
    assert top < inner < leaf
    assert text.count("end") == 2  # closes the two subgraphs; the leaf node needs none
    assert undrawn == 0


def test_mermaid_packages_does_not_draw_a_containment_edge_and_reports_its_count():
    # outer -> outer/inner targets a package nested inside its own source -- dagre has nowhere
    # sensible to route that, and the real bug drew a stub arrow whose midpoint sat flush against
    # the inner box (reported as "numbers should be on center of arrow not next to a package").
    # outer -> sib is a normal edge between non-nested packages and must still draw as an arrow.
    nodes = [
        {"id": "outer", "label": "outer", "kind": "pkg", "parent": None, "depth": 0},
        {"id": "outer/inner", "label": "inner", "kind": "pkg", "parent": "outer", "depth": 1},
        {"id": "sib", "label": "sib", "kind": "pkg", "parent": None, "depth": 0},
        {"id": "outer:A", "label": "A", "kind": "symbol", "parent": "outer", "depth": 0,
         "state": "new", "file": "outer/a.go"},
        {"id": "outer/inner:B", "label": "B", "kind": "symbol", "parent": "outer/inner",
         "depth": 1, "state": "new", "file": "outer/inner/b.go"},
        {"id": "sib:C", "label": "C", "kind": "symbol", "parent": "sib", "depth": 0,
         "state": "new", "file": "sib/c.go"},
    ]
    edges = [
        {"source": "outer:A", "target": "outer/inner:B", "state": "new"},
        {"source": "outer:A", "target": "sib:C", "state": "new"},
    ]

    text, undrawn = _mermaid_packages(nodes, edges)

    assert '["inner"]' in text  # just the package's own segment name
    assert "×" not in text
    assert text.count("-->") == 1  # only outer -> sib is a real, drawable edge
    assert "|1|" in text  # the surviving edge still carries its own count
    assert undrawn == 1  # outer -> outer/inner, skipped rather than drawn


def test_mermaid_symbols_wraps_a_long_label_without_touching_ids_arrows_or_classdef():
    nodes = SYMDELTA["nodes"] + [
        {"id": "a:Filter", "label": "filters unusable identifiers before they reach the exchange",
         "kind": "symbol", "parent": "a", "depth": 1, "state": "new", "file": "a/z.go"},
    ]
    ids, edges = _symbols_scope(nodes, SYMDELTA["edges"])
    ids = ids | {"a:Filter"}

    text, _ = _mermaid_symbols(nodes, ids, edges, emit_classdef=True)
    label_line = next(line for line in text.splitlines() if "filters" in line)

    assert "<br/>" in label_line
    assert label_line.count('"') == 2  # one label, one pair of quotes -- not one per wrapped line
    wrapped = label_line.split('"')[1]
    assert " ".join(wrapped.split("<br/>")) == (
        "filters unusable identifiers before they reach the exchange")
    # everything else about the diagram is exactly what it was without the long label
    assert f"classDef new stroke:{LINK_COLOR_NEW},stroke-width:2px;" in text
    assert f"classDef gone stroke:{LINK_COLOR_GONE},stroke-dasharray:4 4,opacity:0.7;" in text
    assert "var(" not in text  # mermaid's own parser can't resolve a CSS var()
    assert "class " in text
    assert text.count("-->") == len(edges)


def test_mermaid_symbols_appends_a_was_line_for_a_renamed_symbol():
    nodes = SYMDELTA["nodes"] + [
        {"id": "a:IncrementChecksTotal", "label": "IncrementChecksTotal", "kind": "symbol",
         "parent": "a", "depth": 1, "state": "changed", "file": "a/x.go",
         "was": "incrementChecksTotal"},
    ]

    text, _ = _mermaid_symbols(nodes, {"a:IncrementChecksTotal"}, [])
    label_line = next(line for line in text.splitlines() if "IncrementChecksTotal" in line)

    assert label_line.count('"') == 2  # still one label, one pair of quotes
    assert "<br/>was incrementChecksTotal" in label_line


def test_mermaid_symbols_label_has_no_was_line_when_the_name_did_not_change():
    # b:Moved carries no "was" key at all (see SYMDELTA), matching a plain package move with no
    # rename -- its label must stay exactly its own name, no second line.
    text, _ = _mermaid_symbols(SYMDELTA["nodes"], {"b:Moved"}, [])
    label_line = next(line for line in text.splitlines() if "Moved" in line)

    assert "was" not in label_line


def test_symbols_legend_ignores_the_word_new_in_a_label_not_a_class_line():
    # Reproduces the Go export-capitalisation case _mermaid_symbols itself documents: renaming
    # `new` to `New` puts the word "new" in the label's "was" line even though the symbol's own
    # state is "changed", not "new" -- no `class ... new` line is emitted at all. The legend
    # must key off that line, not a substring scan of the whole diagram source.
    nodes = [
        {"id": "a", "label": "a", "kind": "pkg", "parent": None, "depth": 0},
        {"id": "a:New", "label": "New", "kind": "symbol", "parent": "a", "depth": 1,
         "state": "changed", "file": "a/x.go", "was": "new"},
    ]
    text, _ = _mermaid_symbols(nodes, {"a:New"}, [])

    assert "class " not in text
    assert _symbols_legend(text) == ""
    assert _symbols_legend_text(text) == ""


def test_new_and_gone_class_lists_are_declaration_order_not_set_order():
    # new_ids/gone_ids used to build off the caller's raw symbol_ids set, whose iteration order
    # varies with PYTHONHASHSEED across process runs even for the exact same input. Pinning to
    # ascending S-index order (the order mm_id itself was built in) is what makes two runs of
    # the same data byte-identical. Needs several new-state symbols or a one-element "list" would
    # pass trivially regardless of the bug.
    nodes = [{"id": "a", "label": "a", "kind": "pkg", "parent": None, "depth": 0}] + [
        {"id": f"a:N{i}", "label": f"N{i}", "kind": "symbol", "parent": "a", "depth": 1,
         "state": "new", "file": "a/x.go"}
        for i in range(8)
    ]
    ids = {n["id"] for n in nodes if n["kind"] == "symbol"}

    text, _ = _mermaid_symbols(nodes, ids, [])

    line = next(l for l in text.splitlines() if l.strip().startswith("class "))
    listed = line.strip().split()[1].split(",")
    assert listed == sorted(listed, key=lambda s: int(s[1:]))


def test_mermaid_symbols_subgraphs_by_package_and_classes_new_gone_but_not_changed():
    ids, edges = _symbols_scope(SYMDELTA["nodes"], SYMDELTA["edges"])
    text, _ = _mermaid_symbols(SYMDELTA["nodes"], ids, edges, emit_classdef=True)

    assert 'subgraph G0["a"]' in text
    assert 'subgraph G1["b"]' in text
    assert "classDef new" in text and "classDef gone" in text
    assert "var(" not in text
    assert "class new" not in text  # never a bare "changed" classDef
    assert "linkStyle" in text
    assert LINK_COLOR_NEW in text and LINK_COLOR_GONE in text


def test_mermaid_symbols_omits_classdef_by_default_html_shape():
    # Default (no emit_classdef) is what render_symbols (HTML) uses -- see
    # test_symbols_html_never_emits_classdef for the full-pipeline pin. `class ... new/gone`
    # itself must still be there, for the legend.
    ids, edges = _symbols_scope(SYMDELTA["nodes"], SYMDELTA["edges"])
    text, _ = _mermaid_symbols(SYMDELTA["nodes"], ids, edges)

    assert "classDef" not in text
    assert "class " in text


def test_mermaid_symbols_own_symbols_sit_inside_a_box_that_also_nests_children():
    nodes = [
        {"id": "corelib", "label": "corelib", "kind": "pkg", "parent": None, "depth": 0},
        {"id": "corelib/ratelimit", "label": "ratelimit", "kind": "pkg", "parent": "corelib", "depth": 1},
        {"id": "corelib/ratelimit/ratelimit_config", "label": "ratelimit_config", "kind": "pkg",
         "parent": "corelib/ratelimit", "depth": 2},
        {"id": "corelib/ratelimit:Foo", "label": "Foo", "kind": "symbol", "parent": "corelib/ratelimit",
         "depth": 2, "state": "new", "file": "corelib/ratelimit/x.go"},
        {"id": "corelib/ratelimit/ratelimit_config:Bar", "label": "Bar", "kind": "symbol",
         "parent": "corelib/ratelimit/ratelimit_config", "depth": 3, "state": "new",
         "file": "corelib/ratelimit/ratelimit_config/y.go"},
    ]
    ids = {"corelib/ratelimit:Foo", "corelib/ratelimit/ratelimit_config:Bar"}

    text, _ = _mermaid_symbols(nodes, ids, [])

    assert 'subgraph G0["corelib"]' in text
    assert 'subgraph G1["ratelimit"]' in text  # own label only, not "corelib/ratelimit"
    assert 'subgraph G2["ratelimit_config"]' in text
    assert '["Foo"]' in text and '["Bar"]' in text
    lines = text.splitlines()
    ratelimit_open = next(i for i, l in enumerate(lines) if 'subgraph G1["ratelimit"]' in l)
    foo_line = next(i for i, l in enumerate(lines) if '["Foo"]' in l)
    config_open = next(i for i, l in enumerate(lines) if 'subgraph G2["ratelimit_config"]' in l)
    # Foo is ratelimit's own symbol, drawn directly inside ratelimit's box rather than a further subgraph.
    assert ratelimit_open < foo_line < config_open


def test_mermaid_symbols_keeps_a_symbol_whose_parent_is_unknown_or_missing():
    # symdelta.py always gives a symbol either a real package parent or None, but sections.py is
    # a standalone CLI too; neither case should silently lose the symbol's own box.
    nodes = [
        {"id": "a:Root", "label": "Root", "kind": "symbol", "parent": None, "depth": 0,
         "state": "new", "file": "a.go"},
        {"id": "a:Stray", "label": "Stray", "kind": "symbol", "parent": "no_such_pkg", "depth": 0,
         "state": "new", "file": "b.go"},
    ]
    ids = {"a:Root", "a:Stray"}

    text, _ = _mermaid_symbols(nodes, ids, [])

    assert '["Root"]' in text and '["Stray"]' in text
    assert "no_such_pkg" in text  # unknown parent shown as itself, not collapsed onto "(root)"
    # two distinct boxes, not one shared "(root)" that would hide which symbol belongs where
    assert text.count("subgraph") == 2


def test_connected_components_orders_by_lowest_id_and_uses_it_as_the_representative():
    # b-c is one component (lowest id "b"); a and d are singletons -- three components in all,
    # ordered by each one's own lowest id.
    reps = _connected_components({"d", "a", "c", "b"}, [("b", "c")])

    assert reps == ["a", "b", "d"]


def test_connected_components_single_component_returns_just_its_one_representative():
    assert _connected_components({"x", "y"}, [("x", "y")]) == ["x"]


def test_mermaid_packages_chains_disconnected_packages_with_an_invisible_link():
    # No edges roll up between "a" and "b" at all, so level 1 would otherwise draw them as two
    # components side by side -- the same failure mode fixed for levels 2/3 below. Each package
    # owns a symbol of its own, as a real leaf package always does (see the container-only test
    # below for the case where one doesn't).
    nodes = [
        {"id": "a", "label": "a", "kind": "pkg", "parent": None, "depth": 0},
        {"id": "b", "label": "b", "kind": "pkg", "parent": None, "depth": 0},
        {"id": "a:F", "label": "F", "kind": "symbol", "parent": "a", "depth": 1,
         "state": "new", "file": "a/x.go"},
        {"id": "b:G", "label": "G", "kind": "symbol", "parent": "b", "depth": 1,
         "state": "new", "file": "b/y.go"},
    ]

    text, _ = _mermaid_packages(nodes, [])

    assert "P0 ~~~ P1" in text


def test_mermaid_packages_never_chains_a_container_to_its_own_nested_child():
    # "root" owns no symbol of its own -- a pure container -- so without excluding it, it would
    # be its own singleton component, chained to its own nested child. Real PR data hit this.
    nodes = [
        {"id": "root", "label": "root", "kind": "pkg", "parent": None, "depth": 0},
        {"id": "root/leaf", "label": "leaf", "kind": "pkg", "parent": "root", "depth": 1},
        {"id": "root/leaf:F", "label": "F", "kind": "symbol", "parent": "root/leaf", "depth": 1,
         "state": "new", "file": "root/leaf/x.go"},
    ]

    text, _ = _mermaid_packages(nodes, [])

    assert "~~~" not in text


def test_mermaid_packages_never_chains_an_owning_package_to_its_own_owning_child():
    # p0 owns A directly and also has child p1, which owns B -- unlike the pure-container case
    # above, p0 isn't excluded from the node set, so without merging it with its owning
    # descendant first, the two land in separate components and hit the same defect.
    nodes = [
        {"id": "p0", "label": "p0", "kind": "pkg", "parent": None, "depth": 0},
        {"id": "p1", "label": "p1", "kind": "pkg", "parent": "p0", "depth": 1},
        {"id": "p0:A", "label": "A", "kind": "symbol", "parent": "p0", "depth": 0,
         "state": "new", "file": "p0/a.go"},
        {"id": "p1:B", "label": "B", "kind": "symbol", "parent": "p1", "depth": 1,
         "state": "new", "file": "p1/b.go"},
    ]

    text, _ = _mermaid_packages(nodes, [])

    assert "~~~" not in text


def test_mermaid_packages_single_component_has_no_invisible_link():
    text, _ = _mermaid_packages(SYMDELTA["nodes"], SYMDELTA["edges"])

    assert "~~~" not in text


def test_mermaid_symbols_chains_disconnected_clusters_within_one_subgraph():
    # X-Y and Z-W are two separate pairs, both filed under the same package box "a" -- dagre
    # treats them as disconnected components regardless of sharing a subgraph, and used to lay
    # them out side by side just like top-level components would be.
    nodes = [{"id": "a", "label": "a", "kind": "pkg", "parent": None, "depth": 0}] + [
        {"id": f"a:{n}", "label": n, "kind": "symbol", "parent": "a", "depth": 1, "state": "new",
         "file": "a/x.go"}
        for n in ("X", "Y", "Z", "W")
    ]
    edges = [
        {"id": "e0", "source": "a:X", "target": "a:Y", "state": "new"},
        {"id": "e1", "source": "a:Z", "target": "a:W", "state": "new"},
    ]
    ids = {"a:X", "a:Y", "a:Z", "a:W"}

    text, _ = _mermaid_symbols(nodes, ids, edges)

    assert text.count("subgraph") == 1  # one shared box, two components inside it
    assert "S0 ~~~ S1" in text  # component {W,Z} (min id W -> S0) chained to {X,Y} (min id X -> S1)
    lines = text.splitlines()
    chain_line = next(i for i, l in enumerate(lines) if "~~~" in l)
    last_linkstyle = max(i for i, l in enumerate(lines) if l.strip().startswith("linkStyle"))
    assert chain_line > last_linkstyle  # invisible link is emitted after every real link


def test_mermaid_symbols_invisible_links_are_never_indexed_by_linkstyle():
    ids, edges = _symbols_scope(SYMDELTA["nodes"], SYMDELTA["edges"])
    text, _ = _mermaid_symbols(SYMDELTA["nodes"], ids, edges)

    linkstyle_lines = [l for l in text.splitlines() if l.strip().startswith("linkStyle")]
    # both real links (e0, g0) get a style; an invisible chain link would shift these if it
    # were emitted among them instead of strictly after.
    assert linkstyle_lines == [
        f"  linkStyle 0 stroke:{LINK_COLOR_NEW},stroke-width:2px;",
        f"  linkStyle 1 stroke:{LINK_COLOR_GONE},stroke-dasharray:4 4;",
    ]


def test_symbols_chain_links_are_deterministic_regardless_of_symbol_id_set_order():
    # Same bug class as test_new_and_gone_class_lists_are_declaration_order_not_set_order, but
    # for the invisible-link chain: three disconnected pairs, fed in as a set whose own
    # iteration order isn't guaranteed to match insertion order across runs.
    nodes = [{"id": "a", "label": "a", "kind": "pkg", "parent": None, "depth": 0}] + [
        {"id": f"a:N{i}", "label": f"N{i}", "kind": "symbol", "parent": "a", "depth": 1,
         "state": "new", "file": "a/x.go"}
        for i in range(6)
    ]
    edges = [
        {"id": "e0", "source": "a:N0", "target": "a:N1", "state": "new"},
        {"id": "e1", "source": "a:N2", "target": "a:N3", "state": "new"},
        {"id": "e2", "source": "a:N4", "target": "a:N5", "state": "new"},
    ]
    ids = {n["id"] for n in nodes if n["kind"] == "symbol"}

    text, _ = _mermaid_symbols(nodes, ids, edges)

    chain_lines = [l.strip() for l in text.splitlines() if "~~~" in l]
    assert chain_lines == ["S0 ~~~ S2", "S2 ~~~ S4"]


# ---- markdown symbols output, sections.py --kind symbols --format md ------------------------

def test_symbols_md_returns_nothing_when_there_are_no_nodes():
    assert render_symbols_md({"nodes": []}) == ""
    assert render_symbols_md({}) == ""


def test_symbols_md_has_marker_two_fenced_blocks_and_no_html():
    out = render_symbols_md(SYMDELTA)

    assert out.startswith("<!-- visual-diff:symbols -->")
    assert "### Changes visualization" in out
    assert out.count("```mermaid") == 2
    assert "**Packages**" in out and "**Symbols**" in out
    assert "flowchart LR" in out
    assert "data-ids" not in out  # that attribute is an HTML-page mechanism only
    assert "<div" not in out and "<li>" not in out and "<ul" not in out


def test_symbols_md_and_html_share_the_same_note_text():
    note = "Showing 5 packages and symbols, 2 relations between them."

    assert note in render_symbols(SYMDELTA)  # inside the escaped/codeified <p class="note">
    assert note in render_symbols_md(SYMDELTA)  # verbatim, markdown never HTML-escapes


def test_symbols_md_orphans_render_as_a_plain_bullet_not_a_list_tag():
    out = render_symbols_md(SYMDELTA_WITH_ORPHAN)

    assert "- b: Lonely (new)" in out
    assert "<li>" not in out and "<ul" not in out


def test_symbols_md_moved_renders_as_a_plain_bullet_too():
    out = render_symbols_md(SYMDELTA)

    assert "* 2 call sites moved, a/old -> b" in out
    assert "&gt;" not in out  # markdown never HTML-escapes, unlike the html arrow in the <li>


def test_symbols_md_orphan_and_moved_bullets_use_different_markers_so_they_stay_two_lists():
    # A blank line alone doesn't start a new CommonMark list, only a bullet-marker change does --
    # SYMDELTA_WITH_ORPHAN carries both an orphaned symbol and a moved one, which mean different
    # things (see render_symbols_md), so they must render as two lists, not one merged list.
    out = render_symbols_md(SYMDELTA_WITH_ORPHAN)

    orphan_marker = next(l for l in out.splitlines() if "Lonely" in l)[0]
    moved_marker = next(l for l in out.splitlines() if "call sites moved" in l)[0]
    assert orphan_marker in "-*" and moved_marker in "-*"
    assert orphan_marker != moved_marker


def test_symbols_md_mermaid_source_carries_a_classdef_for_every_class_it_applies():
    # Derived from the emitted source itself, not a hardcoded "classDef new"/"classDef gone"
    # string: whichever states a level's `class <ids> <state>` line actually applies must each
    # get a matching `classDef <state>`, or GitHub renders that state's nodes uncoloured while
    # the legend's caption still claims otherwise (see _symbols_legend_text).
    out = render_symbols_md(SYMDELTA)
    found_any = False
    for block in out.split("```mermaid")[1:]:
        source = block.split("```", 1)[0]
        applied = set(re.findall(r"^\s*class \S+ (\S+)$", source, re.MULTILINE))
        defined = set(re.findall(r"^\s*classDef (\S+) ", source, re.MULTILINE))
        assert applied <= defined
        found_any = found_any or bool(applied)
    assert found_any  # otherwise this test would pass even if classDef were never emitted


def test_symbols_cli_supports_markdown_format():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "symdelta.json"
        path.write_text(json.dumps(SYMDELTA))
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "sections.py"),
             "--kind", "symbols", "--format", "md", "--data", str(path)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("<!-- visual-diff:symbols -->")


def test_symbols_cli_rejects_a_kind_other_than_symbols():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "x.json"
        path.write_text("{}")
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "sections.py"),
             "--kind", "coupling", "--format", "md", "--data", str(path)],
            capture_output=True, text=True,
        )
        assert result.returncode != 0


def test_the_legend_rides_with_the_level_that_actually_draws_a_new_or_gone_node():
    # It used to be one static row under the heading, explaining both states even on a package
    # view that has neither on screen.
    out = render_symbols(SYMDELTA)

    heading = out.split('class="h2-row"')[1].split("</h2>")[0]
    assert "sw-new" not in heading  # the package level draws no new/gone node
    level2 = out.split('data-level="2" hidden', 1)[1].split(">", 1)[0]
    assert "sw-new" in level2 and "sw-gone" in level2


if __name__ == "__main__":
    tests = [
        test_symbols_returns_nothing_when_there_are_no_nodes,
        test_symbols_html_has_marker_legend_slider_and_three_levels,
        test_symbols_html_never_emits_classdef,
        test_symbols_note_is_the_counts_sentence_plus_drawn_and_listed,
        test_symbols_note_names_the_undrawn_containment_count_when_nonzero,
        test_symbols_moved_renders_as_prose_not_a_graph_node,
        test_mermaid_packages_level1_never_draws_a_dashed_move_edge,
        test_symbols_orphan_symbol_is_listed_instead_of_drawn,
        test_symbols_orphans_helper_groups_by_package_and_marks_new_gone,
        test_symbols_orphans_is_empty_string_when_nothing_is_held_back,
        test_edge_endpoint_ids_collects_both_sides_of_every_edge,
        test_mermaid_symbols_for_level_returns_a_placeholder_node_when_nothing_qualifies,
        test_mermaid_symbols_for_level_draws_normally_when_something_qualifies,
        test_symbols_all_orphaned_level_gets_a_placeholder_not_a_degenerate_diagram,
        test_mermaid_symbols_id_map_keys_symbol_nodes_not_package_boxes,
        test_symbols_html_level1_data_ids_is_always_empty,
        test_symbols_html_level2_data_ids_maps_symbol_nodes_not_package_boxes,
        test_symbols_html_data_ids_survives_a_quote_in_a_file_path,
        test_mm_escape_strips_parens_quotes_and_backticks,
        test_wrap_label_breaks_a_long_multiword_label_at_spaces,
        test_wrap_label_breaks_a_single_long_camelcase_word_at_its_boundaries,
        test_wrap_label_keeps_going_around_a_long_camelcase_word_between_short_ones,
        test_wrap_label_keeps_going_rather_than_truncating_a_long_sentence,
        test_wrap_label_empty_label_is_unchanged,
        test_wrap_label_breaks_a_package_path_at_slashes,
        test_wrap_label_falls_back_to_underscore_when_a_segment_alone_is_too_long,
        test_wrap_label_leaves_a_short_package_path_untouched,
        test_wrap_label_breaks_the_real_clipped_go_symbol_names,
        test_wrap_label_breaks_a_dotted_selector_after_the_dot,
        test_wrap_label_keeps_an_acronym_run_together_when_camel_splitting,
        test_wrap_label_all_caps_word_with_no_boundary_is_returned_whole,
        test_changed_symbol_ids_excludes_the_moved_state,
        test_symbols_scope_pulls_in_the_context_endpoint_of_a_changed_edge,
        test_mermaid_packages_rolls_up_cross_package_edges_with_a_count_label,
        test_mermaid_packages_nests_child_packages_inside_their_parent_box,
        test_mermaid_packages_does_not_draw_a_containment_edge_and_reports_its_count,
        test_mermaid_symbols_wraps_a_long_label_without_touching_ids_arrows_or_classdef,
        test_mermaid_symbols_appends_a_was_line_for_a_renamed_symbol,
        test_mermaid_symbols_label_has_no_was_line_when_the_name_did_not_change,
        test_symbols_legend_ignores_the_word_new_in_a_label_not_a_class_line,
        test_new_and_gone_class_lists_are_declaration_order_not_set_order,
        test_mermaid_symbols_subgraphs_by_package_and_classes_new_gone_but_not_changed,
        test_mermaid_symbols_omits_classdef_by_default_html_shape,
        test_mermaid_symbols_own_symbols_sit_inside_a_box_that_also_nests_children,
        test_mermaid_symbols_keeps_a_symbol_whose_parent_is_unknown_or_missing,
        test_connected_components_orders_by_lowest_id_and_uses_it_as_the_representative,
        test_connected_components_single_component_returns_just_its_one_representative,
        test_mermaid_packages_chains_disconnected_packages_with_an_invisible_link,
        test_mermaid_packages_never_chains_a_container_to_its_own_nested_child,
        test_mermaid_packages_never_chains_an_owning_package_to_its_own_owning_child,
        test_mermaid_packages_single_component_has_no_invisible_link,
        test_mermaid_symbols_chains_disconnected_clusters_within_one_subgraph,
        test_mermaid_symbols_invisible_links_are_never_indexed_by_linkstyle,
        test_symbols_chain_links_are_deterministic_regardless_of_symbol_id_set_order,
        test_symbols_md_returns_nothing_when_there_are_no_nodes,
        test_symbols_md_has_marker_two_fenced_blocks_and_no_html,
        test_symbols_md_and_html_share_the_same_note_text,
        test_symbols_md_orphans_render_as_a_plain_bullet_not_a_list_tag,
        test_symbols_md_moved_renders_as_a_plain_bullet_too,
        test_symbols_md_orphan_and_moved_bullets_use_different_markers_so_they_stay_two_lists,
        test_symbols_md_mermaid_source_carries_a_classdef_for_every_class_it_applies,
        test_symbols_cli_supports_markdown_format,
        test_symbols_cli_rejects_a_kind_other_than_symbols,
        test_the_legend_rides_with_the_level_that_actually_draws_a_new_or_gone_node,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
