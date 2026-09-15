# The coupling and symbol-delta graphs

Background for steps 2b2 and 2b3. Read it when a graph looks wrong, when deciding whether to
trust a `language: null` result, or when the fleet-wide runtime is the thing you're planning
around.

## A module is a directory, full stop

`layers.py` folds the whole repo into modules, and is the only one of the three 2b2 scripts that
sees the files the diff never touched, so it is the only one that can place a change against the
shape of the whole repo. A module is the file's own directory, at full depth, with only
source-root segments like `src` and `pkg` stripped. There is no depth dial to tune: in Go, Java,
Python and TypeScript the directory already is the package, so folding it to a fixed number of
segments replaces a fact with a guess.

There is also no architecture-layer grouping and no layer vocabulary. An earlier version guessed
layers by matching directory names against hexagonal and DDD words, and on a repo that names
directories by domain it put 9 of 10 modules in `unknown` while one accidental hit gave a
config directory a private `infrastructure` box, which read as an architecture that was not
there. So the module map answers "what depends on what, folded by directory", and
`section_notes` should say something about coupling or reach, never that the change respects or
violates a layering.

Fixed depth also hides real work. Truncating to two segments once put four related
subdirectories of the same feature area in one box, which was exactly the set a 45-file PR was
rearranging, so the module map showed nothing happening. Full directories cost more boxes, 275
against 102 across 1255 files on that repo, and the caps in each script plus scoping to touched
modules are what keep the picture readable.

Both `structure.py` and `coupling.py` treat `--base` as the merge base of the two refs, matching
`git diff base...head`, so a commit that landed on the base branch after the fork is never
attributed to this change.

## Never background the four scripts with `&` and `wait`

The Bash tool treats any command containing `&` as a background command: it stops waiting, hands
back a task id, and the call burns its whole timeout before the harness gives up on it. Measured
on a 1256-file Go repo, `&` plus `wait` sat for the full 600s after the scripts had already
finished their real work in 34 seconds, and detaching every file descriptor did not help, so
this is the tool's own detection rather than an inherited pipe. Run the four sequentially instead;
sequentially they cost under a minute together, which is noise next to `symdelta.py`.

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

`layers.py` walks every code file at one ref, so it is the slow one on a large repo. It reads
from the working tree when the ref is HEAD and the tree is clean, which is the common case and
much faster than a subprocess per file.

## `symdelta.py`: two tiers, and why it resolves calls instead of matching names

`structure.py`'s symbol edges are matched by name, not by resolving the call, and a name is
often not unique across a repo. Measured on a 1256-file Go repo, 591 of the 674 structure edges
had a target name that occurred at more than one path, and one collision drew an edge like
`billing.Error -> auth.String`, between two things that never call each other. `symdelta.py`
resolves calls with a real type checker instead, so when it returns a language the page shows
the symbol graph; when it returns `language: null` the page gets no graph at all for this diff, a
known and accepted gap, not a fallback to draw. A `null` is never licence to fall back to
name-matched edges; that guess is exactly the failure mode this graph exists to avoid.

Two tiers. Go uses a native extractor built on `go/packages`, extracting the whole repo, about 3
seconds on a 1255-file repo. Everything else goes over LSP `callHierarchy`, querying only the
symbols in the files the diff changed, in both directions. Each LSP language is a profile in
`extractors/lsp/extract.py`'s `LANGUAGES` table (server command, languageId, vendor directories,
retry budget, qualification strategy), selected with `--lang`:

| Language | Extensions | Server | Measured |
|---|---|---|---|
| Go | `.go` | `go/packages`, native | 4.6s on a 20-file diff |
| TypeScript | `.ts`, `.tsx` | `typescript-language-server` | 20.4s on a 27-file diff |
| Python | `.py` | `pyright-langserver` | 20.1s on a toy repo |
| Rust | `.rs` | `rust-analyzer` | 23.2s on a toy crate |

Extracting a whole repo is cheap with `go/packages` and far too slow over LSP, and it would be
wasted anyway: an edge between two symbols that both went unchanged cannot itself have changed.
So the LSP tier queries `outgoingCalls` for new calls the changed code makes, and
`incomingCalls` for new calls into it from code that did not change.

Each LSP language needs its server on PATH. Missing it, `symdelta.py` returns `language: null`
naming the missing tool rather than degrading.

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

## Cache location and runtime budget

`symdelta.py` builds and caches its extractor binary under `$XDG_CACHE_HOME/visual-diff` when
that is set, else `~/.cache/visual-diff`. Set `XDG_CACHE_HOME` yourself when `~/.cache` is
read-only in your sandbox; a build failure there surfaces as a build error, not a language
mismatch, and is easy to misread as one.

Budget for it: on a 1256-file Go repo this took 10m19s with the extractor binary already built
and cached, which was two thirds of a 15-minute run. The cost is not the build, it is
`go/packages` typechecking the whole module and its dependency tree once per ref. Start it
first, in its own call alongside the fan-out spawns, and expect it rather than any other step to
set the wall clock on a large repo.
