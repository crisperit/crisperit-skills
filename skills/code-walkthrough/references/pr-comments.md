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
