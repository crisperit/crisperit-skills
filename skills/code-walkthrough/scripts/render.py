#!/usr/bin/env python3
"""Assemble the finished HTML page from analysis.json and the section files.

  python3 render.py --analysis analysis.json --diff raw.diff --format html \
      --template ../assets/diff-review-template.html \
      --walkthrough section-walkthrough.html \
      [--links links.json] [--title "..."] > out.html

The page body was the last thing a model still typed out, and almost none of it was judgment.
Six of its seven sections are either a section file pasted byte for byte or a string that
analysis.json already carries; the facts strip is arithmetic and the footer is a timestamp. So
the renderer subagents are gone and this walks the same data instead.

What that removes, beyond the tokens: the two escaping rules in references/builder.md were
instructions a model had to remember every time, and getting one wrong meant either a live
`<img onerror=...>` from a branch name in someone else's PR, or a diagram replaced by a raw
mermaid error box. They are now one function with a test.

The judgment stays in analysis.json: `overview`, `verdict`, `groups` and the per-file `role` and per-hunk `note` that walkthrough.py
places.

Prose fields mark identifiers with backticks; HTML promotes them to `<code>` after escaping,
so a tag is only ever added to text that is already safe. Same rule sections.py already
follows for its captions.

Stdlib only, no network.
"""

import argparse
import json
import re
import sys
from datetime import datetime
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from complexity import summary_line  # noqa: E402  one owner for wording a complexity delta
from sections import _MAX_BUTTON_HTML, _codeify, render_symbols  # noqa: E402  one owner for these
import visuals as visuals_mod  # noqa: E402  the sanitiser, the single trust boundary for pictures
from validate_analysis import parse_hunks  # noqa: E402  one owner for diff parsing
from walkthrough import story  # noqa: E402  one owner for the group order

TITLE_PLACEHOLDER = "<!-- DIFF_TITLE -->"
CONTENT_PLACEHOLDER = "<!-- DIFF_CONTENT -->"
STATE_PLACEHOLDER = "<!-- HR_STATE -->"
PICTURE_MARKER = re.compile(r"<!-- cw-picture:(\d+) -->")


def counts_from(files):
    """(file count, added, removed, net) for the facts strip. Taken from the diff rather than
    from `git diff --numstat`, because numstat writes `-` for a binary file and the walkthrough
    already proved this count matches numstat exactly on a real 76-file branch."""
    added = sum(entry["added"] for entry in files.values())
    removed = sum(entry["removed"] for entry in files.values())
    return len(files), added, removed, added - removed


def _paragraphs(text):
    """Prose split on blank lines. A model writing two paragraphs into one JSON string is the
    normal case, and joining them into one <p> loses the break it meant.

    Merges adjacent all-bullet blocks into one: a model putting a blank line between two
    bullets (instead of between the lead and the bullet block) is one list, not a run of
    one-item lists. A block that opens with a non-bullet line is never merged into this run,
    since _block_html already splits its own lead back out into a <p>."""
    blocks = [block.strip() for block in (text or "").split("\n\n") if block.strip()]
    merged = []
    for block in blocks:
        lines = [line for line in block.splitlines() if line.strip()]
        all_bulleted = bool(lines) and all(line.strip().startswith("- ") for line in lines)
        if all_bulleted and merged and merged[-1][1]:
            merged[-1] = (merged[-1][0] + "\n" + block, True)
        else:
            merged.append((block, all_bulleted))
    return [block for block, _ in merged]


def _block_html(block):
    """Render one blank-line-delimited block as a <p>, a <ul>, or both. A block becomes a list
    from its first "- " line onward; anything before that first bullet is the lead and stays a
    <p>, since a model that skips the blank line between the lead and the bullets still meant a
    lead, not one more list item. A non-bullet line once the list has started instead folds in
    as its own <li>, a wrapped continuation rather than a second lead. Each item goes through
    the same escape-then-backtick path as a paragraph."""
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    first_bullet = next((i for i, line in enumerate(lines) if line.startswith("- ")), None)
    if first_bullet is None:
        return f"<p>{_html_prose(block)}</p>"
    html = f"<p>{_html_prose(' '.join(lines[:first_bullet]))}</p>" if first_bullet else ""
    items = [line[2:].strip() if line.startswith("- ") else line for line in lines[first_bullet:]]
    return html + "<ul>" + "".join(f"<li>{_html_prose(item)}</li>" for item in items) + "</ul>"


