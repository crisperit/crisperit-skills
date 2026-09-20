# Plan: the two-way loop, and cutting the skill loose from one runtime

Successor to `PLAN.md`, which took `visual-diff` to `human-review` and left phase 7 as
"MCP, not now". This plan covers what came out of reviewing PR #2498 end to end on a phone,
reading the closest prior art (`populationgenomics/semantic-code-review`, 0.35.0, henceforth
`scr`), and researching what the GitHub API and the agent-protocol landscape actually allow as
of 2026-09.

## What changes, and what does not

Four things change. **GitHub delivery** moves off the REST review-comments endpoint onto a
GraphQL pending review, because REST cannot append to a pending review and cannot resolve a
thread at all, and because one comment per click walks into secondary rate limits. **Comment
anchors** get resolved against the diff's own postable ranges before they are sent, because
GitHub silently accepts and discards a comment aimed outside a hunk. **The local server** gains
an explicit host and origin allowlist, so the page can be reached through a tunnel without
reopening the DNS-rebinding hole the current pin exists to close. **The note lifecycle**
generalises into a request queue, and `notes.py watch` becomes the wake channel that hands the
agent its next action as a runnable line rather than a status to interpret.

**Superseded.** The local server and the request queue/`watch` wake channel described above are
gone; served mode was removed, see `PLAN-comments-and-no-serve.md`.

Two things change in prose only, at no runtime cost: the main loop stops loading the diff, and
the model names in `SKILL.md` become capability hints.

What does not change: the deterministic core stays exactly as it is. All 15 scripts remain
stdlib-plus-tree-sitter, JSON in and JSON out, with no vendor SDK and no network beyond `gh`.
`validate_analysis.py` stays the gate over `analysis.json` + `raw.diff` and stays the thing that
licenses delegating work to a weaker model at all. The page stays a self-contained HTML file that
can be opened, archived, or shared as a link with no server, which is the one axis where this
skill beats `scr`, whose viewer cannot open without its server at all (its `index.html` pulls
`/data.json`, `/file-text`, `/comments`, `/events`). The four graph analysers keep their CLI
contracts, the `<!-- visual-diff:kind -->` markers keep their literal names, and
`$XDG_CACHE_HOME/visual-diff` keeps its path.

> **Superseded.** Both were renamed later, once the skill itself became `code-walkthrough`: the section markers are `<!-- code-walkthrough:kind -->` and the cache is `~/.cache/code-walkthrough`, falling back to the temp dir when `~/.cache` is not writable. The reasons below stopped holding: the PR-description `visual-diff:start/end` markers this protected no longer exist in the skill at all, and the cache had to move anyway because an agent sandbox does not allowlist `~/.cache`, which cost explain mode its whole symbols section.


Deliberately not built, with the reason: **CRDTs** (Automerge, Yjs) for shared state, because
writes here are coarse and human-paced and a single-writer server plus a `rev` counter never
realistically contends, so the cost is a dependency and a merge-semantics burden for nothing.
**AG-UI**, because it assumes a live frontend runtime subscribed to an event stream, and this
artifact is a generated file. **ACP**, because it is editor-integration plumbing and the review
surface is a browser page. **A2A**, because it is peer-to-peer task delegation, a different layer
from "one artifact, several writers". **SSE from the server to the page**, because the page
already polls `state.json` every four seconds and that is indistinguishable at human speed.
**Any LLM call from the server**, and **any `anthropic` or Agent SDK dependency**, both still out.
**An agent-authored review verdict**: the LLM directs attention, it does not approve or block.

---

## Phase 1: deliver through a GraphQL pending review

**Why**

`SKILL.md:571-607` posts one comment per click with
`gh api repos/{o}/{r}/pulls/{n}/comments --input <file>`, then records it with
`notes.py promote`. Three problems, in descending severity:

1. GitHub's secondary limits throttle rapid content creation independently of the primary quota,
   so a session where twenty notes are clicked in a few minutes is a plausible 403.
2. Twenty separate comments reach the author as twenty notifications rather than one review.
3. The post-then-promote split means a failure between the two loses the record of a comment
   that did land, which the current text works around by telling the agent to promote after each
   success.

