---
name: code-walkthrough
description: Use when the user wants a change or an area of code shown to them rather than searched: "visual diff", "visualize diff", "visualise diff", "visualize-diff", "visualize this PR", "visualise this branch", "visual recap", "review my changes visually", "show me what changed", "turn this PR into a review page", "graph the symbol changes in this PR", "review this with me", "walk me through this change", "walk me through this PR", "explain the auth flow", "explain how billing works", "how does X work", "walk me through the auth flow", "help me understand this codebase", "help me understand src/parser/", or /code-walkthrough; both the -ize and -ise spellings mean this skill. Turn a git diff, commit range, branch, GitHub PR, or an unchanged area of code into a self-contained local HTML walkthrough page: mermaid flow diagram, annotated diff walkthrough, per-line comments to hand back to the agent or post to the PR. Reviews or explains code that already exists, unlike a planning skill (plans work that does not exist yet) and unlike a code-review skill (hunts defects rather than recapping or visualizing).
---

# Code Walkthrough

The diff target is what the user named when invoking this skill (in Claude Code: `$ARGUMENTS`).
Stay thin: capture the diff to a file and hand the reading of it to subagents; the raw diff and
the HTML template never enter your context. The main loop never loads diff hunks, file contents,
or per-hunk notes; it may load the verdict, the group titles, and the gate result. The output is
then assembled by `render.py`, so no subagent writes markup. Style rule for all prose: apply the
no-ai-slop skill if available, otherwise plain sentences, no dashes, no filler, no hedging.

Four terms recur throughout and are defined once, here:

- **`<scratchpad>`**: a private temp dir outside the repo -- the harness's session scratch dir if
  it has one, else `mktemp -d`.
- **Strong tier**: a capable model one step below the session model (on Claude, sonnet class).
  Every spawn that needs the whole change in view, where nothing downstream checks its judgment,
  uses this tier; each such spawn still states its own reason.
- **Pin the tier**: pin the tier on every spawn when the harness allows it, since an unpinned
  spawn can inherit the expensive session model; spawn anyway and accept the cost when it can't.
- **"In parallel"**: spawn at the same time, in one message on a harness (Claude Code included)
  that only runs tool calls concurrently when they share one.

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

Links still skip themselves for this target too: `snap` is never pushed, so `head_pushed` comes
back false the same way it does for any other unpushed head.

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

Fetch, then compare against the remote-tracking base, never the local branch. Run the fetch
outside any command sandbox, because it needs the network and symdelta later writes caches
outside the repo too (in Claude Code, `dangerouslyDisableSandbox: true`):

```bash
git fetch origin <baseRefName> <headRefName>
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

Write it to `<scratchpad>`, never the git working tree (a stray review page there
pollutes commits and PRs). A branch, range or path target: `git diff <target> >
<scratchpad>/raw.diff`, stats from `git diff --numstat <target>`. The working tree target diffs
against its snapshot commit instead: `git diff HEAD <snap> > <scratchpad>/raw.diff`, stats from
`git diff --numstat HEAD <snap>`. A PR target: `gh pr diff <n> >
<scratchpad>/raw.diff`, stats from `gh pr view <n> --json files --jq '.files[] |
"\(.additions)\t\(.deletions)\t\(.path)"'`, the PR equivalent of numstat.

Pick the cheapest route that fits the diff; both produce the same `<scratchpad>/analysis.json`,
gated the same way in step 2b, and the diff is never read twice.

| Diff | Route |
|---|---|
| up to about 1500 changed lines (numstat added plus deleted) | one subagent that can run shell commands and read files, on the strong tier |
| bigger | fan out, step 2a |

Delegating below the fan-out threshold costs no wall clock over doing it in the main thread and
nothing in context; splitting further into batches is where the cost shows up, since a single
pass finishes before parallel spawns have even started.

The moment `raw.diff` exists, in the same message that spawns the fan-out (or the single
subagent below the threshold), run two more Bash calls: the complexity chart and the
symbol-delta graph. Neither reads `analysis.json`, so neither has to wait for it.

```bash
python3 <skill>/scripts/pipeline.py early --dir <scratchpad> --repo <repo> --base <base> \
  --head <head>
```

`symdelta.py` is the slow one on a large repo; running it concurrently with the annotation agents
keeps it off the critical path, and it is why the script stays outside the driver rather than
folding into `early`. Its own command and decision points: step 2b3, `references/graphs.md`.

