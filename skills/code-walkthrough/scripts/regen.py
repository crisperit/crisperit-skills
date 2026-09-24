#!/usr/bin/env python3
"""Compute cache keys for incremental regeneration and print a plan. Runs nothing.

  python3 regen.py --diff raw.diff --repo <repo> --base <base_sha> --head <head_sha> \
      --cache-dir <scratchpad>/cache [--prior-state <scratchpad>/state.json] \
      [--manifest <fanout split manifest.json>] [--head-file <path from
      `git rev-parse --git-path HEAD`>] [--threads <resolved-threads.json>]

This module only computes keys, decides cache hit/miss by checking whether
`<cache-dir>/<key>.json` exists, and pre-fills fanout.py's fragment-N.seed.json files in
place from the prior state.json. It never shells out, never calls git, never runs an
analyser, never spawns a subagent. The agent reads the printed plan and runs the named
commands itself.

Keys:
  per-hunk annotation      sha256(hunk body lines + "\\n" + file blob sha)[:16], body only,
                           never the `@@` header, so a pure line-number shift keeps the hash
  set hash                 sha256 over sorted (path, hunk hash) pairs; drives grouping/prose
  symdelta                 (base_sha, head_sha, SCRIPT_VERSION); it diffs internally
  complexity/links         set hash, keyed with SCRIPT_VERSION same as the others
  resolution               a positive/null key pair per thread, see below

Degradation: a file with no `index` line (rename-only in practice, not binaries) has no
blob sha. Its hunks still hash (falling back to body-only), but they are marked
`always_stale` and `plan_hunks` never carries them forward, matching hash or not.

Known ceiling: two textually identical hunks in one file collide on (path, hash). Resolved
first-unmatched-wins in file order, which can swap two identical hunks' carried notes.
Since the hunks are byte-identical, so is any note that actually explains them; not worth
a smarter identity key.

This script runs twice per regeneration: once for hunk/analyser caching (`--manifest`, as
above) and again, later, once thread data exists, for resolution caching (`--threads`).
The two are never passed in the same invocation.

Resolution keys on (thread_id, last_comment_id), asymmetrically: the positive key omits
head_sha, so a thread whose resolution WAS found caches forever no matter how many commits
land afterwards. The null key ("no change found") includes head_sha, because that answer is
worth retrying once there is new code to look at, so it falls out of cache naturally when
head moves. `plan_resolution` checks the positive path first, then the null path.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from validate_analysis import HUNK_PREFIX, parse_hunks  # noqa: E402

# Bumped by hand when a script's own behaviour changes, so a cache entry keyed on git refs
# alone (which say nothing about the script that produced it) doesn't survive the edit.
SCRIPT_VERSION = {
    "symdelta": 2,
    "complexity": 1,
    "links": 1,
    "resolution": 1,
}
# Takes --base/--head and diffs internally rather than consuming raw.diff, so a ref pair is
# its whole input and a better key than any diff hash.
REF_BASED = ("symdelta",)


def hunk_id(path, prefix):
    return f"{path}\t{prefix}"


def hunk_hash(hunk, blob):
    """sha256(body lines + "\\n" + blob)[:16]. `blob` is "" for the always-stale
    degradation case; callers must not treat a matching fallback hash as a real match."""
    body = "\n".join(raw for _, raw in hunk["lines"])
    return hashlib.sha256(f"{body}\n{blob or ''}".encode()).hexdigest()[:16]


def hunk_records(diff_text):
    """[{"id", "path", "prefix", "hash", "always_stale"}], one per hunk, in diff order."""
    order, files = parse_hunks(diff_text)
    records = []
    for path in order:
        blob = files[path]["blob"]
        for hunk in files[path]["hunks"]:
            records.append({
                "id": hunk_id(path, hunk["prefix"]),
                "path": path,
                "prefix": hunk["prefix"],
                "hash": hunk_hash(hunk, blob),
                "always_stale": not blob,
            })
    return records


def set_hash(records):
    """sha256 over sorted (path, hash) pairs. Changes when any hunk's hash changes."""
    pairs = sorted((r["path"], r["hash"]) for r in records)
    text = "\n".join(f"{p}\t{h}" for p, h in pairs)
    return hashlib.sha256(text.encode()).hexdigest()


