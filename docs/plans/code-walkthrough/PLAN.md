# Plan: evolving `visual-diff` into `human-review`

> Superseded on the name: the skill is now `code-walkthrough`. `human-review` collided with
> petergyang/human-review, an unrelated skill on the same `/human-review` command. The markdown
> recap and the `--pr` description flag were cut at the same time, so the skill builds only the
> HTML review page now. Comment posting to GitHub stayed. Everything below still describes the
> state document and the note lifecycle accurately; read `human-review` as `code-walkthrough`.

## What changes, and what does not

The skill is renamed to `human-review` and grows a state document, `state.json`, that owns every mutable part of the review page: human notes, their GitHub lifecycle, per-hunk annotation hashes, and staleness. One JavaScript renderer draws that state, inline JSON for the dependency-free single file the page always is. Notes gain a draft -> posted lifecycle targeted at a GitHub PR, a sync-in path for comments that already exist on the PR, text-based re-anchoring when line numbers shift, and hash-keyed incremental regeneration so a re-run only redoes the work whose input actually changed. What deliberately does not change: the diff body itself stays server-rendered HTML from `walkthrough.py`, `render.py` stays a string-interpolation renderer, the four graph analysers keep their current CLI contracts, `validate_analysis.py` stays the gate over `analysis.json` + `raw.diff`, the `<!-- visual-diff:kind -->` markers and the `~/.cache/visual-diff` directory keep their literal names, and agent-fix notes are out of scope for this plan and are not designed for beyond one `target` field left in the note record.

Deliberately not built, with the reason: a client-side diff renderer (the JS would have to reproduce the `<span class="a|d|c|h">` line-numbering contract that `test_walkthrough.py` pins in three tests, code-map.md:292-298, with no runnable Python check left behind, and it buys nothing because the diff text is immutable for a given diff); a daemon or watcher process (the agent is the orchestrator, scope item 7); GraphQL `resolveReviewThread` (state.md:165-180, no REST equivalent, and resolving is not in scope); any LLM call from the server; any `anthropic` or Agent SDK dependency; note deletion server-side.

---

## Phase 1: rename to `human-review`, keep `/visual-diff` as an alias

**Files touched**

- `skills/visual-diff/` -> `skills/human-review/` (`git mv` the whole tree; `install.sh` takes the install name from `basename` of each `skills/*/` dir and hardcodes no skill name, state.md:53-59, so the rename alone reaches `~/.claude/skills`)
- `skills/human-review/SKILL.md:2` (`name: visual-diff` -> `name: human-review`), `:3` (description; keep every existing trigger phrase including "visual diff", "/visual-diff", both -ize and -ise spellings, and add "human review", "review this with me", "/human-review"), `:6` (`# Visual Diff` heading), `:384`, `:392` (example scratchpad filenames `visual-diff-<slug>.{html,md}` -> `human-review-<slug>.{html,md}`)
- `README.md:4,6,13,14,15,16,27,40`
- `.claude-plugin/marketplace.json:10` (the description string starts with the literal `visual-diff:`, state.md:40,48-51)
- New: `skills/visual-diff/SKILL.md`, a six-line stub, frontmatter `name: visual-diff`, description "Renamed to human-review. Invoke the human-review skill with the same arguments.", body one sentence. `install.sh` symlinks it alongside, so a literal `/visual-diff` still resolves. **Superseded 2026-09-18: the stub was removed, `/visual-diff` no longer resolves as a command; the word triggers "visual diff"/"visualize diff" stay in the `human-review` description.**

**Not renamed, and why**

- `<!-- visual-diff:coupling|layers|structure|symbols|walkthrough|walkthrough-floor -->` (sections.py:12,32,36,46,60; render.py:117; walkthrough.py:345,502,531; validate_analysis.py:361). These are opaque internal tokens checked by `check_sections` (code-map.md:214-215). Renaming them is a no-value diff across five source files and four test files (test_render.py 12 occurrences, test_sections.py 3, test_validate_analysis.py 3, test_walkthrough.py 4, state.md:93-97).
- `<!-- visual-diff:start -->` / `<!-- visual-diff:end -->` (pr-markdown.md:243,245,270; pr-workflow.md:29; test_markers.py:10). These markers are **already in the wild inside PR descriptions on GitHub**. Renaming them makes the next refresh of an existing PR append a second block instead of splicing the first, which is exactly the idempotency `test_markers.py` exists to protect. They stay.
- `$XDG_CACHE_HOME/visual-diff` (symdelta.py:50, graphs.md:126-127, asserted in test_symdelta.py). This holds the compiled Go extractor binary plus Go's build caches (code-map.md:246-251). Renaming it orphans every existing user's cache and forces a recompile, a behaviour change for zero gain (state.md:101-104).

**Check**

```bash
cd /home/crispy/dev/private/crisperit-skills/skills/human-review/scripts \
  && for t in test_*.py; do python3 "$t" >/dev/null || echo "FAIL $t"; done; echo done
```

All 14 suites must pass unchanged at the new path. Nothing in them should need editing in this phase; if one does, a marker or cache path was renamed that should not have been.

---

## Phase 2: the state document and the client-rendered note layer (static mode)

