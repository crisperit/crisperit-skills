# Resolving non-trivial targets: snapshot, PR, fetch

Read this when the target is the working tree, a PR, or any ref pair that needs fetching, before `static.md` step 2.

## Working tree target: a snapshot commit as the second ref

"nothing" names no second ref either: the working tree and the index are not commits. Give it
one with a snapshot, so `<head>` is the snapshot and `<base>` is `HEAD`, and every step
downstream that needs two refs runs instead of skipping.

```bash
idx=$(mktemp); cp "$(git rev-parse --git-path index)" "$idx"
GIT_INDEX_FILE=$idx git add -A --ignore-errors 2>/dev/null
snap=$(git commit-tree "$(GIT_INDEX_FILE=$idx git write-tree)" -p HEAD -m "code-walkthrough snapshot")
rm "$idx"
```

`--ignore-errors` matters in a sandbox, where a mounted dotfile can show as an untracked unsupported file type and abort the add. The trailing `2>/dev/null` quiets that case but also hides a genuine staging failure: if a snapshot looks incomplete, rerun the `add` without it. The real index, working tree and every ref are
untouched: `snap` is a commit object nothing points at, so it never shows up in `git log` or `git
branch`. It sits as a loose object until a later `git gc` prunes it, not cleaned up the moment
this session ends. `references/static.md`'s step 2 then builds `raw.diff` and numstat from
`git diff HEAD <snap>` rather than plain `git diff HEAD`, which also picks up untracked files that plain `git diff HEAD`
misses. The page's title and output slug still say "working", not the snapshot's sha; only
`<base>`/`<head>` downstream change.

Links still skip themselves for this target too: `snap` is never pushed, so `head_pushed` comes
back false the same way it does for any other unpushed head.

## "This PR", and three-dot over two-dot

"This PR" resolves through the current branch:

```bash
gh pr view --json number,baseRefName,headRefName --jq '[.number,.baseRefName,.headRefName]|@tsv'
```

Prefer `<baseRefName>...HEAD` locally over `gh pr diff <n>`: it gives the repo path, base and head that `complexity.py`, `symdelta.py` and `links.py` need, and matches GitHub's three-dot comparison. Fall back to `gh pr diff <n>` when the branch is not checked out locally, and say in one line that the symbol-delta graph is skipped. Bail with one clear line when the branch has no PR yet.
Always use three-dot: two-dot also shows commits that landed on the base meanwhile, attributing other people's work to this change.

## Fetch both refs first, and diff the remote base

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

Every line should plausibly belong to this change. A commit with someone else's PR number, or a merge-base equal to your local base tip while the log shows unrelated work, means the base is stale and the diff is wrong. Why this matters and what it once cost:
`references/rationale.md`.

Bail with one clear line on an empty diff; an empty review page is worse than a message. In explain mode an empty diff means a path with no matching files, not "nothing changed": same bail, worded for that case.
