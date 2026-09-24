---
name: code-walkthrough
description: Use when the user wants a change or an area of code shown to them rather than searched: "visual diff", "visualize diff", "visualise diff", "visualize-diff", "visualize this PR", "visualise this branch", "visual recap", "review my changes visually", "show me what changed", "turn this PR into a review page", "graph the symbol changes in this PR", "review this with me", "walk me through this change", "walk me through this PR", "explain the auth flow", "explain how billing works", "how does X work", "walk me through the auth flow", "help me understand this codebase", "help me understand src/parser/", or /code-walkthrough; both the -ize and -ise spellings mean this skill. Turn a git diff, commit range, branch, GitHub PR, or an unchanged area of code into a self-contained local HTML walkthrough page: mermaid flow diagram, annotated diff walkthrough, per-line comments to hand back to the agent or post to the PR. Reviews or explains code that already exists, unlike a planning skill (plans work that does not exist yet) and unlike a code-review skill (hunts defects rather than recapping or visualizing).
---

# Code Walkthrough

Arguments: `$ARGUMENTS`, the diff target. Stay thin: capture the diff to a file and
hand the reading of it to subagents; the raw diff and the HTML template never enter your context.
The main loop never loads diff hunks, file contents, or per-hunk notes; it may load the verdict,
the group titles, and the gate result. The output is then assembled by `render.py`, so no
subagent writes markup. Every subagent
spawn passes an explicit `model` param; a built-in agent type with no pinned model would silently
inherit the expensive session model. Style rule for all prose: apply the no-ai-slop skill if
available, otherwise plain sentences, no dashes, no filler, no hedging.

## 1. Resolve the target

The target arrives as prose as often as a ref, so read the words:

| What the user said | Target |
|---|---|
| nothing | working tree plus staged changes, snapshotted as a second ref so it diffs as `git diff HEAD <snap>`, see below |
| "this PR", "my PR", "the PR", "this pull request" | the current branch's PR, see below |
| `123`, `#123`, a PR URL | that PR |
| "this branch", "my branch", "what this branch adds" | `<base>...HEAD` three-dot |
| `HEAD~3..HEAD`, `main...HEAD` | as given |
| "explain X", "how does X work", "walk me through the auth flow", "help me understand this codebase" | X as it stands, diffed against the empty baseline, see below |
| a bare path or directory, with no change described | ambiguous, see below |

"Explain X" and "walk me through the auth flow" name an area, not a ref: nothing downstream turns
prose into files on its own, so resolve it yourself first, see "Turning prose into a file list"
below. A bare path is genuinely ambiguous: it can mean "diff filtered to this path" (there is a
real change and the user is narrowing it) or "explain this path as it stands" (there is no change,
and the user wants it read cold). Tell the two apart from context: a branch ahead of its base,
staged changes, or the words "diff" or "changes" mean the first; a clean checkout of the base
branch, or "explain", "how does", "understand", "walk me through" attached to the path, mean the
second. When neither signal is there, ask the user in one line rather than guessing; guessing wrong
here means redoing the whole walkthrough.

### Turning prose into a file list

Nothing downstream reads prose, so "the auth flow" has to become paths before step 2 can start.
Keep it cheap: grep and glob the repo for the terms the ask names, skim the hits enough to tell
signal from noise, and propose the file list back to the user in one line, for example "That looks
like `src/auth/*.go` and `middleware/session.go`, 6 files. Use those?", before spending any
subagent budget on it. Do not write a new script for this: it is a grep-and-confirm pass, not a
search feature.

### Explain mode: diff against an empty baseline

"Explain X as it stands" names no second ref, so build one: an orphan commit wrapping the empty
tree. Every line in the named path then comes back as an addition and the machinery below runs
unchanged, one code path rather than two. That choice also sets the page's mode, `<explain>` in
step 3. Explain mode has no size ceiling of its own either, so it negotiates scope with the user
before spending.

Read `references/explain-mode.md` now, before step 2 writes anything: the baseline commands, the
three size bands that decide whether to proceed, mention the cost, or stop and offer cuts, and
what `--explain` changes downstream.

### Working tree target: a snapshot commit as the second ref

"nothing" names no second ref either: the working tree and the index are not commits. Give it
one with a snapshot, so `<head>` is the snapshot and `<base>` is `HEAD`, and every step
downstream that needs two refs runs instead of skipping.

```bash
idx=$(mktemp); cp "$(git rev-parse --git-path index)" "$idx"
GIT_INDEX_FILE=$idx git add -A --ignore-errors 2>/dev/null
snap=$(git commit-tree "$(GIT_INDEX_FILE=$idx git write-tree)" -p HEAD -m "code-walkthrough snapshot")
rm "$idx"
```

`--ignore-errors` matters in a sandbox, where a mounted dotfile can show up as an untracked
unsupported file type and would otherwise abort the add; the trailing `2>/dev/null` quiets that
same case but also hides a genuine staging failure, so if a snapshot ever looks incomplete, rerun
the `add` without the redirect to see why. The real index, working tree and every ref are
untouched: `snap` is a commit object nothing points at, so it never shows up in `git log` or `git
branch`. It sits as a loose object until a later `git gc` prunes it, not cleaned up the moment
this session ends. Step 2's `raw.diff` and numstat then come from `git diff HEAD <snap>` rather
than plain `git diff HEAD`, which also picks up untracked files that plain `git diff HEAD`
misses. The page's title and output slug still say "working", not the snapshot's sha; only
`<base>`/`<head>` downstream change.

