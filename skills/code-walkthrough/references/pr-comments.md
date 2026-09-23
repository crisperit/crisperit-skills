# Turning notes into PR comments, and syncing back the ones already there

## Posting comments, or acting on them directly, when the user asks

The page has no server to post a click to: the user opens the Comments button and either runs
the `gh` command Copy gh command printed -- one heredoc per postable comment plus one
`resolveReviewThread` mutation per thread marked Resolve conversation, posting straight to
GitHub, in which case this step is already done -- or clicks Copy for agent and pastes what it
copies into the conversation instead.

Both buttons render from the same stored notes JSON. Copy for agent copies plain text: a
`Feedback:` line, then one `path:line` (or `path:start-end` for a range comment) and its body per
comment, blank-line separated, in page order. It carries no id and no anchor, so there is nothing
here for `notes.py import` to merge into `state.json` -- read it the way a human's review
feedback would be read, and act on it: implement what each comment asks for, or, when a PR exists
and the user wants these posted as real review comments too, use the comment's own `path:line` to
do that with a fresh `gh api` call or by pointing the user at Copy gh command.

Still true either way: show the exact comment text and where each one lands before doing
anything with it, then ask once. Posting is outward facing and other people get notified, so it
is never automatic and not covered by any earlier confirmation in the session. A review comment
on a colleague's PR is the normal case, so nothing here guards on authorship, but confirm it is
deliberate all the same. Rewrite nothing: the note is the user's words, tighten only if they ask.

Explain mode never has a PR, so the page already hides the Copy gh command button and shows a
note instead; Copy for agent is the only exit there, and reads purely as feedback to act on in
the repo, since there is no PR to post it to.

`notes.py import`, `deliver` and `submit` (hand-fed notes JSON, one GraphQL pending review) still
work for a state.json-driven posting flow; neither button feeds them.

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
