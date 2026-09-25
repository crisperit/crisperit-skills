#!/usr/bin/env python3
"""Render the walkthrough section from analysis.json and raw.diff.

  python3 walkthrough.py --analysis analysis.json --diff raw.diff --format html \
      [--links links.json] [--symdelta symdelta.json] [--complexity complexity.json] [--open 3]

Everything in a walkthrough except the prose is mechanical: the paths, the line counts, the
bars, the `<details>` nesting, the fences, the escaping and the diff bodies all follow from
raw.diff. Judgment is `role`, each hunk's `note`, and which files form a group and in what
order the groups read; all of those come from analysis.json. The order inside a group is
mechanical again: caller-first from symdelta.json, tests and generated files last.

Writing the mechanical part by hand is how a file goes missing from the inventory, how a
fenced block loses the blank line after `</summary>` that GitHub needs to parse it, and how
two diff lines end up merged into one span so every line comment below lands on the wrong
line of a real pull request. The page's comment code reads this markup as a contract, so it
is generated here once and pasted byte for byte, the same rule the graph sections already
follow.

Stdlib only, no network.
"""

import argparse
import json
import sys
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from complexity import NOTEWORTHY_DEPTH  # noqa: E402  one owner for "how deep is too deep"
from fanout import rename_map  # noqa: E402  one owner for parsing "rename from/to" headers
from links import line_range  # noqa: E402  one owner for a hunk's new-side line span
from sections import _codeify  # noqa: E402  one owner for backtick-to-<code> conversion
from sections import _diagram_max_button  # noqa: E402  one owner for the diagram header icon
from sections import _wrap_flow_labels  # noqa: E402  one owner for mermaid long-label wrapping
from sections import render_symbols  # noqa: E402  one owner for the symbol-delta diagram
from validate_analysis import is_test_path  # noqa: E402  one owner for test-path classification
from validate_analysis import parse_hunks  # noqa: E402  one owner for diff parsing

DEFAULT_OPEN = 3
# A file whose diff is smaller than this many changed lines expands by default regardless of
# its position in the reading order -- a one-line fix buried in group 4 gains nothing from
# staying collapsed just because it isn't among the first few files.
SMALL_DIFF_LINES = 80
# The story map's fixed palette, --g1..--g6 in the template, cycling by group index so a
# seventh group repeats the first colour rather than needing a seventh variable.
GROUP_PALETTE_SIZE = 6
# Complexity worth a chip when this change did not move it. McCabe's own "consider
# restructuring" line, and it is what keeps the chip a signal: without it, the measured 13-file
# commit put a chip on all 13, including "1 cx" on four new one-line getters. With it, three
# files carry one, and all three hold a function a reviewer should look at.
NOTEWORTHY_CX = 10

_LOCKFILES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock",
    "Cargo.lock", "composer.lock", "Gemfile.lock", "go.sum", "requirements.txt",
})
_GENERATED_SEGMENTS = frozenset({"vendor", "node_modules", "generated", "__generated__", "dist"})


def interest_rank(path):
    """0 for code a reviewer came to read, 2 for what they never do: a lockfile refresh and a
    `.gitkeep` are the natural tail, test files the one before it. Sinks those files within
    their own group in the reading order, and decides which files keep their per-hunk notes
    when the whole section will not fit."""
    name = path.rsplit("/", 1)[-1]
    segments = set(path.split("/")[:-1])
    if name in _LOCKFILES or segments & _GENERATED_SEGMENTS or name == ".gitkeep":
        return 2
    return 1 if is_test_path(path) else 0


