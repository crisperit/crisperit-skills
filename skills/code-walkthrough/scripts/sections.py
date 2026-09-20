#!/usr/bin/env python3
"""Render a ready-to-embed symbol-delta section from symdelta.py output.

Usage:
  python3 sections.py --kind symbols --data symdelta.json --format html > section.html

The diagram, its legend, its caption and its data-ids map are built here, in code, so they
cannot go missing or get paraphrased by whoever assembles the page. A renderer inserts this
output verbatim; it does not rewrite it.

The first line of the output is an HTML comment marker, `<!-- code-walkthrough:symbols -->`, which
survives GitHub's sanitizer and renders as nothing. validate_analysis.py --sections uses it to
prove the section actually reached the rendered file.

Prints nothing at all when the analysis found no nodes to draw, so an empty section is never
emitted.

Stdlib only, no network.
"""

import argparse
import json
import re
import sys
from pathlib import Path

SYMBOLS_MARKER = "<!-- code-walkthrough:symbols -->"


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


_MM_UNSAFE_RE = re.compile(r'[()"`]')

# The two pre-rendered detail levels. Index 0 is what the page shows first. There were three:
# symbols with and without their outside callers were separate levels, and on a real PR they
# differed by nine nodes out of thirty -- a detail nudge dressed up as a view, which is what
# made a slider feel arbitrary. Callers are always in now.
LEVEL_NAMES = ["Packages", "Symbols"]

# Mermaid can't resolve a CSS var() inside linkStyle, so it carries a literal hex mirroring the
# page's default (dark-theme) --accent/--muted; there's no page CSS for edges either way. The
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


ZWSP = "\u200b"