def _html_prose(text):
    """Escape first, then promote backticks, so a `<` inside an identifier cannot become markup.
    The order is the whole point: doing it the other way lets a label from someone else's branch
    name inject a live element."""
    return _codeify(escape(text))

OV_W, OV_H, OV_COL, OV_ROW, OV_PAD = 150, 58, 110, 28, 16
_OV_KEYS = ("new", "changed", "guard", "removed", "same", "config", "moved")


def overview_layout(nodes, edges):
    """Layered layout of the overview graph, deterministic for the same input.

    Cycles are broken by a DFS in node input order; the edge that closes a cycle is flagged in
    `back` and still drawn. A node's layer (column) is its longest path from a source over the
    forward edges, and rows inside a layer follow the barycenter of the predecessors' rows
    (2 sweeps, ties by input order). Returns {layer, cols, row, back, pairs}: per-node layer,
    node indexes per column in row order, per-node row, per-edge back flag, per-edge (from, to)."""
    index = {n["id"]: i for i, n in enumerate(nodes)}
    pairs = [(index[e["from"]], index[e["to"]]) for e in edges]
    out = [[] for _ in nodes]
    for k, (a, b) in enumerate(pairs):
        out[a].append((b, k))
    state, back = [0] * len(nodes), [False] * len(edges)

    def visit(u):
        state[u] = 1
        for v, k in out[u]:
            if state[v] == 1:
                back[k] = True
            elif state[v] == 0:
                visit(v)
        state[u] = 2

    for u in range(len(nodes)):
        if not state[u]:
            visit(u)
    preds = [[] for _ in nodes]
    for k, (a, b) in enumerate(pairs):
        if not back[k]:
            preds[b].append(a)
    layer = {}

    def depth(v):
        if v not in layer:
            layer[v] = 1 + max((depth(u) for u in preds[v]), default=-1)
        return layer[v]

    layers = [depth(i) for i in range(len(nodes))]
    cols = [[] for _ in range(max(layers) + 1)]
    for i, ly in enumerate(layers):
        cols[ly].append(i)
    row = {i: r for col in cols for r, i in enumerate(col)}
    for _ in range(2):
        for col in cols[1:]:
            col.sort(key=lambda i: (sum(row[u] for u in preds[i]) / len(preds[i]) if preds[i] else row[i], i))
            row.update({i: r for r, i in enumerate(col)})
    return {"layer": layers, "cols": cols, "row": row, "back": back, "pairs": pairs}


def _clip(text, limit):
    text = text.replace("`", "")
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


def _group_attrs(item, target_of):
    group = item.get("group")
    return (f' data-path="{escape(target_of(item), quote=True)}"'
            f' data-group="{"" if group is None else group}"')


