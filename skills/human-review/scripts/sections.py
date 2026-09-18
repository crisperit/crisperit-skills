#!/usr/bin/env python3
"""Render a ready-to-embed coupling section from coupling.py, structure.py or symdelta.py output.

Usage:
  python3 sections.py --kind coupling --data coupling.json --format md   > section.md
  python3 sections.py --kind symbols  --data symdelta.json  --format html > section.html

The diagram, its legend, its limitation line and its skipped-files line are built here, in
code, so they cannot go missing or get paraphrased by whoever assembles the page. A renderer
inserts this output verbatim; it does not rewrite it.

The first line of the output is an HTML comment marker, `<!-- visual-diff:<kind> -->`, which
survives GitHub's sanitizer and renders as nothing. validate_analysis.py --sections uses it to
prove the section actually reached the rendered file.

Prints nothing at all when the analysis found no relations to draw, so an empty section is
never emitted.

Stdlib only, no network.
"""

import argparse
import json
import re
import sys
from pathlib import Path

LEGEND = (
    "Thick arrow is new, dotted is one that went away, plain is unchanged."
)

SYMBOLS_MARKER = "<!-- visual-diff:symbols -->"

KINDS = {
    "coupling": {
        "marker": "<!-- visual-diff:coupling -->",
        "heading": "File coupling",
        "legend": f"Arrows point from the file that imports to the file it imports. {LEGEND}",
        "limitation": (
            "This reads imports only, so coupling through string references, dynamic "
            "dispatch, dependency injection or config wiring does not appear here."
        ),
        "aria": "File coupling diagram, click to enlarge",
    },
    "layers": {
        "marker": "<!-- visual-diff:layers -->",
        "heading": "Modules",
        "legend": (
            "The whole repo, not only the diff: every code file folded into its module. A star "
            "marks a module this change touched. An arrow's number is how many file imports it "
            "stands for, and a thick one carries a dependency this change introduced."
        ),
        "limitation": (
            "This reads imports only, so coupling through string references, dynamic "
            "dispatch, dependency injection or config wiring does not appear here."
        ),
        "aria": "Module map, click to enlarge",
    },
    "structure": {
        "marker": "<!-- visual-diff:structure -->",
        "heading": "Structure coupling",
        "legend": (
            "Boxes are classes and functions grouped by file, with a class's methods listed "
            "under its name. An arrow means the box at the tail uses the one at the head, "
            f"unless it is labelled extends or implements. {LEGEND} A structure that appeared "
            "or disappeared is marked (new) or (gone). One marked (context) was not touched "
            "by this change and is drawn only because something in the diff extends or "
            "implements it."
        ),
        "limitation": (
            "Python relations come from the parser and are exact. TypeScript and JavaScript "
            "resolve a reference through the file's own imports. Every other language falls "
            "back to a generic tree-sitter heuristic matched by name, which is weaker."
        ),
        "aria": "Structure coupling diagram, click to enlarge",
    },
}


def escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_CODE_RE = re.compile(r"`([^`]+)`")


def _codeify(text):
    """Turn backtick-paired identifiers into <code> tags. Must run on already-escaped text,
    after escape(), so a `<` inside an identifier can't inject markup; an unpaired backtick
    has no partner to match and is left as a literal character."""
    return _CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", text)


def _html_note(text):
    """Prose bound for a `.note` <p>: escape first, then promote backtick-wrapped identifiers
    to <code>, so the tags are added to text that is already HTML-safe."""
    return _codeify(escape(text))


def edges_of(data):
    """[(src, dst, state, weight)] with plain string node names. The three analysers report
    differently: coupling.py [importer, imported] pairs under added/removed/unchanged,
    structure.py {from, to, kind} with [path, symbol] ends, layers.py one `edges` list of
    {from, to, count} with no per-edge state. Normalise all three here."""
    out = []
    for state in ("added", "removed", "unchanged"):
        for edge in data.get(state) or []:
            if isinstance(edge, dict):
                out.append((":".join(edge["from"]), ":".join(edge["to"]), state, 1))
            else:
                out.append((edge[0], edge[1], state, 1))
    for edge in data.get("edges") or []:
        out.append((edge["from"], edge["to"], "unchanged", edge.get("count", 1)))
    return out


def _dirname(node):
    return node.split(":")[0].rsplit("/", 1)[0]


