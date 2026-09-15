---
name: visual-diff
description: Turns a git diff, commit range, branch, or GitHub PR that already exists into a markdown recap for the PR description plus a self-contained local HTML review page, with a mermaid flow diagram, an interactive relations graph the reader expands from modules down to files and classes, an annotated diff walkthrough, and per-line commenting the user can copy back for posting as PR review comments. Nothing is published by default: the page stays a local file unless the user passes --pr or asks in words for it to be hosted. Use for "visual diff", "visualize diff", "visualise diff", "visualize-diff", "visualize this PR", "visualise this branch", "visual recap", "review my changes visually", "show me what changed", "turn this PR into a review page", "graph the coupling in this PR", or /visual-diff; both the -ize and -ise spellings mean this skill. This is for reviewing changes that already exist, unlike a planning skill (which plans work that does not exist yet) and unlike a code-review skill (which hunts for defects and reports findings rather than recapping or visualizing the change).
---

# Visual Diff

Arguments: `$ARGUMENTS`, the diff target plus flags. Stay thin: capture the diff to a file and
hand the reading of it to subagents; the raw diff and the HTML template never enter your context.
Both outputs are then assembled by `render.py`, so no subagent writes markup. Every subagent
spawn passes an explicit `model` param; a built-in agent type with no pinned model would silently
inherit the expensive session model. Style rule for all prose: apply the no-ai-slop skill if
available, otherwise plain sentences, no dashes, no filler, no hedging.

| Flag | Effect |
|---|---|
| (default) | build both outputs: markdown recap and local HTML page |
| `--md-only` / `--markdown-only` | skip the HTML page |
| `--html-only` | skip the markdown |
| `--html` | accepted, ignored (default already covers it) |
| `--pr [<number>]` | also write the markdown into a pull request description, step 6 |
| `--recap-only` | cheap prose-only route, step 2a-recap; implies `--md-only` |

Bail with one clear line rather than silently honouring either half:
- `--html-only` + `--pr`: `--pr` needs the markdown that `--html-only` skips.
- `--html-only` + `--recap-only`: same reason.

`--recap-only` composes with `--pr`.

## 1. Resolve the target

The target arrives as prose as often as a flag, so read the words:

| What the user said | Target |
|---|---|
| nothing | working tree plus staged changes, `git diff HEAD` |
| "this PR", "my PR", "the PR", "this pull request" | the current branch's PR, see below |
| `123`, `#123`, a PR URL | that PR |
| "this branch", "my branch", "what this branch adds" | `<base>...HEAD` three-dot |
| `HEAD~3..HEAD`, `main...HEAD` | as given |
| a path | that path, as a filter |

"This PR" resolves through the current branch:

```bash
gh pr view --json number,baseRefName,headRefName --jq '[.number,.baseRefName,.headRefName]|@tsv'
```

Prefer diffing `<baseRefName>...HEAD` locally over `gh pr diff <n>`: it gives the local repo
path, base and head that `coupling.py`, `structure.py` and `links.py` all need, and it is the
same three-dot comparison GitHub shows. Fall back to `gh pr diff <n>` when the branch is not
checked out locally, and say in one line that the coupling graphs are skipped in that case. Bail
with one clear line when the branch has no PR yet, naming `--pr` as the way to say which one.
Always prefer three-dot over two-dot: two-dot also shows commits that landed on the base branch
meanwhile, attributing other people's work to this change.

Naming a PR as the target says which diff to read, not that anything gets written to GitHub;
that needs `--pr`, and it asks first. On a `--pr` run, add `author` to that same `gh pr view`
call and check it against `gh api /user --jq .login` now, not at step 6, so the authorship guard
refuses someone else's PR before the analysis is paid for. Say in one line that the recap will
be built but not written back.

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
than a message.

## 2. Capture the diff to a file, never into context

Write it to the session scratchpad, never the git working tree (a stray review page or recap file
there pollutes commits and PRs). A branch, range or path target: `git diff <target> >
<scratchpad>/raw.diff`, stats from `git diff --numstat <target>`. A PR target: `gh pr diff <n> >
<scratchpad>/raw.diff`, stats from `gh pr view <n> --json files --jq '.files[] |
"\(.additions)\t\(.deletions)\t\(.path)"'`, the PR equivalent of numstat.

Pick the cheapest route that fits the diff; all three produce the same
`<scratchpad>/analysis.json`, gated the same way in step 2b, and the diff is never read twice.

