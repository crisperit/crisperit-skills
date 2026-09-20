# Turning notes into PR comments, and syncing back the ones already there

## Posting comments and submitting the review, when the user asks

The live flow -- `notes.py import` then `deliver` then `submit`, one GraphQL pending review --
is SKILL.md step 7. `payloads` and `promote`, the one-REST-call-per-comment pair this replaced,
are superseded and kept working for one release.

Still true either way: show the exact comment text and where each one lands, then ask once.
Posting is outward facing and other people get notified, so it is never automatic and not
covered by any earlier confirmation in the session. A review comment on a colleague's PR is
the normal case, so nothing here guards on authorship, but confirm it is deliberate all the
same. Rewrite nothing: the note is the user's words, tighten only if they ask.

The page has no server to post a click to: the user opens the Comments button, clicks Copy for
agent there, and pastes the JSON it copies into the conversation (or, when a PR exists, may
instead run the `gh` command Copy gh command printed -- one heredoc per postable comment plus
one `resolveReviewThread` mutation per thread marked Resolve conversation, in which case this
step is already done). Explain mode never has a PR, so the page already hides the Copy gh command
button and shows a note instead; Copy for agent is the only exit. `import` still works, it only
merges JSON into `state.json`, but `deliver` and `submit` post through a PR review that does not
exist here, so a note stays imported and local rather than going anywhere; there is nothing to
post it to. Import the pasted JSON, then deliver the ids it names:

```bash
python3 <skill>/scripts/notes.py import --state <scratchpad>/state.json   # JSON on stdin
python3 <skill>/scripts/notes.py deliver --state <scratchpad>/state.json --id <id>
```

`import` prints how many notes were added versus updated and which ids are ready to post, so
which `deliver` calls come next is never a guess.

`deliver` resolves the note's anchor against the current diff before it posts: a line still
inside a hunk goes where it always did, a line just outside one moves to the nearest hunk line
and says so in the body, and one with nothing near it becomes a file-level comment instead of the
silent drop GitHub gives a raw out-of-hunk post.

Once every drafted note for this ask has been delivered, submit the review as one unit instead
of leaving it as loose comments:

```bash
python3 <skill>/scripts/notes.py submit --state <scratchpad>/state.json \
  --event COMMENT --body-file <f>
```

`--event` is `COMMENT`, `APPROVE` or `REQUEST_CHANGES`; ask the user which if it is not obvious.
`submit` publishes one review and sends the author one notification, not one per comment.

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

Then a second, additive pass so a resolved thread's state is known before the page renders --
the REST comments above carry no review-thread node id, so resolving one is only possible once
this has run:

```bash
gh api graphql -f query='
  query ReviewThreads($owner: String!, $repo: String!, $number: Int!) {
    repository(owner: $owner, name: $repo) {
      pullRequest(number: $number) {
        reviewThreads(first: 100) {
          nodes { id isResolved resolvedBy { login } comments(first: 100) { nodes { databaseId } } }
        }
      }
    }
  }' -F owner=<owner> -F repo=<repo> -F number=<n> | \
  python3 <skill>/scripts/notes.py sync-threads --state <scratchpad>/state.json
```

When the user asks to refresh the comments, re-run both passes above, then reload the page to
see them: nothing pushes an update to a page that is already open. A thread the reader marked
Resolve conversation shows as resolved for real once this confirms it; until then the page
shows it as pending.
