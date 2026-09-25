#!/usr/bin/env python3
"""Self-check for validate_analysis.py. Assert-based, no framework."""

import copy
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from validate_analysis import (  # noqa: E402
    EMPTY_NOTE_FLOOR,
    OVERVIEW_BLOB_MAX_CHARS,
    _empty_note_floor,
    _overview_blob,
    check_rendered,
    check_sections,
    is_test_path,
    parse_diff,
    validate,
)

DIFF = '''diff --git a/pkg/thing.py b/pkg/thing.py
index 1111111..2222222 100644
--- a/pkg/thing.py
+++ b/pkg/thing.py
@@ -1,3 +1,3 @@ def head():
-old
+new
@@ -20,4 +20,5 @@ def tail():
+++ b/not-a-header
 ctx
+added
diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-bye
diff --git a/logo.png b/logo.png
index 3333333..4444444 100644
Binary files a/logo.png and b/logo.png differ
'''

GOOD = {
    "target": "master...HEAD",
    "overview": "Renames the thing.",
    "flow_mermaid": "",
    "files": [
        {
            "path": "pkg/thing.py",
            "role": "carries the change",
            "hunks": [
                {"header": "@@ -1,3 +1,3 @@ def head():", "note": "swaps old for new"},
                {"header": "@@ -20,4 +20,5 @@", "note": "appends the added line"},
            ],
        },
        {"path": "gone.py", "role": "deleted, nothing read it", "hunks": [
            {"header": "@@ -1,2 +0,0 @@", "note": "removes the bye line"},
        ]},
        {"path": "logo.png", "role": "binary, replaced artwork", "hunks": []},
    ],
}


def test_is_test_path_classifies_across_ecosystems():
    hits = [
        "internal/ratelimit/ratelimit_request_test.go",
        "corelib/macros/macros_test.go",
        "app/foo_test.py",
        "src/main/java/FooTest.java",
        "pkg/a/b_test.go",
        "lib/foo_spec.rb",
        "lib/foo_test.rb",
        "Services/FooTests.cs",
        "Sources/FooTests.swift",
        "conftest.py",
        "src/foo.test.ts",
        "src/foo.spec.ts",
        "tests/x.py",
        "spec/y.rb",
    ]
    for path in hits:
        assert is_test_path(path), f"expected test path: {path}"

    non_test = [
        "latest/x.py",
        "src/contest.py",
        "src/greatest/x.py",
        "greatest.cs",
        "contest.java",
        "manifest.py",
    ]
    for path in non_test:
        assert not is_test_path(path), f"expected non-test path: {path}"


def test_parse_diff_finds_files_hunks_and_the_deleted_side():
    order, hunks = parse_diff(DIFF)

    assert order == ["pkg/thing.py", "gone.py", "logo.png"]
    assert hunks["pkg/thing.py"] == ["@@ -1,3 +1,3 @@", "@@ -20,4 +20,5 @@"]
    assert hunks["gone.py"] == ["@@ -1,2 +0,0 @@"]
    assert hunks["logo.png"] == []


def test_complete_analysis_passes():
    assert validate(DIFF, GOOD) == []


def test_missing_file_fails():
    bad = copy.deepcopy(GOOD)
    bad["files"] = [f for f in bad["files"] if f["path"] != "logo.png"]

    problems = validate(DIFF, bad)

    assert any("logo.png" in p and "missing from files[]" in p for p in problems)


def test_missing_hunk_fails():
    bad = copy.deepcopy(GOOD)
    bad["files"][0]["hunks"] = bad["files"][0]["hunks"][:1]

    problems = validate(DIFF, bad)

    assert any("@@ -20,4 +20,5 @@" in p and "no note" in p for p in problems)


def test_blank_role_fails():
    bad = copy.deepcopy(GOOD)
    bad["files"][1]["role"] = ""

    problems = validate(DIFF, bad)

    assert any("role is empty" in p for p in problems)


def test_bare_verb_role_fails():
    for word in ("Moved", "Moved.", "Moved:", "moved", "DELETED"):
        bad = copy.deepcopy(GOOD)
        bad["files"][1]["role"] = word

        problems = validate(DIFF, bad)

        assert any("gone.py" in p and "for or where its contents went" in p for p in problems), \
            f"{word!r} should have failed as a bare change-verb role"


def test_moved_from_role_passes():
    bad = copy.deepcopy(GOOD)
    bad["files"][1]["role"] = "Moved from `internal/`"

    assert validate(DIFF, bad) == []