### No-subagent fallback

Only on a harness that cannot spawn subagents at all. Still run `fanout.py split` on any diff,
and work batch by batch: read `batch-N.diff`, fill `fragment-N.json`, gate it with
`validate_analysis.py --fragment`. Each fragment is on disk the moment it passes, so a context
compaction mid-run loses no work. Write `prose.json`, with `groups`, last, from the fragments'
paths and roles plus the numstat, then run `fanout.py merge`, the gate in step 2b, and step 3.

Honest bound: a single thread cannot drop earlier batches from its own context, so this reads up
to the whole diff into context -- a named exception to the rule that the main loop never loads
diff hunks. Above the fan-out threshold, tell the user the cost in one line and offer to narrow
the target first, the same pattern as explain mode's size bands (`references/explain-mode.md`).
The template and the rendered HTML still never enter context.

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
agent on the strong tier write the prose and the groups that need the whole picture.

```bash
python3 <skill>/scripts/fanout.py split --diff <scratchpad>/raw.diff \
  --out <scratchpad>/batches
```

1. Spawn one subagent per batch, all in a single message, each on the strong tier: the job is
   per-hunk noticing inside one slice, and the gate checks every answer. Each fills only `role`
   and `note` in its own pre-seeded fragment, then gates itself with
   `validate_analysis.py --fragment` before replying.
2. Then one subagent on the strong tier, since it needs the whole change in view and nothing
   downstream can check its judgment, writes `<scratchpad>/prose.json` with `target`,
   `overview`, `verdict`, `groups` and a top-level `flow_mermaid`, from the fragments, the
   numstat and `<scratchpad>/symdelta.json`'s counts and moved files. Under about 2000 lines,
   also hand it `<scratchpad>/raw.diff` and tell it to read it. It waits for every batch and for
   `symdelta.json`. Full brief: `references/fanout.md`.
3. Merge:

   ```bash
   python3 <skill>/scripts/fanout.py merge --diff <scratchpad>/raw.diff \
     --prose <scratchpad>/prose.json --fragments <scratchpad>/batches/fragment-[0-9].json \
     --out <scratchpad>/analysis.json
   ```

Full subagent brief, the role and note rules with their bad/good pairs, and what the merge
canonicalizes: `references/fanout.md`.

## 2a-regen. Skip what has not changed, optional

Optional, droppable, only on a re-review with a prior state.json and cache. Run it right after
`fanout.py split`: `references/regen.md`.

## 2b. Gate the analysis

After step 2a's merge, run the gate. It is the only gate before render, and stays unchanged
regardless of which route wrote `analysis.json`:

```bash
python3 <skill>/scripts/validate_analysis.py --diff <scratchpad>/raw.diff \
  --analysis <scratchpad>/analysis.json
```

It prints one plain line per problem and exits non-zero: a file present in the diff but missing
from `files[]`, a blank `role`, an invented file or hunk, and a filled-in `note` that shares no
identifier with that hunk's own changed lines. It also fails when more than 80% of the diff's
hunks have an empty note (`EMPTY_NOTE_FLOOR`), or when a `groups` entry fails validation. Do not
proceed to step 3 until it exits 0. Never render an analysis that has not passed.

Send the gate's problem lines to a subagent to fix, never patch `analysis.json` yourself: the
gate output already names the path and the field, so nothing else needs to travel. Under about
ten lines, one subagent working from just those lines can patch it directly. Above that, or when
a whole file entry is missing, route the lines to the batch and fragment that produced them
instead, since fixing it needs more context than the gate line alone gives, and respawn that
batch on the strong tier if it fails twice. Full routing, the identifier-overlap check, and the
empty-note floor's reasoning: `references/fanout.md`.

Two things the gate does not check, so check them yourself: a hunk entry the fragment appended
for a deleted file's `hunks` list, and the `verdict` key.

## 2b3. Build the symbol-delta graph

Every target names two refs by this point, a working-tree target's via the snapshot commit from
step 1, so this step always runs. It is the only graph either output carries besides the flow
diagram, and it stays outside `pipeline.py`: see step 2 above for why. Issue it as its own Bash
call, run outside any command sandbox, because git fetch needs the network and symdelta writes
caches outside the repo (in Claude Code, `dangerouslyDisableSandbox: true`), in the same message
as the fan-out spawns and `pipeline.py early`. In explain mode `<base>` is the empty baseline:
`resolve_base` already falls back to the base itself when three-dot's merge-base does not exist,
so this runs two-dot under the hood without any separate handling.