REST offers no fix: `POST /pulls/{n}/reviews` takes its `comments[]` array up front and there is
no append-to-pending-review call, an [open gap as of
2026](https://github.com/orgs/community/discussions/168380). `scr` hit the same wall and moved to
GraphQL for exactly this reason, recorded verbatim at `github_graphql.py:17-19` in its tree.

**Files touched**

- `scripts/notes.py`: new `deliver` subcommand, `--state`, `--id`, doing the GraphQL call and the
  local state update in one step. New `submit` subcommand, `--state`,
  `--event COMMENT|APPROVE|REQUEST_CHANGES`, `--body-file`, publishing the pending review.
  `payloads` and `promote` stay for one release, marked superseded in the module docstring.
- Mutations used, all through `gh api graphql`: `addPullRequestReview` to open the pending review
  lazily on first `deliver`, `addPullRequestReviewThread` for a new anchored thread,
  `addPullRequestReviewComment` for a reply into an existing thread,
  `submitPullRequestReview` to publish. `resolveReviewThread` and `unresolveReviewThread` become
  available for free and are wired in Phase 4's queue, not here.
- `state.json`: each note gains `gh_thread_id` and `gh_node_id` beside the existing `gh_id`, since
  edits and replies address GraphQL nodes. The pending review's id is looked up on demand rather
  than stored, so a review submitted from the GitHub web UI mid-session does not strand us.
- `SKILL.md:571-607` (step 8) collapses to: run the lines `watch` printed. `SKILL.md:609-627`
  (step 9) loses its per-comment `gh api` recipe.

**The injection rule, and why it is in this phase**

`state.json` note bodies are written by a browser page. No note text ever reaches a command line.
`deliver --id n7` is safe because the id is opaque and the tool reads the body from state;
`gh api ... --body "<note text>"` is a shell injection with extra steps. This is also why the
GitHub call belongs inside the tool rather than being emitted for the agent to run with the text
interpolated.

**Check**

A fake `gh` on `PATH` recording its argv; assert `deliver` opens exactly one pending review across
three calls, that a note with `in_reply_to` set uses `addPullRequestReviewComment` and not
`addPullRequestReviewThread`, and that no argv element contains a note body.

---

## Phase 2: resolve the anchor before sending

**Why**

`SKILL.md:590-592` copies `side` verbatim from the note, captured at wire time, and posts
`path`/`line`/`side` as-is. If that line is not inside a hunk of the diff being reviewed,
GitHub's GraphQL mutation returns 200 with a null thread and no error: the comment is silently
discarded. `scr` documents this as an empirical finding and resolves proactively
(`review/anchors.py:64-122` in its tree). Our page can produce an out-of-hunk anchor whenever the
head commit moved since the page was built, which the current banner warns about but does not
correct.

**Files touched**

- `scripts/notes.py`: `postable_ranges(diff_text)` parsing `@@` headers into
  `{(path, side): [(start, end), ...]}`, LEFT for removed lines and RIGHT for added and context.
  `resolve(path, line, side, ranges)`: inside a hunk, unchanged; otherwise the nearest hunk line
  within 10, with a note appended to the body saying it moved and from where; otherwise a
  file-level thread with the same kind of note. Called from `deliver`, never from the page.
- `state.json`: each note gains `anchor_status`, one of `anchored | shifted | file_level`, set at
  delivery and shown in the page's thread header.

**Check**

Table-driven: a line inside a hunk, one 3 lines outside, one 40 lines outside, one in a file with
no hunks at all. Assert the resolved target and that the body carries the relocation note in the
two moved cases and not in the first.

---

## Phase 3: an explicit host and origin allowlist

**Superseded.** `serve.py` and the tunnel path are gone; see `PLAN-comments-and-no-serve.md`.

**Why**

`serve.py:173-181` pins `Host` to `127.0.0.1:<port>` or `localhost:<port>` and `Origin` to
`http://127.0.0.1:<port>`. Both checks are correct and worth keeping: they are what stops a page
on an attacker's domain from re-resolving to loopback and scripting this server. But they also
make the page unreachable from a phone. Measured on 2026-09-17 against PR #2498: with an ngrok
tunnel and `--host-header=rewrite`, `GET /` and `GET /state.json` succeed and every `POST` 403s,
because a browser on the tunnel origin always sends that origin.

Disabling the check is the wrong fix and is what every comparable tool warns against. The right
fix is the one Vite (`server.allowedHosts`), code-server (`--trusted-origin`) and Grafana
(`csrf_trusted_origins`) all landed on: an explicit allowlist the operator adds a specific
hostname to. Note for contrast that `scr` has no Host check, no Origin check and no token at all,
relying solely on the loopback bind, so this phase leaves us ahead of it rather than catching up.

**Files touched**

- `scripts/serve.py:321-324`: `--allowed-host` and `--allowed-origin`, both repeatable, both
  appended to the default allowlist and never replacing it. No wildcard, no `true`, no
  `--no-check`: an unlisted origin must still 403, and that is the acceptance test.
- `scripts/serve.py:173-181`: both predicates read the allowlist instead of a literal.
- `scripts/serve.py:346` prints the token on stdout, which is the only place it exists. Also write
  it into `serve.json` with `0600`, so a second process, including an agent that did not spawn the
  server, can attach. The trust boundary is unchanged and already documented at `serve.py:19-26`:
  any process running as this user can read it.
- `SKILL.md:489-531` (step 5): document the tunnel path in two lines, name that the link is public
  to anyone holding it, and keep hosting opt-in and never default.

**Check**

Three requests against a live server: allowlisted origin POST 200, unlisted origin POST 403,
unlisted Host GET 403. Plus an assertion that the default invocation, with no flags, behaves
exactly as today.

---

## Phase 4: a request queue, replacing the one hardcoded verb

**Superseded.** The `requests[]` queue and `publish_requested` are gone; see
`PLAN-comments-and-no-serve.md`.

**Why**

`publish_requested` (`notes.py:203`) encodes a single action. The loop the page needs is wider:
ask the agent to explain a symbol, redraw a flow, regroup the walkthrough, run the tests, resolve
a thread. Adding a boolean per verb does not scale and each one needs its own `watch` branch.

**Files touched**

- `state.json` gains `requests[]`, each `{id, kind, payload, status, result, created_at}` with
  `status` in `pending | claimed | done | failed`. `kind` is uninterpreted by the protocol, which
  is what lets a new verb ship without a schema change.
- `publish_requested` becomes `kind: "post_comment"` with the note id in `payload`. `notes.py`
  keeps reading the old field for one release so a page built by the previous version still works.
- `scripts/state.py`: carry `requests[]` across a re-run the way `notes[]` is carried today.

**The durability rule, which is the point of the phase**

A request sits at `pending` until something handles it, with no expiry. The wake channel is a
latency optimisation, not a correctness requirement: if `watch` was never started, died, or the
session ended, the next agent turn for any reason drains the queue. A user action must never be
lost because nobody was listening. This is the one thing `scr` gets unambiguously right, where
comments survive in the run directory regardless of whether anything is attached.

---

## Phase 5: `watch` emits the next action, and exits before the harness kills it

**Superseded.** `notes.py watch`/`answer` are gone; see `PLAN-comments-and-no-serve.md`.

**Why**

Two distinct failures in the current shape (`notes.py:414-417`, `--timeout 1800`,
`--interval 2`). First, 1800s exceeds the 600s cap on a single Bash call, so the harness kills the
process and the agent gets a kill notice without our payload or our re-arm instruction. Second,
exit code 2 plus prose means the agent has to interpret a status and decide what to do, and the
decision it most often gets wrong is re-arming.

Note what is *not* wrong here: the agent does not poll. A subprocess polls, and the agent is woken
by that subprocess exiting. That is the only push channel into a turn-based agent, MCP included,
since an MCP server cannot initiate a call to its client. It is also universal, because "start a
subprocess, learn when it exited, read its stdout" is the one capability every agent runtime has.
`scr` arrived at the same design (`scr review --wait` long-polls and is re-armed).

**Files touched**

- `scripts/notes.py:414-417`: add `--tick`, default 300, the quiet-exit horizon. `--timeout` keeps
  its meaning as the overall deadline; `--interval` keeps its meaning as the internal poll.
- `watch` stdout becomes a script rather than a report. One action per line, in order. Empty
  output means stop, which is also the correct behaviour when the server or page is gone.
  - quiet tick: one line, the `watch` command to run again.
  - requests pending: one `deliver`/`answer` line each, then the `watch` line.
  - page gone: nothing.
- A line the agent must think about carries the work and ends with the command that closes it:
  `TASK r7 explain_symbol {...}` then `when done: notes.py answer --state ... --id r7 --body-file <f>`.
- Exit codes stop carrying meaning: 0 normal, non-zero a real crash.
- `SKILL.md:489-531,609-627`: the standing instruction collapses to one sentence, run the lines you
  get back in order, no lines means stop. That sentence is what makes the loop portable.

Optional, only if the wake churn becomes annoying in practice: an escalating tick, 5/5/10/15/30
minutes while nothing happens, reset to 5 on any activity. Review pages get used in bursts, so
this cuts an idle hour from twelve wakes to four with no latency cost while the user is clicking.
Not in the first cut.

---

## Phase 6: the main loop stops loading the diff

**Why**

Three leaks, all observed live while reviewing PR #2498 rather than inferred:

1. `SKILL.md:101` routes a diff under about 400 lines to the main thread, which then reads
   `raw.diff` directly. The rationale printed beside it, that a single pass beats parallel spawn
   latency, is an argument against *fan-out*, not against delegation: one subagent costs roughly
   the same wall clock as the main thread doing it, and costs the main context nothing.
2. `SKILL.md:276` says to patch `analysis.json` in the main thread on a small gate failure, and to
   read `git diff <base>...<head> -- <path>` to do it.
3. Delegation did not help where it was already in place, because the reports carried the payload:
   the batch agents handed back their full role lists and the prose agent pasted `what_changed`
   into the main context verbatim. The files were written correctly; the hand-back leaked.

**Files touched**

- `SKILL.md:101`: delete the row. The table becomes two rows, one subagent up to about 1200 lines
  and 6 files, fan out above that.
- `SKILL.md:276`: send the gate's problem lines to a subagent. The gate output already names the
  path and the field, so nothing else needs to travel.
- `references/fanout.md`, batch brief: reply with the fragment path and counts only, never content.

**The line, stated precisely so it is followable**

The main loop never loads diff hunks, file contents, or per-hunk notes. It may load the verdict,
the group titles, and the gate result, because reviewing four group titles is fifteen lines of
prose and that decision genuinely wants a human-checkable step.

---

## Phase 7: model names become capability hints

**Why**

`model="haiku"` and `model="sonnet"` (`SKILL.md:102,195,216,218,261,278`) are both vendor-specific
and perishable. The intent behind them is not.

**Files touched**

Those six sites, rewritten to state the shape of the job and anchor it to a familiar tier: "each on
a small fast model (haiku class): the job is per-hunk noticing inside one slice, and the gate checks
every answer", and "one subagent on a stronger model (sonnet class), since it needs the whole change
in view and nothing downstream can check its judgment".

No config file, no tier registry, no mapping table. The agent knows its own roster better than a
config could, and a config would be a stale copy of that knowledge. The criterion worth writing
down once, because it survives every model release in a way the names do not: **delegate down when
a validator can catch the mistake, keep it up when it cannot.**

---

## Phase 8: the portable contract

**Why**

Measured: the scripts are already runtime-agnostic. Stdlib plus `tree_sitter`, no Anthropic import,
no model name in code, the word "subagent" appearing only in docstrings and one JSON key. All of the
coupling is in the driver prose, 627 lines of `SKILL.md` and 949 of `references/`, naming
Claude-specific mechanisms seventeen times. A runtime with no subagent primitive, Codex CLI or Aider,
has no legal path through step 2 at all.

**Files touched**

- New `scripts/human_review.py` with two subcommands. `prep <target>` does everything deterministic,
  diff capture, the four graph scripts, symdelta, links, and the seed skeleton, then prints the
  fill-in contract. `build` does gate, walkthrough, render, state, splice, serve. An agent's whole
  job becomes: run `prep`, fill the JSON, run `build`.
- `prep --print-brief` emits the role and note rules from `references/fanout.md`, so every runtime
  gets byte-identical instructions instead of a per-runtime paraphrase.
- The routing table gains a row for "no subagent primitive available": single pass, widen the
  batches.
- `SKILL.md` stays, but as a thin Claude adapter over that contract, adding the fan-out speedup
  Claude Code can exploit. Other runtimes get a short `AGENTS.md` instead of a 600-line procedure.

**MCP, and why it is still last**

**Superseded premise.** This section's sequencing ("after the server is the sole writer and the
queue exists") assumes the removed server/queue design; see `PLAN-comments-and-no-serve.md`.

Exposing `list_notes` / `add_request` / `answer` over MCP is the move that makes any MCP-capable
runtime a first-class writer, and Codex, Cursor, Copilot and Gemini CLI all speak it. It must be
Streamable HTTP, not stdio: stdio is one client per spawned process and therefore structurally
single-agent. `scr` already does this for its own tool surface and records a measured 765ms
per-spawn cold start as the reason (its ADR 0003). Worth doing, worth doing after the server is the
sole writer and the queue exists, since those are what it would expose.

---

## Risks

- **A wrong allowlist entry is a real hole.** Mitigated by refusing a wildcard outright, and by the
  acceptance test asserting an unlisted origin still 403s. The flag names a host; it never disables
  a check.
- **The token in `serve.json` is readable by any process running as this user.** Already the stated
  boundary at `serve.py:19-26`, unchanged by writing it to a `0600` file rather than stdout. Worth
  restating in the plan so nobody later reads it as a regression.
- **GraphQL node ids churn.** A thread deleted from the web UI mid-session leaves a stale
  `gh_thread_id`. `deliver` must treat a null-node response as "open a new thread", not as an error.
- **ngrok free shows an interstitial** before the page, which will read as a broken link to anyone
  not expecting it. Say so in the handoff line, every time.
- **Tick churn.** Twelve wakes an hour on an idle review is real context cost. The quiet line must
  be one line, and the escalating tick is the escape hatch if that is not enough.
- **Two tools, one state file.** Phase 4 assumes the server stays the single writer. Any future
  direct-file writer needs the advisory lock this plan does not add, which is the failure mode that
  shows up as lost updates rather than as an error. Superseded along with the server; see
  `PLAN-comments-and-no-serve.md`.

## Grilled decisions

- *Why not simply widen `_origin_ok` to accept anything when a tunnel is in use?* Because that is
  the check's entire purpose, and the class of bug it prevents was a patched Vite CVE, not a
  hypothetical. Naming the host costs one flag.
- *Why fold the GitHub call into `notes.py` rather than emit a `gh` line?* Two reasons, and the
  weaker one is convenience. The stronger one is that emitting the call means interpolating
  page-authored text into a command line.
- *Why keep the self-contained HTML rather than follow `scr` into a served SPA?* Because it is the
  only reason this artifact can be archived, attached to a ticket, or read on a phone with no
  server. `scr` traded that away for liveness, SSE, an in-viewer console, and a two-way agent loop.
  This plan buys the loop without paying that price.
- *Why no config file for the model tiers?* Because the agent already knows its roster, a file would
  go stale on the next model release, and a missing or wrong entry becomes a new failure mode for
  something the agent can just decide.
- *Why is the durable queue not optional?* Because without it the loop's failure mode is a silently
  dropped user action, which is indistinguishable from the tool ignoring them. Superseded: the
  queue this refers to is gone; see `PLAN-comments-and-no-serve.md`.

## Phase ordering, and what is optional

1 and 2 are correctness and go first: today a click can be silently discarded by GitHub, or refused
in a batch. 3 is what makes the page usable from a phone and is a contained change to one file.
4 and 5 are the loop and want to land together, since a queue with no wake channel is just a file
and a wake channel with one verb is what we already have. (3, 4 and 5 are superseded; served mode
they build on is gone, see `PLAN-comments-and-no-serve.md`.) 6 and 7 are prose-only, cost nothing, and
can land at any point, including first if the context churn is annoying sooner than the rest. 8 is
the largest and the least urgent, and it is worth doing only once the phases above have settled what
the contract would even expose.

Optional throughout: the escalating tick in Phase 5, and MCP in Phase 8. Everything else in 1 to 5
is load-bearing for the loop.
