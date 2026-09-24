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
from sections import ZWSP  # noqa: E402
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
    "overview": ("`loadSession` now falls back to the refresh token.\n\nThe title changed."
                "\n\n`store.get` reads the access token from the cookie jar."),
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
    assert "<title>Code walkthrough: master...HEAD</title>" in out


def test_an_explicit_title_wins_even_with_no_target():
    out = html({**ANALYSIS, "target": ""}, title="My page")

    assert "<title>My page</title>" in out


def test_html_escapes_prose_before_promoting_backticks():
    # A branch name or commit subject from someone else's PR reaches this prose. Escaping after
    # wrapping would let `<img src=x onerror=alert(1)>` become a live element the moment the
    # browser parses the page, before any script runs.
    hostile = {**ANALYSIS, "overview": "broke `<img src=x onerror=alert(1)>` here"}
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


def test_symbols_pastes_verbatim_inside_a_collapsed_details_at_the_end():
    # Mirrors the explorer paste: section-symbols.html carries its own <h2>CHANGES
    # VISUALIZATION</h2> and its own marker comment, so render.py adds nothing around the
    # content itself -- only the wrapping <details>, and only at the very end of the page.
    symbols = ('<!-- code-walkthrough:symbols -->\n<h2>CHANGES VISUALIZATION</h2>\n'
               '<div class="vd-symbols"></div>')
    out = html(symbols=symbols,
               walkthrough='<!-- code-walkthrough:walkthrough -->\n<p>wt-body</p>')

    assert symbols in out
    assert out.count("CHANGES VISUALIZATION") == 1
    assert '<details class="collapse">' in out
    assert "<summary>Package graph</summary>" in out
    # validate_analysis.py's --sections gate looks for this marker in the rendered file.
    assert "<!-- code-walkthrough:symbols -->" in out
    assert out.index("wt-body") < out.index('<details class="collapse">')
    assert out.index('<details class="collapse">') < out.index('class="foot"')


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


def test_wrap_flow_labels_leaves_a_long_multiword_label_alone():
    # mermaid wraps on spaces by itself, so a label with no over-length word comes back
    # byte for byte (see sections.wrap_label).
    flow = ('flowchart TB\n  A["short"] --> B["NewRequest sets Request User Id to usrID '
            'for the MediaGuard lookup"]')

    assert _wrap_flow_labels(flow) == flow


def test_wrap_flow_labels_breaks_a_long_identifier_at_camel_boundaries():
    flow = 'flowchart TB\n  A["filterUnusableIdentifierThatIsVeryLongIndeedYes"] --> B'

    out = _wrap_flow_labels(flow)

    label = out.split('A["', 1)[1].rsplit('"]', 1)[0]
    assert ZWSP in label
    assert label.replace(ZWSP, "") == "filterUnusableIdentifierThatIsVeryLongIndeedYes"


def test_wrap_flow_labels_leaves_ids_arrows_and_edge_counts_alone():
    flow = 'flowchart TB\n  A["one two three four five six seven eight"] -->|1| B["ok"]'

    out = _wrap_flow_labels(flow)

    assert out.count("-->") == 1
    assert "|1|" in out
    assert 'B["ok"]' in out  # short label untouched


def test_wrap_flow_labels_breaks_a_long_identifier_in_a_quoted_edge_label():
    flow = 'flowchart TB\n  A["short"] -->|"via resolveSymbolMergesAcrossPackages"| B["ok"]'

    out = _wrap_flow_labels(flow)

    label = out.split('-->|"', 1)[1].split('"|', 1)[0]
    assert ZWSP in label
    assert label.replace(ZWSP, "") == "via resolveSymbolMergesAcrossPackages"


def test_wrap_flow_labels_breaks_a_long_identifier_in_a_bare_edge_label():
    flow = 'flowchart TB\n  A["short"] -->|via resolveSymbolMergesAcrossPackages| B["ok"]'

    out = _wrap_flow_labels(flow)

    label = out.split('-->|', 1)[1].split('|', 1)[0]
    assert ZWSP in label
    assert label.replace(ZWSP, "") == "via resolveSymbolMergesAcrossPackages"


def test_wrap_flow_labels_reaches_a_node_and_an_edge_label_on_the_same_line():
    flow = ('flowchart TB\n  A["filterUnusableIdentifierThatIsVeryLong"] '
            '-->|"via resolveSymbolMergesAcrossPackages"| B["ok"]')

    out = _wrap_flow_labels(flow)

    node_label = out.split('A["', 1)[1].split('"]', 1)[0]
    edge_label = out.split('-->|"', 1)[1].split('"|', 1)[0]
    assert ZWSP in node_label
    assert ZWSP in edge_label


