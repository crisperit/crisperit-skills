# Plan: remove served mode, then fix the commenting UX built on top of it

Successor to `PLAN.md` and `PLAN-loop-and-portability.md`. Phase A of this plan is done: served
mode is gone. Phase B, not yet built, is recorded here so the reasoning survives past this
session.

## Why

The review page has always been used as a plain local file. `serve.py` (a stdlib localhost
server, a per-run token, Host/Origin checks, a `serve.json` singleton, a 30-minute idle exit)
and the page's own served branch (`window.__HR`, a 4-second `POST`/poll loop, `enqueueRequest`/
`pendingRequestFor`, a `Resolve` button on a posted thread) never actually ran in that mode.
`notes.py`'s `watch`/`answer` subcommands and the `requests[]` queue existed only to drain what
the served page could queue. None of it was load-bearing; all of it was a second code path to
keep in sync with the one that runs.

The commenting UX built on top of that served design carried three problems of its own, independent
of the served/static split, worth fixing once served mode is out of the way:

1. **No replies.** A note can be posted, but there is no way to answer one already on the page.
2. **An unwanted per-note "Post to PR" mark.** Marking readiness to post is a separate click from
   writing the comment, and a marked note just sits there labelled "queued" until the agent is
   told to look.
3. **A marked-comments banner stuck at the top of the page.** `hr-copy-agent` is a fixed
   above-the-fold banner the reader has to see and dismiss mentally on every page, whether or not
   they have written a comment yet.

## Phase A: remove served mode (done)

Deleted outright, not deprecated:

- `skills/human-review/scripts/serve.py` and `scripts/test_serve.py`.
- In `diff-review-template.html`: the `SERVED` constant and every `window.__HR` branch,
  `postPartial`, `POLL_MS` and the `refresh`/`setInterval` poll, `enqueueRequest`,
  `pendingRequestFor`, and the served-only `Resolve` button on a posted note. Note state is now
  localStorage-only on a single code path: `loadState` no longer branches on `SERVED`,
  `saveNote`/`deleteNote` always call `persistLocal()`. `noteSig` dropped its
  `pendingRequestFor` element. The `hr-dirty` banner stayed; `regen.py` still computes
  `meta.dirty` independently of any server.
- In `scripts/notes.py`: the `watch` and `answer` subcommands and the `requests[]` queue they
  drained, including the request-closing side effect `do_deliver` used to perform and the module
  docstring paragraphs describing both. `publish_requested` is no longer a gate:
  `pending_publish_ids`/`payloads_for` now select every non-stale local draft, not only one
  marked with the flag, since nothing left in this phase still needs the flag to decide what is
  ready. `merge_state` and the constants it needs (`PAGE_DENIED_NOTE_FIELDS`, `VALID_ID_RE`)
  moved from `serve.py` into `notes.py`, dropping the `requests[]` half of the merge and every
  part that only served the HTTP route; `import` calls it directly instead of importing it from
  a module that no longer exists.
- In `scripts/state.py`: the built state no longer has a `requests` key.
- Tests: the watch/answer/requests cases are gone from `test_notes.py`; the `merge_state`
  page-origin deny-list coverage from the old `test_serve.py` (the behaviour `import` still
  relies on) moved into `test_notes.py`, adapted to call `merge_state` directly instead of over
  HTTP; `test_state.py`'s `--no-serve` docstring reference and its `requests[]` carry-forward
  tests are gone, replaced by one assertion that the built state has no `requests` key at all.
- Docs: `SKILL.md` step 5 lost the `serve.py` launch and the tunnel/ngrok block, the
  `--serve`/`--no-serve` flags are accepted and ignored, step 8 is import/deliver/submit only,
  step 9 (the watcher that posted a comment on each click) is gone, and step 7's "Tell the user"
  text stopped promising click-to-post. `references/pr-workflow.md`, `PLAN.md`, `contracts.md`
  and `state.md` had the same served-mode content cut, without a wholesale rewrite.

**Check:** `python3 -m pytest scripts/ -q` under `skills/human-review/`, green except two
pre-existing `test_layers.py` failures unrelated to this change (`layers.py` was not touched).
No live reference to `serve.py`, `watch`, `answer`, `requests[]` or `publish_requested` survives
in `skills/human-review/` or `docs/plans/human-review/`, aside from the deliberate historical
mentions in `PLAN.md`'s Phase 3 heading and grilled-decisions record, and this file's own record
of what was deleted.

---

## Phase B: replies, inline drafts, a bottom-right comments panel, and resolving a conversation

Done. Six pieces, five in `diff-review-template.html` and one in `notes.py`. Two of them --
(a)'s persistent reply box and (e), resolving a conversation -- were added after this record was
first written, once the user pointed at GitHub's own thread UI as the target; (a) below
describes what was actually built, not the earlier per-note `Reply` button design.

