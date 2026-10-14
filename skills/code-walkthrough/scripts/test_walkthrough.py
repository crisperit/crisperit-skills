#!/usr/bin/env python3
"""Self-check for walkthrough.py. Assert-based."""

import base64
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from walkthrough import (  # noqa: E402
    IMAGE_CAP,
    MARKDOWN_CAP,
    SMALL_DIFF_LINES,
    caller_depth,
    depth_chip,
    render_html,
    rename_index,
    rename_note,
    story,
)
from validate_analysis import parse_hunks, check_rendered  # noqa: E402
from cw_testlib import git, require_git  # noqa: E402


def flat(order, files):
    """One untitled group holding every file, which is what an analysis with no groups
    produces. Tests about rendering use this so they say nothing about grouping."""
    return story(order, files, None, None)

DIFF = """diff --git a/src/auth.py b/src/auth.py
index 111..222 100644
--- a/src/auth.py
+++ b/src/auth.py
@@ -40,4 +40,4 @@ def load_session():
     token = store.get("access")
-    if not token:
-        return None
+    if not token:
+        return load_from_refresh()
     return parse(token)
@@ -58,2 +59,3 @@ def load_from_refresh():
     session = store.refresh()
+    cache.pop(session.user_id, None)
     return session
diff --git a/README.md b/README.md
index 333..444 100644
--- a/README.md
+++ b/README.md
@@ -1,1 +1,1 @@
-# old
+# new
"""

ANALYSIS = {
    "target": "master...HEAD",
    "files": [
        {"path": "src/auth.py", "role": "Session loading falls back to the refresh token.",
         "hunks": [{"header": "@@ -40,4 +40,4 @@", "note": "Falls back instead of re-login."},
                   {"header": "@@ -58,2 +59,3 @@", "note": "Drops the cached session."}]},
        {"path": "README.md", "role": "Title reworded.",
         "hunks": [{"header": "@@ -1,1 +1,1 @@", "note": "One heading."}]},
    ],
}


def parsed():
    return parse_hunks(DIFF)


def test_line_counts_come_from_the_diff_not_the_analysis():
    _order, files = parsed()

    assert files["src/auth.py"]["added"] == 3
    assert files["src/auth.py"]["removed"] == 2
    assert files["README.md"]["added"] == 1
    assert files["README.md"]["removed"] == 1


def test_every_diff_line_is_its_own_span_with_the_right_class():
    order, files = parsed()
    out = render_html(flat(order, files), files, {e["path"]: e for e in ANALYSIS["files"]})

    first = out.split("</details>")[0]
    pre = first.split('<pre class="diff">')[1].split("</pre>")[0]
    kinds = re.findall(r'<span class="([hacd])">', pre)
    # One header, then the hunk's own lines in order: context, two removed, two added, context.
    assert kinds == ["h", "c", "d", "d", "a", "a", "c"], kinds
    # The comment code counts lines by walking these spans, so one span may never hold two.
    assert "\n" not in pre.replace("</span>\n<span", "")


def test_html_carries_the_markup_the_comment_code_reads():
    order, files = parsed()
    out = render_html(flat(order, files), files, {e["path"]: e for e in ANALYSIS["files"]})

    for path in order:
        assert f'<span class="hunk-path">{path}</span>' in out
    assert out.count('<details class="hunk"') == 2
    assert out.count('<pre class="diff">') == 3  # two hunks in auth.py, one in README
    assert '<p class="hunk-note">Session loading falls back to the refresh token.</p>' in out


def test_line_kinds_add_up_to_the_spans_the_header_declares():
    # The page walks these spans to number lines: context advances both sides, added the new
    # side, removed the old. If context+removed does not equal the old span the header declares,
    # or context+added the new span, the walk drifts and every comment below it lands on the
    # wrong line of a real pull request. Verified the same way on a real 76-file diff.
    _order, files = parsed()

    for path, entry in files.items():
        for hunk in entry["hunks"]:
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", hunk["header"])
            assert m, hunk["header"]
            old_span = 1 if m.group(2) is None else int(m.group(2))
            new_span = 1 if m.group(4) is None else int(m.group(4))
            kinds = [kind for kind, _text in hunk["lines"]]
            c, a, d = kinds.count("c"), kinds.count("a"), kinds.count("d")
            assert c + d == old_span, (path, hunk["header"], kinds)
            assert c + a == new_span, (path, hunk["header"], kinds)


def test_hunk_header_keeps_its_trailing_function_context():
    order, files = parsed()
    out = render_html(flat(order, files), files, {})

    # The prefix alone is what validate_analysis matches on; the reader wants the whole line.
    assert '<span class="h">@@ -40,4 +40,4 @@ def load_session():</span>' in out


def test_html_escapes_diff_content_and_paths():
    diff = ('diff --git a/x.ts b/x.ts\n--- a/x.ts\n+++ b/x.ts\n'
            '@@ -1,1 +1,1 @@\n+const a = b < c && d > e;\n')
    order, files = parse_hunks(diff)
    out = render_html(flat(order, files), files, {})

    assert "&lt; c &amp;&amp; d &gt;" in out
    assert "< c && d >" not in out


