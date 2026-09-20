#!/usr/bin/env python3
"""Assemble the finished HTML page from analysis.json and the section files.

  python3 render.py --analysis analysis.json --diff raw.diff --format html \
      --template ../assets/diff-review-template.html \
      --walkthrough section-walkthrough.html \
      [--symbols section-symbols.html] [--links links.json] [--title "..."] > out.html

The page body was the last thing a model still typed out, and almost none of it was judgment.
Six of its seven sections are either a section file pasted byte for byte or a string that
analysis.json already carries; the facts strip is arithmetic and the footer is a timestamp. So
the renderer subagents are gone and this walks the same data instead.

What that removes, beyond the tokens: the two escaping rules in references/builder.md were
instructions a model had to remember every time, and getting one wrong meant either a live
`<img onerror=...>` from a branch name in someone else's PR, or a diagram replaced by a raw
mermaid error box. They are now one function with a test.

The judgment stays in analysis.json: `what_changed`, `how_it_works`, `flow_mermaid`, `verdict`
and the per-file `role` and per-hunk `note` that walkthrough.py places.

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
from sections import _codeify, wrap_label  # noqa: E402  one owner for backtick-to-<code>, wrap
from validate_analysis import parse_hunks  # noqa: E402  one owner for diff parsing

TITLE_PLACEHOLDER = "<!-- DIFF_TITLE -->"
CONTENT_PLACEHOLDER = "<!-- DIFF_CONTENT -->"
STATE_PLACEHOLDER = "<!-- HR_STATE -->"


def counts_from(files):
    """(file count, added, removed, net) for the facts strip. Taken from the diff rather than
    from `git diff --numstat`, because numstat writes `-` for a binary file and the walkthrough
    already proved this count matches numstat exactly on a real 76-file branch."""
    added = sum(entry["added"] for entry in files.values())
    removed = sum(entry["removed"] for entry in files.values())
    return len(files), added, removed, added - removed


def _paragraphs(text):
    """Prose split on blank lines. A model writing two paragraphs into one JSON string is the
    normal case, and joining them into one <p> loses the break it meant."""
    return [block.strip() for block in (text or "").split("\n\n") if block.strip()]


def _html_prose(text):
    """Escape first, then promote backticks, so a `<` inside an identifier cannot become markup.
    The order is the whole point: doing it the other way lets a label from someone else's branch
    name inject a live element."""
    return _codeify(escape(text))


def _flow_tb(mermaid):
    """Force FLOW to a portrait-friendly top-to-bottom layout, at render time rather than
    trusting the prose agent to write `flowchart TB` itself: analysis.json is free-form prose
    output, and the one thing this page cannot afford is a wide `flowchart LR` shrunk to
    illegibility on a phone."""
    if mermaid.startswith("flowchart LR"):
        return "flowchart TB" + mermaid[len("flowchart LR"):]
    return mermaid


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
    """Run every quoted node/subgraph label, and every edge label, in FLOW through
    `sections.wrap_label`, so a long space-free identifier gets somewhere to break instead of
    stretching its node across the page (see `wrap_label` for why it is zero-width spaces and
    not `<br/>`)."""
    mermaid = _MERMAID_LABEL_RE.sub(lambda m: f'{m.group(1)}"{wrap_label(m.group(2))}"', mermaid)
    return _MERMAID_EDGE_LABEL_RE.sub(_wrap_edge_label, mermaid)


def render_html(analysis, files, template, walkthrough="", links=None, title=None,
                now=None, symbols="", complexity=None, state=None, explain=False):
    target = analysis.get("target") or ""
    count, added, removed, net = counts_from(files)
    sign = "+" if net >= 0 else ""

    # Explain mode diffs the named path against the empty baseline, so the add/remove arithmetic
    # only ever restates the file size. It reads as scope instead.
    scope = (f'<div class="fact"><b>Scope</b><span>{count} file{"" if count == 1 else "s"}, '
             f'{added} line{"" if added == 1 else "s"}</span></div>') if explain else (
            f'<div class="fact"><b>Changed</b><span>{count} '
            f'file{"" if count == 1 else "s"}, +{added} -{removed} (net {sign}{net})</span></div>')
    body = [
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

    what = _paragraphs(analysis.get("what_changed"))
    if what:
        body.append(f'<h2>{"What this is" if explain else "What changed"}</h2>')
        body += [f"<p>{_html_prose(p)}</p>" for p in what]

    flow = _wrap_flow_labels(_flow_tb((analysis.get("flow_mermaid") or "").strip()))
    if flow:
        body.append("<h2>Flow</h2>")
        # tabindex and role give the zoom modal keyboard access before its JS has run; wire()
        # sets them again on the live element, which is a harmless no-op.
        # svgbox-flow: tells panZoom() and the CSS to size FLOW from its own viewBox (fit,
        # never upscale) instead of stretching to the panel like the bigger stacked diagrams
        # sections.py pastes in below.
        body.append('<div class="panel svgbox svgbox-flow" tabindex="0" role="button"'
                    ' aria-label="Expand diagram to full size">'
                    f'<pre class="mermaid">{escape(flow)}</pre></div>')

    how = _paragraphs(analysis.get("how_it_works"))
    if how:
        body.append("<h2>How it works</h2>")
        body += [f"<p>{_html_prose(p)}</p>" for p in how]

    # section-symbols.html opens with its own <h2>CHANGES VISUALIZATION</h2> and caption, both
    # written by sections.py, so nothing is added here.
    if symbols.strip():
        body.append(symbols.rstrip("\n"))

    if walkthrough.strip():
        # The toggle lives in the heading because that is the only place on the page that
        # belongs to the walkthrough as a whole. No walkthrough, no heading, no toggle --
        # there would be nothing for it to hide. It names the state it is in, not the one it
        # switches to, and explanations start on, so the label here is the on label.
        body.append('<h2 class="h2-row"><span>Walkthrough</span><span class="ctl">'
                    '<button type="button" id="wt-notes-toggle"'
                    ' aria-label="Explanations shown. Activate to hide them."'
                    '>explanations</button></span></h2>')
        body.append(walkthrough.rstrip("\n"))

    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    foot = f"{escape(target)} · generated {stamp}"
    if links and links.get("pr_url"):
        foot += (f' · <a href="{escape(links["pr_url"])}" target="_blank"'
                 ' rel="noopener noreferrer">pull request</a>')
    body.append(f'<div class="foot">{foot}</div>')

    heading = title or (f"Code walkthrough: {target}" if target else "Code walkthrough")
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
    parser.add_argument("--template", required=True, help="the page template")
    parser.add_argument("--symbols", help="section-symbols.html")
    parser.add_argument("--state", help="state.json, inlined behind HR_STATE")
    parser.add_argument("--title")
    parser.add_argument("--explain", action="store_true",
                        help="the target is code as it stands, not a change: reword the headings "
                             "and drop the add/remove arithmetic the empty baseline makes empty")
    args = parser.parse_args()

    analysis = json.loads(Path(args.analysis).read_text())
    _order, files = parse_hunks(Path(args.diff).read_text(errors="replace"))
    links = json.loads(Path(args.links).read_text()) if args.links else None
    complexity = json.loads(Path(args.complexity).read_text()) if args.complexity else None
    state = json.loads(Path(args.state).read_text()) if args.state else None

    sys.stdout.write(render_html(
        analysis, files, _read(args.template),
        _read(args.walkthrough), links, args.title, symbols=_read(args.symbols),
        complexity=complexity, state=state, explain=args.explain,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
