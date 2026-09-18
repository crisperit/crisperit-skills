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

## Posting line comments, when the user asks

The notes live in `state.json`, and `notes.py payloads` writes one JSON file per note ready to
post -- every non-stale local draft. Post only those, and only what the user confirms.

- One comment per note, on the file and line it names, is the useful shape: `gh api
  repos/<owner>/<repo>/pulls/<n>/comments --input <payload>`, where the payload carries `path`,
  `line` and `side` copied from the note and `commit_id` set to the head sha from `links.json`.
  `side: "LEFT"` on a removed line is not decoration: posting a `LEFT` line as `RIGHT`, or the
  reverse, lands the comment on unrelated code, so the note's own `side` is authoritative and
  never second-guessed.
- One general `gh pr comment <n> --body-file` is a deliberate choice, not a fallback, and the
  right one when the notes read as one train of thought rather than separate points.
- Show the exact comment text and where each one lands, then ask once. Posting is outward facing
  and other people get notified, so it is never automatic, and it is not covered by any earlier
  confirmation in the session.
- The authorship guard above does not apply here. A review comment on a colleague's PR is the
  normal case, unlike editing their description. Confirm it is deliberate all the same.
- Rewrite nothing. The note is the user's words; tighten only if they ask.

## Syncing in comments already on the PR

Before posting anything new, or whenever the page should show threads that already exist, pull
existing review comments into `state.json`:

```bash
gh api repos/<owner>/<repo>/pulls/<n>/comments --paginate | \
  python3 <skill>/scripts/notes.py sync --state <scratchpad>/state.json
```

Matched by `gh_id`, so running this twice is safe: a comment already in state gets its `body`
updated (GitHub's copy always wins for an `origin: "github"` record), everything else is added.
`line: null` is GitHub's signal for an outdated comment, not a missing one: the note is kept with
`line` set to `original_line` and `stale: true`, and renders in a separate stale list rather than
being dropped. `position`/`original_position` are never stored: GitHub's own docs mark `position`
as closing down, and it turns up non-null on outdated comments too, so `line` and `original_line`
are the only fields trusted. A reply's `in_reply_to_id` resolves to its parent's local id when
the parent is already in state; when `--paginate`'s page order puts a reply before its parent,
the raw id is kept and resolved on the next sync instead of dropped. Replies inherit their
parent's `line` and `side` rather than anchoring independently.

Rate limits: this is one `--paginate` call per sync, run when the agent decides to, never on a
poll loop against GitHub.

## Promoting a draft to posted

After a successful post (`gh api ... --input <file>`, above), record the result on that one note
before moving to the next:

```bash
python3 <skill>/scripts/notes.py promote --state <scratchpad>/state.json \
  --id <the note's local id> --gh-id <the new comment's id> --gh-url <its html_url>
```

This sets `state: "posted"`, `gh_id` and `gh_url` on the existing record and changes nothing
else; the note is never deleted and never rewritten in place by any other step. Promote one note
at a time, right after its own post succeeds, so a failure partway through a batch leaves the
already-posted notes marked and the rest still drafts rather than losing track of which is
which. A draft whose `(path, line, side, body)` already matches a `github`-origin comment by
this user, after a sync, is already posted in substance; promote it instead of posting it again.
