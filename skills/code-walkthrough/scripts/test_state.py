#!/usr/bin/env python3
"""Self-check for state.py. Assert-based, no framework."""

import copy
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from state import build  # noqa: E402
from render import render_html  # noqa: E402
from validate_analysis import parse_hunks  # noqa: E402

TEMPLATE_PATH = Path(__file__).parent.parent / "assets" / "diff-review-template.html"

# A Python-syntax mirror of the template's parseHeader regex: same two numbered capture
# groups, same optional ",<count>" groups, same trailing " @@". Matching m[0] (JS) / group(0)
# (Python) on this shape is what makes a hunk "id" reconstructible byte-identically in the
# browser without any markup change.
JS_HEADER_MIRROR = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# The exact literal currently in diff-review-template.html's parseHeader.
EXPECTED_JS_REGEX_SRC = r"/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/"

DIFF_MOVED = """diff --git a/x.py b/x.py
index aaa1111..bbb2222 100644
--- a/x.py
+++ b/x.py
@@ -10,2 +10,3 @@
 context line
-old line
+new line
"""

DIFF_SHIFTED = """diff --git a/x.py b/x.py
index aaa1111..bbb2222 100644
--- a/x.py
+++ b/x.py
@@ -20,2 +20,3 @@
 context line
-old line
+new line
"""

DIFF_BODY_CHANGED = """diff --git a/x.py b/x.py
index aaa1111..bbb2222 100644
--- a/x.py
+++ b/x.py
@@ -10,2 +10,3 @@
 context line
-old line
+new line, edited
"""

# Same header and body as DIFF_MOVED; only the index line's post-image sha (bbb2222 ->
# ccc3333) differs, isolating the blob as the sole cause of a hash change.
DIFF_BLOB_CHANGED = """diff --git a/x.py b/x.py
index aaa1111..ccc3333 100644
--- a/x.py
+++ b/x.py
@@ -10,2 +10,3 @@
 context line
-old line
+new line
"""

ANALYSIS = {
    "target": "master...HEAD",
    "overview": "does a thing",
    "flow_mermaid": "",
    "files": [{"path": "x.py", "role": "does a thing",
               "hunks": [{"header": "@@ -10,2 +10,3 @@", "note": "swaps the line"}]}],
    "groups": [{"title": "g", "why": "", "paths": ["x.py"]}],
}

LINKS = {
    "repo_url": "https://github.com/crisperit/crisperit-skills",
    "pr_url": "https://github.com/crisperit/crisperit-skills/pull/482",
    "head_sha": "4b7a0f1",
    "head_pushed": True,
}


def _hunk(state, path="x.py"):
    matches = [h for h in state["hunks"] if h["path"] == path]
    assert len(matches) == 1
    return matches[0]


def test_hunk_id_is_path_tab_prefix():
    state = build(ANALYSIS, DIFF_MOVED)
    hunk = _hunk(state)

    assert hunk["prefix"] == "@@ -10,2 +10,3 @@"
    assert hunk["id"] == "x.py\t@@ -10,2 +10,3 @@"


def test_hunk_id_matches_the_js_mirror_regex():
    """The identity key built from (path, prefix) has to match what the browser reconstructs
    from parseHeader's m[0] once it carries one."""
    state = build(ANALYSIS, DIFF_MOVED)
    hunk = _hunk(state)

    header_line = "@@ -10,2 +10,3 @@"
    mirror_match = JS_HEADER_MIRROR.match(header_line)
    assert mirror_match is not None
    mirror_prefix = mirror_match.group(0)

    assert mirror_prefix == hunk["prefix"]
    assert f"x.py\t{mirror_prefix}" == hunk["id"]


