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
    render_html, load_visuals, counts_from, overview_layout, overview_svg,
    visuals_mod,
    CONTENT_PLACEHOLDER, TITLE_PLACEHOLDER,
)
from state import build  # noqa: E402
from walkthrough import render_html as walkthrough_html, story  # noqa: E402
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


def test_pasted_sections_are_never_re_escaped():
    # sections.py already escaped these. Escaping again renders "&amp;lt;" on the page, and
    # re-indenting or re-wording them is how a legend drifts from its arrows.
    walk = '<pre class="diff"><span class="c">  x &lt; y</span></pre>'
    out = html(walkthrough=walk)

    assert walk in out
    assert "&amp;lt;" not in out


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


def test_the_walkthrough_heading_has_a_control_slot_and_no_explanations_toggle():
    wt = "<!-- code-walkthrough:walkthrough -->\n<details>x</details>"
    out = html(walkthrough=wt, links={"pr_url": "https://gh/pull/7"})

    heading = out.split('class="h2-row"')[1].split("</h2>")[0]
    assert 'id="wt-ctl"' in heading
    assert "<a " not in heading
    assert "wt-notes-toggle" not in out
    assert "show-notes" not in _real_template()


def test_no_walkthrough_means_no_heading_and_no_control_slot():
    out = html(walkthrough="")

    assert "Walkthrough" not in out
    assert "wt-ctl" not in out


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


def test_no_story_map_is_rendered_however_many_groups_there_are():
    order, files = parsed()
    analysis = {**ANALYSIS,
                "groups": [{"title": "Auth", "paths": ["src/auth.py"]},
                           {"title": "Docs", "paths": ["README.md"]}]}
    out = render_html(analysis, files, TEMPLATE, now=FROZEN, order=order)

    assert "Story map" not in out and '<div class="map">' not in out
    assert "cw-story" not in out


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
    ("postJson('/trust'", "/trust", "_TRUST_RE"),
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
    assert "cw-drafts-bar" not in template
    assert "id='cw-panel-submit'" in template
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
    md = "function mdUrl(" + _between(template, "function mdUrl(", "// Pinned locale")
    wire_ask = _wire_ask()
    assert "function parseMd(" in md and "function mdEl(" in md

    for scope in (wire_ask, md, sibling):
        for banned in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write"):
            assert banned not in scope, banned
    # model-authored text only reaches the DOM through mk(..., text)
    mk = _between(template, "const mk=(tag,cls,text)=>{", "};")
    assert "e.textContent=text" in mk
    assert "window.open(r.url,'_blank','noopener')" in wire_ask
    turn = _between(wire_ask, "function turnEl(", "function upsertOutcome(")
    assert "mdEl(r.answer||'')" in turn and "mk('p','cw-turn-a',r.answer" not in turn
    assert "pre-wrap" not in re.search(r"\.cw-turn-a\{[^}]*\}", template).group(0)
    assert "mdEl(n.body)" in template


def test_live_turn_and_outcome_cards_show_mode_queue_denials_and_unknown_kinds():
    wire_ask = _wire_ask()
    outcome = _between(wire_ask, "function outcomeEl(t,o){", "function turnEl(")
    assert "if(o.outcome==='edits') return editsCard(o);" in outcome
    assert "if(o.outcome!=='resolve') return plainOutcomeCard(" in outcome
    assert outcome.index("plainOutcomeCard(") < outcome.index("Suggests resolving")
    edits = _between(wire_ask, "function editsCard(o){", "function outcomeEl(t,o){")
    assert "'Agent edited '" in edits and "pl.files" in edits and "pl.stat" in edits and "pl.branch" in edits
    assert ".slice(0,7)" in edits
    for literal in ("'Open updated walkthrough'", "'Copy cherry-pick'", "'Discard all agent edits'", "'Edits discarded'",
                    "oidPath('discard')", "'git cherry-pick '", "showSibling(o,b,btns)"):
        assert literal in edits, literal
    assert "if(o.outcome==='reload') return reloadCard(o);" in outcome
    reload = _between(wire_ask, "function reloadCard(o){", "const dcPosted")
    for literal in ("'Opening the updated walkthrough\\u2026'", "'Nothing to show: '", "'Open updated walkthrough'", "sb||{}"):
        assert literal in reload, literal
    assert ".cw-edits button,.cw-reload button{min-height:44px" in _real_template()
    assert "'changed by agent'" in _real_template() and "startsWith('e-')" in _real_template()
    turn = _between(wire_ask, "function turnEl(r,t,isLast){", "function upsertOutcome(")
    assert "r.mode==='full'" in turn and "'cw-mode-badge'" in turn
    assert "'Queued behind '+r.queued_behind" in turn
    assert "Array.isArray(r.denied)" in turn and "'Needs permission'" in turn
    progress = _between(wire_ask, "function progressText(p){", "const noteById")
    assert "p.tool==='queued'" in progress and "'Queued behind '+d" in progress
    assert "p.tool==='Bash'" in progress and "'Running '+d" in progress
    template = _real_template()
    badge = re.search(r"\.cw-mode-badge\{[^}]*\}", template).group(0)
    assert "var(--accent)" in badge
    assert "#" not in badge


def test_composer_has_no_mode_switch_and_send_times_out():
    wire_ask = _wire_ask()
    composer = _between(wire_ask, "function threadComposer(view){", "function sendView(view){")
    assert "fieldset" not in composer and "radio" not in composer
    send = _between(wire_ask, "function sendView(view){", "function buildThread(t){")
    assert "body.mode" not in send and "signal:ctl.signal" in send and "ctl.abort(),30000" in send
    assert "press Send to retry" in send
    retry = _between(wire_ask, "function retryBtn(t,r){", "const pageEdits=")
    assert "body.mode" not in retry and "postJson('/comment',body)" in retry
    panel = _between(wire_ask, "function renderPanelList(){", "const vv=window.visualViewport")
    assert "(t.turns[t.turns.length-1]||{}).mode==='full'" in panel
    assert "if(e.full) meta.append(mk('span','cw-mode-badge','Full'))" in panel
    assert "postJson('/triage',{})" in wire_ask


def test_draft_comments_outcome_materialises_local_drafts_once():
    wire_ask = _wire_ask()
    upsert = _between(wire_ask, "function upsertOutcome(o){", "const ED_MAX")
    assert upsert.index("materialiseDrafts(o)") < upsert.index("renderThread(t)")
    assert "if(o.outcome==='draft_comments') return draftCommentsCard(o);" in wire_ask
    mat = _between(wire_ask, "function materialiseDrafts(o){", "function draftCommentsCard(")
    assert "o.outcome!=='draft_comments'||o.state!=='applied'" in mat
    assert "'kept'" not in mat
    assert "if(noteById(id)) return;" in mat and "saveNote(note)" in mat and "newDraft({" in mat
    assert "if(!rec) return;" in mat
    assert "ids.length&&!dcPosted.has(o.oid)" in mat
    assert "'/keep',{note_ids:ids}" in mat
    assert "publishNote" not in mat and "'/post" not in mat
    ident = _between(wire_ask, "const dcId=", "\n")
    assert "replace(/[^0-9a-fA-F]/g,'')" in ident
    assert cw_server.VALID_ID_RE.fullmatch("n-" + "0123abcd" + "-29")
    card = _between(wire_ask, "function draftCommentsCard(o){", "function outcomeEl(t,o){")
    assert "'Drafts removed'" in card and "'Agent added '" in card and "skipped, not on this page" in card
    assert "'Show'" in card and "'Remove these drafts'" in card
    assert "'/dismiss',{}" in card and "filter(id=>!left.includes(id)).forEach(deleteNote)" in card and "already in your review, left as is" in card and "revealNote(n)" in card
    assert ".cw-dc button{min-height:44px" in _real_template()


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