def plan_hunks(records, prior_hunks):
    """Decide carry-forward vs re-annotate per current hunk.

    Matches within the same path and hash only, first-unmatched-wins in file order (the
    collision ceiling documented in the module docstring). Because a hunk's hash already
    folds in its file's whole blob sha, a file whose content changed anywhere invalidates
    every one of its hunks at once, not just the edited one -- conservative in the safe
    direction, so this is an all-or-nothing decision per file in practice, never a mix.

    prior_hunks: prior state.json's `hunks[]`, each a dict with at least "path", "hash",
    "note". Returns {hunk_id: {"carry": bool, "note": str}}.
    """
    pool = {}
    for h in prior_hunks:
        pool.setdefault(h["path"], {}).setdefault(h["hash"], []).append(h)

    plan = {}
    for r in records:
        candidates = pool.get(r["path"], {}).get(r["hash"], [])
        if not r["always_stale"] and candidates:
            prior = candidates.pop(0)
            plan[r["id"]] = {"carry": True, "note": prior.get("note", "")}
        else:
            plan[r["id"]] = {"carry": False, "note": ""}
    return plan


def analyser_key(script, *parts):
    raw = "|".join([script, str(SCRIPT_VERSION[script]), *(str(p) for p in parts)])
    return f"{script}-{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def plan_analyser(cache_dir, script, key, cmd):
    cache_path = str(Path(cache_dir) / f"{key}.json")
    hit = Path(cache_path).exists()
    return {
        "script": script,
        "key": key,
        "cache_path": cache_path,
        "status": "hit" if hit else "miss",
        "action": f"copy {cache_path}" if hit else cmd,
    }


def plan_resolution(cache_dir, thread, head_sha):
    """Contract-(F) entry for one resolved thread: checks the positive cache path before
    the null path (asymmetry documented in the module docstring)."""
    thread_id = thread["thread_id"]
    last_comment_id = thread["last_comment_id"]
    positive_key = analyser_key("resolution", thread_id, last_comment_id)
    null_key = analyser_key("resolution", thread_id, last_comment_id, head_sha)
    cache_path_positive = str(Path(cache_dir) / f"{positive_key}.json")
    cache_path_null = str(Path(cache_dir) / f"{null_key}.json")

    if Path(cache_path_positive).exists():
        cache_path_hit = cache_path_positive
    elif Path(cache_path_null).exists():
        cache_path_hit = cache_path_null
    else:
        cache_path_hit = None

    return {
        "thread_id": thread_id,
        "cached": cache_path_hit is not None,
        "cache_path_positive": cache_path_positive,
        "cache_path_null": cache_path_null,
        "cache_path_hit": cache_path_hit,
    }


def plan_analysers(cache_dir, repo, base_sha, head_sha, diff_path, set_hash_value):
    """One entry per cached analyser script; see the module docstring for each key."""
    plans = {}
    for script in REF_BASED:
        key = analyser_key(script, base_sha, head_sha)
        cmd = f"python3 {script}.py --repo {repo} --base {base_sha} --head {head_sha}"
        plans[script] = plan_analyser(cache_dir, script, key, cmd)

    for script in ("complexity", "links"):
        key = analyser_key(script, set_hash_value)
        cmd = f"python3 {script}.py --repo {repo} --diff {diff_path} --head {head_sha}"
        if script == "complexity":
            cmd += f" --base {base_sha}"
        plans[script] = plan_analyser(cache_dir, script, key, cmd)
    return plans