def test_js_mirror_regex_is_still_accurate_against_the_template():
    """A Python suite can't execute the JS, so this pins the JS source: if parseHeader's
    regex literal drifts from validate_analysis.HUNK_PREFIX's shape, this fails loudly instead
    of the two silently disagreeing on every hunk id."""
    text = TEMPLATE_PATH.read_text()
    fn_match = re.search(r"function parseHeader\(text\)\{(.*?)\n  \}", text, re.S)
    assert fn_match, "parseHeader() not found in diff-review-template.html"
    body = fn_match.group(1)

    regex_match = re.search(r"const m=(/.*?/)\.exec\(text\);", body)
    assert regex_match, "parseHeader's regex literal has moved or changed shape"
    assert regex_match.group(1) == EXPECTED_JS_REGEX_SRC


def test_js_mirror_still_needs_the_prefix_fix():
    """parseHeader must also return prefix:m[0], the matched substring, not just the two
    numbered groups it throws away today -- otherwise hunk ids drift silently between Python
    and the browser. Written to hold once that fix lands; until then it fails on purpose."""
    text = TEMPLATE_PATH.read_text()
    fn_match = re.search(r"function parseHeader\(text\)\{(.*?)\n  \}", text, re.S)
    assert fn_match, "parseHeader() not found in diff-review-template.html"
    body = fn_match.group(1)

    return_match = re.search(r"return (.*?);", body)
    assert return_match, "parseHeader has no return statement"
    assert re.search(r"prefix\s*:\s*m\[0\]", return_match.group(1)), (
        "parseHeader's return does not carry prefix:m[0] -- hunk ids will silently mismatch "
        "between state.py and the browser"
    )


def test_no_stray_identifiers_survive_from_the_old_per_line_rendering_path():
    """draftAt and renderLine are dead once renderNotes replaces every caller (phase 1a); a
    survivor of either throws ReferenceError on the first diff-line click and takes the whole
    script block down with it."""
    text = TEMPLATE_PATH.read_text()

    assert not re.search(r"\bdraftAt\b", text)
    assert not re.search(r"\brenderLine\b", text)


def test_no_trace_of_the_deleted_fixed_panel_survives():
    """Phase 1b deleted the fixed right-hand comment panel outright (renderPanel, its markup,
    and the copy-for-Claude button) now that inline per-line rows do all the rendering. A
    surviving id, class, or function name here means either the markup or the JS wiring it was
    still half-deleted."""
    text = TEMPLATE_PATH.read_text()

    for needle in ("fb-panel", "fb-copy", "fb-toggle", "panel-open",
                   "renderPanel", "draftAt", "renderLine"):
        assert needle not in text, f"stray reference to deleted panel: {needle!r}"


def test_render_notes_no_longer_gates_the_inline_row_on_draft_state():
    """renderNotes() is the single reconciler for every note now (posted, synced, stale --
    not just local drafts), so the drafts-only filter the old per-line renderLine() used must
    be gone from its body."""
    text = TEMPLATE_PATH.read_text()
    fn_match = re.search(r"function renderNotes\(\)\{(.*?)\n  \}", text, re.S)
    assert fn_match, "renderNotes() not found in diff-review-template.html"
    body = fn_match.group(1)

    assert "'draft'" not in body


def test_template_has_the_copy_for_agent_control():
    """The page is a plain local file with no server to post a click to; Copy for agent, now in
    the comments panel, is the manual path that lets a draft reach the agent anyway. A
    source-pin like the deleted-panel tests above, so losing the control fails loudly here
    instead of silently.
    """
    text = TEMPLATE_PATH.read_text()

    assert 'id="hr-comments-agent-btn"' in text
    assert "notesForAgent" in text


def test_template_has_the_comments_panel_and_its_gh_command_control():
    """Phase B replaced the page-top banner and the per-note Post to PR mark with a
    bottom-right Comments button that opens a native dialog; the dialog also gets a
    Copy gh command control alongside Copy for agent, the fast path when a PR exists.
    """
    text = TEMPLATE_PATH.read_text()

    assert 'id="hr-comments-btn"' in text
    assert 'id="hr-comments"' in text
    assert 'id="hr-comments-gh-btn"' in text
    assert "showModal" in text


