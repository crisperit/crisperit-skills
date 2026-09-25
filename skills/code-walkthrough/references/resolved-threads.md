# Resolving PR review threads

Background and full steps for SKILL.md's 2g, run only when the diffed target is a GitHub PR.
SKILL.md carries the stub; this file carries every step and the worker prompt, and is meant to
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
   git log --format='%x02%H%x09%s%x09%cI%x09%an' --numstat \
     origin/<base>..origin/<head> > <scratchpad>/commit-log.raw

   python3 - <<'PY'
   import json, subprocess
   from pathlib import Path

   raw = Path("<scratchpad>/commit-log.raw").read_text()
   commits, diffs = [], {}
   for block in raw.split("\x02")[1:]:
       header, _, rest = block.partition("\n")
       sha, subject, committed_at, author = header.split("\t")
       files = []
       for line in rest.strip("\n").splitlines():
           if not line.strip():
               continue
           added, removed, path = line.split("\t", 2)
           files.append({
               "path": path,
               "additions": int(added) if added != "-" else 0,
               "deletions": int(removed) if removed != "-" else 0,
           })
       commits.append({"sha": sha, "subject": subject, "committed_at": committed_at,
                        "author": author, "files": files})
       diffs[sha] = subprocess.run(["git", "show", "--format=", sha], cwd="<repo>",
                                    capture_output=True, text=True).stdout

   Path("<scratchpad>/commit-index.json").write_text(
       json.dumps({"commits": commits}, indent=2) + "\n")
   Path("<scratchpad>/diffs.json").write_text(json.dumps(diffs, indent=2) + "\n")
   PY
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

Hand the worker `<seed path>` and this:

```
Read <seed path>. "thread" is the comment and its replies that make up one resolved GitHub
review thread: path, line, the root comment's body, and every reply in order. "commits" is
every commit on this PR from the thread's first comment onward, each with its subject,
author, and files touched, but no diff text of its own. "diffs" maps a commit sha to its
unified diff text for whichever commits are already fetched; it may be empty.

1. Gate. Decide how this thread actually ended, from the thread alone:
   - conversation: a reply explained, argued, or agreed, and that settled it. No commit
     needed. Take the reply that did the settling as closing_message.
   - deferred: punted to a ticket or a later pass. Take the ticket URL if a reply names one,
     otherwise null.
   - commits: a code change is what closed it.
   - none: none of the above is clear from the thread. This is a real answer, not something
     to avoid. Guessing a commit you cannot support is worse than saying you found nothing.
   For conversation, deferred, and none, stop here. Write thread-N.json now, in the shape
   below, with commits: [] and files: []. Nothing below applies.

2. Match, only when the gate said commits. Pick every sha in "commits" that plausibly closed
   this thread. Plural is normal: one push can answer several comments, and "extract this" or
   "refactor this" can span several commits and files. Weigh, but do not filter on: the
   commit's author matching the thread's resolved_by, the commit touching the thread's path
   with files or hunks near its line, and the subject mentioning review, the file, or the
   symbol the comment names. The path is a signal, not a requirement: a comment on one file is
   sometimes answered entirely in others. If nothing you can find genuinely reads as the fix,
   write outcome: none instead of forcing a match.

3. Explain, only once you have diffs for every sha you picked in step 2. If "diffs" already
   covers all of them, continue below. If it does not, you cannot finish this pass: write
   thread-N.needs.json, {"thread_id": "<id>", "need_diffs_for": [sha, ...]}, and stop. The
   driver fetches those and runs you again with the same seed, this time carrying them.
   Once every diff is in hand, name the files and hunks that actually answer the comment, not
   every file the commit touched: a nine-file refactor may answer one comment in three of
   them. Copy those hunks verbatim. Write one sentence on what the change actually did, in
   the code's own terms. Do not restate the comment: the reader has it open directly above
   your answer. Write null when the diff says it on its face, which is common for a one-line
   mechanical change.

Write thread-N.json:
{"thread_id": "<id>", "outcome": "conversation"|"deferred"|"commits"|"none",
 "closing_message": <string or null, only for conversation>,
 "ticket": <string or null, only for deferred>,
 "commits": [sha, ...],
 "files": [{"path": "...", "hunks": ["@@ ...", ...]}],
 "why": <one sentence, or null when the diff says it on its face>,
 "confidence": "high"|"medium"|"low"}

confidence is your certainty in the match, not the gate: high when the ranking signals agree,
low when you picked a sha on one weak signal alone.
```