def test_generated_output_passes_the_rendered_coverage_check():
    order, files = parsed()
    by_path = {e["path"]: e for e in ANALYSIS["files"]}

    assert not check_rendered(DIFF, render_html(flat(order, files), files, by_path))


def test_a_file_the_analysis_forgot_is_still_rendered():
    order, files = parsed()
    out = render_html(flat(order, files), files, {"src/auth.py": ANALYSIS["files"][0]})

    # A missing entry costs its prose, never its presence: a file absent from the walkthrough
    # reads as a file that was not touched.
    assert '<span class="hunk-path">README.md</span>' in out
    assert not check_rendered(DIFF, out)


def test_extra_hunks_beyond_the_analysis_still_render_without_a_note():
    order, files = parsed()
    short = {"path": "src/auth.py", "role": "r",
             "hunks": [{"header": "@@ -40,7 +40,8 @@", "note": "only the first"}]}
    out = render_html(flat(order, files), files, {"src/auth.py": short})

    assert out.count('<pre class="diff">') == 3
    assert "only the first" in out


def test_binary_change_gets_an_entry_and_no_pre_block():
    diff = "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
    order, files = parse_hunks(diff)
    out = render_html(flat(order, files), files, {})

    assert '<span class="hunk-path">logo.png</span>' in out
    assert '<pre class="diff">' not in out
    assert "No textual diff" in out


def test_no_newline_marker_is_dropped_so_line_counting_stays_exact():
    diff = ('diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n'
            '@@ -1,2 +1,2 @@\n-old\n\\ No newline at end of file\n+new\n')
    _order, files = parse_hunks(diff)
    kinds = [kind for kind, _text in files["x.py"]["hunks"][0]["lines"]]

    assert kinds == ["d", "a"]
    assert files["x.py"]["added"] == 1 and files["x.py"]["removed"] == 1


def test_open_count_expands_the_front_of_the_reading_order():
    # Both files are pushed past SMALL_DIFF_LINES so the size rule can't also explain the
    # result -- this test is only about the reading-order rule.
    diff = ("diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
            f"@@ -0,0 +1,{SMALL_DIFF_LINES} @@\n" +
            "\n".join(f"+line{i}" for i in range(SMALL_DIFF_LINES)) + "\n" +
            "diff --git a/src/auth.py b/src/auth.py\n--- a/src/auth.py\n+++ b/src/auth.py\n"
            f"@@ -0,0 +1,{SMALL_DIFF_LINES} @@\n" +
            "\n".join(f"+line{i}" for i in range(SMALL_DIFF_LINES)) + "\n")
    order, files = parse_hunks(diff)
    groups = story(order, files, [{"title": "Docs first", "paths": ["README.md"]},
                                  {"title": "Then the code", "paths": ["src/auth.py"]}], None)
    out = render_html(groups, files, {}, open_count=1)

    assert out.count('<details class="hunk" open>') == 1
    opened = out.split('<details class="hunk" open>')[1]
    assert '<span class="hunk-path">README.md</span>' in opened.split("</details>")[0]


def test_open_count_zero_leaves_a_large_file_collapsed():
    # Both files in DIFF are small, so they auto-expand regardless of open_count (see below);
    # a file has to clear SMALL_DIFF_LINES to test the position-based rule in isolation.
    diff = ("diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n"
            f"@@ -0,0 +1,{SMALL_DIFF_LINES} @@\n" +
            "\n".join(f"+line{i}" for i in range(SMALL_DIFF_LINES)) + "\n")
    order, files = parse_hunks(diff)

    assert " open>" not in render_html(flat(order, files), files, {}, open_count=0)


def test_small_diff_files_expand_regardless_of_open_count():
    # README.md and src/auth.py are both well under SMALL_DIFF_LINES, so they expand even with
    # open_count=0 -- the reading-order rule above and this size rule are independent.
    order, files = parsed()
    out = render_html(flat(order, files), files, {}, open_count=0)

    assert out.count('<details class="hunk" open>') == 2


LINKS = {
    "head_pushed": True,
    "files": [{"path": "src/auth.py", "diff_url": "https://gh/pull/1/files#diff-abc",
               "hunks": [{"header": "@@ -40,4 +40,4 @@", "url": "https://gh/blob/s/a.py#L40-L43"},
                         {"header": "@@ -58,2 +59,3 @@", "url": "https://gh/blob/s/a.py#L59-L61"}]}],
}


def test_the_only_link_inside_a_summary_is_the_file_name():
    # A link inside a <summary> is a fight between opening the block and following it, which
    # the template settles with a stopPropagation handler. Nothing else may join it there:
    # the hunk permalinks stay on their notes, below.
    order, files = parsed()
    by_path = {e["path"]: e for e in ANALYSIS["files"]}
    out = render_html(flat(order, files), files, by_path, links=LINKS)

    for chunk in out.split("<summary>")[1:]:
        head = chunk.split("</summary>")[0]
        assert "https://gh/blob" not in head
        assert head.count("<a ") <= 1


