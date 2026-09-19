# Fan-out: the batch brief, note rules, and the gate

Background for steps 2a and 2b. Read it before running the fan-out on a large diff, or when a
batch or the gate keeps failing. SKILL.md carries the commands; this file carries what each
subagent is told and why the gate is shaped the way it is.

## Why split at all

Per-hunk notes are local work: what one hunk does is visible in that hunk. So split the diff and
let a fast cheap model do the noticing in parallel, then have one better model write the prose
that needs the whole picture.

## Splitting

`fanout.py split` writes `batch-N.diff`, contiguous slices, a file never straddles two batches,
plus a manifest of `{batch, fragment, files, lines}`. Defaults: about 400 lines per batch, at
most 8 batches, so a huge diff widens each batch instead of spawning 60 agents.

## The batch subagent brief

One subagent per batch, each on a small fast model (haiku class): the job is per-hunk noticing
inside one slice, and the gate checks every answer, so delegate down when a validator can catch
the mistake and keep it up when it cannot. All spawned in a single message so they run
concurrently. Each reads only its own `batch-N.diff` and its manifest-listed
`fragment-N.seed.json`, a skeleton with every `files[]` and `hunks[]` entry already pre-filled
and only `role` and `note` left blank. It copies the seed to `fragment-N.json` and fills those
in. It never types an `@@` header and never adds or removes a file or hunk entry, since the seed
already has the full shape.

Tell each one explicitly: do not read the repo, do not read other batches, do not judge the
code. Retyping the header used to be the only proof an agent had actually read the hunk, and
that is where an invented hunk boundary once slipped through on a large Go repo's diff; the
identifier-overlap gate in step 2b is what proves it now.

Tell it to build the fragment as a Python data structure and write it with `json.dump`, never by
typing the JSON text by hand: a note quoting code routinely contains a double quote, and
hand-typed JSON gets that escaping wrong often enough to cost a retry on half the batches. Tell
it to verify its own fragment parses with `json.load` before it finishes.

Tell it to reply with the fragment's path and a count of hunks filled, never the fragment's
content. A batch agent that pastes its role and note text back into its own report is exactly
how that text ends up in the main thread's context a second time; the fragment file is the
deliverable, the reply is a receipt.

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
is the failure this schema exists to prevent, and the gate's empty-note floor below is what
catches it in practice.

A note still has to name at least one identifier from its own changed lines, because that is
what it is explaining and because the gate checks it. Keep a note to about 20 words; when a hunk
adds many symbols, explain the group and name the two that matter, never a comma series of
everything.

## The prose subagent

One subagent, on a stronger model (sonnet class) since it needs the whole change in view and
nothing downstream can check its judgment, writes `<scratchpad>/prose.json` with `target`,
`what_changed`, `how_it_works` and `flow_mermaid`, from the fragments and the numstat. Under
about 2000 lines, hand it `<scratchpad>/raw.diff` too and tell it to read it, since that removes
the guessing; above that, fragments and the numstat only, never `raw.diff`, the size this split
exists to protect. Either way it may open specific files in the repo when a fragment note is not
enough to explain the machinery.

Tell it, on either brief: every identifier in the prose is copied from the source, never
reconstructed from what a name in that language usually looks like, so an exported
`UIDFromOzoneCookie` is never softened into "a helper" because unexported names are usually
lowercase. A signature change is claimed only when it is visibly in the diff, never because a
function of that name plausibly gained a parameter elsewhere.

Tell it to reply with `prose.json`'s path and a count of fields filled, never their content. An
agent that pastes `what_changed` back into its own report is exactly how that prose ends up in
the main thread's context a second time; the file is the deliverable, the reply is a receipt.

## Merging

The glob in the merge command is `fragment-[0-9].json`, not `fragment-*.json`: the latter also
matches the `fragment-N.seed.json` skeletons sitting beside them, and every file then looks like
it was claimed by two fragments.

The merge orders entries the way `raw.diff` orders files and refuses two fragments claiming the
same file. It also canonicalizes a fragment's old-side rename path to the new-side path before
that check, since a cheap model often only sees one side of a rename. Why that canonicalization
step exists: `references/rationale.md`.

## Gating the merged analysis

After the merge, one subagent, on a stronger model (sonnet class) since it needs the whole change
in view and nothing downstream can check its judgment, gets the `(path, role)` pairs from the
merged analysis, plus `symdelta.counts`/`symdelta.moved`. It returns `groups` and `verdict`;
write what it returns into `analysis.json` before the gate, and review it rather than author it
yourself. This is a deliberate tradeoff: `groups` is the reading order a
human follows and is the least safe field here to hand off, but a `(path, role)` list plus the
graph summary is enough to group from, and it moves 40 to 50 seconds off what the main thread
would otherwise spend writing groups, notes and gate patches by hand.

`validate_analysis.py` prints one plain line per problem and exits non-zero: a file present in
the diff but missing from `files[]`, a blank `role`, an invented file or hunk, and a filled-in
`note` that shares no identifier with that hunk's own added or removed lines. That identifier
check tokenises with `[A-Za-z_][A-Za-z0-9_]*`, splits camelCase and snake_case, compares
case-insensitively, ignores context lines, and skips a hunk whose changed lines carry no
identifiers at all. It is deliberately language independent: `symdelta.py` already owns the
per-language token split, and duplicating it here is the thing to avoid.

### The empty-note floor, and why the old per-hunk churn check is gone

A hunk's `note` may be blank regardless of what the hunk does. An earlier version tried to tell
churn (rename, export capitalisation, import repoint, qualifier drop) apart from everything else
per hunk, and on a measured 45-file analysis it produced 49 false rejections: a type rename, a
package substitution and a bare added import line all introduce a token the subset test read as
new, when every one of them was genuine churn. That check is gone.

What's left is a global floor: if more than `EMPTY_NOTE_FLOOR` (80%) of the diff's hunks have an
empty note, the gate fails and names the actual percentage and count, because that's the failure
worth catching, a fan-out that blanked almost everything, and the fix is to annotate the
substantive hunks, not to lower the floor. The measured good run sits at 84 of 148 hunks empty,
57%, comfortably under it. Do not proceed to step 3 until the gate exits 0. Never render an
analysis that has not passed.

### Gate-failure routing

Count the problem lines before deciding how to fix them. Either way a subagent does the fixing,
never the main thread: the gate's own lines already name the path and the field, so nothing else
needs to travel.

- **Under about ten**: send them to one subagent to patch `analysis.json` directly with a
  `json.load`, assign, `json.dump` script; the gate's lines are enough context on their own, so
  this beats a round trip through the batch that produced them to fix one sentence. Six gaps on a
  45-file diff took one patch script and under a minute.
- **Above that, or a whole file entry missing rather than a field**: send the exact problem
  lines back to whoever produced that part. The fan-out manifest maps each file to its batch and
  fragment, so a complaint about one file goes to that batch's subagent alone, not to all of
  them. Re-run the gate after each fix.
- **Same batch fails twice**: respawn that batch on a stronger model (sonnet class) rather than
  pushing a third time. The fan-out is a speed optimisation, and a batch the cheap model cannot
  cover is exactly where it stops paying off.

Two things the gate does not check, so check them yourself: a hunk whose entry the fragment
appended with an empty `hunks` list for a deleted file (append the entry, do not assume it is
there), and the `verdict` key from the schema.
