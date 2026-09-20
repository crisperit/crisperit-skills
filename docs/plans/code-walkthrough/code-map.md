# Code map: skills/visual-diff, for the human-review evolution

All paths relative to `/home/crispy/dev/private/crisperit-skills/skills/visual-diff/` unless
stated otherwise. Line numbers are as of this snapshot (`ba254db`/`d1681d9` era, working tree
clean).

## 1. Pipeline: SKILL.md driving scripts/

`SKILL.md` is a runbook, not code; the model (agent) is the orchestrator, `scripts/*.py` are
stdlib-only leaves it shells out to. Order (see SKILL.md line numbers):

1. **Resolve target** (SKILL.md:30-84) — `git`/`gh` calls only, no script.
2. **Capture diff** (SKILL.md:86-105) → `<scratchpad>/raw.diff`, via `git diff` or `gh pr diff`.
3. **Write `analysis.json`** (SKILL.md:107-189) — by the main thread or subagents; schema at
   SKILL.md:112-140. This is the only file a model authors; everything else is derived.
   - 2a-recap (`--recap-only`): references/recap-mode.md — one subagent writes a lighter
     `analysis.json` with no `files[]`.
   - 2a (fan-out, big diffs): `scripts/fanout.py split` (SKILL.md:210-212) → per-batch haiku
     subagents fill `role`/`note` into pre-seeded fragments → `scripts/fanout.py merge`
     (SKILL.md:220-224) → `analysis.json`.
4. **2b Gate**: `scripts/validate_analysis.py` (SKILL.md:236-238) — must exit 0 before render.
5. **2b2 Complexity chart** (only when target names two refs, SKILL.md:287-296):
   `scripts/complexity.py` → `complexity.json`. `structure.py`, `coupling.py` and `layers.py`
   are gone; `complexity.py` is the only analyser left here, and `sections.py --kind` no
   longer accepts `layers`/`coupling`/`structure`, only `symbols` (point 6 below).
6. **2b3 Symbol-delta** (same two-ref condition, SKILL.md:289-322):
   `scripts/symdelta.py` → `symdelta.json` → `scripts/sections.py --kind symbols --format html`
   and `--format md` → `section-symbols.{html,md}`.
7. **2c Links**: `scripts/links.py` (SKILL.md:324-341) → `links.json`.
8. **2d Walkthrough**: `scripts/walkthrough.py --format html` and `--format md`
   (SKILL.md:349-357) → `section-walkthrough.{html,md}`. Consumes `analysis.json`, `raw.diff`,
   optionally `symdelta.json` and `complexity.json`.
9. **3 Build outputs**: `scripts/render.py --format html` and `--format md` (SKILL.md:377-393)
   — assembles the final page/recap from `analysis.json` + `raw.diff` + all section files +
   the HTML template + `links.json`.
10. **3b Gate rendered output**: `validate_analysis.py --rendered ... --sections ...`
    (SKILL.md:406-410).
11. **4 Splice assets**: `scripts/splice_assets.py` (SKILL.md:424-426) — inlines vendored
    `mermaid.min.js` into the rendered HTML in place of a placeholder comment.
12. **5-8**: open/host/write-PR-description/turn-notes-into-PR-comments — all `gh`/`xdg-open`
    calls, no scripts, except the pasted "Diff notes" text block format is defined by the
    template's own JS (see §4 below) and consumed by hand per references/pr-workflow.md.

References read in full: `references/builder.md` (page shape, escaping rules, why sections are
pasted not regenerated), `references/graphs.md` (complexity/symdelta behavior
and cost), `references/fanout.md` (batch brief, note rules, gate routing), `references/pr-workflow.md`
(PR description splice + Diff-notes-to-gh-comments), `references/recap-mode.md` (cheap route).

## 2. `scripts/render.py`: how the HTML is assembled

**Not a structured-document renderer.** `render.py` interpolates strings into
`assets/diff-review-template.html` via two literal placeholder replacements
(render.py:46-47, 197-198):

```
TITLE_PLACEHOLDER = "<!-- DIFF_TITLE -->"
CONTENT_PLACEHOLDER = "<!-- DIFF_CONTENT -->"
...
page = template.replace(TITLE_PLACEHOLDER, escape(heading))
page = page.replace(CONTENT_PLACEHOLDER, "\n".join(body))
```