def overview_svg(ov, target_of=lambda item: "", mid=""):
    """Inline SVG for a validated overview. Every string is escaped; ids never reach the markup
    (nodes are addressed by index), kinds come from visuals.py's fixed vocabulary."""
    nodes, edges = ov["nodes"], ov["edges"]
    lay = overview_layout(nodes, edges)
    cols, row = lay["cols"], lay["row"]
    rows_max = max(len(c) for c in cols)
    pos = {}
    for c, col in enumerate(cols):
        shift = (rows_max - len(col)) * (OV_H + OV_ROW) // 2
        for i in col:
            pos[i] = (OV_PAD + c * (OV_W + OV_COL), OV_PAD + shift + row[i] * (OV_H + OV_ROW))
    bottom = OV_PAD + rows_max * OV_H + (rows_max - 1) * OV_ROW
    n_back = sum(lay["back"])
    height = (bottom + 16 + 18 * (n_back - 1) + 12 + OV_PAD) if n_back else bottom + OV_PAD
    width = 2 * OV_PAD + len(cols) * OV_W + (len(cols) - 1) * OV_COL
    kinds = [e["kind"] for e in edges]
    mp = f"cw-ov-m-{mid}-" if mid else "cw-ov-m-"
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" class="cw-ov" viewBox="0 0 {width} {height}" role="group"'
             f' aria-label="{escape(ov["title"], quote=True)}" style="min-width:{int(width * 0.8)}px"><defs>']
    for kind in dict.fromkeys(kinds):
        parts.append(f'<marker id="{mp}{kind}" markerUnits="userSpaceOnUse" markerWidth="10"'
                     ' markerHeight="10" refX="9" refY="5" orient="auto">'
                     f'<path class="cw-ov-m-{kind}" d="M0,0L10,5L0,10z"/></marker>')
    parts.append("</defs>")
    labels, lane = [], 0
    for k, edge in enumerate(edges):
        a, b = lay["pairs"][k]
        (ax, ay), (bx, by) = pos[a], pos[b]
        if lay["back"][k] or lay["layer"][a] >= lay["layer"][b]:
            sx, sy, tx, ty = ax + OV_W // 2 + 14, ay + OV_H, bx + OV_W // 2 - 14, by + OV_H
            low = bottom + 16 + 18 * lane
            lane += 1
            cy = (low - (sy + ty) / 8) / 0.75
            d = f"M{sx},{sy}C{sx},{cy:g} {tx},{cy:g} {tx},{ty}"
            mid = ((sx + tx) / 2, low)
        else:
            sx, sy, tx, ty = ax + OV_W, ay + OV_H // 2, bx, by + OV_H // 2
            dx = (tx - sx) // 2
            d = f"M{sx},{sy}C{sx + dx},{sy} {tx - dx},{ty} {tx},{ty}"
            mid = ((sx + tx) / 2, (sy + ty) / 2)
        parts.append(f'<path class="cw-ov-e cw-ov-e-{edge["kind"]}" data-from="{a}" data-to="{b}"'
                     f' marker-end="url(#{mp}{edge["kind"]})" d="{d}"/>')
        if edge["label"]:
            w = int(len(edge["label"]) * 5.6 + 10)
            labels.append(f'<g class="cw-ov-lbl" data-from="{a}" data-to="{b}">'
                          f'<rect x="{mid[0] - w / 2:g}" y="{mid[1] - 7:g}" width="{w}" height="14" rx="3"/>'
                          f'<text x="{mid[0]:g}" y="{mid[1] + 3.5:g}" text-anchor="middle">'
                          f'{escape(_clip(edge["label"], 14))}</text></g>')
    for i, node in enumerate(nodes):
        x, y = pos[i]
        tip = [f'{node["label"]}: {node["note"]}' if node["note"] else node["label"]] + node["refs"]
        group = node["group"]
        dot = f'<circle class="cw-ov-dot cw-ov-g{group % 6 + 1}" cx="{x + 10}" cy="{y + 11}" r="4"/>' \
            if group is not None else ""
        pill = (f'<rect class="cw-ov-pill" x="{x + OV_W - 54}" y="{y + 4}" width="48" height="13" rx="6"/>'
                f'<text class="cw-ov-pilltxt" x="{x + OV_W - 30}" y="{y + 13.5}" text-anchor="middle">REVIEW</text>'
                ) if node["watch"] else ""
        parts.append(
            f'<g class="cw-ov-node cw-ov-n-{node["kind"]}{" cw-ov-watch" if node["watch"] else ""}"'
            f' data-ov="{i}"{_group_attrs(node, target_of)} tabindex="0" role="link"'
            f' aria-label="{escape(node["label"], quote=True)}, {node["kind"]}'
            f'{", needs review" if node["watch"] else ""}">'
            f'<title>{escape(chr(10).join(tip))}</title>'
            f'<rect class="cw-ov-box" x="{x}" y="{y}" width="{OV_W}" height="{OV_H}" rx="8"/>{dot}'
            f'<text class="cw-ov-kind" x="{x + (18 if dot else 10)}" y="{y + 14}">{node["kind"]}</text>{pill}'
            f'<text class="cw-ov-name" x="{x + 10}" y="{y + 33}">{escape(_clip(node["label"], 20))}</text>'
            f'<text class="cw-ov-note" x="{x + 10}" y="{y + 49}">{escape(_clip(node["note"], 26))}</text></g>')
    parts += labels
    parts.append("</svg>")
    return "".join(parts)


