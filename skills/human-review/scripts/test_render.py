#!/usr/bin/env python3
"""Self-check for render.py. Assert-based, no framework."""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from render import (  # noqa: E402
    render_html, render_md, counts_from, overflow_warning, _flow_tb, _wrap_flow_labels,
    CONTENT_PLACEHOLDER, TITLE_PLACEHOLDER, MAX_BODY_CHARS, MIN_USEFUL_MAX_CHARS,
    OVERFLOW_MARGIN_CHARS,
)
from validate_analysis import parse_hunks  # noqa: E402

DIFF = """diff --git a/src/auth.py b/src/auth.py
--- a/src/auth.py
+++ b/src/auth.py
@@ -40,2 +40,3 @@ def load_session():
     token = store.get("access")
+    if not token: return refresh()
     return parse(token)
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,1 +1,1 @@
-# old
+# new
"""

ANALYSIS = {
    "target": "master...HEAD",
    "verdict": "Session loading gained a refresh fallback.",
    "what_changed": "`loadSession` now falls back to the refresh token.\n\nThe title changed.",
    "how_it_works": "`store.get` reads the access token from the cookie jar.",
    "flow_mermaid": 'flowchart LR\n  A["load()"] --> B["refresh()"]',
    "section_notes": {"explorer": "Start at files, the change is in one module.",
                      "layers": "Layers note.", "coupling": "Coupling note.",
                      "structure": "Structure note."},
    "files": [{"path": "src/auth.py", "role": "r", "hunks": [{"header": "@@", "note": "n"}]}],
}

TEMPLATE = f"<html><title>{TITLE_PLACEHOLDER}</title><body>{CONTENT_PLACEHOLDER}</body></html>"
FROZEN = datetime(2026, 9, 11, 18, 15)


def parsed():
    return parse_hunks(DIFF)


def html(analysis=None, **kw):
    _order, files = parsed()
    kw.setdefault("now", FROZEN)
    return render_html(analysis or ANALYSIS, files, TEMPLATE, **kw)


def md(analysis=None, **kw):
    _order, files = parsed()
    return render_md(analysis or ANALYSIS, files, **kw)


def test_facts_come_from_the_diff_not_the_analysis():
    _order, files = parsed()

    assert counts_from(files) == (2, 2, 1, 1)
    assert "2 files, +2 -1 (net +1)" in html()
    assert "**2 files changed, +2 -1 (net +1)**" in md()


def test_a_net_deletion_reads_as_negative():
    diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,1 @@\n-a\n-b\n-c\n+d\n")
    _order, files = parse_hunks(diff)

    assert counts_from(files) == (1, 1, 3, -2)
    assert "1 file, +1 -3 (net -2)" in render_html(ANALYSIS, files, TEMPLATE, now=FROZEN)


def test_both_placeholders_are_replaced():
    out = html()

    assert TITLE_PLACEHOLDER not in out and CONTENT_PLACEHOLDER not in out
    assert "<title>Visual diff: master...HEAD</title>" in out


def test_an_explicit_title_wins_even_with_no_target():
    out = html({**ANALYSIS, "target": ""}, title="My page")

    assert "<title>My page</title>" in out


def test_html_escapes_prose_before_promoting_backticks():
    # A branch name or commit subject from someone else's PR reaches this prose. Escaping after
    # wrapping would let `<img src=x onerror=alert(1)>` become a live element the moment the
    # browser parses the page, before any script runs.
    hostile = {**ANALYSIS, "what_changed": "broke `<img src=x onerror=alert(1)>` here"}
    out = html(hostile)

    assert "<img src=x" not in out
    assert "&lt;img src=x onerror=alert(1)&gt;" in out
    # still marked up as code, just safely
    assert "<code>&lt;img src=x onerror=alert(1)&gt;</code>" in out


def test_html_promotes_backticks_in_every_prose_field():
    out = html()

    assert "<code>loadSession</code>" in out
    assert "<code>store.get</code>" in out
    assert "`" not in out.split("<body>")[1]