This is the phase that makes the page render its mutable layer from JSON. It is also where the rewrite is deliberately kept small.

**The insight that avoids a rewrite.** `wireHunk` already derives, per line, the file path (`.hunk-path` textContent) and the hunk header, which it parses with `/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/` (template lines 758-761, 853-868). Python's `HUNK_PREFIX` is `^(@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@)` (validate_analysis.py:64), the same shape. So the browser can reconstruct a hunk's identity key, `path + "\t" + prefix`, byte-identically to Python **without a single change to the emitted markup**. No `data-hunk` attribute, no new class.

One correction the grill caught, and it is load-bearing. `parseHeader` today returns only `{oldStart:+m[1], newStart:+m[2]}` and throws away `m[0]`, the matched substring. Python's `HUNK_PREFIX` captures the literal substring *including* the `,<count>` groups, so reconstructing from `oldStart`/`newStart` alone yields `@@ -204 +204 @@` where Python has `@@ -204,7 +204,9 @@`. Those disagree for every hunk whose line counts are not exactly 1, which is nearly every hunk. The fix is one line, `parseHeader` also returns `prefix: m[0]`, and it is a change to a JS function's return value, not to emitted markup, so the `'<pre class="diff">'` string assertions are still untouched. Without this fix, hunk ids silently mismatch between `state.py` and the browser and every mechanism built on them breaks quietly.

Why the markup must stay untouched: `test_walkthrough.py:85,100,198,208` hard-code the literal string `'<pre class="diff">'`, so adding any attribute to that tag breaks four assertions in the suite that is most load-bearing for this work (code-map.md:292-298). Deriving the id in JS keeps those tests passing unedited.

**Files touched**

- New `skills/human-review/scripts/state.py`. `build(analysis, diff_text, links=None, prior=None)` -> the state dict; `--out` writes it. Reuses `validate_analysis.parse_hunks` (validate_analysis.py:94-156, the single owner of diff parsing, already imported by render.py:45 and walkthrough.py) rather than re-parsing. Adds one thing to `parse_hunks`: capture the `index <old>..<new>` line into `files[path]["blob"]`, an additive key, absent -> `""`.
- `skills/human-review/scripts/render.py`: one new constant `STATE_PLACEHOLDER = "<!-- HR_STATE -->"`, one new `--state` argument beside the existing ones (render.py:319-332), one extra `.replace()` after the two at render.py:209-210. The state JSON is embedded as `<script type="application/json" id="hr-state">...</script>` with `<` escaped as `&lt;` so the blob cannot terminate the script element. `render_html`'s signature gains `state=None`. Existing behaviour is untouched: `test_render.py:43` builds its own two-placeholder template and a `.replace()` for a missing placeholder is a no-op, so `test_both_placeholders_are_replaced` (test_render.py:78-81) and the paste-verbatim and escaping tests all still pass.
- `skills/human-review/assets/diff-review-template.html`: `<!-- HR_STATE -->` placed near line 426 beside `<!-- DIFF_CONTENT -->`, inert when unfilled. The commenting engine at lines 751-919 is rewritten in place so that the in-memory `Map` at line 755 is replaced by the state document's `notes` array as the single source, and every mutation goes through one `save()` function. `wireLine` keeps capturing `span.textContent` verbatim before inserting the number badge (template line 837) and now writes it into the note's `anchor_text`. `renderPanel()` (768-787) renders from state. The `fb-copy` output format (874-917) is unchanged byte for byte, because `references/pr-workflow.md:38-55` parses that exact block back out. It is scoped to `state == "draft"` notes only: once `notes[]` can also hold already-posted notes and synced GitHub comments (phase 4), an unfiltered copy would hand the agent comments that are already on the PR, and the pasted-block format has no field that would let the agent tell the difference, so it would post duplicates.
- `skills/human-review/SKILL.md` step 3: add `--state <scratchpad>/state.json` to the HTML `render.py` invocation, and a new step 2e that runs `state.py`.
- New `skills/human-review/scripts/test_state.py`.

**Mechanism.** `state.py` writes `state.json`. `render.py --state` inlines it. On load the JS parses `#hr-state`, merges any locally-persisted drafts from `localStorage` keyed `hr-notes:<meta.id>`, renders, and persists on every mutation. The template already wraps its one `localStorage` use in try/catch because `file://` and sandboxed share hosts throw (template 986-997); reuse that guard. This alone is a real win over today, where a reload loses every comment (code-map.md:153-158).

**Check**

```bash
python3 skills/human-review/scripts/test_state.py
```

Asserts: the hunk identity key built from `(path, prefix)` matches a Python mirror of the template's regex, **and** that the mirror is still accurate, by reading `assets/diff-review-template.html`, extracting the `parseHeader` regex literal and the `prefix:` return, and comparing them against the expected source strings. A pure-Python suite cannot execute the JS, so the test pins the JS source instead; if someone edits the regex, the assertion fails rather than the mismatch going unnoticed. a hunk's `content_hash` is stable when only its `@@` line numbers move and changes when a body line changes; `build()` round-trips through `json.dumps`/`loads`; `render.py --state` produces a page containing `id="hr-state"` and no literal `</script>` inside the blob.