def fill_seeds(seed_path, hunk_plan, prior_roles):
    """Pre-fill one fragment-N.seed.json in place: a hunk's "note" from hunk_plan when it
    carried forward, and a file's "role" from prior_roles when every one of its hunks did
    (the all-or-nothing case above). Returns True when the whole seed carried forward, the
    signal fanout.py's batch should not get a subagent spawned for at all.
    """
    seed = json.loads(Path(seed_path).read_text())
    seed_all_carry = True
    for file_entry in seed["files"]:
        path = file_entry["path"]
        hunks = file_entry.get("hunks") or []
        file_all_carry = True
        for h in hunks:
            match = HUNK_PREFIX.match(h["header"].strip())
            prefix = match.group(1) if match else h["header"]
            decision = hunk_plan.get(hunk_id(path, prefix), {"carry": False, "note": ""})
            if decision["carry"]:
                h["note"] = decision["note"]
            else:
                file_all_carry = False
        if file_all_carry and path in prior_roles:
            file_entry["role"] = prior_roles[path]
        else:
            seed_all_carry = False
    Path(seed_path).write_text(json.dumps(seed, indent=2) + "\n")
    return seed_all_carry


def dirty(head_file, prior_head_sha):
    """Read `head_file` (no git call -- expected already resolved via `git rev-parse
    --git-path HEAD`), follow a symbolic ref one step to the real sha via `<head_file's
    dir>/<ref path>`, or through a linked worktree's `commondir` file when present, and
    report whether that disagrees with the sha the page was built against. A packed ref (no
    loose file) reports not dirty rather than guessing."""
    try:
        current = Path(head_file).read_text().strip()
    except OSError:
        return False
    if current.startswith("ref: "):
        head_dir = Path(head_file).parent
        commondir = head_dir / "commondir"
        base_dir = (head_dir / commondir.read_text().strip()) if commondir.exists() else head_dir
        try:
            current = (base_dir / current[5:].strip()).read_text().strip()
        except OSError:
            return False
    return bool(prior_head_sha) and current != prior_head_sha


def build_plan(args):
    diff_text = Path(args.diff).read_text(errors="replace")
    records = hunk_records(diff_text)
    current_set_hash = set_hash(records)

    prior = {}
    if args.prior_state and Path(args.prior_state).exists():
        prior = json.loads(Path(args.prior_state).read_text())
    prior_hunks = prior.get("hunks", [])
    prior_roles = {f["path"]: f.get("role", "") for f in prior.get("files", [])}
    prior_meta = prior.get("meta", {})

    hunk_plan = plan_hunks(records, prior_hunks)
    analysers = plan_analysers(
        args.cache_dir, args.repo, args.base, args.head, args.diff, current_set_hash
    )

    fanout_plan = None
    if args.manifest:
        manifest = json.loads(Path(args.manifest).read_text())
        fanout_plan = [
            {
                "batch": entry["batch"],
                "fragment": entry["fragment"],
                "skip_subagent": fill_seeds(entry["seed"], hunk_plan, prior_roles),
            }
            for entry in manifest
        ]

    resolution_plan = None
    if args.threads:
        threads = json.loads(Path(args.threads).read_text())
        resolution_plan = [
            plan_resolution(args.cache_dir, thread, args.head)
            for thread in threads["threads"]
        ]

    plan = {
        "set_hash": {
            "prior": prior_meta.get("set_hash"),
            "current": current_set_hash,
            "changed": prior_meta.get("set_hash") != current_set_hash,
        },
        "hunks": {
            "carry_forward": [hid for hid, d in hunk_plan.items() if d["carry"]],
            "re_annotate": [hid for hid, d in hunk_plan.items() if not d["carry"]],
        },
        "analysers": analysers,
        "fanout": fanout_plan,
        "resolution": resolution_plan,
        "dirty": dirty(args.head_file, prior_meta.get("head_sha")) if args.head_file else None,
    }
    return plan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--diff", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--prior-state")
    parser.add_argument("--manifest")
    parser.add_argument("--head-file")
    parser.add_argument("--threads")
    args = parser.parse_args()

    print(json.dumps(build_plan(args), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
