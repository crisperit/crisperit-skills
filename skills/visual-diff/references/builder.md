# How the HTML page is shaped, and why

**Nothing here is a task.** `scripts/render.py` builds the page; SKILL.md step 3 has the
command. This file is background: what the page contains, and the reasons behind the parts that
look arbitrary. Read it when changing `render.py` or the template, not when running the skill.

The only thing a model contributes is `analysis.json` (schema in SKILL.md step 2). Every string
in it follows the caller's style rule: apply the no-ai-slop skill if available, otherwise plain
sentences, no dashes, no filler.

The template carries all CSS, the line-commenting UI and the copy-for-Claude mechanism.
`render.py` replaces `<!-- DIFF_TITLE -->` and `<!-- DIFF_CONTENT -->` and touches nothing else.

## Two independent rules apply to mermaid source, not one

These protect different things. Handling one does not excuse skipping the other.

1. **HTML-escaping protects the page, and covers diff content AND mermaid source.** Every
   line taken from `raw.diff` and placed inside `<pre>`, and the mermaid source placed
   inside `<pre class="mermaid">`, must have `&`, `<` and `>` escaped to `&amp;`, `&lt;`,
   `&gt;` before being written. For diff lines, escape first, wrap in a `<span>` second.
   Diagram labels are built from branch names, file paths and commit subjects, which are
   attacker-influenced when reviewing someone else's PR, so an unescaped label such as
   `<img src=x onerror=...>` becomes a live element as soon as the browser parses the page,
   before mermaid or any other script runs. Mermaid reads the element's text, and the
   browser decodes the escaped entities back to literal characters before mermaid parses
   it, so escaped source still renders correctly.
2. **Mermaid-syntax quoting protects the parse, and is a separate rule.** A label
   containing `(`, `)`, `:`, `#` or a quote must be wrapped in double quotes inside its
   node, for example `A["auth(): early return"]`, or mermaid fails to parse and the page
   shows a raw error box instead of a diagram. Apply this on top of the escaping above, not
   instead of it.

## Linking, from `links.json`

When `links.json` was handed to you and its `head_pushed` is true:

- FILE MAP: wrap each `.path` span's text in `<a>` to that file's `diff_url` (the PR's Files
  changed tab) when there is a PR, otherwise its `blob_url`.
- WALKTHROUGH: the `summary` keeps the plain path, and each `hunk-note` ends with a small
  `<a>` reading `L<start>-L<end>` pointing at that hunk's `url`.
- Nothing else gets linked, and no link opens automatically.

Every `<a>` gets `target="_blank" rel="noopener noreferrer"`: the page is opened from
`file://`, and a link that replaces it costs the reader their marked-up notes. Skip all links
when `head_pushed` is false or `repo_url` is empty, they would 404. The page stays
self-contained either way; only following a link needs the network.

## What the page contains

Order runs orientation, then concepts, then structure, then detail. Every row is either
arithmetic, a string from `analysis.json`, or a section file pasted byte for byte.

| Section | Markup | Source |
|---|---|---|
| Facts strip | `.facts` with up to three `.fact` blocks | file count and net delta from `raw.diff`, the target, `verdict` |
| WHAT CHANGED | `h2` + one `p` per paragraph | `what_changed`, omitted when blank |
| FLOW | `h2` + `.panel.svgbox` wrapping `pre.mermaid` | `flow_mermaid`, omitted when blank |
| HOW IT WORKS | `h2` + one `p` per paragraph | `how_it_works`, omitted when blank |
| RELATIONS | one `p`, then `section-explorer.html` | `section_notes.explorer`, then the file |
| WALKTHROUGH | `h2` + `section-walkthrough.html` | `walkthrough.py` |
| Footer | `.foot` | target, timestamp, PR link from `links.json` |

Notes on the rows that look arbitrary:

- **No FILE MAP section.** A panel listing the same paths directly above the same paths is one
  section too many, so the walkthrough `summary` carries the inventory.
- **RELATIONS gets no `h2` from the renderer.** `section-explorer.html` opens with its own
  `<h2>RELATIONS</h2>`, and adding one produced two headings in a row on a real page. The note
  therefore lands above that heading, which is where the spec always put it; move it below by
  splitting the paste if that ever reads wrong.
- **One relations box, not three graphs.** It carries level buttons for modules, files and
  symbols plus a focus select, so the reader switches level and drills down in place instead of
  scrolling past three diagrams. Markdown still stacks three, because GitHub cannot run the JS.