---

## Phase 3: `--serve`, the dumb localhost server (built, later removed)

Built as designed below, then deleted: the served path never actually ran, the page was always
opened as a plain local file. Removal, and the comment-UX phase that follows it, are recorded in
`docs/plans/human-review/PLAN-comments-and-no-serve.md`.

---

## Phase 4: notes targeted at a GitHub PR

Agent-fix notes are out of scope for this plan. Said once, not designed for. The only accommodation is the `target` key on a note, always `"pr"` today, which is what keeps a second target addable later without a migration.

**Files touched**

- New `skills/human-review/scripts/notes.py`, three subcommands
- `skills/human-review/SKILL.md` step 8 (SKILL.md:482-495), rewritten to read drafts from `state.json` instead of a pasted block; the pasted-block path stays as the static-mode fallback and its format is unchanged
- `skills/human-review/references/pr-workflow.md`, additive only: a section on sync-in and on promotion. Nothing in lines 12-21, 26-34, or 56-74 is weakened.
- New `skills/human-review/scripts/test_notes.py`

**Lifecycle.** `draft` -> `posted`. A successful `gh api` POST sets `gh_id`, `gh_url` and `state: "posted"` on the existing record. The record is never deleted and never rewritten in place by any other step. A failed post leaves the note a `draft`, so nothing is lost. A `posted` note whose `gh_id` reappears in the next sync gains `origin_sync` and its replies, and renders as a thread.

**Sync in.** `gh api repos/<owner>/<repo>/pulls/<n>/comments --paginate` piped to `notes.py sync --state state.json`. Per comment:

- `origin: "github"`, `gh_id: id`, `author: user.login`, `body`, `created_at`, `path`, `side`.
- `line` is used when non-null. **`line === null` is the outdated signal** (state.md:148-163, confirmed empirically on `vercel/next.js` PR #98661 comment `4018096690`): set `line: original_line`, `stale: true`, and render in the separate stale list. `position` and `original_position` are ignored entirely; GitHub's own docs mark `position` as closing down and it was observed non-null on outdated comments anyway (state.md:117-119, 159-163).
- `in_reply_to_id` present: resolve the parent by `gh_id` and set `reply_to` to the parent's local `id`; if the parent is not in state yet, keep `reply_to_gh_id` and resolve on the next pass, since `--paginate` does not guarantee parent-before-child. Replies carry the same `line`/`side` as their parent (state.md:134-138), so no independent anchoring is done for them.
- A comment already in state, matched by `gh_id`, is updated in place; `body` from GitHub always wins for `origin: "github"` records.

**Posting out.** `notes.py payloads --state state.json --commit-id <sha>` emits one JSON object per draft to a directory, each `{body, commit_id, path, line, side}`, or `{body, in_reply_to}` for a reply. The binding contract, every clause preserved:

- `side` is copied verbatim from the note record, which captured it at wire time from the DOM walk. It is **never re-derived** at post time, not from the diff, not from the line number, not by re-anchoring. Posting a `LEFT` line as `RIGHT` lands the comment on unrelated code (pr-workflow.md:61-66).
- `commit_id` is `links.json`'s `head_sha` (SKILL.md:492-494), and `links.json` is also where `head_pushed` gates whether posting is safe at all (code-map.md:260-265).
- `--input <file>` on `gh api`, never an inline `--body`, for the same reason `--body-file` is mandatory on `gh pr edit`: notes contain backticks, newlines and quotes that shell quoting mangles (pr-workflow.md:32-34).
- The agent shows the exact text and where each comment lands, then asks **once, per posting action**. This is not covered by any earlier confirmation in the session (pr-workflow.md:69-71). Nothing is published by default.
- Rewrite nothing. The note is the user's words (pr-workflow.md:74).
- The authorship guard at pr-workflow.md:12-21 continues to gate `gh pr edit` on the description. It does not gate review comments, which are the normal case on someone else's PR, but the deliberateness confirmation still applies (pr-workflow.md:72-73). `gh auth switch` is not called and `GH_TOKEN` is not set (pr-workflow.md:21, and the user's global `gh` PATH shim keys off `$PWD`, state.md:206-214).
- `notes.py promote --state state.json --id <local> --gh-id <id> --gh-url <url>` records the result after each successful post, one note at a time, so a mid-batch failure leaves the already-posted ones marked and the rest still drafts.

**Rate limits.** Sync is agent-triggered, one `--paginate` call, against a 5000/hour personal-token budget (state.md:183-194). There is no poll loop against GitHub. The page polls only the local server.

**Check**

```bash
python3 skills/human-review/scripts/test_notes.py
```

Asserts against an inline fixture array: a reply with `in_reply_to_id` attaches to its parent even when it appears first; a comment with `line: null` lands with `line == original_line` and `stale == true`; running `sync` twice produces an identical state (idempotent); `payloads` emits `side` exactly as stored for both a LEFT and a RIGHT note and emits `{body, in_reply_to}` only for replies; `promote` moves one note to `posted` and leaves the others untouched.

---

## Phase 5: note re-anchoring

**Files touched**: `skills/human-review/scripts/notes.py` (`reanchor` subcommand), `skills/human-review/scripts/test_notes.py`.

