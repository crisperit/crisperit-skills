# Resolving PR review threads

Background and full steps for `static.md`'s 2g, run only when the diffed target is a GitHub PR.
`static.md` carries the stub; this file carries every step and the worker prompt, and is meant to
be read in full before starting.

For each thread `sync-threads` just marked resolved, work out what closed it: conversation, a
deferred ticket, or a commit, and for a commit, which hunks and why. Unlike the optional regen
step (`references/regen.md`), this step is **not** droppable. It produces the page's primary
content for a resolved thread, not a cache optimisation, so it runs on a first review too, and
the only thing that skips it is step 1 below coming back with no threads at all.

1. **List the resolved threads.** `resolved-threads` wants the exact `last_comment_id` from
   GraphQL, not the notes-based fallback, so run the same `REVIEW_THREADS_QUERY` call step 2f
   already ran (`references/pr-comments.md`) a second time, this time saved to a file instead
   of piped straight into `sync-threads`:

   ```bash
   gh api graphql -f query='<REVIEW_THREADS_QUERY>' \
     -F owner=<owner> -F repo=<repo> -F number=<n> > <scratchpad>/raw-graphql.json

   python3 <skill>/scripts/notes.py resolved-threads --state <scratchpad>/state.json \
     --payload <scratchpad>/raw-graphql.json --out <scratchpad>/threads.json
   ```

   Stop here when `threads.json`'s `threads` list is empty. Nothing below has anything to do.

2. **Build the commit index**, contract (B), from `git log` over the range step 1 already
   fetched, never the GitHub API: the scripts are no-network by convention and the repo is
   already local. Capture each commit's own diff in the same pass, keyed by sha, so a
   thread whose window turns out small enough gets answered in one worker call instead of
   two:

   Two-dot, not three-dot: `git log`'s `...` is a symmetric difference, so it would pull in
   commits that landed on `<base>` after the branch point too, attributing someone else's
   work to this thread's resolution. `..` keeps it to commits unique to `<head>`.

   ```bash
   python3 <skill>/scripts/fanout_threads.py index --repo <repo> \
     --base origin/<base> --head origin/<head> \
     --out-commits <scratchpad>/commit-index.json --out-diffs <scratchpad>/diffs.json
   ```

3. **Plan the cache**, a second `regen.py` invocation, `--threads` this time instead of
   `--manifest`, against the same `--cache-dir`, `<scratchpad>/cache`:

   ```bash
   mkdir -p <scratchpad>/cache
   python3 <skill>/scripts/regen.py --diff <scratchpad>/raw.diff --repo <repo> \
     --base <base> --head <head> --cache-dir <scratchpad>/cache \
     --threads <scratchpad>/threads.json > <scratchpad>/resolution-plan.json
   ```

   Read `plan["resolution"]`: a `"cached": true` entry already has its answer sitting at
   `cache_path_hit` and needs no worker. A `"cached": false` entry is a miss and goes to the
   split below.

4. **Split, then spawn one worker per miss, in parallel**, exactly the way step 2a spawns one
   subagent per batch:

   ```bash
   python3 <skill>/scripts/fanout_threads.py split --threads <scratchpad>/threads.json \
     --commits <scratchpad>/commit-index.json --plan <scratchpad>/resolution-plan.json \
     --diffs <scratchpad>/diffs.json --out <scratchpad>/resolutions
   ```

   This writes one `thread-N.seed.json` per miss and prints a manifest naming each thread's
   mode: `cached` (already handled in step 3, no seed written), `inline` (the window's diffs
   already sit in the seed, one call finishes it), `two-pass` (the window was too big to
   pre-fetch, or wasn't covered, so the seed's `diffs` is empty) or `invalidated` (step 3
   reported a cache hit, but the commits it names are gone from the window, a force-push in
   practice, so it was re-seeded and must be re-run). Spawn every `inline`, `two-pass` and
   `invalidated` seed's worker in one message, each on the strong tier, with the prompt below.
   A cheaper model is tempting here because the unit is small, but the judgment is not: deciding
   that a commit does NOT answer a comment is the whole value of the step, and a weaker model
   either forces a match or declines one it should have made. A worker that cannot finish because
   it needs a diff its seed does not carry writes `thread-N.needs.json` instead of `thread-N.json`,
   naming the shas it picked; fetch exactly those with `git show`, add them to that seed's `diffs`
   map, and spawn that one worker again with the same prompt. It now has what it needs and writes
   the real `thread-N.json`.

5. **Cache, then merge, then apply.** Before merging, copy every fresh answer into its cache
   slot from step 3's plan: `cache_path_positive` for any outcome other than `none`
   (conversation, deferred, and commits are all permanent answers), `cache_path_null` for
   outcome `none` (worth retrying once a new commit lands, so it is not permanent). A
   `cached: true` thread from step 3 needs no copy, its answer is already there. An
   `invalidated` thread is a fresh answer like any other, copy it too, overwriting the stale
   slot it just replaced.

   Collect fragment paths from the split manifest rather than globbing `thread-*.json`: that
   pattern also matches `thread-N.seed.json`. For each `cached` entry, copy its
   `cache_path_hit` file's contents verbatim into a fragment path of your own naming, its
   shape is already contract D. Every other entry's fragment is its `seed` path with
   `.seed.json` swapped for `.json`, the file that thread's worker wrote.

   ```bash
   python3 <skill>/scripts/fanout_threads.py merge --threads <scratchpad>/threads.json \
     --commits <scratchpad>/commit-index.json \
     --fragments <the fragment paths collected above> --out <scratchpad>/resolutions.json

   python3 -c "import json; d=json.load(open('<scratchpad>/resolutions.json')); \
     print(json.dumps(d['resolutions']))" | \
     python3 <skill>/scripts/notes.py apply-resolutions --state <scratchpad>/state.json
   ```

   `merge`'s output nests every resolution under a `"resolutions"` key; `apply-resolutions`
   reads a flat `{thread_id: resolution}` map from stdin, hence the unwrap.

## The per-thread worker prompt

Judgment rules (the gate, the match, the explain step, and the answer's shape): `prompts/thread.md`.
Hand the worker `<seed path>` plus that file's text. The worker writes its answer to
`thread-N.json`, in the shape `prompts/thread.md` gives (it says "answer with"; here, on the
agent path, that means write the file). A worker that cannot finish because it needs a diff its
seed does not carry writes `thread-N.needs.json` instead, naming the shas it picked (step 3
there), and is re-spawned with the same seed once those diffs are added to it, per step 4 above.
