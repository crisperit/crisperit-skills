# Live mode setup

Run this when the `walkthrough_*` tools are missing, or when a live call or step reports a config
or backend remedy. `<skill>` is the code-walkthrough skill dir; every command below runs
`<skill>/scripts/cw_mcp.py`.

## 1. Write a config, if there isn't one yet

```bash
python3 <skill>/scripts/cw_mcp.py setup
```

Under a plugin install, the plugin's own manifest already registers the MCP server, so this
skips registration and only writes `~/.code-walkthrough/config.json` the first time it runs: the
claude-code template when `claude` is on PATH, otherwise the proxy template. It never overwrites
an existing config.

On a manual clone (or any other MCP-capable harness), register the server too. That writes
to the agent's user-level MCP config, so ask the user once before running it:

```bash
python3 <skill>/scripts/cw_mcp.py setup --agent claude   # Claude Code
python3 <skill>/scripts/cw_mcp.py setup --agent print     # any other harness, prints mcpServers JSON to paste
```

## 2. Fill in a backend, if the config still has placeholders

A freshly written proxy template points at placeholder `base_url`/`model` values. Ask the user
which backend to use (their own Claude Code again, an OpenAI-compatible proxy, or a local Ollama
or vLLM instance), then fill `profiles` and `roles` in the config path step 1 named. The config
keys are documented in `<skill>/references/live.md`; read that rather than guessing the shape.

## 3. Check it

```bash
python3 <skill>/scripts/cw_mcp.py check
```

Prints one `ok <role> (...)` or `FAIL <role>: <error>` line per role, each `FAIL` followed by its
own remedy line. Apply each remedy in turn (the same table lives in `<skill>/references/live.md`
under "Remedies"), then re-run `check` until every role passes.

## 4. Restart the daemon after any config or env change

```bash
python3 <skill>/scripts/cw_mcp.py stop
```

The daemon inherits its environment at spawn time, so a newly exported API key or an edited
`config.json` is invisible to it until this runs; the next tool call respawns it fresh.

## 5. Confirm the tools are live

If `walkthrough_start`/`walkthrough_get`/`walkthrough_list` are still not available in this
session, the harness needs a restart to pick up the registration from step 1. In Claude Code,
restart the agent and check `/mcp` for `code-walkthrough`.

Report one line per step: what it found, what it ran, and the result.