**Mechanism.** Every note stores `anchor_text`, the diff line's verbatim text including its leading `+`/`-`/space, captured at wire time (the template already does this at line 837). On a rebuild, `notes.py reanchor --state state.json --diff raw.diff` walks the new `parse_hunks` output and, per note:

1. Restrict the search to the same `path` and the same `side`. A LEFT note searches only removed lines, a RIGHT note only added and context lines. **Re-anchoring never changes `side`**, because that is the field that must never be re-derived.
2. Exact match on `anchor_text` within `anchor_line ± 25`. Nearest wins, ties go to the lower line number.
3. Failing that, match on `anchor_text.strip()` within the same window, for a pure reindent.
4. Refuse to rebind on a trivial anchor: an anchor whose stripped text is shorter than 8 characters or is one of `}`, `});`, `return`, `)`, `],` matches everywhere and rebinding it is guessing. Mark stale.
5. No match, or a trivial anchor: set `stale: true`, leave `line` and `anchor_line` at their old values, and render the note in a separate "stale notes" list on the page. Never guess.

A `posted` note that goes stale is not re-posted and not deleted; it is shown stale with its `gh_url` intact.

**Check**: extra cases in `test_notes.py`, run the same way. Asserts: a note rebinds when its anchor moved 6 lines down; it goes stale when the anchor moved 60 lines; it goes stale when the anchor text is `}`; a LEFT note never binds to an added line; a rebound note keeps its `side`, `gh_id` and `body`.

---

## Phase 6: incremental regeneration, and the post-commit dirty flag

This is the droppable phase. Nothing above depends on it, and the skill is fully usable without it.

**What can actually be keyed, honestly.** Nothing in the repo hashes anything today (code-map.md:238-258). And three of the analysers cannot be keyed per hunk at all: `coupling.py`, `structure.py` and `symdelta.py` take `--base`/`--head` git refs and diff internally rather than consuming `raw.diff` (code-map.md:225-236). That is not a limitation to work around, it is a better key: their output is a pure function of `(base_sha, head_sha)`, so they are cached on exactly that, which is cheaper and more precise than any set-level diff hash. `layers.py` reads the whole repo tree at one ref plus `coupling.json`, so it keys on `(head_sha, coupling_cache_key)`. Only `complexity.py`, `links.py` and `fanout.py` take the diff (code-map.md:233-235), and those key on the set hash.

| Work | Key | Notes |
|---|---|---|
| per-hunk annotation (`role`, `note` in `analysis.json`) | `sha256(hunk body lines + "\n" + file blob sha)` | body lines only, **not** the `@@` header, so a hunk that only shifted line numbers still matches |
| grouping / reading order, `what_changed`, `how_it_works`, `flow_mermaid` | set hash = `sha256` over sorted `(path, hunk body hash)` pairs | whole-change judgment, changes when anything changes |
| `coupling.py`, `structure.py`, `symdelta.py` | `(base_sha, head_sha, SCRIPT_VERSION)` | no CLI change, no re-diffing of `raw.diff` needed |
| `layers.py` | `(head_sha, coupling key, SCRIPT_VERSION)` | whole-repo input (code-map.md:227) |
| `complexity.py`, `links.py` | set hash | both already take `--diff` |

**Mechanism.** New `skills/human-review/scripts/regen.py`. It computes every key, compares against `<scratchpad>/cache/<key>.json`, and **prints a plan**: which cached outputs to copy in, which commands to run. The agent reads the plan and runs only the named commands. No analyser's CLI changes, which is precisely what keeps `test_coupling.py`, `test_structure*.py`, `test_layers.py`, `test_symdelta.py`, `test_complexity.py` and `test_links.py` untouched (code-map.md:308-312).

The per-hunk carry-forward is the one that saves real money. `fanout.py split` pre-seeds `fragment-N.seed.json` with every `files[]` and `hunks[]` entry present and only `role` and `note` blank (fanout.md:21-26). `regen.py` pre-fills those two fields from the prior `state.json` for every hunk whose body hash is unchanged, and a batch whose hunks are all unchanged does not get a subagent spawned at all. Two gate properties hold by construction: a carried-forward note on a byte-identical hunk still shares a tokenized identifier with that hunk's own added/removed lines, so `_identifier_pieces` (validate_analysis.py:179-200) still passes, and carrying notes forward can only lower the empty-note ratio, so `EMPTY_NOTE_FLOOR = 0.8` (validate_analysis.py:77) still passes. The gate stays a hard stop either way (SKILL.md:243-244).

Known ceiling: two textually identical hunks in one file collide on `(path, body_hash)`. Resolved first-unmatched-wins in file order, which can swap two identical hunks' notes. Since the hunks are identical, so are useful notes about them. Not worth more code.

Degradation: a file with no `index` line (binary, rename-only) has no blob sha, so its hunk hashes fall back to body-only and its annotations are treated as always stale. Correct direction to fail in.