def caller_depth(symdelta, paths):
    """{path: depth}, 0 for a file nothing else in the diff calls into.

    Kahn over symdelta's caller -> callee symbol edges, lifted to the file each symbol lives in
    (a `pkg` node carries no `file` and drops out, same as an edge whose other end isn't in this
    diff) so the reviewer meets an entry point before the thing it calls. A cycle breaks at the
    least-called node, so the walk terminates and lands on the same order every run.

    A file absent from the result sorts last: no symdelta.json, `language: null` (an
    unsupported language, or the extractor tool missing), and a diff with no edge landing
    inside it all take this same path, degrading to the pre-coupling ordering rather than
    falling back to import parsing -- a real coverage loss versus that, accepted deliberately.
    """
    file_of = {node.get("id"): node.get("file") for node in (symdelta or {}).get("nodes") or []
               if isinstance(node, dict)}
    touched_paths = set(paths)
    edges = []
    for edge in (symdelta or {}).get("edges") or []:
        if not isinstance(edge, dict):
            continue
        src, dst = file_of.get(edge.get("source")), file_of.get(edge.get("target"))
        if src and dst and src != dst and src in touched_paths and dst in touched_paths:
            edges.append((src, dst))

    touched = {node for edge in edges for node in edge}
    outgoing = {unit: set() for unit in touched}
    indegree = dict.fromkeys(touched, 0)
    for src, dst in edges:
        if dst not in outgoing[src]:
            outgoing[src].add(dst)
            indegree[dst] += 1

    depth, remaining, level = {}, set(touched), 0
    while remaining:
        ready = sorted(unit for unit in remaining if indegree[unit] == 0)
        if not ready:
            # A call cycle. Break it at the least-called node, so the walk terminates and
            # lands on the same order every run.
            ready = [min(remaining, key=lambda unit: (indegree[unit], unit))]
        for unit in ready:
            depth[unit] = level
            remaining.discard(unit)
            for successor in outgoing[unit]:
                if successor in remaining:
                    indegree[successor] -= 1
        level += 1
    return depth


def story(order, files, groups, symdelta):
    """[(title, why, [paths], flow_mermaid, hop, side)] in the order a reviewer should read
    them -- the same order render_story() draws as the page's story map, so a group's index
    here is its stop number there.

    Which files belong together, and which theme comes first, is judgment and comes from
    analysis.json. The order inside a theme is mechanical: caller depth, then size, with tests
    and generated files sinking to the bottom of their own theme rather than the bottom of the
    page, so a test still sits next to the code it covers. `flow_mermaid` is the group's own
    optional small diagram, carried through opaquely; a synthetic catch-all group never has one.
    `hop` (the hand-off to the next stop) and `side` (true for a supporting group off the main
    line) are carried through the same way, defaulting to "" and False.

    A file the groups missed is swept into a trailing catch-all rather than dropped: a stale
    analysis.json must not be able to hide a file from the reader.
    """
    depth = caller_depth(symdelta or {}, order)
    unplaced = max(depth.values(), default=0) + 1

    def reading_key(path):
        return (interest_rank(path), depth.get(path, unplaced),
                -(files[path]["added"] + files[path]["removed"]), path)

    seen, out = set(), []
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        paths = [p for p in dict.fromkeys(group.get("paths") or [])
                 if p in files and p not in seen]
        if not paths:
            continue
        seen.update(paths)
        out.append((group.get("title") or "", group.get("why") or "",
                    sorted(paths, key=reading_key), group.get("flow_mermaid") or "",
                    group.get("hop") or "", bool(group.get("side"))))

    rest = [p for p in order if p not in seen]
    if rest:
        out.append(("Everything else" if out else "", "", sorted(rest, key=reading_key), "",
                    "", False))
    return out


