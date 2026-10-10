#!/usr/bin/env python3
"""Self-check for validate_analysis.py. Assert-based."""

import copy
import sys
import tempfile
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("role", ["Moved from `internal/`", "Macro tests"])
def test_role_check_accepts_real_roles(role):
    # Weak, but a length/quality heuristic here produces false rejections, so only the
    # single-bare-verb case is checked (see _is_bare_verb_role).
    bad = copy.deepcopy(GOOD)
    bad["files"][1]["role"] = role

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


# ---- hop / side (story map) ----

VALID_GROUPS = {
    "complete": [
        {"title": "The rename", "why": "the point", "paths": ["pkg/thing.py", "gone.py"]},
        {"title": "Artwork", "paths": ["logo.png"]},
    ],
    "partial, the renderer sweeps the rest": [{"title": "The rename", "paths": ["pkg/thing.py"]}],
    # Optional: a group with no flow_mermaid at all, or an explicit blank one, is not a problem.
    "flow blank or absent": [
        {"title": "The rename", "paths": ["pkg/thing.py"]},
        {"title": "Artwork", "paths": ["logo.png"], "flow_mermaid": ""},
    ],
    "real flow diagram": [
        {"title": "The rename", "paths": ["pkg/thing.py"],
         "flow_mermaid": 'flowchart LR\n  A["old"] --> B["new"]'},
    ],
    # "added" is a real identifier on pkg/thing.py's own second hunk (+added).
    "hop naming an identifier in the diff": [
        {"title": "A", "paths": ["pkg/thing.py"], "hop": "feeds the `added` line"},
        {"title": "B", "paths": ["gone.py", "logo.png"]},
    ],
    # "side" groups sit off the main line and are skipped when the gate looks for the last
    # stop, so one trailing a hop-carrying main-line group is not the last-group failure.
    "side group after the last stop carries a hop": [
        {"title": "A", "paths": ["pkg/thing.py"]},
        {"title": "Docs", "paths": ["gone.py", "logo.png"], "side": True,
         "hop": "feeds the `added` line"},
    ],
}


@pytest.mark.parametrize("groups", VALID_GROUPS.values(), ids=VALID_GROUPS.keys())
def test_valid_group_shapes_pass(groups):
    good = copy.deepcopy(GOOD)
    good["groups"] = groups

    assert validate(DIFF, good) == []


_FLOW_OVER_CAP = "flowchart LR\n" + "\n".join(f"  n{i} --> n{i + 1}" for i in range(13))

INVALID_GROUPS = {
    "file outside the diff": (
        [{"title": "Stale", "paths": ["ghost.py"]}],
        [("ghost.py", "not a file in the diff")]),
    "file claimed by two groups": (
        [{"title": "One", "paths": ["pkg/thing.py"]}, {"title": "Two", "paths": ["pkg/thing.py"]}],
        [("pkg/thing.py", "both group")]),
    "no title and no paths": (
        [{"paths": ["pkg/thing.py"]}, {"title": "Empty", "paths": []}],
        [("groups[0] has no title", ""), ("groups[1] has no paths", "")]),
    "flow_mermaid not a known kind": (
        [{"title": "The rename", "paths": ["pkg/thing.py"], "flow_mermaid": "graph LR\n  A --> B"}],
        [("groups[0].flow_mermaid", "flowchart or sequenceDiagram")]),
    "flow_mermaid over the line cap": (
        [{"title": "The rename", "paths": ["pkg/thing.py"], "flow_mermaid": _FLOW_OVER_CAP}],
        [("groups[0].flow_mermaid", "line cap")]),
    # A subagent handing back a list instead of a string must not slip past _blank, which
    # would read "not a string" as "blank" and skip validation entirely -- and the unvalidated
    # value would go on to crash walkthrough.py's own .strip() call.
    "flow_mermaid not a string": (
        [{"title": "The rename", "paths": ["pkg/thing.py"], "flow_mermaid": ["flowchart LR"]}],
        [("groups[0].flow_mermaid", "must be a string")]),
    "hop over six words": (
        [{"title": "A", "paths": ["pkg/thing.py"], "hop": "this hop has entirely way too many words here"},
         {"title": "B", "paths": ["gone.py", "logo.png"]}],
        [("groups[0].hop", "word cap")]),
    "hop identifier missing from the diff": (
        [{"title": "A", "paths": ["pkg/thing.py"], "hop": "leads to `nonexistentSymbol`"},
         {"title": "B", "paths": ["gone.py", "logo.png"]}],
        [("groups[0].hop", "does not appear")]),
    "side not a boolean": (
        [{"title": "A", "paths": ["pkg/thing.py"], "side": "yes"},
         {"title": "B", "paths": ["gone.py", "logo.png"]}],
        [("groups[0].side", "must be a boolean")]),
}