def test_two_word_role_passes():
    # Weak, but a length/quality heuristic here produces false rejections, so only the
    # single-bare-verb case is checked (see _is_bare_verb_role).
    bad = copy.deepcopy(GOOD)
    bad["files"][1]["role"] = "Macro tests"

    assert validate(DIFF, bad) == []


def test_an_empty_note_on_any_hunk_passes():
    # No more per-hunk churn detection: a blank note is legal on any hunk, substantive or
    # not, as long as the global floor below isn't tripped.
    bad = copy.deepcopy(GOOD)
    bad["files"][0]["hunks"][1]["note"] = "   "

    assert validate(DIFF, bad) == []


def test_invented_file_and_invented_hunk_fail():
    bad = copy.deepcopy(GOOD)
    bad["files"].append({"path": "ghost.py", "role": "made up", "hunks": []})
    bad["files"][0]["hunks"].append({"header": "@@ -99,1 +99,1 @@", "note": "made up"})

    problems = validate(DIFF, bad)

    assert any("ghost.py" in p and "not changed in the diff" in p for p in problems)
    assert any("@@ -99,1 +99,1 @@" in p and "not in the diff" in p for p in problems)


def test_missing_top_level_key_fails():
    bad = copy.deepcopy(GOOD)
    del bad["flow_mermaid"]

    assert any("flow_mermaid" in p for p in validate(DIFF, bad))


def test_rendered_must_mention_every_path():
    rendered = "# recap\n\n`pkg/thing.py` and `gone.py` changed.\n"

    problems = check_rendered(DIFF, rendered)

    assert problems == ["logo.png: never mentioned in the rendered recap"]


def test_a_generated_section_must_reach_the_rendered_output():
    with tempfile.TemporaryDirectory() as tmp:
        section = Path(tmp) / "section-symbols.md"
        section.write_text("<!-- code-walkthrough:symbols -->\n\n### Symbols touched\n")
        empty = Path(tmp) / "section-links.md"
        empty.write_text("")

        missing = check_sections([str(section), str(empty)], "# recap, no section here")
        present = check_sections(
            [str(section), str(empty)], "# recap\n<!-- code-walkthrough:symbols -->\nstuff"
        )

        assert len(missing) == 1 and "code-walkthrough:symbols" in missing[0]
        # An empty section file means there was nothing to draw, which is not a failure.
        assert present == []


def test_no_groups_at_all_is_fine():
    assert validate(DIFF, GOOD) == []


def test_complete_grouping_passes():
    good = copy.deepcopy(GOOD)
    good["groups"] = [
        {"title": "The rename", "why": "the point", "paths": ["pkg/thing.py", "gone.py"]},
        {"title": "Artwork", "paths": ["logo.png"]},
    ]

    assert validate(DIFF, good) == []


def test_a_partial_grouping_passes_because_the_renderer_sweeps_the_rest():
    good = copy.deepcopy(GOOD)
    good["groups"] = [{"title": "The rename", "paths": ["pkg/thing.py"]}]

    assert validate(DIFF, good) == []


def test_a_group_naming_a_file_outside_the_diff_fails():
    bad = copy.deepcopy(GOOD)
    bad["groups"] = [{"title": "Stale", "paths": ["ghost.py"]}]

    assert any("ghost.py" in p and "not a file in the diff" in p for p in validate(DIFF, bad))


def test_a_file_claimed_by_two_groups_fails():
    bad = copy.deepcopy(GOOD)
    bad["groups"] = [{"title": "One", "paths": ["pkg/thing.py"]},
                     {"title": "Two", "paths": ["pkg/thing.py"]}]

    assert any("pkg/thing.py" in p and "both group" in p for p in validate(DIFF, bad))


def test_a_group_with_no_title_or_no_paths_fails():
    bad = copy.deepcopy(GOOD)
    bad["groups"] = [{"paths": ["pkg/thing.py"]}, {"title": "Empty", "paths": []}]

    problems = validate(DIFF, bad)

    assert any("groups[0] has no title" in p for p in problems)
    assert any("groups[1] has no paths" in p for p in problems)


def test_a_group_flow_mermaid_passes_when_blank_or_absent():
    # Optional: a group with no flow_mermaid at all, or an explicit blank one, is not a problem.
    good = copy.deepcopy(GOOD)
    good["groups"] = [{"title": "The rename", "paths": ["pkg/thing.py"]},
                      {"title": "Artwork", "paths": ["logo.png"], "flow_mermaid": ""}]

    assert validate(DIFF, good) == []


