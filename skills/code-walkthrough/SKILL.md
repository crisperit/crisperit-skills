---
name: code-walkthrough
description: Turns a git diff, commit range, branch, or GitHub PR into a self-contained local HTML review page, with a mermaid flow diagram, an annotated diff walkthrough, and per-line comments you hand back to the agent or post to the PR. Use for "visual diff", "visualize diff", "visualise diff", "visualize-diff", "visualize this PR", "visualise this branch", "visual recap", "review my changes visually", "show me what changed", "turn this PR into a review page", "graph the symbol changes in this PR", "review this with me", "walk me through this change", "walk me through this PR", or /code-walkthrough; both the -ize and -ise spellings mean this skill. This is for reviewing changes that already exist, unlike a planning skill (which plans work that does not exist yet) and unlike a code-review skill (which hunts for defects and reports findings rather than recapping or visualizing the change).
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
than a message.

## 2. Capture the diff to a file, never into context

Write it to the session scratchpad, never the git working tree (a stray review page there
pollutes commits and PRs). A branch, range or path target: `git diff <target> >
<scratchpad>/raw.diff`, stats from `git diff --numstat <target>`. A PR target: `gh pr diff <n> >
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
  "what_changed": "2 to 4 sentences on what the change accomplishes and why",
  "how_it_works": "machinery a cold reader needs, or \"\" when nothing needs it",
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
      "paths": ["pkg/thing.py"]
    }
  ]
}
```

This file is the whole of your judgment. Nothing downstream writes prose: `render.py` and
`walkthrough.py` place these strings and build every other part of the output from `raw.diff`.
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
derives that from `symdelta.json`, caller before callee, then by size, tests and generated files
last. Leave `groups` out entirely and the whole walkthrough becomes one such group. A file you
forget still renders in a trailing "Everything else" group, so a partial grouping is safe to
ship. The gate rejects a group with no title, a path not in the diff, or a file in two groups.

**Mark identifiers with backticks** in `verdict`, `what_changed`, `how_it_works`,
every `role` and every `note`: paths, function/method/type/class names, config
keys, metric names, literal values. Markdown passes them through as inline code; the HTML
renderer promotes them to `<code>` after escaping.

**`flow_mermaid` node labels are 2 to 6 words naming the step**, not a sentence explaining it;
the reasoning belongs in `how_it_works`. **An edge carries a label only when the arrow itself is
the action or transition**, 1 to 4 words, verb-led; a plain sequential step needs none.

One entry per file in `raw.diff`, one `hunks` entry per `@@` hunk in that file, every `role`
non-blank. A hunk's `note` explains the code, not the diff, and is often correctly left blank
when the lines say it on their face. Full note-writing rules and bad/good pairs:
`references/fanout.md`.

`verdict` is yours, not a subagent's, by default: a batch agent sees one slice and cannot write
it, and the prose agent never sees the graphs. On the fan-out route `fanout.py merge` carries it
through from `prose.json` when present, so write it into `analysis.json` yourself after merging
and before the gate; missing it does not fail the gate, but it costs the facts strip. Step 2b's
grouping subagent is the one exception: it writes it too, with the main thread reviewing rather
than authoring it.

## 2a. Fan out, only for a big diff

Split the diff so a fast cheap model can do the per-hunk noticing in parallel, then have one
better model write the prose that needs the whole picture.

```bash
python3 <skill>/scripts/fanout.py split --diff <scratchpad>/raw.diff \
  --out <scratchpad>/batches
```

1. Spawn one subagent per batch, all in a single message, each on a small fast model (haiku
   class): the job is per-hunk noticing inside one slice, and the gate checks every answer. Each
   fills only `role` and `note` in its own pre-seeded fragment.
2. Then one subagent on a stronger model (sonnet class), since it needs the whole change in view
   and nothing downstream can check its judgment, writes `<scratchpad>/prose.json` with `target`,
   `what_changed`, `how_it_works` and `flow_mermaid`, from the fragments and the numstat. Under
   about 2000 lines, also hand it `<scratchpad>/raw.diff` and tell it to read it. Full brief:
   `references/fanout.md`.
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
role)` pairs plus the graph summaries; it returns `groups` and `verdict` for you
to write into `analysis.json` and review rather than author.

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

## 2b2. Build the complexity chart, only when the target names two refs

```bash
python3 <skill>/scripts/complexity.py --repo . --base <base> --head <head> \
  --diff <scratchpad>/raw.diff > <scratchpad>/complexity.json
