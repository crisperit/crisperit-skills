# Live mode: the daemon, setup, cost, and remedies

Read this once before relying on live mode, and again whenever `walkthrough_start` or
`walkthrough_get` reports a `remedy` you don't recognise. SKILL.md's "1b. Live mode" carries the
calling convention; this file carries the mechanics behind it.

Live mode moves the capture, split, worker, gate and render steps to a daemon process, and the
worker conversations are no longer this agent's own subagent spawns: every diff hunk, every
prompt and every file a worker reads goes to whichever model backend `config.json` points at. A
`claude-code` role runs `claude -p` under the same account and routing your own Claude Code uses,
whether that's a subscription, an API key, or a gateway set in `settings.json`'s `env`, and counts
against that; the page banner shows its tokens only, with no `cost_usd`. An `openai` role instead
bills per token against that profile's own API key: only an Ollama or vLLM instance on the same
machine keeps that traffic local, a proxy, OpenRouter or any hosted API sees it the same as any
other client. `walkthrough_get`'s `usage` and the page banner show the running total either way,
with a `cost_usd` once an openai profile carries `price_per_mtok`.

## Setup

With `claude` on PATH, `python3 scripts/cw_mcp.py setup` writes a working claude-code config;
there is nothing to edit. Installing this skill as a Claude Code plugin registers the MCP server
automatically, but plugin users still run `setup` once to get this config; under the plugin,
`setup` just writes that config and skips registration, for either `--agent`. A manual clone (or any
other MCP-capable harness) needs one command instead:

```bash
python3 scripts/cw_mcp.py setup --agent claude    # registers with `claude mcp add -s user`
python3 scripts/cw_mcp.py setup --agent print      # prints the mcpServers JSON for any other harness
```

Either form also writes `~/.code-walkthrough/config.json` from a template the first time it
runs, the claude-code template when `claude` is on PATH, otherwise the proxy template below, and
never overwrites an existing one; paste a profile in instead.

## Config keys

```json
{"profiles": {
   "claude": {"kind": "claude-code", "model": "sonnet"},
   "proxy": {"base_url": "http://localhost:4000/v1", "model": "<a model your proxy routes>",
             "api_key_env": "LITELLM_API_KEY"},
   "local": {"base_url": "http://localhost:11434/v1", "model": "<an Ollama model with tools>"}
 },
 "roles": {"analysis": "claude", "prose": "claude", "ask": "claude", "escalate": "proxy"},
 "max_concurrency": 4, "timeout_s": 300, "max_conversation_tokens": 200000,
 "small_diff_lines": 1500, "batch_max_lines": 400}
```

A profile's `kind` is `openai` (the default; needs `base_url` and `model`) or `claude-code`
(needs only `model`, an alias or full name passed straight to `claude -p --model`). `timeout_s`
bounds each profile's call either way, HTTP request or claude process; `max_conversation_tokens`
and `price_per_mtok` apply to the openai kind only. `roles.escalate` is optional and falls back to
`roles.prose`; it is the fresh retry a batch gets after failing its fragment gate twice.
`roles.thread` is the optional role for thread turns and falls back to `roles.ask` when unset; its
usage is booked under the "thread" role. `small_diff_lines` and `batch_max_lines` mirror the fan-out thresholds in `static.md` step 2 and
step 2a. An openai profile may also carry `price_per_mtok: {"input": x, "output": y}`, which is what
turns a usage total into `cost_usd`; no price table ships, since one would go stale.

## `check`

```bash
python3 scripts/cw_mcp.py check
```

For an openai role, sends the profile one tiny request carrying one tool and asserts the reply is
a tool call, since tool-call support is the one capability every role depends on and small local
models often lack it. For a claude-code role it instead runs `claude auth status`, then one tiny
`claude -p` call with a schema. Prints `ok <role> (<profile>/<model>)` or `FAIL <role>: <error>`
with its remedy, and needs no daemon running.

## Workers on claude-code

A claude-code worker runs `claude -p --safe-mode --restricted` with only the `Read`, `Grep` and
`Glob` tools, cwd set to the walkthrough's `head` worktree; your own hooks, plugins, MCP servers
and `CLAUDE.md` are not loaded. Reads outside that worktree are denied. The live `head` worktree
is checked out with `core.symlinks=false`, so a worker reading a symlink sees a regular file
holding the link's target text instead.

