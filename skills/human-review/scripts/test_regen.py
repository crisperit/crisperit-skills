#!/usr/bin/env python3
"""Self-check for regen.py. Assert-based, no framework, no network."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import regen  # noqa: E402


def _diff(path, index_line, header, body_lines):
    """One diff --git block: `index_line` is included verbatim when not None (the
    degradation case), `header` is the bare @@ prefix, `body_lines` are raw lines
    (leading +/-/space kept) making up the hunk body."""
    parts = [f"diff --git a/{path} b/{path}"]
    if index_line is not None:
        parts.append(index_line)
    parts.append(f"--- a/{path}")
    parts.append(f"+++ b/{path}")
    parts.append(header)
    parts.extend(body_lines)
    return "\n".join(parts) + "\n"


INDEX = "index aaa1111..bbb2222 100644"
BODY = [" ctx", "+added"]


def test_hunk_hash_survives_a_pure_line_number_shift():
    prior_diff = _diff("f.py", INDEX, "@@ -10,3 +10,4 @@", BODY)
    later_diff = _diff("f.py", INDEX, "@@ -20,3 +20,4 @@", BODY)

    prior_hash = regen.hunk_records(prior_diff)[0]["hash"]
    later_hash = regen.hunk_records(later_diff)[0]["hash"]

    assert prior_hash == later_hash


def test_hunk_hash_changes_when_a_body_line_changes():
    prior_diff = _diff("f.py", INDEX, "@@ -10,3 +10,4 @@", BODY)
    changed_diff = _diff("f.py", INDEX, "@@ -10,3 +10,4 @@", [" ctx", "+added2"])

    prior_hash = regen.hunk_records(prior_diff)[0]["hash"]
    changed_hash = regen.hunk_records(changed_diff)[0]["hash"]

    assert prior_hash != changed_hash


def test_plan_hunks_carries_forward_the_unchanged_hunk_and_flags_the_changed_one():
    prior_diff = _diff("f.py", INDEX, "@@ -10,3 +10,4 @@", BODY)
    later_diff = _diff("f.py", INDEX, "@@ -20,3 +20,4 @@", BODY)
    prior_records = regen.hunk_records(prior_diff)
    prior_hunks = [{"path": r["path"], "hash": r["hash"], "note": "kept from before"}
                   for r in prior_records]

    later_records = regen.hunk_records(later_diff)
    plan = regen.plan_hunks(later_records, prior_hunks)

    assert plan[later_records[0]["id"]] == {"carry": True, "note": "kept from before"}

    changed_diff = _diff("f.py", INDEX, "@@ -10,3 +10,4 @@", [" ctx", "+added2"])
    changed_records = regen.hunk_records(changed_diff)
    changed_plan = regen.plan_hunks(changed_records, prior_hunks)

    assert changed_plan[changed_records[0]["id"]]["carry"] is False


def test_set_hash_changes_exactly_when_a_hunk_hash_changes():
    prior_diff = _diff("f.py", INDEX, "@@ -10,3 +10,4 @@", BODY)
    shifted_diff = _diff("f.py", INDEX, "@@ -20,3 +20,4 @@", BODY)
    changed_diff = _diff("f.py", INDEX, "@@ -10,3 +10,4 @@", [" ctx", "+added2"])

    prior_set = regen.set_hash(regen.hunk_records(prior_diff))
    shifted_set = regen.set_hash(regen.hunk_records(shifted_diff))
    changed_set = regen.set_hash(regen.hunk_records(changed_diff))

    assert prior_set == shifted_set
    assert prior_set != changed_set


def test_no_index_line_falls_back_to_body_only_and_is_always_stale():
    no_index_diff = _diff("r.py", None, "@@ -1,2 +1,3 @@", BODY)
    with_index_diff = _diff("r.py", INDEX, "@@ -1,2 +1,3 @@", BODY)

    stale_record = regen.hunk_records(no_index_diff)[0]
    assert stale_record["always_stale"] is True

    # Fabricate a prior hunk whose hash matches the fallback hash exactly -- even then,
    # always_stale must refuse the carry, because a matching fallback hash proves nothing
    # about the file (there is no blob sha to compare).
    prior_hunks = [{"path": "r.py", "hash": stale_record["hash"], "note": "should not carry"}]
    plan = regen.plan_hunks([stale_record], prior_hunks)
    assert plan[stale_record["id"]]["carry"] is False

    # The same body with a real index line is not always-stale and does carry.
    normal_record = regen.hunk_records(with_index_diff)[0]
    normal_prior = [{"path": "r.py", "hash": normal_record["hash"], "note": "carries"}]
    normal_plan = regen.plan_hunks([normal_record], normal_prior)
    assert normal_plan[normal_record["id"]]["carry"] is True


def test_two_identical_hunks_in_one_file_resolve_first_unmatched_wins():
    # Same body text at two different line ranges in the same file: different ids (the
    # header differs) but the same hash (body and blob are what the hash is over), the
    # collision this module documents as a known ceiling.
    dup_diff = (
        f"diff --git a/f.py b/f.py\n{INDEX}\n--- a/f.py\n+++ b/f.py\n"
        "@@ -1,2 +1,3 @@\n" + "\n".join(BODY) + "\n"
        "@@ -20,2 +21,3 @@\n" + "\n".join(BODY) + "\n"
    )
    records = regen.hunk_records(dup_diff)
    assert len(records) == 2
    assert records[0]["id"] != records[1]["id"]
    assert records[0]["hash"] == records[1]["hash"]

    prior_hunks = [
        {"path": "f.py", "hash": records[0]["hash"], "note": "note-A"},
        {"path": "f.py", "hash": records[0]["hash"], "note": "note-B"},
    ]
    plan = regen.plan_hunks(records, prior_hunks)

    assert [plan[r["id"]]["note"] for r in records] == ["note-A", "note-B"]


def test_ref_based_analysers_cache_hit_depends_only_on_base_and_head():
    with tempfile.TemporaryDirectory() as tmp:
        key = regen.analyser_key("coupling", "base1", "head1")
        (Path(tmp) / f"{key}.json").write_text("{}")

        light_hunks = regen.plan_analysers(tmp, "/repo", "base1", "head1", "raw.diff", "sethash-1")
        heavy_hunks = regen.plan_analysers(tmp, "/repo", "base1", "head1", "raw.diff", "sethash-2")

        assert light_hunks["coupling"]["status"] == "hit"
        assert heavy_hunks["coupling"]["status"] == "hit"
        assert light_hunks["coupling"]["key"] == heavy_hunks["coupling"]["key"]
        # complexity is keyed on the set hash, so it is the one allowed to differ here.
        assert light_hunks["complexity"]["key"] != heavy_hunks["complexity"]["key"]


def test_bumping_script_version_invalidates_the_ref_based_cache():
    original = regen.SCRIPT_VERSION["coupling"]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            key = regen.analyser_key("coupling", "base1", "head1")
            (Path(tmp) / f"{key}.json").write_text("{}")
            before = regen.plan_analysers(tmp, "/repo", "base1", "head1", "raw.diff", "sethash")
            assert before["coupling"]["status"] == "hit"

            regen.SCRIPT_VERSION["coupling"] = original + 1
            after = regen.plan_analysers(tmp, "/repo", "base1", "head1", "raw.diff", "sethash")
            assert after["coupling"]["status"] == "miss"
            assert after["coupling"]["key"] != before["coupling"]["key"]
    finally:
        regen.SCRIPT_VERSION["coupling"] = original


def test_fill_seeds_prefills_role_and_note_and_flags_a_fully_carried_batch():
    hunk_plan = {
        regen.hunk_id("a.py", "@@ -10,3 +10,4 @@"): {"carry": True, "note": "carried note"},
    }
    prior_roles = {"a.py": "carried role"}
    seed = {"files": [{
        "path": "a.py", "role": "",
        "hunks": [{"header": "@@ -10,3 +10,4 @@ def go():", "note": ""}],
    }]}
    with tempfile.TemporaryDirectory() as tmp:
        seed_path = Path(tmp) / "fragment-1.seed.json"
        seed_path.write_text(json.dumps(seed))

        skip = regen.fill_seeds(str(seed_path), hunk_plan, prior_roles)

        assert skip is True
        rewritten = json.loads(seed_path.read_text())
        assert rewritten["files"][0]["role"] == "carried role"
        assert rewritten["files"][0]["hunks"][0]["note"] == "carried note"


def test_fill_seeds_leaves_an_unmatched_hunk_blank_and_does_not_skip_the_batch():
    hunk_plan = {
        regen.hunk_id("a.py", "@@ -10,3 +10,4 @@"): {"carry": True, "note": "carried note"},
        regen.hunk_id("b.py", "@@ -1,2 +1,3 @@"): {"carry": False, "note": ""},
    }
    prior_roles = {"a.py": "carried role", "b.py": "stale role"}
    seed = {"files": [
        {"path": "a.py", "role": "",
         "hunks": [{"header": "@@ -10,3 +10,4 @@", "note": ""}]},
        {"path": "b.py", "role": "",
         "hunks": [{"header": "@@ -1,2 +1,3 @@", "note": ""}]},
    ]}
    with tempfile.TemporaryDirectory() as tmp:
        seed_path = Path(tmp) / "fragment-2.seed.json"
        seed_path.write_text(json.dumps(seed))

        skip = regen.fill_seeds(str(seed_path), hunk_plan, prior_roles)

        assert skip is False
        rewritten = json.loads(seed_path.read_text())
        assert rewritten["files"][0]["role"] == "carried role"
        assert rewritten["files"][1]["role"] == ""
        assert rewritten["files"][1]["hunks"][0]["note"] == ""


def test_fill_seeds_skips_the_batch_when_a_zero_hunk_file_carries_by_role():
    hunk_plan = {
        regen.hunk_id("a.py", "@@ -10,3 +10,4 @@"): {"carry": True, "note": "carried note"},
    }
    prior_roles = {"a.py": "carried role", "b.bin": "binary role"}
    seed = {"files": [
        {"path": "a.py", "role": "",
         "hunks": [{"header": "@@ -10,3 +10,4 @@", "note": ""}]},
        {"path": "b.bin", "role": "", "hunks": []},
    ]}
    with tempfile.TemporaryDirectory() as tmp:
        seed_path = Path(tmp) / "fragment-3.seed.json"
        seed_path.write_text(json.dumps(seed))

        skip = regen.fill_seeds(str(seed_path), hunk_plan, prior_roles)

        assert skip is True
        rewritten = json.loads(seed_path.read_text())
        assert rewritten["files"][1]["role"] == "binary role"


def test_dirty_reads_the_head_file_and_compares_to_the_built_sha():
    with tempfile.TemporaryDirectory() as tmp:
        head_file = Path(tmp) / "HEAD"
        head_file.write_text("abc123\n")

        assert regen.dirty(str(head_file), "abc123") is False
        assert regen.dirty(str(head_file), "def456") is True
        # No prior head_sha at all (first build) is not "dirty", just unknown.
        assert regen.dirty(str(head_file), None) is False
        # A head file that does not exist yet (no build has happened) reads as not dirty.
        assert regen.dirty(str(Path(tmp) / "missing"), "abc123") is False


def test_dirty_follows_a_symbolic_ref_to_the_branchs_sha():
    with tempfile.TemporaryDirectory() as tmp:
        git_dir = Path(tmp)
        (git_dir / "refs" / "heads").mkdir(parents=True)
        head_file = git_dir / "HEAD"
        head_file.write_text("ref: refs/heads/dev\n")
        (git_dir / "refs" / "heads" / "dev").write_text("abc123\n")

        assert regen.dirty(str(head_file), "abc123") is False
        assert regen.dirty(str(head_file), "def456") is True

        # A ref with no loose file (packed) has no sha to compare: not dirty, not guessed.
        packed_head = git_dir / "HEAD"
        packed_head.write_text("ref: refs/heads/packed-only\n")
        assert regen.dirty(str(packed_head), "abc123") is False


def test_dirty_follows_a_linked_worktrees_commondir_to_the_common_repos_refs():
    with tempfile.TemporaryDirectory() as tmp:
        git_dir = Path(tmp)
        (git_dir / "refs" / "heads").mkdir(parents=True)
        (git_dir / "refs" / "heads" / "dev").write_text("abc123\n")

        worktree_dir = git_dir / "worktrees" / "wt"
        worktree_dir.mkdir(parents=True)
        head_file = worktree_dir / "HEAD"
        head_file.write_text("ref: refs/heads/dev\n")
        (worktree_dir / "commondir").write_text("../..\n")

        assert regen.dirty(str(head_file), "abc123") is False
        assert regen.dirty(str(head_file), "def456") is True


if __name__ == "__main__":
    tests = [
        test_hunk_hash_survives_a_pure_line_number_shift,
        test_hunk_hash_changes_when_a_body_line_changes,
        test_plan_hunks_carries_forward_the_unchanged_hunk_and_flags_the_changed_one,
        test_set_hash_changes_exactly_when_a_hunk_hash_changes,
        test_no_index_line_falls_back_to_body_only_and_is_always_stale,
        test_two_identical_hunks_in_one_file_resolve_first_unmatched_wins,
        test_ref_based_analysers_cache_hit_depends_only_on_base_and_head,
        test_bumping_script_version_invalidates_the_ref_based_cache,
        test_fill_seeds_prefills_role_and_note_and_flags_a_fully_carried_batch,
        test_fill_seeds_leaves_an_unmatched_hunk_blank_and_does_not_skip_the_batch,
        test_fill_seeds_skips_the_batch_when_a_zero_hunk_file_carries_by_role,
        test_dirty_reads_the_head_file_and_compares_to_the_built_sha,
        test_dirty_follows_a_symbolic_ref_to_the_branchs_sha,
        test_dirty_follows_a_linked_worktrees_commondir_to_the_common_repos_refs,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
