#!/usr/bin/env python3
"""Self-check for render.py. Assert-based, no framework."""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from render import (  # noqa: E402
    render_html, counts_from, _flow_tb, _wrap_flow_labels,
    CONTENT_PLACEHOLDER, TITLE_PLACEHOLDER,
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


def test_facts_come_from_the_diff_not_the_analysis():
    _order, files = parsed()

    assert counts_from(files) == (2, 2, 1, 1)
    assert "2 files, +2 -1 (net +1)" in html()


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


def test_pasted_sections_are_never_re_escaped():
    # sections.py already escaped these. Escaping again renders "&amp;lt;" on the page, and
    # re-indenting or re-wording them is how a legend drifts from its arrows.
    walk = '<pre class="diff"><span class="c">  x &lt; y</span></pre>'
    out = html(walkthrough=walk)

    assert walk in out
    assert "&amp;lt;" not in out


def test_symbols_pastes_verbatim_with_no_heading_added():
    # Mirrors the explorer paste: section-symbols.html carries its own <h2>CHANGES VISUALIZATION</h2>,
    # so render.py adds nothing around it.
    symbols = '<h2>CHANGES VISUALIZATION</h2>\n<div class="vd-symbols"></div>'
    out = html(symbols=symbols)

    assert symbols in out
    assert out.count("CHANGES VISUALIZATION") == 1


def test_an_empty_flow_drops_the_whole_section():
    out = html({**ANALYSIS, "flow_mermaid": ""})

    assert "<h2>Flow</h2>" not in out
    assert "pre class=\"mermaid\"" not in out
    assert "svgbox-flow" not in out


def test_flow_box_carries_a_distinct_class_from_the_capped_stacked_diagrams():
    # svgbox-flow is what lets the template give FLOW its own CSS rule instead of sharing the
    # scale-to-fit one sized for the symbols graph sections.py pastes in below it.
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


def test_html_rewrites_a_flowchart_lr_analysis_to_tb():
    lr = {**ANALYSIS, "flow_mermaid": 'flowchart LR\n  A["load()"] --> B["refresh()"]'}

    html_out = html(lr)

    assert "flowchart TB" in html_out and "flowchart LR" not in html_out


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


def test_flow_diagram_wraps_a_long_label_end_to_end():
    long_flow = {**ANALYSIS, "flow_mermaid":
                 'flowchart LR\n  A["short"] --> B["NewRequest sets Request User Id to usrID '
                 'for the MediaGuard lookup"]'}

    html_out = html(long_flow)

    # The HTML source escapes the diagram like any other prose; the browser's own textContent
    # decoding turns &lt;br/&gt; back into a real <br/> before Mermaid ever parses it.
    assert "&lt;br/&gt;" in html_out
    assert "MediaGuard lookup" in html_out


def test_an_empty_how_it_works_drops_the_whole_section():
    out = html({**ANALYSIS, "how_it_works": ""})

    assert "<h2>How it works</h2>" not in out


def test_an_empty_section_file_inserts_nothing_not_even_its_note():
    out = html(walkthrough="")

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


def test_no_links_json_means_no_links():
    assert "pull request" not in html()


def test_html_footer_link_is_safe_to_click_from_a_file_url():
    out = html(links={"pr_url": "https://gh/o/r/pull/7"})

    # The page is opened from file:// and a link that replaces it costs the reader their notes.
    assert 'target="_blank" rel="noopener noreferrer"' in out


if __name__ == "__main__":
    tests = [
        test_facts_come_from_the_diff_not_the_analysis,
        test_a_net_deletion_reads_as_negative,
        test_both_placeholders_are_replaced,
        test_an_explicit_title_wins_even_with_no_target,
        test_html_escapes_prose_before_promoting_backticks,
        test_html_promotes_backticks_in_every_prose_field,
        test_html_escapes_the_mermaid_source,
        test_pasted_sections_are_never_re_escaped,
        test_symbols_pastes_verbatim_with_no_heading_added,
        test_an_empty_flow_drops_the_whole_section,
        test_flow_box_carries_a_distinct_class_from_the_capped_stacked_diagrams,
        test_flow_tb_leaves_a_non_lr_diagram_alone,
        test_flow_tb_rewrites_only_the_leading_lr_token,
        test_html_rewrites_a_flowchart_lr_analysis_to_tb,
        test_wrap_flow_labels_breaks_a_long_label_at_word_boundaries,
        test_wrap_flow_labels_breaks_a_long_identifier_at_camel_boundaries,
        test_wrap_flow_labels_leaves_ids_arrows_and_edge_counts_alone,
        test_wrap_flow_labels_wraps_a_long_quoted_edge_label,
        test_wrap_flow_labels_wraps_a_long_bare_edge_label,
        test_wrap_flow_labels_wraps_a_node_and_an_edge_label_on_the_same_line,
        test_flow_diagram_wraps_a_long_label_end_to_end,
        test_an_empty_how_it_works_drops_the_whole_section,
        test_an_empty_section_file_inserts_nothing_not_even_its_note,
        test_no_text_size_control_is_emitted_at_all,
        test_the_walkthrough_heading_carries_only_the_explanations_toggle,
        test_a_missing_verdict_drops_only_its_fact_block,
        test_prose_paragraphs_stay_separate,
        test_footer_carries_the_target_and_a_stamp_but_no_local_path,
        test_no_links_json_means_no_links,
        test_html_footer_link_is_safe_to_click_from_a_file_url,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