def overview_html(ov, explain, target_of=lambda item: ""):
    """The whole System change section: heading, title as subtitle, diagram, legend, note chips."""
    heading = "How it works" if explain else "System change"
    present = [k for k in _OV_KEYS if any(x["kind"] == k for x in ov["nodes"] + ov["edges"] + ov["notes"])]
    legend = "".join(f'<span class="cw-ov-key cw-ov-n-{k}"><i></i>{k}</span>' for k in present)
    chips = "".join(
        f'<span class="cw-ov-chip cw-ov-n-{c["kind"]}"{_group_attrs(c, target_of)} tabindex="0" role="link">'
        f'{escape(_clip(c["text"], 48))}</span>' for c in ov["notes"])
    return ('<section class="cw-overview" id="cw-overview" aria-labelledby="cw-overview-h">'
            f'<h2 class="h2-row" id="cw-overview-h"><span>{heading}</span><span class="ctl">'
            f'{_MAX_BUTTON_HTML.replace("<button ", "<button data-ov-zoom ", 1)}</span></h2>'
            f'<p class="cw-ov-sub">{escape(ov["title"])}</p>'
            f'<div class="cw-vis cw-ov-wrap">{overview_svg(ov, target_of)}</div>'
            f'<div class="cw-ov-meta"><div class="cw-ov-legend">{legend}</div>'
            f'<div class="cw-ov-chips">{chips}</div></div></section>')


_CTX_SOURCES = ("jira", "github", "linear", "other")
_CTX_FOUND_IN = {"branch": "branch name", "pr_body": "PR description", "commit": "a commit message", "link": "a pasted link"}
_CTX_STATUS = {"covered": ("ok", "\u2713"), "partial": ("pt", "\u25d0"), "missing": ("no", "\u2717"), "extra": ("ex", "+")}
CTX_URL = re.compile(r"https?://\S{1,500}")
_CTX_SHA = re.compile(r"[0-9a-fA-F]{7,40}")
_CTX_REF = re.compile(r"(.+):(\d+)")


def _ctx_text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ""


def clean_context(raw):
    """(clean | None, problems) for context.json. Everything the context card shows passes through
    here: enums are enforced, text is capped, a bad item is dropped without losing the rest."""
    if not isinstance(raw, dict):
        return None, ["context: not an object"]
    problems = []
    clean = {"ticket": None, "head_sha": "", "purpose": _ctx_text(raw.get("purpose"), 400), "items": [], "gaps": []}
    t = raw.get("ticket")
    if isinstance(t, dict):
        key, source = _ctx_text(t.get("key"), 40), t.get("source")
        if not key or not isinstance(source, str) or source not in _CTX_SOURCES:
            problems.append("context.ticket: needs a key and a source of jira, github, linear or other")
        else:
            url = t.get("url")
            if url is not None and not (isinstance(url, str) and CTX_URL.fullmatch(url)):
                problems.append("context.ticket.url: not an http(s) URL, dropped")
                url = None
            found = t.get("found_in")
            clean["ticket"] = {"source": source, "key": key, "title": _ctx_text(t.get("title"), 200),
                               "status": _ctx_text(t.get("status"), 40), "url": url,
                               "found_in": found if isinstance(found, str) and found in _CTX_FOUND_IN else None}
    sha = raw.get("head_sha")
    if isinstance(sha, str) and _CTX_SHA.fullmatch(sha):
        clean["head_sha"] = sha
    items = raw.get("items") if isinstance(raw.get("items"), list) else []
    if len(items) > 20:
        problems.append(f"context.items: {len(items) - 20} beyond the first 20 dropped")
    for i, item in enumerate(items[:20]):
        text = _ctx_text(item.get("text"), 200) if isinstance(item, dict) else ""
        if not text or not isinstance(item.get("status"), str) or item["status"] not in _CTX_STATUS:
            problems.append(f"context.items[{i}]: needs text and a status of covered, partial, missing or extra")
            continue
        ref = item.get("ref")
        if ref is not None and not (isinstance(ref, str) and _CTX_REF.fullmatch(ref)):
            problems.append(f"context.items[{i}].ref: not path:line, dropped")
            ref = None
        clean["items"].append({"status": item["status"], "text": text, "ref": ref,
                               "note": _ctx_text(item.get("note"), 200)})
    gaps = raw.get("gaps") if isinstance(raw.get("gaps"), list) else []
    clean["gaps"] = [g for g in (_ctx_text(g, 200) for g in gaps[:8]) if g]
    if not (clean["ticket"] or clean["purpose"] or clean["items"] or clean["gaps"]):
        return None, problems + ["context: nothing usable, ignored"]
    return clean, problems