| Diff | Route |
|---|---|
| under about 400 lines | read `raw.diff` in the main thread, write `analysis.json` yourself |
| up to about 1200 lines and 6 files | one `general-purpose` subagent, `model="sonnet"` |
| bigger than either | fan out per batch, step 2a |
| `--recap-only` given | skip the fan-out entirely, step 2a-recap |

Below the fan-out threshold a single pass finishes before parallel spawns have started, so
splitting there costs wall clock rather than saving it.

Start steps 2b2 and 2b3's five graph scripts the moment `raw.diff` exists, in the same message
that spawns the fan-out: two Bash calls alongside the spawns, not one per script. They read git
and the diff, never `analysis.json`. `layers.py` is the slow one on a large repo; running it
concurrently with the annotation agents keeps it off the critical path (`references/graphs.md`).

```json
{
  "target": "master...HEAD",
  "verdict": "one line for the facts strip: the change in a breath",
  "what_changed": "2 to 4 sentences on what the change accomplishes and why",
  "how_it_works": "machinery a cold reader needs, or \"\" when nothing needs it",
  "flow_mermaid": "flowchart LR ..., or \"\" when there is no flow worth drawing",
  "section_notes": {
    "explorer": "one or two sentences above the HTML relations box: which level to start at",
    "layers": "one line above the markdown module map",
    "coupling": "one line above the markdown file graph",
    "structure": "one line above the markdown symbol graph"
  },
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
      "paths": ["pkg/thing.py"]
    }
  ]
}
```

This file is the whole of your judgment. Nothing downstream writes prose: `render.py` and
`walkthrough.py` place these strings and build every other part of both outputs from `raw.diff`.
So a field left blank is a section that will not appear, not a section someone else will fill in.

`groups` is the reading order, the one part of the walkthrough that needs you rather than the
diff: only someone who read the change knows which files belong to which theme and which theme a
reviewer should meet first. Write one group per theme, in reading order, titled as the theme
rather than as a directory.

Keep the groups wide. Aim for three to five whatever the file count, and never more than six:
a group is a theme a reviewer holds in their head, not a stage in the data flow. Tracing the
change end to end and giving each hop its own group is the failure mode here, and it reads as
a pipeline diagram rather than a reading order. A group of one or two files almost always
belongs merged into the neighbour it feeds, and a test file belongs with the code it covers,
never in a group of its own. Under about five changed files, one group is the right answer.

You do **not** decide the order inside a group; `walkthrough.py`
derives that from `coupling.json`, caller before callee, then by size, tests and generated files
last. Leave `groups` out entirely and the whole walkthrough becomes one such group. A file you
forget still renders in a trailing "Everything else" group, so a partial grouping is safe to
ship. The gate rejects a group with no title, a path not in the diff, or a file in two groups.

**Mark identifiers with backticks** in `verdict`, `what_changed`, `how_it_works`,
`section_notes`, every `role` and every `note`: paths, function/method/type/class names, config
keys, metric names, literal values. Markdown passes them through as inline code; the HTML
renderer promotes them to `<code>` after escaping.

`section_notes` is where an opinion belongs, and the only place: anchor it in something visible
in the diagram or its `Numbers:` line, no severity or blocking language, structure only. See
`references/pr-markdown.md`. Omit a key when its section has nothing worth pointing at.

**`flow_mermaid` node labels are 2 to 6 words naming the step**, not a sentence explaining it;
the reasoning belongs in `how_it_works`. **An edge carries a label only when the arrow itself is
the action or transition**, 1 to 4 words, verb-led; a plain sequential step needs none.

One entry per file in `raw.diff`, one `hunks` entry per `@@` hunk in that file, every `role`
non-blank. A hunk's `note` explains the code, not the diff, and is often correctly left blank
when the lines say it on their face. Full note-writing rules and bad/good pairs:
`references/fanout.md`.

`verdict` and `section_notes` are yours, not a subagent's, by default: a batch agent sees one
slice and cannot write the verdict, and the prose agent never sees the graphs. On the fan-out
route `fanout.py merge` carries them through from `prose.json` when present, so write them into
`analysis.json` yourself after merging and before the gate; neither missing one fails the gate,
but a missing `verdict` costs the facts strip and a missing `section_notes` drops every caption.
Step 2b's grouping subagent is the one exception: it writes both, with the main thread reviewing
rather than authoring them.

## 2a-recap. `--recap-only`, the cheap route

