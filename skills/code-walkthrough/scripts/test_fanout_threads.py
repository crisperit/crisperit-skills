#!/usr/bin/env python3
"""Self-check for fanout_threads.py. Assert-based, no framework."""

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fanout_threads import (  # noqa: E402
    build_seed,
    diff_line_estimate,
    do_split,
    merge,
    window_commits,
)


def _thread(thread_id, first_comment_at="2026-09-01T10:00:00Z"):
    return {
        "thread_id": thread_id,
        "last_comment_id": 1,
        "resolved_by": "octocat",
        "path": "src/auth.py",
        "line": 40,
        "first_comment_at": first_comment_at,
        "body": "extract this into a helper",
        "replies": [{"author": "octocat", "created_at": first_comment_at, "body": "done"}],
    }


def _commit(sha, committed_at, additions=3, deletions=9, path="src/auth.py"):
    return {
        "sha": sha,
        "subject": "address review",
        "committed_at": committed_at,
        "author": "octocat",
        "files": [{"path": path, "additions": additions, "deletions": deletions}],
    }


def _fragment(thread_id, commits=(), outcome="commits", confidence="high"):
    return {
        "thread_id": thread_id,
        "outcome": outcome,
        "closing_message": None,
        "ticket": None,
        "commits": list(commits),
        "files": [{"path": "src/auth.py", "hunks": ["@@ -1,4 +1,6 @@\n"]}] if commits else [],
        "why": "the reviewer wanted it extracted; the commit extracts it" if commits else None,
        "confidence": confidence,
    }


class _Args:
    """Stand-in for the argparse.Namespace do_split reads its options from."""

    def __init__(self, threads, commits, plan, out, max_diff_lines=400, diffs=None):
        self.threads = threads
        self.commits = commits
        self.plan = plan
        self.out = out
        self.max_diff_lines = max_diff_lines
        self.diffs = diffs


OLD_COMMIT = _commit("aaa111", "2026-08-30T00:00:00Z", path="other.py")
IN_WINDOW_COMMIT = _commit("bbb222", "2026-09-02T09:00:00Z")


def test_window_commits_drops_commits_before_the_first_comment():
    kept = window_commits([OLD_COMMIT, IN_WINDOW_COMMIT], "2026-09-01T10:00:00Z")

    assert [c["sha"] for c in kept] == ["bbb222"]


def test_window_commits_keeps_a_commit_at_the_exact_cutoff():
    kept = window_commits([OLD_COMMIT], "2026-08-30T00:00:00Z")

    assert [c["sha"] for c in kept] == ["aaa111"]


def test_diff_budget_switches_to_two_pass_when_the_window_is_too_big():
    thread = _thread("T1")
    entry = {"cache_path_positive": "/pos.json", "cache_path_null": "/null.json"}

    seed, mode = build_seed(thread, [IN_WINDOW_COMMIT], {"bbb222": "diff text"}, max_diff_lines=5,
                             resolution_entry=entry)

    assert mode == "two-pass"
    assert seed["diffs"] == {}


def test_diff_budget_inlines_when_the_window_fits_and_diffs_are_supplied():
    thread = _thread("T1")
    entry = {"cache_path_positive": "/pos.json", "cache_path_null": "/null.json"}

    seed, mode = build_seed(thread, [IN_WINDOW_COMMIT], {"bbb222": "diff text"},
                             max_diff_lines=400, resolution_entry=entry)

    assert mode == "inline"
    assert seed["diffs"] == {"bbb222": "diff text"}


def test_diff_budget_falls_back_to_two_pass_when_no_diffs_were_supplied():
    thread = _thread("T1")
    entry = {"cache_path_positive": "/pos.json", "cache_path_null": "/null.json"}

    seed, mode = build_seed(thread, [IN_WINDOW_COMMIT], None, max_diff_lines=400,
                             resolution_entry=entry)

    assert mode == "two-pass"


def test_empty_window_counts_as_inline_with_no_shas_to_fetch():
    thread = _thread("T1")
    entry = {"cache_path_positive": "/pos.json", "cache_path_null": "/null.json"}

    seed, mode = build_seed(thread, [], None, max_diff_lines=1, resolution_entry=entry)

    assert mode == "inline"
    assert seed["diffs"] == {}


def _write_split_inputs(tmp, threads, commits, resolution):
    threads_path = Path(tmp) / "threads.json"
    commits_path = Path(tmp) / "commit-index.json"
    plan_path = Path(tmp) / "plan.json"
    threads_path.write_text(json.dumps({"threads": threads}))
    commits_path.write_text(json.dumps({"commits": commits}))
    plan_path.write_text(json.dumps({"resolution": resolution}))
    return threads_path, commits_path, plan_path


