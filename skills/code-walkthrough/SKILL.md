---
name: code-walkthrough
description: Use when the user wants a change or an area of code shown to them rather than searched: "visual diff", "visualize diff", "visualise diff", "visualize-diff", "visualize this PR", "visualise this branch", "visual recap", "review my changes visually", "show me what changed", "turn this PR into a review page", "graph the symbol changes in this PR", "review this with me", "walk me through this change", "walk me through this PR", "explain the auth flow", "explain how billing works", "how does X work", "walk me through the auth flow", "help me understand this codebase", "help me understand src/parser/", or /code-walkthrough, or "set up code-walkthrough"; both the -ize and -ise spellings mean this skill. Turn a git diff, commit range, branch, GitHub PR, or an unchanged area of code into a self-contained local HTML walkthrough page: mermaid flow diagram, annotated diff walkthrough, per-line comments to hand back to the agent or post to the PR. Reviews or explains code that already exists, unlike a planning skill (plans work that does not exist yet) and unlike a code-review skill (hunts defects rather than recapping or visualizing).
---

# Code Walkthrough

The diff target is what the user named when invoking this skill (in Claude Code: `$ARGUMENTS`).
Style rule for all prose: apply the no-ai-slop skill if available, otherwise plain sentences, no
dashes, no filler, no hedging.

## 1. Resolve the target

The target arrives as prose as often as a ref, so read the words:

| What the user said | Target |
|---|---|
| nothing | working tree plus staged changes, snapshotted as a second ref so it diffs as `git diff HEAD <snap>`, see below |
| "this PR", "my PR", "the PR", "this pull request" | the current branch's PR, see below |
| `123`, `#123`, a PR URL | that PR |
| "this branch", "my branch", "what this branch adds" | `<base>...HEAD` three-dot |
| `HEAD~3..HEAD`, `main...HEAD` | as given |
| "explain X", "how does X work", "walk me through the auth flow", "help me understand this codebase" | X as it stands, diffed against the empty baseline, see below |
| a bare path or directory, with no change described | ambiguous, see below |

"Explain X" and "walk me through the auth flow" name an area, not a ref: nothing downstream turns
prose into files on its own, so resolve it yourself first, see "Turning prose into a file list"
below. A bare path is genuinely ambiguous: it can mean "diff filtered to this path" (there is a
real change and the user is narrowing it) or "explain this path as it stands" (there is no change,
and the user wants it read cold). Tell the two apart from context: a branch ahead of its base,
staged changes, or the words "diff" or "changes" mean the first; a clean checkout of the base
branch, or "explain", "how does", "understand", "walk me through" attached to the path, mean the
second. When neither signal is there, ask the user in one line rather than guessing; guessing wrong
here means redoing the whole walkthrough.

### Turning prose into a file list

Nothing downstream reads prose, so "the auth flow" has to become paths before `references/static.md`'s step 2 can start.
Keep it cheap: grep and glob the repo for the terms the ask names, skim the hits enough to tell
signal from noise, and propose the file list back to the user in one line, for example "That looks
like `src/auth/*.go` and `middleware/session.go`, 6 files. Use those?", before spending any
subagent budget on it. Do not write a new script for this: it is a grep-and-confirm pass, not a
search feature.

### Explain mode: diff against an empty baseline

"Explain X as it stands" names no second ref, so build one: an orphan commit wrapping the empty
tree. Every line in the named path then comes back as an addition and the machinery below runs
unchanged, one code path rather than two. That choice also sets the page's mode, `<explain>` in
`references/static.md`'s step 3. Explain mode has no size ceiling of its own either, so it
negotiates scope with the user before spending.

Read `references/explain-mode.md` now, before `references/static.md`'s step 2 writes anything: the baseline commands, the
three size bands that decide whether to proceed, mention the cost, or stop and offer cuts, and
what `--explain` changes downstream.

### Working tree target: a snapshot commit as the second ref

"nothing" names no second ref either: the working tree and the index are not commits. Give it
one with a snapshot, so `<head>` is the snapshot and `<base>` is `HEAD`, and every step
downstream that needs two refs runs instead of skipping.

```bash
idx=$(mktemp); cp "$(git rev-parse --git-path index)" "$idx"
GIT_INDEX_FILE=$idx git add -A --ignore-errors 2>/dev/null
snap=$(git commit-tree "$(GIT_INDEX_FILE=$idx git write-tree)" -p HEAD -m "code-walkthrough snapshot")
rm "$idx"
```