def test_a_group_flow_mermaid_passes_with_a_real_diagram():
    good = copy.deepcopy(GOOD)
    good["groups"] = [{"title": "The rename", "paths": ["pkg/thing.py"],
                       "flow_mermaid": 'flowchart LR\n  A["old"] --> B["new"]'}]

    assert validate(DIFF, good) == []


def test_a_group_flow_mermaid_not_starting_with_a_known_kind_fails():
    bad = copy.deepcopy(GOOD)
    bad["groups"] = [{"title": "The rename", "paths": ["pkg/thing.py"],
                      "flow_mermaid": "graph LR\n  A --> B"}]

    problems = validate(DIFF, bad)

    assert any("groups[0].flow_mermaid" in p and "flowchart or sequenceDiagram" in p
               for p in problems)


def test_a_group_flow_mermaid_over_the_line_cap_fails():
    bad = copy.deepcopy(GOOD)
    lines = "\n".join(f"  n{i} --> n{i + 1}" for i in range(13))
    bad["groups"] = [{"title": "The rename", "paths": ["pkg/thing.py"],
                      "flow_mermaid": f"flowchart LR\n{lines}"}]

    problems = validate(DIFF, bad)

    assert any("groups[0].flow_mermaid" in p and "line cap" in p for p in problems)


def test_a_group_flow_mermaid_that_is_not_a_string_fails():
    # A subagent handing back a list instead of a string must not slip past _blank, which
    # would read "not a string" as "blank" and skip validation entirely -- and the unvalidated
    # value would go on to crash walkthrough.py's own .strip() call.
    bad = copy.deepcopy(GOOD)
    bad["groups"] = [{"title": "The rename", "paths": ["pkg/thing.py"],
                      "flow_mermaid": ["flowchart LR"]}]

    problems = validate(DIFF, bad)

    assert any("groups[0].flow_mermaid" in p and "must be a string" in p for p in problems)


# ---- hop / side (story map) ----

def test_hop_over_six_words_fails():
    bad = copy.deepcopy(GOOD)
    bad["groups"] = [{"title": "A", "paths": ["pkg/thing.py"],
                      "hop": "this hop has entirely way too many words here"},
                     {"title": "B", "paths": ["gone.py", "logo.png"]}]

    problems = validate(DIFF, bad)

    assert any("groups[0].hop" in p and "word cap" in p for p in problems)


def test_hop_backticked_identifier_missing_from_diff_fails():
    bad = copy.deepcopy(GOOD)
    bad["groups"] = [{"title": "A", "paths": ["pkg/thing.py"],
                      "hop": "leads to `nonexistentSymbol`"},
                     {"title": "B", "paths": ["gone.py", "logo.png"]}]

    problems = validate(DIFF, bad)

    assert any("groups[0].hop" in p and "does not appear" in p for p in problems)


def test_hop_with_an_identifier_in_the_diff_passes():
    # "added" is a real identifier on pkg/thing.py's own second hunk (+added).
    good = copy.deepcopy(GOOD)
    good["groups"] = [{"title": "A", "paths": ["pkg/thing.py"], "hop": "feeds the `added` line"},
                      {"title": "B", "paths": ["gone.py", "logo.png"]}]

    assert validate(DIFF, good) == []


def test_side_not_a_boolean_fails():
    bad = copy.deepcopy(GOOD)
    bad["groups"] = [{"title": "A", "paths": ["pkg/thing.py"], "side": "yes"},
                     {"title": "B", "paths": ["gone.py", "logo.png"]}]

    problems = validate(DIFF, bad)

    assert any("groups[0].side" in p and "must be a boolean" in p for p in problems)


def test_last_group_with_a_hop_fails():
    # The last stop in the story has no next stop for a hop to name.
    bad = copy.deepcopy(GOOD)
    bad["groups"] = [{"title": "A", "paths": ["pkg/thing.py"], "hop": "feeds the `added` line"},
                     {"title": "B", "paths": ["gone.py", "logo.png"],
                      "hop": "feeds the `added` line"}]

    problems = validate(DIFF, bad)

    assert any("groups[1]" in p and "must not have a hop" in p for p in problems)
    assert not any("groups[0]" in p and "must not have a hop" in p for p in problems)


def test_side_group_after_the_last_stop_may_still_carry_a_hop():
    # "side" groups sit off the main line and are skipped when the gate looks for the last
    # stop, so one trailing a hop-carrying main-line group is not the failure above.
    good = copy.deepcopy(GOOD)
    good["groups"] = [{"title": "A", "paths": ["pkg/thing.py"]},
                      {"title": "Docs", "paths": ["gone.py", "logo.png"], "side": True,
                       "hop": "feeds the `added` line"}]

    assert validate(DIFF, good) == []