def _write_cache_hit(tmp, name, commits=(), outcome="commits"):
    """A cached resolution's fragment, as the cache stores it at cache_path_hit."""
    path = Path(tmp) / name
    path.write_text(json.dumps(_fragment("whatever-id", commits=commits, outcome=outcome)))
    return str(path)


def test_split_skips_a_cached_thread_and_writes_no_seed_for_it():
    with tempfile.TemporaryDirectory() as tmp:
        cached = _thread("T-cached")
        missed = _thread("T-missed")
        cache_path_hit = _write_cache_hit(tmp, "pos.json", commits=["bbb222"])
        resolution = [
            {"thread_id": "T-cached", "cached": True, "cache_path_positive": "/pos.json",
             "cache_path_null": "/null.json", "cache_path_hit": cache_path_hit, "cmd": ""},
            {"thread_id": "T-missed", "cached": False, "cache_path_positive": "/pos2.json",
             "cache_path_null": "/null2.json", "cache_path_hit": None, "cmd": "..."},
        ]
        threads_path, commits_path, plan_path = _write_split_inputs(
            tmp, [cached, missed], [IN_WINDOW_COMMIT], resolution
        )
        out_dir = Path(tmp) / "out"

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            do_split(_Args(str(threads_path), str(commits_path), str(plan_path), str(out_dir)))
        manifest = json.loads(buf.getvalue())

        cached_entry = next(m for m in manifest if m["thread_id"] == "T-cached")
        missed_entry = next(m for m in manifest if m["thread_id"] == "T-missed")
        assert cached_entry == {"thread_id": "T-cached", "mode": "cached", "cache_path_hit": cache_path_hit}
        assert missed_entry["seed"] == str(out_dir / "thread-1.seed.json")
        assert not (out_dir / "thread-2.seed.json").exists()
        assert sorted(p.name for p in out_dir.iterdir()) == ["thread-1.seed.json"]


def test_split_reseeds_a_stale_cached_thread_as_invalidated():
    """A cached fragment naming a sha that's no longer in the window -- e.g. rewritten out
    of history by a force-push -- is not trusted; the thread is re-seeded like a miss."""
    with tempfile.TemporaryDirectory() as tmp:
        thread = _thread("T1")
        cache_path_hit = _write_cache_hit(tmp, "pos.json", commits=["ccc333"])
        resolution = [
            {"thread_id": "T1", "cached": True, "cache_path_positive": "/pos.json",
             "cache_path_null": "/null.json", "cache_path_hit": cache_path_hit, "cmd": ""},
        ]
        threads_path, commits_path, plan_path = _write_split_inputs(
            tmp, [thread], [IN_WINDOW_COMMIT], resolution
        )
        out_dir = Path(tmp) / "out"

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            do_split(_Args(str(threads_path), str(commits_path), str(plan_path), str(out_dir)))
        manifest = json.loads(buf.getvalue())

        entry = next(m for m in manifest if m["thread_id"] == "T1")
        assert entry["mode"] == "invalidated"
        assert entry["seed"] == str(out_dir / "thread-1.seed.json")
        assert (out_dir / "thread-1.seed.json").exists()


def test_split_trusts_a_cached_thread_whose_shas_are_all_still_in_the_window():
    with tempfile.TemporaryDirectory() as tmp:
        thread = _thread("T1")
        cache_path_hit = _write_cache_hit(tmp, "pos.json", commits=["bbb222"])
        resolution = [
            {"thread_id": "T1", "cached": True, "cache_path_positive": "/pos.json",
             "cache_path_null": "/null.json", "cache_path_hit": cache_path_hit, "cmd": ""},
        ]
        threads_path, commits_path, plan_path = _write_split_inputs(
            tmp, [thread], [IN_WINDOW_COMMIT], resolution
        )
        out_dir = Path(tmp) / "out"

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            do_split(_Args(str(threads_path), str(commits_path), str(plan_path), str(out_dir)))
        manifest = json.loads(buf.getvalue())

        entry = next(m for m in manifest if m["thread_id"] == "T1")
        assert entry == {"thread_id": "T1", "mode": "cached", "cache_path_hit": cache_path_hit}
        assert list(out_dir.iterdir()) == []


