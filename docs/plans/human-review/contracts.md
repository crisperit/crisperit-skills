# Contracts and conventions — visual-diff skill

Repo: `/home/crispy/dev/private/crisperit-skills`. Quotes and citations only.

## 1. SKILL.md and rationale.md — the skill's own invariants

**Context discipline** (never let the raw diff/template into the orchestrator context):

> "Stay thin: capture the diff to a file and hand the reading of it to subagents; the raw diff and the HTML template never enter your context."
> — `skills/visual-diff/SKILL.md:8-9`

> "## 2. Capture the diff to a file, never into context"
> — `skills/visual-diff/SKILL.md:86`

> "Write it to the session scratchpad, never the git working tree (a stray review page or recap file there pollutes commits and PRs)."
> — `skills/visual-diff/SKILL.md:88-89`

> "...all three produce the same `<scratchpad>/analysis.json`, gated the same way in step 2b, and the diff is never read twice."
> — `skills/visual-diff/SKILL.md:94-95`

> "They read git and the diff, never `analysis.json`."
> — `skills/visual-diff/SKILL.md:108-109` (re: the 2b2/2b3 graph scripts)

> "One subagent, `model=\"sonnet\"`, reads the fragments and the numstat, never `raw.diff`, and writes `<scratchpad>/prose.json`..."
> — `skills/visual-diff/references/fanout.md:86-87`

**Subagent model pinning** (every spawn names a model explicitly):

> "Every subagent spawn passes an explicit `model` param; a built-in agent type with no pinned model would silently inherit the expensive session model."
> — `skills/visual-diff/SKILL.md:10-12`

Concrete pins used throughout SKILL.md: batch annotators `model="haiku"` (`SKILL.md:100`, `:214`), prose/grouping/gate subagents `model="sonnet"` (`SKILL.md:100,192,216,231`), and the fanout.md gate-failure routing rule: "respawn that batch with `model=\"sonnet\"` rather than pushing a third time" (`references/fanout.md:149-151`).

**Confirmation before outward-facing actions:**

> "Naming a PR as the target says which diff to read, not that anything gets written to GitHub; that needs `--pr`, and it asks first."
> — `skills/visual-diff/SKILL.md:57-58`

> "Confirm once per PR per session before the first edit."
> — `skills/visual-diff/SKILL.md:463`

> "In interactive runs, confirm once before the first `gh pr edit` on a given PR this session; a refresh of a region this skill already wrote does not need re-confirming."
> — `skills/visual-diff/references/pr-workflow.md:23-24`

> "Show the exact comment text and where each one lands, then ask once. Posting is outward facing and other people get notified, so it is never automatic, and it is not covered by any earlier confirmation in the session."
> — `skills/visual-diff/references/pr-workflow.md:69-71`

> "Offer to post it back onto the PR only when the user confirms, and only what they confirm."
> — `skills/visual-diff/SKILL.md:485-486`

**"Nothing is published by default":**

> "Nothing is published by default: the page stays a local file unless the user passes --pr or asks in words for it to be hosted." (frontmatter description)
> — `skills/visual-diff/SKILL.md:3`

> "Nothing is uploaded by default. When the user asks for a link, to read it on a phone, or names a host, hand the built page to whatever file-sharing or upload skill is available..."
> — `skills/visual-diff/SKILL.md:440-441`

> "Nothing is published anywhere unless `--pr` was given, or the user asked for hosting in step 5b."
> — `skills/visual-diff/SKILL.md:480`

> "Nothing is published. The page stays a local file unless you pass `--pr` or ask for it to be hosted."
> — `README.md` (visual-diff section)

## 2. pr-workflow.md — the full posting contract

**Authorship guard** (only recap your own PRs; never override silently):

> "Check authorship before writing anything: only recap PRs of your own."
> "ME=$(gh api /user --jq .login)" / "AUTHOR=$(gh pr view <n> --json author --jq .author.login)"
> — `skills/visual-diff/references/pr-workflow.md:12-17`

> "Bail with one clear line naming both logins when they differ. Do not offer to override: a recap on someone else's PR is a deliberate act the user can ask for explicitly. Use whatever `gh` auth is already configured; do not call `gh auth switch` and do not set `GH_TOKEN`."
> — `skills/visual-diff/references/pr-workflow.md:19-21`

(Note: this "do not call `gh auth switch`" convention matches the user's global CLAUDE.md rule below, and this repo tree is `crisperit`'s own account per that rule.)