**Dirty flag, without a git hook.** The original design installed a `post-commit` hook that touched a flag file. Dropped, for two reasons the grill surfaced. First, `state.json` already stores `meta.head_sha`, so "has HEAD moved since this page was built" is answerable by reading the current ref and comparing, no hook needed. Second, the hook was worktree-broken: in a linked worktree `.git` is a file, not a directory, so writing `.git/hooks/post-commit` fails outright, and hooks live in the common dir and fire for every worktree of the repo, so installing one while reviewing worktree A would silently flag worktree B.

Instead, the agent resolves the ref path once at build time, when it has git available and can get it right (`git rev-parse --git-path HEAD` handles worktrees correctly), and passes it as `--head-file`. `regen.py` reads that one file, compares to `meta.head_sha`, and reports `meta.dirty` plus `meta.dirty_at`. It is a file read, not a git command, so nothing here shells out or knows what a commit is. The page shows a banner reading "commits since this page was built". The agent, next time it is invoked, sees the banner condition, runs `regen.py`, rebuilds. No hook is installed, nothing is written into the user's repo, and no consent dialog is needed for something that is now a read.

**Check**

```bash
python3 skills/human-review/scripts/test_regen.py
```

Asserts on a fixture pair of diffs: a hunk whose body is unchanged but whose `@@` line numbers moved keeps its hash and is planned as carry-forward; a hunk with one changed line gets a new hash and is planned for re-annotation; the set hash changes when any hunk hash changes; the ref-based analysers are planned as cache hits when `base_sha` and `head_sha` are unchanged even though hunks changed; bumping `SCRIPT_VERSION` invalidates them.

---

## Phase 7: MCP, not now

Not built. The verdict and its trigger, recorded so it is not re-argued from scratch. Phase 3,
the server this section reasons about, was itself later removed (see
`docs/plans/human-review/PLAN-comments-and-no-serve.md`), which only reinforces the verdict below.

MCP's real value is discovery by an agent that was never told the tool exists. That is not this situation: a skill is already the instruction sheet, the agent already has Bash, and the server is three routes. So MCP would buy convenience, not capability.

The costs are concrete. MCP config is static, so the ephemeral port would have to become a fixed one. A static config cannot carry a per-run token, so the token would become a long-lived secret in a shell profile, on a server that writes review state into a repo. And MCP connects at session startup, so a server that exists for twenty minutes a day would be a failed server entry the rest of the time. Implementation is either roughly 200 lines of hand-rolled JSON-RPC plus the `initialize`/`tools/list`/`tools/call` handshake, or the `mcp` SDK as a dependency in what is meant to be the stdlib tier.

The security work is not duplicated, which is the one piece of good news: MCP's guidance for local HTTP servers is bind 127.0.0.1, validate Origin, 403 on mismatch, allow a missing Origin for non-browser clients. Phase 3 already does exactly that, so the adapter inherits it.

**Trigger to revisit:** when the server stops being per-review and becomes a long-lived personal daemon holding many reviews. At that point a fixed port and a stable token stop being compromises and MCP is the right call. Until then, `POST /mcp` returns 501.

---

## Acceptance criteria to mechanism

| Acceptance criterion | Mechanism | Phase |
|---|---|---|
| Skill is named `human-review` | dir rename + `SKILL.md:2,3,6`, `README.md`, `.claude-plugin/marketplace.json:10` | 1 |
| ~~`/visual-diff` still works~~ (dropped 2026-09-18) | the stub skill was deleted; only the word triggers in the `human-review` description remain | 1 |
| Nothing breaks in the rename | markers and the `~/.cache/visual-diff` dir keep their literal names; all 14 suites pass unedited | 1 |
| Page renders from a state JSON document | `state.py` builds it, `render.py --state` inlines it behind `<!-- HR_STATE -->`, template JS is the single renderer for notes, threads, staleness | 2 |
| Static, dependency-free page inlines the JSON | `<script type="application/json" id="hr-state">` with `<` escaped to `&lt;`; the output is one file, python3 stdlib only, and drafts survive reload via `localStorage` | 2 |
| Per-line human notes, PR target only | `notes[]` with `target: "pr"`; `notes.py payloads` emits `path`/`line`/`side`/`commit_id`/`body` | 4 |
| draft -> posted -> synced thread | `state` field, `gh_id` set by `notes.py promote`, replies attached by `notes.py sync` | 4 |
| Promotion never deletion | `promote` mutates one record in place; no code path removes a note server-side | 4 |
| Sync existing PR comments including replies | `gh api ... --paginate` -> `notes.py sync`, `in_reply_to_id` -> `reply_to` with deferred resolution | 4 |
| Outdated comments where `line` is null | `line === null` -> `line = original_line`, `stale = true`, separate list; `position` ignored | 4 |
| Authorship guard preserved | `pr-workflow.md:12-21` untouched; still gates `gh pr edit`, not comments | 4 |
| LEFT/RIGHT never re-derived | `side` stored at wire time, copied verbatim into the payload; re-anchoring searches within one side and cannot change it | 4, 5 |
| Always `--body-file` | `gh api --input <file>`, `gh pr edit --body-file` | 4 |
| Confirmation before every post, nothing published by default | agent shows exact text and asks once per posting action, per `pr-workflow.md:69-71` | 4 |
| Store anchor text, rebind near old line | `anchor_text` captured verbatim at wire time; `reanchor` searches `anchor_line ± 25`, exact then whitespace-normalized | 5 |
| No match -> stale, separate list, no guessing | `stale: true`, rendered apart; trivial anchors refuse to rebind at all | 5 |
| Per-hunk hash for annotation work | `sha256(hunk body + file blob sha)`, body excludes the `@@` header | 6 |
| Set-level hash for whole-change work | sorted `(path, hunk hash)` pairs, drives grouping, prose, flow, complexity, links | 6 |
| Regenerate only what changed | `regen.py` prints a plan; carried-forward notes pre-fill `fragment-N.seed.json` | 6 |
| Agent performs all page content updates | nothing regenerates on its own; every rebuild is an explicit agent-run command | 6 |
| Page knows commits landed since it was built | agent passes `--head-file` (resolved with `git rev-parse --git-path HEAD`, worktree correct); `regen.py` reads it and compares to `meta.head_sha`; no git hook installed | 6 |
| MCP deferred | never built; phase 7 records the verdict and the trigger condition | 7 |
| Agent agnostic, no SDK, no `anthropic` | no new dependency anywhere; every script is stdlib; no LLM call from any script | all |
| Raw diff and template never enter orchestrator context | unchanged: `state.py`, `render.py` and `regen.py` are shelled-out leaves reading files (SKILL.md:8-9, 86) | all |
| Every subagent spawn pins a model | unchanged: `model="haiku"` annotators, `model="sonnet"` prose/grouping/gate (SKILL.md:10-12, 100, 192, 214, 216, 231) | all |
| No subagent writes markup | unchanged: only `render.py` and `walkthrough.py` emit HTML (SKILL.md:9-10, 374) | all |
| Tests are assert-based `test_*.py`, no pytest | four new files in the existing style, each with a `tests = [...]` list and an `if __name__ == "__main__":` runner | all |