def render_story(groups):
    """The story map: one stop per group from `story()`, in story order, plain HTML/CSS with
    no JS of its own -- a vertical spine on a phone, horizontal from the template's own
    900px breakpoint (see the CSS). A main-line stop links to its group heading at
    `#wt-group-N` and carries `id="story-N"`, the landing spot that heading's own "back to the
    story" link (render_html, `has_story`) points at. A side group renders the same stop
    markup but sits below the line instead of in the chain, with no hop before or after it.

    "" when there is nothing to draw: a single group has no story to map, so render.py falls
    back to the old top-level flow diagram instead (see render.py's render_html).
    """
    if len(groups) < 2:
        return ""

    def stop(gi, title, why, paths, hop, side):
        n = gi + 1
        count = f'{len(paths)} file{"s" if len(paths) != 1 else ""}'
        meta = f"{count} · {_codeify(escape(why))}" if why else count
        html = (f'<a class="stop" id="story-{n}" href="#wt-group-{n}" style="--c:'
                f'{_group_color(gi)}"><span class="n">{n}</span>'
                f'<span class="t">{_codeify(escape(title))}</span>'
                f'<span class="m">{meta}</span></a>')
        return html, hop, side

    stops = [stop(gi, title, why, paths, hop, side)
             for gi, (title, why, paths, _flow, hop, side) in enumerate(groups)]

    main = [html for html, _hop, side in stops if not side]
    main_hops = [hop for _html, hop, side in stops if not side]
    spine = []
    for i, html in enumerate(main):
        spine.append(html)
        if i < len(main) - 1 and main_hops[i]:
            spine.append(f'<div class="hop">{_codeify(escape(main_hops[i]))}</div>')

    side_stops = [html for html, _hop, side in stops if side]

    out = ['<div class="map">', '<div class="spine">'] + spine + ['</div>']
    if side_stops:
        out.append('<div class="side"><span class="lbl">Supports the story</span>')
        out.extend(side_stops)
        out.append('</div>')
    out.append('</div>')
    return "\n".join(out) + "\n"


def link_index(links):
    """({path: file url}, {(path, hunk prefix): hunk url}) from links.json, empty when those
    urls would 404. Keyed by hunk prefix rather than by position: links.py walks the same diff
    through the same parser, so the prefixes line up and a mismatch means something is stale."""
    if not links or not links.get("head_pushed"):
        return {}, {}
    files, hunks = {}, {}
    for entry in links.get("files") or []:
        path = entry.get("path")
        files[path] = entry.get("diff_url") or entry.get("blob_url") or ""
        for hunk in entry.get("hunks") or []:
            if hunk.get("url"):
                hunks[(path, hunk.get("header"))] = hunk["url"]
    return files, hunks


def hunk_label(header):
    span = line_range(header)
    if not span:
        return ""
    start, end = span
    return f"L{start}" + (f"-L{end}" if end > start else "")


def _notes_for(entry, hunks):
    """One note per real hunk, positioned by index. analysis.json is written against the same
    diff, but a stale or hand-edited one can be short or long; pairing by index and padding
    keeps every real hunk rendered rather than silently dropping the tail of the file."""
    notes = [h.get("note", "") if isinstance(h, dict) else "" for h in entry.get("hunks") or []]
    return notes + [""] * (len(hunks) - len(notes))


def _open_paths(groups, count, files):
    """The paths to render expanded: the first few in reading order, plus every file whose own
    diff is small (see SMALL_DIFF_LINES) regardless of where it falls in that order."""
    flat = [path for _title, _why, paths, _flow, _hop, _side in groups for path in paths]
    is_open = set(flat[:max(0, count)])
    is_open.update(path for path in flat
                   if files[path]["added"] + files[path]["removed"] < SMALL_DIFF_LINES)
    return is_open


def complexity_index(complexity):
    """{path: file entry} from complexity.json, empty when it was not produced. Absent is
    normal: the measurement is best-effort per language and the walkthrough renders without
    it rather than blocking on it."""
    return (complexity or {}).get("files") or {}


def complexity_chip(entry):
    """(text, direction) naming a function and spelling out its complexity in words, or
    ("", "") when this diff earned no chip.

    `jump` -- a pre-existing function this diff actually moved -- wins whenever it is set: it
    names and quantifies the one thing worth seeing move, e.g. "checkAuth 3→4 branches". When
    that function is gone at head (deleted, not merely renamed -- complexity.py resolves a
    rename to delta 0 upstream), it renders as "checkAuth removed" with no arrow: a branch
    count of 0 is not an improvement, it is the function no longer existing, and "3→0" said
    the opposite. A function always has cc >= 1 while it exists, so `after == 0` on an
    `existed` jump means gone even without a `removed` key. Failing all that, a brand-new
    function is worth a beat only if it was born hairy, e.g.
    "resolveSource 11 branches" (it has no `jump` because it never had a before). A function
    that pre-existed and this diff left alone is not news no matter how high it sits: showing
    it anyway was the original bug -- main.go's `serve`, 31 before and 31 after, rendered as a
    bare "31 cx".
    """
    jump = (entry or {}).get("jump")
    if jump:
        if jump.get("removed") or (jump["after"] == 0 and jump["existed"]):
            return f"{jump['name']} removed", "gone"
        arrow = f"{jump['before']}→{jump['after']}"
        direction = "up" if jump["delta"] > 0 else "down"
        return f"{jump['name']} {arrow} branches", direction
    peak = (entry or {}).get("peak")
    if peak and not peak["existed"] and peak["after"] >= NOTEWORTHY_CX:
        return f"{peak['name']} {peak['after']} branches", "flat"
    return "", ""


