# Live mode: the daemon, setup, cost, and remedies

Read this once before relying on live mode, and again whenever `walkthrough_start` or
`walkthrough_get` reports a `remedy` you don't recognise. SKILL.md's "1b. Live mode" carries the
calling convention; this file carries the mechanics behind it.

Live mode moves the capture, split, worker, gate and render steps a daemon process, and the
worker conversations are no longer this agent's own subagent spawns: every diff hunk, every
prompt and every file a worker reads goes to whichever model backend `config.json` points at.
Only an Ollama or vLLM instance on the same machine keeps that traffic local; a proxy, OpenRouter
or any hosted API sees it the same as any other client. Each call also bills per token against an
API key: a Claude Pro or Max subscription cannot drive this, and `walkthrough_get`'s `usage` and
the page banner show the running total, with a `cost_usd` once a profile carries
`price_per_mtok`.

## Setup

Installing this skill as a Claude Code plugin registers the MCP server automatically; nothing
else to run. A manual clone (or any other MCP-capable harness) needs one command instead:

```bash
python3 scripts/cw_mcp.py setup --agent claude    # registers with `claude mcp add -s user`
python3 scripts/cw_mcp.py setup --agent print      # prints the mcpServers JSON for any other harness
```

Either form also writes `~/.code-walkthrough/config.json` from a template the first time it
runs, and never overwrites an existing one.

## Config keys

```json
{"profiles": {
   "proxy": {"base_url": "http://localhost:4000/v1", "model": "<a model your proxy routes>",
             "api_key_env": "LITELLM_API_KEY"},
   "local": {"base_url": "http://localhost:11434/v1", "model": "<an Ollama model with tools>"}
 },
 "roles": {"analysis": "proxy", "prose": "proxy", "ask": "proxy", "escalate": "proxy"},
 "max_concurrency": 4, "timeout_s": 300, "max_conversation_tokens": 200000,
 "small_diff_lines": 1500, "batch_max_lines": 400}
```

`roles.escalate` is optional and falls back to `roles.prose`; it is the fresh retry a batch gets
after failing its fragment gate twice. `small_diff_lines` and `batch_max_lines` mirror the
fan-out thresholds in SKILL.md step 2 and step 2a. A profile may also carry
`price_per_mtok: {"input": x, "output": y}`, which is what turns a usage total into `cost_usd`;
no price table ships, since one would go stale.

## `check`

```bash
python3 scripts/cw_mcp.py check
```

Sends each configured role's profile one tiny request carrying one tool and asserts the reply is
a tool call, since tool-call support is the one capability every role depends on and small local
models often lack it. Prints `ok <role> (<profile>/<model>)` or `FAIL <role>: <error>` with its
remedy, and needs no daemon running.

## Environment variables and `stop`

The daemon inherits its environment from the MCP process, which inherits the harness's, so an API
key exported after the daemon already started is invisible to it. Run `cw_mcp.py stop` after
exporting a new variable; the next tool call respawns the daemon with the refreshed environment.

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
another worker already wrote there.

## PR targets

A PR target still uses the static, agent-driven path end to end: `walkthrough_start` refuses it
with `CWError("PR targets use the static path until phase 3", remedy="continue with step 2 of
SKILL.md")` until a later phase wires PR comment sync and thread resolution into the daemon.

## Remedies

Every error the daemon or client can raise carries a one-line remedy; this is the full table.

| Problem | Remedy |
|---|---|
| No model backend configured | run `cw_mcp.py setup`, fill `profiles` and `roles` in the config path it names, run `cw_mcp.py check`; this run continues on the static path |
| `<ref>` not in the local object store | `git fetch origin <ref>` |
| Store would sit inside the repo | set `CODE_WALKTHROUGH_HOME` outside the repo |
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

POSIX only: the daemon lock relies on `fcntl.flock`, so live mode does not run on Windows; use
the static path there.
