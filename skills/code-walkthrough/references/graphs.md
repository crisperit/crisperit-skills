# The symbol-delta graph

Background for `complexity.py` and `structure.py` (both run automatically by `pipeline.py`) and
for `symdelta.py` (SKILL.md step 2b3, still a manual call since it is the slow one). Read it when
a graph looks wrong, when deciding whether to trust a `language: null` result, or when the
fleet-wide runtime is the thing you're planning around.

Both scripts treat `--base` as the merge base of the two refs, matching `git diff base...head`,
so a commit that landed on the base branch after the fork is never attributed to this change.

## `complexity.py`: report the worst touched function, not a file total

`complexity.py` measures cyclomatic complexity per function at both refs, for the functions the
diff's own hunks land inside, and reports where the worst touched function now stands rather
than a per-file total. Summing a file's functions ranked files backwards on a measured commit,
scoring four new one-branch getters above the file holding a function at complexity 43. It also
reports that function's max nesting depth, where cyclomatic complexity is weakest, and only
shows it when it is 4 or more.

`walkthrough.py` states both in words rather than as a bare number, because a reader who is not
the author does not know what `4 cx +1` means. A file whose complexity this change moved reads
`NewRateLimiter 3→4 branches`; a new function that lands above the threshold reads
`resolveSource 11 branches`; deep nesting gets its own `nested 4 deep` chip. A pre-existing
function the change did not move gets no chip at all, since a static number on untouched code is
noise that outranked real findings on a measured run.

In explain mode `<base>` is the empty baseline, so every function has no before; the chip already
treats a function with no before as new either way, so it names the worst genuinely complex
function per file (`resolve_symbol_merges 27 branches`) instead of drawing a before/after arrow.
Nothing about `complexity.py` itself needs adjusting for this mode.

## `symdelta.py`: three tiers, and why it resolves calls instead of matching names

Never background this with `&` and `wait`: some agent shell tools (measured on Claude Code's
Bash tool) treat any command containing `&` as a background one, stop waiting, and burn their
whole timeout before the harness gives up on it. Measured on a 1256-file Go repo, `&` plus `wait`
sat for the full 600s after the script had already finished its real work in seconds. Run it as a
plain foreground call instead, on any harness.

An earlier version matched symbol edges by name rather than resolving the call, and a name is
often not unique across a repo. Measured on a 1256-file Go repo, 591 of 674 name-matched edges
had a target name that occurred at more than one path, and one collision drew an edge like
`billing.Error -> auth.String`, between two things that never call each other. `symdelta.py`
resolves calls with a real type checker instead, so when it returns a language the page shows
the symbol graph; when it returns `language: null` the page gets no graph at all for this diff, a
known and accepted gap, not a fallback to draw. A `null` is never licence to fall back to
name-matched edges; that guess is exactly the failure mode this graph exists to avoid.

There is a third, opt-in tier for exactly that gap: `--llm-head-edges` (and `--llm-base-edges`)
take a file of hand-supplied call edges, in the extractor's own wire shape, and feed them through
the same node-building, merge and preset pipeline as the other two tiers. This is not the
name-matching fallback the previous paragraph rules out. Name-matching is a mechanical guess that
produced `billing.Error -> auth.String` between two symbols that never call each other, with no
way for a reader to tell which edges to distrust. The LLM tier reads the actual call sites instead
of matching identifiers, and every page it appears on is stamped with a caveat naming it as
inferred, not compiler-resolved, so the reader can discount it deliberately rather than trust it
by default. It is still worse than a real graph: a wrong edge is worse than no edge, which is why
it is opt-in, asked for once per diff, and never the default when `language` is `null`.

When `<paths>` scopes the symbols graph (SKILL.md step 2b3), the graph keeps symbols under those
paths plus whatever their edges reach one hop out, not just the exact files named.