def depth_chip(entry):
    """"nested N deep" for the file's worst touched function, or "" below NOTEWORTHY_DEPTH or
    when that function is gone at head (nothing left to be nested in).

    Independent of complexity_chip: a function can sit at a modest branch count and still bury
    a reviewer four levels deep, and the reverse holds too, so this is its own small chip
    rather than a clause tacked onto the other one.
    """
    peak = (entry or {}).get("peak") or {}
    if peak.get("removed"):
        return ""
    depth = peak.get("depth", 0)
    return f"nested {depth} deep" if depth >= NOTEWORTHY_DEPTH else ""


def rename_index(diff_text):
    """{new path: old path} for a renamed file, the inverse of fanout.rename_map's old->new so
    a lookup by the diff's own new-side path (what everything else here is keyed on) is direct."""
    return {new: old for old, new in rename_map(diff_text).items()}


def rename_note(old, new):
    """Compact "moved" phrasing for a rename: git's own `{old => new}` diff-header shape when
    the two paths share a directory, so a long rename fits on a phone instead of repeating the
    whole path twice. Falls back to the full "old => new" when they share nothing."""
    old_parts, new_parts = old.split("/"), new.split("/")
    keep = min(len(old_parts), len(new_parts)) - 1  # leave at least one differing segment each
    i = 0
    while i < keep and old_parts[i] == new_parts[i]:
        i += 1
    j = 0
    while j < keep - i and old_parts[-1 - j] == new_parts[-1 - j]:
        j += 1
    if not i and not j:
        return f"{old} => {new}"
    prefix = "/".join(old_parts[:i])
    middle = f"{{{'/'.join(old_parts[i:len(old_parts) - j])} => " \
             f"{'/'.join(new_parts[i:len(new_parts) - j])}}}"
    suffix = "/".join(old_parts[len(old_parts) - j:])
    return "/".join(part for part in (prefix, middle, suffix) if part)


def _group_color(index):
    """The palette variable for a group at this 0-based index, cycling through --g1..--g6.
    Shared by render_html's group heading and render_story's stop so the two colour the same
    group identically -- the whole point of the two-way nav between them."""
    return f"var(--g{(index % GROUP_PALETTE_SIZE) + 1})"


def _flow_box(flow_mermaid):
    """The group flow diagram, in the same `.panel.svgbox` + `pre.mermaid` shape render.py
    uses for the top-level FLOW, so panZoom() and the CSS fit it to its own viewBox instead of
    stretching it like the bigger stacked diagrams below. No LR-to-TB rewrite here, unlike
    FLOW: a small group diagram needs no phone-width rescue, and rewriting a sequenceDiagram's
    leading token would just break it."""
    escaped_flow = escape(_wrap_flow_labels(flow_mermaid))
    return ('<div class="panel svgbox svgbox-flow" tabindex="0" role="button"'
            ' aria-label="Expand diagram to full size">'
            f'<pre class="mermaid">{escaped_flow}</pre></div>')