```bash
python3 <skill>/scripts/symdelta.py --repo . --base <base> --head <head> > <scratchpad>/symdelta.json
```

Check `"language"` in the output before spending anything further on this step: `null` means the
diff touched no supported language, or is missing a required tool, with why in `"reason"`.
`sections.py` puts that on the page itself (run automatically inside step 3's `prepare`), so
there is no separate line to relay by hand. When the result carries a `"remedy"`, it is a command
that fixes the local tooling, and it still needs asking before you run it: `npm ci` rebuilds the
user's node_modules and a `-g` install mutates their machine. Tell the user the problem and the
exact command in one line, ask once, then re-run symdelta.py and continue. Each language server's
install command, the `--doctor` command, and why a `null` is never licence to fall back to
name-matched edges: `references/graphs.md`.

When `"language"` is `null`, there is a second option beyond fixing the local tooling: offer,
once, to build the graph yourself by reading the code instead of the compiler doing it; the page
then labels it inferred rather than compiler-resolved. If the user agrees, write one JSON object
per line to a scratch file, in the extractor's own wire shape (`FromFile`, `FromSym`, `ToFile`,
`ToSym`, repo-relative paths, `Type.method` for a method), then re-run with
`--llm-head-edges <scratchpad>/llm-edges-head.jsonl`. Still not licence to name-match: the LLM
tier reads the actual call sites, it does not guess from identifier names.

`<paths>` is the files and directories the page is about, the same pathspec the diff used, space
separated -- pass it to `pipeline.py prepare --paths` in step 3, since that is what actually runs
`sections.py` now. **Required in explain mode**: the empty baseline makes every symbol in the
repository new, so without it a page explaining one package draws the entire tree (measured: 3909
nodes, 5905 edges, 611 KB of mermaid). Omit it when the diff is a real change, where the delta is
already the scope. Explaining a whole repository is a legitimate `<paths>` of `.`.

Supported: Go, TypeScript, Python and Rust, each needing its language server on PATH. The
per-language profiles, install commands, cache location, the name-collision problem this graph
avoids, and the runtime budget on a large repo: `references/graphs.md`.

## 2f. Sync existing PR comments, only when the diffed target is a GitHub PR

Pull the PR's own review comments in before rendering, or the page shows zero threads even when
the PR already has them: a `notes.py sync` pass over the REST comments, then an additive
`sync-threads` pass over the GraphQL review threads, since the REST payload carries no thread
node id and resolving one needs it.

Both commands, which fields are trusted, how an outdated comment is kept, and how a reply finds
its parent: `references/pr-comments.md`.

## 2g. Resolve threads, only when the diffed target is a GitHub PR

Not droppable. When the target is a GitHub PR it runs on a first review too, and the only thing
that skips it is `resolved-threads` returning an empty list. It produces the page's primary
content for a resolved thread, not a cache optimisation. Every worker it spawns runs on the
strong tier: deciding that a commit does NOT answer a comment is the whole value of the step, and
a weaker model either forces a match or declines one it should have made. Steps, commands and
the worker prompt: `references/resolved-threads.md`, read in full before starting.

## 3. Build the page

Everything from here on is deterministic; `pipeline.py` runs it as one driver instead of the
dozen manual calls this used to be. No subagent renders it: `render.py` builds the page at
`<scratchpad>/<slug>.html`, and `render` prints that path as its last line of stdout. The slug
comes from the diffed target (branch name, PR number, or "working").

For any target that is not a GitHub PR:

```bash
python3 <skill>/scripts/pipeline.py all --dir <scratchpad> --repo <repo> --base <base> \
  --head <head> [--pr <n>] [--paths <paths>] [--explain] [--prior] [--links-cached] \
  --slug <slug> [--title "Code walkthrough: <slug>"]
```

For a GitHub PR target, run `prepare`, then steps 2f and 2g (they change `state.json`), then
`render`, in that order -- reversing it silently drops a synced thread off the page:

```bash
python3 <skill>/scripts/pipeline.py prepare --dir <scratchpad> --repo <repo> --base <base> \
  --head <head> --pr <n> [--paths <paths>] [--explain] [--prior] [--links-cached]
# steps 2f and 2g run here
python3 <skill>/scripts/pipeline.py render --dir <scratchpad> --slug <slug> \
  [--title "Code walkthrough: <slug>"]
```

