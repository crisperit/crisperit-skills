#!/usr/bin/env python3
"""Assemble the finished HTML page or markdown recap from analysis.json and the section files.

  python3 render.py --analysis analysis.json --diff raw.diff --format html \
      --template ../assets/diff-review-template.html \
      --explorer section-explorer.html --walkthrough section-walkthrough.html \
      [--symbols section-symbols.html] [--links links.json] [--title "..."] > out.html

  python3 render.py --analysis analysis.json --diff raw.diff --format md \
      --layers section-layers.md --coupling section-coupling.md \
      --structure section-structure.md --walkthrough section-walkthrough.md \
      [--links links.json] > out.md

The page body was the last thing a model still typed out, and almost none of it was judgment.
Six of its seven sections are either a section file pasted byte for byte or a string that
analysis.json already carries; the facts strip is arithmetic and the footer is a timestamp. So
the renderer subagents are gone and this walks the same data instead.

What that removes, beyond the tokens: the two escaping rules in references/builder.md were
instructions a model had to remember every time, and getting one wrong meant either a live
`<img onerror=...>` from a branch name in someone else's PR, or a diagram replaced by a raw
mermaid error box. They are now one function with a test.

The judgment stays in analysis.json: `what_changed`, `how_it_works`, `flow_mermaid`, `verdict`,
`section_notes` and the per-file `role` and per-hunk `note` that walkthrough.py places.

Prose fields mark identifiers with backticks. Markdown passes those through, since GitHub
renders them as inline code; HTML promotes them to `<code>` after escaping, so a tag is only
ever added to text that is already safe. Same rule sections.py already follows for its captions.

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
# Order runs orientation, then concepts, then structure, then detail. Markdown stacks the three
# graphs widest first, because which modules exist and which way they depend is the context that
# makes the narrower two mean anything.
MD_SECTION_ORDER = ("layers", "coupling", "structure")
# Readable budget for the whole recap, which is the number walkthrough.DEFAULT_MAX_CHARS is
# sized against. GitHub's hard ceiling is 65536 and it refuses a longer body outright; 45000
# leaves headroom for the graph sections and prose while still being a body someone will
# scroll rather than skip.
MAX_BODY_CHARS = 45000
# The demotion logic in walkthrough.py reflows line counts as it shrinks, so cutting
# --max-chars by the overflow amount doesn't cut the recap by the same amount. This margin
# pushes the naive subtraction down far enough to actually land under budget on the retry,
# instead of landing just short and overflowing again.
OVERFLOW_MARGIN_CHARS = 200
# A one-file walkthrough's own skeleton (leading comment, <details> wrapper, boilerplate closing
# sentence, one `- path +n -n` line) measures 272 chars with nothing else in it. A suggested
# --max-chars below that isn't a smaller ask, it's one walkthrough.py's own demotion floor
# already ignores, returning that skeleton regardless. 300 sits just past it.
MIN_USEFUL_MAX_CHARS = 300


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

# walkthrough.py's own demotion floor for this diff, stamped on the line after its section
# marker. An older section file (or no --walkthrough at all) carries no such line, and
# overflow_warning falls back to MIN_USEFUL_MAX_CHARS rather than treating that as an error.
_WALKTHROUGH_FLOOR_RE = re.compile(r"<!-- visual-diff:walkthrough-floor (\d+) -->")


def _wrap_edge_label(m):
    arrow, quoted, bare = m.groups()
    if quoted is not None:
        return f'{arrow}|"{wrap_label(quoted)}"|'
    return f'{arrow}|{wrap_label(bare)}|'


def _wrap_flow_labels(mermaid):
    """Wrap every quoted node/subgraph label, and every edge label, in FLOW so Mermaid receives
    lines pre-broken with `<br/>` instead of auto-wrapping -- and clipping -- SVG text at the
    `flowchart.wrappingWidth` default under `securityLevel: 'strict'` (see `sections.wrap_label`)."""
    mermaid = _MERMAID_LABEL_RE.sub(lambda m: f'{m.group(1)}"{wrap_label(m.group(2))}"', mermaid)
    return _MERMAID_EDGE_LABEL_RE.sub(_wrap_edge_label, mermaid)


def render_html(analysis, files, template, explorer="", walkthrough="", links=None, title=None,
                now=None, symbols="", complexity=None, state=None):
    target = analysis.get("target") or ""
    count, added, removed, net = counts_from(files)
    sign = "+" if net >= 0 else ""
    notes = analysis.get("section_notes") or {}

    body = [
        '<div class="facts">',
        f'  <div class="fact"><b>Changed</b><span>{count} '
        f'file{"" if count == 1 else "s"}, +{added} -{removed} (net {sign}{net})</span></div>',
        f'  <div class="fact"><b>Target</b><span>{escape(target)}</span></div>',
    ]
    cx = summary_line(complexity)
    if cx:
        body.append(f'  <div class="fact"><b>Complexity</b><span>{_html_prose(cx)}</span></div>')
    verdict = analysis.get("verdict") or ""
    if verdict:
        body.append(f'  <div class="fact"><b>Verdict</b><span>{_html_prose(verdict)}</span></div>')
    body.append("</div>")

    what = _paragraphs(analysis.get("what_changed"))
    if what:
        body.append("<h2>What changed</h2>")
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

    # No heading of our own here: section-explorer.html opens with its own <h2>RELATIONS</h2>,
    # and adding one produced two headings in a row on a real page. The note goes above the
    # section, so it has to come before the paste rather than after it.
    if explorer.strip():
        if notes.get("explorer"):
            body.append(f"<p>{_html_prose(notes['explorer'])}</p>")
        body.append(explorer.rstrip("\n"))

    # Same paste-verbatim rule as explorer above: section-symbols.html opens with its own
    # <h2>CHANGES VISUALIZATION</h2> and caption, both written by sections.py, so nothing is added here.
    if symbols.strip():
        body.append(symbols.rstrip("\n"))

    if walkthrough.strip():
        # The toggle lives in the heading because that is the only place on the page that
        # belongs to the walkthrough as a whole. No walkthrough, no heading, no toggle --
        # there would be nothing for it to hide. It names the state it is in, not the one it
        # switches to, and explanations start on, so the label here is the on label.
        body.append('<h2 class="h2-row"><span>Walkthrough</span><span class="ctl">'
                    # The page has no other keyboard-help affordance since #fb-toggle was
                    # unwired, so the n/N comment-nav shortcut is announced here.
                    '<span class="ctl-hint">n / N: next, previous comment</span>'
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

    heading = title or (f"Visual diff: {target}" if target else "Visual diff")
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


def _reading_order_md(groups):
    """Stand-in for the walkthrough's own group listing when there is no walkthrough: a
    PR-description-only run has no per-hunk notes, but validate_analysis --rendered still
    requires every changed path to show up somewhere, and analysis["groups"] is the only thing
    left naming them."""
    out = ["### Reading order", ""]
    for group in groups:
        if not isinstance(group, dict):
            continue
        paths = [p for p in (group.get("paths") or []) if p]
        if not paths:
            continue
        title, why = group.get("title") or "", group.get("why") or ""
        if title or why:
            out.append(f"**{title}**" + (f" - {why}" if why else "") if title else why)
        out += [", ".join(f"`{p}`" for p in paths), ""]
    return out


def render_md(analysis, files, sections=None, walkthrough="", links=None, complexity=None):
    sections = sections or {}
    target = analysis.get("target") or ""
    count, added, removed, net = counts_from(files)
    sign = "+" if net >= 0 else ""
    notes = analysis.get("section_notes") or {}

    # No HTML escaping anywhere below. GitHub's own renderer escapes what it displays, and
    # escaping first produces double-escaped output like "&amp;lt;". Backticks stay backticks,
    # which GitHub renders as inline code.
    facts = (f"**{count} file{'' if count == 1 else 's'} changed, "
             f"+{added} -{removed} (net {sign}{net})**")
    if target:
        facts += f" · `{target}`"
    cx = summary_line(complexity)
    if cx:
        facts += f" · complexity {cx}"
    out = [facts, ""]
    verdict = analysis.get("verdict") or ""
    if verdict:
        out += [verdict, ""]

    for para in _paragraphs(analysis.get("what_changed")):
        out += [para, ""]

    flow = _wrap_flow_labels(_flow_tb((analysis.get("flow_mermaid") or "").strip()))
    if flow:
        out += ["```mermaid", flow, "```", ""]

    how = _paragraphs(analysis.get("how_it_works"))
    if how:
        out.append("### How it works")
        out.append("")
        for para in how:
            out += [para, ""]

    for kind in MD_SECTION_ORDER:
        text = (sections.get(kind) or "").strip()
        if not text:
            continue  # that analysis found nothing to draw
        if notes.get(kind):
            out += [notes[kind], ""]
        out += [text, ""]

    if walkthrough.strip():
        out += [walkthrough.strip(), ""]
    elif analysis.get("groups"):
        out += _reading_order_md(analysis["groups"])

    # The recap never names a local path: every reader of a PR description is on someone else's
    # machine, so a scratch directory means nothing to them and leaks a directory layout.
    if links and links.get("pr_url"):
        out += [f"[Files changed]({links['pr_url']}/files)", ""]

    return "\n".join(out).rstrip("\n") + "\n"


def _read(path):
    return Path(path).read_text(errors="replace") if path else ""


def overflow_warning(text, walkthrough_text):
    """The walkthrough is the only section with a size dial, so it gets a --max-chars
    suggestion when it can actually absorb the overflow. When the suggestion falls below
    walkthrough.py's own stamped floor for this diff, no retry can satisfy it, so say so by
    name instead of handing out a number proven not to work. With no floor to check against
    (older section file, or no walkthrough at all), fall back to MIN_USEFUL_MAX_CHARS."""
    overflow = len(text) - MAX_BODY_CHARS
    suggested = len(walkthrough_text) - overflow - OVERFLOW_MARGIN_CHARS
    prefix = f"warning: recap is {len(text)} characters, over the {MAX_BODY_CHARS} budget; "
    floor_match = _WALKTHROUGH_FLOOR_RE.search(walkthrough_text)
    if floor_match:
        floor = int(floor_match.group(1))
        if suggested < floor:
            return (prefix + "unsatisfiable by shrinking the walkthrough, its floor for this "
                    f"diff is {floor} characters; look at the stacked layers, coupling and "
                    "structure sections instead, which have no size dial")
        return prefix + f"re-run walkthrough.py with --max-chars {suggested}"
    if suggested < MIN_USEFUL_MAX_CHARS:
        # No floor marker: the walkthrough was already near-empty (or --walkthrough was never
        # passed), so the excess is upstream in the other sections instead.
        return (prefix + "the walkthrough section is not where the excess is, so shrinking it "
                "will not help; look at the stacked layers, coupling and structure sections "
                "instead, which have no size dial")
    return prefix + f"re-run walkthrough.py with --max-chars {suggested}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--diff", required=True)
    parser.add_argument("--format", required=True, choices=("md", "html"))
    parser.add_argument("--walkthrough", help="section-walkthrough.{html,md}")
    parser.add_argument("--links")
    parser.add_argument("--complexity", help="complexity.json, for the complexity fact")
    parser.add_argument("--template", help="html only: the page template")
    parser.add_argument("--explorer", help="html only: section-explorer.html")
    parser.add_argument("--symbols", help="html only: section-symbols.html")
    parser.add_argument("--state", help="html only: state.json, inlined behind HR_STATE")
    parser.add_argument("--title", help="html only")
    for kind in MD_SECTION_ORDER:
        parser.add_argument(f"--{kind}", help=f"md only: section-{kind}.md")
    args = parser.parse_args()

    analysis = json.loads(Path(args.analysis).read_text())
    _order, files = parse_hunks(Path(args.diff).read_text(errors="replace"))
    links = json.loads(Path(args.links).read_text()) if args.links else None
    complexity = json.loads(Path(args.complexity).read_text()) if args.complexity else None
    state = json.loads(Path(args.state).read_text()) if args.state else None

    if args.format == "html":
        if not args.template:
            parser.error("--template is required for --format html")
        sys.stdout.write(render_html(
            analysis, files, _read(args.template), _read(args.explorer),
            _read(args.walkthrough), links, args.title, symbols=_read(args.symbols),
            complexity=complexity, state=state,
        ))
    else:
        sections = {kind: _read(getattr(args, kind)) for kind in MD_SECTION_ORDER}
        walkthrough_text = _read(args.walkthrough)
        text = render_md(analysis, files, sections, walkthrough_text, links, complexity)
        if len(text) > MAX_BODY_CHARS:
            print(overflow_warning(text, walkthrough_text), file=sys.stderr)
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