### (a) GitHub-shaped threads: a persistent reply box, not a button

Every thread whose root is not one of my own unposted drafts (a synced GitHub comment, a bot
comment, or one of my own already-posted notes) gets a persistent one-line `Reply...` input at
the bottom of the thread, always visible -- no button to click to reveal it, matching the
screenshot of GitHub's own thread UI the user gave as the target. Typing into it and committing
(blur, or Cmd/Ctrl+Enter) creates a new local draft with `reply_to` set to the root's id and
`path`/`line`/`side` copied from the root (a reply never anchors independently, matching how
`sync_comments` already threads a synced reply onto its parent), then clears the box for the
next reply. The new draft renders itself inline via (b) below, so no separate editor or focus
call is needed for it.

`elementsForThreads` is the one place a run of notes turns into DOM: it groups an
already-thread-ordered array into `{root, notes}` runs (`groupThreads` -- a thread's whole
subtree is contiguous in `orderThreaded`'s output, so no recursion up the reply chain is
needed), then either collapses the group behind (e)'s resolved wrapper or renders its rows plus
whichever of the reply box (`replyBoxFor`) and Resolve button (`resolveBtnFor`, see (e)) apply to
that thread's root. A fresh reply's `order` is one past the highest `order` in `state.notes`, not
copied from the root: copying the root's own order tied a new reply with the root itself and
could sort it ahead of an older sibling reply whose order was already higher.

Deviation from the original record: `merge_state`'s `PAGE_DENIED_NOTE_FIELDS` includes
`reply_to` -- "only the agent may set it" -- so a page-created reply's `reply_to` is silently
stripped back to `null` the moment it round-trips through `notes.py import`. `deliver_note`
already falls back to `note.get("in_reply_to")` when `reply_to` is absent, and `in_reply_to` is
not on the deny list, so every reply draft now carries the parent id on *both* `reply_to` (read
by the page itself: `orderThreaded`, the `.reply` CSS class, threading in general) and
`in_reply_to` (the alias that survives the import). `payloads_for`/`pending_publish_ids` (the
superseded REST path, and the "ready to post" line `import` prints) still only reads `reply_to`,
so the browser's own mirror of that predicate (`notesForAgent`) checks `n.reply_to||n.in_reply_to`
to stay in agreement with what `deliver` will actually do. No Python file changed.

### (b) Inline draft editing

A row for one of my own drafts renders its body in a `<textarea>` rather than as static text,
and autosaves on debounced input and on blur, so there is no separate save step for an edit. The
`Edit` button goes; `×` (delete) stays. `rowFor`'s reconciler skips rebuilding a row that
currently has focus inside it (`cur.el.contains(document.activeElement)`), which replaced
`openLineKey` outright rather than growing it into a set: with a draft rendering its own
textarea in place, the separate uncommitted-editor row `openLineKey` was protecting no longer
exists. Clicking a diff line now just creates an empty draft (or focuses the existing one) and
lets this same inline editing handle the rest; `openEditor`, `clearLineRow`, `.dline-edit` and
`.dline-comment` (already-dead CSS, see below) all went with it. A reply is typed in the exact
same place, since it is just another draft.

### (c) A bottom-right Comments panel, replacing the top banner

A fixed `Comments (n)` button, bottom-right, opens a native `<dialog>` (the same pattern
`#mermaid-zoom` already uses for the flow diagram) listing my comments in anchor order.
Activating one opens its ancestor `<details class="hunk">` (`openAncestorDetails`, already
written for the `n`/`N` keyboard-cycle handler) and focuses that comment's row. This replaces two
things at once: the per-note `Post to PR` button (marking readiness moves into the panel, item
(d) below) and the page-top `hr-copy-agent` banner (its job moves into the same panel).

### (d) Publishing from the panel

The panel gets two buttons instead of the removed per-note mark and page-top banner:

- **Copy gh command** builds a shell block client-side from `meta.repo`, `meta.pr` and
  `meta.head_sha`: one `gh api repos/<repo>/pulls/<pr>/comments --input - <<'HR'` heredoc per
  comment, each carrying `{body, commit_id, path, line, side}` for a fresh top-level comment or
  `{body, in_reply_to}` for a reply to a comment that is already synced (has a `gh_id`). This is
  the fast path when `meta.pr` is set: no round trip through the agent, the user runs the printed
  block themselves.