def wrap_label(label, target=LABEL_WRAP_TARGET):
    """Give `label` break opportunities mermaid can use, without adding or removing a character
    of it.

    This used to join pre-broken lines with `<br/>`. Mermaid 11.15 under `securityLevel: 'strict'`
    draws a node label as an HTML `foreignObject` and its sanitiser *deletes* `<br/>` outright, so
    "picks winner<br/>bid" reached the page as "picks winnerbid": two words silently welded
    together, and the label clipped at the box edge. Verified in Chrome against the vendored
    build; do not put `<br/>` back.

    What that label div does have is `white-space: break-spaces` and a `wrappingWidth` max-width,
    so mermaid wraps on spaces by itself and needs no help there. The one case it cannot wrap is a
    single space-free word longer than the box -- a package path, a snake_case identifier, a Go
    `Type.Method` selector, a run-together camelCase name -- which grows the node sideways instead.
    `_split_overlong` already knows where such a word wants to break, so its split points get a
    zero-width space: invisible, survives the sanitiser, and gives the wrapper somewhere to break.

    Nothing is ever dropped; the text is exactly the input plus zero-width spaces."""
    words = label.split()
    if not words:
        return label
    return " ".join(
        ZWSP.join(_split_overlong(word, target)) if len(word) > target else word
        for word in words
    )


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
    graph's note instead (see `_nested_inner_id`).

    Returns `(mermaid_text, undrawn_count, ids)`, where `ids` maps each drawn box's mermaid id to
    the package's own path -- a package node's id already is its path (see symdelta.py's
    `_collapse_chains`). That map is what gives a package box the node menu the symbols level
    gets from its file map."""
    pkg_by_id = {n["id"]: n for n in nodes if n["kind"] == "pkg"}
    if not pkg_by_id:
        return "flowchart LR", 0, {}
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
    # A root package can have an empty id (the repo root itself); it would prefix-match every
    # path in the walkthrough, so it gets no entry and stays menu-less.
    ids = {mm: pid for pid, mm in mm_id.items() if pid}
    return "\n".join(lines), undrawn_count, ids


MAX_SYMBOL_NODES = 400


def _scope_to_paths(nodes, edges, paths):
    """Cut the delta down to the area the page is about. Explain mode diffs against the empty
    baseline, so symdelta reports every symbol in the repository as new; without a scope, a page
    explaining one package got the whole tree (measured on a real repo: 3909 nodes, 5905 edges,
    611 KB of mermaid, enough to hang the tab). Kept: a symbol whose file sits under one of
    `paths`, every edge touching one, the symbol at the far end of such an edge so callers and
    callees still show one hop out, and the package ancestors needed to contain what is left.
    Empty `paths` means no scope, which is what diff mode passes -- its delta is already small.
    A `paths` entry of ".", "", or "./" also means no scope: node `file` values are plain
    repo-relative paths that never start with "./" or ".", so once normalized any of those three
    collapse to an empty prefix that would otherwise match nothing instead of the whole repo
    (SKILL.md documents "." as legitimate whole-repo scope)."""

    def _norm(p):
        p = p[2:] if p.startswith("./") else p
        p = p.rstrip("/")
        return "" if p == "." else p

    prefixes = [_norm(p) for p in paths]
    if not prefixes or any(p == "" for p in prefixes):
        return nodes, edges

    def in_scope(node):
        path = node.get("file") or ""
        return any(path == pre or path.startswith(pre + "/") for pre in prefixes)

    subjects = {n["id"] for n in nodes if n["kind"] == "symbol" and in_scope(n)}
    kept_edges = [e for e in edges if e["source"] in subjects or e["target"] in subjects]
    keep = set(subjects)
    for e in kept_edges:
        keep.add(e["source"])
        keep.add(e["target"])

    pkg_by_id = {n["id"]: n for n in nodes if n["kind"] == "pkg"}
    pkgs = set()
    for n in nodes:
        if n["kind"] != "symbol" or n["id"] not in keep:
            continue
        pid = n.get("parent")
        if pid is not None:
            pkgs.add(pid)
            pkgs.update(_pkg_ancestor_ids(pid, pkg_by_id))
    keep |= pkgs
    return [n for n in nodes if n["id"] in keep], kept_edges


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


def _mermaid_symbols(nodes, symbol_ids, kept_edges, explain=False):
    """Levels 2 and 3 share this: symbol_ids grouped by package, nested the same way level 1 is
    -- a package that owns an in-scope symbol gets a box, and so does every ancestor needed to
    contain it, so `corelib/ratelimit` sits inside one `corelib` box rather than beside it. A
    package with both an in-scope symbol of its own and child packages gets that symbol listed
    directly inside its box, alongside the nested child boxes. Nodes get `class ... new/gone`,
    for the legend's new/gone check, unless `explain` is set. Edges have no per-edge classDef at all, only
    `linkStyle <index>`, so they're styled by their position in the diagram instead.

    Returns `(mermaid_text, id_to_file)`: the second is the mermaid `S<n>` id of every symbol
    node mapped to its file path, for the page's node menu to resolve a click to a file. A
    package box carries no single file of its own, so it is never a key here."""
    if not symbol_ids:
        return "flowchart LR", {}
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
        # title that wraps has its second line painted under the border and lost. A long
        # single-line title overhangs the box instead, which is readable; a clipped one is not.
        lines.append(f'{pad}subgraph {gid}["{_mm_escape(_pkg_label(pid, pkg_by_id))}"]')
        for sid in sorted(symbols_by_pkg.get(pid, [])):
            mm_id[sid] = f"S{len(mm_id)}"
            # A rename resolved across a package move (see resolve_symbol_merges in symdelta.py,
            # e.g. a Go export-capitalisation change) carries its pre-rename name as `was`. It
            # rides in the same label, comma-joined rather than on a forced second line: there is
            # no way to force one (see wrap_label), and the label wraps on the space anyway.
            text = by_id[sid]["label"]
            was = by_id[sid].get("was")
            if was:
                text = f"{text}, was {was}"
            label = wrap_label(_mm_escape(text))
            lines.append(f'{pad}  {mm_id[sid]}["{label}"]')
        for kid in sorted(children.get(pid, [])):
            emit(kid, indent + 1)
        lines.append(f"{pad}end")

    for root in sorted(roots, key=lambda p: p or ""):
        emit(root, 1)

    # Iterates mm_id, not the caller's symbol_ids set directly: mm_id was just built in a fixed
    # (package, then symbol) sort order, and a set's own iteration order isn't stable run to run.
    # Explain mode diffs against the empty baseline, so every symbol and every edge comes back
    # "new": colouring them all green and printing a legend for a gone state that cannot occur
    # says nothing. Left plain, the graph reads as structure, which is what it is there for.
    new_ids = [] if explain else [nid for sid, nid in mm_id.items() if by_id[sid].get("state") == "new"]
    gone_ids = [] if explain else [nid for sid, nid in mm_id.items() if by_id[sid].get("state") == "gone"]
    if new_ids:
        lines.append(f'  class {",".join(new_ids)} new')
    if gone_ids:
        lines.append(f'  class {",".join(gone_ids)} gone')

    new_links, gone_links = [], []
    for idx, e in enumerate(kept_edges):
        lines.append(f'  {mm_id[e["source"]]} --> {mm_id[e["target"]]}')
        if not explain:
            (new_links if e["state"] == "new" else gone_links).append(str(idx))
    if new_links:
        lines.append(f'  linkStyle {",".join(new_links)} stroke:{LINK_COLOR_NEW},stroke-width:2px;')
    if gone_links:
        lines.append(f'  linkStyle {",".join(gone_links)} stroke:{LINK_COLOR_GONE},stroke-dasharray:4 4;')

    edge_pairs = [(e["source"], e["target"]) for e in kept_edges]
    lines.extend(_chain_components_lines(symbol_ids, edge_pairs, mm_id))
    id_to_file = {nid: by_id[sid]["file"] for sid, nid in mm_id.items() if by_id[sid].get("file")}
    return "\n".join(lines), id_to_file


def _symbols_note_text(node_count, edge_count, drawn_count, listed_count, undrawn_count,
                       explain=False, oversized_count=0):
    """The counts sentence shared by both formats: since the symbols level holds back an
    edge-less changed symbol as text instead of a node, how many landed each way -- so a reader
    is never left wondering whether one was silently dropped (see render_symbols). The trailing
    sentence names `undrawn_count` (see `_mermaid_packages`), the total relations skipped because
    one endpoint's package nests inside the other's -- omitted entirely when it's zero, so the
    note never states a count that adds nothing."""
    parts = [
        f"Showing {node_count} packages and symbols, {edge_count} relations between them.",
        f"{drawn_count} symbols drawn as nodes, {listed_count} listed below." if explain else
        f"{drawn_count} changed symbols drawn as nodes, {listed_count} listed below.",
    ]
    if undrawn_count:
        parts.append(
            f"{undrawn_count} relations between a package and a package inside it are not "
            "drawn because the containment box already shows them."
        )
    if oversized_count:
        parts.append(
            f"The symbols level holds {oversized_count} boxes, too many to open unasked, so "
            "this starts on packages. Switch with the button above to draw it."
        )
    return " ".join(parts)


def _symbols_note(node_count, edge_count, drawn_count, listed_count, undrawn_count,
                  explain=False, oversized_count=0):
    """html counterpart of _symbols_note_text: the legend is generated markup now
    (`.vd-legend`), not a paragraph, so this is the only remaining prose."""
    return _html_note(
        _symbols_note_text(node_count, edge_count, drawn_count, listed_count, undrawn_count,
                           explain, oversized_count)
    )


def _symbols_orphan_rows(nodes, orphan_ids):
    """(package label, symbol label, state) tuples for changed symbols _mermaid_symbols never
    gets to draw, grouped by package then by symbol -- the sort order both format's listings
    share. [] when nothing was held back."""
    if not orphan_ids:
        return []
    by_id = {n["id"]: n for n in nodes}
    pkg_by_id = {n["id"]: n for n in nodes if n["kind"] == "pkg"}
    return sorted(
        (_pkg_label(by_id[sid].get("parent"), pkg_by_id), by_id[sid]["label"], by_id[sid]["state"])
        for sid in orphan_ids
    )


def _symbols_orphans(nodes, orphan_ids, explain=False):
    """The compact text list for changed symbols _mermaid_symbols never gets to draw, marked
    (new)/(gone) the same way the old structure caption marked an appeared/disappeared
    structure. Reuses the `.vd-moved` list styling rather than adding page CSS for a second
    list. Empty (no list at all) when nothing was held back."""
    rows = _symbols_orphan_rows(nodes, orphan_ids)
    if not rows:
        return ""
    items = "".join(
        f"<li>{escape(pkg)}: {escape(label)}</li>" if explain else
        f"<li>{escape(pkg)}: {escape(label)} ({state})</li>"
        for pkg, label, state in rows
    )
    return f'<ul class="vd-moved">{items}</ul>\n'


_EMPTY_LEVEL_LABEL = "Nothing to draw at this detail level"


def _mermaid_symbols_for_level(nodes, symbol_ids, kept_edges, explain=False):
    """_mermaid_symbols, except when filtering to edge-having symbols leaves nothing at all: a
    bare `flowchart LR` renders at a near-zero viewBox, which the page's own `degenerate()`
    check (diff-review-template.html) mistakes for a real render failure rather than a level
    that legitimately has nothing to draw. One placeholder node keeps it a valid diagram, with
    no id to map since it draws nothing real."""
    if not symbol_ids:
        return f'flowchart LR\n  N0["{_EMPTY_LEVEL_LABEL}"]', {}
    return _mermaid_symbols(nodes, symbol_ids, kept_edges, explain)


def _moved_lines(moved):
    """One plain-text line per symdelta.py `moved` entry, shared by both formats' lists."""
    return [
        f"{m.get('callsites', 0)} call sites moved, {m.get('from', '')} -> {m.get('to', '')}"
        for m in moved
    ]


