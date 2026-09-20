# Plan: node menu on the coupling graph

Status: shipped. The gesture table below was revised after use: everything is a plain click now, see the note under it.
Design mockup: `/tmp/claude-1000/node-menu-mockup-v3.html` (verified in Chrome, no JS errors).

## Goal

Make the coupling graph navigation into the diff, not just a picture. A node carries a menu with two actions: jump to that file in the walkthrough, and dim everything not downstream of it.

This is the thing Archify (tt-a1i/archify) cannot do. Its graph is authored by the model into typed JSON; ours is parsed from the code, so a node already knows its file.

## Interaction contract

| Surface | Gesture | Result |
|---|---|---|
| Any pointer | click or tap a node | menu opens; same gesture again closes it |
| Any pointer | click or tap the graph background | fullscreen dialog, and the same click opens the menu in there too |
| Keyboard | Tab to a node, Enter or Space | menu opens |
| Any | drag, pinch, wheel | pan and zoom, never opens the menu |
| Any | Escape | closes the menu, clears the focus dimming |

The hover badge, the 500ms long-press and the `contextmenu` handler are gone. Three gestures
for one action meant the phone and the desktop behaved differently for no gain, and the badge
was the only thing a node click did not already cover.

Menu items: the file path as a header, `Go to file`, `Focus downstream`, and `Clear focus` only while a focus is active.

`Go to file` scrolls the walkthrough to that file and flashes its border. Opened from inside the fullscreen dialog it closes the dialog first.

## Decided, do not relitigate

- **Jump to file is the primary action, not dimming.** Dimming is a picture-level feature; jumping is what moves a reviewer toward the code.
- **No hover-only affordance.** Touch browsers keep `:hover` stuck after a tap, so a hover-gated control is either invisible or sticky on a phone. The badge went for that reason; the rule still applies to anything added to the page later.
- **Fullscreen stays.** It is how the graph is readable on a phone at all.
- **No list view.** Tried in mockup v2, rejected: it duplicates the walkthrough's own file list and costs a view switch.
- **No sequence diagram.** Separate idea, separate plan.
- **The menu is an HTML overlay** positioned once when it opens, not an SVG child, so pan and zoom do not have to carry it.
- **A package box gets the menu too.** A package with children renders as `g.cluster`, not `g.node`, and its mermaid dom id is `<renderId>-<id>` with no `-flowchart-` infix -- both verified against the vendored mermaid 11.15. `Focus downstream` is the point of it; the path is a directory, so the file item resolves to the package's first hunk in the walkthrough and reads `Go to first file`.

## Steps

1. **Verify the mermaid id mapping.** `coupling.py` already emits `ids` and `ids_notests` (`{"N0": "app/api/routes.py"}`) at `scripts/coupling.py:591` and includes them in the JSON payload (`:687`). Confirm what the bundled mermaid (`assets/mermaid.VERSION`) puts on the rendered `g.node` element, `id="flowchart-N0-*"` versus a `data-id`, and pick the selector from what it actually emits. This is the only unknown in the plan.

2. **Splice the id map into the page.** The template needs `ids` as JSON to resolve a clicked node to a path, and a path to a walkthrough anchor. Follow the existing placeholder mechanism in `SKILL.md:502` and `scripts/splice_assets.py`.

3. **Tag the nodes after mermaid renders.** In the existing `wire()`/`wireAll()`/`MutationObserver` pass in `assets/diff-review-template.html:856-880`, set `data-path` and `data-file-anchor` on each `g.node` from the spliced map. Nodes with no matching walkthrough file get the menu without `Go to file`.

4. **Badge and menu.** Port from the mockup. The CSS uses only existing tokens (`--panel`, `--border`, `--accent`, `--muted`, `--fs-*`).

5. **Gestures.** The click/drag split already exists: `panZoom()` tracks `moved` with a 4px threshold, sets `suppressClick`, and swallows the trailing click in the capture phase (`assets/diff-review-template.html:786-835`). Add only the long-press timer and the `contextmenu` handler, both cancelled by the same `moved` flag. iOS fires `contextmenu` on long-press too, so guard against a double open, and set `-webkit-touch-callout: none` on the nodes.

6. **Focus dimming.** BFS over the adjacency, built from the same `ids` payload rather than re-derived from the SVG.

7. **Tests.** Extend `scripts/test_coupling.py` for whatever the id map needs, and add a check to `scripts/test_render.py` that a rendered page carries `data-path` on its graph nodes. The gestures themselves are not unit-testable here; verify by hand.

## Accessibility

Right-click and long-press are undiscoverable and unreachable by keyboard, so Tab plus Enter must open the same menu, and the walkthrough's own file list stays the primary navigation. Focus dimming must not be the only way to read the graph.

## Open questions

- Badge placement on small nodes: top-right corner is cramped. Needs a minimum size so it does not vanish when zoomed out.
- 500ms long-press: may want tuning on a real phone.
