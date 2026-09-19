#!/usr/bin/env python3
"""Render the walkthrough section from analysis.json and raw.diff.

  python3 walkthrough.py --analysis analysis.json --diff raw.diff --format html
  python3 walkthrough.py --analysis analysis.json --diff raw.diff --format md \
      [--hunks notes] [--links links.json] [--symdelta symdelta.json] \
      [--complexity complexity.json] [--open 3]

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
from validate_analysis import is_test_path  # noqa: E402  one owner for test-path classification
from validate_analysis import parse_hunks  # noqa: E402  one owner for diff parsing

# Same scaling rule in both formats, so the HTML page and the PR description agree about which
# file is the big one: widest file gets all ten cells, everything else is proportional to it.
BAR_CELLS = 10
DEFAULT_OPEN = 3
# The walkthrough is the residual: render.MAX_BODY_CHARS (45000) is the budget for the whole
# recap, and the prose plus the three graph sections take the rest of it. It has to be the
# part that gives, because it is the only part that scales with file count, and render.py
# warns if the total still comes out over.
DEFAULT_MAX_CHARS = 32000
# Complexity worth a chip when this change did not move it. McCabe's own "consider
# restructuring" line, and it is what keeps the chip a signal: without it, the measured 13-file
# commit put a chip on all 13, including "1 cx" on four new one-line getters. With it, three
# files carry one, and all three hold a function a reviewer should look at.
NOTEWORTHY_CX = 10
COMPLEXITY_LEGEND = ('"Branches" counts the independent paths through a function (cyclomatic '
                     'complexity); the arrow shows how this change moved it, or "removed" '
                     "when the function is gone.")

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
    """[(title, why, [paths])] in the order a reviewer should read them.

    Which files belong together, and which theme comes first, is judgment and comes from
    analysis.json. The order inside a theme is mechanical: caller depth, then size, with tests
    and generated files sinking to the bottom of their own theme rather than the bottom of the
    page, so a test still sits next to the code it covers.

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
                    sorted(paths, key=reading_key)))

    rest = [p for p in order if p not in seen]
    if rest:
        out.append(("Everything else" if out else "", "", sorted(rest, key=reading_key)))
    return out


def scaled_bars(files):
    """{path: (added cells, removed cells, empty cells)}, every bar BAR_CELLS wide and filled in
    proportion to the widest file, so a two-line tweak next to a 900-line rewrite reads as the
    small one it is. A file that changed anything keeps at least one cell, and a side with any
    lines at all keeps at least one of them, or a 1-line deletion in a big diff rounds away to a
    bar that says nothing changed."""
    widest = max((e["added"] + e["removed"] for e in files.values()), default=0)
    out = {}
    for path, entry in files.items():
        added, removed = entry["added"], entry["removed"]
        total = added + removed
        if not total or not widest:
            out[path] = (0, 0, BAR_CELLS)
            continue
        filled = max(1, round(BAR_CELLS * total / widest))
        a = round(filled * added / total)
        if added and a == 0:
            a = 1
        if removed and a == filled:
            a = filled - 1
        out[path] = (a, filled - a, BAR_CELLS - filled)
    return out


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


def _open_paths(groups, count):
    """The paths to render expanded: the first few in reading order. Size used to decide this,
    because the diff recorded nothing about importance; now that the groups carry a reading
    order, the front of that order is the better answer and the one the reader starts at."""
    flat = [path for _title, _why, paths in groups for path in paths]
    return set(flat[:max(0, count)])


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


def rename_hint(old, new):
    """Cheap "moved from" hint: trims the directory prefix `old` shares with `new`, already
    shown right beside it (the header row's hunk-was, or md's own path token), and shows only
    what's left — unlike rename_note's `{old => new}` form, which repeats the new path a
    second time for no reason once the new path is already alongside it. Falls back to the
    old basename too when it also changed, since nothing else would tell the file apart."""
    old_parts, new_parts = old.split("/"), new.split("/")
    old_dir, new_dir = old_parts[:-1], new_parts[:-1]
    i = 0
    while i < len(old_dir) and i < len(new_dir) and old_dir[i] == new_dir[i]:
        i += 1
    tail = old_dir[i:]
    if old_parts[-1] == new_parts[-1]:
        return "/".join(tail) + "/" if tail else "./"
    return "/".join(tail + old_parts[-1:])


