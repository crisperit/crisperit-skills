Judgment rules for the subagent that writes the whole-picture fields `overview`, `verdict`,
`groups` and the flow diagrams, from the fragments (or, on the small-diff route, straight from
the diff) plus the numstat and the graph summaries. Delivery mechanics (what it is handed, what
it writes to, how it replies) stay with each caller: `references/fanout.md` for the fan-out
route, SKILL.md's schema block for the single small-diff subagent, and the daemon's prose worker.

## Overview

Tell it what `overview` is for: a lead of one or two sentences saying what this is (or what the
change is) and who or what uses it, no file names in the lead, then three to five bullets, one
line each, carrying the organizing ideas only. A bullet may name one symbol or path when that
name IS the idea; most should name none -- density, not the markup, is what produced the wall
this schema replaces. Nothing goes below that altitude: the detail a reader wants next already
lives in each file's `role`, each hunk's `note`, and a group's `why`, and reaching for
completeness here is exactly the failure this brief exists to head off.

Worked example, the shape wanted, as prose (it becomes one JSON string, the lead first, then the
bullets, one `\n` between bullets; the renderer tells the lead from the bullets by where the
first `- ` line starts, not by a blank line, so this reads fine with or without one):

    Rate limiting now reads its thresholds from live config instead of compile-time constants,
    so an operator can tighten a limit without redeploying.

    - Thresholds load once at startup and refresh on a config change event
    - A stale config falls back to the last good values rather than zero
    - `RateLimiter` swaps its polling loop for a debounced watcher
    - Metrics moved out to their own package, so the limiter stays free of reporting concerns

Tell it, on the whole brief: every identifier in the prose is copied from the source, never
reconstructed from what a name in that language usually looks like, so an exported
`UIDFromOzoneCookie` is never softened into "a helper" because unexported names are usually
lowercase. A signature change is claimed only when it is visibly in the diff, never because a
function of that name plausibly gained a parameter elsewhere.

**Mark identifiers with backticks** in `verdict`, `overview`, every `role` and every `note`:
paths, function/method/type/class names, config keys, metric names, literal values. Markdown
passes them through as inline code; the HTML renderer promotes them to `<code>` after escaping.
In `overview` specifically, most sentences should carry no backticked symbol at all -- density,
not the markup, is what produced the wall this schema exists to prevent.

## Groups

It also settles `groups`, in story order, from the merged `(path, role)` pairs plus the graph
summaries. This is a deliberate tradeoff: `groups` is the reading order a human follows
and is the least safe field here to hand off, but a `(path, role)` list plus the graph summary is
enough to group from, and it moves 40 to 50 seconds off what the main thread would otherwise
spend writing groups, notes and gate patches by hand.

Write one group per theme, in story order, not just reading order: the list's own order is what
the page's story map draws as stops one after another, so a group earlier in the list reads as
happening earlier in the story. Title it as the theme rather than as a directory.

Keep the groups wide. Aim for three to five whatever the file count, and never more than six: a
group is a theme a reviewer holds in their head, not a stage in the data flow. Tracing the change
end to end and giving each hop its own group is the failure mode here, and it reads as a pipeline
diagram rather than a reading order. A group of one or two files almost always belongs merged
into the neighbour it feeds, and a test file belongs with the code it covers, never in a group of
its own. Under about five changed files, one group is the right answer.

It does not decide the order inside a group; that is derived downstream from the symbol-delta
graph, caller before callee, then by size, tests and generated files last. Leaving `groups` out
entirely turns the whole walkthrough into one such group, and a file it forgets still renders in
a trailing "Everything else" group, so a partial grouping is safe to ship. The gate rejects a
group with no title, a path not in the diff, or a file in two groups.

## Flow diagrams

Tell it each group may also carry its own `flow_mermaid`: one small diagram, `sequenceDiagram` or
`flowchart LR`, its pick per group. Default to a sequence; fall back to a flowchart only when the
group genuinely has no order to show, a theme like error handling or config plumbing where
participants and an ordered exchange would have to be invented. At most about 8 steps, and omit
the field rather than draw something it had to guess.

And about the top-level `flow_mermaid` it now writes itself: leave it blank when it settles on
two or more groups (the story map replaces it), but when it settles on exactly one group, copy
that group's own `flow_mermaid` (if it wrote one) up to the top level too, since a single group
has no map to carry it instead.

**`flow_mermaid` node labels are 2 to 6 words naming the step**, not a sentence explaining it; the
reasoning belongs in `overview`. **An edge carries a label only when the arrow itself is the
action or transition**, 1 to 4 words, verb-led; a plain sequential step needs none. A group's own
`flow_mermaid` follows the same two rules.

## `hop` and `side`

Tell it about the two fields that drive the story map. `hop`, 2 to 6 words with one backticked
identifier that has to appear in `raw.diff`, names the hand-off to the NEXT group in the list --
the last group that isn't a `side` group must not have one, since there is no next stop for it
to name. `side: true` marks a supporting group (docs, dev setup, anything that doesn't advance
the story) that the map lists off the main line rather than in the chain.