def test_html_escapes_the_mermaid_source():
    flow = {**ANALYSIS, "flow_mermaid": 'flowchart LR\n  A["a < b && c"] --> B'}
    out = html(flow)

    assert "a &lt; b &amp;&amp; c" in out
    assert "a < b && c" not in out


def test_markdown_never_html_escapes_anything():
    flow = {**ANALYSIS, "flow_mermaid": 'flowchart LR\n  A["a < b && c"] --> B'}
    out = md(flow)

    # GitHub escapes what it displays; doing it here gives "&amp;lt;".
    assert "a < b && c" in out
    assert "&lt;" not in out and "&amp;" not in out


def test_markdown_keeps_backticks_as_backticks():
    out = md()

    assert "`loadSession`" in out
    assert "<code>" not in out


def test_pasted_sections_are_never_re_escaped():
    # sections.py already escaped these. Escaping again renders "&amp;lt;" on the page, and
    # re-indenting or re-wording them is how a legend drifts from its arrows.
    explorer = '<div class="vd-explorer">a &lt; b &amp;&amp; c</div>'
    walk = '<pre class="diff"><span class="c">  x &lt; y</span></pre>'
    out = html(explorer=explorer, walkthrough=walk)

    assert explorer in out
    assert walk in out
    assert "&amp;lt;" not in out


def test_markdown_pastes_its_sections_byte_for_byte():
    sections = {"layers": "<!-- visual-diff:layers -->\n### Layers\n```mermaid\nflowchart LR\n```",
                "coupling": "<!-- visual-diff:coupling -->\n### Files",
                "structure": "<!-- visual-diff:structure -->\n### Symbols"}
    out = md(sections=sections, walkthrough="<!-- visual-diff:walkthrough -->\n<details>x</details>")

    for text in list(sections.values()) + ["<!-- visual-diff:walkthrough -->\n<details>x</details>"]:
        assert text in out


def test_markdown_stacks_the_graphs_widest_first():
    sections = {k: f"<!-- visual-diff:{k} -->" for k in ("layers", "coupling", "structure")}
    out = md(sections=sections)

    order = [out.index(f"<!-- visual-diff:{k} -->") for k in ("layers", "coupling", "structure")]
    assert order == sorted(order)


def test_each_section_note_sits_above_its_own_section():
    sections = {k: f"<!-- visual-diff:{k} -->" for k in ("layers", "coupling", "structure")}
    out = md(sections=sections)

    for kind, note in (("layers", "Layers note."), ("coupling", "Coupling note."),
                       ("structure", "Structure note.")):
        assert out.index(note) < out.index(f"<!-- visual-diff:{kind} -->")


def test_relations_note_sits_above_the_explorer_and_no_heading_is_added():
    # section-explorer.html opens with its own <h2>RELATIONS</h2>; adding one here put two
    # headings in a row on a real page.
    explorer = '<h2>RELATIONS</h2>\n<div class="vd-explorer"></div>'
    out = html(explorer=explorer)

    assert out.index("Start at files") < out.index("<h2>RELATIONS</h2>")
    assert out.count("RELATIONS") == 1
    assert "<h2>Relations</h2>" not in out


def test_symbols_pastes_verbatim_with_no_heading_added():
    # Mirrors the explorer paste: section-symbols.html carries its own <h2>CHANGES VISUALIZATION</h2>,
    # so render.py adds nothing around it.
    symbols = '<h2>CHANGES VISUALIZATION</h2>\n<div class="vd-symbols"></div>'
    out = html(symbols=symbols)

    assert symbols in out
    assert out.count("CHANGES VISUALIZATION") == 1


def test_explorer_and_symbols_both_appear_on_one_page():
    explorer = '<h2>RELATIONS</h2>\n<div class="vd-explorer"></div>'
    symbols = '<h2>CHANGES VISUALIZATION</h2>\n<div class="vd-symbols"></div>'
    out = html(explorer=explorer, symbols=symbols)

    assert explorer in out
    assert symbols in out