def test_the_file_name_itself_is_the_link_to_the_file_on_the_pr():
    order, files = parsed()
    by_path = {e["path"]: e for e in ANALYSIS["files"]}
    out = render_html(flat(order, files), files, by_path, links=LINKS)

    assert "view in PR" not in out  # 45 copies of it on a real diff
    assert ("<span class=\"hunk-path\"><a href=\"https://gh/pull/1/files#diff-abc\""
            " target=\"_blank\" rel=\"noopener noreferrer\" title=\"Open in PR\">"
            "src/auth.py</a></span>") in out
    assert ">L40-L43</a>" in out
    # The page is opened from file://; a link that replaces it costs the reader their notes.
    assert out.count('rel="noopener noreferrer"') == 3


def test_the_path_span_still_reads_as_the_bare_path_for_the_comment_js():
    # The page reads .hunk-path's textContent as the literal path a PR comment is filed
    # against, so the anchor has to be inside the span, never around it.
    order, files = parsed()
    by_path = {e["path"]: e for e in ANALYSIS["files"]}
    out = render_html(flat(order, files), files, by_path, links=LINKS)

    span = out.split('<span class="hunk-path">')[1].split("</span>")[0]
    assert re.sub(r"<[^>]+>", "", span) == "src/auth.py"


def test_a_file_link_only_renders_for_a_pushed_head():
    order, files = parsed()
    by_path = {e["path"]: e for e in ANALYSIS["files"]}
    out = render_html(flat(order, files), files, by_path, links={**LINKS, "head_pushed": False})

    assert '<span class="hunk-path">src/auth.py</span>' in out
    # Those urls 404 until the commit exists on the remote.
    assert "https://gh" not in out


def test_walkthrough_carries_the_marker_the_section_check_looks_for():
    order, files = parsed()

    assert render_html(flat(order, files), files, {}).startswith("<!-- code-walkthrough:walkthrough -->")


# ---- reading order ----

# One symbol per file, wired caller -> callee down the chain: api calls service, service calls
# store, store calls util.
SYMDELTA = {
    "language": "python",
    "nodes": [
        {"id": "src/api.py:handle", "kind": "symbol", "file": "src/api.py"},
        {"id": "src/service.py:run", "kind": "symbol", "file": "src/service.py"},
        {"id": "src/store.py:get", "kind": "symbol", "file": "src/store.py"},
        {"id": "src/util.py:fmt", "kind": "symbol", "file": "src/util.py"},
    ],
    "edges": [
        {"source": "src/api.py:handle", "target": "src/service.py:run"},
        {"source": "src/service.py:run", "target": "src/store.py:get"},
        {"source": "src/store.py:get", "target": "src/util.py:fmt"},
    ],
}


def _chain_files(*paths):
    return {p: {"hunks": [], "added": 1, "removed": 0} for p in paths}


def test_a_caller_reads_before_the_thing_it_calls():
    paths = ["src/store.py", "src/api.py", "src/service.py"]
    depth = caller_depth(SYMDELTA, paths)

    assert depth["src/api.py"] < depth["src/service.py"] < depth["src/store.py"]


@pytest.mark.parametrize("symdelta, paths", [
    # two symbols in the same file do not order it against itself
    ({"language": "go", "nodes": [
        {"id": "pkg/a.go:F", "kind": "symbol", "file": "pkg/a.go"},
        {"id": "pkg/a.go:G", "kind": "symbol", "file": "pkg/a.go"},
    ], "edges": [{"source": "pkg/a.go:F", "target": "pkg/a.go:G"}]}, ["pkg/a.go"]),
    # a pkg node carries no file, so its edge is dropped
    ({"language": "go", "nodes": [
        {"id": "pkg/a", "kind": "pkg", "label": "a", "depth": 0},
        {"id": "pkg/a.go:F", "kind": "symbol", "file": "pkg/a.go"},
    ], "edges": [{"source": "pkg/a", "target": "pkg/a.go:F"}]}, ["pkg/a.go"]),
    ({"language": None, "reason": "unsupported language: rb"}, ["a.py", "b.py"]),
    ({"language": "python",
      "nodes": [{"id": "a.py:f", "kind": "symbol", "file": "a.py"}], "edges": []}, ["a.py"]),
], ids=["same-file-symbols", "pkg-node", "language-null", "no-edges"])
def test_caller_depth_yields_no_data(symdelta, paths):
    assert caller_depth(symdelta, paths) == {}


def test_a_call_cycle_still_terminates_and_places_every_file():
    symdelta = {"language": "python", "nodes": [
        {"id": "a.py:f", "kind": "symbol", "file": "a.py"},
        {"id": "b.py:g", "kind": "symbol", "file": "b.py"},
    ], "edges": [{"source": "a.py:f", "target": "b.py:g"},
                 {"source": "b.py:g", "target": "a.py:f"}]}
    depth = caller_depth(symdelta, ["a.py", "b.py"])

    assert set(depth) == {"a.py", "b.py"}