def find_cycle(edges):
    """One example cycle among the head-state edges, or None. Depth-first, iterative, so a
    deep graph cannot blow the stack."""
    graph = {}
    for src, dst, state, _ in edges:
        if state != "removed":
            graph.setdefault(src, []).append(dst)

    colour = {}
    for root in list(graph):
        if colour.get(root):
            continue
        stack = [(root, iter(graph.get(root, ())))]
        path = [root]
        colour[root] = "grey"
        while stack:
            node, children = stack[-1]
            nxt = next(children, None)
            if nxt is None:
                colour[node] = "black"
                stack.pop()
                path.pop()
                continue
            if colour.get(nxt) == "grey":
                return path[path.index(nxt):] + [nxt]
            if colour.get(nxt) != "black":
                colour[nxt] = "grey"
                path.append(nxt)
                stack.append((nxt, iter(graph.get(nxt, ()))))
    return None


def numbers_from_edges(edges):
    """The edges-only half of numbers(), split out so its dict shape is defined once."""
    live = [e for e in edges if e[2] != "removed"]
    fan_in, fan_out = {}, {}
    for src, dst, _, weight in live:
        fan_out[src] = fan_out.get(src, 0) + weight
        fan_in[dst] = fan_in.get(dst, 0) + weight

    def top(counts):
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return [{"node": n, "count": c} for n, c in ranked[:3] if c > 1]

    return {
        "relations": len(edges),
        "new": sum(1 for e in edges if e[2] == "added"),
        "gone": sum(1 for e in edges if e[2] == "removed"),
        "top_depended_on": top(fan_in),
        "top_depends_on": top(fan_out),
        "across_directories": sum(1 for s, d, _, _ in live if _dirname(s) != _dirname(d)),
        "cycle": find_cycle(edges),
    }


def numbers(data):
    """Facts about the graph, computed rather than eyeballed, so any commentary written next
    to the diagram has something to be accountable to. A layer edge stands for several file
    imports, so its count is its weight."""
    return numbers_from_edges(edges_of(data))


def numbers_line(nums):
    parts = [
        f"{nums['relations']} relations, {nums['new']} new, {nums['gone']} gone",
        f"{nums['across_directories']} crossing a directory boundary",
    ]
    if nums["top_depended_on"]:
        top = nums["top_depended_on"][0]
        parts.append(f"most depended on is `{top['node']}` with {top['count']} incoming")
    if nums["top_depends_on"]:
        top = nums["top_depends_on"][0]
        parts.append(f"widest reach is `{top['node']}` with {top['count']} outgoing")
    if nums["cycle"]:
        parts.append("cycle present: " + " -> ".join(f"`{n}`" for n in nums["cycle"]))
    return "Numbers: " + "; ".join(parts) + "."


def _few(paths, limit=3):
    shown = ", ".join(paths[:limit])
    return shown if len(paths) <= limit else f"{shown} and {len(paths) - limit} more"


def caption(kind, data, legend=None):
    """One paragraph under the diagram: how to read it, then what was left out. It used to be
    five stacked grey paragraphs, which is more apparatus than diagram and got skipped.
    `legend` overrides `KINDS[kind]["legend"]` for a caller drawing a different picture from
    the same underlying data; the limitation, skipped-files and note prose stay tied to `kind`,
    since those describe the data, not the picture."""
    spec = KINDS[kind]
    parts = [legend if legend is not None else spec["legend"], spec["limitation"]]

    unsupported = data.get("skipped", {}).get("unsupported") or []
    unparseable = data.get("skipped", {}).get("unparseable") or []
    if unsupported:
        parts.append(f"Not analysed: {_few([f'`{p}`' for p in unsupported])}.")
    if unparseable:
        parts.append(f"Could not be parsed: {_few([f'`{p}`' for p in unparseable])}.")
    if data.get("note"):
        parts.append(data["note"])
    return " ".join(parts)


def render(kind, data, fmt):
    mermaid = data.get("mermaid") or ""
    if not mermaid.strip():
        return ""

    spec = KINDS[kind]
    heading = spec["heading"]
    aria = spec["aria"]
    tail = f"{caption(kind, data)} {numbers_line(numbers(data))}"

    if fmt == "md":
        return "\n".join([
            spec["marker"],
            "",
            f"### {heading}",
            "",
            "```mermaid",
            mermaid,
            "```",
            "",
            tail,
            "",
        ])

    return "\n".join([
        spec["marker"],
        f"<h2>{escape(heading).upper()}</h2>",
        '<div class="panel svgbox" tabindex="0" role="button"'
        f' aria-label="{escape(aria)}">',
        f'<pre class="mermaid">{escape(mermaid)}</pre>',
        "</div>",
        f'<p class="note">{_html_note(tail)}</p>',
        "",
    ])