def test_an_empty_flow_drops_the_whole_section():
    out = html({**ANALYSIS, "flow_mermaid": ""})

    assert "<h2>Flow</h2>" not in out
    assert "pre class=\"mermaid\"" not in out
    assert "svgbox-flow" not in out


def test_flow_box_carries_a_distinct_class_from_the_capped_stacked_diagrams():
    # svgbox-flow is what lets the template give FLOW its own CSS rule instead of sharing the
    # scale-to-fit one sized for the whole-repo module map sections.py pastes in below it.
    out = html()

    assert 'class="panel svgbox svgbox-flow"' in out


def test_flow_tb_leaves_a_non_lr_diagram_alone():
    assert _flow_tb('flowchart TB\n  A --> B') == 'flowchart TB\n  A --> B'
    assert _flow_tb('') == ''


def test_flow_tb_rewrites_only_the_leading_lr_token():
    # Slicing on len("flowchart LR") rather than a blind replace: an LR appearing again later
    # in the diagram (a node label, say) must survive untouched.
    out = _flow_tb('flowchart LR\n  A["go LR"] --> B')
    assert out == 'flowchart TB\n  A["go LR"] --> B'


def test_html_and_markdown_both_rewrite_a_flowchart_lr_analysis_to_tb():
    lr = {**ANALYSIS, "flow_mermaid": 'flowchart LR\n  A["load()"] --> B["refresh()"]'}

    html_out = html(lr)
    md_out = md(lr)

    assert "flowchart TB" in html_out and "flowchart LR" not in html_out
    assert "flowchart TB" in md_out and "flowchart LR" not in md_out


def test_wrap_flow_labels_breaks_a_long_label_at_word_boundaries():
    flow = ('flowchart TB\n  A["short"] --> B["NewRequest sets Request User Id to usrID '
            'for the MediaGuard lookup"]')

    out = _wrap_flow_labels(flow)

    assert "<br/>" in out
    assert "MediaGuard lookup" in out  # never split across the break
    label = out.split('B["', 1)[1].rsplit('"]', 1)[0]
    assert " ".join(label.split("<br/>")) == (
        "NewRequest sets Request User Id to usrID for the MediaGuard lookup")


def test_wrap_flow_labels_breaks_a_long_identifier_at_camel_boundaries():
    flow = 'flowchart TB\n  A["filterUnusableIdentifierThatIsVeryLongIndeedYes"] --> B'

    out = _wrap_flow_labels(flow)

    label = out.split('A["', 1)[1].rsplit('"]', 1)[0]
    assert "<br/>" in label
    assert "".join(label.split("<br/>")) == "filterUnusableIdentifierThatIsVeryLongIndeedYes"


def test_wrap_flow_labels_leaves_ids_arrows_and_edge_counts_alone():
    flow = 'flowchart TB\n  A["one two three four five six seven eight"] -->|1| B["ok"]'

    out = _wrap_flow_labels(flow)

    assert out.count("-->") == 1
    assert "|1|" in out
    assert 'B["ok"]' in out  # short label untouched


def test_wrap_flow_labels_wraps_a_long_quoted_edge_label():
    flow = 'flowchart TB\n  A["short"] -->|"cache miss on the primary lookup index"| B["ok"]'

    out = _wrap_flow_labels(flow)

    label = out.split('-->|"', 1)[1].split('"|', 1)[0]
    assert "<br/>" in label
    assert " ".join(label.split("<br/>")) == "cache miss on the primary lookup index"


def test_wrap_flow_labels_wraps_a_long_bare_edge_label():
    flow = 'flowchart TB\n  A["short"] -->|cache miss on the primary lookup index| B["ok"]'

    out = _wrap_flow_labels(flow)

    label = out.split('-->|', 1)[1].split('|', 1)[0]
    assert "<br/>" in label
    assert " ".join(label.split("<br/>")) == "cache miss on the primary lookup index"