def render_symbols(data, explain=False, paths=()):
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

    nodes, edges = _scope_to_paths(nodes, edges, paths)
    if not nodes:
        return ""

    level1, undrawn_count, pkg_map = _mermaid_packages(nodes, edges)
    ids2, edges2 = _symbols_scope(nodes, edges)
    drawn2 = _edge_endpoint_ids(edges2)
    drawn_ids2 = ids2 & drawn2
    level2, file_map2 = _mermaid_symbols_for_level(nodes, drawn_ids2, edges2, explain)

    # Past this many symbol boxes the level is still worth having, just not worth opening
    # unasked: the page renders a level the first time it is shown, so starting on packages
    # leaves the big one to a deliberate click. See MAX_SYMBOL_NODES.
    oversized = len(drawn_ids2) > MAX_SYMBOL_NODES
    default_level = 1 if oversized else 2

    changed_ids = _changed_symbol_ids(nodes)
    orphan_ids = changed_ids - drawn2

    note = _symbols_note(
        len(nodes), len(edges), len(changed_ids) - len(orphan_ids), len(orphan_ids), undrawn_count,
        explain, len(drawn_ids2) if oversized else 0,
    )
    names_attr = escape(json.dumps(LEVEL_NAMES)).replace('"', "&quot;")
    orphans_html = _symbols_orphans(nodes, orphan_ids, explain)

    moved_html = ""
    if moved:
        items = "".join(f"<li>{escape(line)}</li>" for line in _moved_lines(moved))
        moved_html = f'<ul class="vd-moved">{items}</ul>\n'

    # The legend is per level, not per section: a level with no new and no gone node was
    # spending a row on two swatches for states that were not on screen. Both levels get one
    # so the script can swap it with the graph.
    legends = [_symbols_legend(level1), _symbols_legend(level2)]
    # This module's escape() leaves quotes alone, so an attribute holding markup needs the
    # same &quot; pass names_attr above does or the first inner quote ends the attribute.
    legend_attrs = [escape(x).replace('"', "&quot;") for x in legends]
    # Level 1's map holds directories and level 2's files; the page treats both the same way,
    # resolving a clicked box to a walkthrough hunk by exact path or, for a directory, by its
    # first file. Same escape/&quot; idiom as data-names above.
    ids_attrs = [escape(json.dumps(m, sort_keys=True)).replace('"', "&quot;")
                 for m in (pkg_map, file_map2)]
    here = default_level - 1
    there = 1 - here
    return "\n".join([
        SYMBOLS_MARKER,
        f'<div class="vd-symbols" data-default="{default_level}">',
        # Title left, controls right, matching the walkthrough heading. The button names the
        # level you are looking at rather than the one it switches to, which is why the
        # slider's caption underneath is gone. No aria-pressed: a label reading "packages" and
        # a state reading "pressed" say different things, so the accessible name spells out
        # both the state and what activating does, and the script keeps it in step.
        f'<h2 class="h2-row"><span>{"Structure" if explain else "Changes visualization"}'
        '</span><span class="ctl">'
        f'{legends[here]}'
        '<button type="button" id="vd-level-toggle" '
        f'data-names="{names_attr}" aria-label="Detail level: {LEVEL_NAMES[here].lower()}.'
        f' Activate to show {LEVEL_NAMES[there].lower()}."'
        f'>{escape(LEVEL_NAMES[here].lower())}</button></span></h2>',
        f'<div class="mermaid" data-level="1"{"" if default_level == 1 else " hidden"} '
        f'data-legend="{legend_attrs[0]}" data-ids="{ids_attrs[0]}">{escape(level1)}</div>',
        f'<div class="mermaid" data-level="2"{"" if default_level == 2 else " hidden"} '
        f'data-legend="{legend_attrs[1]}" data-ids="{ids_attrs[1]}">{escape(level2)}</div>',
        f'<p class="note">{note}</p>',
    ]) + "\n" + orphans_html + moved_html + "</div>\n"


