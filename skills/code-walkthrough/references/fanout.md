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

Judgment rules (what a batch subagent is told about `role` and `note`): `prompts/batch.md`. One
subagent per batch, each on the strong tier: the job is per-hunk noticing inside one slice, and
the gate checks every answer, so delegate down when a validator can catch the mistake and keep
it up when it cannot. All spawned in a single message so they run concurrently. Each reads only
its own `batch-N.diff` and its manifest-listed `fragment-N.seed.json`, a skeleton with every
`files[]` and `hunks[]` entry already pre-filled and only `role` and `note` left blank. It copies
the seed to `fragment-N.json` and fills those in. It never types an `@@` header and never adds or
removes a file or hunk entry, since the seed already has the full shape.

Tell each one explicitly: do not read the repo, do not read other batches, do not judge the
code. Retyping the header used to be the only proof an agent had actually read the hunk, and
that is where an invented hunk boundary once slipped through on a large Go repo's diff; the
identifier-overlap gate in step 2b is what proves it now.

Tell it to build the fragment as a Python data structure and write it with `json.dump`, never by
typing the JSON text by hand: a note quoting code routinely contains a double quote, and
hand-typed JSON gets that escaping wrong often enough to cost a retry on half the batches. Tell
it to verify its own fragment parses with `json.load` before it finishes.

Before replying, run
`python3 <skill>/scripts/validate_analysis.py --fragment --diff <batch-N.diff> --analysis
<fragment-N.json>`. Fix what it names and re-run, at most twice: it skips the whole-diff checks
(the top-level keys, the empty-note floor) that only make sense over the merged analysis, so a
batch that gates clean here can still contribute to a central-gate failure later, just not on
anything this check already caught.

Tell it to reply with the fragment's path, a count of hunks filled, and `gate: ok` or the number
of problems left, never the fragment's content. A batch agent that pastes its role and note text
back into its own report is exactly how that text ends up in the main thread's context a second
time; the fragment file is the deliverable, the reply is a receipt.

Role and note rules, with the bad/good pairs: `prompts/batch.md`.

## The prose and grouping subagent

Judgment rules (what `overview`, `groups`, the flow diagrams and `hop`/`side` are for, the
worked example): `prompts/prose.md`. One subagent, on the strong tier since it needs the whole
change in view and nothing downstream can check its judgment, writes `<scratchpad>/prose.json`
with `target`, `overview`, `verdict`, `groups` and a top-level `flow_mermaid`, from the
fragments, the numstat and the graph summaries (`symdelta.counts`/`symdelta.moved`). Under about
2000 lines, hand it `<scratchpad>/raw.diff` too and tell it to read it, since that removes the
guessing; above that, fragments and the numstat only, never `raw.diff`, the size this split
exists to protect. Either way it may open specific files in the repo when a fragment note is not
enough to explain the machinery.

The main thread no longer writes `groups` into `analysis.json` by hand: this subagent writes them
directly into `prose.json`, and `fanout.py merge` carries them through.

Ordering: this subagent waits for every batch fragment and for `symdelta.json`, the same
dependency the old grouping pass already had, so the critical path does not get longer.

Tell it to reply with the path, field counts and group titles only, never their content. An
agent that pastes `overview` or a group's `why` back into its own report is exactly how that
prose ends up in the main thread's context a second time; the file is the deliverable, the reply
is a receipt. Group titles themselves are the one exception allowed in the main context
(SKILL.md's intro), so naming them in the reply is fine.

## Merging

The glob in the merge command is `fragment-[0-9].json`, not `fragment-*.json`: the latter also
matches the `fragment-N.seed.json` skeletons sitting beside them, and every file then looks like
it was claimed by two fragments.

The merge orders entries the way `raw.diff` orders files and refuses two fragments claiming the
same file. It also canonicalizes a fragment's old-side rename path to the new-side path before
that check, since a cheap model often only sees one side of a rename. Why that canonicalization
step exists: `references/rationale.md`.

## The gate

`validate_analysis.py` prints one plain line per problem and exits non-zero: a file present in
the diff but missing from `files[]`, a blank `role`, an invented file or hunk, and a filled-in
`note` that shares no identifier with that hunk's own added or removed lines. That identifier
check tokenises with `[A-Za-z_][A-Za-z0-9_]*`, splits camelCase and snake_case, compares
case-insensitively, ignores context lines, and skips a hunk whose changed lines carry no
identifiers at all. It is deliberately language independent: `symdelta.py` already owns the
per-language token split, and duplicating it here is the thing to avoid. A group's `hop` runs
through the same tokeniser, checked against the whole diff rather than one hunk, since a
hand-off can point at either side of a group boundary; `hop` also fails over six words, `side`
fails when it isn't a plain boolean, and the last group that isn't a `side` group fails if it
carries a `hop` at all.

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
- **Same batch fails twice**: respawn that batch on the strong tier rather than pushing a third
  time. The fan-out is a speed optimisation, and a batch the cheap model cannot cover is exactly
  where it stops paying off.

Two things the gate does not check, so check them yourself: a hunk whose entry the fragment
appended with an empty `hunks` list for a deleted file (append the entry, do not assume it is
there), and the `verdict` key from the schema.