`body` (render.py:137-194) is a Python list of literal HTML strings built up procedurally:
facts strip, `What changed`, `Flow` (mermaid), `How it works`, the pasted `symbols`
HTML section file verbatim, the pasted `walkthrough` HTML verbatim, then a footer `div`. There
is no intermediate document object — `render_html` computes and emits HTML strings directly,
mixing computed facts (file/line counts from the diff), escaped/backtick-promoted prose from
`analysis.json`, and byte-for-byte pastes of other scripts' pre-rendered output.

Function signatures (render.py:131-132, 229, 285):

```python
def render_html(analysis, files, template, walkthrough="", links=None, title=None,
                now=None, symbols="", complexity=None, state=None):
def render_md(analysis, files, walkthrough="", links=None, complexity=None, symbols=""):
def overflow_warning(text, walkthrough_text):
```

- `analysis` = the parsed `analysis.json` dict (schema at SKILL.md:112-140).
- `files` = `{path: {"hunks": [...], "added": int, "removed": int}}`, produced by
  `validate_analysis.parse_hunks(diff_text)` (imported at render.py:44) — i.e. render.py
  re-derives file/line stats straight from `raw.diff`, not from `analysis.json`.
- `walkthrough`/`symbols` are raw HTML **strings** (already-rendered section files),
  pasted with `.rstrip("\n")`, not parsed or re-escaped (render.py:175-187).
- `links` = parsed `links.json` dict (schema in §6 below).
- `complexity` = parsed `complexity.json` dict, only used via `complexity.summary_line()`
  (render.py:42, 143-145) for one fact-strip chip.

So today: no JSON "state document" is consumed by the template at load time; the template is
static markup with everything (diff content, notes, section HTML) baked in at render time by
Python string concatenation. The template itself carries no `<script>`-read JSON blob for its
initial state — the walkthrough's diff lines are literal `<pre class="diff">` markup the JS
re-parses from the DOM (see §4).

`scripts/splice_assets.py` (full file read) is a second, much simpler placeholder-fill: one
dict `ASSETS = {"<!-- MERMAID_JS -->": ('class="mermaid"', ["mermaid.min.js"])}`
(splice_assets.py:16-18), same `text.replace(placeholder, js)` mechanism
(splice_assets.py:37), applied to the file on disk in place, guarded by whether the page
actually contains the `needle` marker.

## 3. `scripts/sections.py` and `scripts/walkthrough.py`: existing serializable intermediates

**`sections.py`** now only renders `symdelta.json` into a symbol-delta section (md or html),
never JSON out. `coupling.py`, `structure.py` and `layers.py` are gone, and with them the
`render(kind, data, fmt)` entry point and the `--kind {layers,coupling,structure}` CLI choice
that used to dispatch to it. Entry points:

```python
def render_symbols(data):     # sections.py:551, html
def render_symbols_md(data):  # sections.py:627, md
```

`data` is exactly what `symdelta.py`'s `build_graph()` emits — see §6. With only one analyser
feeding it, `sections.py` has no more `edges_of()`-style normalized intermediate: it works
directly against `symdelta.json`'s own `nodes`/`edges` shape.

**`walkthrough.py`**'s entry points:

```python
def render_html(groups, files, by_path, open_count=DEFAULT_OPEN, links=None, complexity=None,
                 renames=None):                                            # walkthrough.py:321
def render_md(groups, files, by_path, hunks="notes", links=None, max_chars=DEFAULT_MAX_CHARS,
              complexity=None, renames=None):                              # walkthrough.py:432
def story(order, files, groups, symdelta):                                 # walkthrough.py:125
```

`story()` is the one function that builds an actual serializable intermediate:
`[(title, why, [paths])]` — the reading order, blending `analysis.json["groups"]` (judgment)
with `caller_depth()` (mechanical, from `symdelta.json`, now that `coupling.json` is gone) and
`interest_rank()` (mechanical,
tests/lockfiles sink). This list is the natural ancestor of a "state document"'s file/group
ordering, but today it's built fresh on every `walkthrough.py` invocation and immediately
consumed by `render_html`/`render_md` in the same process — never written to disk.

`files` (both scripts) is the same `parse_hunks()` output as render.py's, keyed by path, so
**the diff itself, freshly re-parsed from `raw.diff` on every script invocation, is the closest
thing to ground truth today** — nothing caches or serializes the parsed-hunks structure between
`sections.py`, `walkthrough.py`, `render.py`, and `validate_analysis.py`; each independently
calls `parse_hunks(Path(diff).read_text())`.

