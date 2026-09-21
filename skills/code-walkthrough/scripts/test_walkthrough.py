#!/usr/bin/env python3
"""Self-check for walkthrough.py. Assert-based, no framework."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from walkthrough import (  # noqa: E402
    caller_depth,
    depth_chip,
    render_html,
    rename_index,
    rename_note,
    story,
)
from validate_analysis import parse_hunks, check_rendered  # noqa: E402


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
    order, files = parsed()
    groups = story(order, files, [{"title": "Docs first", "paths": ["README.md"]},
                                  {"title": "Then the code", "paths": ["src/auth.py"]}], None)
    out = render_html(groups, files, {}, open_count=1)

    assert out.count('<details class="hunk" open>') == 1
    # README is the smaller change by lines, so this fails if size still decides.
    opened = out.split('<details class="hunk" open>')[1]
    assert '<span class="hunk-path">README.md</span>' in opened.split("</details>")[0]


def test_open_count_zero_leaves_everything_collapsed():
    order, files = parsed()

    assert " open>" not in render_html(flat(order, files), files, {}, open_count=0)


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


def test_an_unpushed_head_leaves_the_file_name_as_plain_text():
    order, files = parsed()
    by_path = {e["path"]: e for e in ANALYSIS["files"]}
    out = render_html(flat(order, files), files, by_path, links={**LINKS, "head_pushed": False})

    assert '<span class="hunk-path">src/auth.py</span>' in out


def test_skips_links_when_the_head_is_not_pushed():
    order, files = parsed()
    by_path = {e["path"]: e for e in ANALYSIS["files"]}
    unpushed = {**LINKS, "head_pushed": False}

    # Those urls 404 until the commit exists on the remote.
    assert "https://gh" not in render_html(flat(order, files), files, by_path, links=unpushed)


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


def test_two_symbols_in_the_same_file_do_not_order_it_against_itself():
    symdelta = {"language": "go", "nodes": [
        {"id": "pkg/a.go:F", "kind": "symbol", "file": "pkg/a.go"},
        {"id": "pkg/a.go:G", "kind": "symbol", "file": "pkg/a.go"},
    ], "edges": [{"source": "pkg/a.go:F", "target": "pkg/a.go:G"}]}
    depth = caller_depth(symdelta, ["pkg/a.go"])

    assert depth == {}


def test_a_pkg_node_carries_no_file_and_drops_its_edge():
    symdelta = {"language": "go", "nodes": [
        {"id": "pkg/a", "kind": "pkg", "label": "a", "depth": 0},
        {"id": "pkg/a.go:F", "kind": "symbol", "file": "pkg/a.go"},
    ], "edges": [{"source": "pkg/a", "target": "pkg/a.go:F"}]}
    depth = caller_depth(symdelta, ["pkg/a.go"])

    assert depth == {}


def test_a_call_cycle_still_terminates_and_places_every_file():
    symdelta = {"language": "python", "nodes": [
        {"id": "a.py:f", "kind": "symbol", "file": "a.py"},
        {"id": "b.py:g", "kind": "symbol", "file": "b.py"},
    ], "edges": [{"source": "a.py:f", "target": "b.py:g"},
                 {"source": "b.py:g", "target": "a.py:f"}]}
    depth = caller_depth(symdelta, ["a.py", "b.py"])

    assert set(depth) == {"a.py", "b.py"}


def test_language_null_yields_no_depth_data():
    symdelta = {"language": None, "reason": "unsupported language: rb"}
    depth = caller_depth(symdelta, ["a.py", "b.py"])

    assert depth == {}


def test_no_edges_yields_no_depth_data():
    symdelta = {"language": "python",
                "nodes": [{"id": "a.py:f", "kind": "symbol", "file": "a.py"}], "edges": []}
    depth = caller_depth(symdelta, ["a.py"])

    assert depth == {}


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


# ---- per-group graphs ----

# Two symbols per file, each with its own edge, so a group scoped to one file draws a graph
# that does not mention the other file's symbols -- proof the scoping actually happened rather
# than both groups happening to render the same thing.
GRAPH_SYMDELTA = {
    "nodes": [
        {"id": "p", "label": "p", "kind": "pkg", "parent": None, "depth": 0},
        {"id": "p:Handle", "label": "Handle", "kind": "symbol", "parent": "p", "depth": 1,
         "state": "new", "file": "src/api.py"},
        {"id": "p:HandleCallee", "label": "HandleCallee", "kind": "symbol", "parent": "p",
         "depth": 1, "state": "new", "file": "src/api.py"},
        {"id": "p:Get", "label": "Get", "kind": "symbol", "parent": "p", "depth": 1,
         "state": "new", "file": "src/store.py"},
        {"id": "p:GetCallee", "label": "GetCallee", "kind": "symbol", "parent": "p", "depth": 1,
         "state": "new", "file": "src/store.py"},
    ],
    "edges": [
        {"id": "e0", "source": "p:Handle", "target": "p:HandleCallee", "state": "new"},
        {"id": "e1", "source": "p:Get", "target": "p:GetCallee", "state": "new"},
    ],
}


def test_symdelta_past_one_group_draws_each_group_its_own_scoped_graph():
    files = _chain_files("src/api.py", "src/store.py")
    groups = story(list(files), files, [{"title": "API", "paths": ["src/api.py"]},
                                        {"title": "Store", "paths": ["src/store.py"]}], None)
    out = render_html(groups, files, {}, symdelta=GRAPH_SYMDELTA)

    assert out.count("vd-symbols-group") == 2
    api_chunk, store_chunk = out.split('<h3 class="wt-group">')[1:3]
    assert "HandleCallee" in api_chunk and "GetCallee" not in api_chunk
    assert "GetCallee" in store_chunk and "HandleCallee" not in store_chunk


def test_symdelta_group_graph_data_symbols_matches_its_scoped_count():
    files = _chain_files("src/api.py", "src/store.py")
    groups = story(list(files), files, [{"title": "API", "paths": ["src/api.py"]},
                                        {"title": "Store", "paths": ["src/store.py"]}], None)
    out = render_html(groups, files, {}, symdelta=GRAPH_SYMDELTA)

    api_chunk, store_chunk = out.split('<h3 class="wt-group">')[1:3]
    assert 'data-symbols="2"' in api_chunk
    assert 'data-symbols="2"' in store_chunk


def test_symdelta_with_a_single_group_draws_no_per_group_graph():
    # With one group the graph would be the global section, character for character -- nothing
    # new to show.
    files = _chain_files("src/api.py", "src/store.py")
    groups = story(list(files), files, None, None)

    out = render_html(groups, files, {}, symdelta=GRAPH_SYMDELTA)

    assert "vd-symbols-group" not in out


def test_a_group_with_no_symbols_in_scope_gets_no_graph():
    files = _chain_files("src/api.py", "docs.md")
    groups = story(list(files), files, [{"title": "API", "paths": ["src/api.py"]},
                                        {"title": "Docs", "paths": ["docs.md"]}], None)
    out = render_html(groups, files, {}, symdelta=GRAPH_SYMDELTA)

    api_chunk, docs_chunk = out.split('<h3 class="wt-group">')[1:3]
    assert "vd-symbols-group" in api_chunk
    assert "vd-symbols-group" not in docs_chunk


def test_without_symdelta_no_per_group_graph_at_all():
    files = _chain_files("src/api.py", "src/store.py")
    groups = story(list(files), files, [{"title": "API", "paths": ["src/api.py"]},
                                        {"title": "Store", "paths": ["src/store.py"]}], None)

    assert "vd-symbols-group" not in render_html(groups, files, {})


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


def test_an_unmoved_but_new_functions_high_peak_still_shows():
    # A brand-new function born already hairy is worth a beat even though nothing "moved": it
    # has no before to move from. NOTEWORTHY_CX is 10, so 11 clears it.
    order, files = parsed()
    fresh = _cx("src/auth.py", peak={"name": "resolveSource", "before": 0, "after": 11,
                                     "delta": 11, "existed": False, "touched": True, "depth": 2})
    out = render_html(flat(order, files), files, {}, complexity=fresh)

    assert '<span class="hunk-cx flat"' in out
    assert "resolveSource 11 branches" in out


def test_an_unmoved_pre_existing_peak_gets_no_chip_even_when_high():
    # The bug report: main.go's `serve` sat at 31 before and after this diff (delta 0, but
    # `touched` true since a hunk landed inside it) and still showed a bare "31 cx". A number
    # this diff did not move is not news, no matter how high it already was.
    order, files = parsed()
    unmoved = _cx("src/auth.py", peak={"name": "serve", "before": 31, "after": 31, "delta": 0,
                                       "existed": True, "touched": True, "depth": 1})
    out = render_html(flat(order, files), files, {}, complexity=unmoved)

    assert "hunk-cx" not in out


def test_an_unmoved_function_below_the_noteworthy_line_gets_no_chip():
    # Otherwise every file in a diff carries one: the measured 13-file commit put a chip on all
    # 13, four of them reading "1 cx" for a new getter.
    order, files = parsed()
    quiet = _cx("src/auth.py", peak={"name": "small", "before": 3, "after": 3, "delta": 0,
                                     "existed": True, "touched": True, "depth": 1})
    out = render_html(flat(order, files), files, {}, complexity=quiet)

    assert "hunk-cx" not in out


def test_a_file_the_change_touched_nothing_branching_in_gets_no_chip():
    order, files = parsed()
    out = render_html(flat(order, files), files, {}, complexity=CX)

    readme = out.split('<span class="hunk-path">README.md</span>')[1]
    assert "hunk-cx" not in readme.split("</details>")[0]


def test_a_falling_functions_chip_reads_as_falling_not_just_as_changed():
    order, files = parsed()
    falling = _cx("src/auth.py",
                  peak={"name": "load_session", "before": 20, "after": 6, "delta": -14,
                        "existed": True, "touched": True, "depth": 1},
                  jump={"name": "load_session", "before": 20, "after": 6, "delta": -14,
                        "existed": True, "touched": True, "depth": 1})
    out = render_html(flat(order, files), files, {}, complexity=falling)

    assert '<span class="hunk-cx down"' in out
    assert "load_session 20→6 branches" in out


def test_deep_nesting_is_its_own_chip_shown_at_the_threshold():
    order, files = parsed()
    deep = _cx("src/auth.py", peak={"name": "load_session", "before": 9, "after": 9, "delta": 0,
                                    "existed": True, "touched": True, "depth": 4})
    out = render_html(flat(order, files), files, {}, complexity=deep)

    assert "nested 4 deep" in out
    assert depth_chip(deep["files"]["src/auth.py"]) == "nested 4 deep"


def test_shallow_nesting_gets_no_depth_chip():
    assert depth_chip({"peak": {"name": "x", "depth": 3}}) == ""


def test_a_removed_functions_jump_reads_as_gone_with_no_arrow():
    # Real output from PR 2498: distributionChannelResolverChain.Resolve's branch count fell
    # 3->0 because the function was deleted, not because this diff improved anything.
    order, files = parsed()
    removed = _cx("src/auth.py", peak=None,
                 jump={"name": "distributionChannelResolverChain.Resolve", "before": 3,
                       "after": 0, "delta": -3, "existed": True, "touched": True, "depth": 0,
                       "removed": True})
    out = render_html(flat(order, files), files, {}, complexity=removed)

    assert '<span class="hunk-cx gone"' in out
    assert "distributionChannelResolverChain.Resolve removed" in out
    assert "→" not in out


def test_a_removed_function_with_no_removed_key_still_reads_as_gone():
    # Stale complexity.json from before the "removed" key existed: existed=True with after==0
    # is the only signal available, and it alone is enough to suppress the "N→0" bug.
    order, files = parsed()
    stale = _cx("src/auth.py", peak=None,
               jump={"name": "ipString", "before": 2, "after": 0, "delta": -2,
                     "existed": True, "touched": True, "depth": 0})
    out = render_html(flat(order, files), files, {}, complexity=stale)

    assert '<span class="hunk-cx gone"' in out
    assert "ipString removed" in out
    assert "→" not in out


def test_a_real_jump_is_still_reported_as_moved_not_as_removed():
    order, files = parsed()
    out = render_html(flat(order, files), files, {}, complexity=CX)

    assert '<span class="hunk-cx up"' in out
    assert "load_session 2→9 branches" in out
    assert "hunk-cx gone" not in out


def test_a_new_hairy_function_is_still_reported_as_new_not_as_removed():
    order, files = parsed()
    fresh = _cx("src/auth.py", peak={"name": "resolveSource", "before": 0, "after": 11,
                                     "delta": 11, "existed": False, "touched": True, "depth": 2})
    out = render_html(flat(order, files), files, {}, complexity=fresh)

    assert '<span class="hunk-cx flat"' in out
    assert "resolveSource 11 branches" in out
    assert "hunk-cx gone" not in out


def test_depth_chip_is_suppressed_for_a_removed_function():
    assert depth_chip({"peak": {"name": "x", "depth": 5, "removed": True}}) == ""


def test_the_page_carries_no_legend():
    # The chip says "branches" in words and repeats the long form in its title, so a legend
    # would only add a paragraph of vocabulary before the first file.
    order, files = parsed()

    assert "cyclomatic complexity" not in render_html(flat(order, files), files, {}, complexity=CX)


def test_no_complexity_input_renders_the_same_walkthrough_as_before():
    order, files = parsed()

    assert (render_html(flat(order, files), files, {}, complexity=None)
            == render_html(flat(order, files), files, {}))


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


def test_complexity_chips_render_outside_the_summary():
    order, files = parsed()
    out = render_html(flat(order, files), files, {}, complexity=CX)

    summary = out.split("<summary>")[1].split("</summary>")[0]
    assert "hunk-cx" not in summary
    meta = out.split('<div class="hunk-meta">')[1].split("</div>")[0]
    assert "hunk-cx" in meta


def test_the_stat_stays_in_the_summary_and_the_bar_is_gone_from_html():
    order, files = parsed()
    out = render_html(flat(order, files), files, {}, complexity=CX)

    summary = out.split("<summary>")[1].split("</summary>")[0]
    assert "hunk-stat" in summary
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

def test_a_moved_block_renders_with_no_m_class_anywhere():
    # The moved-line encoding is gone: a line that relocated renders as a plain added/removed
    # line, styled only by the existing "a"/"d" classes, never by an "m" class.
    diff = ('diff --git a/old.py b/old.py\n--- a/old.py\n+++ b/old.py\n'
            '@@ -1,3 +1,0 @@\n-def handler(request):\n-    validate(request)\n'
            '-    return process(request)\n'
            'diff --git a/new.py b/new.py\n--- a/new.py\n+++ b/new.py\n'
            '@@ -0,0 +1,3 @@\n+def handler(request):\n+    validate(request)\n'
            '+    return process(request)\n')
    order, files = parse_hunks(diff)
    out = render_html(flat(order, files), files, {})

    assert ' m"' not in out
    assert 'class="d"' in out and 'class="a"' in out


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

def test_rename_note_compacts_the_shared_prefix_and_suffix_git_style():
    assert (rename_note("corelib/ratelimit/internal/config.go",
                        "corelib/ratelimit/ratelimit_config/config.go")
            == "corelib/ratelimit/{internal => ratelimit_config}/config.go")


def test_rename_note_falls_back_to_full_paths_when_nothing_is_shared():
    assert rename_note("a.go", "b.go") == "a.go => b.go"


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


def test_a_non_renamed_file_gets_no_arrow_or_hunk_was():
    order, files = parsed()
    out = render_html(flat(order, files), files, {})

    assert "→" not in out
    assert "hunk-was" not in out


def test_renames_absent_changes_nothing():
    order, files = parsed()

    assert (render_html(flat(order, files), files, {})
            == render_html(flat(order, files), files, {}, renames=None))


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
    assert "lines</span>" in out or "line</span>" in out


if __name__ == "__main__":
    # Collected rather than listed: module order is definition order, so this runs the same
    # sequence a hand-written list did without going stale every time a test is added.
    tests = [fn for name, fn in list(globals().items())
             if name.startswith("test_") and callable(fn)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