`--ignore-errors` matters in a sandbox, where a mounted dotfile can show up as an untracked
unsupported file type and would otherwise abort the add; the trailing `2>/dev/null` quiets that
same case but also hides a genuine staging failure, so if a snapshot ever looks incomplete, rerun
the `add` without the redirect to see why. The real index, working tree and every ref are
untouched: `snap` is a commit object nothing points at, so it never shows up in `git log` or `git
branch`. It sits as a loose object until a later `git gc` prunes it, not cleaned up the moment
this session ends. `references/static.md`'s step 2 then builds `raw.diff` and numstat from
`git diff HEAD <snap>` rather than plain `git diff HEAD`, which also picks up untracked files that plain `git diff HEAD`
misses. The page's title and output slug still say "working", not the snapshot's sha; only
`<base>`/`<head>` downstream change.

Links still skip themselves for this target too: `snap` is never pushed, so `head_pushed` comes
back false the same way it does for any other unpushed head.

### "This PR", and three-dot over two-dot

"This PR" resolves through the current branch:

```bash
gh pr view --json number,baseRefName,headRefName --jq '[.number,.baseRefName,.headRefName]|@tsv'
```

Prefer diffing `<baseRefName>...HEAD` locally over `gh pr diff <n>`: it gives the local repo
path, base and head that `complexity.py`, `symdelta.py` and `links.py` all need, and it is the
same three-dot comparison GitHub shows. Fall back to `gh pr diff <n>` when the branch is not
checked out locally, and say in one line that the symbol-delta graph is skipped in that case. Bail
with one clear line when the branch has no PR yet.
Always prefer three-dot over two-dot: two-dot also shows commits that landed on the base branch
meanwhile, attributing other people's work to this change.

### Fetch both refs first, and diff the remote base

Fetch, then compare against the remote-tracking base, never the local branch. Run the fetch
outside any command sandbox, because it needs the network and symdelta later writes caches
outside the repo too (in Claude Code, `dangerouslyDisableSandbox: true`):

```bash
git fetch origin <baseRefName> <headRefName>
git diff origin/<baseRefName>...origin/<headRefName>
```

Then sanity-check the commit list before spending anything on it:

```bash
git log --oneline origin/<base>..origin/<head>
```

Every line should plausibly belong to this change. A commit carrying someone else's PR number,
or a merge-base that equals your local base tip while the log shows unrelated work, means the
base is stale and the diff is wrong. Why this matters and what it once cost:
`references/rationale.md`.

Bail with one clear line if the target resolves to an empty diff; an empty review page is worse
than a message. In explain mode this is what an empty diff means: a path with no matching files,
not "nothing changed", so the same bail applies, worded for that case.

## 1b. Live mode, when the code-walkthrough MCP tools exist

When the `walkthrough_start`/`walkthrough_get`/`walkthrough_list` MCP tools exist, prefer this
over the static pipeline, including for a GitHub PR target. Call `walkthrough_start` with `repo`
the toplevel, `base`/`head` as resolved above (the snapshot commit for the working tree,
`$EMPTY_BASE` in explain mode), `target`, `slug`, `explain`, `paths`, `title`, and `pr` whenever
the target is a PR. A re-call on a PR walkthrough that already finished `done` refreshes its
comments and resolved threads instead of redoing any batch or prose work.

Any error from the call: read `references/static.md` and follow it, and mention the remedy in
one line at the end of the report.

Success: open `url` with `xdg-open <url>` on Linux, `open <url>` on macOS, `start "" <url>` on
Windows, or print it instead and do not open it when none of those work or the session is remote
or headless, since a text-mode browser fallback would launch inside the agent's own terminal and
block it. Then poll `walkthrough_get(id, key, wait_s=600)` while status is `building`. A step
carrying a `remedy` is a config or backend problem the daemon cannot fix on its own: tell the
user the problem and the exact command in one line, running `references/setup.md` when
the remedy is a config or backend fix, ask, and call `walkthrough_start` again once they confirm
it is fixed.

`done`: report the url, the verdict (`summary.verdict`), and the group titles. Tell the user to
select any text on the page and click Ask to question it, where the answer appears in place and
in the page's own Q&A list; drafts persist server-side, nothing to relay about them here; and
that posting or submitting a review happens from the page's own Comments panel. `failed`: report
the gate line count and the remedy.

Setup, config keys, cost, data egress and the full remedy table: `references/live.md`.

## No live tools

When the `walkthrough_*` tools are not available, set live mode up first: follow
`references/setup.md`. New tools only appear after the agent restarts, so this run then reads
`references/static.md` in full and follows its steps 2 to 6. End the report with one line: restart
the agent to get the live page next time. When the user only asked to set up code-walkthrough,
stop after `references/setup.md`.