Thread turns differ: they run `claude` without `--safe-mode`, since safe mode disables MCP servers,
but still with `--restricted`, the read-only `Read`, `Grep` and `Glob` tools, `--strict-mcp-config`
and a per-turn stdio outcome server exposing two tools, `propose_resolve` and `propose_page_edit`. `CLAUDE.md` and user
settings are verified not to load under `--restricted`. Each thread is one Claude Code session (first
turn `--session-id`, later turns `--resume`), stored in `~/.claude/projects` for the head checkout;
a lost session is reseeded from the thread's Q&A. Stop sends SIGTERM to the turn's process group
(SIGKILL after 3 s) and the session stays resumable. A `ctx/` folder in the walkthrough directory
holds copies of `analysis.json` and `raw.diff` the agent may read.

## Environment variables and `stop`

The daemon inherits its environment from the MCP process, which inherits the harness's, so an API
key exported after the daemon already started is invisible to it. Run `cw_mcp.py stop` after
exporting a new variable; the next tool call respawns the daemon with the refreshed environment.
A claude-code worker's process inherits that same environment minus the session-linkage variables
that identify the parent Claude Code session (`CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, and a few
others); everything else, including any `ANTHROPIC_*` variable, passes through unchanged.

## Store layout and pruning

Everything lives under `~/.code-walkthrough/` (override with `CODE_WALKTHROUGH_HOME`):
`server.json`, `server.lock` and `server.log` for the daemon itself, `config.json`, and one
`w/<repo-key>/<id>/` directory per walkthrough holding its `meta.json`, read-only `head/`
worktree, batches, drafts and rendered pages. The daemon prunes a walkthrough untouched for more
than 30 days (`CW_PRUNE_DAYS`), skipping any that still carries an unposted local draft.

## The `walkthrough/` prefix

A worker's `read_file`/`grep`/`list_dir` calls resolve against the repo's `head` worktree by
default. A path starting with `walkthrough/` (or exactly `walkthrough`) resolves against the
walkthrough's own directory instead, for the rare case a worker needs to re-read something it or
another worker already wrote there. This prefix is an openai-kind feature only: a claude-code
worker's `Read`/`Grep`/`Glob` tools stay rooted at the head worktree, with no equivalent escape.

## PR targets

A PR target runs through the daemon too (phase 3): `walkthrough_start` with `pr` set syncs the
PR's own comments and resolves its threads as part of the build, between `prepare` and `render`,
steps `comments` and `threads` (plus one `thread-<N>` per resolved thread needing a fresh
answer). `walkthrough_start` on an origin that isn't a GitHub remote still refuses with
`CWError("origin is not a GitHub remote", remedy="follow references/static.md of the code-walkthrough skill")`.

Every `gh` and `notes.py` call the daemon makes on a walkthrough's behalf runs with that
walkthrough's repo as cwd and with `GH_TOKEN`/`GITHUB_TOKEN` stripped from its environment
(`cw_run.gh_env`), so a shared daemon serving several repos or users never posts under whichever
identity its own process environment or working directory happens to carry.

## Ask and threads

Selecting text on a live page and clicking Ask starts a thread on the `thread` role (`roles.thread`,
falling back to `roles.ask`; see the remedy table if neither is set). The prompt
built from the selection, its surrounding hunk lines, the file's notes and the last few prior
answers is capped at 16000 characters; the selected quote itself is never truncated to make room.
Every question and answer is appended to that walkthrough's `qa.jsonl`, which is what repopulates
the page's Q&A list on reload and what the last-few-answers window above reads back from.

Replies stream into the thread as they are written. Stop interrupts a turn, and Retry reruns a turn
that ended in error or was stopped. A thread has a soft cap of 20 turns. A tab that reconnects
catches up from the server's in-flight buffers.

On a PR the agent may suggest resolving the GitHub review thread on the commented line. Keep adds it
to the pending resolves exactly like the Resolve conversation button, and Submit review still
previews and posts it. Dismiss tells the agent not to propose it again.

When a comment asks to change what the page shows, the agent may also add or replace a prose, list
or mermaid block next to the commented block. The edit applies at once, is local only (it never
reaches the PR), and is persisted in `qa.jsonl`. Undo reverts it and Redo reapplies it; the agent is
told about either on the next turn. Page edits are not offered on diff-line comments.

The agent may also draft a GitHub comment (`propose_github_draft`): a new review comment on the
commented diff line, or a reply in a review thread on those lines. A comment can be anchored to a
review thread directly (anchor `kind: "thread"` with the thread root's `note_id`); the prompt then
carries the thread's comments and diff hunk. A draft is a proposal stored in `qa.jsonl`, never in
`notes[]`: Keep makes it a local draft through the page's normal draft path, Edit and Use my words
change its text, Dismiss drops it, and nothing reaches GitHub from the draft itself. Outcomes of
this kind never enter the Post preview. The agent is told about a dismissal or an edit on the next
turn.

`POST /api/walkthrough/<key>/<id>/publish-one` publishes one local draft by itself, with
`{id, body_sha, oid?}` (the sha256 of the draft text, so a draft edited since the user looked is
refused), or resolves one thread with `{resolve, oid?}`. It carries the same guards as `/post`, 409s
while the walkthrough builds, in a sibling-revision view, when the parent of a reply is not yet on
GitHub, or when the viewer has a pending review, and makes exactly one REST call otherwise. A
preview taken before a publish 409s on `/post`, because the nonce covers the ready list.

## Posting

The comments panel on a live page replaces Copy gh command with a Post to GitHub button
(hidden under the same conditions Copy gh command would be: a PR, and the compared commit
pushed). Clicking it flushes pending drafts to the daemon, then previews what would be sent
-- every draft's `path:line` and body, every thread marked resolved locally -- in a confirm
dialog before anything reaches GitHub. The confirm dialog's checkbox, "Submit review as
COMMENT", is unchecked by default: unchecked, the comments land in a pending review only the
poster can see on GitHub until they submit it there themselves; checked, this submits the
review as a COMMENT immediately. Confirming re-sends the same preview request's `nonce`; if
anything changed since the preview (another tab posted first, a draft was edited), that 409s
and the dialog re-previews instead of posting a stale list. Per-item progress (one line per
note delivered or thread resolved) streams in over the same SSE connection as everything else,
and a successful post clears the locally pending-resolve marks for threads it resolved -- the
page itself reloads from the `rebuilt` event once the daemon re-renders.

## Remedies

Every error the daemon or client can raise carries a one-line remedy; this is the full table.

| Problem | Remedy |
|---|---|
| No model backend configured | run `cw_mcp.py setup`, fill `profiles` and `roles` in the config path it names, run `cw_mcp.py check`; this run continues on the static path |
| `<ref>` not in the local object store | `git fetch origin <ref>` |
| Store would sit inside the repo | set `CODE_WALKTHROUGH_HOME` outside the repo |
| `pr` set, origin isn't a GitHub remote | follow references/static.md of the code-walkthrough skill |
| `comments`/`threads` step failed | `gh auth login` (`GH_TOKEN`/`GITHUB_TOKEN` are not passed to `gh` here) |
| Ask selection too large | select a smaller range |
| No thread role | set `roles.thread` or `roles.ask` in `config.json` |
| The outcome server did not start | see `server.log`; check `python3` is on PATH and `cw_mcp.py` runs |
| Thread has 20 turns | start a new thread |
| Turn was interrupted | Retry the turn |
| Daemon didn't start | see the `server.log` path the error names |
| 401 / 403 from the model backend | set `<api_key_env>` in the shell that starts your agent, then `cw_mcp.py stop` |
| 404 from the model backend | check `model` for the named profile |
| 400, context length exceeded | this batch is too big for `<model>`: point the role at a larger-context model |
| Other 400 | message carries the first 300 characters of the response body; no fixed remedy |
| Connection refused | nothing listening at `<base_url>` |
| Socket timeout | raise `timeout_s` in `config.json`, or check `<base_url>` |
| Non-JSON reply, or no `choices` | protocol mismatch with the backend; no fixed remedy |
| Conversation over `max_conversation_tokens` | raise `max_conversation_tokens` in `config.json` |
| No tool call in a `check` reply | point this role at a model that supports tool calls |
| A walkthrough ended `failed`, any reason | re-run `/code-walkthrough` on the same target; passing fragments are reused |
| `claude` not on PATH | install Claude Code and log in, or point the role at an openai profile, then `cw_mcp.py stop` |
| `claude` not logged in | run `claude` in a terminal and log in (`claude auth login`) |
| Usage or rate limit from a claude-code call | wait for the reset the message names, or point the role at another profile |
| Prompt too long for a claude-code call | lower `batch_max_lines` |
| A claude process timed out | raise `timeout_s` in `config.json` |
| Worktree missing for a claude-code call | re-run `/code-walkthrough` on the same target |
| Other claude-code error | message carries the first 300 characters of `result` or stderr; no fixed remedy |

POSIX only: the daemon lock relies on `fcntl.flock`, so live mode does not run on Windows; use
the static path there.
