#!/usr/bin/env python3
"""Self-check for state.py."""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from state import build  # noqa: E402
from render import render_html  # noqa: E402
from validate_analysis import parse_hunks  # noqa: E402

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


@pytest.mark.parametrize("other, same_prefix, same_hash", [
    (DIFF_SHIFTED, False, True),
    (DIFF_BODY_CHANGED, True, False),
    # Guards blob's participation in _hunk_hash: the other rows hold the index line constant,
    # so a change that dropped blob from the hash entirely would still pass them.
    (DIFF_BLOB_CHANGED, True, False),
])
def test_hunk_hash_identity(other, same_prefix, same_hash):
    moved = _hunk(build(ANALYSIS, DIFF_MOVED))
    changed = _hunk(build(ANALYSIS, other))

    assert (moved["prefix"] == changed["prefix"]) is same_prefix
    assert (moved["hash"] == changed["hash"]) is same_hash


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


@pytest.mark.parametrize("key, prior, expected", [
    ("notes", {"notes": [{"id": "n-1", "body": "leave this"}]}, [{"id": "n-1", "body": "leave this"}]),
    ("notes", None, []),
    ("resolutions", {"resolutions": {"T1": {"outcome": "none"}}}, {"T1": {"outcome": "none"}}),
    ("resolutions", None, {}),
])
def test_prior_notes_and_resolutions_carry_forward(key, prior, expected):
    state = build(ANALYSIS, DIFF_MOVED, prior=prior)

    assert state[key] == expected


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
