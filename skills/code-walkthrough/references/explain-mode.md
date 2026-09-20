# Explain mode: the empty baseline, its scope bands, and `<explain>`

Read this at step 1 whenever the target resolved to the empty-baseline row, before step 2
writes `raw.diff`. SKILL.md carries the decision; the mechanics are here.

## The empty baseline

Diff against an empty tree and every line in the named path comes back as an addition, so the
walkthrough machinery below runs unchanged, one code path, not two. Deciding to use it is also
deciding the page's mode: every renderer in step 4 takes `<explain>`, and in this mode it is
`--explain`. The empty tree
`4b825dc642cb6eb9a060e54bf8d69288fbee4904` cannot be used directly as a diff base: `git merge-base`
and three-dot both reject it with "is a tree, not a commit". Wrap it in an orphan commit first,
once per session:

```bash
EMPTY_BASE=$(git commit-tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904 -m "empty baseline" </dev/null)
```

Then diff two-dot against it, filtered to the path or file list from above. Three-dot still fails
here, "no merge base", since the orphan shares no history with `HEAD`, so two-dot is not a
shortcut, it is the only option; do not "fix" it to three-dot later. Run numstat first, before
`raw.diff` gets written at all: it is the size check the next section runs on, and it costs
nothing to ask for:

```bash
git diff --numstat $EMPTY_BASE HEAD -- <path>
git diff $EMPTY_BASE HEAD -- <path> > <scratchpad>/raw.diff
```

`$EMPTY_BASE` is this mode's `<base>` everywhere downstream: pass it to `complexity.py` and
`symdelta.py` the same as any other base.

## Negotiate scope before spending

Explain mode has no size ceiling of its own: "explain the auth flow" might be 400 lines, but
"help me understand this codebase" or a bare `src/` aims the empty baseline at an entire subtree,
and every line in it comes back as an addition to fan out. Read the numstat total above before
writing `raw.diff`, spawning a subagent, or starting the graph scripts. Three bands, keyed to
SKILL.md step 2's fan-out table so this does not invent a second scale:

- At or under 1200 lines and 6 files, the same line that keeps a diff on the single-subagent
  route: proceed, say nothing.
- Above that but under 5000 lines: say the total and what it triggers, one line, then keep going.
  "1800 lines across 9 files, that fans out into a few batches" is enough; the user asked to
  understand something, not to approve a budget, so do not turn it into a question.
- At 5000 lines or more: stop before writing `raw.diff`. Come back with the count and two or
  three concrete ways to cut it, drawn from what the numstat and file list actually show, never a
  bare "this is large, continue?": a subdirectory carrying most of the weight, entry points and
  types instead of every file, one language out of a mixed tree, or tests and generated files
  excluded. For example: "help me understand this codebase" against a bare `src/` comes back
  40000 lines across 300 files, 32000 of them under `src/vendor/`. Say "40000 lines, 300 files,
  32000 under `src/vendor/`. Explain `src/` without vendor, or just the entry points (`main.go`,
  `cmd/*.go`) plus the types (`pkg/*/types.go`)?" and wait for an answer.

Diff mode hits the same ceiling from the other side, a 20000-line PR fans out just as wide; there
the change is not negotiable, so state the size and proceed rather than asking the user to review
less of their own PR.

## What `--explain` changes in the page

SKILL.md step 3 carries the rule for resolving `<explain>` itself. What the flag does once
resolved: the page renders plain code with the file's own line numbers instead of green `+` rows,
"Scope: 6 files, 1400 lines" instead of add/remove arithmetic, "What this is" and "Structure" for
the headings, and a graph with no new/gone colouring or legend. Same pipeline either way, only
the wording and colouring change.

All three renderers have to agree: a page with diff-coloured headings over plain-code hunks is
worse than either mode on its own.

Scope the symbols graph too. Against the empty baseline every symbol in the repository is new, so
`sections.py --paths` takes the same pathspec the diff used; `references/graphs.md` has the rest.
