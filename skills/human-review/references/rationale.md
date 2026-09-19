# War stories behind the rules

Measured incidents that justify a rule elsewhere in this skill but are not needed to follow it.
Read this when you want to know why a step insists on something that looks like extra work, not
when running the skill.

## Why the base ref is always fetched and remote-tracking, never local

Three-dot against a stale local base is the same bug as two-dot, and it looks like a correct
run. On a real run local `main` was five merged PRs behind: `main...HEAD` gave 105 files and
6747 lines, `origin/main...HEAD` gave the true 45 files and 3462 lines. Catching it late cost
3m40s of a 9m run, because the whole fan-out had to be thrown away and redone. The fetch costs a
second. This is why step 1 fetches both refs and diffs `origin/<base>...origin/<head>` rather
than the local branches.

## Why the per-hunk churn check was replaced with a global floor

An earlier gate tried to tell churn (rename, export capitalisation, import repoint, qualifier
drop) apart from everything else per hunk. On a measured 45-file analysis it produced 49 false
rejections: a type rename, a package substitution and a bare added import line all introduce a
token the subset test read as new, when every one of them was genuine churn. It was replaced
with the empty-note floor in `references/fanout.md`, which catches the failure that actually
matters, a fan-out that blanked almost everything, without penalising correct blank notes.

## Why there is no per-line moved-block marker

A `moves.py` step used to mark moved blocks line by line in the walkthrough. On a 45-file
refactor it marked 550 lines, over half the diff body, so its colour became the page's dominant
hue instead of a highlight. A file's move is now said once in its header row as `old → new`,
which is the same fact without the per-line noise.

## Why the markdown budget retry uses the printed number, not a guess

`walkthrough.py --format md`'s default `--max-chars` is 32000, sized against a typical case, and
it overflows when the graph sections come out fatter than that. `render.py` names the exact
value to retry with on an over-budget warning. Converging by hand instead cost three render
cycles on a 45-file diff, back when the default was still 12000: 12000, then 9800, then 9600
characters before it fit. Take the printed number.

## Why `symdelta.py` resolves calls instead of matching names

An earlier version matched symbol edges by name, and a name is often not unique across a repo.
Measured on a 1256-file Go repo, 591 of 674 name-matched edges had a target name occurring at
more than one path, drawing edges between symbols that never call each other. See
`references/graphs.md` for how `symdelta.py` avoids this with a real type checker instead.

## Why the merge canonicalizes rename paths before checking for duplicate claims

A cheap batch model often only sees one side of a rename, old or new, and reports it under
whichever path it read. 8 of the 12 gate failures on a measured 45-file diff were exactly this:
two fragments each claiming what was really the same renamed file, under its two different
names. The merge now canonicalizes a fragment's old-side path to the new-side path before
checking for duplicates, closing off that whole failure class rather than patching it case by
case.