def load_context(path):
    """(clean, lines) for context.json; `lines` are the `context:` report lines pipeline.py echoes."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError, RecursionError) as exc:
        return None, [f"context: unreadable ({exc.__class__.__name__})"]
    clean, problems = clean_context(raw)
    return clean, [p if p.startswith("context:") else f"context: {p}" for p in problems]


def _ctx_ref(ref, files):
    path, _, line = ref.rpartition(":") if ref else ("", "", "")
    if path in files:
        return (f'<span class="cw-ctx-ev cw-ctx-ref" role="link" tabindex="0" data-path="{escape(path)}"'
                f' data-line="{escape(line)}">{escape(ref)}</span>')
    return '<span class="cw-ctx-ev cw-ctx-none">not in diff</span>'


def context_html(ctx, files, current_head=""):
    """The Context card, or the empty-state form when there is no ticket context. `files` is the
    parsed diff (a ref links only when its path is in it); `current_head` is the page's head sha,
    compared with the one the comparison was made against."""
    if not ctx:
        return ('<section class="cw-context" id="cw-context" aria-labelledby="cw-context-h">'
                '<h2 id="cw-context-h">Context</h2><form class="cw-ctx-link">'
                '<span class="cw-ctx-empty">No linked ticket found.</span>'
                '<input type="text" name="ticket" class="cw-ctx-input" autocomplete="off" maxlength="500"'
                ' placeholder="Paste a Jira / GitHub / Linear link or key" aria-label="Ticket link or key">'
                '<button type="submit" class="cw-ctx-submit">Link ticket</button>'
                '<p class="cw-ctx-msg" aria-live="polite"></p></form></section>')
    t = ctx.get("ticket")
    head = ""
    if t:
        key = escape(t["key"])
        url = t.get("url")
        key_html = (f'<a class="cw-ctx-key" href="{escape(url)}" target="_blank" rel="noopener noreferrer">{key}</a>'
                    if url else f'<span class="cw-ctx-key">{key}</span>')
        found = _CTX_FOUND_IN.get(t.get("found_in"))
        head = (f'<div class="cw-ctx-head"><span class="cw-ctx-src"><span class="cw-ctx-glyph" aria-hidden="true">'
                f'&#9670;</span>{escape(t["source"].capitalize())}</span>{key_html}'
                + (f'<span class="cw-ctx-title">{_html_prose(t["title"])}</span>' if t["title"] else "")
                + (f'<span class="cw-ctx-pill">{escape(t["status"])}</span>' if t["status"] else "")
                + (f'<span class="cw-ctx-found">found in {found}</span>' if found else "")
                + "</div>")
    secs = []
    if ctx.get("purpose"):
        secs.append(f'<div class="cw-ctx-sec"><div class="cw-ctx-label">What it\'s for</div>'
                    f'<p class="cw-ctx-purpose">{_html_prose(ctx["purpose"])}</p></div>')
    items = ctx.get("items") or []
    if items:
        counts = {k: sum(i["status"] == k for i in items) for k in ("covered", "partial", "missing")}
        bar = "".join(f'<i class="{_CTX_STATUS[k][0]}" style="flex:{n}"></i>' for k, n in counts.items() if n)
        rows = "".join(
            f'<li><span class="cw-ctx-mark {_CTX_STATUS[i["status"]][0]}" role="img" aria-label="{i["status"]}">'
            f'{_CTX_STATUS[i["status"]][1]}</span><span>{_html_prose(i["text"])}'
            + (f'<span class="cw-ctx-sub">{_html_prose(i["note"])}</span>' if i.get("note") else "")
            + f'</span>{_ctx_ref(i.get("ref"), files)}</li>' for i in items)
        secs.append('<div class="cw-ctx-sec"><div class="cw-ctx-label"><span>Asked for vs. this diff</span>'
                    f'<span class="cw-ctx-bar" aria-hidden="true">{bar}</span></div><ul class="cw-ctx-items">{rows}</ul></div>')
    if ctx.get("gaps"):
        secs.append('<div class="cw-ctx-sec"><div class="cw-ctx-label">Gaps and questions</div>'
                    + "".join(f'<p class="cw-ctx-gap">{_html_prose(g)}</p>' for g in ctx["gaps"]) + "</div>")
    seen, now = ctx.get("head_sha") or "", current_head or ""
    stale = ""
    if seen and now and not (seen.startswith(now) or now.startswith(seen)):
        stale = (f'<p class="cw-ctx-stale">Compared against {escape(seen[:7])}, head is now {escape(now[:7])}.</p>')
    return ('<section class="cw-context" id="cw-context" aria-labelledby="cw-context-h"><h2 id="cw-context-h">Context</h2>'
            f'<div class="cw-ctx-card">{head}{"".join(secs)}</div>{stale}</section>')


def render_html(analysis, files, template, walkthrough="", links=None, title=None,
                now=None, complexity=None, state=None, explain=False, order=None,
                symdelta=None, visuals=None, context=None):
    target = analysis.get("target") or ""
    count, added, removed, net = counts_from(files)
    sign = "+" if net >= 0 else ""

    # Explain mode diffs the named path against the empty baseline, so the add/remove arithmetic
    # only ever restates the file size. It reads as scope instead.
    scope = (f'<div class="fact"><b>Scope</b><span>{count} file{"" if count == 1 else "s"}, '
             f'{added} line{"" if added == 1 else "s"}</span></div>') if explain else (
            f'<div class="fact"><b>Changed</b><span>{count} '
            f'file{"" if count == 1 else "s"}, +{added} -{removed} (net {sign}{net})</span></div>')
    heading = title or (f"Code walkthrough: {target}" if target else "Code walkthrough")
    pr_url = links.get("pr_url") if links else None
    pr_link = (f'<p class="sub cw-title-sub"><a href="{escape(pr_url)}" target="_blank" rel="noopener noreferrer">'
               'View pull request</a></p>') if pr_url else ""
    body = [
        f'<h1 class="cw-title">{escape(heading)}</h1>{pr_link}',
        '<div class="facts">',
        "  " + scope,
        f'  <div class="fact"><b>Target</b><span>{escape(target)}</span></div>',
    ]
    cx = summary_line(complexity)
    if cx:
        body.append(f'  <div class="fact"><b>Complexity</b><span>{_html_prose(cx)}</span></div>')
    verdict = analysis.get("verdict") or ""
    if verdict:
        label = "Summary" if explain else "Verdict"
        body.append(f'  <div class="fact"><b>{label}</b><span>{_html_prose(verdict)}</span></div>')
    body.append("</div>")

    if not explain:
        body.append(context_html(context, files, (links or {}).get("head_sha", "")))

    overview = _paragraphs(analysis.get("overview"))
    if overview:
        # One heading in both modes: the topic a cold reader needs is the same shape whether
        # it is a change ("what changed") or an area ("what this is"), so the two headings this
        # used to swap between said nothing the field itself didn't already carry.
        body.append("<h2>Overview</h2>")
        body += [_block_html(block) for block in overview]

    clean = visuals or {}
    pr_picture = clean.get("pr")
    ov = clean.get("overview")
    if ov:
        group_file = {g.idx: g[2][0] for g in story(order or list(files), files, analysis.get("groups"), None)
                      if hasattr(g, "idx") and g[2]}

        def target_of(item):
            refs = item.get("refs") or []
            return refs[0].partition(":")[0] if refs else group_file.get(item.get("group"), "")

        body.append(overview_html(ov, explain, target_of))
    elif pr_picture:
        pic_heading = "This code in one picture" if explain else "The PR in one picture"
        body.append('<section class="cw-pr-picture" id="cw-pr-picture"'
                    f' aria-labelledby="cw-pr-picture-h"><h2 id="cw-pr-picture-h">{escape(pic_heading)}</h2>'
                    f'<div class="cw-vis" role="group" aria-label="{escape(pr_picture["title"])}">'
                    f'{pr_picture["html"]}</div></section>')

    if walkthrough.strip():
        body.append('<h2 class="h2-row"><span>Walkthrough</span><span class="ctl" id="wt-ctl"></span></h2>')
        group_pictures = clean.get("groups") or {}

        def picture(match):
            html = group_pictures.get(int(match.group(1)))
            return f'<div class="cw-vis cw-vis-group">{html}</div>' if html else ""

        body.append(PICTURE_MARKER.sub(picture, walkthrough.rstrip("\n")))

    all_symbols = render_symbols(symdelta, explain, paths=order if explain else (), inline=True) \
        if symdelta else ""
    if all_symbols:
        body.append('<details class="cw-allsyms"><summary>All symbols</summary>'
                    f'{all_symbols.rstrip(chr(10))}</details>')

    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    foot = f"{escape(target)} · generated {stamp}"
    if links and links.get("pr_url"):
        foot += (f' · <a href="{escape(links["pr_url"])}" target="_blank"'
                 ' rel="noopener noreferrer">pull request</a>')
    body.append(f'<div class="foot">{foot}</div>')

    page = template.replace(TITLE_PLACEHOLDER, escape(heading))
    page = page.replace(CONTENT_PLACEHOLDER, "\n".join(body))
    if state is not None:
        # \u003c is a JSON string escape, not an HTML entity -- script content is
        # never HTML-entity-decoded, and JSON.parse decodes \u003c natively, so
        # nothing has to reverse it client-side (an &lt; entity would, and would
        # also mangle a note body that already contained the literal text "&lt;").
        blob = json.dumps(state).replace("<", "\\u003c")
        page = page.replace(STATE_PLACEHOLDER,
                             f'<script type="application/json" id="hr-state">{blob}</script>')
    return page


def load_visuals(path, n_groups, placed=None, ctx=None):
    """(clean, lines) for visuals.json. Everything inlined on the page passes through
    visuals.check_visuals here; `lines` are the `visuals:` report lines pipeline.py echoes."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError, RecursionError) as exc:
        return None, [f"visuals: unreadable ({exc.__class__.__name__})"]
    clean, problems = visuals_mod.check_visuals(raw, n_groups, ctx)
    if problems and problems[0].startswith("visuals: "):
        return clean, [f"visuals: ignored, {problems[0][len('visuals: '):]}"]
    if placed is not None:
        for idx in sorted(set(clean["groups"]) - set(placed)):
            del clean["groups"][idx]
            problems.append(f"visuals.groups[{idx}]: group has no files on this page")
    raw = raw if isinstance(raw, dict) else {}
    total = int(bool(raw.get("pr"))) + int(raw.get("overview") is not None) + (len(raw["groups"]) if isinstance(raw.get("groups"), list) else 0)
    kept = int(bool(clean["pr"])) + int(bool(clean["overview"])) + len(clean["groups"])
    return clean, [f"visuals: {kept} kept, {max(total - kept, 0)} dropped"] + [
        f"visuals: {problem}" for problem in problems]