def test_a_file_symdelta_never_saw_sorts_after_every_placed_one():
    files = _chain_files("src/api.py", "src/service.py", "src/store.py", "notes.md")
    [(_title, _why, ordered)] = story(list(files), files, None, SYMDELTA)

    assert ordered[-1] == "notes.md"


def test_symdelta_bailing_on_the_language_degrades_to_no_reordering():
    """No depth data at all falls back to the pre-symdelta tie-break: size, then path -- the
    same fallback a run with no symdelta.json ever had."""
    files = _chain_files("b.py", "a.py")
    symdelta = {"language": None, "reason": "unsupported language: rb"}
    [(_title, _why, ordered)] = story(list(files), files, None, symdelta)

    assert ordered == ["a.py", "b.py"]


def test_tests_sink_inside_their_own_group_not_to_the_bottom_of_the_page():
    files = _chain_files("src/api.py", "src/api_test.py", "src/store.py")
    groups = story(list(files), files, [
        {"title": "The api", "paths": ["src/api.py", "src/api_test.py"]},
        {"title": "The store", "paths": ["src/store.py"]},
    ], SYMDELTA)

    assert [paths for _t, _w, paths in groups] == [["src/api.py", "src/api_test.py"],
                                                  ["src/store.py"]]


def test_group_order_is_the_analysis_order_not_the_diff_order():
    files = _chain_files("a.py", "b.py")
    groups = story(["a.py", "b.py"], files, [{"title": "Second thing", "paths": ["b.py"]},
                                             {"title": "First thing", "paths": ["a.py"]}], None)

    assert [title for title, _w, _p in groups] == ["Second thing", "First thing"]


def test_a_file_no_group_claimed_lands_in_a_trailing_catch_all():
    files = _chain_files("a.py", "orphan.py")
    groups = story(["a.py", "orphan.py"], files, [{"title": "Known", "paths": ["a.py"]}], None)

    assert groups[-1] == ("Everything else", "", ["orphan.py"])


def test_a_group_claiming_a_file_twice_renders_it_once():
    files = _chain_files("a.py")
    groups = story(["a.py"], files, [{"title": "One", "paths": ["a.py"]},
                                     {"title": "Two", "paths": ["a.py"]}], None)

    assert [paths for _t, _w, paths in groups] == [["a.py"]]


def test_a_group_naming_a_file_outside_the_diff_does_not_invent_a_block():
    files = _chain_files("a.py")
    groups = story(["a.py"], files, [{"title": "Stale", "paths": ["deleted-long-ago.py"]},
                                     {"title": "Real", "paths": ["a.py"]}], None)

    assert [title for title, _w, _p in groups] == ["Real"]


def test_no_groups_at_all_renders_one_untitled_block_with_no_heading():
    order, files = parsed()
    out = render_html(flat(order, files), files, {})

    assert "wt-group" not in out


def test_group_titles_reach_the_page():
    order, files = parsed()
    groups = story(order, files, [{"title": "Session handling", "why": "the point of the PR",
                                   "paths": ["src/auth.py"]}], None)

    assert "Session handling" in render_html(groups, files, {})
    # The catch-all has to be named once something else is, or README looks like it belongs
    # to the group above it.
    assert "Everything else" in render_html(groups, files, {})


def test_group_heading_carries_an_empty_viewed_placeholder():
    # The template's JS fills this live from localStorage; the render knows nothing about
    # viewed state, so it ships empty, same convention as .hunk-count.
    order, files = parsed()
    groups = story(order, files, [{"title": "Session handling", "paths": ["src/auth.py"]}], None)

    assert '<span class="wt-viewed"></span>' in render_html(groups, files, {})


def test_every_hunk_summary_carries_a_viewed_checkbox():
    order, files = parsed()
    out = render_html(flat(order, files), files, {})

    assert out.count('<label class="hunk-viewed"') == 2
    assert out.count('<input type="checkbox" class="hunk-viewed-cb">') == 2


# ---- group headings and picture slots ----

def test_group_headings_carry_no_id_or_style():
    order, files = parsed()
    groups = story(order, files, [{"title": "Session handling", "paths": ["src/auth.py"]},
                                   {"title": "Docs", "paths": ["README.md"]}], None)
    out = render_html(groups, files, {})

    assert out.count('<h3 class="wt-group">') == 2
    assert "wt-group-" not in out and "wt-badge" not in out


def test_each_group_has_one_picture_slot_between_its_why_line_and_its_first_file():
    order, files = parsed()
    groups = story(order, files, [{"title": "Session handling", "why": "the point",
                                   "paths": ["src/auth.py"]},
                                  {"title": "Docs", "paths": ["README.md"]}], None)
    out = render_html(groups, files, {})

    assert re.findall(r"<!-- cw-picture:(\d+) -->", out) == ["0", "1"]
    why = out.index('<p class="wt-why">')
    assert why < out.index("<!-- cw-picture:0 -->") < out.index('<details class="hunk"')
    assert out.index("<!-- cw-picture:1 -->") > out.index("Docs")