`prepare` runs the gate first and stops before anything else on failure, clears any stale
`section-*.html` from an earlier run, runs `structure.py`/`sections.py` when `symdelta.json`
exists, runs `links.py`, `walkthrough.py` and `state.py`, then writes `pipeline.json`. `render`
reads `pipeline.json` (refusing to run without one), then runs `render.py`, the rendered gate,
and `splice_assets.py` in that order. A rendered-gate failure is a bug in `render.py` or the
template to fix, never something to send back to a subagent and retry.

Always pass `--pr <n>` when the branch has a PR at all, whatever the target was (the number comes
from step 1's `gh pr view` call); a link into the Files changed tab is where a reviewer wants to
land. Explain mode never has a PR, so omit `--pr` entirely. `--paths` is required in explain mode
(`references/explain-mode.md`, `references/graphs.md`) and omitted for a real diff. Pass
`--prior` only when a `state.json` from an earlier run on this same target already sits at that
scratchpad path, so its notes carry forward -- never automatically, since a reused scratchpad
could carry notes from another target. Pass `--links-cached` only when regen reported a hit and
already copied its cache to `links.json` (`references/regen.md`); `prepare` refuses to run when
the flag is passed and that file isn't there.

### `<explain>`

Resolve it like `<skill>` and `<scratchpad>`, not like an optional extra: it is `--explain` when
step 1 read the ask as "explain X as it stands" (the empty-baseline row of the target table), and
omitted in every other mode. Do not re-derive the mode from the diff's shape down here: the empty
baseline makes everything an addition, but so does a real change that only adds files, and only
the ask tells those two apart. What `--explain` changes in the rendered page:
`references/explain-mode.md`.

Stdout is a few plain lines: the gate result, each section's language or null reason, `links:
on`/`off`, a verdict warning when `verdict` is blank, and the page's path, never hunk text or
notes. `references/builder.md` and `references/graphs.md` are background on why the output is
shaped this way, not instructions to follow.

## 4. Open it, only when the HTML page was built

The page is a plain local file. Open it with `xdg-open <out>.html` on Linux, `open <out>.html` on
macOS, or `start "" <out>.html` on Windows. When none of those work, or the session is remote or
headless, print the path instead and do not open it -- a text-mode browser fallback would launch
inside the agent's own terminal and block it. Notes typed into the page live in `localStorage` on
this file's origin; nothing round-trips to `state.json` on its own. The Comments panel's two copy
buttons are the only way out: Copy gh command posts straight to GitHub (a later sync pulls the
posted comments back into `state.json`), and Copy for agent copies the same comments as feedback
text to paste back into this session, see step 6.

### 4b. Hosting, only when the user asks for it in words

Nothing is uploaded by default. When the user asks for a link, to read it on a phone, or names a
host, hand the built page to whatever file-sharing or upload skill is available in the session,
and post the URL it prints on its own line. Say that the link is public to anyone holding it.

## 5. Tell the user

Report what actually ran:

- Name the HTML page's path, say whether it was opened, and add: click a diff
  line in the walkthrough to comment on it, reply inline under an existing thread (the persistent
  box under it, no button needed), click Resolve conversation to mark a thread done, and open the
  Comments button (bottom right) to review every comment and copy either a `gh` command to post
  them (and any resolved threads) directly, or the agent payload, see step 6.
- When the diffed target was a PR, add that every comment in the panel can be turned into a
  review comment once its payload is pasted back, see step 6.
- When step 4b uploaded the page, give the URL on its own line and repeat that commenting is
  local-file only.

Nothing is published anywhere unless the user asked for hosting in step 4b, or the user pastes a
Copy for agent payload and asks for it to be delivered, step 6.

## 6. Turn notes into PR comments, when the user asks

The page has no server to post a click to. When there is a pushed PR, the user runs the `gh`
command Copy gh command printed -- one heredoc per postable comment plus one
`resolveReviewThread` mutation per thread marked Resolve conversation -- in which case posting is
already done. Explain mode never has a PR, so the page offers only Copy for agent there. Copy for
agent copies the same comments as plain text (`Feedback:` then one `path:line` and its body per
comment, in page order): read it as feedback to act on directly in this conversation, not a
payload to machine-import, since it carries no ids.

How the `gh` command payload is built, what to show the user before posting, and why posting is
never automatic: `references/pr-comments.md`.
