#!/usr/bin/env python3
"""Split resolved review threads into per-thread seeds for parallel analysis, then merge
the results.

  python3 fanout_threads.py split --threads threads.json --commits commit-index.json \
      --plan <regen plan json> --out <dir> [--max-diff-lines N] [--diffs diffs.json]
  python3 fanout_threads.py merge --threads threads.json --commits commit-index.json \
      --fragments <dir>/thread-*.json --out resolutions.json

Same shape as fanout.py (one seed per unit, one cheap model per unit, merge at the end),
but a thread is not a diff slice: fanout.py's split_chunks/batch/rename_map/merge are all
built on `diff --git` boundaries and cannot be reused here.

split writes <dir>/thread-N.seed.json for every thread the given regen plan's
plan["resolution"] marks as a miss (cached==false), plus any thread marked cached whose
cache_path_hit fragment names a commit sha outside the thread's current window (a
force-push rewrote history after the cache was written, so the hit is stale and gets
re-seeded like a miss, reported as "invalidated"). A cached thread whose fragment's shas
(if any) are all still in the window gets no seed; its answer already lives at
cache_path_hit. N counts only the seeds actually written, in threads.json order. Prints a
JSON manifest on stdout: one entry per thread,
{"thread_id", "mode": "cached"|"invalidated"|"inline"|"two-pass", ...}.

A thread's window is every commit-index.json commit with committed_at >= the thread's
first_comment_at -- no upper bound, and never filtered by path (a comment on one file can
be answered entirely in others). --diffs, if given, is a {sha: unified diff text} map (the
agent's own `git show`, piped to a file -- this script never calls git). When the window's
total +/- lines fit --max-diff-lines and --diffs covers every window sha, the seed inlines
those diffs ("inline" mode); otherwise it ships {} and the driver fetches on demand before
re-invoking the worker ("two-pass" mode).

merge collects thread-N.json fragments keyed by thread_id (not by path), recomputing every
thread's candidate window itself from --commits and threads.json (window_commits, same as
split) -- the hallucination guard, applied uniformly to every fragment whether it came from
a worker or was copied in for a cached thread. It skips *.seed.json paths outright, since
split's own seeds live in the same directory and a naive `thread-*.json` glob picks them up
too. It also demands full coverage: every threads.json thread_id appears exactly once in
the output. Unlike fanout.py's merge, a missing fragment file is reported by path, not an
uncaught FileNotFoundError.

Fragment shape, one per thread: {"thread_id", "outcome": "conversation"|
"deferred"|"commits"|"none", "closing_message", "ticket", "commits": [sha, ...],
"files": [{"path", "hunks": [str, ...]}], "why", "confidence": "high"|"medium"|"low"}.

Stdlib only, no network.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

OUTCOMES = {"conversation", "deferred", "commits", "none"}
CONFIDENCES = {"high", "medium", "low"}
FRAGMENT_KEYS = (
    "thread_id", "outcome", "closing_message", "ticket", "commits", "files", "why",
    "confidence",
)


def _parse_iso(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def window_commits(commits, first_comment_at):
    """Commits at or after the thread's first comment. No path filter: see module docstring."""
    cutoff = _parse_iso(first_comment_at)
    return [c for c in commits if _parse_iso(c["committed_at"]) >= cutoff]


def diff_line_estimate(commits):
    """Sum of +/- lines across a commit list's files, from commit-index.json's counts --
    the only diff-size signal split has without shelling out to git."""
    return sum(f["additions"] + f["deletions"] for c in commits for f in c["files"])


def build_seed(thread, window, diffs_map, max_diff_lines, resolution_entry):
    """Return (seed dict for the worker's input, mode). mode is "two-pass" only when the window has
    commits to answer for and either the budget or --diffs can't cover them; an empty
    window has nothing to fetch either way, so it counts as "inline"."""
    shas = [c["sha"] for c in window]
    fits_budget = diff_line_estimate(window) <= max_diff_lines
    have_all_diffs = diffs_map is not None and all(sha in diffs_map for sha in shas)
    if shas and not (fits_budget and have_all_diffs):
        diffs, mode = {}, "two-pass"
    else:
        diffs, mode = ({sha: diffs_map[sha] for sha in shas} if have_all_diffs else {}), "inline"
    seed = {
        "thread_id": thread["thread_id"],
        "thread": thread,
        "commits": window,
        "diffs": diffs,
        "cache_path_positive": resolution_entry["cache_path_positive"],
        "cache_path_null": resolution_entry["cache_path_null"],
    }
    return seed, mode


def _cache_is_stale(cached_fragment, window):
    """True once a cached resolution names a commit sha outside the thread's current
    window -- e.g. a force-push rewrote history after the cache was written. Outcomes with
    no commits[] (see module docstring) can never go stale."""
    if cached_fragment.get("outcome") in ("conversation", "deferred", "none"):
        return False
    window_shas = {c["sha"] for c in window}
    return any(sha not in window_shas for sha in cached_fragment.get("commits", []))


