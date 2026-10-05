Judgment rules for the per-thread worker that decides how a resolved GitHub review thread
actually ended. Delivery mechanics (what the worker is handed, where its answer ends up, how a
missing diff is requested and supplied) stay with each caller: `references/resolved-threads.md`
for the agent fan-out and the daemon's thread worker.

Read `<seed path>`. "thread" is the comment and its replies that make up one resolved GitHub
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
   For conversation, deferred, and none, stop here. Answer with the resolution now, in the
   shape below, with commits: [] and files: []. Nothing below applies.

2. Match, only when the gate said commits. Pick every sha in "commits" that plausibly closed
   this thread. Plural is normal: one push can answer several comments, and "extract this" or
   "refactor this" can span several commits and files. Weigh, but do not filter on: the
   commit's author matching the thread's resolved_by, the commit touching the thread's path
   with files or hunks near its line, and the subject mentioning review, the file, or the
   symbol the comment names. The path is a signal, not a requirement: a comment on one file is
   sometimes answered entirely in others. If nothing you can find genuinely reads as the fix,
   write outcome: none instead of forcing a match.

3. Explain, only once you have diffs for every sha you picked in step 2. If "diffs" already
   covers all of them, continue below. If it does not, you cannot finish this pass: answer
   with {"thread_id": "<id>", "need_diffs_for": [sha, ...]} instead of the resolution, and
   stop. The driver fetches those and runs you again with the same seed, this time carrying
   them. Once every diff is in hand, name the files and hunks that actually answer the
   comment, not every file the commit touched: a nine-file refactor may answer one comment in
   three of them. Copy those hunks verbatim. Write one sentence on what the change actually
   did, in the code's own terms. Do not restate the comment: the reader has it open directly
   above your answer. Write null when the diff says it on its face, which is common for a
   one-line mechanical change.

Answer with:
{"thread_id": "<id>", "outcome": "conversation"|"deferred"|"commits"|"none",
 "closing_message": <string or null, only for conversation>,
 "ticket": <string or null, only for deferred>,
 "commits": [sha, ...],
 "files": [{"path": "...", "hunks": ["@@ ...", ...]}],
 "why": <one sentence, or null when the diff says it on its face>,
 "confidence": "high"|"medium"|"low"}

confidence is your certainty in the match, not the gate: high when the ranking signals agree,
low when you picked a sha on one weak signal alone.