def _ident_analysis(note):
    return {
        "target": "x",
        "overview": "renames a variable",
        "flow_mermaid": "",
        "files": [{
            "path": "pkg/mod.py",
            "role": "renames the sensor variable",
            "hunks": [{"header": "@@ -1,2 +1,2 @@", "note": note}],
        }],
    }


IDENT_DIFF = '''diff --git a/pkg/mod.py b/pkg/mod.py
index 1111111..2222222 100644
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,2 +1,2 @@
-old_value = 1
+token_bucket = 1
'''


def test_note_sharing_an_identifier_passes():
    assert validate(IDENT_DIFF, _ident_analysis("introduces token_bucket")) == []


def test_plausible_unread_note_fails():
    problems = validate(IDENT_DIFF, _ident_analysis("adds a plausible new gauge"))

    assert any("shares no identifier" in p for p in problems)


def test_camel_case_and_snake_case_cross_match():
    # hunk spells it token_bucket, note spells it TokenBucket: both must reduce to the
    # same pieces for the check to be language independent.
    assert validate(IDENT_DIFF, _ident_analysis("splits out TokenBucket")) == []


REAL_GUARD_DIFF = '''diff --git a/pkg/guard.go b/pkg/guard.go
index 1111111..2222222 100644
--- a/pkg/guard.go
+++ b/pkg/guard.go
@@ -5,2 +5,2 @@
-	if err != nil { return handleAuthError(err) }
+	if err == nil { return handleAuthError(err) }
'''


def _guard_analysis(note):
    return {
        "target": "x", "overview": "flips a guard", "flow_mermaid": "",
        "files": [{
            "path": "pkg/guard.go",
            "role": "flips the nil guard",
            "hunks": [{"header": "@@ -5,2 +5,2 @@", "note": note}],
        }],
    }


def test_if_err_nil_no_longer_passes_via_a_shared_keyword():
    # "if", "err" and "nil" are all filtered (too short, or stoplisted for "nil"), so a
    # fabricated note that only shares "if" with the hunk must now fail, not pass.
    problems = validate(REAL_GUARD_DIFF, _guard_analysis("if the config is valid"))

    assert any("shares no identifier" in p for p in problems)


def test_a_note_naming_the_real_changed_symbol_still_passes():
    assert validate(REAL_GUARD_DIFF, _guard_analysis("inverts the handleAuthError guard")) == []


KEYWORD_ONLY_DIFF = '''diff --git a/pkg/guard.go b/pkg/guard.go
index 1111111..2222222 100644
--- a/pkg/guard.go
+++ b/pkg/guard.go
@@ -5,2 +5,2 @@
-	if err != nil {
+	if err == nil {
'''


def test_a_hunk_of_only_keywords_and_short_names_is_skipped_not_failed():
    # Every changed-line token here is either too short ("if", "err") or stoplisted
    # ("nil"), so there is no qualifying identifier left to hold any note to.
    assert validate(KEYWORD_ONLY_DIFF, _guard_analysis("if the config is valid")) == []


CONTEXT_DIFF = '''diff --git a/pkg/other.py b/pkg/other.py
index 1111111..2222222 100644
--- a/pkg/other.py
+++ b/pkg/other.py
@@ -1,3 +1,3 @@
 def widget_loader():
-counter = 1
+counter = 2
'''


def test_a_match_against_a_context_line_only_still_fails():
    analysis = {
        "target": "x", "overview": "bumps counter", "flow_mermaid": "",
        "files": [{
            "path": "pkg/other.py",
            "role": "bumps a counter",
            "hunks": [{"header": "@@ -1,3 +1,3 @@", "note": "updates the widget_loader value"}],
        }],
    }

    problems = validate(CONTEXT_DIFF, analysis)

    # "widget_loader" only appears on the unchanged context line; the changed lines only
    # carry "counter", so a note built only from the context line must still fail.
    assert any("shares no identifier" in p for p in problems)


def test_empty_hunks_file_is_skipped():
    # logo.png is binary: hunks is [] and there is no note to check at all.
    assert validate(DIFF, GOOD) == []


WHITESPACE_DIFF = '''diff --git a/pkg/ws.py b/pkg/ws.py
index 1111111..2222222 100644
--- a/pkg/ws.py
+++ b/pkg/ws.py
@@ -1,2 +1,2 @@
-
+
'''