---

## The state document

`<scratchpad>/state.json`. One file, one schema, one delivery mode: the page is always a plain
local file, and reads this document inline.

```
{
  "meta": {
    "id": "pr-482-a3f1",            str, stable per review target, localStorage key
    "target": "master...HEAD",      str, copied from analysis.json
    "repo": "crisperit/crisperit-skills",   str|null
    "pr": 482,                      int|null
    "base_sha": "9c1d2e4...",       str|null
    "head_sha": "4b7a0f1...",       str|null   also the commit_id for posting
    "head_pushed": true,            bool, from links.json; false gates posting
    "set_hash": "1f4c...",          str, sha256 over sorted (path, hunk.hash)
    "built_at": "2026-09-16T11:02:55Z",
    "dirty": false,                 bool, server-computed from the flag file
    "dirty_at": null                str|null
  },
  "files": [
    {"path": "scripts/render.py", "role": "...", "blob": "e4f1a9c", "added": 12, "removed": 3}
  ],
  "hunks": [ ... ],                 see below
  "notes": [ ... ],                 see below
  "groups": [{"title": "...", "why": "...", "paths": ["..."]}]
}
```

`hunks[]`, one record:

```json
{
  "id": "scripts/render.py\t@@ -204,7 +204,9 @@",
  "path": "scripts/render.py",
  "prefix": "@@ -204,7 +204,9 @@",
  "hash": "a91c4e77b2d0f315",
  "note": "adds the state placeholder replace after the two existing ones"
}
```

`id` is `path + "\t" + prefix`, reconstructible identically in Python (`HUNK_PREFIX`, validate_analysis.py:64) and in the browser (template lines 758-761), which is why no markup change is needed. `hash` is `sha256(body_lines + "\n" + file.blob)[:16]`, body lines only, so a pure line-number shift preserves it. `note` is the same string `walkthrough.py` baked into the page, kept here so `regen.py` can carry it forward.

`notes[]`, one local draft:

```json
{
  "id": "n-7f3a1c",
  "origin": "local",
  "target": "pr",
  "state": "draft",
  "path": "scripts/notes.py",
  "line": 88,
  "side": "RIGHT",
  "hunk_id": "scripts/notes.py\t@@ -80,6 +80,14 @@",
  "anchor_text": "+    if note_id not in by_id:",
  "anchor_line": 88,
  "stale": false,
  "body": "does this handle a note whose id was never seen before",
  "order": 14,
  "author": "crisperit",
  "created_at": "2026-09-16T11:31:02Z",
  "gh_id": null,
  "gh_url": null,
  "reply_to": null
}
```

The same record after a successful post: `"state": "posted"`, `"gh_id": 4018096690`, `"gh_url": "https://github.com/.../#discussion_r4018096690"`. Nothing else changes, and the record is never removed.

A synced-in outdated GitHub comment:

```json
{
  "id": "gh-4018096690",
  "origin": "github",
  "target": "pr",
  "state": "posted",
  "path": "packages/next/src/build/index.ts",
  "line": 721,
  "side": "RIGHT",
  "hunk_id": null,
  "anchor_text": null,
  "anchor_line": 721,
  "stale": true,
  "body": "this branch is unreachable after the refactor",
  "order": 9999,
  "author": "ijjk",
  "created_at": "2026-09-02T08:14:00Z",
  "gh_id": 4018096690,
  "gh_url": "https://github.com/vercel/next.js/pull/98661#discussion_r4018096690",
  "reply_to": null
}
```