def test_no_trace_of_the_removed_top_banner_or_per_note_post_to_pr_mark():
    """hr-copy-agent (the page-top banner) and publish_requested (the per-note mark it read)
    are both gone: the panel replaced the first, and every non-stale local draft is offered
    for publishing now, not just one flagged by a separate click. The phrase "Post to PR" may
    still appear in a comment explaining what was removed and why, so this checks the actual
    stale ids/attributes rather than banning the phrase outright.
    """
    text = TEMPLATE_PATH.read_text()

    for needle in ("hr-copy-agent", "publish_requested", "'Post to PR'"):
        assert needle not in text, f"stray reference to a removed control: {needle!r}"


def test_no_trace_of_the_removed_separate_line_editor():
    """The separate new-comment editor row (openEditor, its .dline-edit box, and the
    openLineKey guard that protected it from renderNotes) collapsed into just creating an
    empty draft note and focusing its own textarea -- an inline draft renders itself, so no
    second control is needed to reach it.
    """
    text = TEMPLATE_PATH.read_text()

    for needle in ("openEditor", "clearLineRow", "openLineKey", "dline-edit"):
        assert needle not in text, f"stray reference to the removed line editor: {needle!r}"


def test_thread_reply_box_is_persistent_and_withheld_under_my_own_unposted_drafts():
    """GitHub-shaped threads: a persistent one-line Reply input sits under every thread with no
    unposted draft of mine among its notes, with no button to reveal it -- replacing the old
    per-note Reply button. My own drafts get inline autosaving editing instead (see
    test_own_draft_renders_an_editable_textarea below). The check spans the whole thread, not
    just its root, and detaches an already-rendered box explicitly rather than relying on
    placeAfter/placeIn to drop it -- those only insert what they're handed.
    """
    text = TEMPLATE_PATH.read_text()

    assert "'Reply'" not in text  # the old per-note button is gone outright
    assert "fb-reply-input" in text
    fn_match = re.search(r"function elementsForThreads\(notesArr,byId\)\{(.*?)\n  \}", text, re.S)
    assert fn_match, "elementsForThreads() not found in diff-review-template.html"
    body = fn_match.group(1)
    assert re.search(r"hasUnpostedDraft=group\.notes\.some\(n=>n\.origin===.local.&&n\.state===.draft.\);", body), (
        "elementsForThreads no longer withholds the reply box on a thread with an unposted own draft"
    )
    assert "cached.remove()" in body, (
        "a suppressed reply box must be detached from the DOM explicitly, not just left out of out[]"
    )


def test_resolve_conversation_button_only_appears_once_a_thread_has_a_gh_thread_id():
    """A thread with nothing posted yet (no gh_thread_id) has nothing to resolve, so it gets
    no Resolve conversation button -- only a thread whose root has already reached GitHub does.
    """
    text = TEMPLATE_PATH.read_text()

    assert "'Resolve conversation'" in text
    fn_match = re.search(r"function elementsForThreads\(notesArr,byId\)\{(.*?)\n  \}", text, re.S)
    assert fn_match, "elementsForThreads() not found in diff-review-template.html"
    assert "if(root.gh_thread_id) out.push(resolveBtnFor(root));" in fn_match.group(1)


def test_resolved_thread_collapses_into_a_details_summary():
    """A resolved thread (confirmed by sync-threads, or still only requested locally) renders
    collapsed in a <details> with a Resolved summary, the same idiom as the .hr-lo stray fold.
    The resolver's name is optional: the summary falls back to plain "Resolved" when
    root.resolved_by is absent, and gets a "(pending sync)" suffix while unconfirmed.
    """
    text = TEMPLATE_PATH.read_text()

    assert "hr-thread-resolved" in text

    summary_match = re.search(
        r"summary\.appendChild\(document\.createTextNode\(\s*(.*?)\)\);", text, re.S
    )
    assert summary_match, "resolved-thread summary text builder not found"
    summary_src = summary_match.group(1)

    assert re.search(r"['\"]\s*Resolved['\"]", summary_src)
    assert re.search(r"root\.resolved_by\s*\?.*?:\s*''", summary_src), (
        "resolver name must be optional, falling back to plain Resolved"
    )
    assert "pending sync" in summary_src


