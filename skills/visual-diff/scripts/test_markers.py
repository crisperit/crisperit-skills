#!/usr/bin/env python3
"""Self-check for the PR body splice documented in references/pr-markdown.md.

This is the source of truth: keep this implementation and the copy in
pr-markdown.md identical if either changes.
"""

import re

START, END = "<!-- visual-diff:start -->", "<!-- visual-diff:end -->"
# Anchored to whole lines so a marker quoted inside a code fence cannot open a match,
# and the last pair wins so an illustrative pair earlier in the body is left alone.
PAIR = re.compile(rf"^{re.escape(START)}$.*?^{re.escape(END)}$", re.S | re.M)


def _neutralise(section: str) -> str:
    # A bare marker at column 0 inside the section would close the region early and make
    # refreshes non-idempotent, appending the tail again each time. One leading space
    # renders identically (an indented HTML comment is still an invisible comment) and no
    # longer matches the line-anchored pattern.
    return "\n".join(
        f" {ln}" if ln.strip() in (START, END) else ln
        for ln in section.splitlines()
    )


def splice(body: str, section: str) -> str:
    wrapped = f"{START}\n{_neutralise(section.strip())}\n{END}"
    body = body.rstrip("\n")
    matches = list(PAIR.finditer(body))
    if matches:
        m = matches[-1]
        merged = body[: m.start()] + wrapped + body[m.end() :]
    else:
        merged = f"{body}\n\n{wrapped}" if body else wrapped
    return merged.rstrip("\n") + "\n"


def test_appends_when_no_markers_present():
    body = "## What and why\n\nFixes the login bug.\n"
    result = splice(body, "recap here")
    assert result.startswith(body.rstrip("\n") + "\n\n" + START), result
    assert "recap here" in result
    assert result.endswith(END + "\n")


def test_replaces_in_place_preserving_above_and_below():
    body = (
        "## What and why\n\nOriginal text.\n\n"
        f"{START}\nold recap\n{END}\n\n"
        "## Test plan\n\nRan it locally.\n"
    )
    result = splice(body, "new recap")
    assert "Original text." in result
    assert "## Test plan" in result
    assert "Ran it locally." in result
    assert "old recap" not in result
    assert "new recap" in result


def test_applying_twice_is_idempotent():
    body = "## What and why\n\nOriginal text.\n"
    once = splice(body, "recap v1")
    twice = splice(once, "recap v1")
    assert once == twice


def test_backslash_sequences_survive():
    # re.sub(pattern, wrapped, body) as a string replacement would raise
    # re.error on \1, or silently mangle \d and \n as escapes. This section
    # is exactly the diff content that broke that approach.
    section = "See C:\\1\\Users, pattern \\d+, and a literal \\n in the diff."
    body = "## What and why\n\nOriginal text.\n"
    result = splice(body, section)
    assert section in result


def test_illustrative_marker_pair_not_eaten():
    body = (
        "## What and why\n\n"
        "Example of the marker syntax:\n\n"
        "```\n"
        f"    {START}\n"
        "    old example content\n"
        f"    {END}\n"
        "```\n\n"
        f"{START}\nreal old recap\n{END}\n"
    )
    result = splice(body, "real new recap")
    assert "old example content" in result
    assert "real old recap" not in result
    assert "real new recap" in result


def test_trailing_newline_from_jq_matches_no_trailing_newline():
    body_clean = "## What and why\n\nOriginal text.\n"
    body_with_jq_newline = body_clean + "\n"
    assert splice(body_clean, "recap") == splice(body_with_jq_newline, "recap")


def test_empty_body_produces_just_wrapped_section():
    result = splice("", "recap")
    assert result == f"{START}\nrecap\n{END}\n"
    assert not result.startswith("\n")


def test_bare_marker_in_section_stays_idempotent():
    section = f"The recap is delimited by:\n{END}\nand that is the end of it."
    once = splice("Human wrote this. Keep it.", section)
    twice = splice(once, section)
    assert once == twice
    assert "Human wrote this. Keep it." in twice


def test_bare_marker_in_section_is_neutralised():
    section = f"The recap is delimited by:\n{END}\nand that is the end of it."
    result = splice("Human wrote this. Keep it.", section)
    assert re.search(rf"^{re.escape(END)}$", result, re.M) is not None
    assert len(re.findall(rf"^{re.escape(END)}$", result, re.M)) == 1
    assert f" {END}" in result


if __name__ == "__main__":
    tests = [
        test_appends_when_no_markers_present,
        test_replaces_in_place_preserving_above_and_below,
        test_applying_twice_is_idempotent,
        test_backslash_sequences_survive,
        test_illustrative_marker_pair_not_eaten,
        test_trailing_newline_from_jq_matches_no_trailing_newline,
        test_empty_body_produces_just_wrapped_section,
        test_bare_marker_in_section_stays_idempotent,
        test_bare_marker_in_section_is_neutralised,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