def test_the_catch_all_group_gets_no_picture_slot_and_a_skipped_group_keeps_later_indices():
    order, files = parsed()

    assert "cw-picture:" not in render_html(flat(order, files), files, {})

    groups = story(order, files, [{"title": "Gone", "paths": ["deleted.py"]},
                                  {"title": "Docs", "paths": ["README.md"]}], None)
    out = render_html(groups, files, {})

    assert [g.idx for g in groups] == [1, None]
    assert re.findall(r"<!-- cw-picture:(\d+) -->", out) == ["1"]


def test_no_group_diagram_or_tab_markup_is_emitted():
    files = _chain_files("src/api.py", "src/store.py")
    groups = story(list(files), files, [
        {"title": "API", "paths": ["src/api.py"]},
        {"title": "Store", "paths": ["src/store.py"]}], None)
    out = render_html(groups, files, {})

    for gone in ("mermaid", "svgbox", "wt-tab", "vd-symbols", "vd-max-btn", 'class="map"'):
        assert gone not in out


# ---- complexity ----

def _cx(path, peak, jump=None):
    return {"files": {path: {"before": 0, "after": 0, "delta": 0,
                             "peak": peak, "jump": jump, "symbols": []}}}


CX = _cx("src/auth.py",
         peak={"name": "load_session", "before": 2, "after": 9, "delta": 7,
               "existed": True, "touched": True, "depth": 1},
         jump={"name": "load_session", "before": 2, "after": 9, "delta": 7,
               "existed": True, "touched": True, "depth": 1})


def test_a_moved_function_names_itself_and_its_movement_in_words():
    order, files = parsed()
    out = render_html(flat(order, files), files, {}, complexity=CX)

    assert '<span class="hunk-cx up"' in out
    assert "load_session 2→9 branches" in out
    # The old "9 cx +7" vocabulary, and the separate "Complexity rose:" line restating the
    # same fact underneath, are both gone: one statement, in words, is enough.
    assert "9 cx" not in out
    assert "Complexity rose" not in out
    assert "hunk-cx gone" not in out


def _peak(name, before, after, existed=True, depth=1, **extra):
    return {"name": name, "before": before, "after": after, "delta": after - before,
            "existed": existed, "touched": True, "depth": depth, **extra}


# (complexity input, strings the page must carry, strings it must not)
CX_CHIP_ROWS = {
    # A brand-new function born already hairy is worth a beat even though nothing "moved": it
    # has no before to move from. NOTEWORTHY_CX is 10, so 11 clears it.
    "new hairy function": (
        _cx("src/auth.py", peak=_peak("resolveSource", 0, 11, existed=False, depth=2)),
        ['<span class="hunk-cx flat"', "resolveSource 11 branches"], ["hunk-cx gone"]),
    # The bug report: main.go's `serve` sat at 31 before and after this diff (delta 0, but
    # `touched` true since a hunk landed inside it) and still showed a bare "31 cx". A number
    # this diff did not move is not news, no matter how high it already was.
    "unmoved pre-existing peak": (
        _cx("src/auth.py", peak=_peak("serve", 31, 31)), [], ["hunk-cx"]),
    # Otherwise every file in a diff carries one: the measured 13-file commit put a chip on all
    # 13, four of them reading "1 cx" for a new getter.
    "unmoved below the noteworthy line": (
        _cx("src/auth.py", peak=_peak("small", 3, 3)), [], ["hunk-cx"]),
    "falling function": (
        _cx("src/auth.py", peak=_peak("load_session", 20, 6), jump=_peak("load_session", 20, 6)),
        ['<span class="hunk-cx down"', "load_session 20→6 branches"], []),
    # Real output from PR 2498: distributionChannelResolverChain.Resolve's branch count fell
    # 3->0 because the function was deleted, not because this diff improved anything.
    "removed function": (
        _cx("src/auth.py", peak=None,
            jump=_peak("distributionChannelResolverChain.Resolve", 3, 0, depth=0, removed=True)),
        ['<span class="hunk-cx gone"', "distributionChannelResolverChain.Resolve removed"], ["→"]),
    # Stale complexity.json from before the "removed" key existed: existed=True with after==0
    # is the only signal available, and it alone is enough to suppress the "N→0" bug.
    "removed function with no removed key": (
        _cx("src/auth.py", peak=None, jump=_peak("ipString", 2, 0, depth=0)),
        ['<span class="hunk-cx gone"', "ipString removed"], ["→"]),
}


@pytest.mark.parametrize("complexity, present, absent", CX_CHIP_ROWS.values(), ids=CX_CHIP_ROWS.keys())
def test_complexity_chip_rows(complexity, present, absent):
    order, files = parsed()
    out = render_html(flat(order, files), files, {}, complexity=complexity)

    for text in present:
        assert text in out
    for text in absent:
        assert text not in out