- **Copy for agent** keeps the existing JSON payload shape for `notes.py import`. This stays the
  only path when `meta.pr` is null (there is no PR to build a `gh api` command against), and it
  is the only one with anchor resolution (`deliver`'s `postable_ranges`/`resolve`) and a
  single-review `submit` at the end, so it remains the recommended path even when a PR exists.

Copy gh command's block also carries one `gh api graphql --input - <<'HR'` heredoc per thread
the reader marked resolved (`pendingResolveSet`, see (e)): `{query: RESOLVE_THREAD_MUTATION,
variables: {id: <gh_thread_id>}}`, the thread id sent as a GraphQL variable rather than
interpolated into the query string, the same rule `notes.py`'s own `gh` calls already follow.

### (e) Resolving a conversation

Added after the rest of this phase, once GitHub's own thread UI became the explicit target.
Each thread whose root has a `gh_thread_id` (something has actually posted -- nothing to resolve
otherwise, so no button) gets a `Resolve conversation` button at its bottom. Resolving on GitHub
addresses a review *thread* node id, which the REST `pulls/<n>/comments` payload `sync` consumes
never carries, so this is a second, additive `notes.py` sync pass rather than a rewrite of the
working REST one:

- `notes.py sync-threads --state state.json` reads a `gh api graphql` response for
  `REVIEW_THREADS_QUERY` (documented in the module docstring and in `SKILL.md` beside the
  existing `sync` call) on stdin, walks `repository.pullRequest.reviewThreads.nodes` for `id`,
  `isResolved`, and each thread's `comments.nodes[].databaseId`, and `sync_threads` maps every
  thread onto the notes whose `gh_id` matches one of those `databaseId`s, setting `gh_thread_id`
  and `resolved`. Idempotent like `sync`, matched by `gh_id`. `first: 100` on both connections
  rather than `--paginate`: the nested `comments` connection has its own cursor that `--paginate`'s
  single outer cursor does not reach, so a PR past that size needs a second call, not attempted
  here.
- The page's `Resolve conversation` click has no server to post to either: it adds the thread
  root's id to a `pendingResolveSet`, persisted to `localStorage` (`hr-resolve:<meta.id>`,
  the same per-review-target key convention `hr-notes:<meta.id>` already uses), and
  `elementsForThreads` renders that thread collapsed behind `resolvedWrapFor`'s `<details
  class="hr-thread-resolved">` -- the same idiom as the `.hr-lo` stray fold -- with a "Resolved
  (pending sync)" summary until a later `sync-threads` sets `resolved: true` on the root, at
  which point the summary drops to "Resolved" and the pending flag is cleared (it has nothing
  left to confirm). Replying and re-showing the Resolve button on an already-collapsed thread
  were both left out on purpose: GitHub's own "reopen by replying" behaviour was not asked for
  and would have doubled the scope of this piece for no request behind it.

## Check

`test_state.py` got the source-pin tests the record called for, updated for the persistent reply
box and the two additions: the reply box exists and is withheld under my own unposted drafts
(`test_thread_reply_box_is_persistent_and_withheld_under_my_own_unposted_drafts`); the removed
`Post to PR` button and `hr-copy-agent` banner leave no trace
(`test_no_trace_of_the_removed_top_banner_or_per_note_post_to_pr_mark`); the removed separate
line editor leaves no trace (`test_no_trace_of_the_removed_separate_line_editor`); a
`<dialog id="hr-comments">` and its `Copy gh command` control exist
(`test_template_has_the_comments_panel_and_its_gh_command_control`); a draft renders a
`<textarea>` with no `Edit` button
(`test_own_draft_renders_an_editable_textarea_with_no_edit_button`); a thread's Resolve
conversation button only appears once it has a `gh_thread_id`
(`test_resolve_conversation_button_only_appears_once_a_thread_has_a_gh_thread_id`); a resolved
thread collapses into a `<details>` summary
(`test_resolved_thread_collapses_into_a_details_summary`). `test_notes.py` got `sync-threads`
coverage in the same assert-based style as the rest of the file: a thread mapping onto several
comments (`test_sync_threads_maps_one_thread_onto_several_comments`), a comment with no matching
note left alone (`test_sync_threads_leaves_a_comment_with_no_matching_note_alone`), and an
idempotent second run (`test_sync_threads_is_idempotent`), plus one round-trip through
`do_sync_threads`'s stdin shape.

`rowFor`/`elementsForThreads` not tearing down a focused element stayed the honest gap this
record already flagged for `rowFor` alone: no DOM/browser-driven test exists in this
stdlib-only suite, so it was verified with a small jsdom harness against the real template
instead of by hand -- see the implementation report for exactly what was clicked through (a
reply nesting under its parent, a draft autosaving and surviving a simulated reload, Resolve
collapsing a thread and the copied block carrying its mutation, the panel jumping to a line, and
the copied `gh` block passing `bash -n`).

`python3 <script> for every test_*.py` under `scripts/` is green except the same two pre-existing
`test_layers.py` failures Phase A already carried forward, confirmed unrelated since neither
`layers.py` nor `test_layers.py` was touched by this phase either.