def do_split(args):
    threads = json.loads(Path(args.threads).read_text())["threads"]
    commits = json.loads(Path(args.commits).read_text())["commits"]
    plan = json.loads(Path(args.plan).read_text())
    diffs_map = json.loads(Path(args.diffs).read_text()) if args.diffs else None
    resolution = {entry["thread_id"]: entry for entry in plan.get("resolution", [])}

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = []
    n = 0
    for thread in threads:
        thread_id = thread["thread_id"]
        entry = resolution.get(thread_id)
        if entry is None:
            raise RuntimeError(f"no resolution plan entry for thread {thread_id}")
        window = window_commits(commits, thread["first_comment_at"])
        stale = False
        if entry.get("cached"):
            cached_fragment = json.loads(Path(entry["cache_path_hit"]).read_text())
            stale = _cache_is_stale(cached_fragment, window)
            if not stale:
                manifest.append({
                    "thread_id": thread_id,
                    "mode": "cached",
                    "cache_path_hit": entry.get("cache_path_hit"),
                })
                continue
        n += 1
        seed, mode = build_seed(thread, window, diffs_map, args.max_diff_lines, entry)
        seed_path = out_dir / f"thread-{n}.seed.json"
        seed_path.write_text(json.dumps(seed, indent=2) + "\n")
        manifest.append({
            "thread_id": thread_id,
            "mode": "invalidated" if stale else mode,
            "seed": str(seed_path),
        })

    print(json.dumps(manifest, indent=2))
    return 0


def _validate_fragment(data):
    if not isinstance(data, dict):
        raise ValueError("fragment must be a JSON object")
    missing = [key for key in FRAGMENT_KEYS if key not in data]
    if missing:
        raise ValueError(f"missing key(s): {', '.join(missing)}")
    if data["outcome"] not in OUTCOMES:
        raise ValueError(f"invalid outcome: {data['outcome']!r}")
    if data["confidence"] not in CONFIDENCES:
        raise ValueError(f"invalid confidence: {data['confidence']!r}")
    if not isinstance(data["commits"], list):
        raise ValueError("commits must be a list")
    if not isinstance(data["files"], list):
        raise ValueError("files must be a list")


def merge(threads, commits, fragment_paths):
    """threads: threads.json's "threads" list. commits: commit-index.json's "commits" list,
    used to recompute each thread's candidate window (window_commits) for the hallucination
    guard. Returns {"resolutions": {thread_id: <fragment dict, see module docstring>}}."""
    expected = {t["thread_id"] for t in threads}
    windows = {
        t["thread_id"]: {c["sha"] for c in window_commits(commits, t["first_comment_at"])}
        for t in threads
    }
    results, errors = {}, []
    for fragment_path in fragment_paths:
        if Path(fragment_path).name.endswith(".seed.json"):
            continue
        try:
            data = json.loads(Path(fragment_path).read_text())
        except FileNotFoundError:
            errors.append(f"{fragment_path}: no such fragment file")
            continue
        except json.JSONDecodeError as exc:
            errors.append(f"{fragment_path}: {exc}")
            continue
        try:
            _validate_fragment(data)
        except ValueError as exc:
            errors.append(f"{fragment_path}: {exc}")
            continue
        thread_id = data["thread_id"]
        if thread_id not in expected:
            errors.append(f"{fragment_path}: unknown thread_id {thread_id!r}")
            continue
        bogus = [sha for sha in data["commits"] if sha not in windows[thread_id]]
        if bogus:
            errors.append(
                f"{fragment_path}: commits {bogus} not in thread {thread_id}'s window"
            )
            continue
        if thread_id in results:
            errors.append(f"{fragment_path}: duplicate fragment for thread {thread_id}")
            continue
        results[thread_id] = data
    if errors:
        raise RuntimeError("\n".join(errors))

    missing_ids = expected - set(results)
    if missing_ids:
        raise RuntimeError(f"no resolution for thread(s): {', '.join(sorted(missing_ids))}")

    return {"resolutions": results}


def do_merge(args):
    threads = json.loads(Path(args.threads).read_text())["threads"]
    commits = json.loads(Path(args.commits).read_text())["commits"]
    result = merge(threads, commits, args.fragments)
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    print(f"merged {len(result['resolutions'])} thread resolution(s) into {args.out}")
    return 0


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)

    split_parser = sub.add_parser("split")
    split_parser.add_argument("--threads", required=True)
    split_parser.add_argument("--commits", required=True)
    split_parser.add_argument("--plan", required=True)
    split_parser.add_argument("--out", required=True)
    split_parser.add_argument("--max-diff-lines", type=int, default=400)
    split_parser.add_argument("--diffs")
    split_parser.set_defaults(func=do_split)

    merge_parser = sub.add_parser("merge")
    merge_parser.add_argument("--threads", required=True)
    merge_parser.add_argument("--commits", required=True)
    merge_parser.add_argument("--fragments", nargs="+", required=True)
    merge_parser.add_argument("--out", required=True)
    merge_parser.set_defaults(func=do_merge)

    args = parser.parse_args()
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