`line` here came from `original_line` because the API returned `line: null` (state.md:148-163). `position` and `original_position` are not stored; they are unreliable (state.md:159-163). A reply carries `reply_to` set to the parent's local `id` and inherits the parent's `line`/`side` rather than anchoring independently (state.md:134-138).

`target` is `"pr"` on every record today. It is the only concession to a second note kind later, and it costs one string.

---

## Write ownership

| Field | Owner | Other party |
|---|---|---|
| `notes[]` where `origin == "local"` and `state == "draft"`: `body`, `line`, `side`, `anchor_text`, `anchor_line`, `order`, creation | page | agent reads only, except `reanchor` may set `line` and `stale` |
| `notes[].state`, `gh_id`, `gh_url`, `reply_to` | agent | page reads only, renders as a thread |
| `notes[]` where `origin == "github"` | agent (via `notes.py sync`) | page reads only, cannot edit or delete |
| `notes[].stale` | agent (`sync`, `reanchor`) | page reads only, renders the stale list |
| `hunks[]`, `files[]`, `groups[]`, everything under `meta` except `dirty` | agent | page reads only |
| `meta.dirty`, `meta.dirty_at` | agent (`regen.py`, from a plain file read of `--head-file`) | both read only |
| the file on disk | agent, always | the page never writes it directly; it holds drafts in `localStorage` until pasted back, see `notes.py import` below |

**Conflicting concurrent write.** The ownership table above already says every field has exactly one writer, so a whole-document compare-and-swap would be the wrong tool: it would manufacture conflicts between writes that do not actually overlap. There is exactly one writer of `state.json` at any time (the agent), so there is nothing to reconcile there; the remaining question is only how a page-authored draft, sitting in `localStorage`, gets into that file at all.

`notes.py import`'s `merge_state` answers that with a **keyed merge**, not a replace: the pasted Copy for agent payload is a partial document. `notes[]` is upserted by `id`, `meta` is merged key by key, `hunks[]`/`files[]`/`groups[]` replace wholesale when present and are left alone when absent. A page payload only ever carries the keys the page owns (`PAGE_DENIED_NOTE_FIELDS` withholds the rest). Two imported drafts touching different notes commute, so there is no conflict to report and no retry loop.

The one genuine conflict left is two edits to the same note body, which for one human plus one serially-invoked agent is rare and is resolved last write wins. That is a deliberate ceiling, recorded rather than engineered around.

---

## Risks

| Risk | Specific failure | Mitigation |
|---|---|---|
| Adding an attribute to `<pre class="diff">` | `test_walkthrough.py:85,100,198,208` compare against the literal string `'<pre class="diff">'`; all four break silently in a suite that also guards line numbering | the hunk id is derived in JS from `(path, prefix)` which `wireHunk` already parses; markup is not touched at all. A comment in `walkthrough.py` beside line 421 states this. |
| Rewriting `render.py` into a document renderer | `test_render.py`'s 627 lines pin the interpolation contract, escaping-before-backtick-promotion order, and byte-for-byte section pasting (code-map.md:299-301); all of it would need rewriting for no user-visible gain | `render.py` stays string interpolation; a third placeholder is additive and is a no-op against `test_render.py:43`'s two-placeholder template |
| Crash mid-write | truncated `state.json`, every note lost | temp file in the same directory + `os.replace` |
| Post succeeds, promotion fails | the note stays a draft and gets posted twice on the next run | sync before every posting round; a draft whose `(path, line, side, body)` already matches a `github`-origin comment by this user is treated as already posted and promoted without re-posting |
| Re-anchoring binds to the wrong line | a comment lands on unrelated code on the PR, which is the exact failure `side` discipline exists to prevent | same-side-only search, `± 25` line window, trivial anchors refuse to bind, no match means stale not guessed; `side` is never recomputed |
| Carrying a stale annotation forward | the page describes a hunk that has changed | the carry key includes the file's blob sha, so any change to the file invalidates every hunk in it, not just the edited one. Conservative in the safe direction. |
| Two identical hunks in one file collide on the carry key | their notes swap | first-unmatched-wins in file order; the hunks are byte-identical so a useful note applies to both. Known ceiling, documented in `regen.py`. |
| Stale cache after a script edit | `coupling.json` reused across a behaviour change | `SCRIPT_VERSION` constant in each cache key, bumped with the script |
| No `index` line in the diff | no blob sha, weaker hash | fall back to body-only hashing and mark the file always-stale |
| Static-mode drafts do not survive regeneration | a user types notes into a `file://` page, the skill regenerates at a new scratchpad path, and the browser's `localStorage` for the old file's origin is unreachable, so the notes look lost | `localStorage` is honestly scoped as a same-file reload convenience, not durability. `meta.id` is derived from the review target (repo + PR, or base..head), never from `head_sha`, so it is stable across rebuilds, and on rebuild the agent carries notes forward from the prior `state.json` in the scratchpad. Cross-session carry-forward of drafts typed into a page whose `state.json` is gone is not promised. |
| Writing review artifacts into the working tree | they pollute commits and PRs (SKILL.md:88-89) | `state.json` stays in the scratchpad; the dirty flag lives under `.git/`, which is never committed |
| Renaming the PR-description markers | an existing PR gets a second appended block instead of a splice, breaking `test_markers.py`'s idempotency guarantee | the `<!-- visual-diff:start/end -->` markers are not renamed |
| Renaming the cache directory | every existing user's compiled Go extractor is orphaned and recompiles (state.md:101-104) | `~/.cache/visual-diff` is not renamed |
| Template edits triggering the FRONTEND.md design-skill gate | a UI change made without loading a design skill | phase 2 does change the template's script and add a stale-notes list and a dirty banner, so the design skill is loaded before that markup and CSS is written (contracts.md:185-191). Phases 1, 4, 5 and 6 are pure logic and do not trip it. |

