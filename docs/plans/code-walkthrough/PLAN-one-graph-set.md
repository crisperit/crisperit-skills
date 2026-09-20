# Plan: one graph set everywhere

Status: in progress, split across three parallel agents.

## Why this exists

`PLAN-graph-node-menu.md` built the node menu against the coupling graph: `coupling.py`'s
`ids`/`ids_notests` maps, spliced into `section-explorer.html`'s `.svgbox[data-ids]`. That
graph never reaches the HTML page. `render.py` pastes `section-explorer.html` for the
relations list, but the coupling diagram itself is a markdown-only artifact
(`sections.py --kind coupling --format md`), built for the PR description's three stacked
graphs, not for the page. The menu had no element to attach to.

Rather than wire the menu to a script the HTML page doesn't render, this plan cuts the graph
set down to the two that already are on the page and settles the mismatch by removing the
side the menu can't reach. This plan **supersedes the surface half of
`PLAN-graph-node-menu.md`**: the interaction contract, gestures, badge and accessibility
rules there are unchanged and still the target, but every step that named `coupling.py`'s
`ids` map as the data source is replaced by what follows here. `PLAN-graph-node-menu.md`
stays as the record of that design work; this plan is what actually ships.

## Decided, do not relitigate

- **One graph set everywhere, not three.** Both outputs (the local HTML page and the PR
  markdown recap) carry exactly two graphs: the flow diagram (`flow_mermaid`, already
  page-native) and the symbol delta (`symdelta.py`, packages level then symbols level). The
  module map (`layers.py`, whole-repo directories folded into modules), the file coupling
  graph (`coupling.py`'s own diagram) and the symbol structure graph (`structure.py`) are
  removed as output, on both paths. Markdown's version of the symbol delta ships as two
  fenced mermaid blocks instead of the page's toggle, since markdown has no client-side
  swap.
- **`coupling.py` is deleted outright**, not just unwired from the sections. Nothing left
  after this plan reads `coupling.json` for a graph: the walkthrough's file-reading order,
  which used to come from it, moves to `symdelta.py`'s own symbol edges instead (caller
  before callee, same ordering rule, different source). `structure.py` and `layers.py` are
  deleted with it; the tree-sitter parsing helpers `structure.py` also carried for
  `complexity.py`'s use move into `complexity.py` itself, since it is now the only
  caller.
- **The node menu's data source is `symdelta.py`'s symbol nodes**, not a coupling id map.
  `symdelta.py` already records a `file` field on every symbol node
  (`kind == "symbol"`); a package node has no single file and gets no menu entry. The
  `data-ids` attribute moves from the coupling `.svgbox` (deleted) to each
  `.mermaid[data-level]` element of the symbols graph, one id-to-file map per level (level
  1's is always empty, packages have no file of their own).

## Split across three agents, same session

- **This agent**: `sections.py`, `render.py`, `validate_analysis.py`, `regen.py`,
  `fanout.py`, `complexity.py` and their tests; deletes `layers.py`/`structure.py` and their
  tests; SKILL.md and references; this document.
- **Agent B**: deletes `coupling.py`/`test_coupling.py`; moves the walkthrough's caller-first
  reading order from `coupling.json` to `symdelta.py`'s symbol edges.
- **Agent C**: `assets/diff-review-template.html`'s node menu, reading `data-ids` off
  `.mermaid[data-level]` instead of `.svgbox`, and everywhere else the template assumed a
  coupling graph existed on the page.

## Open questions

- Whether the markdown recap's two fenced symbol-delta blocks read well back to back on a
  large diff, or whether the packages level should collapse to a one-line summary when the
  symbols level already covers the same ground. Unmeasured; revisit once a real PR recap
  exists to look at.
