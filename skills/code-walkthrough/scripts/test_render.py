#!/usr/bin/env python3
"""Self-check for render.py and for the contracts diff-review-template.html must keep."""

import functools
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import cw_server  # noqa: E402
from cw_testlib import require_node, skip  # noqa: E402
from render import (  # noqa: E402
    render_html, counts_from, _flow_tb, _wrap_flow_labels,
    CONTENT_PLACEHOLDER, TITLE_PLACEHOLDER,
)
from sections import ZWSP  # noqa: E402
from state import build  # noqa: E402
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

TEMPLATE_FILE = Path(__file__).parent.parent / "assets" / "diff-review-template.html"


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


def test_an_empty_flow_drops_the_whole_section():
    out = html({**ANALYSIS, "flow_mermaid": ""})

    assert "<h2>Flow</h2>" not in out
    assert "pre class=\"mermaid\"" not in out
    assert "svgbox-flow" not in out


@pytest.mark.parametrize("flow, expected", [
    ('flowchart TB\n  A --> B', 'flowchart TB\n  A --> B'),
    ('', ''),
    # Slicing on len("flowchart LR") rather than a blind replace: an LR appearing again later
    # in the diagram (a node label, say) must survive untouched.
    ('flowchart LR\n  A["go LR"] --> B', 'flowchart TB\n  A["go LR"] --> B'),
])
def test_flow_tb(flow, expected):
    assert _flow_tb(flow) == expected


def test_html_rewrites_a_flowchart_lr_analysis_to_tb():
    lr = {**ANALYSIS, "flow_mermaid": 'flowchart LR\n  A["load()"] --> B["refresh()"]'}

    html_out = html(lr)

    assert "flowchart TB" in html_out and "flowchart LR" not in html_out


def test_wrap_flow_labels_leaves_ids_arrows_edge_counts_and_multiword_labels_alone():
    # mermaid wraps on spaces by itself, so a label with no over-length word comes back
    # byte for byte (see sections.wrap_label).
    for flow in (
        'flowchart TB\n  A["one two three four five six seven eight"] -->|1| B["ok"]',
        ('flowchart TB\n  A["short"] --> B["NewRequest sets Request User Id to usrID '
         'for the MediaGuard lookup"]'),
    ):
        assert _wrap_flow_labels(flow) == flow


@pytest.mark.parametrize("flow, labels", [
    ('flowchart TB\n  A["short"] -->|"via resolveSymbolMergesAcrossPackages"| B["ok"]',
     [('-->|"', '"|', "via resolveSymbolMergesAcrossPackages")]),
    ('flowchart TB\n  A["short"] -->|via resolveSymbolMergesAcrossPackages| B["ok"]',
     [('-->|', '|', "via resolveSymbolMergesAcrossPackages")]),
    ('flowchart TB\n  A["filterUnusableIdentifierThatIsVeryLong"] '
     '-->|"via resolveSymbolMergesAcrossPackages"| B["ok"]',
     [('A["', '"]', "filterUnusableIdentifierThatIsVeryLong"),
      ('-->|"', '"|', "via resolveSymbolMergesAcrossPackages")]),
])
def test_wrap_flow_labels_edge_forms(flow, labels):
    out = _wrap_flow_labels(flow)

    for start, end, original in labels:
        label = out.split(start, 1)[1].split(end, 1)[0]
        assert ZWSP in label
        assert label.replace(ZWSP, "") == original


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


@pytest.mark.parametrize("overview, present, absent", [
    ("Lead sentence.\n\n- first idea\n- second idea",
     ["<ul><li>first idea</li><li>second idea</li></ul>", "<p>Lead sentence.</p>"], []),
    # No "\n\n" between the lead and the bullets: the lead must stay a <p> and must not become
    # one of the <li>s.
    ("Lead sentence.\n- first idea\n- second idea",
     ["<p>Lead sentence.</p>", "<ul><li>first idea</li><li>second idea</li></ul>"],
     ["<li>Lead sentence.</li>"]),
    ("Lead sentence.\n\n- first idea\n\n- second idea",
     ["<ul><li>first idea</li><li>second idea</li></ul>"], []),
    ("- bulleted\nplain line too", ["<ul><li>bulleted</li><li>plain line too</li></ul>"], []),
    ("- broke `<img src=x onerror=alert(1)>` here",
     ["<li>broke <code>&lt;img src=x onerror=alert(1)&gt;</code> here</li>"], ["<img src=x"]),
])
def test_overview_prose_blocks(overview, present, absent):
    out = html({**ANALYSIS, "overview": overview})

    for needle in present:
        assert needle in out
    for needle in absent:
        assert needle not in out
    assert out.count("<ul>") == 1


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