Skip the fan-out. Build step 2b2's four graph JSONs, spawn one `general-purpose` subagent
(`model="sonnet"`) to write a lighter `analysis.json` from `git diff --numstat` and the graph
summaries only, then gate and render without a walkthrough.

```bash
python3 <skill>/scripts/validate_analysis.py --diff <scratchpad>/raw.diff \
  --analysis <scratchpad>/analysis.json --recap
```

Full flow, what the subagent reads and writes, and the rendering budget on this path:
`references/recap-mode.md`.

## 2a. Fan out, only for a big diff

Split the diff so a fast cheap model can do the per-hunk noticing in parallel, then have one
better model write the prose that needs the whole picture.

```bash
python3 <skill>/scripts/fanout.py split --diff <scratchpad>/raw.diff \
  --out <scratchpad>/batches
```

1. Spawn one subagent per batch, all in a single message, each `model="haiku"`, each filling
   only `role` and `note` in its own pre-seeded fragment.
2. Then one prose subagent, `model="sonnet"`, reads the fragments and the numstat and writes
   `<scratchpad>/prose.json` with `target`, `what_changed`, `how_it_works` and `flow_mermaid`.
3. Merge:

   ```bash
   python3 <skill>/scripts/fanout.py merge --diff <scratchpad>/raw.diff \
     --prose <scratchpad>/prose.json --fragments <scratchpad>/batches/fragment-[0-9].json \
     --out <scratchpad>/analysis.json
   ```

Full subagent brief, the role and note rules with their bad/good pairs, and what the merge
canonicalizes: `references/fanout.md`.

## 2b. Gate the analysis

After step 2a's merge, spawn one subagent, `model="sonnet"`, with the merged `(path, role)`
pairs plus the graph summaries; it returns `groups`, `verdict` and `section_notes` for you to
write into `analysis.json` and review rather than author.

```bash
python3 <skill>/scripts/validate_analysis.py --diff <scratchpad>/raw.diff \
  --analysis <scratchpad>/analysis.json
```

It prints one plain line per problem and exits non-zero: a file present in the diff but missing
from `files[]`, a blank `role`, an invented file or hunk, and a filled-in `note` that shares no
identifier with that hunk's own changed lines. It also fails when more than 80% of the diff's
hunks have an empty note (`EMPTY_NOTE_FLOOR`). Do not proceed to step 3 until it exits 0. Never
render an analysis that has not passed.

Under about ten problem lines, patch `analysis.json` in the main thread; above that, or when a
whole file entry is missing, send the exact lines back to the batch and fragment that produced
them, and respawn with `model="sonnet"` if the same batch fails twice. Full routing, the
identifier-overlap check, and the empty-note floor's reasoning: `references/fanout.md`.

Two things the gate does not check, so check them yourself: a hunk entry the fragment appended
for a deleted file's `hunks` list, and the `verdict`/`section_notes` keys.

## 2b2. Build the coupling sections, only when the target names two refs

Three graphs, generated by code so none can go missing or get paraphrased. `layers.py` folds the
whole repo into modules (the only one that sees untouched files too), `coupling.py` covers the
files this change moved, `structure.py` the classes and functions inside them. A module is a
directory, at full depth, no architecture-layer vocabulary.

Run the four sequentially, in one sandboxed Bash call. **Never background them with `&` and
`wait`** (`references/graphs.md` has the measurement).

```bash
cd <repo>
python3 <skill>/scripts/structure.py  --repo . --base <base> --head <head> > <scratchpad>/structure.json
python3 <skill>/scripts/complexity.py --repo . --base <base> --head <head> \
  --diff <scratchpad>/raw.diff > <scratchpad>/complexity.json
python3 <skill>/scripts/coupling.py --repo . --base <base> --head <head> > <scratchpad>/coupling.json
python3 <skill>/scripts/layers.py --repo . --head <head> \
  --coupling <scratchpad>/coupling.json > <scratchpad>/layers.json
```

`layers.py` has to run after `coupling.py`, whose output it reads. The other two have no
ordering constraint. `complexity.py` reports where the worst touched function now stands, not a
per-file total; see `references/graphs.md` for why. Both `structure.py` and `coupling.py` treat
`--base` as the merge base of the two refs.

```bash
for kind in layers coupling structure; do
  python3 <skill>/scripts/sections.py --kind $kind --data <scratchpad>/$kind.json \
    --format md > <scratchpad>/section-$kind.md
done
```

The HTML page does not get its own build of these three: step 2b3's `symdelta.py` result feeds
the HTML page's own graph instead.

