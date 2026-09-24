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

Prints nothing at all when the analysis found no nodes to draw. The one exception is
`language: null` (symdelta.py couldn't build a graph, see `render_symbols`): that gets a short
note explaining why instead of silence, since a missing section reads exactly like a diff that
legitimately drew nothing.

Stdlib only, no network.
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

SYMBOLS_MARKER = "<!-- code-walkthrough:symbols -->"
STRUCTURE_MARKER = "<!-- code-walkthrough:structure -->"


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


_MAX_BUTTON_LABEL = "Expand diagram to full size"

# One button, pasted verbatim into every diagram's header row (this module's own two, plus
# render.py's page-level FLOW and walkthrough.py's per-group tab strip, which both import this
# rather than growing a second copy). Icon only, wording carried by aria-label/title alone,
# reusing wire()'s own label from the template so the two affordances read as one.
_MAX_BUTTON_HTML = (
    f'<button type="button" class="vd-max-btn" aria-label="{_MAX_BUTTON_LABEL}" '
    f'title="{_MAX_BUTTON_LABEL}"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
    'aria-hidden="true" focusable="false"><path d="M8 3H5a2 2 0 0 0-2 2v3"/>'
    '<path d="M16 3h3a2 2 0 0 1 2 2v3"/><path d="M8 21H5a2 2 0 0 1-2-2v-3"/>'
    '<path d="M16 21h3a2 2 0 0 0 2-2v-3"/></svg></button>'
)


def _diagram_max_button():
    return _MAX_BUTTON_HTML


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


# A label's opening delimiter: one or two of mermaid's bracket-shape characters directly
# before the quote -- rectangle `["`, subroutine `[["`, cylinder `[("`, stadium `(["`, round
# `("`, circle `(("`, diamond `{"`, hexagon `{{"`, and the asymmetric flag `>"`. Matching just
# this prefix finds the label regardless of which shape closes it.
_MERMAID_LABEL_RE = re.compile(r'([\[({>]{1,2})"([^"]*)"')

# An edge label, `-->|text|` or `-->|"text"|`, anchored on the arrow so a `|` used for
# something else on the line can't be mistaken for one. Matches any arrow shape (solid, dotted,
# thick, `--x`/`--o`, and their `<`-reversed forms); quoted and bare are separate groups so the
# substitution can put back exactly the quoting it found.
_MERMAID_EDGE_LABEL_RE = re.compile(r'(<?[-=.]+[-=]?[>ox])\|(?:"([^"|\n]*)"|([^|\n]*))\|')


def _wrap_edge_label(m):
    arrow, quoted, bare = m.groups()
    if quoted is not None:
        return f'{arrow}|"{wrap_label(quoted)}"|'
    return f'{arrow}|{wrap_label(bare)}|'


def _wrap_flow_labels(mermaid):
    """Run every quoted node/subgraph label, and every edge label, in a flow diagram through
    `wrap_label`, so a long space-free identifier gets somewhere to break instead of stretching
    its node across the page (see `wrap_label` for why it is zero-width spaces and not `<br/>`).

    Shared by render.py's top-level FLOW and walkthrough.py's per-group flow, one owner for the
    regex pair rather than two copies drifting apart. Safe to run unconditionally on a
    `sequenceDiagram` too: its message syntax (`A->>B: text`) never matches either regex, since
    both require the bracket/pipe shapes only a flowchart node or edge label uses."""
    mermaid = _MERMAID_LABEL_RE.sub(lambda m: f'{m.group(1)}"{wrap_label(m.group(2))}"', mermaid)
    return _MERMAID_EDGE_LABEL_RE.sub(_wrap_edge_label, mermaid)


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


MAX_SYMBOL_NODES = 40


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

    Returns `(mermaid_text, id_to_file, id_to_loc)`: the second is the mermaid `S<n>` id of
    every symbol node mapped to its file path, for the page's node menu to resolve a click to a
    file. A package box carries no single file of its own, so it is never a key in either map.
    The third maps a node's id to its line range and side, `{"start", "end", "side"}`, for
    nodes that carry both a `range` and a `file` -- the LLM edge tier's symbols never do, since
    that tier is position-less by schema."""
    if not symbol_ids:
        return "flowchart LR", {}, {}
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
    # side mirrors wireHunk's LEFT/RIGHT: a gone symbol's range is BASE-side (drawn on the
    # diff's left), anything else is HEAD-side.
    id_to_loc = {
        nid: {"start": by_id[sid]["range"][0], "end": by_id[sid]["range"][1],
              "side": "LEFT" if by_id[sid].get("state") == "gone" else "RIGHT"}
        for sid, nid in mm_id.items() if by_id[sid].get("range") and by_id[sid].get("file")
    }
    return "\n".join(lines), id_to_file, id_to_loc


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


def _packages_note_text(pkg_count, edge_count, undrawn_count):
    """The page-level graph's note: unlike `_symbols_note_text`, there is no drawn/listed split
    to report here, since the page level never holds a symbol back as an orphan -- it never
    draws a symbol at all. `edge_count` counts the same (scoped) symbol edges the page-level
    diagram is built from, not the rolled-up package arrows on screen, matching what the
    two-level note used to report before the split."""
    parts = [f"Showing {pkg_count} packages, {edge_count} relations between them."]
    if undrawn_count:
        parts.append(
            f"{undrawn_count} relations between a package and a package inside it are not "
            "drawn because the containment box already shows them."
        )
    return " ".join(parts)


def _packages_note(pkg_count, edge_count, undrawn_count):
    return _html_note(_packages_note_text(pkg_count, edge_count, undrawn_count))


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
        return f'flowchart LR\n  N0["{_EMPTY_LEVEL_LABEL}"]', {}, {}
    return _mermaid_symbols(nodes, symbol_ids, kept_edges, explain)


def _moved_lines(moved):
    """One plain-text line per symdelta.py `moved` entry, shared by both formats' lists."""
    return [
        f"{m.get('callsites', 0)} call sites moved, {m.get('from', '')} -> {m.get('to', '')}"
        for m in moved
    ]


_NO_SUPPORTED_LANGUAGE_PREFIX = "no supported files ("


def _symdelta_failure_text(data):
    """Prose for symdelta.py's `language: null`: what was attempted, why, and the fix when
    there is one. detect_language's own "nothing to analyse" reason is the one `null` that
    isn't a failure, so it gets a plain factual line instead -- matched by its fixed wording,
    the only signal this dict carries for that case. The `--doctor` pointer rides along only
    when there's a `remedy`: a dependency-incompatibility refusal (package.json/lockfile
    changed) has none and nothing to do with language-server setup, and `--doctor` would just
    report everything fine, which is no help there."""
    reason = data.get("reason") or "no reason given"
    if reason.startswith(_NO_SUPPORTED_LANGUAGE_PREFIX):
        return f"No call graph: {reason}."
    parts = [f"Call graph: attempted, not built. {reason}."]
    remedy = data.get("remedy")
    if remedy:
        parts.append(f"Fix: `{remedy}`.")
        parts.append("Run `symdelta.py --doctor` to check language-server setup.")
    return " ".join(parts)


def _llm_caveat_text():
    """Caveat for `resolver == "llm"`: the graph below is real and shown, but its edges came
    from an LLM reading the code rather than a compiler resolving it, so a caller/callee pair
    can be wrong in a way go/packages or the LSP tier would never produce."""
    return ("This graph was inferred by an LLM reading the code, not resolved by a compiler. "
            "Edges may be wrong.")


def _llm_caveat():
    return _html_note(_llm_caveat_text())


def render_symbols(data, explain=False, paths=(), inline=False):
    """The symbol-delta diagram. At page scope (`inline=False`) this is packages only: a big
    diff's symbols level is unreadable laid out across a whole page, so that detail now lives
    per group instead (`inline=True`, from walkthrough.py), where the scope is already narrow
    enough to read. `flowchart LR`, not TB: these graphs are dominated by fan-out (one caller
    reaching several callees), and TB spreads that fan sideways -- the wrong axis for a phone --
    where LR grows it downward and the page just scrolls. `moved` is rendered as prose, not as
    graph nodes: a symbol that only changed package sits outside the diff, and its callers --
    not its own address -- are what a call-graph edge is about. A package-level move edge was
    tried and removed because it breaks the containment layout: dagre has to place the vacated
    package outside the box it used to nest in, and the arrow crosses the whole diagram to
    reach it.

    `inline=True` adds the symbols level and its toggle: same two levels, same legend, only the
    frame differs from the page-level section -- no `SYMBOLS_MARKER`, no heading, and a
    `data-symbols` count of changed symbols in scope for the template's dialog button label.
    Mermaid has no live collapse/expand of its own, so each level is a complete diagram rather
    than one graph the reader folds. The symbols level draws a changed symbol only if it takes
    part in its edges: most changed symbols have no changed call edge, and dagre lays out every
    edge-less node in one wide rank (measured on a real PR: 25 such nodes side by side, each as
    wide as its identifier, blowing the diagram out to thousands of pixels). A symbol dropped
    from the graph still has to reach the reader, so _symbols_orphans lists it as text instead.
    The template positions the block off-screen via CSS rather than `display:none`, since
    mermaid.render() measures a hidden container's labels as zero and collapses the diagram.

    `language: null` (symdelta.py couldn't build a graph at all) is never silent at page scope:
    the reader gets a `.note` saying why instead of a section that just isn't there. A group's
    own call-graph tab, `inline=True`, still has nothing to show and stays empty -- repeating
    the same line per group would be noise the page-level note already covered once. Checked as
    `"language" in data`, not truthiness: a bare `{}` (no symdelta.json at all) is not the same
    as symdelta.py having run and explicitly failed, and must still fall through to the plain
    no-nodes empty string below."""
    if "language" in data and data["language"] is None:
        return "" if inline else "\n".join([
            SYMBOLS_MARKER,
            '<div class="vd-symbols">',
            f'<h2>{"Structure" if explain else "Changes visualization"}</h2>',
            f'<p class="note">{_html_note(_symdelta_failure_text(data))}</p>',
            '</div>',
        ]) + "\n"

    nodes = data.get("nodes") or []
    if not nodes:
        return ""
    edges = data.get("edges") or []
    moved = data.get("moved") or []

    nodes, edges = _scope_to_paths(nodes, edges, paths)
    if not nodes:
        return ""

    level1, undrawn_count, pkg_map = _mermaid_packages(nodes, edges)

    moved_html = ""
    if moved:
        items = "".join(f"<li>{escape(line)}</li>" for line in _moved_lines(moved))
        moved_html = f'<ul class="vd-moved">{items}</ul>\n'

    if not inline:
        # No toggle, no symbols level: _mermaid_packages never marks a new/gone class, so
        # there is no legend to show either. The page-level graph is exactly what it draws.
        pkg_count = sum(1 for n in nodes if n["kind"] == "pkg")
        note = _packages_note(pkg_count, len(edges), undrawn_count)
        ids_attr = escape(json.dumps(pkg_map, sort_keys=True)).replace('"', "&quot;")
        caveat_html = f'<p class="note">{_llm_caveat()}</p>\n' if data.get("resolver") == "llm" else ""
        body = "\n".join([
            f'<div class="mermaid" data-ids="{ids_attr}">{escape(level1)}</div>',
            f'<p class="note">{note}</p>',
        ]) + "\n" + caveat_html + moved_html
        heading = "Structure" if explain else "Changes visualization"
        return "\n".join([
            SYMBOLS_MARKER,
            '<div class="vd-symbols">',
            f'<h2 class="h2-row"><span>{heading}</span><span class="ctl">'
            f'{_diagram_max_button()}</span></h2>',
        ]) + "\n" + body + "</div>\n"

    _, edges2 = _symbols_scope(nodes, edges)
    drawn_ids2 = _edge_endpoint_ids(edges2)
    level2, file_map2, loc_map2 = _mermaid_symbols_for_level(nodes, drawn_ids2, edges2, explain)

    # Legibility, not layout cost, sets this threshold: measured in Chrome, a symbols level of
    # ~70 nodes draws 8478px wide, rendering at 0.16x -- 25x9px boxes, ~3px labels, a click a
    # coin flip between a node and its background. Past MAX_SYMBOL_NODES a group's own symbols
    # level opens on packages by default instead, leaving the full graph to a deliberate click.
    oversized = len(drawn_ids2) > MAX_SYMBOL_NODES
    default_level = 1 if oversized else 2

    changed_ids = _changed_symbol_ids(nodes)
    orphan_ids = changed_ids - drawn_ids2

    note = _symbols_note(
        len(nodes), len(edges), len(changed_ids) - len(orphan_ids), len(orphan_ids), undrawn_count,
        explain, len(drawn_ids2) if oversized else 0,
    )
    names_attr = escape(json.dumps(LEVEL_NAMES)).replace('"', "&quot;")
    orphans_html = _symbols_orphans(nodes, orphan_ids, explain)

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
    # data-lines only on level 2: level 1 draws packages, which own no line range of their own.
    # Omitted outright when empty, same as every other conditional attribute on this page,
    # rather than shipping an empty `{}` the script would have to special-case.
    lines_json = escape(json.dumps(loc_map2, sort_keys=True)).replace('"', "&quot;")
    lines_attr = f' data-lines="{lines_json}"' if loc_map2 else ""
    here = default_level - 1
    there = 1 - here

    # The button names the level you are looking at rather than the one it switches to, which
    # is why the slider's caption underneath is gone. No aria-pressed: a label reading
    # "packages" and a state reading "pressed" say different things, so the accessible name
    # spells out both the state and what activating does, and the script keeps it in step.
    # A class, not an id: a per-group graph puts more than one of these on the page.
    toggle = ('<button type="button" class="vd-level-toggle" '
              f'data-names="{names_attr}" aria-label="Detail level: {LEVEL_NAMES[here].lower()}.'
              f' Activate to show {LEVEL_NAMES[there].lower()}."'
              f'>{escape(LEVEL_NAMES[here].lower())}</button>')
    body = "\n".join([
        f'<div class="mermaid" data-level="1"{"" if default_level == 1 else " hidden"} '
        f'data-legend="{legend_attrs[0]}" data-ids="{ids_attrs[0]}">{escape(level1)}</div>',
        f'<div class="mermaid" data-level="2"{"" if default_level == 2 else " hidden"} '
        f'data-legend="{legend_attrs[1]}" data-ids="{ids_attrs[1]}"{lines_attr}>{escape(level2)}</div>',
        f'<p class="note">{note}</p>',
    ]) + "\n" + (
        f'<p class="note">{_llm_caveat()}</p>\n' if data.get("resolver") == "llm" else ""
    ) + orphans_html + moved_html

    return "\n".join([
        f'<div class="vd-symbols vd-symbols-group" data-symbols="{len(changed_ids)}" '
        f'data-default="{default_level}">',
        f'<div class="vd-ctl">{legends[here]}{toggle}</div>',
    ]) + "\n" + body + "</div>\n"


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


# ---------- structure.py's component/edge view ----------


def _structure_failure_text(data):
    """Prose for structure.py's `language: null`: the reason it gives, same short
    note-instead-of-a-view treatment `_symdelta_failure_text` gives symdelta's own null."""
    return f"No structure view: {data.get('reason') or 'no reason given'}."


_COMPONENT_STATE_BADGE = {
    "new": "+ new", "changed": "~ changed", "moved": "↪ moved",
    "removed": "− removed", "unchanged": "unchanged",
}
_COMPONENT_STATE_CLASS = {
    "new": "new", "changed": "chg", "moved": "mov", "removed": "gone", "unchanged": "unc",
}
_MEMBER_STATE_CLASS = {"new": "n", "changed": "c", "unchanged": "o", "removed": "r"}
# A leading glyph per state, same +/~/- vocabulary the component badge uses: colour alone would
# leave new and changed (and unchanged) indistinguishable to a reader who can't see it. Removed
# also keeps its own strikethrough (a shape cue, in the CSS); unchanged gets no glyph at all --
# blank is itself the fourth, distinct state, not a fifth colour.
_MEMBER_STATE_GLYPH = {"new": "+", "changed": "~", "removed": "−"}
# Caps how many unchanged names the title lists -- a type with dozens of untouched members (a Go
# receiver, say) shouldn't balloon the attribute; past this many it just says how many more, same
# idea as apply_cap's own dropped count in structure.py.
_MAX_UNCHANGED_TITLE_NAMES = 20


def _structure_members_html(members):
    """Member chips, one per touched (`_member_states` state != unchanged) entry, coded n/c/r
    for new/changed/removed -- text (a leading glyph) and shape (removed's strikethrough) both
    carry the state, never colour alone. Unchanged members collapse into one "+N unchanged" chip
    (muted, same styling as an unadorned chip) instead of a chip each; their names move to that
    chip's title so they stay discoverable on hover."""
    if not members:
        return ""
    touched = [m for m in members if m.get("state") != "unchanged"]
    unchanged = [m for m in members if m.get("state") == "unchanged"]
    chips = "".join(
        f'<i class="{_MEMBER_STATE_CLASS.get(m.get("state"), "o")}">'
        f'{_MEMBER_STATE_GLYPH.get(m.get("state"), "")}{escape(m.get("name") or "")}</i>'
        for m in touched
    )
    if unchanged:
        names = [m.get("name") or "" for m in unchanged]
        shown = ", ".join(names[:_MAX_UNCHANGED_TITLE_NAMES])
        if len(names) > _MAX_UNCHANGED_TITLE_NAMES:
            shown += ", …"
        title = escape(shown).replace('"', "&quot;")
        chips += f'<i class="o" title="{title}">+{len(unchanged)} unchanged</i>'
    return f'<span class="vds-members">{chips}</span>'


def _structure_component_html(comp, dcol, group_color, explain=False):
    """One component box, keyboard-operable and click-operable: a menu (see the template
    script) opens on either, offering "Go to walkthrough" / "Focus" / "Clear focus". `dcol` is
    the box's *display* column (its index into the rendered columns list, not `comp["column"]`
    itself, which can skip values) -- the edge router needs it to tell an adjacent-column call
    from one that skips a column or runs backward. `group_color` is `walkthrough._group_color`,
    passed in rather than imported at call time per box, so a page with many components pays for
    the (deferred, see render_structure) import once."""
    state = comp.get("state") or "unchanged"
    cls = "" if explain else f' {_COMPONENT_STATE_CLASS.get(state, "unc")}'
    group = comp.get("group")
    style = f' style="--c:{group_color(group)}"' if group is not None else ""
    # The badge names the state in words, never colour alone; dropped in explain mode along with
    # the member chips below, the same empty-baseline-makes-it-meaningless reasoning
    # render_symbols already applies to new/gone.
    badge = "" if explain else (
        f'<span class="vds-badge">{escape(_COMPONENT_STATE_BADGE.get(state, state))}</span>'
    )
    members = "" if explain else _structure_members_html(comp.get("members") or [])
    # comp["id"] is "file:symbol" -- a real file path, not a fixed token -- so it needs the same
    # &quot; pass every other JSON/id attribute on this page gets; a bare escape() leaves a
    # literal `"` free to end the attribute early. Carried as data-cid, not a plain `id`: the
    # template's own dialog clones this box wholesale for the full-size view, and two elements
    # sharing one `id` while the dialog is open is invalid markup a data attribute avoids.
    box_id = escape(comp["id"]).replace('"', "&quot;")
    path = escape(comp.get("file") or "").replace('"', "&quot;")
    # Same &quot; pass again: this one lands inside aria-label rather than data-cid, but a name
    # carrying a literal `"` (an unusual but legal TS/Go identifier is not the concern here --
    # a hostile component id smuggled in as the display name is) would end the attribute early
    # exactly the same way.
    name_attr = escape(comp["name"]).replace('"', "&quot;")
    return (
        f'<div class="vds-comp{cls}" data-cid="{box_id}" tabindex="0" role="button" '
        f'aria-label="Open menu for {name_attr}" '
        f'data-g="{group if group is not None else ""}" data-dcol="{dcol}" '
        f'data-path="{path}"{style}>{badge}'
        f'<b>{escape(comp["name"])}</b>'
        f'<span class="vds-file">{escape(comp.get("file") or "")}</span>{members}</div>'
    )


def _structure_columns(components):
    """Components grouped by their own `column` (walkthrough.caller_depth's rank: 0 is a file
    nothing else in the diff calls into), in ascending column order, one bucket per distinct
    value actually present -- a gap in the values (columns 0 and 3, nothing at 1 or 2) draws two
    columns, not four empty ones. Boxes within a column are sorted by structure.py's own
    barycenter-ordered `row` (falling back to name when a component carries none, e.g. an old
    structure.json from before that field existed), so a caller and its nearest callee land
    close in height instead of whatever order components happened to list in."""
    buckets = defaultdict(list)
    for c in components:
        buckets[c["column"]].append(c)
    for col in buckets.values():
        col.sort(key=lambda c: (c.get("row", 0), c["name"]))
    return [buckets[col] for col in sorted(buckets)]


def _structure_also_touched(also_touched):
    """A note under the diagram for structure.py's `also_touched`: real components with no call
    or implements/extends edge, so there is nothing to draw a box or a line for -- naming them
    still says the diff reached them. Each name goes through the page's own escape+codeify
    convention (backtick, then `_html_note`) rather than a bespoke <code> wrap, so it reads
    exactly like every other identifier list on the page."""
    if not also_touched:
        return ""
    names = ", ".join(f"`{name}`" for name in also_touched)
    return f'<p class="vds-also">{_html_note(f"Also touched: {names}")}</p>'


def _structure_chips(components, group_color, titles=None):
    """One filter chip per story stop (a `group` index some component actually carries) plus
    "All", coloured to match that stop's story-map colour and labelled with that stop's own
    title from structure.py's top-level `groups` (its own `analysis.json` doesn't reach this
    module otherwise) -- "2 Contracts", not just "2". `titles` missing a given index, or `None`
    entirely (structure.py ran with no `--analysis`, or that group's own title was blank), falls
    back to the plain ordinal for just that chip. No group data at all (every component's `group`
    is `None`) draws no chips at all, the same nothing-to-key-off-of reasoning `_symbols_legend`
    already applies to a level with no new/gone node."""
    groups = sorted({c["group"] for c in components if c.get("group") is not None})
    if not groups:
        return ""
    titles = titles or {}
    buttons = ['<button type="button" class="vds-chip" data-g="all" aria-pressed="true">All</button>']
    for gi in groups:
        title = titles.get(gi)
        # analysis.json's own group titles carry backticked identifiers same as `why`/`hop`
        # elsewhere on this page; _html_note's escape-then-codeify is what promotes them to
        # <code> instead of a literal backtick landing in the chip.
        label = _html_note(f"{gi + 1} {title}") if title else str(gi + 1)
        buttons.append(
            f'<button type="button" class="vds-chip" data-g="{gi}" aria-pressed="false" '
            f'style="--c:{group_color(gi)}">{label}</button>'
        )
    return ('<div class="vds-chips" role="group" aria-label="Highlight a story stop">'
            + "".join(buttons) + "</div>")


def _structure_edges(raw_edges, kept_ids, explain):
    """Call edges (structure.py's own `edges`) whose both ends survived the explain-mode
    removed-box filter. A `state: "gone"` edge is dropped outright in explain mode rather than
    drawn unstyled: there is no such thing as a removed call against the empty baseline."""
    out = []
    for e in raw_edges or []:
        src, dst = e.get("from"), e.get("to")
        if src not in kept_ids or dst not in kept_ids:
            continue
        if e.get("state") == "gone":
            if explain:
                continue
            out.append({"from": src, "to": dst, "kind": "call", "state": "gone"})
        else:
            out.append({"from": src, "to": dst, "kind": "call"})
    return out


def _structure_implements_edges(raw_implements, shown, kept_ids):
    """structure.py's `implements` list names its target by plain type name, not by component
    id (there is no cross-file type index to resolve it against), so this matches it against the
    kept components' own names -- the first one wins on a same-named collision, same tradeoff
    `_pkg_label` accepts elsewhere on this page. `shown` is `render_structure`'s already-ordered
    list (structure.py's own column/file/name sort), not `kept_ids` the set it used to iterate:
    a set's iteration order isn't stable run to run, so which component won a collision used to
    depend on PYTHONHASHSEED. A target matching no kept component (an external interface, or one
    the MAX_COMPONENTS cap dropped) is left undrawn rather than dangling."""
    name_to_id = {}
    for comp in shown:
        name_to_id.setdefault(comp["name"], comp["id"])
    out = []
    for item in raw_implements or []:
        src = item.get("from")
        if src not in kept_ids:
            continue
        dst = name_to_id.get(item.get("to"))
        if not dst or dst == src:
            continue
        out.append({"from": src, "to": dst, "kind": item.get("kind") or "implements"})
    return out


def _structure_summary_text(components, raw_edges, dropped, explain):
    """The summary strip's one sentence. Explain mode reads "N components in M columns" -- there
    is no before/after here, so a state breakdown would just restate that everything is new --
    plus how many the MAX_COMPONENTS cap dropped, when any were: that is a size fact about the
    diagram, not a state one, so it survives into explain mode where the rest of the counts do
    not. Review mode counts each component state instead, plus how many calls this diff removed,
    since that is the one edge state carried through at all (see _structure_edges)."""
    if explain:
        cols = len({c["column"] for c in components})
        text = (f"{len(components)} component{'s' if len(components) != 1 else ''} in "
                f"{cols} column{'s' if cols != 1 else ''}.")
        if dropped:
            text += f" {dropped} component{'s' if dropped != 1 else ''} dropped to fit the diagram."
        return text
    counts = Counter(c.get("state") or "unchanged" for c in components)
    parts = [f"{counts[s]} {s}" for s in ("new", "changed", "moved", "removed", "unchanged")
             if counts[s]]
    gone_calls = sum(1 for e in (raw_edges or []) if e.get("state") == "gone")
    text = f"{len(components)} components: {', '.join(parts) if parts else 'no state changes'}."
    if gone_calls:
        text += f" {gone_calls} call{'s' if gone_calls != 1 else ''} removed."
    if dropped:
        text += f" {dropped} component{'s' if dropped != 1 else ''} dropped to fit the diagram."
    return text


def _structure_legend(edges, explain):
    """Edge-kind key, an inline-svg line sample per kind actually drawn -- solid vs dashed is a
    shape difference as well as a colour one, so a reader who can't tell green from red still
    sees which is which. Nothing at all when `edges` carries none of the three kinds, same
    only-if-drawn rule `_symbols_legend` applies to new/gone."""
    has_call = any(e["kind"] == "call" and e.get("state") != "gone" for e in edges)
    has_gone = any(e.get("state") == "gone" for e in edges)
    has_impl = any(e["kind"] != "call" for e in edges)
    if not (has_call or has_gone or has_impl):
        return ""
    items = []
    if has_call:
        label = "call" if explain else "new call"
        items.append(
            '<span><svg><line x1="0" y1="4" x2="28" y2="4" stroke="var(--green)" '
            f'stroke-width="2"/></svg>{escape(label)}</span>'
        )
    if has_gone:
        items.append(
            '<span><svg><line x1="0" y1="4" x2="28" y2="4" stroke="var(--red)" '
            'stroke-width="2" stroke-dasharray="5 4"/></svg>call removed</span>'
        )
    if has_impl:
        items.append(
            '<span><svg><line x1="0" y1="4" x2="28" y2="4" stroke="var(--muted)" '
            'stroke-width="2" stroke-dasharray="1.5 4" stroke-linecap="round"/></svg>'
            "implements</span>"
        )
    return f'<div class="vds-legend">{"".join(items)}</div>'


def render_structure(data, explain=False):
    """The system-structure view: structure.py's own real components (classes, interfaces,
    functions the diff actually touched -- see its module docstring for why that is not
    symdelta's node state), laid out in call-depth columns and coloured by story group like the
    story map above (`walkthrough._group_color`, reused so a component's colour matches its
    stop's exactly), with call and implements/extends edges drawn over the boxes' real screen
    geometry by a small script (see the template) rather than mermaid -- dagre has no notion of
    "column by caller depth", the one layout this view exists to draw.

    `language: null` (structure.py found nothing in a supported language, or refused a
    dependency-manifest change it can't safely diff across) gets the same short
    note-instead-of-a-view treatment render_symbols gives symdelta's own null, not silence: a
    missing section reads exactly like a diff that legitimately had no components. Checked as
    `"language" in data`, not truthiness, for the same reason render_symbols checks it that way:
    a bare `{}` (no structure.json at all) must still fall through to the plain empty string
    below rather than read as a `null` structure.py actually returned.

    Explain mode drops every state signal the empty baseline would make meaningless: a removed
    component has nothing left to explain against that baseline, so it is filtered out here
    (not just unstyled) before columns are built; new/changed/moved badges and member state
    chips go with it, same reasoning `_mermaid_symbols` already applies to new/gone classing."""
    if "language" in data and data["language"] is None:
        return "\n".join([
            STRUCTURE_MARKER,
            '<div class="vd-structure">',
            f'<h2>{"How it fits together" if explain else "System change"}</h2>',
            f'<p class="note">{_html_note(_structure_failure_text(data))}</p>',
            "</div>",
        ]) + "\n"

    components = data.get("components") or []
    if not components:
        return ""

    # Deferred: walkthrough.py imports this module at load time, so a top-level import here
    # would be circular. Both modules are fully loaded by the time anything calls this function.
    from walkthrough import _group_color

    shown = [c for c in components if not (explain and c.get("state") == "removed")]
    if not shown:
        return ""
    kept_ids = {c["id"] for c in shown}

    edges = _structure_edges(data.get("edges"), kept_ids, explain)
    edges += _structure_implements_edges(data.get("implements"), shown, kept_ids)

    cols_html = "\n".join(
        f'<div class="vds-col">'
        + "".join(_structure_component_html(c, i, _group_color, explain) for c in comps)
        + "</div>"
        for i, comps in enumerate(_structure_columns(shown))
    )
    titles = {
        g["index"]: g["title"] for g in (data.get("groups") or [])
        if isinstance(g, dict) and isinstance(g.get("index"), int) and g.get("title")
    }
    chips = _structure_chips(shown, _group_color, titles)
    summary = _html_note(_structure_summary_text(
        shown, data.get("edges"), data.get("dropped") or 0, explain,
    ))
    also_touched = _structure_also_touched(data.get("also_touched") or [])
    legend = _structure_legend(edges, explain)
    # Read by the template's own edge-drawing script, keyed by the same ids the boxes above
    # carry as `id="vds-<id>"`; escape()+&quot; is the same idiom render_symbols uses for its
    # own data-ids attribute, for the same reason -- a bare escape() leaves quotes alone.
    edges_attr = escape(json.dumps(edges, sort_keys=True)).replace('"', "&quot;")

    body = "\n".join([
        f'<p class="note">{summary}</p>',
        chips,
        '<p class="vds-hint">Callers on the left, callees on the right.</p>',
        # tabindex+aria-label: a horizontally-overflowing box with no native control of its own
        # (no scrollbar-visible affordance on most platforms) is otherwise unreachable by
        # keyboard -- a mouse/touch drag is not the only way in.
        f'<div class="vds-scroll" tabindex="0" aria-label="Component diagram, scrollable">'
        # tabindex+role+aria-label here match wire()'s own zoom-control attributes for every
        # other diagram in the template (~L1577): the container itself is the full-size
        # control, box clicks below stopPropagation() before this ever sees them.
        f'<div class="vds-sys" tabindex="0" role="button" '
        f'aria-label="Expand diagram to full size" data-edges="{edges_attr}">',
        '<svg class="vds-edges" aria-hidden="true"></svg>',
        f'<div class="vds-cols">{cols_html}</div>',
        "</div></div>",
        '<p class="vds-swipe">Swipe sideways to see the whole diagram.</p>',
        also_touched,
        legend,
    ])

    heading = "How it fits together" if explain else "System change"
    return "\n".join([
        STRUCTURE_MARKER,
        '<div class="vd-structure">',
        f'<h2 class="h2-row"><span>{heading}</span><span class="ctl">'
        f'{_diagram_max_button()}</span></h2>',
    ]) + "\n" + body + "\n</div>\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", required=True, choices=("symbols", "structure"))
    parser.add_argument("--data", required=True)
    parser.add_argument("--format", required=True, choices=("html",))
    parser.add_argument("--explain", action="store_true",
                        help="the target is code as it stands, not a change: drop the new/gone "
                             "colouring and legend, which the empty baseline makes meaningless")
    parser.add_argument("--paths", nargs="*", default=[],
                        help="repo-relative files or directories the page is about; the graph "
                             "is cut down to symbols under them plus one hop out. Needed in "
                             "explain mode, where the empty baseline makes the whole repo new. "
                             "Unused for --kind structure, which is already scoped to the diff.")
    args = parser.parse_args()

    data = json.loads(Path(args.data).read_text())
    if args.kind == "structure":
        sys.stdout.write(render_structure(data, args.explain))
    else:
        sys.stdout.write(render_symbols(data, args.explain, args.paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