def test_own_draft_renders_an_editable_textarea_with_no_edit_button():
    """A draft of mine renders its body in a <textarea> rather than as static text, with no
    separate Edit button to reach it through -- autosave (wireDraftAutosave) is the only path
    to changing it now.
    """
    text = TEMPLATE_PATH.read_text()

    assert "wireDraftAutosave" in text
    assert "fb-draft-body" in text
    assert "'Edit'" not in text


def test_template_has_the_execcommand_copy_fallback():
    """navigator.clipboard is undefined on plenty of file:// pages (gated behind a secure
    context), so Copy for agent needs the deprecated execCommand path as a fallback, not just
    the modern Clipboard API.
    """
    text = TEMPLATE_PATH.read_text()

    assert "document.execCommand('copy')" in text


def test_legacy_copy_targets_the_open_dialog_and_never_reports_a_false_success():
    """Both copy buttons live inside a <dialog> opened with showModal(); everything outside an
    open modal dialog is inert, so a textarea appended straight to document.body could never
    take focus there, and execCommand('copy') would silently no-op while still returning true.
    legacyCopy must append into the open dialog when one exists, and must only trust
    execCommand's result once focus actually landed on its own textarea.
    """
    text = TEMPLATE_PATH.read_text()

    fn_match = re.search(r"function legacyCopy\(text\)\{(.*?)\n  \}", text, re.S)
    assert fn_match, "legacyCopy() not found in diff-review-template.html"
    body = fn_match.group(1)
    assert "document.querySelector('dialog[open]')" in body, (
        "legacyCopy no longer targets the open dialog, so it goes back to appending onto"
        " an inert document.body while the comments panel is open"
    )
    assert re.search(r"if\(document\.activeElement===ta\)\{", body), (
        "legacyCopy no longer gates execCommand('copy') on the textarea actually taking"
        " focus, so a failed focus can be reported as a successful copy again"
    )


def test_build_snippet_expand_label_counts_rendered_rows_not_raw_lines():
    """rows are built from rawLines[1:] (rawLines[0] is the @@ header, never turned into a
    row), so the expand label's more-lines count must be based on rows.length. Basing it on
    rawLines.length instead would overcount the revealed lines by one.
    """
    text = TEMPLATE_PATH.read_text()
    fn_match = re.search(r"function buildSnippet\(root\)\{(.*?)\n  \}", text, re.S)
    assert fn_match, "buildSnippet() not found in diff-review-template.html"
    body = fn_match.group(1)

    assert "const moreCount=rows.length-" in body
    assert "const moreCount=rawLines.length-" not in body, (
        "buildSnippet's expand-button count reverted to rawLines.length, which includes the "
        "@@ header and overcounts the revealed lines by one"
    )


def test_build_snippet_encodes_path_per_segment_for_the_view_file_link():
    """A path with a space or other URL-significant character must still produce a working
    github.com/.../blob/ URL; encodeURIComponent on the whole path would also escape the '/'
    separators the link depends on, so each segment has to be encoded on its own.
    """
    text = TEMPLATE_PATH.read_text()
    fn_match = re.search(r"function buildSnippet\(root\)\{(.*?)\n  \}", text, re.S)
    assert fn_match, "buildSnippet() not found in diff-review-template.html"
    body = fn_match.group(1)

    assert "root.path.split('/').map(encodeURIComponent).join('/')" in body
    assert "+root.path+'#L'" not in body, (
        "the View file link interpolates root.path unencoded again -- a space or other "
        "URL-significant character in the path will produce a broken github.com URL"
    )


def test_hash_is_stable_when_only_line_numbers_shift():
    moved = _hunk(build(ANALYSIS, DIFF_MOVED))
    shifted = _hunk(build(ANALYSIS, DIFF_SHIFTED))

    assert moved["prefix"] != shifted["prefix"]
    assert moved["hash"] == shifted["hash"]


def test_hash_changes_when_a_body_line_changes():
    moved = _hunk(build(ANALYSIS, DIFF_MOVED))
    changed = _hunk(build(ANALYSIS, DIFF_BODY_CHANGED))

    assert moved["hash"] != changed["hash"]