@pytest.mark.parametrize("groups, expected", INVALID_GROUPS.values(), ids=INVALID_GROUPS.keys())
def test_invalid_group_fields_fail(groups, expected):
    bad = copy.deepcopy(GOOD)
    bad["groups"] = groups

    problems = validate(DIFF, bad)

    for key, msg in expected:
        assert any(key in p and msg in p for p in problems), (key, msg, problems)


def test_last_group_with_a_hop_fails():
    # The last stop in the story has no next stop for a hop to name.
    bad = copy.deepcopy(GOOD)
    bad["groups"] = [{"title": "A", "paths": ["pkg/thing.py"], "hop": "feeds the `added` line"},
                     {"title": "B", "paths": ["gone.py", "logo.png"],
                      "hop": "feeds the `added` line"}]

    problems = validate(DIFF, bad)

    assert any("groups[1]" in p and "must not have a hop" in p for p in problems)
    assert not any("groups[0]" in p and "must not have a hop" in p for p in problems)


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


KEYWORD_ONLY_DIFF = '''diff --git a/pkg/guard.go b/pkg/guard.go
index 1111111..2222222 100644
--- a/pkg/guard.go
+++ b/pkg/guard.go
@@ -5,2 +5,2 @@
-	if err != nil {
+	if err == nil {
'''


CONTEXT_DIFF = '''diff --git a/pkg/other.py b/pkg/other.py
index 1111111..2222222 100644
--- a/pkg/other.py
+++ b/pkg/other.py
@@ -1,3 +1,3 @@
 def widget_loader():
-counter = 1
+counter = 2
'''


WHITESPACE_DIFF = '''diff --git a/pkg/ws.py b/pkg/ws.py
index 1111111..2222222 100644
--- a/pkg/ws.py
+++ b/pkg/ws.py
@@ -1,2 +1,2 @@
-
+
'''


def _context_analysis(note):
    return {
        "target": "x", "overview": "bumps counter", "flow_mermaid": "",
        "files": [{
            "path": "pkg/other.py",
            "role": "bumps a counter",
            "hunks": [{"header": "@@ -1,3 +1,3 @@", "note": note}],
        }],
    }


def _whitespace_analysis(note):
    return {
        "target": "x", "overview": "reindents", "flow_mermaid": "",
        "files": [{
            "path": "pkg/ws.py",
            "role": "reindents a block",
            "hunks": [{"header": "@@ -1,2 +1,2 @@", "note": note}],
        }],
    }


# (diff, analysis builder, note, fails): "fails" means validate reports "shares no identifier".
NOTE_IDENTIFIER_ROWS = {
    "shares token_bucket": (IDENT_DIFF, _ident_analysis, "introduces token_bucket", False),
    "plausible unread note": (IDENT_DIFF, _ident_analysis, "adds a plausible new gauge", True),
    # hunk spells it token_bucket, note spells it TokenBucket: both must reduce to the
    # same pieces for the check to be language independent.
    "camel and snake cross-match": (IDENT_DIFF, _ident_analysis, "splits out TokenBucket", False),
    # "if", "err" and "nil" are all filtered (too short, or stoplisted for "nil"), so a
    # fabricated note that only shares "if" with the hunk must fail, not pass.
    "if err nil shares no identifier": (
        REAL_GUARD_DIFF, _guard_analysis, "if the config is valid", True),
    "real changed symbol named": (
        REAL_GUARD_DIFF, _guard_analysis, "inverts the handleAuthError guard", False),
    # Every changed-line token is too short or stoplisted, so no qualifying identifier is
    # left to hold any note to.
    "keywords and short names only is skipped": (
        KEYWORD_ONLY_DIFF, _guard_analysis, "if the config is valid", False),
    # "widget_loader" only appears on the unchanged context line; the changed lines only
    # carry "counter", so a note built only from the context line must still fail.
    "context-line-only match": (
        CONTEXT_DIFF, _context_analysis, "updates the widget_loader value", True),
    # No identifiers on the changed lines at all, so failing the note would be a false positive.
    "whitespace-only hunk is skipped": (
        WHITESPACE_DIFF, _whitespace_analysis, "adjusts indentation", False),
}


@pytest.mark.parametrize("diff, build, note, fails", NOTE_IDENTIFIER_ROWS.values(),
                         ids=NOTE_IDENTIFIER_ROWS.keys())