**LEFT/RIGHT rule** (never re-derive side; it's authoritative from the pasted block):

> "with `path`, `line`, `side` (`RIGHT`/`LEFT`, taken from the block, never re-derived), `commit_id` from `links.json`'s head sha, and `body` from the `note:` row. Rewrite nothing; the note is the user's words."
> — `skills/visual-diff/SKILL.md:492-494`

> "`side` from the `RIGHT` or `LEFT` token ... `side: \"LEFT\"` on a removed line is not decoration: posting a `LEFT` line as `RIGHT`, or the reverse, lands the comment on unrelated code, so the token in the block is authoritative and never second-guessed."
> — `skills/visual-diff/references/pr-workflow.md:61-66`

**Confirmation rules** (full set):

> "1. Read the current body ... 2. Wrap the markdown recap in `<!-- visual-diff:start -->` / `<!-- visual-diff:end -->` markers and splice it into the body ... 3. Write it back."
> — `skills/visual-diff/references/pr-workflow.md:26-34`

> "The authorship guard above does not apply here. A review comment on a colleague's PR is the normal case, unlike editing their description. Confirm it is deliberate all the same."
> — `skills/visual-diff/references/pr-workflow.md:72-73`

> "Rewrite nothing. The note is the user's words; tighten only if they ask."
> — `skills/visual-diff/references/pr-workflow.md:74`

**`--body-file` rule** (never inline `--body`):

> "3. Write it back: `gh pr edit <n> --body-file <scratchpad>/pr-body-new.txt`. Always `--body-file`, never `--body` with an inline string: the recap contains backticks, newlines and quotes that shell quoting mangles."
> — `skills/visual-diff/references/pr-workflow.md:32-34`

> "3. `gh pr edit <n> --body-file <scratchpad>/pr-body-new.txt`, always `--body-file`, never an inline `--body` string."
> — `skills/visual-diff/SKILL.md:460-461`

Adjacent contract (pr-markdown.md) a two-way sync must also respect — the splice logic and the no-local-path rule:

> "Do not build this with `re.sub(pattern, wrapped, body)`. A string replacement in `re.sub`..." / "Use a slice-based splice instead. It never touches the replacement-string escaping path..."
> — `skills/visual-diff/references/pr-markdown.md:253-263`

> "The recap never names a local filesystem path, a scratch directory, or a temp file. Every reader of a pull request description is on someone else's machine..."
> — `skills/visual-diff/references/pr-markdown.md:53-56` (also `references/pr-markdown.md:100`)

> "The `jq -r` here appends a trailing newline that is not part of the stored body; the splice below strips it so a refresh stays byte-stable."
> — `skills/visual-diff/references/pr-workflow.md:27-28`

## 3. builder.md and fanout.md — subagent fan-out contract

**How work is split:**

> "`fanout.py split` writes `batch-N.diff`, contiguous slices, a file never straddles two batches, plus a manifest of `{batch, fragment, files, lines}`. Defaults: about 400 lines per batch, at most 8 batches, so a huge diff widens each batch instead of spawning 60 agents."
> — `skills/visual-diff/references/fanout.md:15-17`

> "'Greedy contiguous fill. A file never straddles two batches: its notes read better...'"
> — `skills/visual-diff/scripts/fanout.py:67` (docstring)

**What a batch subagent is forbidden to do:**

> "One subagent per batch, `model=\"haiku\"`, all spawned in a single message so they run concurrently. Each reads only its own `batch-N.diff` and its manifest-listed `fragment-N.seed.json`, a skeleton with every `files[]` and `hunks[]` entry already pre-filled and only `role` and `note` left blank. It copies the seed to `fragment-N.json` and fills those in. It never types an `@@` header and never adds or removes a file or hunk entry, since the seed already has the full shape."
> — `skills/visual-diff/references/fanout.md:21-26`

> "Tell each one explicitly: do not read the repo, do not read other batches, do not judge the code."
> — `skills/visual-diff/references/fanout.md:28`

> "Nothing downstream writes prose: `render.py` and `walkthrough.py` place these strings and build every other part of both outputs from `raw.diff`." → "So a subagent never writes markup." — from SKILL.md:
> "Both outputs are then assembled by `render.py`, so no subagent writes markup."
> — `skills/visual-diff/SKILL.md:9-10`

> "No subagent renders either one. Both are `render.py` calls..."
> — `skills/visual-diff/SKILL.md:374`

**Prose subagent scope** (whole-picture writer, still restricted):

> "One subagent, `model=\"sonnet\"`, reads the fragments and the numstat, never `raw.diff`, and writes `<scratchpad>/prose.json` with `target`, `what_changed`, `how_it_works` and `flow_mermaid`. It may open specific files in the repo when a fragment note is not enough to explain the machinery."
> — `skills/visual-diff/references/fanout.md:86-89`

**Gate/merge constraints relevant to incremental regeneration scheduling:**

> "The glob in the merge command is `fragment-[0-9].json`, not `fragment-*.json`: the latter also matches the `fragment-N.seed.json` skeletons sitting beside them, and every file then looks like it was claimed by two fragments."
> — `skills/visual-diff/references/fanout.md:93-95`

> "**Under about ten**: patch `analysis.json` in the main thread... **Above that, or a whole file entry missing rather than a field**: send the exact problem lines back to whoever produced that part. The fan-out manifest maps each file to its batch and fragment, so a complaint about one file goes to that batch's subagent alone, not to all of them. Re-run the gate after each fix. **Same batch fails twice**: respawn that batch with `model=\"sonnet\"` rather than pushing a third time."
> — `skills/visual-diff/references/fanout.md:140-151`

> "`groups` is the reading order a human follows and is the least safe field here to hand off, but a `(path, role)` list plus the graph summaries is enough to group from..."
> — `skills/visual-diff/references/fanout.md:108-111`

**Rendering ownership (from builder.md), bearing on any incremental-regen plan:**

> "The only thing a model contributes is `analysis.json` ... The template carries all CSS, the line-commenting UI and the copy-for-Claude mechanism. `render.py` replaces `<!-- DIFF_TITLE -->` and `<!-- DIFF_CONTENT -->` and touches nothing else."
> — `skills/visual-diff/references/builder.md:7-12`

> "**Section files are pasted, never regenerated.** Their escaping is already correct, so escaping again renders `&amp;lt;` on the page, and retyping a diagram is how a legend drifts from its arrows. `validate_analysis.py --sections` checks each one's marker survived."
> — `skills/visual-diff/references/builder.md:75-77`

> "**Describe, not judge**. One exception: `section_notes` is deliberately a view, labelled as one. Everything else in `analysis.json` states what the code does and why it is shaped that way, never whether it is correct or should change. Do not assert defects, rank severity, or recommend changes..."
> — `skills/visual-diff/references/builder.md:100-105`

## 4. User's global rules (`~/.claude/CLAUDE.md` and includes)

Read: `/home/crispy/.claude/CLAUDE.md`, `/home/crispy/.claude/RTK.md`, `/home/crispy/.claude/FRONTEND.md`. No repo-local `CLAUDE.md` exists in `/home/crispy/dev/private/crisperit-skills` (checked root and `.claude/`; only ponytail/skill instructions are injected via the session, not a file in the repo).

**Sandbox & git remote rule** — directly relevant, since this skill's own SKILL.md already calls this out at `SKILL.md:68` ("needs dangerouslyDisableSandbox"):

> "Always use `dangerouslyDisableSandbox: true` for any git command that contacts a remote: `git push` / `git fetch` / `git pull` / `git clone` / Any command needing SSH to a remote host."
> — `/home/crispy/.claude/CLAUDE.md` (## Sandbox & Git Remote Commands)

**GitHub CLI account shim per directory tree** — this repo is under `~/dev/private/*`, so the account is `crisperit`:

> "| Tree | Account | |---|---|... | `~/dev/private/*` | crisperit |"
> "`~/.local/gh-shim/gh` is a PATH shim ahead of `/usr/bin/gh`. It reads `$PWD`, injects that account's `GH_TOKEN`, and execs the real binary... Do not switch accounts with `gh auth switch`, and do not add a hook that rewrites `gh`."
> — `/home/crispy/.claude/CLAUDE.md` (## GitHub CLI accounts)

This directly matches (and independently reinforces) the skill's own rule in `references/pr-workflow.md:21`: "do not call `gh auth switch` and do not set `GH_TOKEN`" — a plan touching `gh` auth in this repo must not fight the shim.

**Frontend design skill requirement** — applies only if the plan touches the HTML template/CSS/UI (`assets/diff-review-template.html`, `render.py`'s HTML output, the line-commenting UI):

> "Any work that produces or changes UI (a page, component, layout, styling, redesign, or design system) loads a design skill FIRST, before writing markup or CSS."
> "| Default for any new UI | `design-taste-frontend` | | Improving an existing/ugly UI | `redesign-existing-projects` | ..."
> — `/home/crispy/.claude/FRONTEND.md`

If the plan is pure Python/logic (scripts, gating, fan-out scheduling) with no visual/markup change, this FRONTEND.md gate does not apply — "Skip only for pure logic changes that touch no visual output."

**Ponytail laziness rules** — not repo-specific (no ponytail file found in this repo); ponytail is active session-wide per the system reminder (level: full), not a repo convention. Key constraints bearing on any plan drafted here: ladder-first (YAGNI, reuse before new code, stdlib/native/installed-dep before new deps), no speculative abstractions, and "Lazy code without its check is unfinished" — any non-trivial logic needs one runnable check (assert-based `demo()`/`__main__` or a small `test_*.py`), matching this repo's actual test style (see §5).

## 5. Repo conventions: no conventions skill, assert-based `unittest`-free tests

- No `.claude/skills/conventions/SKILL.md` or equivalent exists in this repo. `.claude/skills` is present but empty (only a placeholder/allowlist entry, no content found; `find .claude -maxdepth 4` shows `agents, commands, hooks, launch.json, loop.md, output-styles, routines, scheduled_tasks.json, settings.json, skills, workflows` — `skills` has nothing under it in this repo).
- No `pytest.ini`, `pyproject.toml`, `setup.cfg`, `tox.ini`, or `Makefile` anywhere in the repo.
- Tests are plain assert-based scripts, not `pytest`/`unittest`. Every `skills/visual-diff/scripts/test_*.py` is self-contained:

> `#!/usr/bin/env python3` / `"""Self-check for fanout.py. Assert-based, no framework."""`
> — `skills/visual-diff/scripts/test_fanout.py:1-2`

Each ends with a manual test list and `if __name__ == "__main__":` runner, e.g.:

> ```
> for test in tests:
>     test()
>     print(f"ok  {test.__name__}")
> print(f"\n{len(tests)} passed")
> ```
> — `skills/visual-diff/scripts/test_fanout.py` (tail, lines ~280-287), gated by `if __name__ == "__main__":` at `test_fanout.py:288` (same pattern confirmed in `test_render.py:572`)

**Exact command to run a given suite** (each file is independently runnable, no aggregate runner exists in the repo):

```bash
python3 skills/visual-diff/scripts/test_fanout.py
```

(substitute the target `test_*.py` filename; run each one directly — there is no single "run all tests" command/config in this repo). Files: `test_complexity.py`, `test_coupling.py`, `test_fanout.py`, `test_layers.py`, `test_links.py`, `test_markers.py`, `test_render.py`, `test_sections.py`, `test_splice_assets.py`, `test_structure.py`, `test_structure_generic.py`, `test_symdelta.py`, `test_validate_analysis.py`, `test_walkthrough.py` (all under `skills/visual-diff/scripts/`), plus `skills/visual-diff/scripts/extractors/lsp/test_extract.py` and `test_extract_languages.py`.

This matches ponytail's own testing rule (§4): no framework, no fixtures, assert-based self-checks — the repo already follows that convention natively, so a plan should add tests in the same style rather than introducing pytest/unittest.

## 6. Frozen-contract / gotcha grep results

Grepped `skills/visual-diff/SKILL.md`, `skills/visual-diff/references/*.md`, and `skills/visual-diff/scripts/*.py` for `ponytail:`, `do not`, `never`, `gotcha`, `IMPORTANT` (case-insensitive). No `ponytail:` or `gotcha` markers exist anywhere in the skill. No `IMPORTANT` markers either. The `do not` / `never` hits that constrain a plan touching this skill (beyond ones already quoted in §1-3 above):

- **`re.sub` splice trap** (constrains any PR-body-editing code):
  > "Do not build this with `re.sub(pattern, wrapped, body)`."
  > — `skills/visual-diff/references/pr-markdown.md:253`

- **Backgrounding the graph scripts is explicitly forbidden**, with its own reference doc:
  > "Run the four sequentially, in one sandboxed Bash call. **Never background them with `&` and `wait`**"
  > — `skills/visual-diff/SKILL.md:261-262`, elaborated at `skills/visual-diff/references/graphs.md:34` ("## Never background the four scripts with `&` and `wait`")

- **No fallback to name-matching when symdelta returns null** (constrains any incremental/regen shortcut logic):
  > "A `null` is never licence to fall back to name-matched edges; see `references/graphs.md` for why that guess is exactly the failure mode this graph exists to avoid."
  > — `skills/visual-diff/SKILL.md:300-303`, and `references/graphs.md:71`

- **Gate is a hard stop, not advisory**, twice stated identically:
  > "Do not proceed to step 3 until it exits 0. Never render an analysis that has not passed."
  > — `skills/visual-diff/SKILL.md:243-244`
  > "Do not proceed to step 3 until the gate exits 0. Never render an analysis that has not passed."
  > — `skills/visual-diff/references/fanout.md:133-134`

- **Recap-only path must not import the fan-out's budget logic**:
  > "The 20000-char walkthrough budget and its demotion machinery do not apply on this path... Do not wire `--max-chars` into this route."
  > — `skills/visual-diff/references/recap-mode.md:27-28`

- **Section files are frozen once generated** (constrains incremental regeneration specifically — a plan cannot patch a section file in place, only regenerate and re-paste it):
  > "Section files are pasted, never regenerated."
  > — `skills/visual-diff/references/builder.md:75`, mirrored in `references/pr-markdown.md:97`

None of these are marked with a `ponytail:` tag (that convention isn't used in this repo's committed files — it's a session-level instruction, not a repo artifact).