def test_a_file_the_change_touched_nothing_branching_in_gets_no_chip():
    order, files = parsed()
    out = render_html(flat(order, files), files, {}, complexity=CX)

    readme = out.split('<span class="hunk-path">README.md</span>')[1]
    assert "hunk-cx" not in readme.split("</details>")[0]


def test_deep_nesting_is_its_own_chip_shown_at_the_threshold():
    order, files = parsed()
    deep = _cx("src/auth.py", peak={"name": "load_session", "before": 9, "after": 9, "delta": 0,
                                    "existed": True, "touched": True, "depth": 4})
    out = render_html(flat(order, files), files, {}, complexity=deep)

    assert "nested 4 deep" in out
    assert depth_chip(deep["files"]["src/auth.py"]) == "nested 4 deep"


@pytest.mark.parametrize("entry, expected", [
    ({"peak": {"name": "x", "depth": 3}}, ""),
    ({"peak": {"name": "x", "depth": 5, "removed": True}}, ""),
    ({"peak": {"name": "x", "depth": 4}}, "nested 4 deep"),
], ids=["shallow", "removed-function", "at-threshold"])
def test_depth_chip(entry, expected):
    assert depth_chip(entry) == expected


# ---- summary layout: path wraps, chips and the bar move into their own meta row ----

def test_a_blank_note_emits_no_paragraph_even_when_the_hunk_has_a_permalink():
    # The link on its own is not worth a line: it says the same as the @@ header below it, and
    # most hunks carry no note, so it left a column of bare L20-L26 links down the page.
    order, files = parsed()
    blank = {"path": "src/auth.py", "role": "Still has a role.",
             "hunks": [{"header": "@@ -40,4 +40,4 @@", "note": ""},
                       {"header": "@@ -58,2 +59,3 @@", "note": ""}]}
    out = render_html(flat(order, files), files, {"src/auth.py": blank}, links=LINKS)

    assert "hunk-note-hunk" not in out
    assert ">L40-L43</a>" not in out
    assert "Still has a role." in out  # the file's role line is untouched


def test_only_per_hunk_notes_carry_the_class_the_explanations_toggle_hides():
    order, files = parsed()
    by_path = {e["path"]: e for e in ANALYSIS["files"]}
    out = render_html(flat(order, files), files, by_path)

    roles = [ln for ln in out.splitlines() if 'class="hunk-note"' in ln]
    assert roles  # the file's one-line role stays visible with explanations off
    assert all("hunk-note-hunk" not in ln for ln in roles)
    assert 'class="hunk-note hunk-note-hunk"' in out


def test_the_stat_stays_in_the_summary_and_the_bar_is_gone_from_html():
    order, files = parsed()
    out = render_html(flat(order, files), files, {}, complexity=CX)

    summary = out.split("<summary>")[1].split("</summary>")[0]
    assert "hunk-stat" in summary
    assert "hunk-cx" not in summary  # the chips render in the meta row below it
    meta = out.split('<div class="hunk-meta">')[1].split("</div>")[0]
    assert "hunk-cx" in meta
    # +N -N beside it says the same thing in words; the sparkline had no legend.
    assert 'class="bar"' not in out


def test_hunk_count_badge_placeholder_sits_in_hunk_lines_between_path_and_stat():
    """Empty here -- the page's renderNotes() fills it in client-side once notes exist for
    this path. Must live inside .hunk-lines, after .hunk-path and before .hunk-stat, so the
    stat's margin-left:auto still pins it to the row's right edge."""
    order, files = parsed()
    out = render_html(flat(order, files), files, {})

    lines = out.split('<span class="hunk-lines">')[1].split("</span>\n  </summary>")[0]
    assert '<span class="hunk-count"></span>' in lines
    assert lines.index('<span class="hunk-path">') < lines.index('<span class="hunk-count">') \
        < lines.index('<span class="hunk-stat">')


def test_the_meta_row_is_skipped_when_it_would_be_empty():
    # It carries a top padding, so an empty one leaves a visible gap under the summary.
    order, files = parsed()

    bare = render_html(flat(order, files), files, {})
    assert "hunk-meta" not in bare

    with_chips = render_html(flat(order, files), files, {}, complexity=CX)
    assert with_chips.count('<div class="hunk-meta">') == 1  # only src/auth.py has data


# ---- moves ----

# ---- backticks become <code> in prose only ----

def test_backticked_identifiers_become_code_in_group_title_why_role_and_note():
    order, files = parsed()
    by_path = {"src/auth.py": {"role": "Uses `raw` mode.",
                               "hunks": [{"header": "@@ -40,4 +40,4 @@",
                                          "note": "Calls `raw` here."}]}}
    groups = story(order, files, [{"title": "The `x` module", "why": "touches `raw`",
                                   "paths": ["src/auth.py", "README.md"]}], None)
    out = render_html(groups, files, by_path)

    assert "<code>x</code>" in out
    assert out.count("<code>raw</code>") == 3  # why, role, note - one each
    assert "`raw`" not in out