def test_hash_changes_when_only_the_blob_changes():
    """Guards blob's participation in _hunk_hash: the other two hash tests both hold the
    index line constant, so a change that dropped blob from the hash entirely would still
    pass them."""
    same_body = _hunk(build(ANALYSIS, DIFF_MOVED))
    blob_changed = _hunk(build(ANALYSIS, DIFF_BLOB_CHANGED))

    assert same_body["prefix"] == blob_changed["prefix"]
    assert same_body["hash"] != blob_changed["hash"]


def test_build_round_trips_through_json():
    state = build(ANALYSIS, DIFF_MOVED, links=LINKS)

    round_tripped = json.loads(json.dumps(state))

    assert round_tripped == state


def test_meta_id_is_stable_and_never_from_head_sha():
    state_one = build(ANALYSIS, DIFF_MOVED, links=LINKS)
    other_links = {**LINKS, "head_sha": "deadbeef"}
    state_two = build(ANALYSIS, DIFF_MOVED, links=other_links)

    assert state_one["meta"]["id"] == state_two["meta"]["id"]
    assert state_one["meta"]["id"] != state_one["meta"]["head_sha"]


def test_meta_head_pushed_comes_from_links():
    pushed = build(ANALYSIS, DIFF_MOVED, links=LINKS)
    not_pushed = build(ANALYSIS, DIFF_MOVED, links={**LINKS, "head_pushed": False})

    assert pushed["meta"]["head_pushed"] is True
    assert not_pushed["meta"]["head_pushed"] is False


def test_meta_dirty_is_always_server_owned_false_and_null():
    state = build(ANALYSIS, DIFF_MOVED, links=LINKS)

    assert state["meta"]["dirty"] is False
    assert state["meta"]["dirty_at"] is None


def test_prior_notes_are_carried_forward():
    prior = {"notes": [{"id": "n-1", "body": "leave this"}]}

    state = build(ANALYSIS, DIFF_MOVED, prior=prior)

    assert state["notes"] == [{"id": "n-1", "body": "leave this"}]


def test_no_prior_means_no_notes():
    state = build(ANALYSIS, DIFF_MOVED)

    assert state["notes"] == []


def test_prior_resolutions_are_carried_forward():
    prior = {"resolutions": {"T1": {"outcome": "none"}}}

    state = build(ANALYSIS, DIFF_MOVED, prior=prior)

    assert state["resolutions"] == {"T1": {"outcome": "none"}}


def test_no_prior_means_no_resolutions():
    state = build(ANALYSIS, DIFF_MOVED)

    assert state["resolutions"] == {}


def test_built_state_has_no_requests_key():
    state = build(ANALYSIS, DIFF_MOVED, prior={"requests": [{"id": "r-1"}]})

    assert "requests" not in state


def test_set_hash_changes_when_any_hunk_hash_changes():
    a = build(ANALYSIS, DIFF_MOVED)
    b = build(ANALYSIS, DIFF_BODY_CHANGED)

    assert a["meta"]["set_hash"] != b["meta"]["set_hash"]


def test_no_index_line_falls_back_to_empty_blob():
    diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
            "@@ -1,1 +1,1 @@\n-a\n+b\n")

    state = build(copy.deepcopy(ANALYSIS), diff)

    assert state["files"][0]["blob"] == ""


RENDER_TEMPLATE = ("<html><title><!-- DIFF_TITLE --></title>"
                    "<body><!-- DIFF_CONTENT --><!-- HR_STATE --></body></html>")