def test_flow_diagram_breaks_a_long_identifier_end_to_end():
    long_flow = {**ANALYSIS, "flow_mermaid":
                 'flowchart LR\n  A["short"] --> B["filterUnusableIdentifierThatIsVeryLong"]'}

    html_out = html(long_flow)

    # A zero-width space needs no HTML escaping, so it reaches the page as itself -- unlike the
    # <br/> this used to emit, which mermaid's sanitiser deleted (see sections.wrap_label).
    assert ZWSP in html_out
    assert "&lt;br" not in html_out


def test_an_empty_overview_drops_the_whole_section():
    out = html({**ANALYSIS, "overview": ""})

    assert "<h2>Overview</h2>" not in out


def test_overview_heading_is_the_same_in_both_modes():
    # The old "What changed" / "What this is" swap is gone: a cold reader needs the same shape
    # of topic whether the target is a change or an area, so one heading covers both.
    assert "<h2>Overview</h2>" in html()
    assert "<h2>Overview</h2>" in html(explain=True)


def test_a_bullet_block_renders_as_a_list():
    out = html({**ANALYSIS, "overview": "Lead sentence.\n\n- first idea\n- second idea"})

    assert "<ul><li>first idea</li><li>second idea</li></ul>" in out
    assert "<p>Lead sentence.</p>" in out


def test_a_lead_with_no_blank_line_before_its_bullets_still_gets_its_own_p():
    # No "\n\n" between the lead and the bullets -- the failure mode a missing blank line
    # used to cause: the lead must stay a <p> and must not become one of the <li>s.
    out = html({**ANALYSIS, "overview": "Lead sentence.\n- first idea\n- second idea"})

    assert "<p>Lead sentence.</p>" in out
    assert "<ul><li>first idea</li><li>second idea</li></ul>" in out
    assert "<li>Lead sentence.</li>" not in out


def test_bullets_split_by_a_blank_line_still_merge_into_one_list():
    out = html({**ANALYSIS, "overview": "Lead sentence.\n\n- first idea\n\n- second idea"})

    assert "<ul><li>first idea</li><li>second idea</li></ul>" in out
    assert out.count("<ul>") == 1


def test_a_mixed_block_treats_plain_lines_as_their_own_items():
    out = html({**ANALYSIS, "overview": "- bulleted\nplain line too"})

    assert "<ul><li>bulleted</li><li>plain line too</li></ul>" in out


def test_escaping_still_happens_inside_a_list_item():
    hostile = {**ANALYSIS, "overview": "- broke `<img src=x onerror=alert(1)>` here"}
    out = html(hostile)

    assert "<img src=x" not in out
    assert "<li>broke <code>&lt;img src=x onerror=alert(1)&gt;</code> here</li>" in out


def test_an_empty_section_file_inserts_nothing_not_even_its_note():
    out = html(walkthrough="")

    assert "<h2>Walkthrough</h2>" not in out


def test_no_text_size_control_is_emitted_at_all():
    # The A-/A/A+ row is gone: pinch-zoom is the escape hatch, and the viewport meta allows it.
    out = html()

    assert "txtsize" not in out


def test_the_walkthrough_heading_carries_only_the_explanations_toggle():
    wt = "<!-- code-walkthrough:walkthrough -->\n<details>x</details>"
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

    # overview holds multiple paragraphs separated by a blank line; one <p> would lose the break.
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


def test_explain_rewords_the_headings_and_drops_the_add_remove_arithmetic():
    out = html(explain=True)

    assert "<b>Scope</b>" in out and "<b>Changed</b>" not in out
    assert "2 files, 2 lines" in out
    assert "(net " not in out
    assert "<h2>Overview</h2>" in out  # same heading as the non-explain page
    assert "<b>Summary</b>" in out and "<b>Verdict</b>" not in out


# ---- story map ----

def test_story_map_replaces_flow_and_lands_right_after_overview():
    order, files = parsed()
    analysis = {**ANALYSIS,
                "groups": [{"title": "Auth", "paths": ["src/auth.py"]},
                           {"title": "Docs", "paths": ["README.md"]}]}
    out = render_html(analysis, files, TEMPLATE, now=FROZEN, order=order)

    assert "<h2>Story map</h2>" in out
    assert "<h2>Flow</h2>" not in out
    assert "svgbox-flow" not in out
    assert '<div class="map">' in out
    assert out.index("<h2>Overview</h2>") < out.index("<h2>Story map</h2>")