def test_wrap_flow_labels_wraps_a_node_and_an_edge_label_on_the_same_line():
    flow = ('flowchart TB\n  A["NewRequest sets Request User Id to usrID for the lookup"] '
            '-->|"cache miss on the primary lookup index"| B["ok"]')

    out = _wrap_flow_labels(flow)

    node_label = out.split('A["', 1)[1].split('"]', 1)[0]
    edge_label = out.split('-->|"', 1)[1].split('"|', 1)[0]
    assert "<br/>" in node_label
    assert "<br/>" in edge_label


def test_flow_diagram_wraps_a_long_label_end_to_end_in_html_and_markdown():
    long_flow = {**ANALYSIS, "flow_mermaid":
                 'flowchart LR\n  A["short"] --> B["NewRequest sets Request User Id to usrID '
                 'for the MediaGuard lookup"]'}

    html_out = html(long_flow)
    md_out = md(long_flow)

    # The HTML source escapes the diagram like any other prose; the browser's own textContent
    # decoding turns &lt;br/&gt; back into a real <br/> before Mermaid ever parses it.
    assert "&lt;br/&gt;" in html_out
    assert "MediaGuard lookup" in html_out
    assert "<br/>" in md_out  # markdown pastes the fenced diagram verbatim, unescaped
    assert "MediaGuard lookup" in md_out


def test_an_empty_how_it_works_drops_the_whole_section():
    out = html({**ANALYSIS, "how_it_works": ""})

    assert "<h2>How it works</h2>" not in out
    assert "### How it works" not in md({**ANALYSIS, "how_it_works": ""})


def test_an_empty_section_file_inserts_nothing_not_even_its_note():
    out = html(explorer="", walkthrough="")

    assert "Start at files" not in out  # the note has no section to introduce
    assert "<h2>Walkthrough</h2>" not in out


def test_no_text_size_control_is_emitted_at_all():
    # The A-/A/A+ row is gone: pinch-zoom is the escape hatch, and the viewport meta allows it.
    out = html()

    assert "txtsize" not in out


def test_the_walkthrough_heading_carries_only_the_explanations_toggle():
    wt = "<!-- visual-diff:walkthrough -->\n<details>x</details>"
    out = html(walkthrough=wt, links={"pr_url": "https://gh/pull/7"})

    assert 'class="h2-row"' in out
    assert out.count('id="wt-notes-toggle"') == 1
    # The label names the state, so aria-pressed would say something different from the word
    # in the button; the accessible name carries the state and the action instead.
    assert "aria-pressed" not in out
    assert 'aria-label="Explanations shown. Activate to hide them."' in out
    assert ">explanations</button>" in out  # on by default, so this is the on label
    heading = out.split('class="h2-row"')[1].split("</h2>")[0]
    assert "<a " not in heading


def test_no_walkthrough_means_no_heading_and_no_toggle():
    out = html(walkthrough="")

    assert "Walkthrough" not in out
    assert "wt-notes-toggle" not in out


def test_a_missing_verdict_drops_only_its_fact_block():
    out = html({k: v for k, v in ANALYSIS.items() if k != "verdict"})

    assert "<b>Verdict</b>" not in out
    assert out.count('class="fact"') == 2  # changed and target survive


def test_prose_paragraphs_stay_separate():
    out = html()

    # what_changed holds two paragraphs separated by a blank line; one <p> would lose the break.
    assert "<p><code>loadSession</code> now falls back to the refresh token.</p>" in out
    assert "<p>The title changed.</p>" in out


def test_footer_carries_the_target_and_a_stamp_but_no_local_path():
    out = html()

    assert 'class="foot"' in out
    assert "master...HEAD · generated 2026-09-11 18:15" in out


def test_markdown_names_no_local_path_and_links_only_into_the_pr():
    out = md(links={"pr_url": "https://gh/o/r/pull/7", "head_pushed": True})

    assert "[Files changed](https://gh/o/r/pull/7/files)" in out
    for leak in ("/tmp/", "scratchpad", "raw.diff", ".html"):
        assert leak not in out


