Judgment rules for the batch subagent: what a `role` and a hunk `note` are for, and what makes
one good or bad. Delivery mechanics (how to build and gate the fragment, how to reply) live with
each caller instead, since those differ: `references/fanout.md` for the agent fan-out and the
daemon's batch worker.

## The batch subagent brief

One subagent per batch, each on the strong tier: the job is per-hunk noticing
inside one slice, and the gate checks every answer, so delegate down when a validator can catch
the mistake and keep it up when it cannot. All spawned in a single message so they run
concurrently. Each reads only its own `batch-N.diff` and its manifest-listed
`fragment-N.seed.json`, a skeleton with every `files[]` and `hunks[]` entry already pre-filled
and only `role` and `note` left blank. It copies the seed to `fragment-N.json` and fills those
in. It never types an `@@` header and never adds or removes a file or hunk entry, since the seed
already has the full shape.

## Role rules

Every file needs a `role`, including a test file and a pure rename, and that `role` is where a
file's churn gets said once, not per hunk, which is exactly why it cannot be a single word.
`Moved`, `Deleted`, `Modified`, `Updated` alone are not roles: the page already shows the path,
the +/- counts, and for a renamed file an `old → new` arrow in its header row, so a bare
change-verb tells the reader strictly less than they already see. For the same reason a role
must not restate the move itself: `Moved from \`internal/\`` is redundant beside the arrow.

Say what the file is FOR, or for a deleted one where its contents went:

- `Deleted: metrics promoted to \`internal/ratelimit/metrics.go\``
- `Now holds only threshold parsing, config loading exported to \`ratelimit_config\``
- `Swaps the polling loop for a debounced watcher`

A deleted file still gets a note on its one removal hunk, saying what went away and where it
went.

## Note rules

A hunk `note` explains the code, it does not narrate the diff. The reader has the lines right
there; what they cannot get from the lines is the meaning. So a note answers one of: what this
code now does, why it has to, or what it breaks if it is wrong. It never answers "what did the
author type here".

- Bad, narrates: `Adds rate-limit config import and passes it to NewRateLimiter.`
- Good, explains: `NewRateLimiter now takes its thresholds from config instead of the literals
  it was compiled with, so they can change without a deploy.`
- Bad, narrates: `Renames metricsInjector to MetricsInjector.`
- Good: leave it blank. Exporting a field is on the face of the line.

Leave the note blank whenever the lines already say it on their face: adds an import, renames a
variable, changes the package clause, reorders struct fields, drops a qualifier, recapitalises
an export. A note that restates the line above it is worse than no note: on a 45-file diff that
is 120 paragraphs standing between the reader and the code. The file's `role` carries the churn
once instead of every hunk repeating it. A binary or mode-only change gets an empty `hunks` list
and keeps its file entry, so the reader can tell "nothing to show" from "forgot to look". A file
with five substantive hunks gets five notes; picking the interesting one and dropping the rest
is the failure this schema exists to prevent, and the gate's empty-note floor is what catches it
in practice.

A note still has to name at least one identifier from its own changed lines, because that is
what it is explaining and because the gate checks it. Keep a note to about 20 words; when a hunk
adds many symbols, explain the group and name the two that matter, never a comma series of
everything.