Step 2c's links still skip themselves for this target: `snap` is never pushed, so `head_pushed`
comes back false the same way it does for any other unpushed head.

### "This PR", and three-dot over two-dot

"This PR" resolves through the current branch:

```bash
gh pr view --json number,baseRefName,headRefName --jq '[.number,.baseRefName,.headRefName]|@tsv'
```

Prefer diffing `<baseRefName>...HEAD` locally over `gh pr diff <n>`: it gives the local repo
path, base and head that `complexity.py`, `symdelta.py` and `links.py` all need, and it is the
same three-dot comparison GitHub shows. Fall back to `gh pr diff <n>` when the branch is not
checked out locally, and say in one line that the symbol-delta graph is skipped in that case. Bail
with one clear line when the branch has no PR yet.
Always prefer three-dot over two-dot: two-dot also shows commits that landed on the base branch
meanwhile, attributing other people's work to this change.

### Fetch both refs first, and diff the remote base

Fetch, then compare against the remote-tracking base, never the local branch:

```bash
git fetch origin <baseRefName> <headRefName>   # needs dangerouslyDisableSandbox
git diff origin/<baseRefName>...origin/<headRefName>
```

Then sanity-check the commit list before spending anything on it:

```bash
git log --oneline origin/<base>..origin/<head>
```

Every line should plausibly belong to this change. A commit carrying someone else's PR number,
or a merge-base that equals your local base tip while the log shows unrelated work, means the
base is stale and the diff is wrong. Why this matters and what it once cost:
`references/rationale.md`.

Bail with one clear line if the target resolves to an empty diff; an empty review page is worse
than a message. In explain mode this is what an empty diff means: a path with no matching files,
not "nothing changed", so the same bail applies, worded for that case.

## 2. Capture the diff to a file, never into context

Write it to the session scratchpad, never the git working tree (a stray review page there
pollutes commits and PRs). A branch, range or path target: `git diff <target> >
<scratchpad>/raw.diff`, stats from `git diff --numstat <target>`. The working tree target diffs
against its snapshot commit instead: `git diff HEAD <snap> > <scratchpad>/raw.diff`, stats from
`git diff --numstat HEAD <snap>`. A PR target: `gh pr diff <n> >
<scratchpad>/raw.diff`, stats from `gh pr view <n> --json files --jq '.files[] |
"\(.additions)\t\(.deletions)\t\(.path)"'`, the PR equivalent of numstat.

Pick the cheapest route that fits the diff; both produce the same
`<scratchpad>/analysis.json`, gated the same way in step 2b, and the diff is never read twice.

| Diff | Route |
|---|---|
| up to about 1200 lines and 6 files | one `general-purpose` subagent, on a stronger model (sonnet class) |
| bigger than either | fan out per batch, step 2a |

Delegating below the fan-out threshold costs no wall clock over doing it in the main thread, and
it costs the main context nothing; splitting it further into batches is where the cost shows up,
since a single pass finishes before parallel spawns have even started.

Start steps 2b2 and 2b3's two graph scripts the moment `raw.diff` exists, in the same message
that spawns the fan-out: two Bash calls alongside the spawns, not one per script. They read git
and the diff, never `analysis.json`. `symdelta.py` is the slow one on a large repo; running it
concurrently with the annotation agents keeps it off the critical path (`references/graphs.md`).

```json
{
  "target": "master...HEAD",
  "verdict": "one line for the facts strip: the change in a breath",
  "overview": "lead (1-2 sentences) + 3-5 one-line bullets of organizing ideas -- shape in references/fanout.md",
  "flow_mermaid": "flowchart LR ..., or \"\" when there is no flow worth drawing",
  "files": [
    {
      "path": "pkg/thing.py",
      "role": "one line: what this file's change is for",
      "hunks": [{"header": "@@ -12,7 +12,9 @@", "note": "one line on this hunk"}]
    }
  ],
  "groups": [
    {
      "title": "Hot-reload the rate-limit config",
      "why": "optional one line on why this theme reads here",
      "paths": ["pkg/thing.py"],
      "flow_mermaid": "optional, one small diagram for this group",
      "hop": "optional, 2-6 words naming the hand-off to the next stop",
      "side": "optional, true for a supporting group off the main line"
    }
  ]
}
```

This file is the whole of your judgment. Nothing downstream writes prose: `render.py` and
`walkthrough.py` place these strings and build every other part of the output from `raw.diff`.
So a field left blank is a section that will not appear, not a section someone else will fill in.

`overview` is the one heading the page shows in both modes, "Overview" whether the target is a
change or an area. It has two parts: a lead of one or two sentences saying what this is (or what
the change is) and who or what uses it, no file names in the lead, then three to five bullets, one
line each, carrying the organizing ideas only -- a bullet names a symbol or path only when that
name IS the idea, and most should name none. Nothing sits below that altitude: the detail a reader
wants next already lives in a group's `why` and in the hunk notes, not here, and reaching for
completeness at this level is exactly the failure mode this schema exists to prevent. Shape and a
worked example: `references/fanout.md`.