def render_html(groups, files, by_path, open_count=DEFAULT_OPEN, links=None, complexity=None,
                 renames=None):
    is_open = _open_paths(groups, open_count)
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
    # No legend here, unlike render_md: the chip reads "load_session 2->9 branches" in words
    # and carries the long form in its title, so on the page the legend was only a paragraph
    # of vocabulary between the reader and the first file. The PR description has no titles to
    # hover, which is why the markdown still opens with it.
    out = ["<!-- visual-diff:walkthrough -->"]
    for title, why, paths in groups:
        if title:
            out.append(f'<h3 class="wt-group">{_codeify(escape(title))} '
                       f'<span class="wt-count">{len(paths)} '
                       f'file{"s" if len(paths) != 1 else ""}</span></h3>')
            if why:
                out.append(f'<p class="wt-why">{_codeify(escape(why))}</p>')
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
            out.append(f'      <span class="hunk-stat"><span class="add">+{file["added"]}'
                       f'</span> <span class="del">-{file["removed"]}</span></span>')
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
                spans = "\n".join(
                    [f'<span class="h">{escape(hunk["header"])}</span>']
                    + [f'<span class="{kind}">{escape(text)}</span>'
                       for kind, text in hunk["lines"]]
                )
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


def render_md(groups, files, by_path, hunks="notes", links=None, max_chars=DEFAULT_MAX_CHARS,
              complexity=None, renames=None):
    bars = scaled_bars(files)
    file_urls, hunk_urls = link_index(links)
    cx = complexity_index(complexity)
    renames = renames or {}
    order = [path for _title, _why, paths in groups for path in paths]
    # Same gating as render_html: the legend costs a line only when there is a chip somewhere
    # for it to explain.
    show_legend = any(complexity_chip(cx.get(p))[0] or depth_chip(cx.get(p)) for p in order)

    def block(path):
        """One file's <details>, full treatment."""
        entry, file = by_path.get(path) or {}, files[path]
        a, d, empty = bars[path]
        chip, _direction = complexity_chip(cx.get(path))
        depth = depth_chip(cx.get(path))
        old_path = renames.get(path)
        # Folded into the summary line rather than a sentence of its own: a whole extra line
        # per renamed file is what blew the size budget (see rename_hint). Arrow points left,
        # unlike the HTML row: the new path is already first here, so "→" would dangle.
        moved = f"(← `{rename_hint(old_path, path)}`) " if old_path else ""
        no_change = " (no content change)" if old_path and not file["hunks"] else ""
        out = ["<details>",
               f'<summary>`{path}` +{file["added"]} -{file["removed"]} '
               + (f"`{chip}` " if chip else "")
               + (f"`{depth}` " if depth else "")
               + moved
               + f'{"█" * a}{"▒" * d}{"░" * empty}{no_change}</summary>',
               # GitHub renders a fence inside <details> as literal backticks unless a blank
               # line follows </summary>. Verified both ways against its /markdown endpoint.
               ""]
        # The link goes here rather than in the summary above: a link inside a summary fights
        # the click that opens the block.
        role = entry.get("role") or ""
        if role or file_urls.get(path):
            tail = f" [view in PR]({file_urls[path]})" if file_urls.get(path) else ""
            out += [role + tail, ""]
        for hunk, note in zip(file["hunks"], _notes_for(entry, file["hunks"])):
            url = hunk_urls.get((path, hunk["prefix"]))
            label = hunk_label(hunk["prefix"]) if url else ""
            if note or label:
                out.append(note + (f" [{label}]({url})" if label else ""))
            if hunks == "full":
                out += ["```diff", hunk["header"], *(t for _kind, t in hunk["lines"]), "```"]
            else:
                out.append(f"`{hunk['prefix']}`")
            out.append("")
        if not file["hunks"] and not old_path:
            out += ["No textual diff: a binary, mode or rename-only change.", ""]
        return out + ["</details>", ""]

    def tail_line(path):
        """A file demoted to the collapsed tail: path, counts and role, so nothing is silently
        dropped and the coverage check still passes.

        No link. A blob url on this repo runs 110 characters, so linking 76 tail files spends
        8400 on urls while the budget is busy truncating the content they point at. These are
        the files the ranking already called least interesting; Files changed has them."""
        entry, file = by_path.get(path) or {}, files[path]
        role = entry.get("role") or ""
        old_path = renames.get(path)
        # Left-pointing arrow, same reason as block()'s moved line: the new path is first here too.
        moved = f" (← `{rename_hint(old_path, path)}`)" if old_path else ""
        return (f'- `{path}` +{file["added"]} -{file["removed"]}' + moved
                + (f" - {role}" if role else ""))

    def assemble(keep):
        out = ["<!-- visual-diff:walkthrough -->", "<details>",
               "<summary>Walkthrough</summary>", ""]
        if show_legend:
            out += [COMPLEXITY_LEGEND, ""]
        tail = []
        for title, why, paths in groups:
            # Bold rather than a heading: this sits inside a <details>, where GitHub's own
            # anchor-generating headings would collide with the PR description's outline.
            if title and any(path in keep for path in paths):
                out += [f"**{title}**" + (f" - {why}" if why else ""), ""]
            for path in paths:
                if path in keep:
                    out += block(path)
                else:
                    tail.append(tail_line(path))
        if tail:
            out += ["<details>",
                    f"<summary>{len(tail)} more files, path and role only</summary>", "",
                    *tail, "", "</details>", ""]
        if hunks != "full":
            out += ["Hunk bodies are in the PR's own Files changed tab, and in full in the "
                    "local HTML review page.", ""]
        return "\n".join(out + ["</details>"]) + "\n"

    # The size of the walkthrough with every file demoted to one line: the true floor no
    # --max-chars retry can shrink past. Computed unconditionally, not just when this run had to
    # demote, so render.py can tell a satisfiable --max-chars suggestion from one that isn't,
    # even on a run that happened to fit.
    floor = len(assemble(set()))
    floor_line = f"<!-- visual-diff:walkthrough-floor {floor} -->"
    overhead = len(floor_line) + 1  # its own line, so it counts against max_chars too

    def with_floor(text):
        """Floor goes on the line right after the section marker, so render.py can read it back
        without re-running the demotion this function already did."""
        marker, rest = text.split("\n", 1)
        return f"{marker}\n{floor_line}\n{rest}"

    text = assemble(set(order))
    if not max_chars or len(text) + overhead <= max_chars:
        return with_floor(text)

    # Over budget. Keep the full treatment for the files a reviewer actually came for and demote
    # the rest to one line each, rather than truncating a hunk or dropping a file. Costed per
    # file up front so this stays one pass: re-assembling to test each candidate is quadratic,
    # and a diff big enough to land here is exactly the one with the most files to test.
    cost = {p: sum(len(line) + 1 for line in block(p)) - (len(tail_line(p)) + 1) for p in order}
    ranked = sorted(order, key=lambda p: (interest_rank(p),
                                          -(files[p]["added"] + files[p]["removed"]), p))
    room, keep = max_chars - floor - overhead, set()
    for path in ranked:
        if cost[path] > room:
            break
        room -= cost[path]
        keep.add(path)
    return with_floor(assemble(keep))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--diff", required=True)
    parser.add_argument("--format", required=True, choices=("md", "html"))
    # A PR description is a high-level artifact and GitHub renders the real diff directly below
    # it, so repeating the hunk bodies there is duplication that costs the whole size budget:
    # measured on a 76-file branch, bodies came to 440845 characters against notes' 14842. The
    # HTML page has no budget and carries every body.
    parser.add_argument("--hunks", default="notes", choices=("full", "notes"),
                        help="md only: 'full' adds the hunk bodies, well past the size budget")
    parser.add_argument("--links", help="links.json, to link each file and hunk into the PR")
    parser.add_argument("--symdelta", help="symdelta.json, to order each group caller-first")
    parser.add_argument("--complexity", help="complexity.json, for the per-file cx deltas")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                        help="md only: demote the least interesting files to one line each "
                             "until the section fits; 0 disables")
    parser.add_argument("--open", type=int, default=DEFAULT_OPEN,
                        help="html only: how many of the first files in reading order render "
                             "expanded")
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
    groups = story(order, files, analysis.get("groups"), optional(args.symdelta))
    complexity = optional(args.complexity)
    if args.format == "html":
        sys.stdout.write(render_html(groups, files, by_path, args.open, links, complexity,
                                      renames))
    else:
        text = render_md(groups, files, by_path, args.hunks, links, args.max_chars, complexity,
                          renames)
        if args.max_chars and len(text) > args.max_chars:
            # Every file demoted to one line and still over. Say so rather than drop a file:
            # GitHub refuses a body over 65536 characters, so the caller needs to know the
            # walkthrough alone leaves no room for the prose and graphs above it.
            print(f"warning: {len(order)} files will not fit in {args.max_chars} characters, "
                  f"the smallest walkthrough for this diff is {len(text)}", file=sys.stderr)
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
