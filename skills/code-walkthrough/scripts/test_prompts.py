#!/usr/bin/env python3
"""Self-check that each prompt file's judgment rules exist in exactly one place: the prompts/
dir, and nowhere in SKILL.md or references/ -- the duplication phase 1 moved text to fix.
Assert-based, no framework."""

import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent
PROMPTS_DIR = SKILL_DIR / "prompts"
sys.path.insert(0, str(Path(__file__).parent))
import visuals  # noqa: E402

# One distinctive sentence per prompt file, present verbatim in the moved text.
DISTINCTIVE_SENTENCES = {
    "batch.md": "it does not narrate the diff",
    "prose.md": "Rate limiting now reads its thresholds from live config instead of "
                "compile-time constants",
    "comment.md": "Never propose an edit, a patch, or a replacement for the selected code",
    "thread.md": "The path is a signal, not a requirement",
    "agent.md": "Never push, and do not try to reach a remote.",
    "visuals.md": "Clarity beats coverage: leave out symbols and files that do not serve",
}


def test_comment_prompt_has_outcome_rules():
    text = " ".join((PROMPTS_DIR / "comment.md").read_text().split())
    assert "A suggestion is not an action: the user decides." in text
    assert "`propose_page_edit` once" in text and "at most one per turn" in text.lower()
    assert "`propose_github_draft` only when" in text and "`verbatim` true only when the user explicitly says" in text
    assert "A draft is only a proposal" in text


def _other_doc_files():
    files = [SKILL_DIR / "SKILL.md"]
    files += sorted((SKILL_DIR / "references").glob("*.md"))
    return files


def test_each_distinctive_sentence_lives_in_exactly_one_place():
    for filename, sentence in DISTINCTIVE_SENTENCES.items():
        prompt_text = (PROMPTS_DIR / filename).read_text()
        assert sentence in prompt_text, f"{sentence!r} missing from prompts/{filename}"

        for other in _other_doc_files():
            assert sentence not in other.read_text(), (
                f"{sentence!r} (owned by prompts/{filename}) still appears in {other}"
            )
        for sibling in PROMPTS_DIR.glob("*.md"):
            if sibling.name == filename:
                continue
            assert sentence not in sibling.read_text(), (
                f"{sentence!r} (owned by prompts/{filename}) leaked into {sibling}"
            )


def test_visuals_prompt_examples_pass_the_sanitiser():
    text = (PROMPTS_DIR / "visuals.md").read_text()
    blocks = re.findall(r"```json\n(.*?)\n```", text, re.S)
    assert len(blocks) >= 3
    for block in blocks:
        example = json.loads(block)
        n_groups = max([g["index"] for g in example["groups"]], default=-1) + 1
        clean, problems = visuals.check_visuals(example, n_groups)
        assert problems == []
        assert bool(clean["pr"]) == bool(example["pr"]) and len(clean["groups"]) == len(example["groups"])


def test_visuals_prompt_stays_small_and_examples_are_phone_sized():
    text = (PROMPTS_DIR / "visuals.md").read_text()
    assert len(text.encode()) <= 10752
    for vb in re.findall(r"viewBox='0 0 (\d+) \d+'", text):
        assert 360 <= int(vb) <= 560


def test_prose_prompt_no_longer_asks_for_visuals_or_flow_diagrams():
    text = (PROMPTS_DIR / "prose.md").read_text()
    assert "Story visuals" not in text and "sequenceDiagram" not in text
    assert "flow_mermaid" not in text and "`hop`" not in text and "`side`" not in text