`by_path` = `{path: analysis_file_entry}` (walkthrough.py:583), i.e. a dict view directly over
`analysis.json["files"]` — this *is* already a serializable per-file dict (`role`, `hunks: [{header,
note}]`), so per-file/per-hunk judgment already has a JSON home; what's missing is a serialized
form of the mechanical parts (`files` stats, `groups` resolved order, symdelta-derived depth).

## 4. `assets/diff-review-template.html`: per-line commenting and "Copy notes for Claude"

1004 lines total. All commenting logic is inline `<script>` (no external JS file except
vendored mermaid), in five `<script>` blocks (line ranges: 445-590 mermaid init/render,
591-750 pan/zoom, **751-919 the commenting engine**, 920-928 summary-link click guard,
929-1002 the explanations show/hide toggle).

**Storage:** comments live only in an in-memory `Map` for the lifetime of the page —
`const comments=new Map(); // cid -> {span,path,num,side,text,note,order}` (line 755). There is
**no `localStorage`/`sessionStorage`/`indexedDB` use for comments** — a page reload loses every
comment. The only `localStorage` use on the whole page is the unrelated `vd-show-notes` toggle
(explanations on/off), keyed `KEY='vd-show-notes'` (lines 986-997), wrapped in try/catch because
`file://` and a sandboxed share host throw on it.

**Note record shape** (constructed at line 826): `{span, path, num, side, text, note, order,
cid}` — `span` is a live DOM node reference (not serializable as-is), `path`/`num`/`side`
identify the PR location (`side` is `'LEFT'`/`'RIGHT'`), `text` is the diff line's verbatim
text captured at wire time (line 837), `note` is the user's typed comment, `order` is a
document-order integer fixed at wiring time (line 846-847, `docOrder++`) so output/panel order
is stable regardless of edit order.

**How lines become commentable — the load-bearing DOM contract** (also documented in
references/builder.md:83-98): `wireHunk(pre)` (lines 853-868) finds the file path via
`.hunk-path` inside the enclosing `<details class="hunk">`, parses the hunk's own `@@` header
via a regex against the `<span class="h">` (`parseHeader`, lines 758-761), then walks
`pre.children` counting `class="c"` (advances both old/new), `class="a"` (advances new only),
`class="d"` (advances old only) to assign each line its `(path, num, side)`. This DOM markup is
generated by `walkthrough.py`'s `render_html` and is a contract the JS depends on structurally
— merging/reordering spans breaks line numbering silently.

**Copy notes for Claude** (`$('fb-copy').onclick`, lines 874-917): builds a plain-text
`## Diff notes (<n>)` block, one entry per comment in `order`, each rendered as:
```
<n>. <path>:<num> <side>
   line: <text, truncated to 160 chars>
   note: <note>
```
then tries `navigator.clipboard.writeText`, falling back to `document.execCommand('copy')`,
falling back to a manual copy-paste modal (`fb-manual`) — all client-side, no network. This
exact text block format is what SKILL.md step 8 / references/pr-workflow.md:38-55 parse back
out of the user's paste to build `gh api .../pulls/<n>/comments` calls.

The feedback panel (`renderPanel()`, lines 768-787) is a live re-render of `comments.values()`
sorted by `order`, showing count and a delete (`×`) per item — again pure in-memory state, wiped
on reload.

## 5. `scripts/validate_analysis.py`: the contract

Full file read. `validate(diff_text, analysis, recap=False)` (validate_analysis.py:268-366) is
the contract a state document would need to satisfy (or an equivalent check would need to
replace). It re-derives ground truth from `raw.diff` via its own `parse_hunks()`
(validate_analysis.py:131-203, the single owner of diff-parsing reused by render.py and
walkthrough.py) and checks `analysis.json` against it:

- top-level keys present (`target`, `what_changed`, `how_it_works`, `flow_mermaid`, `files`, or
  just the prose subset under `--recap`);
- every path the diff touched appears in `files[]`, no path listed twice;
- every file's `role` non-blank and not a bare banned verb (`_is_bare_verb_role`,
  validate_analysis.py:215-223, `BANNED_ROLE_WORDS` at 92-98);
- every `@@` hunk in the diff appears under its file, no invented/duplicate hunks;
- a non-blank hunk `note` must share a tokenized identifier with that hunk's own added/removed
  lines (`_identifier_pieces`, validate_analysis.py:224-247, camelCase/snake_case aware,
  stopword-filtered);