def test_no_links_json_means_no_links():
    assert "http" not in md()
    assert "pull request" not in html()


def test_html_footer_link_is_safe_to_click_from_a_file_url():
    out = html(links={"pr_url": "https://gh/o/r/pull/7"})

    # The page is opened from file:// and a link that replaces it costs the reader their notes.
    assert 'target="_blank" rel="noopener noreferrer"' in out


def test_a_full_recap_of_a_big_diff_fits_the_body_budget():
    # The two budgets have to add up: walkthrough.DEFAULT_MAX_CHARS plus the graph sections and
    # prose must land under render.MAX_BODY_CHARS, or a big diff gets refused by GitHub outright.
    import walkthrough

    diff = "".join(
        f"diff --git a/pkg/mod{i}.py b/pkg/mod{i}.py\n--- a/pkg/mod{i}.py\n"
        f"+++ b/pkg/mod{i}.py\n@@ -1,1 +1,4 @@\n+a\n+b\n+c\n"
        for i in range(120)
    )
    order, files = parse_hunks(diff)
    by_path = {p: {"path": p, "role": f"What `{p}` is for, in one line.",
                   "hunks": [{"header": "@@ -1,1 +1,4 @@", "note": f"One line on `{p}`."}]}
               for p in order}
    walk = walkthrough.render_md(walkthrough.story(order, files, None, None), files, by_path)
    # Graph sections measured at 6250 characters on a real branch; stand in for that.
    sections = {k: f"<!-- visual-diff:{k} -->\n" + "x" * 2000 for k in ("layers", "coupling",
                                                                       "structure")}
    out = render_md(ANALYSIS, files, sections, walk)

    assert len(walk) <= walkthrough.DEFAULT_MAX_CHARS
    assert len(out) <= MAX_BODY_CHARS, len(out)


def test_section_notes_absent_is_not_an_error():
    analysis = {k: v for k, v in ANALYSIS.items() if k != "section_notes"}
    out = html(analysis, explorer='<h2>RELATIONS</h2><div class="vd-explorer"></div>')

    assert "<h2>RELATIONS</h2>" in out
    assert 'class="vd-explorer"' in out


def test_overflow_warning_names_a_concrete_max_chars_below_the_walkthrough_length():
    walkthrough_len = 12000
    overflow = 2264
    walkthrough_text = "x" * walkthrough_len
    text = "x" * (MAX_BODY_CHARS + overflow)

    warning = overflow_warning(text, walkthrough_text)

    assert str(len(text)) in warning
    assert str(MAX_BODY_CHARS) in warning
    suggested = int(warning.rsplit("--max-chars ", 1)[1])
    # Pinned, not just bounded: a bound like `suggested < walkthrough_len` would still pass if
    # OVERFLOW_MARGIN_CHARS regressed toward 0 and reintroduced the multi-retry problem this
    # fix exists to solve.
    assert suggested == walkthrough_len - overflow - OVERFLOW_MARGIN_CHARS
    assert suggested < len(walkthrough_text)


def test_overflow_warning_declines_to_suggest_when_the_walkthrough_cannot_absorb_it():
    # Both reproduced live before this fix: the floor used to hand out `--max-chars 1`, advice
    # that provably cannot work since the recap is already tens of thousands of characters over
    # while the walkthrough itself is empty or nearly so. No --walkthrough passed is the same
    # case: the overflow can't be coming from a section that isn't there.
    for text, walkthrough_text in (
        ("y" * (MAX_BODY_CHARS + 5000), ""), ("y" * (MAX_BODY_CHARS + 3000), "x" * 800),
    ):
        warning = overflow_warning(text, walkthrough_text)

        assert str(len(text)) in warning
        assert str(MAX_BODY_CHARS) in warning
        assert "--max-chars" not in warning
        assert "layers" in warning and "coupling" in warning and "structure" in warning


