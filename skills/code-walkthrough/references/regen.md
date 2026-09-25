# Regen: skip what has not changed

Background for the optional cache step SKILL.md's 2a-regen stub points at, and for the second
invocation `references/resolved-threads.md` makes. Read it before running `regen.py`, or when its
printed plan's fields need explaining.

Droppable: nothing else in this skill depends on it, and every step below works the same without
it. Worth running only on a re-review of a target already reviewed once, when a prior
`<scratchpad>/state.json` and cache directory both still exist. Run it once, right after
`fanout.py split`, capturing that command's manifest to a file this time
(`... > <scratchpad>/batches/manifest.json`), and before spawning the batch subagents:

```bash
python3 <skill>/scripts/regen.py --diff <scratchpad>/raw.diff --repo <repo> --base <base> \
  --head <head> --cache-dir <scratchpad>/cache --prior-state <scratchpad>/state.json \
  --manifest <scratchpad>/batches/manifest.json \
  --head-file "$(git rev-parse --git-path HEAD)"
```

`git rev-parse --git-path HEAD` resolves the ref file itself rather than assuming `.git/HEAD`, so
it still works from a linked worktree; no hook is installed and nothing is written into the repo.
The printed plan says what to skip: a batch under `fanout[].skip_subagent: true` had every hunk
carry forward and needs no subagent spawned at all; `hunks.carry_forward`/`re_annotate` are the
same per-hunk decision, already applied to the batches' seed files. Each of the three analysers
under `analysers.<script>` (`complexity.py`, `symdelta.py` and `links.py`) reports `"hit"` or
`"miss"`: on a hit, copy `cache_path` to that script's usual output path instead of running it;
on a miss, run it as normal and also copy the fresh output to `cache_path`, so the next run gets
the hit. `dirty: true` means HEAD moved since `--prior-state` was built, the usual reason to skip
this step and regenerate from scratch instead.
