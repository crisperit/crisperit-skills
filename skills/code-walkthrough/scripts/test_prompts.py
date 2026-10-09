#!/usr/bin/env python3
"""Self-check that each prompt file's judgment rules exist in exactly one place: the prompts/
dir, and nowhere in SKILL.md or references/ -- the duplication phase 1 moved text to fix.
Assert-based, no framework."""

from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent
PROMPTS_DIR = SKILL_DIR / "prompts"

# One distinctive sentence per prompt file, present verbatim in the moved text.
DISTINCTIVE_SENTENCES = {
    "batch.md": "it does not narrate the diff",
    "prose.md": "Rate limiting now reads its thresholds from live config instead of "
                "compile-time constants",
    "comment.md": "Never propose an edit, a patch, or a replacement for the selected code",
    "thread.md": "The path is a signal, not a requirement",
}


def test_comment_prompt_has_outcome_rules():
    text = " ".join((PROMPTS_DIR / "comment.md").read_text().split())
    assert "A suggestion is not an action: the user decides." in text


def _other_doc_files():
    files = [SKILL_DIR / "SKILL.md"]
    files += sorted((SKILL_DIR / "references").glob("*.md"))
    return files


def test_each_distinctive_sentence_lives_only_in_its_prompt_file():
    for filename, sentence in DISTINCTIVE_SENTENCES.items():
        prompt_text = (PROMPTS_DIR / filename).read_text()
        assert sentence in prompt_text, f"{sentence!r} missing from prompts/{filename}"

        for other in _other_doc_files():
            assert sentence not in other.read_text(), (
                f"{sentence!r} (owned by prompts/{filename}) still appears in {other}"
            )


def test_sentence_owned_by_one_file_does_not_leak_into_a_sibling_prompt_file():
    for filename, sentence in DISTINCTIVE_SENTENCES.items():
        for sibling in PROMPTS_DIR.glob("*.md"):
            if sibling.name == filename:
                continue
            assert sentence not in sibling.read_text(), (
                f"{sentence!r} (owned by prompts/{filename}) leaked into {sibling}"
            )


if __name__ == "__main__":
    tests = [
        test_each_distinctive_sentence_lives_only_in_its_prompt_file,
        test_sentence_owned_by_one_file_does_not_leak_into_a_sibling_prompt_file,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