_LEGEND_STATE_RE = re.compile(r"^  class \S+ (new|gone)$", re.MULTILINE)


def _symbols_legend_states(mermaid_source):
    """Which of new/gone this level's diagram actually marks. Matches the literal `  class
    <ids> new/gone` line _mermaid_symbols emits, not a plain substring scan -- a node label
    can contain the word `new` or `gone` as prose (e.g. a `was` line) without the diagram
    marking that state at all."""
    states = set(_LEGEND_STATE_RE.findall(mermaid_source))
    return "new" in states, "gone" in states


def _symbols_legend(mermaid_source):
    """The new/gone key, emitted only for a level whose own diagram actually carries such a
    node: a package view with neither state was spending a row explaining both."""
    has_new, has_gone = _symbols_legend_states(mermaid_source)
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
    parser.add_argument("--kind", required=True, choices=("symbols",))
    parser.add_argument("--data", required=True)
    parser.add_argument("--format", required=True, choices=("html",))
    parser.add_argument("--explain", action="store_true",
                        help="the target is code as it stands, not a change: drop the new/gone "
                             "colouring and legend, which the empty baseline makes meaningless")
    parser.add_argument("--paths", nargs="*", default=[],
                        help="repo-relative files or directories the page is about; the graph "
                             "is cut down to symbols under them plus one hop out. Needed in "
                             "explain mode, where the empty baseline makes the whole repo new")
    args = parser.parse_args()

    data = json.loads(Path(args.data).read_text())
    sys.stdout.write(render_symbols(data, args.explain, args.paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