_MM_UNSAFE_RE = re.compile(r'[()"`]')

# The two pre-rendered detail levels. Index 0 is what the page shows first. There were three:
# symbols with and without their outside callers were separate levels, and on a real PR they
# differed by nine nodes out of thirty -- a detail nudge dressed up as a view, which is what
# made a slider feel arbitrary. Callers are always in now.
LEVEL_NAMES = ["Packages", "Symbols"]

# Mermaid's own parser doesn't reliably resolve a CSS var() inside classDef/linkStyle, so node
# colour goes through `class S0,S1 new` plus page CSS instead (see diff-review-template.html).
# Edges have no per-edge classDef though, only `linkStyle <idx> stroke:...`, so their colour has
# to be a literal; these mirror the page's default (dark-theme) --accent/--muted. The
# colour-semantics table lives once in diff-review-template.html next to :root -- these hexes
# must match it by hand.
LINK_COLOR_NEW = "#7aa2f7"
LINK_COLOR_GONE = "#9aa3b2"


def _mm_escape(label):
    """Strip parentheses, quotes and backticks: mermaid's own `ID["label"]` syntax breaks on
    any of the three, and a real symbol name can carry any of them (generics, string
    literals)."""
    return _MM_UNSAFE_RE.sub("", label)


LABEL_WRAP_TARGET = 24


_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _split_camel(word, target):
    """The terminal fallback once `_split_overlong`'s separator chain is exhausted and a word is
    still too long: break at camelCase/PascalCase boundaries -- lowercase-or-digit to uppercase,
    and an uppercase run followed by a capitalised word, so an acronym like the GRPC in
    MediaGuardGRPCDecorator breaks after the acronym instead of inside it -- then greedily
    re-join the pieces up to `target`, the same accumulate loop `_split_overlong` runs on its own
    separator pieces. A word with no camel boundary at all (an acronym, a single run of one
    case) comes back as a single piece, unsplit."""
    atoms = _CAMEL_BOUNDARY_RE.split(word)
    if len(atoms) == 1:
        return [word]
    lines = [atoms[0]]
    for atom in atoms[1:]:
        candidate = lines[-1] + atom
        if len(candidate) <= target:
            lines[-1] = candidate
        else:
            lines.append(atom)
    return lines


def _split_overlong(word, target, seps=("/", "_", ".")):
    """Break one over-length, space-free `word` into `target`-ish pieces, trying '/' then '_'
    then '.' in turn -- each break keeps the separator on the end of the earlier piece, which
    reads better mid-path/mid-identifier/mid-selector than leading with it. Once that chain is
    exhausted and a piece is still too long (a Go `Type.Method` selector split down to a bare
    identifier, or a run-together identifier with none of those separators at all), it falls
    back to `_split_camel`. A piece with no separator and no camel boundary -- an acronym, a
    single English word -- is returned whole; never truncated."""
    if len(word) <= target:
        return [word]
    if not seps:
        return _split_camel(word, target)
    sep, rest = seps[0], seps[1:]
    parts = word.split(sep)
    if len(parts) == 1:
        return _split_overlong(word, target, rest)
    pieces = [p + sep for p in parts[:-1]] + [parts[-1]]
    lines = [pieces[0]]
    for piece in pieces[1:]:
        candidate = lines[-1] + piece
        if len(candidate) <= target:
            lines[-1] = candidate
        else:
            lines.append(piece)
    out = []
    for line in lines:
        out.extend(_split_overlong(line, target, rest) if len(line) > target else [line])
    return out


def wrap_label(label, target=LABEL_WRAP_TARGET):
    """Break `label` into `<br/>`-joined lines of about `target` chars, so Mermaid arrives with
    lines already broken instead of auto-wrapping (and clipping) its SVG text at the
    `flowchart.wrappingWidth` default under `securityLevel: 'strict'`. Breaks at spaces first; a
    single word still over `target` (a package path, a snake_case identifier, a Go `Type.Method`
    selector, or a run-together identifier) is broken further by `_split_overlong`, down to a
    camelCase fallback as a last resort. A word with no separator and no camel boundary, or a
    label that keeps overflowing regardless, keeps growing more lines rather than dropping any
    of it -- a tall node beats a lost name."""
    words = label.split()
    if not words:
        return label
    # Each atom carries its own joiner: a real space where the label itself had one (the start of
    # a new word), empty where the atom is a fragment `_split_overlong` cut out of one overlong
    # word -- joining two such fragments with a space would inject a character the label never had.
    atoms = []
    for wi, word in enumerate(words):
        pieces = _split_overlong(word, target) if len(word) > target else [word]
        atoms.extend((piece, " " if wi and i == 0 else "") for i, piece in enumerate(pieces))
    lines = [atoms[0][0]]
    for piece, joiner in atoms[1:]:
        candidate = lines[-1] + joiner + piece
        if len(candidate) <= target:
            lines[-1] = candidate
        else:
            lines.append(piece)
    return "<br/>".join(lines)