def test_codeify_runs_after_escape_so_markup_cannot_be_injected():
    order, files = parsed()
    by_path = {"src/auth.py": {"role": "Uses `<script>` mode.", "hunks": []}}
    out = render_html(flat(order, files), files, by_path)

    assert "<code>&lt;script&gt;</code>" in out
    assert "<script>" not in out


def test_backticks_in_a_path_or_diff_line_stay_literal():
    diff = ('diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n'
            '@@ -1,1 +1,1 @@\n+call(`raw`)\n')
    order, files = parse_hunks(diff)
    out = render_html(flat(order, files), files, {})

    assert "call(`raw`)" in out
    assert '<span class="hunk-path">x.py</span>' in out


# ---- renames ----

@pytest.mark.parametrize("old, new, expected", [
    ("corelib/ratelimit/internal/config.go", "corelib/ratelimit/ratelimit_config/config.go",
     "corelib/ratelimit/{internal => ratelimit_config}/config.go"),
    ("a.go", "b.go", "a.go => b.go"),
], ids=["shared-prefix-and-suffix", "nothing-shared"])
def test_rename_note(old, new, expected):
    assert rename_note(old, new) == expected


RENAME_DIFF = """diff --git a/corelib/ratelimit/internal/config.go b/corelib/ratelimit/ratelimit_config/config.go
similarity index 100%
rename from corelib/ratelimit/internal/config.go
rename to corelib/ratelimit/ratelimit_config/config.go
"""

RENAME_EDIT_DIFF = """diff --git a/old/util.py b/new/util.py
similarity index 80%
rename from old/util.py
rename to new/util.py
index 111..222 100644
--- a/old/util.py
+++ b/new/util.py
@@ -1,1 +1,1 @@
-old line
+new line
"""


def test_a_pure_rename_shows_where_it_moved_from():
    order, files = parse_hunks(RENAME_DIFF)
    renames = rename_index(RENAME_DIFF)
    out_html = render_html(flat(order, files), files, {}, renames=renames)

    # HTML shows the full old path on its own line, no truncation.
    assert '<span class="hunk-was">moved from corelib/ratelimit/internal/config.go</span>' \
           in out_html
    assert "no content change" in out_html


def test_a_rename_with_edits_is_distinguished_from_a_pure_rename():
    order, files = parse_hunks(RENAME_EDIT_DIFF)
    renames = rename_index(RENAME_EDIT_DIFF)
    out_html = render_html(flat(order, files), files, {}, renames=renames)

    assert '<span class="hunk-was">moved from old/util.py</span>' in out_html
    assert "no content change" not in out_html


def test_renamed_file_header_row_carries_no_arrow_and_a_pure_hunk_path():
    order, files = parse_hunks(RENAME_DIFF)
    renames = rename_index(RENAME_DIFF)
    out = render_html(flat(order, files), files, {}, renames=renames)

    # The two paths sit on their own lines now, so there is no arrow to dangle between them.
    assert "→" not in out
    # .hunk-path must stay exactly the new path: the page's comment JS reads its textContent
    # verbatim as the path field of every PR review comment on that file.
    assert '<span class="hunk-path">corelib/ratelimit/ratelimit_config/config.go</span>' in out


def test_viewed_label_precedes_hunk_was_so_it_stays_on_the_header_row():
    order, files = parse_hunks(RENAME_DIFF)
    renames = rename_index(RENAME_DIFF)
    out = render_html(flat(order, files), files, {}, renames=renames)

    assert out.index('<label class="hunk-viewed"') < out.index('<span class="hunk-was">')


def test_render_html_optional_inputs_default_to_none():
    order, files = parsed()
    plain = render_html(flat(order, files), files, {})

    assert render_html(flat(order, files), files, {}, complexity=None) == plain
    assert render_html(flat(order, files), files, {}, renames=None) == plain
    assert "→" not in plain
    assert "hunk-was" not in plain


def test_explain_renders_plain_code_lines_not_diff_rows():
    order, files = parsed()
    out = render_html(flat(order, files), files, {}, explain=True)

    # class "c" is what the page numbers off the header's new-side start, so the badges come
    # out as the file's own line numbers.
    assert 'class="a"' not in out and 'class="d"' not in out
    assert '<span class="c">' in out
    assert '<span class="h" hidden>' in out
    # the +/- marker is gone from the text itself, not just from the colouring
    assert ">+" not in out.split('<pre class="diff">', 1)[1]


def test_explain_file_stat_counts_lines_instead_of_adds_and_removes():
    order, files = parsed()
    out = render_html(flat(order, files), files, {}, explain=True)

    assert '<span class="add">' not in out
    assert '<span class="hunk-stat">3 lines</span>' in out
    assert '<span class="hunk-stat">1 line</span>' in out


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" + bytes(range(40))
PNG2 = b"\x89PNG\r\n\x1a\n" + b"\x00" + bytes(range(40, 90))