The two mechanical tiers resolve calls with real tooling, not a reader. Go uses a native
extractor built on `go/packages`, extracting the whole repo regardless of the diff's size: 2.65s
wall (11.05s user, 488% cpu) on a 1534-file repo, extractor binary alone, given a populated
`GOMODCACHE`. See "Cache location and runtime budget" below for why a full `symdelta.py` run costs
much more than that. Everything else goes over LSP `callHierarchy`, querying only the
symbols in the files the diff changed, in both directions. Each LSP language is a profile in
`extractors/lsp/extract.py`'s `LANGUAGES` table (server command, languageId, vendor directories,
retry budget, qualification strategy), selected with `--lang`:

| Language | Extensions | Server | Install | Measured |
|---|---|---|---|---|
| Go | `.go` | `go/packages`, native | comes with the Go toolchain | 2.65s on a 1534-file repo, whole-repo extraction, independent of diff size |
| TypeScript | `.ts`, `.tsx` | `typescript-language-server` | `npm i -g typescript typescript-language-server` | 20.4s on a 27-file diff |
| Python | `.py` | `pyright-langserver` | `npm i -g pyright` | 20.1s on a toy repo |
| Rust | `.rs` | `rust-analyzer` | `rustup component add rust-analyzer` | 23.2s on a toy crate |

`symdelta.py --doctor` checks all four on the machine it runs on: server present, callHierarchy
advertised, and the exact fix when either isn't -- no diff, base or head needed. Run it first
when a `language: null` result looks like a setup problem rather than an unsupported diff.

Extracting a whole repo is cheap with `go/packages` and far too slow over LSP, and it would be
wasted anyway: an edge between two symbols that both went unchanged cannot itself have changed.
So the LSP tier queries `outgoingCalls` for new calls the changed code makes, and
`incomingCalls` for new calls into it from code that did not change.

Each LSP language needs its server on PATH. Missing it, `symdelta.py` returns `language: null`
naming the missing tool rather than degrading. For typescript-language-server and pyright,
`check_language_server` checks npm's global bin dir before giving up: a real install that never
made it onto PATH gets a PATH fix as its remedy, not the `npm i -g` line above, which would
reinstall over the same binary and change nothing.

A hand probe that sends a bare `initialize` without declaring `textDocument.callHierarchy` in
its own capabilities gets `callHierarchyProvider: None` back even from a server that supports
call hierarchy fine, so that result alone is not evidence the server lacks it. `extract.py`'s
own `check_language_server` declares that capability before asking, so `extract.py <repo>
--lang <name>` is what actually settles whether a given server supports call hierarchy.

Qualifying a method to `Type.method` differs per server, and getting it wrong silently merges a
method with a same-named free function. For a symbol found via `documentSymbol` the container is
the parent node, uniformly. For a call target, where there is no tree, only
`typescript-language-server` puts the container in `detail`; pyright sends nothing and
rust-analyzer sends the signature. Python and Rust therefore use the `enclosing` strategy, which
re-queries `documentSymbol` for the target's own file (cached per file) and takes the innermost
type whose range contains it. Rust additionally normalises `impl Greeter` and
`impl SomeTrait for Greeter` down to `Greeter`.

rust-analyzer answers `documentSymbol` before `prepareCallHierarchy` is ready, so the readiness
loop waits on a non-empty `prepareCallHierarchy`, not on symbols. Measured: 2 retries on a toy
crate, against 0 for pyright.

Worktree prep is per language. TypeScript symlinks the main checkout's `node_modules` into both
worktrees instead of installing per ref. Python and Rust need nothing: pyright resolves
intra-repo imports from the files on disk, verified against a bare `git worktree add` with no
`.venv`. Rust consequently gives each worktree its own `target/` and double-indexes the crate
graph; sharing one via `CARGO_TARGET_DIR` is the untested optimisation if that proves too slow,
and it risks cargo lock contention.

A dependency-manifest change between the refs makes resolution differ per ref, which fabricates
new and gone edges, so each language bails out rather than guessing: `package.json` and the JS
lockfiles, `pyproject.toml`/`requirements.txt`/`poetry.lock`/`Pipfile.lock`/`uv.lock`, and
`Cargo.toml`/`Cargo.lock`.

