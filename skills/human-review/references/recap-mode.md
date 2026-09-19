# `--recap-only`, the cheap route

Skips the fan-out entirely. Build step 2b2's complexity JSON and step 2b3's symbol-delta graph
first, same condition as always, only when the target names two refs. Then spawn one
`general-purpose` subagent, `model="sonnet"`, that reads `git diff --numstat` plus those two
JSONs' summary fields only, never their full bodies, and writes `analysis.json` with `target`,
`verdict`, `what_changed`, `how_it_works`, `flow_mermaid` and `groups`, one
line per group. No per-file `role`, no
per-hunk `note`, no walkthrough, no HTML page. Against the 9m40s of a full run this is estimated
at 60 to 90 seconds.

Gate it with `--recap` instead of the ordinary call:

```bash
python3 <skill>/scripts/validate_analysis.py --diff <scratchpad>/raw.diff \
  --analysis <scratchpad>/analysis.json --recap
```

This skips every per-file and per-hunk check and instead requires `groups` to cover every path
in the diff exactly once. That coverage check is the only thing standing between this mode and
silently dropping a file, so never skip it.

Render the normal markdown call from step 3, with `--symbols` and `--complexity` but no
`--walkthrough` or `--links`. `render.py` then emits a `### Reading order` section from `groups`
in its place, which is what makes step 3b's `--rendered` gate pass. Skip step 3's HTML call and
steps 4 and 5 entirely.

The 32000-char walkthrough budget and its demotion machinery do not apply on this path: there is
no walkthrough, and GitHub's real cap is 65536. Do not wire `--max-chars` into this route.

Composes with `--pr`. Everything past step 2a-recap other than 3b, 6, 7 and 8 is for the other
routes: step 2a's fan-out and step 2c do not run; step 2b3 does, to build the symbol-delta
section.