def test_split_never_treats_a_commit_free_outcome_as_stale():
    with tempfile.TemporaryDirectory() as tmp:
        thread = _thread("T1")
        # "conversation"/"deferred"/"none" carry no commits[], so nothing can be out of window.
        cache_path_hit = _write_cache_hit(tmp, "pos.json", commits=[], outcome="conversation")
        resolution = [
            {"thread_id": "T1", "cached": True, "cache_path_positive": "/pos.json",
             "cache_path_null": "/null.json", "cache_path_hit": cache_path_hit, "cmd": ""},
        ]
        threads_path, commits_path, plan_path = _write_split_inputs(
            tmp, [thread], [IN_WINDOW_COMMIT], resolution
        )
        out_dir = Path(tmp) / "out"

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            do_split(_Args(str(threads_path), str(commits_path), str(plan_path), str(out_dir)))
        manifest = json.loads(buf.getvalue())

        entry = next(m for m in manifest if m["thread_id"] == "T1")
        assert entry["mode"] == "cached"
        assert list(out_dir.iterdir()) == []


def test_split_window_filtering_reaches_the_written_seed():
    with tempfile.TemporaryDirectory() as tmp:
        thread = _thread("T1", first_comment_at="2026-09-01T10:00:00Z")
        resolution = [{"thread_id": "T1", "cached": False, "cache_path_positive": "/p.json",
                       "cache_path_null": "/n.json", "cache_path_hit": None, "cmd": ""}]
        threads_path, commits_path, plan_path = _write_split_inputs(
            tmp, [thread], [OLD_COMMIT, IN_WINDOW_COMMIT], resolution
        )
        out_dir = Path(tmp) / "out"

        with contextlib.redirect_stdout(io.StringIO()):
            do_split(_Args(str(threads_path), str(commits_path), str(plan_path), str(out_dir)))

        seed = json.loads((out_dir / "thread-1.seed.json").read_text())
        assert [c["sha"] for c in seed["commits"]] == ["bbb222"]


def test_merge_rejects_a_sha_outside_the_threads_candidate_window():
    with tempfile.TemporaryDirectory() as tmp:
        fragment = Path(tmp) / "thread-1.json"
        fragment.write_text(json.dumps(_fragment("T1", commits=["ccc333"])))

        try:
            merge([_thread("T1")], [IN_WINDOW_COMMIT], [str(fragment)])
        except RuntimeError as exc:
            assert "ccc333" in str(exc)
        else:
            raise AssertionError("out-of-window sha was accepted")


def test_merge_accepts_a_sha_inside_the_candidate_window():
    with tempfile.TemporaryDirectory() as tmp:
        fragment = Path(tmp) / "thread-1.json"
        fragment.write_text(json.dumps(_fragment("T1", commits=["bbb222"])))

        result = merge([_thread("T1")], [IN_WINDOW_COMMIT], [str(fragment)])

        assert result["resolutions"]["T1"]["commits"] == ["bbb222"]


def test_merge_validates_a_formerly_exempt_cached_fragment():
    """A cached thread's copied-in fragment has no sibling seed file, but is still checked
    against the thread's window like any other fragment."""
    with tempfile.TemporaryDirectory() as tmp:
        fragment = Path(tmp) / "thread-cached.json"
        fragment.write_text(json.dumps(_fragment("T1", commits=["ccc333"])))

        try:
            merge([_thread("T1")], [IN_WINDOW_COMMIT], [str(fragment)])
        except RuntimeError as exc:
            assert "ccc333" in str(exc)
        else:
            raise AssertionError("out-of-window sha in a cached fragment was accepted")


def test_merge_skips_seed_files_picked_up_by_a_naive_glob():
    with tempfile.TemporaryDirectory() as tmp:
        seed = Path(tmp) / "thread-1.seed.json"
        seed.write_text(json.dumps({"thread_id": "T1"}))
        fragment = Path(tmp) / "thread-1.json"
        fragment.write_text(json.dumps(_fragment("T1", commits=["bbb222"])))

        result = merge([_thread("T1")], [IN_WINDOW_COMMIT], [str(seed), str(fragment)])

        assert set(result["resolutions"]) == {"T1"}


def test_merge_reports_a_missing_fragment_by_path_instead_of_crashing():
    with tempfile.TemporaryDirectory() as tmp:
        ghost = Path(tmp) / "thread-1.json"

        try:
            merge([_thread("T1")], [], [str(ghost)])
        except RuntimeError as exc:
            assert str(ghost) in str(exc)
        else:
            raise AssertionError("missing fragment file was not reported")