`groups` is the reading order, the one part of the walkthrough that needs you rather than the
diff: only someone who read the change knows which files belong to which theme and which theme a
reviewer should meet first. Write one group per theme, **in story order, not just reading
order** -- the list's own order is what the page's story map draws as stops one after another,
so a group earlier in the list reads as happening earlier in the story. Title it as the theme
rather than as a directory.

When there are two or more groups, the story map replaces the page's one top-level `flow_mermaid`
diagram (the story order already shows the shape a flow diagram would), so leave the top-level
`flow_mermaid` blank in that case. With a single group there is no map to draw, so the top-level
`flow_mermaid` still carries the page's one flow diagram exactly as before.

Two optional fields per group serve the map. `hop` is 2 to 6 words naming the hand-off to the
NEXT stop in the story (e.g. `"gated by \`defineTool\`"`), carrying one backticked identifier
that has to appear in `raw.diff`; the gate checks both. The last group that isn't a `side` group
has no next stop to name, so it must not carry a `hop`. `side: true` marks a supporting group
that sits off the main line -- docs, local dev setup, anything that doesn't advance the story --
and the map lists it separately rather than in the chain.

Keep the groups wide. Aim for three to five whatever the file count, and never more than six:
a group is a theme a reviewer holds in their head, not a stage in the data flow. Tracing the
change end to end and giving each hop its own group is the failure mode here, and it reads as
a pipeline diagram rather than a reading order. A group of one or two files almost always
belongs merged into the neighbour it feeds, and a test file belongs with the code it covers,
never in a group of its own. Under about five changed files, one group is the right answer.

You do **not** decide the order inside a group; `walkthrough.py`
derives that from `symdelta.json`, caller before callee, then by size, tests and generated files
last. Leave `groups` out entirely and the whole walkthrough becomes one such group. A file you
forget still renders in a trailing "Everything else" group, so a partial grouping is safe to
ship. The gate rejects a group with no title, a path not in the diff, or a file in two groups.

A group's own `flow_mermaid` is optional: one small diagram for that group, `sequenceDiagram` or
`flowchart LR`, your pick per group. Default to a sequence; fall back to a flowchart only when
the group genuinely has no order to show, a theme like error handling or config plumbing where
participants and an ordered exchange would have to be invented. Keep it to about 8 steps at most,
and omit the field entirely rather than draw something you had to guess. Same label rules as the
top-level `flow_mermaid` below.

**Mark identifiers with backticks** in `verdict`, `overview`,
every `role` and every `note`: paths, function/method/type/class names, config
keys, metric names, literal values. Markdown passes them through as inline code; the HTML
renderer promotes them to `<code>` after escaping. In `overview` specifically, most sentences
should carry no backticked symbol at all -- density, not the markup, is what produced the wall
this schema exists to prevent.

**`flow_mermaid` node labels are 2 to 6 words naming the step**, not a sentence explaining it;
the reasoning belongs in `overview`. **An edge carries a label only when the arrow itself is
the action or transition**, 1 to 4 words, verb-led; a plain sequential step needs none. A
group's own `flow_mermaid` follows the same two rules.

One entry per file in `raw.diff`, one `hunks` entry per `@@` hunk in that file, every `role`
non-blank. A hunk's `note` explains the code, not the diff, and is often correctly left blank
when the lines say it on their face. Full note-writing rules and bad/good pairs:
`references/fanout.md`.

`verdict` is the prose agent's, alongside `overview`: a batch agent sees one slice and cannot
write it, so it waits for the one subagent that sees the whole change. On the fan-out route
`fanout.py merge` carries it through from `prose.json`; on the small-diff route the one
subagent that writes step 2's whole `analysis.json` writes it directly. Missing it does not
fail the gate, but it costs the facts strip.

## 2a. Fan out, only for a big diff

Split the diff so a fast cheap model can do the per-hunk noticing in parallel, then have one
better model write the prose that needs the whole picture.

```bash
python3 <skill>/scripts/fanout.py split --diff <scratchpad>/raw.diff \
  --out <scratchpad>/batches
```

1. Spawn one subagent per batch, all in a single message, each on a sonnet-class model: the job
   is per-hunk noticing inside one slice, and the gate checks every answer. Each fills only
   `role` and `note` in its own pre-seeded fragment.
2. Then one subagent on a stronger model (sonnet class), since it needs the whole change in view
   and nothing downstream can check its judgment, writes `<scratchpad>/prose.json` with `target`,
   `overview` and `verdict`, from the fragments and the numstat. Under
   about 2000 lines, also hand it `<scratchpad>/raw.diff` and tell it to read it. It never
   writes `flow_mermaid`: that field belongs to whichever group ends up alone once step 2b
   decides the groups. Full brief: `references/fanout.md`.
3. Merge:

   ```bash
   python3 <skill>/scripts/fanout.py merge --diff <scratchpad>/raw.diff \
     --prose <scratchpad>/prose.json --fragments <scratchpad>/batches/fragment-[0-9].json \
     --out <scratchpad>/analysis.json
   ```

Full subagent brief, the role and note rules with their bad/good pairs, and what the merge
canonicalizes: `references/fanout.md`.

## 2a-regen. Skip what has not changed, optional

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
under `analysers.<script>` (steps 2b2, 2b3 and 2c) reports `"hit"` or `"miss"`: on a hit, copy
`cache_path` to that script's usual output path instead of running it; on a miss, run it as
normal and also copy the fresh output to `cache_path`, so the next run gets the hit.
`dirty: true` means HEAD moved since `--prior-state` was built, the usual reason to skip this
step and regenerate from scratch instead.