def test_overflow_warning_boundary_around_the_min_useful_threshold():
    # walkthrough_text sized so the unfloored suggestion lands exactly on MIN_USEFUL_MAX_CHARS
    # on one side and one character short of it on the other, isolating the boundary itself
    # from the rest of the arithmetic.
    overflow = 500
    text = "x" * (MAX_BODY_CHARS + overflow)
    base = MIN_USEFUL_MAX_CHARS + overflow + OVERFLOW_MARGIN_CHARS

    at_threshold = overflow_warning(text, "x" * base)
    below_threshold = overflow_warning(text, "x" * (base - 1))

    assert f"--max-chars {MIN_USEFUL_MAX_CHARS}" in at_threshold
    assert "--max-chars" not in below_threshold


def test_overflow_warning_names_the_floor_when_the_marker_makes_it_unsatisfiable():
    walkthrough_text = ("<!-- visual-diff:walkthrough -->\n"
                        "<!-- visual-diff:walkthrough-floor 8042 -->\n" + "x" * 8000)
    text = "x" * (MAX_BODY_CHARS + 50000)  # overflow far too large for any floor to absorb

    warning = overflow_warning(text, walkthrough_text)

    assert "unsatisfiable" in warning
    assert "8042 characters" in warning
    assert "--max-chars" not in warning
    assert "layers" in warning and "coupling" in warning and "structure" in warning


def test_overflow_warning_still_names_a_number_when_the_floor_marker_says_it_fits():
    floor = 2000
    walkthrough_text = ("<!-- visual-diff:walkthrough -->\n"
                        f"<!-- visual-diff:walkthrough-floor {floor} -->\n" + "x" * 12000)
    text = "x" * (MAX_BODY_CHARS + 500)

    warning = overflow_warning(text, walkthrough_text)

    assert "unsatisfiable" not in warning
    suggested = int(warning.rsplit("--max-chars ", 1)[1])
    assert suggested >= floor


def test_overflow_warning_round_trips_the_real_floor_walkthrough_py_computed():
    import walkthrough

    diff = "".join(
        f"diff --git a/pkg/mod{i}.py b/pkg/mod{i}.py\n--- a/pkg/mod{i}.py\n"
        f"+++ b/pkg/mod{i}.py\n@@ -1,1 +1,4 @@\n+a\n+b\n+c\n"
        for i in range(120)
    )
    order, files = parse_hunks(diff)
    by_path = {p: {"path": p, "role": f"What `{p}` is for, in one line.",
                   "hunks": [{"header": "@@ -1,1 +1,4 @@", "note": f"One line on `{p}`."}]}
               for p in order}
    groups = walkthrough.story(order, files, None, None)
    # max_chars=1 forces every file to demote, so this text's length is exactly its own floor
    # plus the marker line -- the real number overflow_warning has to read back.
    floored = walkthrough.render_md(groups, files, by_path, max_chars=1)
    real_floor = int(floored.splitlines()[1].split(" ")[2])
    text = "x" * (MAX_BODY_CHARS + 50000)

    warning = overflow_warning(text, floored)

    assert f"{real_floor} characters" in warning
    assert "unsatisfiable" in warning


def test_reading_order_section_appears_only_when_there_is_no_walkthrough():
    groups = [{"title": "Auth", "why": "core of the change", "paths": ["src/auth.py"]},
              {"title": "Docs", "paths": ["README.md"]}]
    analysis = {**ANALYSIS, "groups": groups}

    no_walk = md(analysis)
    with_walk = md(analysis, walkthrough="<!-- visual-diff:walkthrough -->\n<details>x</details>")

    assert "### Reading order" in no_walk
    assert "### Reading order" not in with_walk