_FAKE_DOM = """
const mk=tag=>({tag,className:'',kids:[],attrs:{},hidden:false,
  setAttribute(k,v){this.attrs[k]=v},append(...x){this.kids.push(...x)},
  set textContent(t){this.kids=[String(t)]},
  before(e){ctx.log.push(['before',this.id||this.tag,e]);ctx.live.push(e)},
  after(e){ctx.log.push(['after',this.id||this.tag,e]);ctx.live.push(e)},
  remove(){ctx.live=ctx.live.filter(x=>x!==this)}});
"""


def _skeleton_titles(status, existing_skel=False, overview=False, notes=False, hunk_pos=4, no_wt=False):
    script = _FAKE_DOM + f"""
const ctx={{log:[],live:[]}};
const facts=mk('facts'), wt={{parentElement:Object.assign(mk('wt-h2'),{{compareDocumentPosition:()=>{hunk_pos}}})}};
const hunk={{querySelector:s=>s==='pre.diff'?mk('pre'):null,
  querySelectorAll:()=>[]}};
if({str(notes).lower()}) hunk.querySelector=s=>s==='pre.diff'?mk('pre'):(s==='.hunk-note'?{{}}:null);
if({str(existing_skel).lower()}) ctx.live.push(mk('old'));
const document={{
  querySelector:s=>s==='.facts'?facts:null,
  getElementById:i=>i==='wt-ctl'?({str(not no_wt).lower()}?wt:null):(i==='cw-overview'&&{str(overview).lower()}?{{}}:null),
  createElement:mk,
  querySelectorAll:s=>s==='.cw-skel'?ctx.live:s==='h2'?[{{textContent:{'"Overview"' if overview else '"x"'}}}]:s==='details.hunk'?[hunk]:[]
}};
let liveStatus={json.dumps(status)};
""" + _between(_real_template(), "function paintSkeletons(){", "\n    }\n").join(["function paintSkeletons(){", "\n}"]) + """
paintSkeletons();
const text=e=>e.kids.map(k=>typeof k==='string'?k:text(k)).join('|');
console.log(JSON.stringify(ctx.live.map(e=>[e.className,e.attrs['aria-busy'],text(e)])));
"""
    require_node()
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_placeholders_appear_only_while_building_and_for_what_is_missing():
    got = _skeleton_titles("building")
    assert [r[2] for r in got] == ["System change|Drawing the system change\u2026",
                                   "Overview|Writing the overview\u2026", "Writing notes\u2026"]
    assert all(r[0] == "cw-skel" and r[1] == "true" for r in got)

    assert _skeleton_titles("building", overview=True, notes=True) == []
    assert _skeleton_titles("done") == []
    only_notes = ["Writing notes\u2026"]
    assert [r[2] for r in _skeleton_titles("building", overview=True)] == only_notes
    assert _skeleton_titles("building", overview=True, hunk_pos=2) == []
    assert _skeleton_titles("building", overview=True, no_wt=True) == []
    assert _skeleton_titles("failed", existing_skel=True) == []


def test_dock_is_hidden_while_building_with_no_threads_and_revealed_otherwise():
    fn = _between(_real_template(), "function paintDock(){", "\n    }\n")
    script = f"""
const els={{'cw-dock':{{hidden:true}}}}, $=i=>els[i];
const dock={{setAttribute(){{}}}}, dockSet=()=>{{}};
let dockView, window={{CW_LIVE:{{status:'building'}}}};
const run=(status,v)=>{{ window.CW_LIVE.status=status; dockView=v; paintDock(); return els['cw-dock'].hidden; }};
function paintDock(){{{fn}
}}
const r=[run('building',{{open:0,waiting:0}}), run('building',{{open:1,waiting:0}}),
  run('building',{{open:0,waiting:1}}), run('done',{{open:0,waiting:0}})];
console.log(JSON.stringify(r));
"""
    require_node()
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [True, False, False, False]


def test_static_dock_starts_hidden_and_only_the_static_script_reveals_it_off_live():
    template = _real_template()
    assert '<div id="cw-dock" hidden>' in template
    assert "#cw-dock[hidden]{display:none}" in template
    assert "details.hunk.cw-previewing .cw-skel{display:none}" in template


def _dock_after_wiring(live, status="done", paint=False):
    block = _between(_real_template(), "  wireCommentsPanel();\n  wireResolutionDialog();", "\n\n  function wireSibling")
    script = f"""
const els={{'cw-dock':{{hidden:true}}}}, $=i=>els[i];
const wireCommentsPanel=()=>{{}}, wireResolutionDialog=()=>{{}}, wireAsk=()=>{{}};
const window={json.dumps({"CW_LIVE": {"status": status}} if live else {})};
if({str(paint).lower()}) window.__cwPaintDock=()=>{{}};
""" + "  wireCommentsPanel();\n  wireResolutionDialog();" + block + "\nconsole.log(els['cw-dock'].hidden);"
    require_node()
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip() == "true"


def test_static_dock_shows_regardless_of_the_panel_and_live_fallback_shows_unless_building():
    assert _dock_after_wiring(live=False) is False
    assert _dock_after_wiring(live=True, status="done") is False
    assert _dock_after_wiring(live=True, status="building") is True
    assert _dock_after_wiring(live=True, status="building", paint=True) is True