## 2b. Gate the analysis

After step 2a's merge, spawn one subagent on a stronger model (sonnet class), since it needs the
whole change in view and nothing downstream can check its judgment, with the merged `(path,
role)` pairs plus the graph summaries; it returns `groups`, in story order, for you to write
into `analysis.json` and review rather than author.

Tell it that each group it writes may also carry `hop` and `side`, same rules as described
above under `groups`, and its own optional `flow_mermaid`. When it settles on two or more
groups, tell it to leave the top-level `flow_mermaid` blank -- the story map replaces it; when
it settles on exactly one group, it writes that one group's `flow_mermaid` (if any) as the
top-level `flow_mermaid` too, since there is no map to carry it instead.

```bash
python3 <skill>/scripts/validate_analysis.py --diff <scratchpad>/raw.diff \
  --analysis <scratchpad>/analysis.json
```

It prints one plain line per problem and exits non-zero: a file present in the diff but missing
from `files[]`, a blank `role`, an invented file or hunk, and a filled-in `note` that shares no
identifier with that hunk's own changed lines. It also fails when more than 80% of the diff's
hunks have an empty note (`EMPTY_NOTE_FLOOR`). Do not proceed to step 3 until it exits 0. Never
render an analysis that has not passed.

Send the gate's problem lines to a subagent to fix, never patch `analysis.json` yourself: the
gate output already names the path and the field, so nothing else needs to travel. Under about
ten lines, one subagent working from just those lines can patch it directly. Above that, or when
a whole file entry is missing, route the lines to the batch and fragment that produced them
instead, since fixing it needs more context than the gate line alone gives, and respawn that
batch on a stronger model (sonnet class) if it fails twice. Full routing, the identifier-overlap
check, and the empty-note floor's reasoning: `references/fanout.md`.

Two things the gate does not check, so check them yourself: a hunk entry the fragment appended
for a deleted file's `hunks` list, and the `verdict` key.

## 2b2. Build the complexity chart

Every target names two refs by this point, a working-tree target's via the snapshot commit from
step 1, so this step always runs.

```bash
python3 <skill>/scripts/complexity.py --repo . --base <base> --head <head> \
  --diff <scratchpad>/raw.diff > <scratchpad>/complexity.json
```

Reports where the worst touched function now stands, not a per-file total; see
`references/graphs.md` for why. In explain mode `<base>` is the empty baseline, so every function
has no before; the chip already treats a function with no before as new either way, so it names
the worst genuinely complex function per file (`resolve_symbol_merges 27 branches`) instead of
drawing a before/after arrow. Nothing here needs adjusting for this mode.

## 2b3. Build the symbol-delta graph

The only graph either output carries besides the flow diagram. Same as 2b2: every target names
two refs by now, including a working-tree target via its snapshot commit. Issue this as its own
Bash call, `dangerouslyDisableSandbox:
true`, in the same message as the fan-out spawns and 2b2's call. In explain mode `<base>` is the
empty baseline: `resolve_base` already falls back to the base itself when three-dot's merge-base
does not exist, so this runs two-dot under the hood without any separate handling.

```bash
python3 <skill>/scripts/symdelta.py --repo . --base <base> --head <head> > <scratchpad>/symdelta.json
```

Check `"language"` in the output before spending anything further on this step: `null` means the
diff touched no supported language, or is missing a required tool, with why in `"reason"`.
`sections.py` puts that on the page itself now -- a short note where the graph would have been,
reason and remedy included -- so there is no separate line to relay by hand. When the result
carries a `"remedy"`, it is a command that fixes the local tooling, and it still needs asking
before you run it: `npm ci` rebuilds the user's node_modules and a `-g` install mutates their
machine. Tell the user the problem and the exact command in one line, ask once, then re-run
symdelta.py and continue. `references/graphs.md` has each language server's install command and
the `--doctor` command for checking a machine's setup, so neither is restated here. A `null` is
never licence to fall back to name-matched edges; see `references/graphs.md` for why that guess
is exactly the failure mode this graph exists to avoid.

When `"language"` is `null`, there is a second option beyond fixing the local tooling: offer to
build the graph yourself by reading the code instead of the compiler doing it. This is one ask,
not automatic, and the page will label whatever it draws as inferred rather than compiler-resolved
so the reader can weigh it accordingly. If the user agrees, write one JSON object per line to a
scratch file, in the extractor's own wire shape (`FromFile`, `FromSym`, `ToFile`, `ToSym`, repo-
relative paths, `Type.method` for a method), then re-run symdelta.py against it:

```bash
python3 <skill>/scripts/symdelta.py --repo . --base <base> --head <head> \
  --llm-head-edges <scratchpad>/llm-edges-head.jsonl > <scratchpad>/symdelta.json
```

This is still not licence to name-match; the LLM tier reads the actual call sites, it does not
guess from identifier names.

```bash
python3 <skill>/scripts/sections.py --kind symbols --data <scratchpad>/symdelta.json \
  --format html <explain> [--paths <paths>] > <scratchpad>/section-symbols.html
```