def test_merge_fails_coverage_when_a_thread_id_has_no_fragment():
    with tempfile.TemporaryDirectory() as tmp:
        fragment = Path(tmp) / "thread-1.json"
        fragment.write_text(json.dumps(_fragment("T1")))

        try:
            merge([_thread("T1"), _thread("T2")], [], [str(fragment)])
        except RuntimeError as exc:
            assert "T2" in str(exc)
        else:
            raise AssertionError("missing coverage for T2 was not reported")


def test_merge_rejects_a_fragment_that_does_not_match_contract_d():
    with tempfile.TemporaryDirectory() as tmp:
        fragment = Path(tmp) / "thread-1.json"
        bad = _fragment("T1", commits=["bbb222"])
        bad["outcome"] = "bogus"
        fragment.write_text(json.dumps(bad))

        try:
            merge([_thread("T1")], [IN_WINDOW_COMMIT], [str(fragment)])
        except RuntimeError as exc:
            assert "bogus" in str(exc)
        else:
            raise AssertionError("malformed fragment was accepted")


def test_round_trip_split_then_merge_covers_both_a_miss_and_a_cached_thread():
    with tempfile.TemporaryDirectory() as tmp:
        cached_thread = _thread("T-cached")
        missed_thread = _thread("T-missed")
        cache_path_hit = _write_cache_hit(tmp, "pos.json", commits=[], outcome="conversation")
        resolution = [
            {"thread_id": "T-cached", "cached": True, "cache_path_positive": "/pos.json",
             "cache_path_null": "/null.json", "cache_path_hit": cache_path_hit, "cmd": ""},
            {"thread_id": "T-missed", "cached": False, "cache_path_positive": "/pos2.json",
             "cache_path_null": "/null2.json", "cache_path_hit": None, "cmd": "..."},
        ]
        threads_path, commits_path, plan_path = _write_split_inputs(
            tmp, [cached_thread, missed_thread], [IN_WINDOW_COMMIT], resolution
        )
        out_dir = Path(tmp) / "out"

        with contextlib.redirect_stdout(io.StringIO()):
            do_split(_Args(str(threads_path), str(commits_path), str(plan_path), str(out_dir)))

        # The worker's real output for the miss, plus the driver's copy of the cache hit
        # for the cached thread -- both land in the same fragments directory merge reads.
        (out_dir / "thread-1.json").write_text(
            json.dumps(_fragment("T-missed", commits=["bbb222"]))
        )
        (out_dir / "thread-cached.json").write_text(
            json.dumps(_fragment("T-cached", commits=[], outcome="conversation"))
        )

        result = merge(
            [cached_thread, missed_thread],
            [IN_WINDOW_COMMIT],
            [str(out_dir / "thread-1.json"), str(out_dir / "thread-cached.json")],
        )

        assert set(result["resolutions"]) == {"T-cached", "T-missed"}
        assert result["resolutions"]["T-missed"]["commits"] == ["bbb222"]
        assert result["resolutions"]["T-cached"]["outcome"] == "conversation"


def test_diff_line_estimate_sums_additions_and_deletions():
    assert diff_line_estimate([IN_WINDOW_COMMIT]) == 12  # 3 + 9, see _commit's defaults


if __name__ == "__main__":
    tests = [
        test_window_commits_drops_commits_before_the_first_comment,
        test_window_commits_keeps_a_commit_at_the_exact_cutoff,
        test_diff_budget_switches_to_two_pass_when_the_window_is_too_big,
        test_diff_budget_inlines_when_the_window_fits_and_diffs_are_supplied,
        test_diff_budget_falls_back_to_two_pass_when_no_diffs_were_supplied,
        test_empty_window_counts_as_inline_with_no_shas_to_fetch,
        test_split_skips_a_cached_thread_and_writes_no_seed_for_it,
        test_split_reseeds_a_stale_cached_thread_as_invalidated,
        test_split_trusts_a_cached_thread_whose_shas_are_all_still_in_the_window,
        test_split_never_treats_a_commit_free_outcome_as_stale,
        test_split_window_filtering_reaches_the_written_seed,
        test_merge_rejects_a_sha_outside_the_threads_candidate_window,
        test_merge_accepts_a_sha_inside_the_candidate_window,
        test_merge_validates_a_formerly_exempt_cached_fragment,
        test_merge_skips_seed_files_picked_up_by_a_naive_glob,
        test_merge_reports_a_missing_fragment_by_path_instead_of_crashing,
        test_merge_fails_coverage_when_a_thread_id_has_no_fragment,
        test_merge_rejects_a_fragment_that_does_not_match_contract_d,
        test_round_trip_split_then_merge_covers_both_a_miss_and_a_cached_thread,
        test_diff_line_estimate_sums_additions_and_deletions,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
