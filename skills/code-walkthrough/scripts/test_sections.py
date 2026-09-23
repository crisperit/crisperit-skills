#!/usr/bin/env python3
"""Self-check for sections.py. Assert-based, no framework."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sections import (  # noqa: E402
    LABEL_WRAP_TARGET,
    STRUCTURE_MARKER,
    ZWSP,
    LINK_COLOR_GONE,
    LINK_COLOR_NEW,
    _changed_symbol_ids,
    _connected_components,
    _edge_endpoint_ids,
    _mermaid_packages,
    _mermaid_symbols,
    _mermaid_symbols_for_level,
    _mm_escape,
    _scope_to_paths,
    _symbols_legend,
    _symbols_orphans,
    _symbols_scope,
    _structure_implements_edges,
    render_structure,
    render_symbols,
    wrap_label,
)
from walkthrough import _group_color  # noqa: E402  the colours render_structure must match

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


def test_scope_to_paths_keeps_the_far_end_of_an_edge_that_leaves_the_scope():
    nodes, edges = _scope_to_paths(SYMDELTA["nodes"], SYMDELTA["edges"], ["a"])
    ids = {n["id"] for n in nodes}
    # b:Moved sits outside a/, and is kept only because a:New calls it; its package comes with it.
    assert ids == {"a", "b", "a:New", "a:Gone", "b:Moved"}
    assert len(edges) == 2


def test_scope_to_paths_drops_what_no_edge_reaches():
    data = {
        "nodes": SYMDELTA["nodes"] + [
            {"id": "c", "label": "c", "kind": "pkg", "parent": None, "depth": 0},
            {"id": "c:Far", "label": "Far", "kind": "symbol", "parent": "c", "depth": 1,
             "state": "new", "file": "c/z.go"},
        ],
        "edges": SYMDELTA["edges"],
    }
    nodes, _ = _scope_to_paths(data["nodes"], data["edges"], ["a"])
    assert {n["id"] for n in nodes} == {"a", "b", "a:New", "a:Gone", "b:Moved"}


def test_scope_to_paths_matches_on_a_path_segment_not_a_string_prefix():
    nodes, _ = _scope_to_paths(SYMDELTA["nodes"], SYMDELTA["edges"], ["a/x.go"])
    assert "a:New" in {n["id"] for n in nodes}
    nodes, edges = _scope_to_paths(SYMDELTA["nodes"], SYMDELTA["edges"], ["a/x"])
    assert nodes == [] and edges == []


def test_scope_to_paths_without_paths_changes_nothing():
    nodes, edges = _scope_to_paths(SYMDELTA["nodes"], SYMDELTA["edges"], [])
    assert nodes is SYMDELTA["nodes"] and edges is SYMDELTA["edges"]


def test_scope_to_paths_dot_means_whole_repo():
    # "." and "./" both normalize to an empty prefix and win even when paired with a
    # narrower entry in the same list -- see _scope_to_paths's docstring for why.
    for dot_paths in (["."], ["./"]):
        nodes, edges = _scope_to_paths(SYMDELTA["nodes"], SYMDELTA["edges"], dot_paths)
        assert nodes is SYMDELTA["nodes"] and edges is SYMDELTA["edges"]

    nodes, edges = _scope_to_paths(SYMDELTA["nodes"], SYMDELTA["edges"], [".", "a"])
    assert nodes is SYMDELTA["nodes"] and edges is SYMDELTA["edges"]


def test_symbols_with_dot_scope_renders_a_nonempty_section():
    out = render_symbols(SYMDELTA, paths=["."])
    assert out != ""


def test_symbols_scoped_out_of_existence_renders_no_section():
    assert render_symbols(SYMDELTA, paths=["nowhere"]) == ""


def test_symbols_opens_on_symbols_and_on_packages_once_it_is_oversized():
    # The default-level flip is an inline-only concern now: a group's own symbols level can
    # still be too big to open unasked, even though the page-level graph never draws one at all.
    out = render_symbols(SYMDELTA, inline=True)
    assert 'data-default="2"' in out
    assert '<div class="mermaid" data-level="1" hidden ' in out

    big = _oversized_delta()
    out = render_symbols(big, explain=True, inline=True)
    assert 'data-default="1"' in out
    assert '<div class="mermaid" data-level="2" hidden ' in out
    assert "too many to open unasked" in out


def _oversized_delta():
    """One package of MAX_SYMBOL_NODES+2 symbols, each with an edge so none is held back as an
    orphan -- the node budget counts drawn boxes, not changed symbols."""
    from sections import MAX_SYMBOL_NODES
    nodes = [{"id": "p", "label": "p", "kind": "pkg", "parent": None, "depth": 0}]
    edges = []
    for i in range(MAX_SYMBOL_NODES + 2):
        nodes.append({"id": f"p:S{i}", "label": f"S{i}", "kind": "symbol", "parent": "p",
                      "depth": 1, "state": "new", "file": "p/x.go"})
        if i:
            edges.append({"id": f"e{i}", "source": "p:S0", "target": f"p:S{i}", "state": "new"})
    return {"nodes": nodes, "edges": edges}


def test_symbols_returns_nothing_when_there_are_no_nodes():
    assert render_symbols({"nodes": []}) == ""
    assert render_symbols({}) == ""


def test_symbols_null_language_shows_a_note_with_reason_and_remedy():
    data = {"language": None, "reason": "typescript-language-server not found on PATH",
            "remedy": "npm i -g typescript typescript-language-server"}
    out = render_symbols(data)
    assert out.startswith("<!-- code-walkthrough:symbols -->")
    assert "typescript-language-server not found on PATH" in out
    assert "npm i -g typescript typescript-language-server" in out
    assert "--doctor" in out
    assert '<p class="note">' in out


def test_symbols_null_language_shows_a_note_with_no_remedy():
    # A dependency-incompatibility refusal has no remedy and nothing to do with
    # language-server setup, so the --doctor pointer (which only checks that) is dropped too.
    data = {"language": None,
            "reason": "package.json or a JS lockfile changed between base and head"}
    out = render_symbols(data)
    assert "package.json or a JS lockfile changed between base and head" in out
    assert "Fix:" not in out
    assert "--doctor" not in out


def test_symbols_null_language_note_escapes_reason_and_remedy():
    data = {"language": None, "reason": "found <script>alert(1)</script> & broke",
            "remedy": 'export PATH="a<b>c&d:$PATH"'}
    out = render_symbols(data)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "a&lt;b&gt;c&amp;d" in out


def test_symbols_no_supported_language_reads_as_a_fact_not_a_fix():
    data = {"language": None,
            "reason": "no supported files (.go, .py, .rs, .ts, .tsx) changed between a and b"}
    out = render_symbols(data)
    assert out.startswith("<!-- code-walkthrough:symbols -->")
    assert "No call graph:" in out
    assert "Fix:" not in out
    assert "--doctor" not in out


def test_symbols_null_language_inline_group_tab_stays_empty():
    # A group's own call-graph tab just doesn't exist when there's nothing to show; the
    # page-level note above already explained why, once.
    data = {"language": None, "reason": "boom", "remedy": "fix it"}
    assert render_symbols(data, inline=True) == ""


def test_symbols_llm_resolver_shows_the_caveat_page_level_and_inline():
    # resolver == "llm" is a graph that IS shown, unlike language: None above; the caveat marks
    # it as inferred rather than compiler-resolved instead of hiding it.
    data = {**SYMDELTA, "resolver": "llm"}
    for out in (render_symbols(data), render_symbols(data, inline=True)):
        assert '<p class="note">' in out
        assert "inferred" in out and "not resolved by a compiler" in out


def test_symbols_mechanical_resolver_has_no_llm_caveat():
    data = {**SYMDELTA, "resolver": "go/packages"}
    assert "inferred" not in render_symbols(data)
    assert "inferred" not in render_symbols(data, inline=True)


def test_symbols_page_level_has_marker_heading_and_packages_only():
    # No toggle, no symbols level: that detail now lives per group instead (inline=True below).
    out = render_symbols(SYMDELTA)

    assert out.startswith("<!-- code-walkthrough:symbols -->")
    assert out.count('<div class="mermaid"') == 1
    assert '<h2>Changes visualization</h2>' in out
    assert "vd-level-toggle" not in out
    assert "data-level" not in out
    assert "data-default" not in out
    assert "vd-legend" not in out
    assert "flowchart LR" in out
    assert "flowchart TB" not in out


def test_symbols_inline_has_legend_slider_and_two_levels():
    out = render_symbols(SYMDELTA, inline=True)

    assert "<!-- code-walkthrough:symbols -->" not in out
    # Two levels, so a toggle: it opens on symbols and its label names that, not the switch.
    # A class, not an id: a per-group graph puts more than one toggle on the page.
    assert 'class="vd-level-toggle"' in out
    assert 'id="vd-level-toggle"' not in out
    assert ">symbols</button>" in out
    assert 'aria-label="Detail level: symbols. Activate to show packages."' in out
    assert "aria-pressed" not in out
    assert 'data-names="' in out and "Symbols + callers" not in out
    assert "vd-level-name" not in out  # the slider's caption went with it
    assert '<div class="mermaid" data-level="1" hidden' in out
    assert '<div class="mermaid" data-level="2" ' in out
    assert 'data-level="3"' not in out
    assert "flowchart LR" in out
    assert "flowchart TB" not in out


def test_symbols_html_never_emits_classdef():
    # Regression guard: classDef used to leak into the HTML page and override its own light/dark
    # node theming (see the comment above LINK_COLOR_NEW/GONE in sections.py). inline=True to
    # cover both levels' source in one pass.
    out = render_symbols(SYMDELTA, inline=True)

    assert "classDef" not in out


def test_symbols_inline_has_no_page_marker_heading_or_details_wrapper():
    out = render_symbols(SYMDELTA, paths=["a"], inline=True)

    assert "<!-- code-walkthrough:symbols -->" not in out
    assert "<h2" not in out
    assert "<details" not in out
    assert "<summary" not in out
    assert '<div class="vd-symbols vd-symbols-group" data-symbols="2" data-default="2">' in out
    assert '<div class="vd-ctl">' in out
    # Both levels still render; only the frame around them differs from the global section.
    assert '<div class="mermaid" data-level="1" hidden' in out
    assert '<div class="mermaid" data-level="2" ' in out
    assert out.rstrip("\n").endswith("</div>")


def test_symbols_inline_toggle_is_a_class_not_an_id():
    out = render_symbols(SYMDELTA, paths=["a"], inline=True)

    assert 'class="vd-level-toggle"' in out
    assert 'id="vd-level-toggle"' not in out


def test_symbols_inline_data_symbols_is_the_changed_symbol_count_in_scope():
    # a:New and a:Gone are the changed symbols in scope; b:Moved is pulled in as edge context
    # but never counts, since only a state of new/gone makes a symbol part of the delta.
    out = render_symbols(SYMDELTA, paths=["a"], inline=True)

    assert 'data-symbols="2"' in out


def test_symbols_note_is_the_counts_sentence_plus_drawn_and_listed():
    # a and b are both top-level packages -- no containment relationship between them -- so
    # undrawn_count is 0 here and the note says nothing about it (see the nonzero case below).
    # A group's symbols level, hence inline=True: the page-level note carries no drawn/listed
    # split at all (see test_symbols_page_level_note_has_no_drawn_listed_split below).
    out = render_symbols(SYMDELTA, inline=True)

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

    out = render_symbols(data, inline=True)

    note = out.split('<p class="note">', 1)[1].split("</p>", 1)[0]
    assert (
        "1 relations between a package and a package inside it are not drawn because the "
        "containment box already shows them." in note
    )
    level1 = out.split('data-level="1"', 1)[1].split(">", 1)[1].split("</div>", 1)[0]
    assert "×" not in level1


def test_symbols_page_level_note_has_no_drawn_listed_split():
    # The page-level graph never holds a symbol back as an orphan -- it never draws a symbol at
    # all -- so its note is just the packages/relations count, plus the same undrawn-containment
    # sentence _mermaid_packages itself can report.
    nodes = SYMDELTA["nodes"] + [
        {"id": "a/inner", "label": "inner", "kind": "pkg", "parent": "a", "depth": 1},
        {"id": "a/inner:D", "label": "D", "kind": "symbol", "parent": "a/inner", "depth": 1,
         "state": "new", "file": "a/inner/d.go"},
    ]
    edges = SYMDELTA["edges"] + [{"id": "e1", "source": "a:New", "target": "a/inner:D", "state": "new"}]
    data = {**SYMDELTA, "nodes": nodes, "edges": edges}

    out = render_symbols(data)

    note = out.split('<p class="note">', 1)[1].split("</p>", 1)[0]
    assert note == (
        "Showing 3 packages, 3 relations between them. "
        "1 relations between a package and a package inside it are not drawn because the "
        "containment box already shows them."
    )


def test_symbols_moved_renders_as_prose_not_a_graph_node():
    # The `vd-moved` prose list is the sole channel for a package-level move: a package-level
    # move edge was tried on the packages diagram and reverted because it broke the containment
    # layout (see render_symbols).
    out = render_symbols(SYMDELTA)

    assert '<ul class="vd-moved">' in out
    assert "2 call sites moved, a/old -&gt; b" in out
    diagram = out.split('<div class="mermaid"', 1)[1].split(">", 1)[1].split("</div>", 1)[0]
    assert "call sites moved" not in diagram


def test_mermaid_packages_level1_never_draws_a_dashed_move_edge():
    # Regression guard for the reverted feature: SYMDELTA carries a non-empty "moved" tally, and
    # render_symbols must not feed it into _mermaid_packages at all, let alone as a `-.->` edge.
    out = render_symbols(SYMDELTA)

    diagram = out.split('<div class="mermaid"', 1)[1].split(">", 1)[1].split("</div>", 1)[0]
    assert "-.->" not in diagram


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
    # out to thousands of pixels wide on a real PR. A group's own symbols level, hence
    # inline=True: the page-level graph never draws or lists individual symbols at all.
    out = render_symbols(SYMDELTA_WITH_ORPHAN, inline=True)

    level2 = out.split('data-level="2"', 1)[1].split("</div>", 1)[0]
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
    # b:Lonely is the only changed symbol and has no edge at all, so the symbols level ends up
    # with nothing to draw -- exactly the all-orphaned case the placeholder exists for. A
    # group's own symbols level, hence inline=True.
    data = {
        "nodes": [
            {"id": "b", "label": "b", "kind": "pkg", "parent": None, "depth": 0},
            {"id": "b:Lonely", "label": "Lonely", "kind": "symbol", "parent": "b", "depth": 1,
             "state": "new", "file": "b/z.go"},
        ],
        "edges": [],
    }

    out = render_symbols(data, inline=True)

    level2 = out.split('data-level="2"', 1)[1].split("</div>", 1)[0]
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


def test_symbols_html_level1_data_ids_maps_package_boxes_to_their_directory():
    # A package box has no file of its own, but it does have a path, which is what the node
    # menu resolves to the package's first hunk in the walkthrough. The page-level graph is
    # packages only, so its one diagram carries this map directly, no data-level split needed.
    out = render_symbols({"nodes": _TWO_LINKED_SYMBOLS, "edges": _ONE_EDGE})

    attr = out.split('data-ids="', 1)[1].split('"', 1)[0]
    ids = json.loads(attr.replace("&quot;", '"'))
    assert set(ids.values()) == {"a"}
    assert all(k.startswith("P") for k in ids)


def test_symbols_html_level2_data_ids_maps_symbol_nodes_not_package_boxes():
    # A group's own symbols level, hence inline=True.
    out = render_symbols({"nodes": _TWO_LINKED_SYMBOLS, "edges": _ONE_EDGE}, inline=True)

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
    out = render_symbols({"nodes": nodes, "edges": _ONE_EDGE}, inline=True)

    attr = out.split('data-level="2"', 1)[1].split('data-ids="', 1)[1].split('"', 1)[0]
    assert '"' not in attr  # every quote became &quot;, so none can end the attribute early
    ids = json.loads(attr.replace("&quot;", '"'))
    assert 'weird"path.go' in ids.values()


def test_mm_escape_strips_parens_quotes_and_backticks():
    assert _mm_escape('Foo("bar") `baz`') == "Foobar baz"


def test_wrap_label_leaves_a_long_multiword_label_alone():
    # mermaid's own label div wraps on spaces, so a label with no over-length word needs
    # nothing done to it -- and must come back byte for byte, not respaced.
    label = "NewRequest sets Request User Id to usrID for the MediaGuard lookup"

    assert wrap_label(label) == label


def test_wrap_label_breaks_a_single_long_camelcase_word_at_its_boundaries():
    # No '/', '_' or '.' to break on -- only camelCase boundaries are left, which is exactly the
    # fallback path this exercises.
    identifier = "filterUnusableIdentifierThatIsVeryLongIndeedYes"

    out = wrap_label(identifier)

    assert ZWSP in out
    assert out.replace(ZWSP, "") == identifier  # every character survives, none lost


def test_wrap_label_keeps_going_around_a_long_camelcase_word_between_short_ones():
    out = wrap_label("short filterUnusableIdentifierThatIsVeryLongIndeedYes done")
    words = out.split(" ")

    assert words[0] == "short" and words[-1] == "done"  # short words untouched
    assert ZWSP in words[1]
    assert words[1].replace(ZWSP, "") == "filterUnusableIdentifierThatIsVeryLongIndeedYes"


def test_wrap_label_keeps_going_rather_than_truncating_a_long_sentence():
    # The 25-word priority-chain sentence from the real bug report, trimmed to keep the test
    # readable. Longer than four lines at the default target -- a taller node beats losing text.
    sentence = ("Resolve priority chain IFA for app then SyncID cookie then "
                "User BuyerUID then User ID then Pubcid then IFA fallback for site")

    assert wrap_label(sentence) == sentence  # not one word over the target, so not one cut


def test_wrap_label_empty_label_is_unchanged():
    assert wrap_label("") == ""


def test_wrap_label_breaks_a_package_path_at_slashes():
    label = "corelib/ratelimit/buckets"  # 25 chars, one over the 24-char default target

    out = wrap_label(label)

    assert ZWSP in out
    assert out.replace(ZWSP, "") == label  # slash stays on the end of the earlier piece
    assert out.split(ZWSP)[0].endswith("/")


def test_wrap_label_falls_back_to_underscore_when_a_segment_alone_is_too_long():
    # No slash makes any given piece short enough on its own, so '_' has to do the rest.
    label = "a_very_long_snake_case_package_name"

    out = wrap_label(label, target=12)

    assert ZWSP in out
    assert out.replace(ZWSP, "") == label
    assert all(len(piece) <= 12 for piece in out.split(ZWSP))


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
        pieces = out.split(ZWSP)

        assert "".join(pieces) == label  # no piece ever loses a character
        assert all(len(piece) <= LABEL_WRAP_TARGET for piece in pieces), label


def test_wrap_label_breaks_a_dotted_selector_after_the_dot():
    out = wrap_label("MediaGuardGRPCDecorator.Lookup")

    assert out.split(ZWSP)[0].endswith(".")  # dot stays on the end of the earlier piece, like '/'


def test_wrap_label_keeps_an_acronym_run_together_when_camel_splitting():
    # GRPC must survive as one piece, not get shredded letter by letter, once there's no
    # separator left and camelCase boundaries are all that's left to break on.
    out = wrap_label("MediaGuardGRPCDecorator", target=12)

    assert out.replace(ZWSP, "") == "MediaGuardGRPCDecorator"
    assert "GRPC" in out.split(ZWSP)


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
    text, undrawn, _ = _mermaid_packages(SYMDELTA["nodes"], SYMDELTA["edges"])

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

    text, undrawn, _ = _mermaid_packages(nodes, [])

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

    text, undrawn, _ = _mermaid_packages(nodes, edges)

    assert '["inner"]' in text  # just the package's own segment name
    assert "×" not in text
    assert text.count("-->") == 1  # only outer -> sib is a real, drawable edge
    assert "|1|" in text  # the surviving edge still carries its own count
    assert undrawn == 1  # outer -> outer/inner, skipped rather than drawn


def test_mermaid_symbols_keeps_a_long_label_in_one_pair_of_quotes():
    nodes = SYMDELTA["nodes"] + [
        {"id": "a:Filter", "label": "filters unusable identifiers before they reach the exchange",
         "kind": "symbol", "parent": "a", "depth": 1, "state": "new", "file": "a/z.go"},
    ]
    ids, edges = _symbols_scope(nodes, SYMDELTA["edges"])
    ids = ids | {"a:Filter"}

    text, _ = _mermaid_symbols(nodes, ids, edges)
    label_line = next(line for line in text.splitlines() if "filters" in line)

    assert label_line.count('"') == 2  # one label, one pair of quotes
    assert label_line.split('"')[1] == (
        "filters unusable identifiers before they reach the exchange")
    # everything else about the diagram is exactly what it was without the long label
    assert "var(" not in text  # mermaid's own parser can't resolve a CSS var()
    assert "class " in text
    assert text.count("-->") == len(edges)


def test_mermaid_symbols_appends_the_old_name_for_a_renamed_symbol():
    nodes = SYMDELTA["nodes"] + [
        {"id": "a:IncrementChecksTotal", "label": "IncrementChecksTotal", "kind": "symbol",
         "parent": "a", "depth": 1, "state": "changed", "file": "a/x.go",
         "was": "incrementChecksTotal"},
    ]

    text, _ = _mermaid_symbols(nodes, {"a:IncrementChecksTotal"}, [])
    label_line = next(line for line in text.splitlines() if "IncrementChecksTotal" in line)

    assert label_line.count('"') == 2  # still one label, one pair of quotes
    assert "IncrementChecksTotal, was incrementChecksTotal" in label_line


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
    text, _ = _mermaid_symbols(SYMDELTA["nodes"], ids, edges)

    assert 'subgraph G0["a"]' in text
    assert 'subgraph G1["b"]' in text
    assert "classDef" not in text
    assert "var(" not in text
    assert "class new" not in text  # never a bare "changed" classDef
    assert "linkStyle" in text
    assert LINK_COLOR_NEW in text and LINK_COLOR_GONE in text


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

    text, _, _ = _mermaid_packages(nodes, [])

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

    text, _, _ = _mermaid_packages(nodes, [])

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

    text, _, _ = _mermaid_packages(nodes, [])

    assert "~~~" not in text


def test_mermaid_packages_single_component_has_no_invisible_link():
    text, _, _ = _mermaid_packages(SYMDELTA["nodes"], SYMDELTA["edges"])

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


def test_symbols_cli_rejects_a_kind_other_than_symbols():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "x.json"
        path.write_text("{}")
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "sections.py"),
             "--kind", "coupling", "--format", "html", "--data", str(path)],
            capture_output=True, text=True,
        )
        assert result.returncode != 0


def test_the_legend_rides_with_the_level_that_actually_draws_a_new_or_gone_node():
    # It used to be one static row under the heading, explaining both states even on a package
    # view that has neither on screen. A group's own two-level graph, hence inline=True: the
    # page-level graph is packages only and never carries a legend at all (see
    # test_symbols_page_level_has_marker_heading_and_packages_only).
    out = render_symbols(SYMDELTA, inline=True)

    ctl = out.split('<div class="vd-ctl">', 1)[1].split("</div>", 1)[0]
    assert "sw-new" in ctl and "sw-gone" in ctl
    level1 = out.split('data-level="1"', 1)[1].split(">", 1)[0]
    assert "sw-new" not in level1  # the package level draws no new/gone node
    level2 = out.split('data-level="2"', 1)[1].split(">", 1)[0]
    assert "sw-new" in level2 and "sw-gone" in level2


def test_explain_renames_the_page_level_heading_to_structure():
    out = render_symbols({"nodes": _TWO_LINKED_SYMBOLS, "edges": _ONE_EDGE}, explain=True)

    assert "<h2>Structure</h2>" in out
    assert "Changes visualization" not in out


def test_explain_drops_the_new_gone_colouring_the_empty_baseline_makes_meaningless():
    # A group's own symbols level, hence inline=True: new/gone colouring, the legend, and the
    # drawn/listed note wording are all symbols-level concepts now.
    out = render_symbols({"nodes": _TWO_LINKED_SYMBOLS, "edges": _ONE_EDGE}, explain=True,
                         inline=True)

    assert "  class " not in out and "linkStyle" not in out
    assert "vd-legend" not in out
    assert "changed symbols drawn as nodes" not in out
    assert "symbols drawn as nodes" in out


# ---- structure.py's render_structure: structure.json fixture, shaped like structure.py's own
# analyse() output --------------------------------------------------------------------------

STRUCTURE = {
    "language": "go",
    "components": [
        {"id": "a/x.go:Foo", "name": "Foo", "kind": "struct", "file": "a/x.go", "state": "new",
         "members": [{"name": "Run", "state": "new"}], "group": 0, "column": 0},
        {"id": "b/y.go:Bar", "name": "Bar", "kind": "struct", "file": "b/y.go", "state": "changed",
         "members": [{"name": "Call", "state": "changed"}, {"name": "Old", "state": "unchanged"}],
         "group": 1, "column": 1},
        {"id": "c/z.go:Baz", "name": "Baz", "kind": "struct", "file": "c/z.go", "state": "removed",
         "members": [{"name": "Gone", "state": "removed"}], "group": None, "column": 1},
    ],
    "edges": [
        {"from": "a/x.go:Foo", "to": "b/y.go:Bar"},
        {"from": "c/z.go:Baz", "to": "a/x.go:Foo", "state": "gone"},
    ],
    "implements": [{"from": "b/y.go:Bar", "to": "Foo", "kind": "implements"}],
    "dropped": 0,
}


def test_structure_marker_is_the_first_line():
    assert render_structure(STRUCTURE).startswith(STRUCTURE_MARKER)


def test_structure_null_language_renders_a_reason_note_not_a_view():
    data = {"language": None, "reason": "no changed files in a supported language"}
    out = render_structure(data)
    assert STRUCTURE_MARKER in out
    assert "<h2>System change</h2>" in out
    assert "No structure view: no changed files in a supported language." in out


def test_structure_explain_null_language_renames_the_heading():
    out = render_structure({"language": None, "reason": "x"}, explain=True)
    assert "<h2>How it fits together</h2>" in out
    assert "System change" not in out


def test_structure_no_components_renders_nothing():
    assert render_structure({"language": "go", "components": []}) == ""


def test_structure_bare_dict_renders_nothing():
    # A bare {} (no structure.json produced at all) must not be read as `language: null`
    # structure.py actually returned -- see render_structure's own docstring.
    assert render_structure({}) == ""


def test_structure_review_mode_shows_badges_members_removed_box_and_gone_edge():
    out = render_structure(STRUCTURE)
    assert "vds-badge" in out
    assert "vds-members" in out
    assert 'vds-comp gone"' in out
    assert "Baz" in out
    assert "vds-e-gone" in out or "&quot;state&quot;: &quot;gone&quot;" in out


def test_structure_explain_mode_drops_badges_members_removed_boxes_and_gone_edges():
    out = render_structure(STRUCTURE, explain=True)
    assert "vds-badge" not in out
    assert "vds-members" not in out
    assert "Baz" not in out  # removed component dropped outright, not just unstyled
    assert "&quot;state&quot;: &quot;gone&quot;" not in out
    assert "<h2>How it fits together</h2>" in out
    assert "2 components in 2 columns." in out


def test_structure_group_colouring_matches_group_color():
    out = render_structure(STRUCTURE)
    assert f"--c:{_group_color(0)}" in out
    assert f"--c:{_group_color(1)}" in out


def test_structure_filter_chips_one_per_story_stop():
    out = render_structure(STRUCTURE)
    assert 'data-g="all"' in out
    assert 'data-g="0"' in out
    assert 'data-g="1"' in out
    # Baz carries group None and must not earn a stop of its own.
    assert out.count('class="vds-chip"') == 3


def test_structure_no_group_data_renders_no_chips():
    data = {"language": "go", "components": [
        {"id": "x:F", "name": "F", "kind": "function", "file": "x.go", "state": "new",
         "members": [], "group": None, "column": 0},
    ], "edges": [], "implements": [], "dropped": 0}
    out = render_structure(data)
    assert "vds-chips" not in out


def test_structure_dropped_count_surfaced_when_positive():
    dropped = {**STRUCTURE, "dropped": 2}
    out = render_structure(dropped)
    assert "2 components dropped to fit the diagram." in out
    assert "dropped" not in render_structure(STRUCTURE)


def test_structure_implements_edge_resolves_target_by_name():
    out = render_structure(STRUCTURE)
    assert "implements" in out  # the legend line for the implements edge from Bar to Foo


def test_structure_implements_edge_with_no_matching_name_is_dropped():
    data = {**STRUCTURE, "implements": [{"from": "b/y.go:Bar", "to": "NoSuchType",
                                         "kind": "implements"}]}
    out = render_structure(data)
    assert "implements" not in out


def test_structure_hostile_component_id_cannot_break_out_of_the_id_attribute():
    data = {"language": "go", "components": [
        {"id": 'a" onmouseover="alert(1)', "name": 'Foo" onmouseover="alert(2)',
         "kind": "struct", "file": "a.go", "state": "new", "members": [], "group": None,
         "column": 0},
    ], "edges": [], "implements": [], "dropped": 0}
    out = render_structure(data)
    assert 'onmouseover="alert(1)"' not in out
    assert 'onmouseover="alert(2)"' not in out  # the display name gets the same &quot; pass
    assert "&quot;" in out
    assert 'aria-label="Open menu for Foo&quot; onmouseover=&quot;alert(2)"' in out


def test_structure_implements_name_collision_resolves_by_shown_order_not_set_order():
    # Two same-named components; whichever sorts first in structure.py's own (column, file,
    # name) order -- the order `shown` already carries -- must win, every run, regardless of
    # PYTHONHASHSEED. Checked both ways by reversing `shown` and confirming the winner flips.
    components = [
        {"id": "a.go:Dup", "name": "Dup", "kind": "struct", "file": "a.go", "state": "new",
         "members": [], "group": None, "column": 0},
        {"id": "b.go:Dup", "name": "Dup", "kind": "struct", "file": "b.go", "state": "new",
         "members": [], "group": None, "column": 1},
        {"id": "c.go:Impl", "name": "Impl", "kind": "struct", "file": "c.go", "state": "new",
         "members": [], "group": None, "column": 1},
    ]
    implements = [{"from": "c.go:Impl", "to": "Dup", "kind": "implements"}]
    kept_ids = {c["id"] for c in components}

    resolved = _structure_implements_edges(implements, components, kept_ids)
    assert resolved == [{"from": "c.go:Impl", "to": "a.go:Dup", "kind": "implements"}]

    reversed_resolved = _structure_implements_edges(implements, list(reversed(components)), kept_ids)
    assert reversed_resolved == [{"from": "c.go:Impl", "to": "b.go:Dup", "kind": "implements"}]


def test_structure_member_chips_carry_a_marker_not_just_colour():
    out = render_structure(STRUCTURE)
    assert ">+Run<" in out  # new member
    assert ">~Call<" in out  # changed member
    assert ">Old<" in out  # unchanged member carries no marker, the neutral baseline


def test_structure_explain_mode_also_surfaces_dropped_count():
    dropped = {**STRUCTURE, "dropped": 3}
    out = render_structure(dropped, explain=True)
    assert "3 components dropped to fit the diagram." in out


def test_structure_chip_labels_use_group_titles_when_present():
    data = {**STRUCTURE, "groups": [{"index": 0, "title": "Tools"}, {"index": 1, "title": "Contracts"}]}
    out = render_structure(data)
    assert ">1 Tools</button>" in out
    assert ">2 Contracts</button>" in out


def test_structure_chip_falls_back_to_ordinal_when_one_title_is_missing():
    data = {**STRUCTURE, "groups": [{"index": 0, "title": "Tools"}]}  # group 1 has no entry
    out = render_structure(data)
    assert ">1 Tools</button>" in out
    assert ">2</button>" in out


def test_structure_chip_title_promotes_a_backticked_identifier_to_code():
    data = {**STRUCTURE, "groups": [{"index": 0, "title": "The `run_workflow` tool"}]}
    out = render_structure(data)
    assert ">1 The <code>run_workflow</code> tool</button>" in out


def test_structure_chip_falls_back_to_ordinal_with_no_groups_key_at_all():
    out = render_structure(STRUCTURE)  # the fixture carries no top-level "groups" at all
    assert ">1</button>" in out
    assert ">2</button>" in out


def test_structure_no_column_headings():
    out = render_structure(STRUCTURE)
    assert "<h5>" not in out
    assert "Column 1" not in out


def test_structure_callers_on_the_left_note_present():
    out = render_structure(STRUCTURE)
    assert '<p class="vds-hint">Callers on the left, callees on the right.</p>' in out


def test_structure_also_touched_note_is_rendered_and_escaped():
    data = {**STRUCTURE, "also_touched": ["helper", "<script>"]}
    out = render_structure(data)
    assert "Also touched: <code>helper</code>, <code>&lt;script&gt;</code>" in out
    assert "<script>" not in out  # would break out of the note otherwise


def test_structure_no_also_touched_renders_no_note():
    out = render_structure(STRUCTURE)  # the fixture carries no also_touched at all
    assert "vds-also" not in out


def test_structure_boxes_carry_role_button_and_an_escaped_data_path():
    data = {"language": "go", "components": [
        {"id": "a.go:Foo", "name": "Foo", "kind": "struct", "file": 'a" onclick="x().go',
         "state": "new", "members": [], "group": None, "column": 0},
    ], "edges": [], "implements": [], "dropped": 0}
    out = render_structure(data)
    assert 'role="button"' in out
    # The hostile quote in the file path is neutralised inside the attribute...
    assert 'data-path="a&quot; onclick=&quot;x().go"' in out
    # ...so the attribute can't be broken out of to inject a live onclick handler.
    assert 'data-path="a" onclick="x().go"' not in out


def test_structure_container_carries_the_zoom_aria_label():
    out = render_structure(STRUCTURE)
    assert ('<div class="vds-sys" tabindex="0" role="button" '
            'aria-label="Expand diagram to full size"') in out


if __name__ == "__main__":
    tests = [
        test_symbols_returns_nothing_when_there_are_no_nodes,
        test_symbols_null_language_shows_a_note_with_reason_and_remedy,
        test_symbols_null_language_shows_a_note_with_no_remedy,
        test_symbols_null_language_note_escapes_reason_and_remedy,
        test_symbols_no_supported_language_reads_as_a_fact_not_a_fix,
        test_symbols_null_language_inline_group_tab_stays_empty,
        test_scope_to_paths_keeps_the_far_end_of_an_edge_that_leaves_the_scope,
        test_scope_to_paths_drops_what_no_edge_reaches,
        test_scope_to_paths_matches_on_a_path_segment_not_a_string_prefix,
        test_scope_to_paths_without_paths_changes_nothing,
        test_scope_to_paths_dot_means_whole_repo,
        test_symbols_with_dot_scope_renders_a_nonempty_section,
        test_symbols_scoped_out_of_existence_renders_no_section,
        test_symbols_opens_on_symbols_and_on_packages_once_it_is_oversized,
        test_symbols_page_level_has_marker_heading_and_packages_only,
        test_symbols_inline_has_legend_slider_and_two_levels,
        test_symbols_html_never_emits_classdef,
        test_symbols_inline_has_no_page_marker_heading_or_details_wrapper,
        test_symbols_inline_toggle_is_a_class_not_an_id,
        test_symbols_inline_data_symbols_is_the_changed_symbol_count_in_scope,
        test_symbols_note_is_the_counts_sentence_plus_drawn_and_listed,
        test_symbols_note_names_the_undrawn_containment_count_when_nonzero,
        test_symbols_page_level_note_has_no_drawn_listed_split,
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
        test_symbols_html_level1_data_ids_maps_package_boxes_to_their_directory,
        test_explain_drops_the_new_gone_colouring_the_empty_baseline_makes_meaningless,
        test_symbols_html_level2_data_ids_maps_symbol_nodes_not_package_boxes,
        test_symbols_html_data_ids_survives_a_quote_in_a_file_path,
        test_mm_escape_strips_parens_quotes_and_backticks,
        test_wrap_label_leaves_a_long_multiword_label_alone,
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
        test_mermaid_symbols_keeps_a_long_label_in_one_pair_of_quotes,
        test_mermaid_symbols_appends_the_old_name_for_a_renamed_symbol,
        test_mermaid_symbols_label_has_no_was_line_when_the_name_did_not_change,
        test_symbols_legend_ignores_the_word_new_in_a_label_not_a_class_line,
        test_new_and_gone_class_lists_are_declaration_order_not_set_order,
        test_mermaid_symbols_subgraphs_by_package_and_classes_new_gone_but_not_changed,
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
        test_symbols_cli_rejects_a_kind_other_than_symbols,
        test_the_legend_rides_with_the_level_that_actually_draws_a_new_or_gone_node,
        test_explain_renames_the_page_level_heading_to_structure,
        test_symbols_llm_resolver_shows_the_caveat_page_level_and_inline,
        test_symbols_mechanical_resolver_has_no_llm_caveat,
        test_structure_marker_is_the_first_line,
        test_structure_null_language_renders_a_reason_note_not_a_view,
        test_structure_explain_null_language_renames_the_heading,
        test_structure_no_components_renders_nothing,
        test_structure_bare_dict_renders_nothing,
        test_structure_review_mode_shows_badges_members_removed_box_and_gone_edge,
        test_structure_explain_mode_drops_badges_members_removed_boxes_and_gone_edges,
        test_structure_group_colouring_matches_group_color,
        test_structure_filter_chips_one_per_story_stop,
        test_structure_no_group_data_renders_no_chips,
        test_structure_dropped_count_surfaced_when_positive,
        test_structure_implements_edge_resolves_target_by_name,
        test_structure_implements_edge_with_no_matching_name_is_dropped,
        test_structure_hostile_component_id_cannot_break_out_of_the_id_attribute,
        test_structure_implements_name_collision_resolves_by_shown_order_not_set_order,
        test_structure_member_chips_carry_a_marker_not_just_colour,
        test_structure_explain_mode_also_surfaces_dropped_count,
        test_structure_chip_labels_use_group_titles_when_present,
        test_structure_chip_falls_back_to_ordinal_when_one_title_is_missing,
        test_structure_chip_title_promotes_a_backticked_identifier_to_code,
        test_structure_chip_falls_back_to_ordinal_with_no_groups_key_at_all,
        test_structure_no_column_headings,
        test_structure_callers_on_the_left_note_present,
        test_structure_also_touched_note_is_rendered_and_escaped,
        test_structure_no_also_touched_renders_no_note,
        test_structure_boxes_carry_role_button_and_an_escaped_data_path,
        test_structure_container_carries_the_zoom_aria_label,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