def test_html_footer_link_is_safe_to_click_from_a_file_url():
    assert "pull request" not in html()

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
    # render.py adds nothing around section-structure.html's own content, only places it --
    # between the story map and the walkthrough, per render_html's own ordering.
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
    # svgbox-flow lets the template size FLOW apart from the capped per-group symbol graphs.
    assert 'class="panel svgbox svgbox-flow"' in out


# ---- the real template: contracts that survive a refactor ----

CSP = ('<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
       "script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'\">")


@functools.lru_cache(maxsize=None)
def _real_template():
    return TEMPLATE_FILE.read_text()


def _between(text, start, end):
    return text.split(start)[1].split(end)[0]


def _wire_ask():
    return _between(_real_template(), "function wireAsk()", "\n  document.querySelectorAll('pre.diff')")


def _wire_post():
    return _between(_real_template(), "function wirePost(canGhCommand){", "\n  function wireCommentsPanel()")


def _node(script):
    require_node()
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode == 3:
        skip("node has no crypto.subtle")
    assert proc.returncode == 0, proc.stderr


def test_the_real_template_carries_the_csp_meta_before_its_first_script_tag():
    order, files = parsed()
    out = render_html(ANALYSIS, files, _real_template(), now=FROZEN, order=order)

    assert CSP in out
    assert out.index(CSP) < out.index("<script")