- global `EMPTY_NOTE_FLOOR = 0.8` (validate_analysis.py:81, checked in `_empty_note_floor` at
  252-267): more than 80% of hunks with empty notes fails the whole gate;
- `groups` (if present) reference only real diffed paths, no path claimed twice
  (`_validate_groups`, validate_analysis.py:369-398);
- with `--rendered`, every diffed path must appear somewhere in the rendered text
  (`check_rendered`, 399-404) and every generated section's `<!-- visual-diff:<kind> -->`
  marker must survive into the rendered output (`check_sections`, 405-422).

Any future state-document schema either has to satisfy every one of these checks (if
`validate_analysis.py` stays the gate) or the checks need porting onto whatever new document
replaces `analysis.json`+`raw.diff` as the pair of inputs.

## 6. One-liner survey: complexity/symdelta/links/fanout

| Script | Diff as input? | Per-file or whole-set | Input | Output |
|---|---|---|---|---|
| `complexity.py` | **Yes**, `--diff` required (complexity.py:544, "for the file list") | Per-file, but scoped to "the worst touched function," not a file total (`analyse(repo, base, head, paths, hunk_ranges, diff_text)`, complexity.py:415) | two refs + raw.diff | JSON: per-file `{jump, peak}` complexity deltas; `summary_line()` renders the fact-strip chip |
| `symdelta.py` | No explicit `--diff` flag — `--repo/--base/--head` only (symdelta.py:883-885); resolves its own diff internally | Two tiers: Go = whole-repo extraction (`go/packages`, cheap); TS/Python/Rust = LSP, scoped only to files the diff changed (`outgoingCalls`/`incomingCalls`) | two refs | JSON via `build_graph()` (symdelta.py:672): `nodes`, `edges`, `moved`, `language`, `reason` |
| `links.py` | **Yes**, `--diff` required (links.py:130) | Per-file/per-hunk, keyed to the diff's own hunks | repo path + raw.diff + head ref (+ optional `--pr`) | JSON via `build(repo, diff_text, head, pr)` (links.py:91): `repo_url`, `pr_url`, `head_sha`, `head_pushed`, `files[]` with `diff_url`/`blob_url`/per-hunk `url` |
| `fanout.py` | **Yes**, `--diff` required for both subcommands (fanout.py:190, 197) | Per-file batches (a file never straddles two batches, fanout.py reference in fanout.md:15) | raw.diff | `split`: `batch-N.diff` + manifest; `merge`: canonicalized `analysis.json` |

`coupling.py`, `structure.py` and `layers.py` are gone: no script here produces an import
graph or a module/layer map anymore. Their generic, non-graph helpers live on inside
`complexity.py` (git/noise-file helpers, the tree-sitter generic parser) and `links.py` (git
helpers, diff parsing), which is why those two files are the only remaining owners of that
code. Only `complexity.py`, `links.py`, and `fanout.py` take the diff directly; `symdelta.py`
takes two git refs and diffs internally, so it would need re-running whole, not incrementally,
unless refactored.

## 7. Existing hashing/caching

No content-addressed cache or fingerprint of analysis output exists anywhere in `scripts/`.
Grep across all of `scripts/*.py` for `cache|hashlib|fingerprint|checksum` turns up only:

- `links.py:88` — `"diff-" + hashlib.sha256(path.encode()).hexdigest()`, used purely to build a
  unique HTML anchor id, not a change-detection fingerprint.
- `complexity.py:167` — `@functools.lru_cache(maxsize=None)` on `_get_parser` (per-process
  parser-instance memoization, not cross-run). This helper moved here from `structure.py`,
  which is gone.
- `symdelta.py:45-127` — `_resolve_cache_dir`/`CACHE_DIR`/`GOCACHE`/`GOMODCACHE`: this caches
  the **compiled Go extractor binary** under `$XDG_CACHE_HOME/visual-diff` (or
  `~/.cache/visual-diff`) plus Go's own build/module caches, to avoid recompiling the extractor
  every run. It does not cache analysis *results* — every `symdelta.py` invocation still
  re-typechecks the whole repo at both refs.
- `symdelta.py:653-659` — `depth_cache`, a same-invocation memo dict for `pkg_depth()`
  recursion, not persisted.

**No per-hunk, per-file, or per-analysis hash exists today.** There is no mechanism that
detects "this hunk/file/section is unchanged since last run" — every script re-derives
everything from `raw.diff` and the two git refs on every invocation. Any hunk-keyed incremental
regeneration is new territory.