def test_structure_section_lands_between_the_story_map_and_the_walkthrough():
    # Mirrors the symbols-section paste test below: render.py adds nothing around
    # section-structure.html's own content, only places it -- between the story map and the
    # walkthrough, per render_html's own ordering.
    order, files = parsed()
    analysis = {**ANALYSIS,
                "groups": [{"title": "Auth", "paths": ["src/auth.py"]},
                           {"title": "Docs", "paths": ["README.md"]}]}
    structure = '<!-- code-walkthrough:structure -->\n<div class="vd-structure"><h2>System change</h2></div>'
    out = render_html(analysis, files, TEMPLATE, now=FROZEN, order=order, structure=structure,
                      walkthrough='<!-- code-walkthrough:walkthrough -->\n<p>wt-body</p>')

    assert '<div class="vd-structure">' in out
    assert out.index("<h2>Story map</h2>") < out.index('<div class="vd-structure">') \
        < out.index("wt-body")


def test_an_empty_structure_section_inserts_nothing():
    order, files = parsed()
    out = render_html(ANALYSIS, files, TEMPLATE, now=FROZEN, order=order, structure="   \n")
    assert "vd-structure" not in out


def test_a_single_group_keeps_the_flow_diagram():
    # No map to draw with one group (see walkthrough.render_story), so FLOW stays exactly as
    # it did before the story map existed.
    order, files = parsed()
    analysis = {**ANALYSIS,
                "groups": [{"title": "Everything", "paths": ["src/auth.py", "README.md"]}]}
    out = render_html(analysis, files, TEMPLATE, now=FROZEN, order=order)

    assert '<h2 class="h2-row"><span>Flow</span>' in out
    assert "<h2>Story map</h2>" not in out
    assert '<div class="map">' not in out


def test_flow_heading_carries_the_maximise_icon_next_to_the_svgbox():
    order, files = parsed()
    analysis = {**ANALYSIS,
                "groups": [{"title": "Everything", "paths": ["src/auth.py", "README.md"]}]}
    out = render_html(analysis, files, TEMPLATE, now=FROZEN, order=order)
    heading = out.split('<span>Flow</span>')[1].split("</h2>")[0]

    assert 'class="vd-max-btn" aria-label="Expand diagram to full size"' in heading
    assert out.index("</h2>") < out.index("svgbox-flow")


if __name__ == "__main__":
    tests = [
        test_facts_come_from_the_diff_not_the_analysis,
        test_explain_rewords_the_headings_and_drops_the_add_remove_arithmetic,
        test_a_net_deletion_reads_as_negative,
        test_both_placeholders_are_replaced,
        test_an_explicit_title_wins_even_with_no_target,
        test_html_escapes_prose_before_promoting_backticks,
        test_html_promotes_backticks_in_every_prose_field,
        test_html_escapes_the_mermaid_source,
        test_pasted_sections_are_never_re_escaped,
        test_symbols_pastes_verbatim_inside_a_collapsed_details_at_the_end,
        test_an_empty_flow_drops_the_whole_section,
        test_flow_box_carries_a_distinct_class_from_the_capped_stacked_diagrams,
        test_flow_tb_leaves_a_non_lr_diagram_alone,
        test_flow_tb_rewrites_only_the_leading_lr_token,
        test_html_rewrites_a_flowchart_lr_analysis_to_tb,
        test_wrap_flow_labels_leaves_a_long_multiword_label_alone,
        test_wrap_flow_labels_breaks_a_long_identifier_at_camel_boundaries,
        test_wrap_flow_labels_leaves_ids_arrows_and_edge_counts_alone,
        test_wrap_flow_labels_breaks_a_long_identifier_in_a_quoted_edge_label,
        test_wrap_flow_labels_breaks_a_long_identifier_in_a_bare_edge_label,
        test_wrap_flow_labels_reaches_a_node_and_an_edge_label_on_the_same_line,
        test_flow_diagram_breaks_a_long_identifier_end_to_end,
        test_an_empty_overview_drops_the_whole_section,
        test_overview_heading_is_the_same_in_both_modes,
        test_an_empty_section_file_inserts_nothing_not_even_its_note,
        test_no_text_size_control_is_emitted_at_all,
        test_the_walkthrough_heading_carries_only_the_explanations_toggle,
        test_a_missing_verdict_drops_only_its_fact_block,
        test_prose_paragraphs_stay_separate,
        test_footer_carries_the_target_and_a_stamp_but_no_local_path,
        test_no_links_json_means_no_links,
        test_html_footer_link_is_safe_to_click_from_a_file_url,
        test_story_map_replaces_flow_and_lands_right_after_overview,
        test_structure_section_lands_between_the_story_map_and_the_walkthrough,
        test_an_empty_structure_section_inserts_nothing,
        test_a_single_group_keeps_the_flow_diagram,
        test_flow_heading_carries_the_maximise_icon_next_to_the_svgbox,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