Shows packages only at this, the page level: a symbols level laid out across a whole page is
unreadable on a big diff, so that detail now lives per group instead, drawn inline by
`walkthrough.py` in step 2d (with its own toggle between packages and symbols, scoped to that
group's files). Pass this file to `render.py`'s `--symbols <path>`; it inserts nothing when the
file is empty or the flag is omitted.

`<paths>` is the files and directories the page is about, the same pathspec the diff used, space
separated. The graph keeps symbols under those paths plus whatever their edges reach one hop out.
**Required in explain mode**: the empty baseline makes every symbol in the repository new, so
without it a page explaining one package draws the entire tree (measured: 3909 nodes, 5905 edges,
611 KB of mermaid). Omit it when the diff is a real change, where the delta is already the scope.
Explaining a whole repository is a legitimate `<paths>` of `.`; the section then opens on the
packages level and says so in its note, with the symbols level one click away.

Supported: Go (`.go`, native `go/packages`), TypeScript (`.ts`, `.tsx`), Python (`.py`) and Rust
(`.rs`), the last three over LSP and each needing its server on PATH
(`typescript-language-server`, `pyright-langserver`, `rust-analyzer`). A mixed diff picks the
language with the most changed files and says so in `"reason"`.

Set `XDG_CACHE_HOME` yourself if `~/.cache` is read-only in your sandbox; `symdelta.py` builds
and caches its extractor binary there. The per-language profiles, the method-qualification rules
that differ per server, the name-collision problem this graph avoids, and the runtime budget on
a large repo: `references/graphs.md`.

## 2b4. Build the structure view

Unlike 2b2 and 2b3, this one needs `analysis.json`'s own `groups` to colour components by story
stop, so run it after step 2b's gate has passed, not alongside the fan-out. It still needs
`<scratchpad>/symdelta.json` from 2b3, whatever that script's own `"language"` came back as:
structure.py parses the two refs itself (tree-sitter for TypeScript/JS, a tiny stdlib-only Go
helper for Go) and only reads symdelta.json for call edges between the components it finds.

```bash
python3 <skill>/scripts/structure.py --repo . --base <base> --head <head> \
  --symdelta <scratchpad>/symdelta.json --analysis <scratchpad>/analysis.json \
  [--paths <paths>] --out <scratchpad>/structure.json

python3 <skill>/scripts/sections.py --kind structure --data <scratchpad>/structure.json \
  --format html <explain> > <scratchpad>/section-structure.html
```

`"language"` in `structure.json` is `null` when the diff touched no supported language
(TypeScript/JS or Go) or the ref pair carries a dependency-manifest change neither language's
parser can safely diff across; `sections.py` puts the reason on the page as a short note, the
same as symdelta's own `null`. Otherwise the page gets a column layout of the real
classes/interfaces/functions the diff touched -- caller-first, left to right, like a group's own
call-graph tab -- coloured by story group, capped at 30 components (`dropped` in the output says
how many were cut to fit). `<paths>` is the same pathspec 2b3's symbols graph uses: against a
real ref pair `structure.py`'s own `git diff` is already bounded to the changed files, but
explain mode's empty baseline makes every file in the repo count as changed, so pass it there
too. Omit it for a real diff, where the delta is already the scope. Pass the rendered file to
`render.py`'s `--structure <path>`; it lands between the story map and the walkthrough.

## 2c. Build the links

```bash
python3 <skill>/scripts/links.py --repo <path> --diff <scratchpad>/raw.diff \
  --head <head ref> [--pr <number>] > <scratchpad>/links.json
```

One JSON object: `repo_url`, `pr_url`, `head_sha`, `head_pushed`, and `files[]` with a `diff_url`
(the file's anchor in the PR's Files changed tab), a `blob_url` (permalink at the head commit)
and per-hunk `url`s (that hunk's new-side line range on the blob page).

Always pass `--pr <number>` when the branch has a PR at all, whatever the target was: a link into
the Files changed tab is where a reviewer wants to land, a blob permalink is the fallback for a
branch with no PR. The number comes from step 1's `gh pr view` call. Explain mode never has a PR,
so omit `--pr` entirely; `diff_url` comes back empty for every file and the blob permalink at
`<head>` is the only link, the same branch-with-no-PR fallback described above.

Skip this step, and every link, when `head_pushed` is false or `repo_url` is empty: an unpushed
commit or a repo with no web remote produces links that 404. Nothing here needs the network.

## 2d. Build the walkthrough

Generated, not written: `analysis.json` supplies each file's `role`, each hunk's `note`, and the
`groups` that set the reading order; the script supplies the paths, counts, bars, nesting,
fences, escaping, and the order inside each group. A group's own `flow_mermaid`, when it has one,
renders inline right after its `why` paragraph.

```bash
python3 <skill>/scripts/walkthrough.py --analysis <scratchpad>/analysis.json \
  --diff <scratchpad>/raw.diff --format html --symdelta <scratchpad>/symdelta.json \
  --complexity <scratchpad>/complexity.json <explain> \
  > <scratchpad>/section-walkthrough.html
```

`--symdelta` orders each group caller-first and, past one group, gives each its own call graph
scoped to its own files, as a `call graph` tab beside `flow` when the group has both (just the
one diagram, no tabs, when it has only one). `--complexity` adds the complexity chip,
`--diff`'s rename headers let a moved file show its old path instead of reading as a new
addition. All optional; pass them when the files exist. It carries every hunk body, no size
budget, since per-line commenting needs the lines, and cannot omit a file even when
`analysis.json` did.

Why there is no per-line moved-block marker: `references/rationale.md`.

## 2e. Build the state document

`state.json` is the one document the page renders from: notes, their GitHub lifecycle,
per-hunk hashes, staleness. Build it once the walkthrough (and links, when step 2c ran) exist:

```bash
python3 <skill>/scripts/state.py --analysis <scratchpad>/analysis.json --diff <scratchpad>/raw.diff \
  [--links <scratchpad>/links.json] [--prior <scratchpad>/state.json] --out <scratchpad>/state.json
```

Pass `--prior` when a `state.json` from an earlier run on this same target already sits at that
scratchpad path, so its `notes[]` carry forward instead of being lost.

## 2f. Sync existing PR comments, only when the diffed target is a GitHub PR

Pull the PR's own review comments in before rendering, or the page shows zero threads even when
the PR already has them: a `notes.py sync` pass over the REST comments, then an additive
`sync-threads` pass over the GraphQL review threads, since the REST payload carries no thread
node id and resolving one needs it.

Both commands, which fields are trusted, how an outdated comment is kept, and how a reply finds
its parent: `references/pr-comments.md`.

## 2g. Resolve threads, only when the diffed target is a GitHub PR

For each thread `sync-threads` just marked resolved, work out what closed it: conversation, a
deferred ticket, or a commit, and for a commit, which hunks and why. Unlike 2a-regen just
above, this step is **not** droppable. It produces the page's primary content for a resolved
thread, not a cache optimisation, so it runs on a first review too, and the only thing that
skips it is step 1 below coming back with no threads at all.

1. **List the resolved threads.** `resolved-threads` wants the exact `last_comment_id` from
   GraphQL, not the notes-based fallback, so run the same `REVIEW_THREADS_QUERY` call step 2f
   already ran (`references/pr-comments.md`) a second time, this time saved to a file instead
   of piped straight into `sync-threads`:

   ```bash
   gh api graphql -f query='<REVIEW_THREADS_QUERY>' \
     -F owner=<owner> -F repo=<repo> -F number=<n> > <scratchpad>/raw-graphql.json

   python3 <skill>/scripts/notes.py resolved-threads --state <scratchpad>/state.json \
     --payload <scratchpad>/raw-graphql.json --out <scratchpad>/threads.json
   ```

   Stop here when `threads.json`'s `threads` list is empty. Nothing below has anything to do.

2. **Build the commit index**, contract (B), from `git log` over the range step 1 already
   fetched, never the GitHub API: the scripts are no-network by convention and the repo is
   already local. Capture each commit's own diff in the same pass, keyed by sha, so a
   thread whose window turns out small enough gets answered in one worker call instead of
   two:

   Two-dot, not three-dot: `git log`'s `...` is a symmetric difference, so it would pull in
   commits that landed on `<base>` after the branch point too, attributing someone else's
   work to this thread's resolution. `..` keeps it to commits unique to `<head>`.

   ```bash
   git log --format='%x02%H%x09%s%x09%cI%x09%an' --numstat \
     origin/<base>..origin/<head> > <scratchpad>/commit-log.raw

   python3 - <<'PY'
   import json, subprocess
   from pathlib import Path

   raw = Path("<scratchpad>/commit-log.raw").read_text()
   commits, diffs = [], {}
   for block in raw.split("\x02")[1:]:
       header, _, rest = block.partition("\n")
       sha, subject, committed_at, author = header.split("\t")
       files = []
       for line in rest.strip("\n").splitlines():
           if not line.strip():
               continue
           added, removed, path = line.split("\t", 2)
           files.append({
               "path": path,
               "additions": int(added) if added != "-" else 0,
               "deletions": int(removed) if removed != "-" else 0,
           })
       commits.append({"sha": sha, "subject": subject, "committed_at": committed_at,
                        "author": author, "files": files})
       diffs[sha] = subprocess.run(["git", "show", "--format=", sha], cwd="<repo>",
                                    capture_output=True, text=True).stdout

   Path("<scratchpad>/commit-index.json").write_text(
       json.dumps({"commits": commits}, indent=2) + "\n")
   Path("<scratchpad>/diffs.json").write_text(json.dumps(diffs, indent=2) + "\n")
   PY
   ```

3. **Plan the cache**, a second `regen.py` invocation, `--threads` this time instead of
   `--manifest`, against the same `--cache-dir` step 2a-regen uses:

   ```bash
   mkdir -p <scratchpad>/cache
   python3 <skill>/scripts/regen.py --diff <scratchpad>/raw.diff --repo <repo> \
     --base <base> --head <head> --cache-dir <scratchpad>/cache \
     --threads <scratchpad>/threads.json > <scratchpad>/resolution-plan.json
   ```

   Read `plan["resolution"]`: a `"cached": true` entry already has its answer sitting at
   `cache_path_hit` and needs no worker. A `"cached": false` entry is a miss and goes to the
   split below.

4. **Split, then spawn one worker per miss, in parallel**, exactly the way step 2a spawns one
   subagent per batch:

   ```bash
   python3 <skill>/scripts/fanout_threads.py split --threads <scratchpad>/threads.json \
     --commits <scratchpad>/commit-index.json --plan <scratchpad>/resolution-plan.json \
     --diffs <scratchpad>/diffs.json --out <scratchpad>/resolutions
   ```

   This writes one `thread-N.seed.json` per miss and prints a manifest naming each thread's
   mode: `cached` (already handled in step 3, no seed written), `inline` (the window's diffs
   already sit in the seed, one call finishes it), `two-pass` (the window was too big to
   pre-fetch, or wasn't covered, so the seed's `diffs` is empty) or `invalidated` (step 3
   reported a cache hit, but the commits it names are gone from the window, a force-push in
   practice, so it was re-seeded and must be re-run). Spawn every `inline`, `two-pass` and
   `invalidated` seed's worker in one message, each on a sonnet-class model, with the prompt
   below. A cheaper model is tempting here because the unit is small, but the judgment is not:
   deciding that a commit does NOT answer a comment is the whole value of the step, and a weaker
   model either forces a match or declines one it should have made. A worker that cannot finish because it needs a diff its seed does not carry
   writes `thread-N.needs.json` instead of `thread-N.json`, naming the shas it picked; fetch
   exactly those with `git show`, add them to that seed's `diffs` map, and spawn that one
   worker again with the same prompt. It now has what it needs and writes the real
   `thread-N.json`.

5. **Cache, then merge, then apply.** Before merging, copy every fresh answer into its cache
   slot from step 3's plan: `cache_path_positive` for any outcome other than `none`
   (conversation, deferred, and commits are all permanent answers), `cache_path_null` for
   outcome `none` (worth retrying once a new commit lands, so it is not permanent). A
   `cached: true` thread from step 3 needs no copy, its answer is already there. An
   `invalidated` thread is a fresh answer like any other, copy it too, overwriting the stale
   slot it just replaced.

   Collect fragment paths from the split manifest rather than globbing `thread-*.json`: that
   pattern also matches `thread-N.seed.json`. For each `cached` entry, copy its
   `cache_path_hit` file's contents verbatim into a fragment path of your own naming, its
   shape is already contract D. Every other entry's fragment is its `seed` path with
   `.seed.json` swapped for `.json`, the file that thread's worker wrote.

   ```bash
   python3 <skill>/scripts/fanout_threads.py merge --threads <scratchpad>/threads.json \
     --commits <scratchpad>/commit-index.json \
     --fragments <the fragment paths collected above> --out <scratchpad>/resolutions.json

   python3 -c "import json; d=json.load(open('<scratchpad>/resolutions.json')); \
     print(json.dumps(d['resolutions']))" | \
     python3 <skill>/scripts/notes.py apply-resolutions --state <scratchpad>/state.json
   ```

   `merge`'s output nests every resolution under a `"resolutions"` key; `apply-resolutions`
   reads a flat `{thread_id: resolution}` map from stdin, hence the unwrap.

### The per-thread worker prompt

Hand the worker `<seed path>` and this:

```
Read <seed path>. "thread" is the comment and its replies that make up one resolved GitHub
review thread: path, line, the root comment's body, and every reply in order. "commits" is
every commit on this PR from the thread's first comment onward, each with its subject,
author, and files touched, but no diff text of its own. "diffs" maps a commit sha to its
unified diff text for whichever commits are already fetched; it may be empty.

1. Gate. Decide how this thread actually ended, from the thread alone:
   - conversation: a reply explained, argued, or agreed, and that settled it. No commit
     needed. Take the reply that did the settling as closing_message.
   - deferred: punted to a ticket or a later pass. Take the ticket URL if a reply names one,
     otherwise null.
   - commits: a code change is what closed it.
   - none: none of the above is clear from the thread. This is a real answer, not something
     to avoid. Guessing a commit you cannot support is worse than saying you found nothing.
   For conversation, deferred, and none, stop here. Write thread-N.json now, in the shape
   below, with commits: [] and files: []. Nothing below applies.

2. Match, only when the gate said commits. Pick every sha in "commits" that plausibly closed
   this thread. Plural is normal: one push can answer several comments, and "extract this" or
   "refactor this" can span several commits and files. Weigh, but do not filter on: the
   commit's author matching the thread's resolved_by, the commit touching the thread's path
   with files or hunks near its line, and the subject mentioning review, the file, or the
   symbol the comment names. The path is a signal, not a requirement: a comment on one file is
   sometimes answered entirely in others. If nothing you can find genuinely reads as the fix,
   write outcome: none instead of forcing a match.

3. Explain, only once you have diffs for every sha you picked in step 2. If "diffs" already
   covers all of them, continue below. If it does not, you cannot finish this pass: write
   thread-N.needs.json, {"thread_id": "<id>", "need_diffs_for": [sha, ...]}, and stop. The
   driver fetches those and runs you again with the same seed, this time carrying them.
   Once every diff is in hand, name the files and hunks that actually answer the comment, not
   every file the commit touched: a nine-file refactor may answer one comment in three of
   them. Copy those hunks verbatim. Write one sentence on what the change actually did, in
   the code's own terms. Do not restate the comment: the reader has it open directly above
   your answer. Write null when the diff says it on its face, which is common for a one-line
   mechanical change.

Write thread-N.json:
{"thread_id": "<id>", "outcome": "conversation"|"deferred"|"commits"|"none",
 "closing_message": <string or null, only for conversation>,
 "ticket": <string or null, only for deferred>,
 "commits": [sha, ...],
 "files": [{"path": "...", "hunks": ["@@ ...", ...]}],
 "why": <one sentence, or null when the diff says it on its face>,
 "confidence": "high"|"medium"|"low"}

confidence is your certainty in the match, not the gate: high when the ranking signals agree,
low when you picked a sha on one weak signal alone.
```

## 3. Build the output

No subagent renders it. `render.py` builds the page, and the slug comes from the diffed target
(branch name, PR number, or "working").

```bash
python3 <skill>/scripts/render.py --analysis <scratchpad>/analysis.json \
  --diff <scratchpad>/raw.diff --format html \
  --template <skill>/assets/diff-review-template.html \
  --walkthrough <scratchpad>/section-walkthrough.html \
  --state <scratchpad>/state.json \
  [--symbols <scratchpad>/section-symbols.html] \
  [--structure <scratchpad>/section-structure.html] \
  [--links <scratchpad>/links.json] [--title "Code walkthrough: <slug>"] <explain> \
  > <scratchpad>/code-walkthrough-<slug>.html
```

Omit `--symbols` when 2b3 did not run, `--structure` when 2b4 did not run, `--links` when 2c did
not run -- a `language: null` note still counts as having run, since the section file it
produced is not empty. An empty section file inserts nothing, not even its heading. `render.py`
derives the story map from `analysis.json`'s own `groups` -- nothing new to pass here -- and
swaps it in for the top-level flow diagram once there are two or more; `--structure`, when
passed, lands between the story map and the walkthrough; `--symbols`, when passed, lands inside
a collapsed `<details>` at the very end of the page instead of up near Flow.

### `<explain>`

Resolve it like `<skill>` and `<scratchpad>`, not like an optional extra: every renderer above
carries it, and all three have to agree. It is `--explain` when step 1 read the ask as "explain X
as it stands" (the empty-baseline row of the target table), and empty in every other mode.

Do not re-derive the mode down here from the diff's shape. The empty baseline makes everything an
addition, but so does a real change that only adds files, and those two want different pages: one
is code being explained, the other is code being reviewed. Only the ask tells them apart. What
`--explain` changes in the rendered page: `references/explain-mode.md`.

`references/builder.md` is background on why the output is shaped the way it is, not
instructions to follow.

## 3b. Gate the rendered output

```bash
python3 <skill>/scripts/validate_analysis.py --diff <scratchpad>/raw.diff \
  --analysis <scratchpad>/analysis.json --rendered <the .html just written> \
  --sections <the section-*.html files>
```

It fails when a changed file's path never appears in the output, and when a section that was
generated never reached it: each section file starts with a `<!-- code-walkthrough:kind -->` marker
that has to show up in the rendered file. Do not proceed until it exits 0; a failure here is a
bug to fix, not something to send back and retry.

## 4. Splice the JS, only when the HTML page was built

One placeholder, filled only when the page actually uses it: mermaid draws the flow diagram and
the symbol-delta diagrams, so splice it when the page contains a `class="mermaid"` block.
Nothing else on the page needs a vendored asset; the explanations toggle and the per-line
commenting are both plain inline script in the template.

```bash
python3 <skill>/scripts/splice_assets.py <out>.html --skill <skill>
```

A page with no `class="mermaid"` block at all keeps the placeholder as an inert comment and
stays small.

## 5. Open it, only when the HTML page was built

The page is a plain local file: `xdg-open <out>.html`. Notes typed into it live in
`localStorage` on this file's origin; nothing round-trips to `state.json` on its own. The
Comments panel's two copy buttons are the only way out: Copy gh command posts straight to GitHub
(a later sync pulls the posted comments back into `state.json`), and Copy for agent copies the
same comments as feedback text to paste back into this session, see step 7.

### 5b. Hosting, only when the user asks for it in words

Nothing is uploaded by default. When the user asks for a link, to read it on a phone, or names a
host, hand the built page to whatever file-sharing or upload skill is available in the session,
and post the URL it prints on its own line. Say that the link is public to anyone holding it.

## 6. Tell the user

Report what actually ran:

- Name the HTML page's path, say whether it was opened, and add: click a diff
  line in the walkthrough to comment on it, reply inline under an existing thread (the persistent
  box under it, no button needed), click Resolve conversation to mark a thread done, and open the
  Comments button (bottom right) to review every comment and copy either a `gh` command to post
  them (and any resolved threads) directly, or the agent payload, see step 7.
- When the diffed target was a PR, add that every comment in the panel can be turned into a
  review comment once its payload is pasted back, see step 7.
- When step 5b uploaded the page, give the URL on its own line and repeat that commenting is
  local-file only.

Nothing is published anywhere unless the user asked for hosting in step 5b, or the user pastes a
Copy for agent payload and asks for it to be delivered, step 7.

## 7. Turn notes into PR comments, when the user asks

The page has no server to post a click to. When there is a pushed PR, the user runs the `gh`
command Copy gh command printed -- one heredoc per postable comment plus one
`resolveReviewThread` mutation per thread marked Resolve conversation -- in which case posting is
already done. Explain mode never has a PR, so the page offers only Copy for agent there. Copy for
agent copies the same comments as plain text (`Feedback:` then one `path:line` and its body per
comment, in page order): read it as feedback to act on directly in this conversation, not a
payload to machine-import, since it carries no ids.

How the `gh` command payload is built, what to show the user before posting, and why posting is
never automatic: `references/pr-comments.md`.