# Rules that fix real browser bugs, and no behaviour test can see them without a browser.
_CSS_RULES = [
    (r"details\.hr-thread-resolved\{([^}]*)\}",
     ["white-space:normal", "line-height:1.6", "overflow-wrap:anywhere"], []),
    (r"\.dline-plus::before,\.cw-ref-dot-btn::before\{([^}]*)\}",
     ["position:absolute", "inset:-16px"], []),
    (r"\.cw-caret\{([^}]*)\}", [], ["animation"]),
    (r"@media \(prefers-reduced-motion:no-preference\)\{\s*\.cw-caret\{([^}]*)\}",
     ["animation:cw-caret"], []),
    (r"\.cw-sibling \.fb-publish-one[^{]*\.cw-sibling #cw-panel-submit[^{]*\.cw-sibling \.fb-reply\{([^}]*)\}",
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


def test_panel_counts_cover_every_listed_row_except_resolved():
    tpl = _real_template()
    src = "function panelCounts(" + _between(tpl, "function panelCounts(", "function wireAsk(){")
    _node(src + """
const e = (kind, state) => ({kind, state});
const c = panelCounts([e('agent','answered'), e('agent','resolved'), e('agent','draft'),
  e('draft','draft'), e('pr','open'), e('pr','resolved')]);
if (JSON.stringify(c) !== JSON.stringify({all:4, agent:2, pr:1, drafts:1})) throw new Error(JSON.stringify(c));
""")
    panel = _between(_wire_ask(), "function renderPanelList(){", "const vv=window.visualViewport")
    assert "const counts=panelCounts(entries), open=counts.all;" in panel
    assert "+counts[k]" in panel and "all.filter(t=>!t.resolved)" not in panel


def test_gutter_plus_and_ranges_open_the_composer_and_text_selection_only_adds_agent_context():
    ask = _wire_ask()
    gutter = _between(ask, "onGutter=(meta,e)=>{", "let openBox=null")
    shift, plain = gutter.split("clearSelection();", 1)
    assert "e.shiftKey" in shift and "commentRange(gutterAnchor,span)" in shift and "updateSelBar" not in shift
    assert "openLineComposer(lineRangeAnchor(span,span),meta)" in plain
    assert "updateSelBar" not in plain and "addRange" not in plain
    drag_end = _between(ask, "function dragEnd(){", "gutterDown=")
    assert "commentRange(g.start,g.end)" in drag_end and "updateSelBar" not in drag_end
    row_tap = _between(ask, "rowTap=span=>{", "rootEl.classList.add('cw-gutter')")
    assert "commentRange(gutterAnchor,span)" in row_tap and "updateSelBar" not in row_tap
    comment = _between(ask, "function commentRange(a,b){", "function setRowRange(")
    assert "openLineComposer(anchor,rec.meta)" in comment and "removeAllRanges()" in comment
    tpl = _real_template()
    assert "cw-selbar-pr" not in tpl and "PR comment" not in tpl and "sbPr" not in tpl
    wire_line = _between(tpl, "function wireLine(span,meta){", "// Extension (no dot)")
    assert "span.addEventListener('keydown',e=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); open(e); } });" in wire_line
    assert re.search(r"plus\.addEventListener\('click',e=>\{\s*e\.stopPropagation\(\);.*open\(e\);\s*\}\);", wire_line)
    update = _between(ask, "function updateSelBar(){", "let selTimer=null;")
    assert "buildAskAnchor(sel)" in update and "planEl.contains(sel.anchorNode)" in update


def test_a_click_right_after_a_range_drag_is_swallowed_but_ordinary_clicks_are_not():
    tpl = _real_template()
    wire_line = _between(tpl, "function wireLine(span,meta){", "// Extension (no dot)")
    for handler in ("plus", "badge"):
        line = next(l for l in wire_line.splitlines() if l.strip().startswith(f"{handler}.addEventListener('click'"))
        assert re.search(r"if\(gutterSwallow\)\s*\{\s*gutterSwallow=false;\s*return;\s*\}\s*open\(e\)", line)
    ask = _wire_ask()
    drag_end = _between(ask, "function dragEnd(){", "gutterDown=")
    assert re.search(r"if\(!stopDrag\(\)\)\s*return;", drag_end) and "gutterSwallow=true" in drag_end
    assert "setTimeout" in drag_end
    down = _between(ask, "gutterDown=(e,span)=>{", "stopDrag();")
    assert "gutterSwallow=false" in down


def test_copy_for_agent_is_gone():
    tpl = _real_template()
    for gone in ("Copy for agent", "hr-comments-agent-btn", "cw-panel-copy", "agentFeedbackText", "notesForAgent"):
        assert gone not in tpl


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


def test_ref_fences_round_trip_and_reject_bad_headers():
    src = "function refFenceFor(" + _between(_real_template(), "function refFenceFor(", "function wireAsk(){")
    _node(src + r"""
const eq = (a, b) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(JSON.stringify(a) + ' != ' + JSON.stringify(b)); };
const line = (path, a, b, quote) => ({anchor: {kind:'line', path, side:'RIGHT', line:a, end_line:b, quote}, label:'x'});
const block = {anchor: {kind:'block', block:'overview|1a2b3c4d', quote:'Some prose'}, label:'Overview · ¶1'};
const hdr = r => { const a = r.anchor; return a.kind === 'line'
  ? {kind:'line', path:a.path, line:a.line, end_line:a.end_line, side:a.side}
  : {kind:'block', block:a.block, label:r.label}; };
const back = (segs) => parseRefText(serializeSegs(segs)).map(p => p.text != null ? p.text
  : {header: p.ref, body: p.body});
const want = segs => segs.map(s => typeof s === 'string' ? s : {header: hdr(s), body: s.anchor.quote});

const l1 = line('src/a.py', 3, 5, '+x = 1\n y = 2\n-z');
eq(back(['Look at', l1, 'and also', block, 'tail']), want(['Look at', l1, 'and also', block, 'tail']));
eq(back([l1, block]), want([l1, block]));
eq(back(['a\n', l1, '\nb']), ['a\n', {header: hdr(l1), body: l1.anchor.quote}, '\nb']);
eq(serializeSegs(['only text']), 'only text');

const tick = line('b.py', 7, null, 'a ```tick``` here\n+k');
if (!refFence(tick).startsWith('````ref b.py:7 RIGHT\n')) throw new Error('fence not widened: ' + refFence(tick));
eq(back(['t', tick]), want(['t', tick]));

const spaced = line('my dir/a "b".py', 2, null, '+q');
eq(back([spaced]), want([spaced]));
eq(parseRefText(refFence(spaced))[0].ref.path, 'my dir/a "b".py');

const bad = ['```ref nothing here\nbody\n```', '```ref a.py:0 RIGHT\nx\n```', '```ref a.py:5-2 RIGHT\nx\n```',
  '```ref a.py:3 MIDDLE\nx\n```', '```ref a.py:1-999999999999 RIGHT\nx\n```'];
for (const t of bad) eq(parseRefText(t), [{text: t}]);
const open = '```ref a.py:3 RIGHT\n+x\nno closing fence';
eq(parseRefText(open), [{text: open}]);

eq(parseRefText('a\r\n```ref a.py:3 RIGHT\r\n+x\r\n```\r\nb'),
  [{text:'a'}, {ref:{kind:'line', path:'a.py', line:3, end_line:null, side:'RIGHT'}, body:'+x'}, {text:'b'}]);
""")


def test_page_edit_label_uses_correct_grammar_for_every_block_type_and_op():
    src = "function pageEditWhat(" + _between(_real_template(), "function pageEditWhat(", "function pageEditCard(")
    _node(src + """
const cases = [
  ['prose', 'added prose after this block', 'replaced this block with prose'],
  ['list', 'added a list after this block', 'replaced this block with a list'],
  ['mermaid', 'added a diagram after this block', 'replaced this block with a diagram'],
  ['picture', 'added a diagram after this block', 'replaced this block with a diagram'],
  ['other', 'added a block after this block', 'replaced this block with a block'],
];
for (const [type, ins, rep] of cases) {
  const a = pageEditWhat({op: 'insert_after', block: {type}}), b = pageEditWhat({op: 'replace', block: {type}});
  if (a !== ins || b !== rep) throw new Error(type + ': ' + a + ' / ' + b);
}
""")


_PAGE_EDIT_DOM = """
class El {
  constructor(tag) { this.tag = tag; this.kids = []; this.parent = null; this.dataset = {}; this.cls = new Set(); this._t = ''; this.listeners = {};
    this.classList = {add: c => this.cls.add(c), remove: c => this.cls.delete(c), contains: c => this.cls.has(c)}; }
  get isConnected() { let n = this; while (n.parent) n = n.parent; return n === root; }
  get textContent() { return this._t + this.kids.map(k => k.textContent).join(''); }
  append(...ns) { ns.forEach(n => { n.parent = this; this.kids.push(n); }); }
  prepend(n) { n.parent = this; this.kids.unshift(n); }
  remove() { if (this.parent) this.parent.kids.splice(this.parent.kids.indexOf(this), 1); this.parent = null; }
  insertAdjacentElement(_w, n) { const i = this.parent.kids.indexOf(this); n.parent = this.parent; this.parent.kids.splice(i + 1, 0, n); }
  addEventListener() {}
  setAttribute() {}
  find(f) { for (const k of this.kids) { if (f(k)) return k; const r = k.find(f); if (r) return r; } return null; }
  querySelector(sel) { const c = sel.replace('.', ''); return this.find(k => k.cls.has(c) || k.tag === sel); }
  querySelectorAll() { return []; }
}
const root = new El('root'), planEl = root;
const mk = (tag, cls, text) => { const e = new El(tag); (cls || '').split(' ').filter(Boolean).forEach(c => e.cls.add(c)); e._t = text || ''; return e; };
const mkBtn = (t) => mk('button', '', t);
const mdEl = (t) => mk('div', 'md', t);
const textOf = e => e.textContent, norm = x => x;
const postJson = () => Promise.resolve({ok: true, body: {}});
const loadQa = () => {};
const outcomesByQid = new Map();
const blockEl = mk('p', '', 'block'); root.append(blockEl);
const blockIndex = () => ({byKey: new Map([['k1', blockEl]]), list: []});
const threads = new Map([['t1', {turns: [{anchors: [{kind: 'block', quote: 'zzz'}]}]}]]);
let bad = false;
global.DOMParser = class { parseFromString(s, type) {
  if (type === 'text/html') return {body: {childNodes: [mk('svg', 'cw-pic', s)]}};
  return {querySelector: () => bad ? {} : null, documentElement: mk('svg', 'cw-ov', s)}; } };
global.document = {importNode: n => n, createElement: t => mk(t)};
const eq = (a, b, m) => { if (a !== b) throw new Error(m + ': ' + JSON.stringify(a) + ' != ' + JSON.stringify(b)); };
"""


def _page_edit_script():
    t = _real_template()
    return _PAGE_EDIT_DOM + "const pageEdits=new Map();" + _between(t, "const pageEdits=new Map();", "function syncThreadEdits(")


def test_page_edit_graph_block_renders_the_server_svg_under_its_title_and_guards_a_bad_one():
    _node(_page_edit_script() + """
const ok = pageEditContent({type: 'graph', graph: {title: 'Flow <b>'}, svg: '<svg class="cw-ov"/>'});
const wrap = ok.find(k => k.cls.has('cw-ov-wrap'));
eq(!!wrap && wrap.cls.has('cw-vis'), true, 'wrap');
eq(wrap.find(k => k.cls.has('cw-ov-sub')).textContent, 'Flow <b>', 'title');
eq(!!wrap.find(k => k.tag === 'svg'), true, 'svg');
bad = true;
const broken = pageEditContent({type: 'graph', graph: {title: 't'}, svg: 'oops'});
eq(!!broken.find(k => k.cls.has('cw-pageedit-err')) && !broken.find(k => k.tag === 'svg'), true, 'guard');
""")


def test_page_edit_picture_block_renders_its_title_and_parsed_html_in_a_cw_vis_wrapper():
    _node(_page_edit_script() + """
const box = pageEditContent({type: 'picture', title: 'Shape <i>', html: '<svg/>'});
const wrap = box.find(k => k.cls.has('cw-vis'));
eq(!!wrap, true, 'wrap');
eq(wrap.find(k => k.cls.has('cw-ov-sub')).textContent, 'Shape <i>', 'title');
eq(!!wrap.find(k => k.tag === 'svg'), true, 'svg');
""")


def test_page_edit_replacing_an_earlier_edit_by_oid_hides_it_and_undo_restores_it_in_any_order():
    _node(_page_edit_script() + """
const edit = (oid, op, target, text) => ({oid, thread_id: 't1', state: 'applied', outcome: 'page_edit',
  payload: {op, target, block: {type: 'prose', text}}});
const A = edit('o-a', 'insert_after', 'k1', 'a'), B = edit('o-b', 'replace', 'o-a', 'b');
syncPageEdit(A); syncPageEdit(B);
const a = pageEdits.get('o-a').node, b = pageEdits.get('o-b').node;
eq(a.cls.has('cw-pageedit-hidden'), true, 'A hidden by B');
eq(root.kids.indexOf(b), root.kids.indexOf(a) + 1, 'B sits after A');
removePageEdit('o-b');
eq(a.cls.has('cw-pageedit-hidden'), false, 'undo of B un-hides A');

removePageEdit('o-a');
syncPageEdit(B);
eq(pageEdits.get('o-b').outdated, true, 'B before A is outdated');
syncPageEdit(A);
resyncPageEdits();
const a2 = pageEdits.get('o-a').node;
eq(pageEdits.get('o-b').outdated, false, 'resync resolves B');
eq(a2.cls.has('cw-pageedit-hidden'), true, 'A hidden after resync');
eq(root.kids.indexOf(pageEdits.get('o-b').node), root.kids.indexOf(a2) + 1, 'B after A');
""")


def test_parse_md_parses_blocks_and_inline_markup_into_plain_data_and_never_markup():
    src = "function mdUrl(" + _between(_real_template(), "function mdUrl(", "function inlineInto(")
    _node(src + """
const eq = (a, b) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(JSON.stringify(a) + ' != ' + JSON.stringify(b)); };
eq(parseMd('| a | b | c | d |\\n| --- | :--- | :---: | ---: |\\n| 1 | 2 | 3 | 4 |'),
   [{kind:'table',head:['a','b','c','d'],align:['','left','center','right'],rows:[['1','2','3','4']]}]);
eq(parseMd('x | y\\n-|-\\na\\\\|b | c').map(b => b.rows), [[['a|b', 'c']]]);
eq(parseMd('| a | b |\\n|---|---|\\n| 1 |\\n| 1 | 2 | 3 |').map(b => b.rows), [[['1', ''], ['1', '2']]]);
eq(parseMd('intro line\\n| a |\\n|---|\\n| 1 |\\n\\nafter'),
   [{kind:'p',text:'intro line'},{kind:'table',head:['a'],align:[''],rows:[['1']]},{kind:'p',text:'after'}]);
eq(parseMd('a | b\\nnot a delimiter\\n\\nx'), [{kind:'p',text:'a | b\\nnot a delimiter'},{kind:'p',text:'x'}]);
const evil = '<img src=x onerror=alert(1)>';
eq(parseMd('| ' + evil + ' |\\n|---|\\n| `c` |')[0].head, [evil]);

eq(parseMd('Intro:\\n- a\\n* b\\n  - c\\n  - d\\n- e'),
   [{kind:'p',text:'Intro:'},{kind:'list',ordered:false,items:[
     {text:'a',sub:null},{text:'b',sub:{ordered:false,items:['c','d']}},{text:'e',sub:null}]}]);
eq(parseMd('1. one\\n2. two\\n   more'), [{kind:'list',ordered:true,items:[{text:'one',sub:null},{text:'two\\nmore',sub:null}]}]);
eq(parseMd('*italic* start\\n**bold** start'), [{kind:'p',text:'*italic* start\\n**bold** start'}]);
eq(parseMd('# One\\n## Two\\n### Three\\n#### four'),
   [{kind:'h',level:1,text:'One'},{kind:'h',level:2,text:'Two'},{kind:'h',level:3,text:'Three'},{kind:'p',text:'#### four'}]);
eq(parseMd('a\\n```py\\nx = 1\\n\\n# not a heading\\n- not a list\\n```\\nb'),
   [{kind:'p',text:'a'},{kind:'pre',text:'x = 1\\n\\n# not a heading\\n- not a list'},{kind:'p',text:'b'}]);
eq(parseMd('```\\nnever closed'), [{kind:'p',text:'```\\nnever closed'}]);

eq(parseInline('plain text'), [{t:'text',v:'plain text'}]);
eq(parseInline('use `foo()` now'), [{t:'text',v:'use '},{t:'code',v:'foo()'},{t:'text',v:' now'}]);
eq(parseInline('open ` never closed'), [{t:'text',v:'open ` never closed'}]);
eq(parseInline(evil), [{t:'text',v:evil}]);
eq(parseInline('`' + evil + '`'), [{t:'code',v:evil}]);
eq(parseInline('a **b `c` d** e *f*'),
   [{t:'text',v:'a '},{t:'b',c:[{t:'text',v:'b '},{t:'code',v:'c'},{t:'text',v:' d'}]},{t:'text',v:' e '},{t:'i',c:[{t:'text',v:'f'}]}]);
eq(parseInline('**`**`'), [{t:'text',v:'**'},{t:'code',v:'**'}]);
for (const open of ['**unclosed', '*unclosed', '2 * 3 * 4', 'a ** b ** c', '[x](https://a.b', '[x] (https://a.b)'])
  if (parseInline(open).some(t => t.t !== 'text') || parseInline(open).map(t => t.v).join('') !== open) throw new Error('not literal: ' + open);
eq(parseInline('_snake_case_ and a_b_c'), [{t:'text',v:'_snake_case_ and a_b_c'}]);
eq(parseInline('[docs](https://a.b/c?d=1)'), [{t:'a',href:'https://a.b/c?d=1',c:[{t:'text',v:'docs'}]}]);
for (const bad of ['javascript:alert(1)', 'JaVaScRiPt:alert(1)', 'data:text/html,x', 'vbscript:x', '/relative', '//a.b'])
  if (parseInline('[x](' + bad + ')').some(t => t.t === 'a')) throw new Error('linked: ' + bad);
eq(parseInline('[<b>x</b>](http://a.b)')[0].c, [{t:'text',v:'<b>x</b>'}]);
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
  'In the code-walkthrough MCP server, call walkthrough_get with id "w1" and parts ["threads"], then do the handed task with oid "o7" in my checkout and call walkthrough_reply with thread_id from that task, that oid and a short summary as text.');
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


def test_check_line_reports_pass_fail_and_not_run():
    _node("function checkLine(" + _between(_wire_ask(), "function checkLine(", "function cherryPickCommand(") + """
const eq = (a, b) => { if (a !== b) throw new Error(a + ' != ' + b); };
eq(checkLine({command: 'go test ./a/', exit: 0, ok: true}), 'go test ./a/ passed');
eq(checkLine({command: 'go test ./a/', exit: 1, ok: false}), 'go test ./a/ failed (exit 1)');
eq(checkLine({command: 'go test ./a/', exit: null, ok: false}), 'go test ./a/ did not run');
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


# ---- pictures and All symbols ----

GRAPH_SD = {"nodes": [
    {"id": "p", "label": "p", "kind": "pkg", "parent": None, "depth": 0},
    *[{"id": f"p:{n}", "label": n, "kind": "symbol", "parent": "p", "depth": 1,
       "file": "src/auth.py", "range": r, "state": "changed"}
      for n, r in (("load", [40, 42]), ("old", [41, 41]))],
], "edges": [{"id": "e0", "source": "p:load", "target": "p:old", "state": "new"}]}

WALK = ('<!-- code-walkthrough:walkthrough -->\n<h3 class="wt-group">One</h3>\n'
        '<!-- cw-picture:0 -->\n<details class="hunk">a</details>\n'
        '<h3 class="wt-group">Two</h3>\n<!-- cw-picture:1 -->\n<details class="hunk">b</details>\n')
PR_PIC = {"title": "Before & after", "html": "<p>pr-picture</p>"}
ALL_SYMS = '<details class="cw-allsyms"><summary>All symbols</summary>'


def pics(clean=None, symdelta=None, walkthrough=WALK, **kw):
    order, files = parsed()
    return render_html(ANALYSIS, files, TEMPLATE, now=FROZEN, order=order, symdelta=symdelta,
                       walkthrough=walkthrough, visuals=clean, **kw)


def test_pr_picture_section_sits_after_overview_before_the_walkthrough_with_an_escaped_label():
    out = pics({"pr": PR_PIC, "groups": {}})

    assert ('<section class="cw-pr-picture" id="cw-pr-picture" aria-labelledby="cw-pr-picture-h">'
            '<h2 id="cw-pr-picture-h">The PR in one picture</h2>'
            '<div class="cw-vis" role="group" aria-label="Before &amp; after"><p>pr-picture</p>'
            '</div></section>') in out
    assert out.index("<h2>Overview</h2>") < out.index('id="cw-pr-picture"') \
        < out.index("<span>Walkthrough</span>")
    assert out.count('class="cw-vis') == 1


def test_a_hostile_title_is_escaped_in_the_label():
    out = pics({"pr": {"title": '"><script>x</script>', "html": "<p>a</p>"}, "groups": {}})

    assert 'aria-label="&quot;&gt;&lt;script&gt;x&lt;/script&gt;"' in out
    assert "<script>x" not in out


@pytest.mark.parametrize("clean, pr_sections, group_pictures", [
    (None, 0, 0),
    ({"pr": None, "groups": {}}, 0, 0),
    ({"pr": PR_PIC, "groups": {}}, 1, 0),
    ({"pr": None, "groups": {1: "<p>g1</p>"}}, 0, 1),
    ({"pr": PR_PIC, "groups": {0: "<p>g0</p>", 1: "<p>g1</p>"}}, 1, 2),
])
def test_layout_with_zero_one_or_two_pictures(clean, pr_sections, group_pictures):
    out = pics(clean)

    assert out.count('<section class="cw-pr-picture"') == pr_sections
    assert out.count('class="cw-vis cw-vis-group"') == group_pictures
    assert "cw-picture:" not in out


def test_group_picture_lands_by_index_between_its_heading_and_its_files():
    out = pics({"pr": None, "groups": {1: "<p>g1</p>"}})

    two = out.index(">Two</h3>")
    assert two < out.index('<div class="cw-vis cw-vis-group"><p>g1</p></div>') < out.index(">b</details>")
    assert out.index(">a</details>") < two


def test_a_group_picture_out_of_range_renders_nothing():
    out = pics({"pr": None, "groups": {7: "<p>nope</p>"}})

    assert "nope" not in out and "cw-picture:" not in out


def test_all_symbols_is_collapsed_once_after_the_walkthrough_only_with_symbols():
    out = pics(symdelta=GRAPH_SD)

    assert out.count(ALL_SYMS) == 1 and out.count('class="vd-symbols') == 1
    assert out.index(">b</details>") < out.index(ALL_SYMS) < out.index('<div class="foot">')
    assert "<details class=\"cw-allsyms\" open" not in out
    for none in (None, {"nodes": []}, {"language": None}):
        assert "cw-allsyms" not in pics(symdelta=none)


def test_page_without_pictures_still_has_all_symbols_and_nothing_else_new():
    out = pics(symdelta=GRAPH_SD)

    assert ALL_SYMS in out
    assert 'class="cw-vis' not in out and '<section class="cw-pr-picture"' not in out


def test_the_old_spec_story_is_gone_even_if_analysis_still_carries_visuals():
    order, files = parsed()
    out = render_html({**ANALYSIS, "visuals": [{"title": "T", "acts": []}]}, files, TEMPLATE,
                      now=FROZEN, order=order, symdelta=GRAPH_SD)

    assert "cw-story" not in out and "cw-story-data" not in out


def _visuals_file(tmp_path, raw):
    path = tmp_path / "visuals.json"
    path.write_text(json.dumps(raw))
    return path


def test_load_visuals_drops_hostile_fragments_and_reports_them(tmp_path):
    hostile = '<p>ok</p><script>evil()</script><img src=x onerror=alert(1)>'
    raw = {"pr": {"title": "t", "html": hostile},
           "groups": [{"index": 0, "html": "<p>fine</p>"}, {"index": 9, "html": "<p>x</p>"},
                      {"index": 1, "html": '<p><!-- HR_STATE -->&lt;!-- HR_STATE --&gt;</p>'}]}
    clean, lines = load_visuals(_visuals_file(tmp_path, raw), 2)
    out = pics(clean)

    assert clean["pr"] is None and set(clean["groups"]) == {0, 1}
    assert lines[0] == "visuals: 2 kept, 2 dropped"
    assert any(line.startswith("visuals: visuals.pr:") for line in lines[1:])
    assert "evil()" not in out and "onerror" not in out and "<script" not in out
    assert "<!-- HR_STATE -->" not in out
    assert "&lt;!-- HR_STATE --&gt;" in out


def test_a_picture_follows_its_analysis_index_past_a_skipped_group(tmp_path):
    order, files = parsed()
    groups = [{"title": "AAA", "paths": ["src/auth.py"]},
              {"title": "BBB", "paths": ["gone.py"]},
              {"title": "CCC", "paths": ["README.md"]}]
    rendered = story(order, files, groups, None)
    raw = {"pr": None, "groups": [{"index": i, "html": f"<p>pic{i}</p>"} for i in range(3)]}
    clean, lines = load_visuals(_visuals_file(tmp_path, raw), len(groups),
                                {g.idx for g in rendered})
    out = pics(clean, walkthrough=walkthrough_html(rendered, files, {}))

    assert lines == ["visuals: 2 kept, 1 dropped",
                     "visuals: visuals.groups[1]: group has no files on this page"]
    assert out.index(">AAA ") < out.index("pic0") < out.index(">CCC ") < out.index("pic2")
    assert "pic1" not in out and out.count('class="cw-vis cw-vis-group"') == 2


def test_load_visuals_survives_unreadable_json(tmp_path):
    path = tmp_path / "visuals.json"
    path.write_text("{not json")

    clean, lines = load_visuals(path, 2)

    assert clean is None and lines == ["visuals: unreadable (JSONDecodeError)"]


def test_level_script_defers_a_graph_inside_a_closed_details_to_its_first_toggle():
    script = _between(_real_template(), "querySelectorAll('.vd-symbols')", "\n})();")

    assert "root.closest('details')" in script and "addEventListener('toggle'" in script


def test_reply_row_is_a_compact_one_row_input_with_a_round_send_and_quiet_actions():
    template = _real_template()
    row = _between(template, "function buildReplyRow(root){", "function wireReplyBox(")
    assert "input.rows=1" in row and "'fb-reply-send'" in row
    assert "btn.setAttribute('aria-label','Reply on GitHub')" in row and "btn.hidden=true" in row
    assert "'fb-quiet cw-reply-ask'" in row and "'Ask the agent'" in row
    assert "fb-primary" not in row and "fb-ghost" not in row
    resolve = _between(template, "function buildResolveButton(root){", "const threadWrapByRoot")
    assert "'fb-quiet'" in resolve and "btn.textContent='Resolve'" in resolve
    assert "setAttribute('aria-label','Resolve conversation')" in resolve and "markPendingResolve(root.id)" in resolve
    wire = _between(template, "function wireReplyBox(", "function buildResolveButton(")
    assert "e.key==='Enter'&&!e.shiftKey" in wire and "btn.hidden=!can" in wire
    for sel, must in ((r"\.fb-reply-input\{([^}]*)\}", ["resize:none", "max-height:"]),
                      (r"\.fb-reply-box:focus-within\{([^}]*)\}", ["outline:2px solid var(--accent)"]),
                      (r"\.fb-reply-box \.fb-reply-send::before\{([^}]*)\}", ["position:absolute", "inset:-9px"]),
                      (r"\.fb-item \.fb-quiet,\.fb-actions \.fb-quiet\{min-height:44px\}", [])):
        m = re.search(sel, template)
        assert m, sel
        for frag in must:
            assert frag in m.group(1), frag


def test_quiet_actions_and_reply_input_do_not_inherit_the_diff_line_height_or_mono_font():
    template = _real_template()
    sans = "font-family:system-ui,-apple-system"
    quiet = re.search(r"\.fb-item \.fb-quiet,\.fb-actions \.fb-quiet\{([^}]*)\}", template).group(1)
    assert "line-height:1.4" in quiet and sans in quiet and "font-size:var(--fs-xs)" in quiet
    acts = re.search(r"\.fb-reply-acts\{([^}]*)\}", template).group(1)
    assert "line-height:1.4" in acts and sans in acts
    inp = re.search(r"\.fb-reply-input\{([^}]*)\}", template).group(1)
    assert sans in inp and "font-size:var(--fs-sm)" in inp and "var(--mono)" not in inp


def test_ask_the_agent_from_a_thread_seeds_a_line_chip_from_the_root_comment():
    template = _real_template()
    ask = _between(template, "openThreadAsk=(root,host,input)=>{", "const openLineComposer=")
    assert "threadRef(root)" in ask and "v.editor.setSegs([ref,' '])" in ask
    ref = _between(template, "function threadRef(root){", "function chipNode(")
    assert "kind:'line'" in ref and "root.path" in ref and "root.side" in ref and "root.body" in ref and "validRef(ref)" in ref


def test_reply_box_helpers_gate_sending_and_cap_the_grown_height():
    src = _between(_real_template(), "const REPLY_MAX_H=160;", "function buildReplyRow(")
    _node(src + """
const eq = (a, b) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(JSON.stringify(a) + ' != ' + JSON.stringify(b)); };
eq([replyCanSend(''), replyCanSend(' \\n\\t'), replyCanSend(' a '), replyCanSend(null)], [false, false, true, true]);
eq(replyFit(30, 160), {h: 30, over: false});
eq(replyFit(160, 160), {h: 160, over: false});
eq(replyFit(400, 160), {h: 160, over: true});
""")


# ---- picture styling and removed features ----

_VIS_ALIASES = {
    "--background": "var(--bg)", "--foreground": "var(--text)", "--card": "var(--panel)",
    "--muted-foreground": "var(--muted)", "--success": "var(--green)",
    "--warning": "var(--yellow)", "--destructive": "var(--red)",
    "--chart-1": "var(--accent)", "--chart-2": "var(--green)", "--chart-3": "var(--yellow)",
    "--chart-4": "var(--purple)", "--chart-5": "var(--cyan)", "--chart-6": "var(--red)",
    "--code-background": "var(--panel2)", "--font-mono": "var(--mono)", "--radius": "8px",
}


def _vis_rule():
    return re.search(r"\n  \.cw-vis\{([^}]*)\}", _real_template()).group(1)


@pytest.mark.parametrize("name, value", _VIS_ALIASES.items())
def test_cw_vis_aliases_t3_theme_names_to_page_tokens(name, value):
    assert f"{name}:{value}" in _vis_rule()


def test_cw_vis_surface_aliases_and_font_are_defined():
    rule = _vis_rule()
    for name in ("--accent-surface", "--destructive-surface", "--font-sans"):
        assert name + ":" in rule
    assert "color-mix(in srgb,var(--red) 14%,var(--panel))" in rule


def test_cw_vis_sets_its_own_line_height_and_font_against_inherited_diff_values():
    rule = _vis_rule()
    assert "line-height:1.4" in rule and "font-family:var(--font-sans)" in rule


def test_the_global_svg_text_restyle_leaves_pictures_alone():
    template = _real_template()
    assert re.search(r"svg:not\(\s*\.cw-vis\s+svg\s*\)\s+text\s*\{", template)
    assert not re.search(r"^\s*svg\s+text\s*\{", template, re.M)


def test_pictures_are_never_wired_as_zoomable_diagrams():
    sel = re.search(r"const DIAGRAM_SEL='([^']*)'", _real_template()).group(1)
    assert "cw-vis" not in sel


@pytest.mark.parametrize("gone", [
    "wireStory", "storyNextIndex", "storyHunkKey", "cw-story", "cw-card", "cw-flow",
    "svgbox", ".hop", ".spine", "In graph", "cw-ingraph", "wt-badge", "wireGroupTabs",
])
def test_template_carries_nothing_of_the_removed_story_and_flow_features(gone):
    assert gone not in _real_template()


def test_a_rendered_picture_reaches_the_page_inside_its_cw_vis_wrapper():
    out = pics({"pr": PR_PIC, "groups": {}})

    wrapper = re.search(r'<div class="cw-vis"[^>]*>(.*?)</div>', out, re.S)
    assert wrapper and wrapper.group(1) == PR_PIC["html"]


@pytest.mark.parametrize("explain, heading", [(False, "The PR in one picture"), (True, "This code in one picture")])
def test_the_picture_heading_follows_the_mode(explain, heading):
    out = pics({"pr": PR_PIC, "groups": {}}, explain=explain)

    assert f'<h2 id="cw-pr-picture-h">{heading}</h2>' in out


def test_load_visuals_survives_pathologically_deep_json(tmp_path):
    path = tmp_path / "visuals.json"
    path.write_text("[" * 100000)

    clean, lines = load_visuals(path, 2)

    assert clean is None and lines == ["visuals: unreadable (RecursionError)"]


@pytest.mark.parametrize("raw, reason", [([1], 'expected an object with "pr" and "groups"'),
                                         ({"pr": {"title": "t", "html": "<p>" + "x" * 400000 + "</p>"}, "groups": []},
                                          "in total, over the")])
def test_a_rejected_visuals_file_reads_as_ignored_not_zero_kept(tmp_path, raw, reason):
    clean, lines = load_visuals(_visuals_file(tmp_path, raw), 2)

    assert clean == {"pr": None, "overview": None, "groups": {}}
    assert len(lines) == 1 and lines[0].startswith("visuals: ignored, ") and reason in lines[0]
    assert "kept" not in lines[0] and "visuals: visuals:" not in lines[0]


# ---- System change diagram ----

OV_NODES = [
    {"id": "agent", "label": "Agent", "note": "calls tools", "kind": "changed", "group": 0, "refs": [], "watch": False},
    {"id": "wrap", "label": "Wrapper", "note": "", "kind": "new", "group": 1, "refs": ["src/auth.py"], "watch": True},
    {"id": "tool", "label": "Tool", "note": "", "kind": "same", "group": None, "refs": ["src/auth.py:load_session"], "watch": False},
    {"id": "gate", "label": "Gate", "note": "", "kind": "guard", "group": 0, "refs": [], "watch": False},
    {"id": "wh", "label": "Warehouse", "note": "", "kind": "removed", "group": 1, "refs": [], "watch": False},
    {"id": "appr", "label": "Approval", "note": "", "kind": "new", "group": 0, "refs": [], "watch": False},
]
OV_EDGES = [
    {"from": a, "to": b, "label": lab, "kind": k} for a, b, lab, k in [
        ("agent", "wrap", "", "new"), ("wrap", "tool", "", "same"), ("tool", "gate", "check", "guard"),
        ("gate", "wh", "", "removed"), ("gate", "appr", "", "changed"), ("appr", "agent", "retry", "new")]
]
OV = {"title": "Agent flow", "nodes": OV_NODES, "edges": OV_EDGES,
      "notes": [{"kind": "config", "text": "flag on", "group": 1, "refs": []}]}


def ov_page(ov=OV, **kw):
    return pics({"pr": None, "overview": ov, "groups": {}}, walkthrough="", **kw)


def test_overview_layout_layers_left_to_right_and_flags_the_back_edge():
    lay = overview_layout(OV_NODES, OV_EDGES)

    assert lay["layer"] == [0, 1, 2, 3, 4, 4]
    assert lay["back"] == [False] * 5 + [True]
    assert sorted(lay["cols"][4]) == [4, 5]


def test_overview_layout_is_deterministic_and_puts_disconnected_nodes_in_layer_zero():
    nodes = OV_NODES[:3] + [dict(OV_NODES[3], id="solo")]
    edges = OV_EDGES[:1]

    assert overview_layout(nodes, edges) == overview_layout(nodes, edges)
    assert overview_layout(nodes, edges)["layer"] == [0, 1, 0, 0]
    assert overview_svg(OV) == overview_svg(OV)


def test_overview_draws_every_node_with_a_path_and_group_colour_and_a_back_edge_below():
    out = ov_page()

    assert out.count('class="cw-ov-node') == 6 and out.count('data-path="') == 6 + 1
    assert 'data-path="src/auth.py"' in out
    assert 'data-path="src/auth.py"' in out and "cw-ov-g1" in out and "cw-ov-g2" in out
    assert "cw-ov-watch" in out and ">REVIEW<" in out
    assert out.count('<path class="cw-ov-e ') == 6
    assert "cw-ov-n-removed" in out and 'class="cw-ov-key cw-ov-n-config"' in out
    assert out.index("cw-overview-h") < out.index("cw-ov-legend") < out.index("cw-ov-chips")


@pytest.mark.parametrize("explain, heading", [(False, "System change"), (True, "How it works")])
def test_overview_heading_follows_the_mode_and_the_title_is_the_subtitle(explain, heading):
    out = ov_page(explain=explain)

    assert f'<h2 class="h2-row" id="cw-overview-h"><span>{heading}</span>' in out
    assert 'data-ov-zoom' in out and '</h2><p class="cw-ov-sub">Agent flow</p>' in out


def test_overview_sits_before_the_walkthrough_and_replaces_the_legacy_picture():
    out = pics({"pr": PR_PIC, "overview": OV, "groups": {}})

    assert out.index("cw-overview") < out.index("Walkthrough") and "pr-picture" not in out


def test_without_an_overview_the_legacy_picture_renders_and_with_neither_there_is_no_section():
    assert "pr-picture" in pics({"pr": PR_PIC, "overview": None, "groups": {}})
    none = pics({"pr": None, "overview": None, "groups": {}})
    assert "cw-overview" not in none and "pr-picture" not in none


def test_a_node_without_refs_jumps_to_its_groups_first_file():
    order, files = parsed()
    analysis = dict(ANALYSIS, groups=[{"title": "g", "why": "w", "paths": ["README.md"]}])
    out = render_html(analysis, files, TEMPLATE, now=FROZEN, order=order,
                      visuals={"pr": None, "overview": OV, "groups": {}})

    assert re.search(r'cw-ov-n-changed"[^>]*data-path="README.md" data-group="0"', out)


EVIL = '<script>alert(1)</script>" onerror="x'


def test_overview_escapes_every_model_string():
    nodes = [dict(OV_NODES[0], label=EVIL, note=EVIL, refs=[EVIL]), dict(OV_NODES[1])]
    ov = {"title": EVIL, "nodes": nodes, "edges": [dict(OV_EDGES[0], label=EVIL)],
          "notes": [{"kind": "new", "text": EVIL, "group": 0, "refs": [EVIL]}]}
    out = ov_page(ov)

    assert "<script>alert" not in out.replace("</script>", "") and 'onerror="' not in out
    assert "&lt;script&gt;" in out and "&quot; onerror=" in out


def test_overview_never_emits_a_node_id_into_markup():
    ov = dict(OV, nodes=[dict(n, id=n["id"]) for n in OV_NODES])

    assert not re.search(r"\b(agent|wrap|gate|appr)\b", overview_svg(ov).replace("Agent", ""))


def test_load_visuals_passes_ctx_so_an_unverifiable_ref_is_dropped(tmp_path):
    raw = {"overview": dict(OV, nodes=[dict(OV_NODES[0], refs=["nope.py"], group=0)] + OV_NODES[1:3] + OV_NODES[3:],
                            notes=[])}
    ctx = visuals_mod.diff_ctx(DIFF)

    clean, lines = load_visuals(_visuals_file(tmp_path, raw), 2, None, ctx)

    assert clean["overview"]["nodes"][0]["refs"] == []
    assert any("nope.py not in diff" in line for line in lines)
    assert clean["overview"]["nodes"][1]["refs"] == ["src/auth.py"]


def test_template_has_a_delegated_overview_handler_and_no_html_sink_in_it():
    template = _real_template()
    script = template.split("</script>")[-2]

    assert "'.cw-ov-node,.cw-ov-chip'" in script and "document.addEventListener('click',jump)" in script
    assert "window.__cwGoToFile(t.dataset.path)" in script and "window.__cwGoToFile=navGoToFile" in template
    for banned in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write", "onclick="):
        assert banned not in script, banned
    assert "onerror=" not in overview_svg(OV)


def test_page_opens_with_a_visible_title_and_the_pr_link():
    out = ov_page(explain=False)
    assert '<h1 class="cw-title">' in out and out.count("<h1") == 1
    link = pics(links={"pr_url": "https://x/pr/1"}, title="A <b> title")
    assert '<h1 class="cw-title">A &lt;b&gt; title</h1><p class="sub cw-title-sub"><a href="https://x/pr/1"' in link


def test_overview_svg_mid_prefixes_marker_ids_default_unchanged():
    plain, pre = overview_svg(OV), overview_svg(OV, mid="ab12cd")
    assert 'id="cw-ov-m-new"' in plain or 'id="cw-ov-m-' in plain
    ids = re.findall(r'<marker id="([^"]+)"', pre)
    assert ids and all(i.startswith("cw-ov-m-ab12cd-") for i in ids)
    assert all(f'url(#{i})' in pre for i in ids)
    assert pre.replace("ab12cd-", "") == plain


def test_overview_svg_is_namespaced_svg_for_domparser():
    import xml.etree.ElementTree as ET
    assert ET.fromstring(overview_svg(OV)).tag == "{http://www.w3.org/2000/svg}svg"


def test_file_preview_button_toggles_the_diff_and_renders_markdown_once():
    fn = _between(_real_template(), "function wireFilePreview(btn){", "\ndocument.querySelectorAll('details.hunk .hunk-preview')")
    script = f"""
const cls=new Set(), attrs={{}}; let handler, rendered=0, last, kids=[];
const pbody={{get firstChild(){{return kids[0]||null}}, append(x){{kids.push(x)}}}};
const tpl={{content:{{textContent:'# Hi'}}}};
const det={{classList:{{contains:c=>cls.has(c), toggle:(c,on)=>on?cls.add(c):cls.delete(c)}},
  querySelector:s=>s==='.cw-preview-body'?pbody:s==='template.cw-md-src'?tpl:null}};
const btn={{closest:()=>det, setAttribute:(k,v)=>{{attrs[k]=v}}, addEventListener:(_,h)=>{{handler=h}}}};
const window={{__cwMdEl:t=>{{rendered++; return t}}}};
function wireFilePreview(btn){{{fn}
wireFilePreview(btn);
const click=()=>handler({{preventDefault(){{}},stopPropagation(){{}}}});
click();
if(!cls.has('cw-previewing')||attrs['aria-pressed']!=='true'||kids[0]!=='# Hi') throw new Error('on');
click();
if(cls.has('cw-previewing')||attrs['aria-pressed']!=='false') throw new Error('off');
click();
if(rendered!==1) throw new Error('rendered '+rendered);
"""
    _node(script)


def test_file_preview_uses_the_safe_renderer_and_never_html_injection():
    template = _real_template()
    wire = _between(template, "function wireFilePreview(btn){", "\ndocument.querySelectorAll('details.hunk .hunk-preview')")

    assert "window.__cwMdEl=mdEl;" in template
    for banned in ("innerHTML", "insertAdjacentHTML", "outerHTML"):
        assert banned not in wire


_ACCESS_DOM = """
class El {
  constructor(tag) { this.tag = tag; this.kids = []; this.cls = new Set(); this.hidden = false; this._t = ''; this.disabled = false; this.attrs = {}; this.on = {}; }
  get textContent() { return this._t + this.kids.map(k => k.textContent).join(''); }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(n, f) { this.on[n] = f; }
  replaceChildren(...k) { this.kids = k; }
  append(...k) { this.kids.push(...k); }
  querySelectorAll() { return this.kids.filter(k => k.tag === 'button'); }
}
const mk = (tag, cls, text) => { const e = new El(tag); (cls || '').split(' ').filter(Boolean).forEach(c => e.cls.add(c)); e._t = text || ''; return e; };
const mkBtn = (t, cls) => mk('button', cls, t);
const calls = [], asked = [];
let reply = {ok: true, body: {agent: null}}, yes = true;
const postJson = (p, b) => { calls.push([p, b]); return Promise.resolve(reply); };
const confirm = m => { asked.push(m); return yes; };
const window = {CW_LIVE: {agent: undefined}};
"""


def _access_script():
    t = _real_template()
    return _ACCESS_DOM + "const pAccess=" + _between(t, "const pAccess=", "window.__cwOnAgent=paintAccess;")


def test_access_line_paints_the_four_agent_states_and_trust_is_confirmed_then_posted():
    _node(_access_script() + """
const eq = (a, b, m) => { if (a !== b) throw new Error(m + ': ' + JSON.stringify(a) + ' != ' + JSON.stringify(b)); };
const btns = () => pAccess.kids.filter(k => k.tag === 'button');
const off = {full: false, own_pr: false, full_off: {reason: 'not trusted', remedy: 'trust it', fixable: 'trust', repo: 'o/r', org: 'o'}, trusted: null};
paintAccess(off);
eq(pAccess.textContent.startsWith('Review only: not trusted. trust it'), true, 'off text');
eq(btns().map(b => b.textContent).join('|'), 'Trust o/r|Trust all of o', 'trust buttons');
paintAccess({full: false, full_off: {reason: 'no bwrap', remedy: 'install it', fixable: null, repo: null, org: null}, trusted: null});
eq(btns().length, 0, 'unfixable has no buttons');
paintAccess({full: true, full_off: null, trusted: {by: 'repo', value: 'o/r'}});
eq(pAccess.textContent.startsWith('Full access: trusted (o/r)'), true, 'trusted text');
eq(btns().map(b => b.textContent).join('|'), 'Stop trusting', 'stop button');
paintAccess({full: true, full_off: null, trusted: {by: 'own_pr', value: null}});
eq(pAccess.textContent, 'Full access: your PR', 'own pr');
eq(btns().length, 0, 'own pr no button');
paintAccess(undefined);
eq(pAccess.hidden, true, 'missing agent hidden');

(async () => {
  paintAccess(off);
  yes = false; btns()[0].on.click();
  eq(asked.length, 1, 'confirm asked'); eq(calls.length, 0, 'declined posts nothing');
  eq(asked[0].includes('forks'), true, 'fork warning');
  yes = true; reply = {ok: true, body: {agent: {full: true, full_off: null, trusted: {by: 'org', value: 'o'}}}};
  btns()[1].on.click();
  eq(asked[1].includes('every repo owned by o'), true, 'org warning');
  eq(JSON.stringify(calls[0]), JSON.stringify(['/trust', {scope: 'org', action: 'add'}]), 'add body');
  await new Promise(r => setTimeout(r));
  eq(pAccess.textContent.startsWith('Full access: trusted (o)'), true, 'repainted from response');
  btns()[0].on.click();
  eq(asked.length, 2, 'stop trusting has no confirm');
  eq(JSON.stringify(calls[1]), JSON.stringify(['/trust', {scope: 'org', action: 'remove'}]), 'remove body');
  reply = {ok: false, body: {error: 'nope', remedy: 'retry'}};
  paintAccess(off); btns()[0].on.click();
  await new Promise(r => setTimeout(r));
  eq(pAccess.textContent.endsWith('nope (retry)'), true, 'error shown');
  eq(btns()[0].disabled, false, 'buttons re-enabled');
})().catch(e => { console.error(e); process.exit(1); });
""")
    tpl = _real_template()
    assert "postJson('/trust'" in tpl and "confirm(" in tpl and "window.__cwOnAgent=paintAccess" in tpl
    assert "['delta','progress','outcome','agent']" in tpl