def _pkg_label(pkg_id, pkg_by_id):
    """A package box's own label: just its path segment (symdelta.py's own bare-last-segment,
    already collapsing a chain of single-child prefixes into one). The box nests inside its
    parent's box now, so ancestry shows as containment instead of a repeated full-path prefix.
    `pkg_id` is None for a symbol with no package of its own -- the one case that earns the
    "(root)" label; anything else absent from `pkg_by_id` is an unknown id from a caller other
    than symdelta.py, shown as itself rather than collapsed onto that same "(root)" box."""
    if pkg_id is None:
        return "(root)"
    return pkg_by_id[pkg_id]["label"] if pkg_id in pkg_by_id else pkg_id


def _pkg_children_and_roots(pkg_ids, pkg_by_id):
    """children[parent_id] -> [child ids] and the roots, restricted to exactly `pkg_ids`: a
    package whose own parent isn't in that set nests as a root here even if it has one in the
    full tree (the symbols level only draws packages that carry an in-scope symbol or lead to one)."""
    ids = set(pkg_ids)
    children, roots = {}, []
    for pid in pkg_ids:
        parent = pkg_by_id[pid].get("parent")
        if parent in ids:
            children.setdefault(parent, []).append(pid)
        else:
            roots.append(pid)
    return children, roots


def _pkg_ancestor_ids(pid, pkg_by_id):
    """Every ancestor above `pid`, walking parent links to the root."""
    out = []
    parent = pkg_by_id.get(pid, {}).get("parent")
    while parent is not None:
        out.append(parent)
        parent = pkg_by_id.get(parent, {}).get("parent")
    return out


def _nested_inner_id(a, b, pkg_by_id):
    """`a` if `a` nests inside `b`, `b` if `b` nests inside `a`, else None. An edge between two
    packages in a containment relationship has nowhere sensible for dagre to route -- the
    containment box already draws that relationship -- so callers skip the arrow entirely and
    total its count for the graph's note instead. Shared rather than inlined into
    `_mermaid_packages`, the only caller with package-to-package edges today, so any level that
    later grows them gets the same treatment."""
    if b in _pkg_ancestor_ids(a, pkg_by_id):
        return a
    if a in _pkg_ancestor_ids(b, pkg_by_id):
        return b
    return None


