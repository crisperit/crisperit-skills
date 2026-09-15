# Writing the PR description, and turning notes into PR comments

Background for steps 6 and 8. SKILL.md carries the commands; this carries the full sequencing,
the confirmation rules, and the diff-notes format.

## Writing the PR description, only with `--pr [<number>]`

Resolve the PR number from the argument, else from the current branch via
`gh pr view --json number --jq .number`. Bail with one clear line when no PR exists yet for this
branch.

Check authorship before writing anything: only recap PRs of your own.

```bash
ME=$(gh api /user --jq .login)
AUTHOR=$(gh pr view <n> --json author --jq .author.login)
```

Bail with one clear line naming both logins when they differ. Do not offer to override: a recap
on someone else's PR is a deliberate act the user can ask for explicitly. Use whatever `gh` auth
is already configured; do not call `gh auth switch` and do not set `GH_TOKEN`.

In interactive runs, confirm once before the first `gh pr edit` on a given PR this session; a
refresh of a region this skill already wrote does not need re-confirming.

1. Read the current body: `gh pr view <n> --json body --jq .body > <scratchpad>/pr-body.txt`.
   The `jq -r` here appends a trailing newline that is not part of the stored body; the splice
   below strips it so a refresh stays byte-stable.
2. Wrap the markdown recap in `<!-- visual-diff:start -->` / `<!-- visual-diff:end -->` markers
   and splice it into the body, writing the merged result to `<scratchpad>/pr-body-new.txt`. See
   `references/pr-markdown.md` for the exact logic and its test script.
3. Write it back: `gh pr edit <n> --body-file <scratchpad>/pr-body-new.txt`. Always
   `--body-file`, never `--body` with an inline string: the recap contains backticks, newlines
   and quotes that shell quoting mangles.

## Turning pasted notes into PR comments, when the user asks

The page's "Copy notes for Claude" button produces a `## Diff notes` block, one numbered entry
per line comment, in page order:

```
## Diff notes (3)

1. src/auth/session.ts:42 RIGHT
   line: + if (!token) return loadFromRefresh();
   note: why not throw here instead of falling back silently

2. src/auth/session.ts:18 LEFT
   line: - if (!token) return null;
   note: this was the only caller that checked, worth keeping

3. test/auth.spec.ts:7 RIGHT
   line: + expect(loadSession()).toBeNull();
   note: does this still hold after the change
```

No comments gives `## Diff notes (0)` and one line reading `(no comments, diff looks fine)`.
When this block comes back into the conversation and the target was a PR, offer to post it, and
post only what the user confirms.

- One comment per entry, on the file and line it names, is the useful shape: `gh api
  repos/<owner>/<repo>/pulls/<n>/comments` with `path` from before the colon, `line` from after
  it, `side` from the `RIGHT` or `LEFT` token, `commit_id` set to the head sha from
  `links.json`, and `body` from the `note:` row. `side: "LEFT"` on a removed line is not
  decoration: posting a `LEFT` line as `RIGHT`, or the reverse, lands the comment on unrelated
  code, so the token in the block is authoritative and never second-guessed.
- One general `gh pr comment <n> --body-file` is a deliberate choice, not a fallback, and the
  right one when the notes read as one train of thought rather than separate points.
- Show the exact comment text and where each one lands, then ask once. Posting is outward facing
  and other people get notified, so it is never automatic, and it is not covered by any earlier
  confirmation in the session.
- The authorship guard above does not apply here. A review comment on a colleague's PR is the
  normal case, unlike editing their description. Confirm it is deliberate all the same.
- Rewrite nothing. The note is the user's words; tighten only if they ask.