def _read(path):
    return Path(path).read_text(errors="replace") if path else ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--diff", required=True)
    parser.add_argument("--format", required=True, choices=("html",))
    parser.add_argument("--walkthrough", help="section-walkthrough.html")
    parser.add_argument("--links")
    parser.add_argument("--complexity", help="complexity.json, for the complexity fact")
    parser.add_argument("--symdelta", help="symdelta.json, for the All symbols graph")
    parser.add_argument("--visuals", help="visuals.json, re-checked here before anything is inlined")
    parser.add_argument("--context", help="context.json, re-checked here before anything is inlined")
    parser.add_argument("--template", required=True, help="the page template")
    parser.add_argument("--state", help="state.json, inlined behind HR_STATE")
    parser.add_argument("--title")
    parser.add_argument("--explain", action="store_true",
                        help="the target is code as it stands, not a change: reword the headings "
                             "and drop the add/remove arithmetic the empty baseline makes empty")
    args = parser.parse_args()

    analysis = json.loads(Path(args.analysis).read_text())
    raw_diff = Path(args.diff).read_text(errors="replace")
    order, files = parse_hunks(raw_diff)
    links = json.loads(Path(args.links).read_text()) if args.links else None
    complexity = json.loads(Path(args.complexity).read_text()) if args.complexity else None
    state = json.loads(Path(args.state).read_text()) if args.state else None
    symdelta = json.loads(Path(args.symdelta).read_text()) if args.symdelta else None

    clean = None
    if args.visuals:
        placed = {g.idx for g in story(order, files, analysis.get("groups"), None)}
        ctx = visuals_mod.diff_ctx(raw_diff, symdelta)
        clean, lines = load_visuals(args.visuals, len(analysis.get("groups") or []), placed, ctx)
        print("\n".join(lines), file=sys.stderr)

    context = None
    if args.context:
        context, lines = load_context(args.context)
        if lines:
            print("\n".join(lines), file=sys.stderr)

    sys.stdout.write(render_html(
        analysis, files, _read(args.template),
        _read(args.walkthrough), links, args.title,
        complexity=complexity, state=state, explain=args.explain, order=order,
        symdelta=symdelta, visuals=clean, context=context,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