def test_note_identifier_check(diff, build, note, fails):
    problems = validate(diff, build(note))

    if fails:
        assert any("shares no identifier" in p for p in problems)
    else:
        assert problems == []


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


QUALIFIER_DROP_DIFF = FILLER_HUNK + '''diff --git a/pkg/kind.go b/pkg/kind.go
index 1111111..2222222 100644
--- a/pkg/kind.go
+++ b/pkg/kind.go
@@ -1,2 +1,2 @@
-var kind internal.MediaType
+var kind MediaType
'''


NEW_SYMBOL_DIFF = FILLER_HUNK + '''diff --git a/corelib/ratelimit/uid.go b/corelib/ratelimit/uid.go
index 1111111..2222222 100644
--- a/corelib/ratelimit/uid.go
+++ b/corelib/ratelimit/uid.go
@@ -10,1 +10,4 @@
+func Resolve(id string) string {
+	return id
+}
'''


DELETION_ONLY_DIFF = FILLER_HUNK + '''diff --git a/pkg/gone.go b/pkg/gone.go
index 1111111..2222222 100644
--- a/pkg/gone.go
+++ b/pkg/gone.go
@@ -10,4 +10,0 @@
-func removedHelper() {
-	doStuff()
-}
'''


def _filler_analysis(path, role, header, overview):
    return {
        "target": "x", "overview": overview,
        "flow_mermaid": "",
        "files": [FILLER_FILE, {"path": path, "role": role,
                                "hunks": [{"header": header, "note": ""}]}],
    }


BLANK_NOTE_ROWS = {
    "export rename": (RENAME_DIFF, _rename_analysis("")),
    "qualifier drop": (QUALIFIER_DROP_DIFF, _filler_analysis(
        "pkg/kind.go", "drops the internal qualifier from MediaType",
        "@@ -1,2 +1,2 @@", "drops a package qualifier")),
    # The case the old per-hunk churn test got wrong: a hunk that adds a real new symbol is
    # not churn, and blank is legal on any hunk.
    "brand new identifier": (NEW_SYMBOL_DIFF, _filler_analysis(
        "corelib/ratelimit/uid.go", "adds Resolve", "@@ -10,1 +10,4 @@", "adds a resolver")),
    "pure removal": (DELETION_ONLY_DIFF, _filler_analysis(
        "pkg/gone.go", "removes removedHelper, nothing calls it",
        "@@ -10,4 +10,0 @@", "deletes a helper")),
}


@pytest.mark.parametrize("diff, analysis", BLANK_NOTE_ROWS.values(), ids=BLANK_NOTE_ROWS.keys())
def test_blank_note_is_legal_on_every_kind_of_hunk(diff, analysis):
    assert validate(diff, analysis) == []


# (hunks, substrings expected in the single problem; None means no problem)
@pytest.mark.parametrize("hunks, needles", [
    # The measured good run: 84 empty of 148 hunks, 57%, must pass at the 80% floor.
    ([{"note": ""}] * 84 + [{"note": "x"}] * 64, None),
    ([{"note": ""}] * 148, ["148/148", "100%", f"{EMPTY_NOTE_FLOOR:.0%}"]),
    ([], None),
], ids=["84-of-148-passes", "all-blank-fails-with-floor-message", "zero-hunks-no-division"])
def test_empty_note_floor(hunks, needles):
    problems = _empty_note_floor(hunks)

    if needles is None:
        assert problems == []
    else:
        assert len(problems) == 1
        assert all(needle in problems[0] for needle in needles)


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


def test_a_long_unbroken_overview_fails_the_blob_gate():
    bad = copy.deepcopy(GOOD)
    bad["overview"] = "x" * (OVERVIEW_BLOB_MAX_CHARS + 1)

    problems = validate(DIFF, bad)

    assert any("blob" in p for p in problems)


@pytest.mark.parametrize("text, names_length", [
    ("x" * (OVERVIEW_BLOB_MAX_CHARS + 1) + "\n\ny", False),
    ("- " + "x" * OVERVIEW_BLOB_MAX_CHARS, False),
    ("x" * (OVERVIEW_BLOB_MAX_CHARS + 1), True),
], ids=["blank-line-break", "bullet-line", "unbroken-names-the-length"])
def test_overview_blob_gate(text, names_length):
    problems = _overview_blob(text)

    if names_length:
        assert len(problems) == 1
        assert str(len(text)) in problems[0]
    else:
        assert problems == []