## 2b3. Build the symbol-delta graph, only when the target names two refs

Same condition as 2b2: a working-tree diff has no second ref. Skip on `--md-only` too, since only
`render.py`'s `--symbols` flag on the HTML call consumes it. Issue this as its own Bash call,
`dangerouslyDisableSandbox: true`, in the same message as the fan-out spawns and 2b2's call.

```bash
python3 <skill>/scripts/symdelta.py --repo . --base <base> --head <head> > <scratchpad>/symdelta.json
```

Check `"language"` in the output before spending anything on the section: `null` means the diff
touched no supported language, or is missing a required tool, with why in `"reason"`. Skip the
symbols section in that case and say so in one line. A `null` is never licence to fall back to
name-matched edges; see `references/graphs.md` for why that guess is exactly the failure mode
this graph exists to avoid.

```bash
python3 <skill>/scripts/sections.py --kind symbols --data <scratchpad>/symdelta.json \
  --format html > <scratchpad>/section-symbols.html
```

`--kind symbols` only supports `--format html`. Pass the file to `render.py`'s html call as
`--symbols <scratchpad>/section-symbols.html`; it inserts nothing when the file is empty or the
flag is omitted.

Supported: Go (`.go`, native `go/packages`), TypeScript (`.ts`, `.tsx`), Python (`.py`) and Rust
(`.rs`), the last three over LSP and each needing its server on PATH
(`typescript-language-server`, `pyright-langserver`, `rust-analyzer`). A mixed diff picks the
language with the most changed files and says so in `"reason"`.

Set `XDG_CACHE_HOME` yourself if `~/.cache` is read-only in your sandbox; `symdelta.py` builds
and caches its extractor binary there. The per-language profiles, the method-qualification rules
that differ per server, the name-collision problem this graph avoids, and the runtime budget on
a large repo: `references/graphs.md`.

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
branch with no PR. The number comes from step 1's `gh pr view` call. `--pr` as a flag decides
whether the recap gets written into the description, a separate question from where links point.

Skip this step, and every link, when `head_pushed` is false or `repo_url` is empty: an unpushed
commit or a repo with no web remote produces links that 404. Nothing here needs the network.

## 2d. Build the walkthrough

Generated, not written: `analysis.json` supplies each file's `role`, each hunk's `note`, and the
`groups` that set the reading order; the script supplies the paths, counts, bars, nesting,
fences, escaping, and the order inside each group.

```bash
python3 <skill>/scripts/walkthrough.py --analysis <scratchpad>/analysis.json \
  --diff <scratchpad>/raw.diff --format html --coupling <scratchpad>/coupling.json \
  --complexity <scratchpad>/complexity.json \
  > <scratchpad>/section-walkthrough.html
python3 <skill>/scripts/walkthrough.py --analysis <scratchpad>/analysis.json \
  --diff <scratchpad>/raw.diff --format md --links <scratchpad>/links.json \
  --coupling <scratchpad>/coupling.json --complexity <scratchpad>/complexity.json \
  > <scratchpad>/section-walkthrough.md
```

`--coupling` orders each group caller-first, `--complexity` adds the complexity chip, `--diff`'s
rename headers let a moved file show its old path instead of reading as a new addition. All
optional; pass them when the files exist. HTML carries every hunk body (no size budget, and
per-line commenting needs the lines); markdown carries notes only, since GitHub renders the real
diff below the recap. Neither format can omit a file even when `analysis.json` did.

Markdown's default budget is `--max-chars 12000`. Over it, markdown demotes lockfiles and
generated output first, then tests, to one line each. On an over-budget warning in step 3, rerun
`walkthrough.py --format md` with the number `render.py` prints, once. Why to take that number
rather than guess, and why there is no per-line moved-block marker: `references/rationale.md`.

## 3. Build the outputs

Both outputs run unless a flag says otherwise: skip the markdown one with `--html-only`, skip
the HTML one with `--md-only`. No subagent renders either one. Both are `render.py` calls, and
the slug comes from the diffed target (branch name, PR number, or "working").