def test_a_whitespace_only_hunk_is_skipped():
    # The changed lines carry no identifiers at all, so there is nothing to hold the
    # note to; failing it would be a false positive.
    analysis = {
        "target": "x", "overview": "reindents", "flow_mermaid": "",
        "files": [{
            "path": "pkg/ws.py",
            "role": "reindents a block",
            "hunks": [{"header": "@@ -1,2 +1,2 @@", "note": "adjusts indentation"}],
        }],
    }

    assert validate(WHITESPACE_DIFF, analysis) == []


# Each of these carries a second, unrelated filler hunk with a real note so the lone
# empty-note hunk under test stays a 50% share, well under EMPTY_NOTE_FLOOR -- otherwise a
# single-hunk diff with a blank note always trips the floor on its own (1/1 = 100%), which
# would test the floor by accident instead of the thing each test names.
FILLER_HUNK = '''diff --git a/pkg/filler.go b/pkg/filler.go
index 1111111..2222222 100644
--- a/pkg/filler.go
+++ b/pkg/filler.go
@@ -1,1 +1,1 @@
-fillerOld
+fillerNew
'''
FILLER_FILE = {"path": "pkg/filler.go", "role": "unrelated filler change",
               "hunks": [{"header": "@@ -1,1 +1,1 @@", "note": "swaps fillerOld for fillerNew"}]}


RENAME_DIFF = FILLER_HUNK + '''diff --git a/pkg/errors.go b/pkg/errors.go
index 1111111..2222222 100644
--- a/pkg/errors.go
+++ b/pkg/errors.go
@@ -1,2 +1,2 @@
-func classifyLookupError(err error) string {
+func ClassifyLookupError(err error) string {
'''


def _rename_analysis(note):
    return {
        "target": "x", "overview": "exports a function",
        "flow_mermaid": "",
        "files": [FILLER_FILE, {
            "path": "pkg/errors.go",
            "role": "exports classifyLookupError as ClassifyLookupError",
            "hunks": [{"header": "@@ -1,2 +1,2 @@", "note": note}],
        }],
    }


def test_empty_note_on_an_export_rename_hunk_passes():
    assert validate(RENAME_DIFF, _rename_analysis("")) == []


def test_non_empty_note_sharing_no_identifier_still_fails():
    # A blank note is always legal, but a filled-in one still has to earn its keep.
    problems = validate(RENAME_DIFF, _rename_analysis("renames this thing"))

    assert any("shares no identifier" in p for p in problems)


QUALIFIER_DROP_DIFF = FILLER_HUNK + '''diff --git a/pkg/kind.go b/pkg/kind.go
index 1111111..2222222 100644
--- a/pkg/kind.go
+++ b/pkg/kind.go
@@ -1,2 +1,2 @@
-var kind internal.MediaType
+var kind MediaType
'''


def test_empty_note_on_a_qualifier_drop_hunk_passes():
    analysis = {
        "target": "x", "overview": "drops a package qualifier",
        "flow_mermaid": "",
        "files": [FILLER_FILE, {
            "path": "pkg/kind.go",
            "role": "drops the internal qualifier from MediaType",
            "hunks": [{"header": "@@ -1,2 +1,2 @@", "note": ""}],
        }],
    }

    assert validate(QUALIFIER_DROP_DIFF, analysis) == []


NEW_SYMBOL_DIFF = FILLER_HUNK + '''diff --git a/corelib/ratelimit/uid.go b/corelib/ratelimit/uid.go
index 1111111..2222222 100644
--- a/corelib/ratelimit/uid.go
+++ b/corelib/ratelimit/uid.go
@@ -10,1 +10,4 @@
+func Resolve(id string) string {
+	return id
+}
'''


def test_empty_note_on_a_hunk_adding_a_brand_new_identifier_passes():
    # This is exactly the case the old per-hunk churn test got wrong: a hunk that adds a
    # real new symbol is not churn, but blank is legal on any hunk now, so it must pass.
    analysis = {
        "target": "x", "overview": "adds a resolver",
        "flow_mermaid": "",
        "files": [FILLER_FILE, {
            "path": "corelib/ratelimit/uid.go",
            "role": "adds Resolve",
            "hunks": [{"header": "@@ -10,1 +10,4 @@", "note": ""}],
        }],
    }

    assert validate(NEW_SYMBOL_DIFF, analysis) == []