def _preview_repo(tmp_path, base_files, head_files):
    """Commit base_files then head_files (bytes, None deletes); returns (repo, base, head, diff)."""
    require_git()
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")

    def commit(files, msg):
        for name, data in files.items():
            if data is None:
                (repo / name).unlink()
            else:
                (repo / name).write_bytes(data)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "--allow-empty", "-m", msg)
        return git(repo, "rev-parse", "HEAD").strip()

    base = commit({"keep.txt": b"x\n", **base_files}, "base")
    head = commit(head_files, "head")
    return repo, base, head, git(repo, "diff", base, head)


def _preview_out(repo, base, head, diff):
    order, files = parse_hunks(diff)
    return render_html(flat(order, files), files, {}, repo=str(repo), base=base, head=head)


def test_modified_png_previews_before_and_after_by_default_with_a_toggle_button(tmp_path):
    repo, base, head, diff = _preview_repo(tmp_path, {"logo.png": PNG}, {"logo.png": PNG2})
    out = _preview_out(repo, base, head, diff)

    assert base64.b64encode(PNG).decode() in out and base64.b64encode(PNG2).decode() in out
    assert out.count("data:image/png;base64,") == 2
    assert "Before" in out and "After" in out
    assert '<details class="hunk cw-previewing"' in out
    assert 'class="hunk-preview"' in out and 'aria-pressed="true"' in out
    assert "No textual diff" not in out


def test_added_png_shows_one_image_and_a_deleted_one_shows_its_old_side(tmp_path):
    repo, base, head, diff = _preview_repo(tmp_path, {"old.png": PNG}, {"new.png": PNG2, "old.png": None})
    out = _preview_out(repo, base, head, diff)

    assert out.count("<img ") == 2
    assert out.count("<figure>") == 2
    assert "<figcaption>Added</figcaption>" in out and "<figcaption>Deleted</figcaption>" in out


def test_oversized_image_gets_a_skip_note_not_bytes(tmp_path):
    big = b"\x89PNG\x00" + b"0" * IMAGE_CAP
    repo, base, head, diff = _preview_repo(tmp_path, {}, {"big.png": big})
    out = _preview_out(repo, base, head, diff)

    assert "too large to preview (1000 KB)" in out
    assert "<img " not in out


def test_markdown_source_goes_into_an_escaped_template_and_the_diff_stays(tmp_path):
    md = b"# Title\n\n<script>x</script> | a |\n"
    repo, base, head, diff = _preview_repo(tmp_path, {"README.md": b"old\n"}, {"README.md": md})
    out = _preview_out(repo, base, head, diff)

    assert '<template class="cw-md-src"># Title' in out
    assert "&lt;script&gt;x&lt;/script&gt;" in out and "<script>" not in out
    assert '<div class="cw-preview-body"></div>' in out
    assert '<details class="hunk"' in out and 'aria-pressed="false"' in out
    assert '<pre class="diff">' in out


def test_oversized_markdown_is_skipped_with_a_note(tmp_path):
    repo, base, head, diff = _preview_repo(tmp_path, {"a.md": b"x\n"}, {"a.md": b"y" * (MARKDOWN_CAP + 1)})
    out = _preview_out(repo, base, head, diff)

    assert "too large to preview" in out and "cw-md-src" not in out


def test_page_budget_stops_previews_once_spent(tmp_path, monkeypatch):
    monkeypatch.setattr("walkthrough.PAGE_PREVIEW_BUDGET", len(PNG) + 10)
    repo, base, head, diff = _preview_repo(tmp_path, {}, {"a.png": PNG, "b.png": PNG2})
    out = _preview_out(repo, base, head, diff)

    assert out.count("<img ") == 1 and out.count("too large to preview") == 1


def test_git_failure_skips_the_preview_silently(tmp_path):
    diff = "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
    order, files = parse_hunks(diff)
    out = render_html(flat(order, files), files, {}, repo=str(tmp_path / "nope"), base="a", head="b")

    assert "hunk-preview" not in out and "No textual diff" in out


def test_without_repo_the_output_has_no_preview_markup():
    diff = "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
    order, files = parse_hunks(diff)

    assert "hunk-preview" not in render_html(flat(order, files), files, {})


def test_cli_accepts_repo_base_head(tmp_path):
    repo, base, head, diff = _preview_repo(tmp_path, {}, {"a.png": PNG})
    (tmp_path / "raw.diff").write_text(diff)
    (tmp_path / "a.json").write_text("{}")
    cmd = [sys.executable, str(Path(__file__).parent / "walkthrough.py"), "--analysis",
           str(tmp_path / "a.json"), "--diff", str(tmp_path / "raw.diff"), "--format", "html"]
    plain = subprocess.run(cmd, capture_output=True, text=True)
    full = subprocess.run(cmd + ["--repo", str(repo), "--base", base, "--head", head],
                          capture_output=True, text=True)

    assert plain.returncode == 0 and "<img" not in plain.stdout
    assert full.returncode == 0 and "<img" in full.stdout