---

## Grilled decisions

One round, three lenses, thirteen findings. Nine changed the plan, four were argued down or bounded. The changes are already merged above; this records what moved and why.

**Changed**

1. **Whole-document CAS replaced by a keyed merge.** The `rev` counter, the 409 response, the page's "replay my own mutations" and the agent's re-GET-and-reapply are all gone. The write-ownership table already guarantees no two writers touch the same field, so the conflicts the CAS existed to detect were manufactured. It also depended on the page tracking which notes it had locally edited, a structure the schema never defined. A merge keyed on note `id` makes concurrent writes commute and deletes four failure modes.
2. **`parseHeader` must also return `m[0]`.** The plan claimed the browser could rebuild the hunk prefix from the existing regex. It cannot: the function discards the matched substring and keeps only the two integers, so it produces `@@ -204 +204 @@` against Python's `@@ -204,7 +204,9 @@`. One line to fix, silent corruption of every hunk id if missed.
3. **The post-commit hook is gone.** `meta.head_sha` plus a read of the current ref answers the same question. The hook was also worktree-broken twice over: `.git` is a file in a linked worktree, and hooks fire across every worktree of a repo.
4. **Singleton and lifetime for `serve.py`.** A `threading.Lock` does nothing against a second process, and the plan had no teardown, which made that second process likely rather than theoretical. Added a pid file, `fcntl.flock` on the state file, and a 30 minute idle exit.
5. **"Copy notes for Claude" filters to drafts.** Once synced GitHub comments and posted notes share `notes[]`, an unfiltered copy would feed the agent comments already on the PR, through a text format with no field to distinguish them. Duplicate posts.
6. **`test_state.py` pins the JS source.** A Python suite cannot execute the template's JS, so the test now reads the template, extracts the regex literal and the `prefix:` return, and asserts on them. Drift fails loudly instead of silently.
7. **Schema trimmed.** `version` had no reader and no second schema to migrate from. `line_old`/`line_new` had no consumer in any phase. Both cut.
8. **`localStorage` honestly scoped.** It is a same-file reload convenience, not durability: a regenerated page at a new path is a new `file://` origin with empty storage. `meta.id` is now explicitly derived from the review target rather than `head_sha`, and carry-forward across rebuilds goes through the prior `state.json`, not the browser.
9. **Origin demoted in the write-up.** Bind address, token and Host carry the defence. Origin is belt and suspenders, kept because it costs four lines, but no longer described as one of four equal walls.

**Argued down, with the defence**

10. **Drop phase 6 entirely.** Refused. The simpler-alternative lens is right that nothing measures slow yet and the user has no users, but incremental regeneration was an explicit scope item, not an inference. It stays, last, and stays marked droppable. If it is cut, it should be cut by the user's decision, not quietly by the plan.
11. **Drop `hunk_id` from notes.** Refused. It is unused by re-anchoring, which matches on path, side and anchor text, but it is what phase 6 carry-forward keys on, and phase 6 is in scope.
12. **Collapse phase 5 into phase 4.** Accepted as true but not acted on. The plan already says they can be built in one pass; keeping them separate costs nothing and makes the re-anchoring rules reviewable on their own.
13. **Binary diffs lack an `index` line.** Correct, the plan overstated it: git does emit `index` for binary files. Rename-only entries are the real case. The fallback is safe either way, so the text stands with that noted here.

**Weakest surviving spot.** Phase 2 rewrites the template's commenting engine, roughly 170 lines of inline JS, and the only automated check that can see it is a source-text assertion. If the note layer renders wrong, no Python test will catch it. The honest mitigation is manual: open the page, add a note, reload, confirm it survives. That is the one place in this plan where the test story is weaker than the rest.

---

## Phase ordering and what is optional

Phases ship in order and each is independently usable. **Phases 1 and 2 are the minimum viable cut**: the skill is renamed, `/visual-diff` still resolves, the page renders its note layer from a state document, and notes survive a reload, all with zero new processes and zero dependencies. Phase 3 shipped and was later removed entirely (`docs/plans/human-review/PLAN-comments-and-no-serve.md`): the page was always used as a plain local file, so the served path never ran. Phase 4 is what makes the notes worth persisting. Phase 5 is small and could fold into phase 4 if it is being built in one pass. **Phase 6 is the droppable one**: hash-keyed regeneration is a cost optimization, and the post-commit dirty flag inside it is droppable independently of the hashing.