```bash
python3 <skill>/scripts/render.py --analysis <scratchpad>/analysis.json \
  --diff <scratchpad>/raw.diff --format html \
  --template <skill>/assets/diff-review-template.html \
  --walkthrough <scratchpad>/section-walkthrough.html \
  [--symbols <scratchpad>/section-symbols.html] \
  [--links <scratchpad>/links.json] [--title "Visual diff: <slug>"] \
  > <scratchpad>/visual-diff-<slug>.html

python3 <skill>/scripts/render.py --analysis <scratchpad>/analysis.json \
  --diff <scratchpad>/raw.diff --format md \
  --layers <scratchpad>/section-layers.md --coupling <scratchpad>/section-coupling.md \
  --structure <scratchpad>/section-structure.md \
  --walkthrough <scratchpad>/section-walkthrough.md \
  [--links <scratchpad>/links.json] [--complexity <scratchpad>/complexity.json] \
  > <scratchpad>/visual-diff-<slug>.md
```

Omit `--layers`/`--coupling`/`--structure` when 2b2 did not run, `--symbols` when 2b3 did not run
or returned `language: null`, `--links` when 2c did not run, `--complexity` when that script did
not run. An empty section file inserts nothing, not even its heading.

`references/builder.md` and `references/pr-markdown.md` are background on why the output is
shaped the way it is, not instructions to follow.

## 3b. Gate the rendered output

Once per rendered file:

```bash
python3 <skill>/scripts/validate_analysis.py --diff <scratchpad>/raw.diff \
  --analysis <scratchpad>/analysis.json --rendered <the .md or .html just written> \
  --sections <the section-*.md or section-*.html files for that format>
```

It fails when a changed file's path never appears in the output, and when a section that was
generated never reached it: each section file starts with a `<!-- visual-diff:kind -->` marker
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

A page with no `class="mermaid"` block at all (a markdown-only run never even builds one) keeps
the placeholder as an inert comment and stays small.

## 5. Open it, only when the HTML page was built and `--pr` was not given

`xdg-open <out>.html`

A `--pr` run is usually part of a longer push flow, so it names the path and leaves the browser
alone; ask before opening if you want it anyway.

### 5b. Hosting, only when the user asks for it in words

Nothing is uploaded by default. When the user asks for a link, to read it on a phone, or names a
host, hand the built page to whatever file-sharing or upload skill is available in the session,
and post the URL it prints on its own line. Say two things with it, because both surprise
people: the link is public to anyone holding it, and the page is served without same-origin, so
the per-line commenting and the "Copy notes for Claude" button do not work there. Anyone who
wants to annotate opens the local file instead.

## 6. Write the PR description, only with `--pr [<number>]`

Resolve the PR number from the argument, else the current branch. Bail with one clear line when
no PR exists yet, or when the authenticated user is not the PR's author:

```bash
ME=$(gh api /user --jq .login)
AUTHOR=$(gh pr view <n> --json author --jq .author.login)
```

1. `gh pr view <n> --json body --jq .body > <scratchpad>/pr-body.txt`
2. Wrap the recap in `<!-- visual-diff:start -->` / `<!-- visual-diff:end -->` markers and
   splice it into the body, writing `<scratchpad>/pr-body-new.txt`.
3. `gh pr edit <n> --body-file <scratchpad>/pr-body-new.txt`, always `--body-file`, never an
   inline `--body` string.

Confirm once per PR per session before the first edit. Full splice logic, the authorship guard,
and confirmation rules: `references/pr-workflow.md` and `references/pr-markdown.md`.

## 7. Tell the user

Report what actually ran:

- Name the markdown file's path, unless `--html-only` was given.
- When the HTML page was built, name its path, say whether it was opened, and add: click a diff
  line in the walkthrough to comment on it, click "Copy notes for Claude", paste it back here.
- When `--pr` ran, confirm the description was updated and give the PR URL
  (`gh pr view <n> --json url --jq .url`).
- When the diffed target was a PR, add that pasted notes can be turned into comments on it, see
  step 8.
- When step 5b uploaded the page, give the URL on its own line and repeat that commenting is
  local-file only.

Nothing is published anywhere unless `--pr` was given, or the user asked for hosting in step 5b.

## 8. Turn pasted notes into PR comments, when the user asks

The page's "Copy notes for Claude" button produces a numbered `## Diff notes` block, one entry
per line comment, each with a `path:line SIDE`, the line itself, and a `note:`. Offer to post it
back onto the PR only when the user confirms, and only what they confirm.

```bash
gh api repos/<owner>/<repo>/pulls/<n>/comments
```

with `path`, `line`, `side` (`RIGHT`/`LEFT`, taken from the block, never re-derived),
`commit_id` from `links.json`'s head sha, and `body` from the `note:` row. Rewrite nothing; the
note is the user's words.

Diff-notes block format, the LEFT/RIGHT rule, and when to use one general `gh pr comment`
instead: `references/pr-workflow.md`.