```

Reports where the worst touched function now stands, not a per-file total; see
`references/graphs.md` for why.

## 2b3. Build the symbol-delta graph, only when the target names two refs

The only graph either output carries besides the flow diagram. Same condition as 2b2: a
working-tree diff has no second ref. Issue this as its own Bash call, `dangerouslyDisableSandbox:
true`, in the same message as the fan-out spawns and 2b2's call.

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

Shows two pre-rendered levels, packages then symbols, with a toggle between them. Pass the file
to `render.py`'s `--symbols <path>`; it inserts nothing when the file is empty or the flag is
omitted.

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
branch with no PR. The number comes from step 1's `gh pr view` call.

Skip this step, and every link, when `head_pushed` is false or `repo_url` is empty: an unpushed
commit or a repo with no web remote produces links that 404. Nothing here needs the network.

## 2d. Build the walkthrough

Generated, not written: `analysis.json` supplies each file's `role`, each hunk's `note`, and the
`groups` that set the reading order; the script supplies the paths, counts, bars, nesting,
fences, escaping, and the order inside each group.

```bash
python3 <skill>/scripts/walkthrough.py --analysis <scratchpad>/analysis.json \
  --diff <scratchpad>/raw.diff --format html --symdelta <scratchpad>/symdelta.json \
  --complexity <scratchpad>/complexity.json \
  > <scratchpad>/section-walkthrough.html
```

`--symdelta` orders each group caller-first, `--complexity` adds the complexity chip, `--diff`'s
rename headers let a moved file show its old path instead of reading as a new addition. All
optional; pass them when the files exist. It carries every hunk body, no size budget, since
per-line commenting needs the lines, and cannot omit a file even when `analysis.json` did.

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
the PR already has them:

```bash
gh api repos/<owner>/<repo>/pulls/<n>/comments --paginate | \
  python3 <skill>/scripts/notes.py sync --state <scratchpad>/state.json
```

Then a second, additive pass so a resolved thread's state is known before the page renders --
the REST comments above carry no review-thread node id, so resolving one is only possible once
this has run:

```bash
gh api graphql -f query='
  query ReviewThreads($owner: String!, $repo: String!, $number: Int!) {
    repository(owner: $owner, name: $repo) {
      pullRequest(number: $number) {
        reviewThreads(first: 100) {
          nodes { id isResolved resolvedBy { login } comments(first: 100) { nodes { databaseId } } }
        }
      }
    }
  }' -F owner=<owner> -F repo=<repo> -F number=<n> | \
  python3 <skill>/scripts/notes.py sync-threads --state <scratchpad>/state.json
```

Which fields are trusted, how an outdated comment is kept, and how a reply finds its parent:
`references/pr-comments.md`.

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
  [--links <scratchpad>/links.json] [--title "Code walkthrough: <slug>"] \
  > <scratchpad>/code-walkthrough-<slug>.html
```

Omit `--symbols` when 2b3 did not run or returned `language: null`, `--links` when 2c did not
run. An empty section file inserts nothing, not even its heading.

`references/builder.md` is background on why the output is shaped the way it is, not
instructions to follow.

## 3b. Gate the rendered output

```bash
python3 <skill>/scripts/validate_analysis.py --diff <scratchpad>/raw.diff \
  --analysis <scratchpad>/analysis.json --rendered <the .html just written> \
  --sections <the section-*.html files>
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

A page with no `class="mermaid"` block at all keeps the placeholder as an inert comment and
stays small.

## 5. Open it, only when the HTML page was built

The page is a plain local file: `xdg-open <out>.html`. Notes typed into it live in
`localStorage` on this file's origin; nothing round-trips to `state.json` until the user opens
the Comments panel and pastes the Copy for agent payload back to this session, see step 7.

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

The page has no server to post a click to: the user opens the Comments button, clicks Copy for
agent there, and pastes the JSON it copies into the conversation (or, when a PR exists, may
instead run the `gh` command Copy gh command printed -- one heredoc per postable comment plus
one `resolveReviewThread` mutation per thread marked Resolve conversation, in which case this
step is already done). Import the pasted JSON, then deliver the ids it names:

```bash
python3 <skill>/scripts/notes.py import --state <scratchpad>/state.json   # JSON on stdin
python3 <skill>/scripts/notes.py deliver --state <scratchpad>/state.json --id <id>
```

`import` prints how many notes were added versus updated and which ids are ready to post, so
which `deliver` calls come next is never a guess.

`deliver` resolves the note's anchor against the current diff before it posts: a line still
inside a hunk goes where it always did, a line just outside one moves to the nearest hunk line
and says so in the body, and one with nothing near it becomes a file-level comment instead of the
silent drop GitHub gives a raw out-of-hunk post.

Once every drafted note for this ask has been delivered, submit the review as one unit instead
of leaving it as loose comments:

```bash
python3 <skill>/scripts/notes.py submit --state <scratchpad>/state.json \
  --event COMMENT --body-file <f>
```

`--event` is `COMMENT`, `APPROVE` or `REQUEST_CHANGES`; ask the user which if it is not obvious.
`submit` publishes one review and sends the author one notification, not one per comment.

When the user asks to refresh the comments, re-run step 2f's `notes.py sync` and `sync-threads`,
then reload the page to see them: nothing pushes an update to a page that is already open. A
thread the reader marked Resolve conversation shows as resolved for real once this confirms it;
until then the page shows it as pending.

What to show the user before posting, and why this one is never automatic:
`references/pr-comments.md`.