def _group_panel(index, flow_mermaid, graph_html):
    """One panel per group, directly under its `why` line: a tab bar switching between the
    flow diagram and the call graph when the group has both, so the two read as views of one
    thing instead of two unrelated affordances -- and no tab bar at all, just the one
    diagram, when the group has only one of them, since a tab bar holding a single tab is
    noise. `graph_html` is `render_symbols(..., inline=True)`'s own `.vd-symbols` block,
    unopened here: its packages/symbols level toggle rides inside the call-graph tabpanel and
    the level script moves it up into `.wt-tabs` once, rather than this function tearing that
    block apart to relocate it itself.

    The graph tabpanel starts `hidden` -- plain markup for a page with no JS -- and the level
    script replaces that with the off-screen (never `display:none`) treatment once it wires
    the tabs, the same reason the inline symbols block already avoids `display:none`: mermaid
    measures a hidden container's labels as zero and collapses the diagram permanently.

    The maximise icon sits in `.wt-tabs-row`, wrapping the tab strip rather than inside it:
    `.vd-ctl` (packages/symbols toggle) gets appended into `.wt-tabs` itself at runtime (see
    the level script) and pushes itself right with its own margin, which would otherwise land
    on the same spot as this icon depending on DOM order. The no-tabs case below has no tab
    strip for the icon to sit beside; render_html puts it in the group's own `<h3>` instead."""
    if flow_mermaid and graph_html:
        flow_id, graph_id = f"wt-tab-flow-{index}", f"wt-tab-graph-{index}"
        flow_panel_id, graph_panel_id = f"wt-tabpanel-flow-{index}", f"wt-tabpanel-graph-{index}"
        return "\n".join([
            '<div class="wt-panel">',
            '<div class="wt-tabs-row">',
            '<div class="wt-tabs" role="tablist" aria-label="diagram views">',
            f'<button type="button" class="wt-tab" role="tab" id="{flow_id}" '
            f'data-tab="flow" aria-selected="true" aria-controls="{flow_panel_id}">flow</button>',
            f'<button type="button" class="wt-tab" role="tab" id="{graph_id}" '
            f'data-tab="graph" aria-selected="false" tabindex="-1" '
            f'aria-controls="{graph_panel_id}">call graph</button>',
            '</div>',
            _diagram_max_button(),
            '</div>',
            f'<div class="wt-tabpanel" role="tabpanel" id="{flow_panel_id}" '
            f'aria-labelledby="{flow_id}">{_flow_box(flow_mermaid)}</div>',
            f'<div class="wt-tabpanel" role="tabpanel" id="{graph_panel_id}" '
            f'aria-labelledby="{graph_id}" hidden>{graph_html}</div>',
            '</div>',
        ])
    return f'<div class="wt-panel">{_flow_box(flow_mermaid) if flow_mermaid else graph_html}</div>'