def _connected_components(node_ids, edge_pairs):
    """One representative id per connected component of `node_ids`, given `edge_pairs`
    (undirected). Each component's own lowest id is both what orders the components and what
    represents it, so the result is byte-stable run to run -- the caller chains components
    together in this order with invisible mermaid links.

    Components here are the level's own drawn nodes, not the boxes containing them: two nodes
    can sit in the same subgraph and still be their own disconnected components if nothing
    edges them together, and dagre lays each one out side by side regardless of nesting -- the
    reason a level lays out far wider than its content warrants. Chaining representatives with
    `A ~~~ B` (mermaid's invisible link) forces dagre to stack them instead."""
    parent = {n: n for n in node_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edge_pairs:
        if a not in parent or b not in parent:
            continue
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    groups = {}
    for n in node_ids:
        groups.setdefault(find(n), []).append(n)
    sort_key = lambda n: n if n is not None else ""
    return sorted((min(g, key=sort_key) for g in groups.values()), key=sort_key)


def _chain_components_lines(node_ids, edge_pairs, mm_id):
    """`repA ~~~ repB` for each adjacent pair of components, in the deterministic order
    `_connected_components` returns -- nothing at all when there's only one component. Callers
    append this after every real link (including any `linkStyle` lines), so the invisible links
    never shift a real link's index."""
    reps = _connected_components(node_ids, edge_pairs)
    return [f"  {mm_id[reps[i]]} ~~~ {mm_id[reps[i + 1]]}" for i in range(len(reps) - 1)]


def _mermaid_packages(nodes, edges):
    """Level 1: one box per package, nested under its parent package's box, with cross-package
    symbol edges rolled up into a single package-to-package arrow labelled with how many symbol
    edges it stands for. A same-package edge has nothing to roll up to at this level -- it's
    already inside one box -- so it's dropped rather than drawn as a self-loop. A package with no
    children of its own is a plain node; one with children is a subgraph containing them, and
    either way its own id is what an edge targets.

    A rolled-up count between a package and one nested inside it is never drawn as an arrow
    either: dagre has nowhere sensible to route a box-to-its-own-child edge, and the containment
    box already states that relationship. It carries little review signal anyway -- it's the
    expected direction -- so it isn't drawn at all here; the caller reports the total once in the
    graph's note instead (see `_nested_inner_id`). Returns `(mermaid_text, undrawn_count)`."""
    pkg_by_id = {n["id"]: n for n in nodes if n["kind"] == "pkg"}
    if not pkg_by_id:
        return "flowchart LR", 0
    pkg_of_symbol = {n["id"]: n.get("parent") for n in nodes if n["kind"] == "symbol"}

    counts = {}
    for e in edges:
        src_pkg, dst_pkg = pkg_of_symbol.get(e["source"]), pkg_of_symbol.get(e["target"])
        if src_pkg is None or dst_pkg is None or src_pkg == dst_pkg:
            continue
        key = (src_pkg, dst_pkg)
        counts[key] = counts.get(key, 0) + 1

    ids = pkg_by_id.keys()
    children, roots = _pkg_children_and_roots(ids, pkg_by_id)
    # A pure container (only descendants own symbols) is never an edge endpoint, so it would
    # otherwise sit in its own singleton component and get chained to one of its own nested
    # children -- an edge shape dagre's compound layout can't place.
    owning_ids = {pid for pid in pkg_of_symbol.values() if pid in pkg_by_id}

    def nearest_owning_ancestor(pid):
        parent = pkg_by_id.get(pid, {}).get("parent")
        while parent is not None and parent not in owning_ids:
            parent = pkg_by_id.get(parent, {}).get("parent")
        return parent

    # An owning package can still be the ancestor of another (a file of its own plus a
    # subpackage); containment already makes the two one layout block, so merge them here too,
    # or they'd land in separate components and hit the same invalid-edge problem.
    containment_pairs = [(pid, anc) for pid in owning_ids
                          for anc in [nearest_owning_ancestor(pid)] if anc is not None]

    undrawn_count = 0
    real_edges = {}
    for (src, dst), count in counts.items():
        inner = _nested_inner_id(src, dst, pkg_by_id)
        if inner is not None:
            undrawn_count += count
        else:
            real_edges[(src, dst)] = count

    lines = ["flowchart LR"]
    mm_id = {}

    def emit(pid, indent):
        pad = "  " * indent
        mm_id[pid] = f"P{len(mm_id)}"
        label = wrap_label(_mm_escape(_pkg_label(pid, pkg_by_id)))
        kids = sorted(children.get(pid, []))
        if kids:
            lines.append(f'{pad}subgraph {mm_id[pid]}["{label}"]')
            for kid in kids:
                emit(kid, indent + 1)
            lines.append(f"{pad}end")
        else:
            lines.append(f'{pad}{mm_id[pid]}["{label}"]')

    for root in sorted(roots):
        emit(root, 1)
    for (src, dst), count in sorted(real_edges.items()):
        lines.append(f'  {mm_id[src]} -->|{count}| {mm_id[dst]}')

    lines.extend(_chain_components_lines(owning_ids, list(counts.keys()) + containment_pairs, mm_id))
    return "\n".join(lines), undrawn_count


def _changed_symbol_ids(nodes):
    """Symbols that are themselves the delta: added or removed outright. A symbol that only
    moved package (`state: "changed"`) is not one of them -- it is drawn, if at all, only as
    context for a changed edge, never as the subject of one; the move itself is
    reported only in the `vd-moved` prose list -- see render_symbols."""
    return {n["id"] for n in nodes if n["kind"] == "symbol" and n.get("state") in ("new", "gone")}


def _symbols_scope(nodes, edges):
    """(symbol_ids, edges) for the symbols level: every changed symbol, plus every other
    endpoint a changed edge reaches, plus every edge that reaches it. The callers-excluded
    variant went with the third level that was the only thing asking for it."""
    changed = _changed_symbol_ids(nodes)
    kept = [e for e in edges if e["source"] in changed or e["target"] in changed]
    symbol_ids = set(changed)
    for e in kept:
        symbol_ids.add(e["source"])
        symbol_ids.add(e["target"])
    return symbol_ids, kept


def _edge_endpoint_ids(edges):
    """Every source/target id touched by `edges`. render_symbols uses this to decide which
    symbol earns a graph node: dagre puts each edge-less node in its own rank, so
    a page of disconnected changed symbols came out one rank wide and thousands of pixels across
    (measured on a real PR: 25 nodes in a row). A symbol with no edge here still has to reach the
    reader, just as the `vd-moved` text list rather than as a node -- see _symbols_orphans."""
    ids = set()
    for e in edges:
        ids.add(e["source"])
        ids.add(e["target"])
    return ids


def _mermaid_symbols(nodes, symbol_ids, kept_edges):
    """Levels 2 and 3 share this: symbol_ids grouped by package, nested the same way level 1 is
    -- a package that owns an in-scope symbol gets a box, and so does every ancestor needed to
    contain it, so `corelib/ratelimit` sits inside one `corelib` box rather than beside it. A
    package with both an in-scope symbol of its own and child packages gets that symbol listed
    directly inside its box, alongside the nested child boxes. Styling is `class ... new/gone`,
    not classDef (see LINK_COLOR_NEW); mermaid has no per-edge classDef at all, only
    `linkStyle <index>`, so edges are styled by their position in the diagram instead."""
    if not symbol_ids:
        return "flowchart LR"
    by_id = {n["id"]: n for n in nodes}
    pkg_by_id = {n["id"]: n for n in nodes if n["kind"] == "pkg"}

    symbols_by_pkg = {}
    for sid in symbol_ids:
        symbols_by_pkg.setdefault(by_id[sid]["parent"], []).append(sid)

    # A package earns a box by owning an in-scope symbol directly; each of its ancestors earns
    # one too, purely to contain it. A sibling package untouched by this delta stays out.
    active = set()
    for pid in symbols_by_pkg:
        node = pkg_by_id.get(pid)
        while node is not None and node["id"] not in active:
            active.add(node["id"])
            node = pkg_by_id.get(node.get("parent"))

    children, roots = _pkg_children_and_roots(active, pkg_by_id)
    # A symbol whose parent isn't a real package -- None (no package of its own), or an unknown
    # id from a caller other than symdelta.py -- still needs a box, at the top level, rather than
    # being silently dropped.
    for pid in symbols_by_pkg:
        if pid not in active:
            roots.append(pid)

    lines = ["flowchart LR"]
    mm_id = {}
    box_count = [0]

    def emit(pid, indent):
        pad = "  " * indent
        gid = f"G{box_count[0]}"
        box_count[0] += 1
        # No wrap_label here, unlike every other label on this page: a cluster title is drawn
        # inside the cluster's own top edge and mermaid reserves one line's height for it, so a
        # second <br/> line is painted under the border and lost. A long single-line title
        # overhangs the box instead, which is readable; a clipped one is not.
        lines.append(f'{pad}subgraph {gid}["{_mm_escape(_pkg_label(pid, pkg_by_id))}"]')
        for sid in sorted(symbols_by_pkg.get(pid, [])):
            mm_id[sid] = f"S{len(mm_id)}"
            label = wrap_label(_mm_escape(by_id[sid]["label"]))
            # A rename resolved across a package move (see resolve_symbol_merges in symdelta.py,
            # e.g. a Go export-capitalisation change) carries its pre-rename name as `was`; give
            # it a second line, through the same escape/wrap treatment as the label itself so it
            # can't break mermaid syntax or get clipped either.
            was = by_id[sid].get("was")
            if was:
                label = f'{label}<br/>{wrap_label(_mm_escape(f"was {was}"))}'
            lines.append(f'{pad}  {mm_id[sid]}["{label}"]')
        for kid in sorted(children.get(pid, [])):
            emit(kid, indent + 1)
        lines.append(f"{pad}end")

    for root in sorted(roots, key=lambda p: p or ""):
        emit(root, 1)

    # Iterates mm_id, not the caller's symbol_ids set directly: mm_id was just built in a fixed
    # (package, then symbol) sort order, and a set's own iteration order isn't stable run to run.
    new_ids = [nid for sid, nid in mm_id.items() if by_id[sid].get("state") == "new"]
    gone_ids = [nid for sid, nid in mm_id.items() if by_id[sid].get("state") == "gone"]
    if new_ids:
        lines.append(f'  class {",".join(new_ids)} new')
    if gone_ids:
        lines.append(f'  class {",".join(gone_ids)} gone')

    new_links, gone_links = [], []
    for idx, e in enumerate(kept_edges):
        lines.append(f'  {mm_id[e["source"]]} --> {mm_id[e["target"]]}')
        (new_links if e["state"] == "new" else gone_links).append(str(idx))
    if new_links:
        lines.append(f'  linkStyle {",".join(new_links)} stroke:{LINK_COLOR_NEW},stroke-width:2px;')
    if gone_links:
        lines.append(f'  linkStyle {",".join(gone_links)} stroke:{LINK_COLOR_GONE},stroke-dasharray:4 4;')

    edge_pairs = [(e["source"], e["target"]) for e in kept_edges]
    lines.extend(_chain_components_lines(symbol_ids, edge_pairs, mm_id))
    return "\n".join(lines)


def _symbols_note(node_count, edge_count, drawn_count, listed_count, undrawn_count):
    """The graph's only remaining prose: the legend is generated markup now (`.vd-legend`), not
    a paragraph, so this is the counts sentence plus, since the symbols level holds back an
    edge-less changed symbol as text instead of a node, how many landed each way -- so a reader
    is never left wondering whether one was silently dropped (see render_symbols). The trailing
    sentence names `undrawn_count` (see `_mermaid_packages`), the total relations skipped because
    one endpoint's package nests inside the other's -- omitted entirely when it's zero, so the
    note never states a count that adds nothing."""
    parts = [
        f"Showing {node_count} packages and symbols, {edge_count} relations between them.",
        f"{drawn_count} changed symbols drawn as nodes, {listed_count} listed below.",
    ]
    if undrawn_count:
        parts.append(
            f"{undrawn_count} relations between a package and a package inside it are not "
            "drawn because the containment box already shows them."
        )
    return _html_note(" ".join(parts))


def _symbols_orphans(nodes, orphan_ids):
    """The compact text list for changed symbols _mermaid_symbols never gets to draw: grouped by
    package, then by symbol, and marked (new)/(gone) the same way the structure caption already
    marks an appeared/disappeared structure. Reuses the `.vd-moved` list styling rather than
    adding page CSS for a second list. Empty (no list at all) when nothing was held back."""
    if not orphan_ids:
        return ""
    by_id = {n["id"]: n for n in nodes}
    pkg_by_id = {n["id"]: n for n in nodes if n["kind"] == "pkg"}
    rows = sorted(
        (_pkg_label(by_id[sid].get("parent"), pkg_by_id), by_id[sid]["label"], by_id[sid]["state"])
        for sid in orphan_ids
    )
    items = "".join(f"<li>{escape(pkg)}: {escape(label)} ({state})</li>" for pkg, label, state in rows)
    return f'<ul class="vd-moved">{items}</ul>\n'


_EMPTY_LEVEL_LABEL = "Nothing to draw at this detail level"


def _mermaid_symbols_for_level(nodes, symbol_ids, kept_edges):
    """_mermaid_symbols, except when filtering to edge-having symbols leaves nothing at all: a
    bare `flowchart LR` renders at a near-zero viewBox, which the page's own `degenerate()`
    check (diff-review-template.html) mistakes for a real render failure rather than a level
    that legitimately has nothing to draw. One placeholder node keeps it a valid diagram."""
    if not symbol_ids:
        return f'flowchart LR\n  N0["{_EMPTY_LEVEL_LABEL}"]'
    return _mermaid_symbols(nodes, symbol_ids, kept_edges)


def render_symbols(data):
    """The symbol-delta diagram: two pre-rendered `flowchart LR` mermaid diagrams (packages,
    then symbols with their callers), swapped client-side by a toggle. LR, not TB: these
    graphs are dominated by fan-out (one caller reaching several callees), and TB spreads that
    fan sideways -- the wrong axis for a phone -- where LR grows it downward and the page just
    scrolls. Mermaid has no live collapse/expand of its own, so each level is a complete diagram
    rather than one graph the reader folds. `moved` is rendered as prose, not as graph nodes: a
    symbol that only changed package sits outside the diff, and its callers -- not its own
    address -- are what a call-graph edge is about. A package-level move edge was tried and
    removed because it breaks the containment layout: dagre has to place the vacated package
    outside the box it used to nest in, and the arrow crosses the whole diagram to reach it.

    The symbols level draws a changed symbol only if it takes part in its edges: most
    changed symbols have no changed call edge, and dagre lays out every edge-less node in one
    wide rank (measured on a real PR: 25 such nodes side by side, each as wide as its
    identifier, blowing the diagram out to thousands of pixels). A symbol dropped from the
    graph still has to reach the reader, so _symbols_orphans lists it as text instead."""
    nodes = data.get("nodes") or []
    if not nodes:
        return ""
    edges = data.get("edges") or []
    moved = data.get("moved") or []

    level1, undrawn_count = _mermaid_packages(nodes, edges)
    ids2, edges2 = _symbols_scope(nodes, edges)
    drawn2 = _edge_endpoint_ids(edges2)
    level2 = _mermaid_symbols_for_level(nodes, ids2 & drawn2, edges2)

    changed_ids = _changed_symbol_ids(nodes)
    orphan_ids = changed_ids - drawn2

    note = _symbols_note(
        len(nodes), len(edges), len(changed_ids) - len(orphan_ids), len(orphan_ids), undrawn_count
    )
    names_attr = escape(json.dumps(LEVEL_NAMES)).replace('"', "&quot;")
    orphans_html = _symbols_orphans(nodes, orphan_ids)

    moved_html = ""
    if moved:
        lines = [
            f"{m.get('callsites', 0)} call sites moved, {m.get('from', '')} -> {m.get('to', '')}"
            for m in moved
        ]
        items = "".join(f"<li>{escape(line)}</li>" for line in lines)
        moved_html = f'<ul class="vd-moved">{items}</ul>\n'

    # The legend is per level, not per section: a level with no new and no gone node was
    # spending a row on two swatches for states that were not on screen. Both levels get one
    # so the script can swap it with the graph.
    legends = [_symbols_legend(level1), _symbols_legend(level2)]
    # This module's escape() leaves quotes alone, so an attribute holding markup needs the
    # same &quot; pass names_attr above does or the first inner quote ends the attribute.
    legend_attrs = [escape(x).replace('"', "&quot;") for x in legends]
    return "\n".join([
        SYMBOLS_MARKER,
        '<div class="vd-symbols">',
        # Title left, controls right, matching the walkthrough heading. The button names the
        # level you are looking at rather than the one it switches to, which is why the
        # slider's caption underneath is gone. No aria-pressed: a label reading "packages" and
        # a state reading "pressed" say different things, so the accessible name spells out
        # both the state and what activating does, and the script keeps it in step.
        '<h2 class="h2-row"><span>Changes visualization</span><span class="ctl">'
        f'{legends[0]}'
        '<button type="button" id="vd-level-toggle" '
        f'data-names="{names_attr}" aria-label="Detail level: {LEVEL_NAMES[0].lower()}.'
        f' Activate to show {LEVEL_NAMES[1].lower()}."'
        f'>{escape(LEVEL_NAMES[0].lower())}</button></span></h2>',
        f'<div class="mermaid" data-level="1" data-legend="{legend_attrs[0]}">'
        f'{escape(level1)}</div>',
        f'<div class="mermaid" data-level="2" hidden '
        f'data-legend="{legend_attrs[1]}">{escape(level2)}</div>',
        f'<p class="note">{note}</p>',
    ]) + "\n" + orphans_html + moved_html + "</div>\n"


def _symbols_legend(mermaid_source):
    """The new/gone key, emitted only for a level whose own diagram actually carries such a
    node. `class ... new`/`gone` is how _mermaid_symbols marks them, so the rendered source is
    the honest test: a package view with neither state was spending a row explaining both."""
    has_new = " new" in mermaid_source
    has_gone = " gone" in mermaid_source
    if not (has_new or has_gone):
        return ""
    parts = []
    if has_new:
        parts.append('<span class="sw sw-new"></span>new')
    if has_gone:
        parts.append('<span class="sw sw-gone"></span>gone')
    return f'<span class="vd-legend">{"".join(parts)}</span>' 


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", required=True, choices=sorted(list(KINDS) + ["symbols"]))
    parser.add_argument("--data", required=True)
    parser.add_argument("--format", required=True, choices=("md", "html"))
    args = parser.parse_args()

    data = json.loads(Path(args.data).read_text())

    if args.kind == "symbols":
        if args.format != "html":
            print("--kind symbols only supports --format html", file=sys.stderr)
            return 1
        sys.stdout.write(render_symbols(data))
        return 0

    sys.stdout.write(render(args.kind, data, args.format))
    return 0


if __name__ == "__main__":
    sys.exit(main())