def test_render_state_embeds_hr_state_and_round_trips_script_and_entity_text():
    state = build(ANALYSIS, DIFF_MOVED)
    state["notes"] = [
        {"id": "n-1", "body": "</script><img onerror=alert(1)>"},
        {"id": "n-2", "body": "the `&lt;div&gt;` tag"},
    ]
    _order, files = parse_hunks(DIFF_MOVED)

    out = render_html(ANALYSIS, files, RENDER_TEMPLATE, state=state)

    assert 'id="hr-state"' in out
    start = out.index('id="hr-state">') + len('id="hr-state">')
    end = out.index("</script>", start)
    blob = out[start:end]

    assert "</script>" not in blob
    decoded = json.loads(blob)  # valid JSON as-is, no client-side un-escape step
    assert decoded["notes"][0]["body"] == "</script><img onerror=alert(1)>"
    # A literal "&lt;" in a note body must survive untouched -- the old &lt;-entity scheme's
    # client-side replace(/&lt;/g,'<') would have corrupted exactly this text.
    assert decoded["notes"][1]["body"] == "the `&lt;div&gt;` tag"


def test_render_without_state_leaves_the_placeholder_untouched():
    _order, files = parse_hunks(DIFF_MOVED)

    out = render_html(ANALYSIS, files, RENDER_TEMPLATE)

    assert "<!-- HR_STATE -->" in out
    assert 'id="hr-state"' not in out


def test_notes_toggle_also_hides_group_why_and_its_panel():
    """The show-notes toggle rule must also drop a group's why line, alongside the existing
    per-hunk notes it already hid. The group's whole diagram panel (flow, call graph, or
    both behind a tab bar) is a separate rule -- see the off-screen test below -- since it
    must not use display:none."""
    text = TEMPLATE_PATH.read_text()
    rule_match = re.search(
        r"body:not\(\.show-notes\) \.hunk-note-hunk,\n\s*"
        r"body:not\(\.show-notes\) \.wt-why\{display:none\}", text)
    assert rule_match, "the show-notes toggle no longer hides .wt-why"


def test_notes_toggle_moves_the_group_panel_off_screen_not_display_none():
    """A group's diagram panel (.wt-panel) holds a mermaid block that renders eagerly at
    load: display:none on it when a reader's stored toggle state starts off would make
    mermaid measure it as zero and collapse the diagram permanently (see the CSS comment
    above the rule). It must go off-screen instead, scoped to a group's own panel so the
    page-level FLOW above the Walkthrough heading, which carries no .wt-panel, is never
    affected."""
    text = TEMPLATE_PATH.read_text()
    rule_match = re.search(
        r"body:not\(\.show-notes\) h3\.wt-group ~ \.wt-panel\{([^}]*)\}", text)
    assert rule_match, "the scoped group-panel off-screen rule is missing"
    rule_body = rule_match.group(1)
    assert "display:none" not in rule_body
    assert "position:absolute" in rule_body and "left:-99999px" in rule_body


def test_inactive_tab_panel_goes_off_screen_not_display_none():
    """The tab bar's inactive panel (.wt-tabpanel.wt-tab-off) may still hold an unrendered
    call-graph diagram (see wireGroupTabs' lazy render), so it must use the same off-screen
    technique as the group panel above rather than display:none, or mermaid measures its
    labels as zero and collapses the diagram permanently the first time that tab is picked."""
    text = TEMPLATE_PATH.read_text()
    rule_match = re.search(r"\.wt-tabpanel\.wt-tab-off\{([^}]*)\}", text)
    assert rule_match, "the inactive-tab off-screen rule is missing"
    rule_body = rule_match.group(1)
    assert "display:none" not in rule_body
    assert "position:absolute" in rule_body and "left:-99999px" in rule_body


def test_vd_ctl_hidden_attribute_has_a_specificity_override():
    """.vd-ctl's own display:flex is an author-normal rule, which always beats the UA
    stylesheet's [hidden]{display:none} regardless of specificity (cascade origin is
    resolved before specificity) -- the same footgun .dl[hidden]{display:none} already works
    around elsewhere in this template. Without an equivalent override, wireGroupTabs setting
    ctl.hidden while the flow tab is active would leave the level toggle visible anyway."""
    text = TEMPLATE_PATH.read_text()
    assert re.search(r"\.wt-tabs \.vd-ctl\[hidden\]\{display:none\}", text), (
        "no specificity override for .vd-ctl[hidden] inside .wt-tabs")


if __name__ == "__main__":
    tests = [fn for name, fn in list(globals().items())
             if name.startswith("test_") and callable(fn)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