DELETION_ONLY_DIFF = FILLER_HUNK + '''diff --git a/pkg/gone.go b/pkg/gone.go
index 1111111..2222222 100644
--- a/pkg/gone.go
+++ b/pkg/gone.go
@@ -10,4 +10,0 @@
-func removedHelper() {
-	doStuff()
-}
'''


def test_empty_note_on_a_pure_removal_hunk_passes():
    analysis = {
        "target": "x", "overview": "deletes a helper",
        "flow_mermaid": "",
        "files": [FILLER_FILE, {
            "path": "pkg/gone.go",
            "role": "removes removedHelper, nothing calls it",
            "hunks": [{"header": "@@ -10,4 +10,0 @@", "note": ""}],
        }],
    }

    assert validate(DELETION_ONLY_DIFF, analysis) == []


def test_84_of_148_empty_notes_passes_the_floor():
    # The measured good run: 84 empty of 148 hunks, 57%, must pass at the 80% floor.
    hunks = [{"note": ""}] * 84 + [{"note": "x"}] * 64

    assert _empty_note_floor(hunks) == []


def test_blanking_every_note_fails_with_the_floor_message():
    hunks = [{"note": ""}] * 148

    problems = _empty_note_floor(hunks)

    assert len(problems) == 1
    assert "148/148" in problems[0]
    assert "100%" in problems[0]
    assert f"{EMPTY_NOTE_FLOOR:.0%}" in problems[0]


def test_zero_hunks_does_not_divide_by_zero():
    assert _empty_note_floor([]) == []


def test_validate_fails_when_almost_every_note_is_blank():
    # End-to-end through validate(): DIFF has 3 hunks total, all blanked, 100% > the floor.
    bad = copy.deepcopy(GOOD)
    for entry in bad["files"]:
        for hunk in entry["hunks"]:
            hunk["note"] = ""

    problems = validate(DIFF, bad)

    assert any("floor" in p for p in problems)


def test_fragment_mode_passes_a_seed_shaped_fragment_missing_target_and_overview():
    # A seed fragment only has "files"; fragment mode must not demand target/overview/
    # flow_mermaid the way the central gate does.
    seed = {"files": copy.deepcopy(GOOD["files"])}

    assert validate(DIFF, seed, fragment=True) == []
    assert validate(DIFF, seed, fragment=False) != []


def test_fragment_mode_still_fails_a_missing_hunk():
    seed = {"files": copy.deepcopy(GOOD["files"])}
    seed["files"][0]["hunks"] = seed["files"][0]["hunks"][:1]

    problems = validate(DIFF, seed, fragment=True)

    assert any("@@ -20,4 +20,5 @@" in p and "no note" in p for p in problems)


def test_fragment_mode_ignores_the_empty_note_floor():
    # The floor is scoped to the whole diff (EMPTY_NOTE_FLOOR); a batch that is mostly
    # renames must not be pushed into padding notes just to clear it on its own.
    seed = {"files": copy.deepcopy(GOOD["files"])}
    for entry in seed["files"]:
        for hunk in entry["hunks"]:
            hunk["note"] = ""

    assert validate(DIFF, seed, fragment=True) == []
    assert any("floor" in p for p in validate(DIFF, seed, fragment=False))


def test_a_short_unstructured_overview_passes():
    bad = copy.deepcopy(GOOD)
    bad["overview"] = "Renames the thing, nothing else changes."

    assert validate(DIFF, bad) == []


def test_a_long_unbroken_overview_fails_the_blob_gate():
    bad = copy.deepcopy(GOOD)
    bad["overview"] = "x" * (OVERVIEW_BLOB_MAX_CHARS + 1)

    problems = validate(DIFF, bad)

    assert any("blob" in p for p in problems)


def test_overview_blob_passes_with_a_blank_line_break():
    assert _overview_blob("x" * (OVERVIEW_BLOB_MAX_CHARS + 1) + "\n\ny") == []


def test_overview_blob_passes_with_a_bullet_line():
    assert _overview_blob("- " + "x" * OVERVIEW_BLOB_MAX_CHARS) == []


def test_overview_blob_names_the_length():
    text = "x" * (OVERVIEW_BLOB_MAX_CHARS + 1)

    problems = _overview_blob(text)

    assert len(problems) == 1
    assert str(len(text)) in problems[0]


if __name__ == "__main__":
    # Collected rather than listed: module order is definition order, so this runs the same
    # sequence a hand-written list did without going stale every time a test is added.
    tests = [fn for name, fn in list(globals().items())
             if name.startswith("test_") and callable(fn)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