`links.json` (per references/pr-workflow.md and SKILL.md:330-333, and confirmed by
`links.py:91`'s `build()` return shape) holds: `repo_url`, `pr_url`, `head_sha`, `head_pushed`
(bool — whether the head commit is actually pushed, gating whether any link is safe to emit),
and `files[]`, each `{path, diff_url (PR Files-changed-tab anchor), blob_url (permalink at head
sha), hunks: [{header/prefix, url (that hunk's new-side line-range on the blob page)}]}`. It is
purely a link-URL cache for one specific rendering pass, not a state/fingerprint store.

## 8. Test suite

`coupling.py`, `structure.py` and `layers.py` are gone, and `test_layers.py`, `test_coupling.py`
and `test_structure_generic.py` went with them. 10 test files remain under `scripts/`, 5554
lines total, all "Self-check for X.py. Assert-based, no
framework" style (no pytest/unittest fixtures) — most build a throwaway git repo under
`tempfile.TemporaryDirectory()`:

| File | Lines | Pins |
|---|---|---|
| `test_splice_assets.py` | 71 | placeholder-fill idempotency, missing-asset error |
| `test_markers.py` | 138 | the PR-body `<!-- visual-diff:start/end -->` splice logic (references/pr-markdown.md is the doc source of truth this test enforces byte-identical) |
| `test_links.py` | 160 | URL building, sha256 anchor ids, `head_pushed` gating |
| `test_fanout.py` | 313 | split/merge batching, rename-map canonicalization |
| `test_complexity.py` | 492 | per-function cyclomatic complexity + nesting-depth chip wording (`complexity_chip`/`depth_chip` semantics tested exhaustively — moved/removed/new-hairy/gone-with-no-removed-key cases) |
| `test_validate_analysis.py` | 583 | every gate rule in §5 above |
| `test_render.py` | 611 | **the exact string-interpolation contract**: both placeholders replaced, escaping-before-backtick-promotion order, mermaid `flowchart LR`→`TB` rewrite, section-note placement, byte-for-byte pasting of section files, overflow-warning math tied to `walkthrough.py`'s own floor marker |
| `test_sections.py` | 854 | mermaid diagram generation (packages/symbols levels, label wrapping, escaping) for the symbol-delta kind |
| `test_walkthrough.py` | 1031 | **the line-numbering DOM contract** (`test_every_diff_line_is_its_own_span_with_the_right_class`, `test_line_kinds_add_up_to_the_spans_the_header_declares`, `test_html_carries_the_markup_the_comment_code_reads` — these three assert exactly the span-class contract the template's JS in §4 depends on), group/reading-order derivation from `symdelta.json`, size-budget demotion, rename handling |
| `test_symdelta.py` | 1301 | delta arithmetic (pure-logic), plus throwaway-repo end-to-end Go extraction (skipped if `go` not on PATH) |

**What is most load-bearing for a `human-review` rewrite, and will break first:**

- `test_walkthrough.py`'s three DOM-contract tests (`test_every_diff_line_is_its_own_span_...`,
  `test_line_kinds_add_up_to_the_spans_...`, `test_html_carries_the_markup_the_comment_code_reads`)
  pin the exact `<span class="a|d|c|h">` per-line markup shape the template's JS parses in §4.
  Any move away from server-rendered `<pre class="diff">` markup toward a JSON state document
  the browser renders client-side invalidates these directly, and would need equivalent
  coverage moved into whatever now emits the per-line markup (client JS, if that's where
  rendering moves).
- `test_render.py`'s placeholder/escaping/paste-verbatim tests assume `render.py` produces a
  fully-formed HTML string via `str.replace`; a "render HTML from a state JSON" design changes
  what `render_html`/`render_md` return and every one of these tests' assertions.
- `test_validate_analysis.py` pins the whole gate contract in §5 against `analysis.json` +
  `raw.diff`; a new state document either has to be checked by an equivalent gate or this test
  suite needs a parallel one.
- `test_markers.py` pins the **separate** PR-description splice format (`<!-- visual-diff:start
  -->`), unrelated to the per-line notes, and should survive most human-review changes
  untouched since it only concerns the markdown PR-description path.
- `test_links.py`, `test_complexity.py`, `test_symdelta.py`, `test_fanout.py`,
  `test_sections.py` mostly pin each analyser's own JSON output shape and are orthogonal to
  the human-review rendering change — they'd only need touching if incremental/hunk-hash
  regeneration changes their CLI contracts (e.g. adding a `--since-hash` filter).