def render_html(groups, files, by_path, open_count=DEFAULT_OPEN, links=None, complexity=None,
                 renames=None, explain=False, symdelta=None):
    is_open = _open_paths(groups, open_count, files)
    file_urls, hunk_urls = link_index(links)
    cx = complexity_index(complexity)
    renames = renames or {}

    def anchor(url, text):
        # The page is opened from file:// and a link that replaces it costs the reader the
        # notes they marked up, so every link leaves the page alone.
        return (f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">'
                f"{escape(text)}</a>")

    # First line is the marker validate_analysis.py --sections looks for, same convention as
    # sections.py, so a walkthrough that never reached the page is caught rather than shipped.
    # No legend here: the chip reads "load_session 2->9 branches" in words and carries the
    # long form in its title, so a legend here would only add a paragraph of vocabulary
    # between the reader and the first file.
    # A story map only exists once there is more than one stop to draw (see render_story), so
    # a lone group keeps its plain heading -- no id, colour or back-link to a map that isn't
    # on the page.
    has_story = len(groups) > 1
    out = ["<!-- code-walkthrough:walkthrough -->"]
    for gi, (title, why, paths, flow_mermaid, _hop, _side) in enumerate(groups):
        flow = flow_mermaid.strip()
        graph = ""
        if symdelta:
            rendered = render_symbols(symdelta, explain, paths=paths, inline=True)
            graph = rendered.rstrip("\n") if rendered else ""
        if title:
            n = gi + 1
            if has_story:
                heading_attrs = f' id="wt-group-{n}" style="--c:{_group_color(gi)}"'
                badge = (f'<a class="wt-badge-link" href="#story-{n}" '
                          f'aria-label="Back to stop {n} in the story" '
                          f'title="Back to the story"><span class="wt-badge">{n}</span></a> ')
            else:
                heading_attrs, badge = "", ""
            # A tabbed panel carries its own maximise icon beside the tab strip (see
            # _group_panel); a single diagram has no tab strip to sit beside, so its icon
            # lands here instead -- a direct child of this flex heading, not wrapped in a
            # `.ctl` span, since .wt-group's own margin:auto rule pushes the icon itself
            # right and only works on a direct flex item -- and a group with neither
            # diagram gets no icon at all.
            max_btn = "" if (flow and graph) or not (flow or graph) else _diagram_max_button()
            out.append(f'<h3 class="wt-group"{heading_attrs}>{badge}'
                       f'{_codeify(escape(title))} '
                       f'<span class="wt-count">{len(paths)} '
                       f'file{"s" if len(paths) != 1 else ""}</span> '
                       # Filled and kept live client-side (updateGroupViewed in the
                       # template): viewed state lives in the reader's own localStorage.
                       f'<span class="wt-viewed"></span>{max_btn}</h3>')
            if why:
                out.append(f'<p class="wt-why">{_codeify(escape(why))}</p>')
        if flow or graph:
            out.append(_group_panel(gi, flow, graph))
        for path in paths:
            entry = by_path.get(path) or {}
            file = files[path]
            chip, direction = complexity_chip(cx.get(path))
            depth = depth_chip(cx.get(path))
            old_path = renames.get(path)
            out.append(f'<details class="hunk"{" open" if path in is_open else ""}>')
            out.append("  <summary>")
            # hunk-lines is the summary's one column-2 grid item (see the template): CSS alone
            # stacks the path+stat row above the optional "moved from" row beneath it.
            out.append('    <span class="hunk-lines">')
            # The name itself is the link to the file on the PR, with no separate glyph beside
            # it. The anchor goes inside .hunk-path, not next to it: the page's comment JS
            # reads .hunk-path's textContent as the literal PR-comment path, and textContent
            # spans descendants, so wrapping the text keeps that pure. A click on it must not
            # also toggle the block, which the template's summary-link handler takes care of.
            name = escape(path)
            if file_urls.get(path):
                name = (f'<a href="{escape(file_urls[path])}" target="_blank"'
                        f' rel="noopener noreferrer" title="Open in PR">{name}</a>')
            out.append(f'      <span class="hunk-path">{name}</span>')
            # Filled in client-side (renderNotes) once notes exist for this path; empty here
            # so :empty hides it and a hunk with no comments shows nothing.
            out.append('      <span class="hunk-count"></span>')
            if explain:
                # Every line is an addition against the empty baseline, so "+400 -0" is a fact
                # about the trick, not about the file.
                out.append(f'      <span class="hunk-stat">{file["added"]} '
                           f'line{"" if file["added"] == 1 else "s"}</span>')
            else:
                out.append(f'      <span class="hunk-stat"><span class="add">+{file["added"]}'
                           f'</span> <span class="del">-{file["removed"]}</span></span>')
            # GitHub-style Viewed checkbox: wireViewedCheckboxes reads the path from this
            # hunk's own .hunk-path rather than a duplicate attribute here; checking it
            # collapses the file and persists to localStorage the same way comments do.
            # Emitted before .hunk-was so Viewed always lands on the header row right after
            # .hunk-stat: .hunk-was's flex-basis:100% forces a new row, and anything emitted
            # after it would wrap onto a third line instead.
            out.append('      <label class="hunk-viewed" title="Mark this file as viewed">'
                       '<input type="checkbox" class="hunk-viewed-cb"> Viewed</label>')
            if old_path:
                # Sibling of .hunk-path, never inside it: the page's comment JS reads
                # .hunk-path's textContent as the literal PR-comment path, which must stay pure.
                out.append(f'      <span class="hunk-was">moved from {escape(old_path)}'
                           f'</span>')
            out.append("    </span>")
            out.append("  </summary>")
            # Complexity chips are furniture, not identity, so they get their own quiet row
            # below the summary instead of crowding the path, which already needs all the room
            # it can get to wrap on a phone. Built as a list, not appended straight out: the
            # row has a top padding, and a diff with no complexity data at all would otherwise
            # leave that gap behind an empty div.
            meta = []
            if chip:
                meta.append(f'    <span class="hunk-cx {direction}" title="worst function this '
                            f'change touched: how far it moved, or that it\'s gone">'
                            f"{escape(chip)}</span>")
            if depth:
                meta.append(f'    <span class="hunk-cx" title="deepest nesting in that function">'
                            f"{escape(depth)}</span>")
            if meta:
                out.append('  <div class="hunk-meta">')
                out.extend(meta)
                out.append("  </div>")
            role = entry.get("role") or ""
            if role:
                out.append(f'  <p class="hunk-note">{_codeify(escape(role))}</p>')
            notes = _notes_for(entry, file["hunks"])
            for hunk, note in zip(file["hunks"], notes):
                url = hunk_urls.get((path, hunk["prefix"]))
                label = hunk_label(hunk["prefix"]) if url else ""
                # No note, no paragraph. The line-range link is not worth a line of its own:
                # it says the same thing as the @@ header right below it, and with most hunks
                # carrying no note it left a column of bare L20-L26 links down the page.
                if note:
                    tail = f" {anchor(url, label)}" if label else ""
                    out.append(f'  <p class="hunk-note hunk-note-hunk">'
                               f'{_codeify(escape(note))}{tail}</p>')
                # One span per line with no newline before the first or after the last, or the
                # <pre> renders a blank row at each end of every hunk.
                #
                # Explain mode reads as plain code: the leading +/-/space marker is dropped and
                # every line is class "c", which is what the page's wireHunk() numbers off the
                # header's new-side start -- so the badges are the file's own line numbers. The
                # header span itself stays in the DOM, hidden, because wireHunk() parses it.
                if explain:
                    head = f'<span class="h" hidden>{escape(hunk["header"])}</span>'
                    body = [f'<span class="c">{escape(text[1:])}</span>'
                            for _kind, text in hunk["lines"]]
                    # No newline after the hidden header: the separator is a text node inside
                    # the <pre>, so hiding the span alone would leave a blank first row.
                    spans = head + "\n".join(body)
                else:
                    head = f'<span class="h">{escape(hunk["header"])}</span>'
                    body = [f'<span class="{kind}">{escape(text)}</span>'
                            for kind, text in hunk["lines"]]
                    spans = "\n".join([head] + body)
                out.append(f'  <pre class="diff">{spans}</pre>')
            if not file["hunks"]:
                if old_path:
                    # The move itself is already in the header's hunk-was; this only needs to
                    # tell the reader they can skip the file.
                    out.append('  <p class="hunk-note">Renamed with no content change.</p>')
                else:
                    out.append('  <p class="hunk-note">No textual diff: a binary, mode or '
                               "rename-only change.</p>")
            out.append("</details>")
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--diff", required=True)
    parser.add_argument("--format", required=True, choices=("html",))
    parser.add_argument("--links", help="links.json, to link each file and hunk into the PR")
    parser.add_argument("--symdelta", help="symdelta.json, to order each group caller-first")
    parser.add_argument("--complexity", help="complexity.json, for the per-file cx deltas")
    parser.add_argument("--open", type=int, default=DEFAULT_OPEN,
                        help="how many of the first files in reading order render expanded")
    parser.add_argument("--explain", action="store_true",
                        help="the target is code as it stands, not a change: render plain code "
                             "with the file's own line numbers instead of diff rows")
    args = parser.parse_args()

    analysis = json.loads(Path(args.analysis).read_text())
    diff_text = Path(args.diff).read_text(errors="replace")
    order, files = parse_hunks(diff_text)
    by_path = {e.get("path"): e for e in analysis.get("files") or [] if isinstance(e, dict)}
    renames = rename_index(diff_text)

    def optional(path):
        # Both inputs are best-effort upstream: symdelta can bail out on an unsupported
        # language and complexity is not produced for every run. Reading them as absent keeps
        # the walkthrough renderable rather than failing the whole recap over a missing extra.
        return json.loads(Path(path).read_text()) if path else None

    links = optional(args.links)
    symdelta = optional(args.symdelta)
    groups = story(order, files, analysis.get("groups"), symdelta)
    complexity = optional(args.complexity)
    sys.stdout.write(render_html(groups, files, by_path, args.open, links, complexity, renames,
                                 args.explain, symdelta))
    return 0


if __name__ == "__main__":
    sys.exit(main())