def test_the_real_template_inline_scripts_all_parse():
    require_node()
    scripts = re.findall(r"<script>(.*?)</script>", _real_template(), re.S)
    check = ("const vm=require('vm');"
             "JSON.parse(require('fs').readFileSync(0,'utf8')).forEach((src,i)=>{"
             "try{new vm.Script(src)}catch(e){console.error('script '+i+': '+e);process.exit(1)}})")
    proc = subprocess.run(["node", "-e", check], input=json.dumps(scripts),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr


# Every daemon call the page makes, as the client-side fragment that issues it and a concrete
# path the daemon must route. A client route the daemon does not serve is a silent 404 in the browser.
_API = "/api/walkthrough/w/r"
_CLIENT_ROUTES = [
    ("api+'/notes'", "/notes", "_NOTES_RE"),
    ("api+'/comment'", "/comment", "_COMMENT_RE"),
    ("api+'/qa'", "/qa", "_QA_RE"),
    ("api+'/post/preview'", "/post/preview", "_POST_PREVIEW_RE"),
    ("api+'/post'", "/post", "_POST_RE"),
    ("api+'/publish-one'", "/publish-one", "_PUBLISH_ONE_RE"),
    ("postJson('/triage'", "/triage", "_TRIAGE_RE"),
    ("api+'/threads/'", "/threads/t-1/resolve", "_RESOLVE_RE"),
    ("/cancel'", "/comment/q-0123abcd/cancel", "_CANCEL_RE"),
    ("LIVE.api+'/events?k='", "/events", "_EVENTS_RE"),
]
_OUTCOME_VERBS = ("dismiss", "edit", "keep", "verbatim", "revert", "reapply", "show", "run", "discard", "handover")


def test_live_code_is_gated_behind_cw_live_and_calls_real_daemon_routes():
    template = _real_template()

    for gate in ("if(window.CW_LIVE) wireAsk();", "if(window.CW_LIVE){", "wirePost(canGhCommand);",
                 "window.CW_LIVE&&window.CW_LIVE.sibling"):
        assert gate in template
    # a static page must show no publish bar
    bar = template.split('<div id="cw-drafts-bar"')[1].split("</div>")[0]
    assert " hidden" in bar.split(">")[0]
    assert "canGhCommand&&(d>0||r>0)" in _wire_post()

    for fragment, path, regex in _CLIENT_ROUTES:
        assert fragment in template, fragment
        assert getattr(cw_server, regex).match(_API + path), (path, regex)
    wire_ask = _wire_ask()
    assert "'/outcomes/'" in template
    for verb in _OUTCOME_VERBS:
        assert f"'{verb}'" in wire_ask, verb
        assert cw_server._OUTCOME_RE.match(f"{_API}/outcomes/o-0123abcd/{verb}"), verb


def test_live_ui_never_uses_html_injection_sinks():
    template = _real_template()
    sibling = "function wireSibling(){" + _between(template, "function wireSibling(){", "wireSibling();")
    split = "function splitBackticks(" + _between(template, "function splitBackticks(", "function wireAsk(){")
    wire_ask = _wire_ask()

    for scope in (wire_ask, split, sibling):
        for banned in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write"):
            assert banned not in scope, banned
    # model-authored text only reaches the DOM through mk(..., text)
    mk = _between(wire_ask, "const mk=(tag,cls,text)=>{", "};")
    assert "e.textContent=text" in mk
    assert "window.open(r.url,'_blank','noopener')" in wire_ask


def test_post_flow_double_submit_guards():
    wire_post = _wire_post()
    send = _between(wire_post, "function send(){", "function retry(){")
    retry = _between(wire_post, "function retry(){", "function submitNow(){")
    dialog = _between(_real_template(), '<dialog id="cw-post-confirm"', "</dialog>")
    publish = "function publishNote(" + _between(_real_template(), "function publishNote(", "function noteSig(")

    assert "if(starting||posting||dlg.open) return;" in wire_post
    assert "if(posting) e.preventDefault()" in wire_post
    assert "ids:selectedIds()" in send and "event:selEvent()" in send and "body:summary.value" in send
    assert "submit_only:true" in wire_post
    assert "failedNoteIds" in retry and "preview(true)" in retry
    assert "resetLocal(body.reset)" in wire_post
    for ev in ("COMMENT", "APPROVE", "REQUEST_CHANGES"):
        assert 'name="cw-post-event" value="%s"' % ev in dialog
    assert dialog.count("checked>") == 1 and 'value="COMMENT" checked>' in dialog
    assert "/publish-one" in publish and "body_sha" in publish and "putNotesNow(false)" in publish
    assert "if(oid) req.oid=oid" in publish


def test_publish_buttons_share_one_in_flight_set_and_a_posted_409_counts_as_success():
    template = _real_template()
    publish = "function publishNote(" + _between(template, "function publishNote(", "function noteSig(")
    assert "const publishing=new Set()" in template
    assert "if(publishing.has(note.id)) return Promise.resolve(false)" in publish
    assert "setPublishBusy(note.id,true)" in publish and "setPublishBusy(note.id,false)" in publish
    assert "status===409" in publish and "no longer a local draft" in publish and "cur.state!=='draft'" in publish


def test_live_reload_flushes_notes_before_reloading():
    # Regression for the live-reload data-loss race: doReload used to call __cwFlushPutNotes()
    # and reload() back to back, so the reload's GET could beat the flush's keepalive PUT and
    # the page came back showing stale notes.
    template = _real_template()
    flush = re.search(r"function flushPutNotes\(\)\{(.*?)\n  \}", template, re.S)
    assert flush, "flushPutNotes() not found in diff-review-template.html"
    assert "return putNotesNow(" in flush.group(1), "flushPutNotes no longer returns the PUT promise"
    body = re.search(r"function doReload\(\)\{(.*?)\n    \}", template, re.S)
    assert body, "doReload() not found in diff-review-template.html"
    body = body.group(1)

    assert "location.reload()" not in body.split(".then(")[0]
    assert re.search(r"\.then\(.*?location\.reload\(\)", body, re.S)
    assert "Promise.race" in body


# Rules that fix real browser bugs, and no behaviour test can see them without a browser.
# display:none on a panel mermaid is about to render makes it measure the labels as zero and
# collapse the diagram for good, so those panels go off-screen instead.
_OFF_SCREEN = (["position:absolute", "left:-99999px"], ["display:none"])
_CSS_RULES = [
    (r"details\.hr-thread-resolved\{([^}]*)\}",
     ["white-space:normal", "line-height:1.6", "overflow-wrap:anywhere"], []),
    (r"body:not\(\.show-notes\) h3\.wt-group ~ \.wt-panel\{([^}]*)\}", *_OFF_SCREEN),
    (r"\.wt-tabpanel\.wt-tab-off\{([^}]*)\}", *_OFF_SCREEN),
    (r"\.wt-tabs \.vd-ctl\[hidden\]\{([^}]*)\}", ["display:none"], []),
    (r"body:not\(\.show-notes\) \.hunk-note-hunk,\n\s*body:not\(\.show-notes\) \.wt-why\{([^}]*)\}",
     ["display:none"], []),
    (r"body:not\(\.show-notes\) h3\.wt-group \.vd-max-btn\{([^}]*)\}", ["display:none"], []),
    (r"\.cw-caret\{([^}]*)\}", [], ["animation"]),
    (r"@media \(prefers-reduced-motion:no-preference\)\{\s*\.cw-caret\{([^}]*)\}",
     ["animation:cw-caret"], []),
    (r"\.cw-sibling \.fb-publish-one[^{]*\.cw-sibling #cw-drafts-bar[^{]*\.cw-sibling \.fb-reply\{([^}]*)\}",
     ["display:none!important"], []),
]


@pytest.mark.parametrize("selector, must, must_not", _CSS_RULES, ids=[r[0][:40] for r in _CSS_RULES])
def test_template_css_rule_pins(selector, must, must_not):
    match = re.search(selector, _real_template())
    assert match, "rule missing: " + selector
    body = match.group(1)

    for decl in must:
        assert decl in body
    for decl in must_not:
        assert decl not in body


def test_block_key_is_stable_whitespace_blind_section_aware_and_charset_safe():
    src = "function blockKeyFrom(" + _between(_real_template(), "function blockKeyFrom(", "function wireAsk(){")
    _node(src + """
const re = /^[A-Za-z0-9:_.|-]{1,200}$/;
const a = blockKeyFrom('s1', 'Hello   world\\n again');
if (a !== blockKeyFrom('s1', ' Hello world again ')) throw new Error('whitespace changed key');
if (a === blockKeyFrom('s2', 'Hello world again')) throw new Error('section ignored');
if (a === blockKeyFrom('s1', 'Hello world')) throw new Error('text ignored');
for (const k of [a, blockKeyFrom('Why it works? / é', 'x'), blockKeyFrom('', ''), blockKeyFrom('a'.repeat(500), 'x')])
  if (!re.test(k)) throw new Error('bad key ' + k);
""")


def test_split_backticks_turns_code_spans_into_nodes_and_never_parses_markup():
    src = "function splitBackticks(" + _between(_real_template(), "function splitBackticks(", "function wireAsk(){")
    _node(src + """
const eq = (a, b) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(JSON.stringify(a) + ' != ' + JSON.stringify(b)); };
eq(splitBackticks('plain text'), [{code:false,text:'plain text'}]);
eq(splitBackticks('use `foo()` now'), [{code:false,text:'use '},{code:true,text:'foo()'},{code:false,text:' now'}]);
const un = splitBackticks('open ` never closed');
if (un.some(p => p.code) || un.map(p => p.text).join('') !== 'open ` never closed') throw new Error('unbalanced backtick changed');
const evil = '<img src=x onerror=alert(1)>';
eq(splitBackticks(evil), [{code:false,text:evil}]);
eq(splitBackticks('`' + evil + '`'), [{code:true,text:evil}]);
""")


def test_bodysha_and_draft_target_mapper_are_pure_and_correct():
    src = "function bodySha(" + _between(_real_template(), "function bodySha(", "function publishNote(")
    # exit 3 = no crypto.subtle on this node; _node turns that into a skip
    _node(src + """
if (typeof crypto === 'undefined' || !crypto.subtle) process.exit(3);
const eq = (a, b) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(JSON.stringify(a) + ' != ' + JSON.stringify(b)); };
eq(ghDraftFields({kind:'new',path:'a.py',line:3,side:'RIGHT',end_line:5,hunk_id:'h1'}, null),
   {path:'a.py',line:3,side:'RIGHT',end_line:5,hunk_id:'h1'});
eq(ghDraftFields({kind:'new',path:'a.py',line:3,side:'RIGHT'}, null).end_line, null);
const root = {id:'n-1',path:'a.py',line:3,side:'RIGHT',hunk_id:'h1',anchor_text:'x',anchor_line:3};
const r = ghDraftFields({kind:'reply',note_id:'n-1'}, root);
eq([r.reply_to, r.in_reply_to, r.path, r.line], ['n-1','n-1','a.py',3]);
eq(ghDraftFields({kind:'reply',note_id:'gone'}, undefined), null);
eq(ghDraftFields({kind:'other'}, root), null);
bodySha('hello').then(h => {
  eq(h, '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824');
  return bodySha('h\\u00e9');
}).then(h => { eq(h.length, 64); });
""")


def test_triage_candidates_and_waiting_count_are_pure_and_correct():
    src = "function triageCandidates(" + _between(_wire_ask(), "function triageCandidates(", "function stateOf(")
    _node(src + """
const eq = (a, b) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(JSON.stringify(a) + ' != ' + JSON.stringify(b)); };
const gh = (id, x) => Object.assign({id, origin:'github', gh_thread_id:'T'+id}, x || {});
const notes = [gh('a'), gh('b', {resolved:true}), gh('c', {reply_to:'a'}), gh('d', {in_reply_to:'a'}),
  gh('e'), {id:'f', origin:'local'}, gh('g'), gh('h', {gh_thread_id:null})];
const turns = new Map([['e', [{source:'triage', status:'done'}]], ['g', [{source:'user', status:'pending'}]],
  ['a', [{source:'user', status:'done'}]]]);
eq(triageCandidates(notes, turns).map(n => n.id), ['a']);
eq(triageCandidates(notes, new Map()).map(n => n.id), ['a', 'e', 'g']);
eq(waitingCount([]), 0);
eq(waitingCount([{state:'proposed'}, {state:'kept'}, {state:'dismissed'}, {state:'proposed'}, {state:'done'}]), 2);
eq(waitingCount([{state:'handed'}, {state:'proposed'}, {state:'handed'}]), 1);
""")


def test_handover_instruction_escapes_ids():
    src = "function handoverInstruction(" + _between(_wire_ask(), "function handoverInstruction(", "function taskPlanFromForm(")
    _node(src + r"""
const eq = (a, b) => { if (a !== b) throw new Error(a + ' != ' + b); };
eq(handoverInstruction('w1', 'o7'),
  'In the code-walkthrough MCP server, call walkthrough_get with id "w1" and parts ["threads"], then do the handed task with oid "o7" in my checkout and call walkthrough_reply with that oid and a short summary.');
const q = handoverInstruction('w"1\n`x`', 'o"7');
if (q.includes('\n')) throw new Error('raw newline');
if (!q.includes(JSON.stringify('w"1\n`x`')) || !q.includes(JSON.stringify('o"7'))) throw new Error('not escaped');
const none = handoverInstruction(undefined, 'o7');
if (none.includes('undefined') || none.includes(' id ')) throw new Error(none);
""")


def test_cherry_pick_and_task_form_helpers_are_pure_and_correct():
    src = "function cherryPickCommand(" + _between(_wire_ask(), "function cherryPickCommand(", "const streams=new Map()")
    _node(src + """
const eq = (a, b) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(JSON.stringify(a) + ' != ' + JSON.stringify(b)); };
eq(cherryPickCommand('abc1234'), 'git cherry-pick abc1234');
eq(cherryPickCommand('abc1234; rm -rf /'), null);
eq(cherryPickCommand('xyz'), null);
eq(cherryPickCommand(undefined), null);
const many = Array.from({length: 30}, (_, i) => 'f' + i).join('\\n');
const r = taskPlanFromForm('  T  ', ' a \\n\\n  \\n b', many);
eq([r.title, r.steps, r.files.length], ['T', ['a', 'b'], 20]);
eq(taskPlanFromForm('x', Array(20).fill('s').join('\\n'), '').steps.length, 12);
const h = taskPlanFromForm('t', '<img src=x onerror=alert(1)>', '');
eq(h.steps, ['<img src=x onerror=alert(1)>']);
""")


@pytest.mark.parametrize("diff_header", [
    "@@ -10,2 +10,3 @@",
    "@@ -20,2 +20,3 @@",
    "@@ -1 +1 @@ def f():",
])
def test_parse_header_prefix_matches_state_hunk_id(diff_header):
    # The hunk id the browser rebuilds from parseHeader must equal the one state.py stored, or
    # every note anchored to a hunk silently detaches.
    diff = ("diff --git a/x.py b/x.py\nindex aaa1111..bbb2222 100644\n--- a/x.py\n+++ b/x.py\n"
            f"{diff_header}\n-a\n+b\n")
    hunk = build({"files": []}, diff)["hunks"][0]
    src = "function parseHeader(" + _between(_real_template(), "function parseHeader(", "\n  }") + "\n  }"

    _node(src + f"""
const p = parseHeader({json.dumps(diff_header)});
if ({json.dumps(hunk["id"])} !== 'x.py\\t' + p.prefix) throw new Error('id mismatch: ' + p.prefix);
""")