A mixed-language diff picks the language with the most changed files, and says so in the
reason. This cuts deeper than reduced granularity on a mixed diff: the changed files in the
language it did not pick get no graph either.

A symbol node's optional `"range": [start, end]` (1-based, inclusive) comes only from the
extractor that produced its edges -- go/types for Go, `selectionRange`/`range` for the three LSP
languages -- never from grep or name matching. The LLM tier's edges carry no position fields by
schema, so its symbols get no `range`. The range is BASE-side when the node's state is `gone`,
HEAD-side otherwise, since that's the only ref the symbol is known to still exist on.

## `structure.py`: real symbols, not symdelta's own node state

symdelta.json's own node `state` answers "did the LSP see a call edge appear", not "did this
symbol exist on base", so a modified function that only gains a new caller comes back "new"
even though it was already there. `structure.py` answers existence itself instead: it parses
the file at both refs directly and diffs declared symbols -- classes, interfaces and top-level
functions/methods, never a local closure -- so its own `new`/`changed`/`moved`/`removed`/
`unchanged` states are trustworthy on their own, independent of whatever symdelta.json says.

It also needs `analysis.json`'s own `groups` to colour components by story stop, which is why
`pipeline.py prepare` runs it only after the gate has passed, never alongside the fan-out. It
still reads `symdelta.json` too, whatever that script's own `"language"` came back as, but only
for call edges between the components it finds; parsing the two refs at each language is its own
job, described below.

Two parse tiers, both stdlib-plus-one-dependency rather than a language server: TypeScript/JS
through a lazily-loaded tree-sitter grammar (bundled with graphify, loaded from its own venv when
no standalone install exists), Go through a tiny stdlib-only helper program under
`extractors/go/structure` (`go/parser` over one file's content via stdin, no `go/packages`, no
module resolution -- it never needs the repo's own dependencies to compile, unlike symdelta's Go
extractor). Missing the tree-sitter grammar, or the `go` toolchain, is the same `language: null`
outcome as symdelta's own missing-tool case, named in `"reason"`.