- **Section files are pasted, never regenerated.** Their escaping is already correct, so
  escaping again renders `&amp;lt;` on the page, and retyping a diagram is how a legend drifts
  from its arrows. `validate_analysis.py --sections` checks each one's marker survived.
- **`flow_mermaid` for a before/after comparison declares the "before" subgraph first.**
  Subgraphs render in declaration order, so a `flowchart TB` puts whichever came first on top.
- **The zoom modal needs no markup help.** `wire()` sets `tabindex` and `role` on every
  `.svgbox` it finds; the copies in the generated markup only matter before the JS has run.

## Why the walkthrough is generated and not typed

The markup is a contract, not a house style. The page's JavaScript reads it to build line
comments: it finds a file's path in the `summary`'s `<span class="hunk-path">`, finds each hunk's
`@@` header as the `<span class="h">` inside `<pre class="diff">`, and counts line numbers by
walking the spans that follow, where `class="c"` advances both sides, `class="a"` advances the
new side only, and `class="d"` advances the old side only.

Merging two diff lines into one span, dropping a header span, omitting the `hunk-path` span, or
using a different class silently breaks that count, and a reader's comment lands on the wrong
line of a real pull request. `walkthrough.py` owns it, with a test asserting that context plus
removed equals the old span each header declares and context plus added equals the new span.

The page has no size budget, so it carries every hunk body. That is where the reader is sent
when the markdown recap dropped them for length. The worked examples below are what the script
emits, kept so a reviewer can see the shape without running it.

## Describe, not judge

One exception: `section_notes` is deliberately a view, labelled as one. Everything else in
`analysis.json` states what the code does and why it is shaped that way, never whether it is
correct or should change. Do not assert defects, rank severity, or recommend changes, and drop
"should", "consider", "worth confirming" and "make sure" from the wording, those phrases turn a
description into a review verdict. When something in the diff genuinely looks broken, say so to
the user in the conversation, not inside the page: a judgment baked into a static HTML file
outlives the conversation and reads as a review that already happened, duplicating what
`core:code-review` exists to do.

## Worked example: the `.bar` blocks that go in a walkthrough `summary`

```html
<div class="panel dstat">
  <div class="row">
    <span class="path">src/auth/session.ts</span>
    <span><span class="add">+42</span> <span class="del">-11</span></span>
    <span class="bar"><i class="a"></i><i class="a"></i><i class="a"></i><i class="d"></i></span>
  </div>
  <div class="row">
    <span class="path">src/auth/token.ts</span>
    <span><span class="add">+6</span> <span class="del">-2</span></span>
    <span class="bar"><i class="a"></i></span>
  </div>
</div>
```

Scale each row's bar against the largest file in the diff (max 10 blocks per row), so one
huge file does not flatten the rest. A file with only deletions still gets `d` blocks; one
with only additions gets `a` blocks.

## Worked example: one `details.hunk` with two hunks

```html
<details class="hunk" open>
  <summary>
    <span class="hunk-path">src/auth/session.ts</span>
    <span class="hunk-stat"><span class="add">+43</span> <span class="del">-11</span></span>
  </summary>
  <p class="hunk-note">Falls back to the refresh token instead of forcing a re-login when
    the access token has expired.</p>
  <pre class="diff"><span class="h">@@ -40,7 +40,12 @@ function loadSession() {</span>
<span class="c">  const token = store.get(&quot;access&quot;);</span>
<span class="d">- if (!token) return null;</span>
<span class="a">+ if (!token) return loadFromRefresh();</span>
<span class="c">  return parse(token);</span></pre>
  <p class="hunk-note">Clears the cached session on the same path, so a stale session
    object never survives a refresh.</p>
  <pre class="diff"><span class="h">@@ -58,3 +63,4 @@ function loadFromRefresh() {</span>
<span class="c">  const session = store.refresh();</span>
<span class="a">+ sessionCache.delete(session.userId);</span>
<span class="c">  return session;</span></pre>
</details>
```

The `summary` carries the per-file orientation (which file this is, how big the change is);
each `hunk-note` carries the per-hunk explanation. A single-hunk file just gets one
`hunk-note`.

Each diff line is a `<span class="a">`, `<span class="d">`, `<span class="h">` or
`<span class="c">` line, escaped per rule 1 above, one line per span, newline inside the
`<pre>` between spans.