def test_reading_order_section_lists_every_group_path_as_inline_code():
    groups = [{"title": "Auth", "why": "core of the change", "paths": ["src/auth.py"]},
              {"title": "Docs", "paths": ["README.md", "docs/x.md"]}]
    analysis = {**ANALYSIS, "groups": groups}

    out = md(analysis)

    assert "**Auth** - core of the change" in out
    assert "`src/auth.py`" in out
    assert "**Docs**" in out
    assert "`README.md`" in out and "`docs/x.md`" in out


def test_no_groups_and_no_walkthrough_means_no_reading_order_section():
    out = md()  # ANALYSIS has no "groups" key, and md() passes no walkthrough

    assert "### Reading order" not in out


if __name__ == "__main__":
    tests = [
        test_facts_come_from_the_diff_not_the_analysis,
        test_a_net_deletion_reads_as_negative,
        test_both_placeholders_are_replaced,
        test_an_explicit_title_wins_even_with_no_target,
        test_html_escapes_prose_before_promoting_backticks,
        test_html_promotes_backticks_in_every_prose_field,
        test_html_escapes_the_mermaid_source,
        test_markdown_never_html_escapes_anything,
        test_markdown_keeps_backticks_as_backticks,
        test_pasted_sections_are_never_re_escaped,
        test_markdown_pastes_its_sections_byte_for_byte,
        test_markdown_stacks_the_graphs_widest_first,
        test_each_section_note_sits_above_its_own_section,
        test_relations_note_sits_above_the_explorer_and_no_heading_is_added,
        test_symbols_pastes_verbatim_with_no_heading_added,
        test_explorer_and_symbols_both_appear_on_one_page,
        test_an_empty_flow_drops_the_whole_section,
        test_flow_box_carries_a_distinct_class_from_the_capped_stacked_diagrams,
        test_flow_tb_leaves_a_non_lr_diagram_alone,
        test_flow_tb_rewrites_only_the_leading_lr_token,
        test_html_and_markdown_both_rewrite_a_flowchart_lr_analysis_to_tb,
        test_wrap_flow_labels_breaks_a_long_label_at_word_boundaries,
        test_wrap_flow_labels_breaks_a_long_identifier_at_camel_boundaries,
        test_wrap_flow_labels_leaves_ids_arrows_and_edge_counts_alone,
        test_wrap_flow_labels_wraps_a_long_quoted_edge_label,
        test_wrap_flow_labels_wraps_a_long_bare_edge_label,
        test_wrap_flow_labels_wraps_a_node_and_an_edge_label_on_the_same_line,
        test_flow_diagram_wraps_a_long_label_end_to_end_in_html_and_markdown,
        test_an_empty_how_it_works_drops_the_whole_section,
        test_an_empty_section_file_inserts_nothing_not_even_its_note,
        test_no_text_size_control_is_emitted_at_all,
        test_the_walkthrough_heading_carries_only_the_explanations_toggle,
        test_a_missing_verdict_drops_only_its_fact_block,
        test_prose_paragraphs_stay_separate,
        test_footer_carries_the_target_and_a_stamp_but_no_local_path,
        test_markdown_names_no_local_path_and_links_only_into_the_pr,
        test_no_links_json_means_no_links,
        test_html_footer_link_is_safe_to_click_from_a_file_url,
        test_a_full_recap_of_a_big_diff_fits_the_body_budget,
        test_section_notes_absent_is_not_an_error,
        test_overflow_warning_names_a_concrete_max_chars_below_the_walkthrough_length,
        test_overflow_warning_declines_to_suggest_when_the_walkthrough_cannot_absorb_it,
        test_overflow_warning_boundary_around_the_min_useful_threshold,
        test_overflow_warning_names_the_floor_when_the_marker_makes_it_unsatisfiable,
        test_overflow_warning_still_names_a_number_when_the_floor_marker_says_it_fits,
        test_overflow_warning_round_trips_the_real_floor_walkthrough_py_computed,
        test_reading_order_section_appears_only_when_there_is_no_walkthrough,
        test_reading_order_section_lists_every_group_path_as_inline_code,
        test_no_groups_and_no_walkthrough_means_no_reading_order_section,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