Call edges are symdelta.json's, rolled up from symbol to component level by matching each edge
endpoint's `(file, top-level name)` against what this script itself parsed; an edge with either
end unresolved (a nested closure, a symbol in a file this script didn't touch) is dropped rather
than guessed at. `implements`/`extends` targets are a class's own heritage clause names, not
component ids -- there is no cross-file type index here, so `sections.py`'s renderer resolves a
target to a component by name, first match wins on a collision, and drops it silently when
nothing in the capped set carries that name.

Every component whose file `is_test_path()` classifies as a test is dropped before the cap, not
after: a test file earns no slot in the 30-component budget at a real component's expense. Past
that, capped at 30 components (`MAX_COMPONENTS`): a round-robin by call-edge degree across each
`analysis.json` group keeps the busiest few from every theme rather than the first 30 found, and
`dropped` in the output says how many the cap itself cut (test files dropped earlier don't count
against that number).

A component that survives the cap with no call edge and no resolved implements/extends edge
moves into a top-level `also_touched` field (plain names, sorted), out of `components` -- it has
nothing to draw a box or a line for. Every remaining component carries `row`: its 0-based
position within its own `column`, from a 3-sweep barycenter ordering over call and
implements/extends edges (ported from the approved mockup's own `_barycenter_order`), so
`sections.py` can draw a column top-to-bottom in an order that keeps a caller close to its
nearest callee instead of alphabetical or arrival order.

## LSP tier guards

`extract_edges` carries two zero guards, mirroring the Go extractor's own check below, since a
language server can be running and answering requests while resolving nothing useful, which
looks identical on the wire to a diff with genuinely no call edges. It tallies how many files
returned a non-empty `documentSymbol` result and how many symbols returned a non-empty
`prepareCallHierarchy` result, and raises after the loop in exactly two cases.

Every file opened returned no `documentSymbol` result at all: the server is up but indexing
nothing, most often a wrong project root, a missing `tsconfig.json`/`pyproject.toml`, or a
server that never finished its cold-load warmup within the retry budget. The message names the
language, the server command, and how many files were opened and got nothing.

Every symbol that did get a `documentSymbol` result then exhausted `prepareCallHierarchy`'s own
retry budget with no item back: `documentSymbol` works, call hierarchy does not. Those retries
exist specifically to survive a server that answers `documentSymbol` before call hierarchy is
warm (rust-analyzer's `chq_ready` gate, above); running out of them for every symbol tried is a
real failure, not a slow warmup.

Neither guard fires when symbols and call hierarchy both resolve but the diff's changed symbols
call nothing new -- a normal, common result -- nor when `existing` (the diff's files that
actually exist at this ref) is itself empty, the ordinary base side of an all-new diff.

## Go workspaces

A repo whose root has a `go.work` (a multi-module workspace, one `go.mod` per submodule) needs
two extra things the plain single-module case does not.

`GOFLAGS=-mod=mod`, set to keep `go build`/`go/packages` from touching `go.sum` in a read-only
sandbox, is illegal once Go is in workspace mode: `go: -mod may only be set to readonly or vendor
when in workspace mode`. Both the extractor (`main.go`) and the process that launches it
(`run_extractor` in `symdelta.py`) check for `go.work` at the target root before setting it, and
skip it when present, since the environment `run_extractor` builds is what the extractor process
inherits.

The single `module` line in `root/go.mod` that used to gate which packages count as in-repo does
not exist at a workspace root, so that filter used to drop every package and emit an empty graph.
The extractor now reads `go.work`'s `use` directives instead, both the block form and the
single-line form, resolves each named directory's own `go.mod`, and treats a package as in-repo
if its path matches any of those module paths. A `use` entry with no readable `go.mod` is skipped,
not fatal, since a stale entry should not take down an otherwise-working run.

Both of those are why the extractor now also checks for zero: after visiting every loaded
package, if none passed the in-repo filter with type information resolved, it writes what it
loaded and the first few package errors to stderr and exits 1, instead of exiting 0 with an empty
graph and no explanation. `analyse_go` in `symdelta.py` turns that failure into `language: null`
with the reason, rather than letting it propagate and take the whole symbols section down with
no note at all.

## Cache location and runtime budget

`symdelta.py` builds and caches its extractor binary under `$XDG_CACHE_HOME/code-walkthrough`
when that is set, else `~/.cache/code-walkthrough`, else the temp dir when `~/.cache` is not
writable. That last fallback is what makes this work in an agent sandbox, which typically
allowlists writes to a handful of paths and `~/.cache` is not one of them; the cost is a rebuild
per boot rather than per machine. Point `XDG_CACHE_HOME` somewhere durable, or allowlist
`~/.cache/code-walkthrough`, to keep the binary for good. A build failure surfaces as a build
error, not a language mismatch, and is easy to misread as one.

Budget for it: `build_extractor` and `run_extractor` both point `GOMODCACHE` at
`CACHE_DIR/mod-cache`, a private module cache, rather than the developer's own warm one. Go needs
write access to its module cache, and in an agent sandbox the developer's `GOMODCACHE` is
typically not writable, the same reason `CACHE_DIR` itself falls back to the temp dir above. The
tradeoff that budget buys: a target repo's dependency tree is not already sitting in the private
cache, so it gets fetched into it during the run, once per ref. On a 1256-file Go repo, a full run
took 10m19s with the extractor binary already built and cached, two thirds of a 15-minute total.
That figure is the full run, dependency fetch included, not the extractor's own typechecking,
which given a populated module cache is seconds regardless of repo size (see the Go row above).
Start the full run first, in its own call alongside the fan-out spawns, and expect it rather than
any other step to set the wall clock on a large repo.
