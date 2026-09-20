# State scout: crisperit-skills — visual-diff rename / PR-sync facts

Facts only, with citations. No design proposals. Repo was not modified.

## 1. Repo state (`/home/crispy/dev/private/crisperit-skills`)

**`git log --oneline -15`:**
```
d1681d9 docs: lead README with the coupling graph
ba254db docs: rewrite README around why visual-diff helps human review
f81048f docs: point install instructions at the renamed repo
71a8e65 feat: add visual-diff skill
```
Only 4 commits total.

**Branch / working tree:** `main`, tracking `origin/main`. `git status` is clean for
tracked files, but shows a long list of **untracked** paths at repo root:
`.bash_profile .bashrc .claude/ .gitconfig .gitmodules .idea .mcp.json .profile
.ripgreprc .vscode .zprofile .zshrc`. These look like stray dotfiles/IDE config
sitting in the repo root (not shell history) — worth noting since the repo root
is also `$HOME`-adjacent tooling; they are untracked so a rename won't touch them,
but they're not `.gitignore`d either. `.gitignore` exists at repo root.

**Remote:**
```
origin  git@crisperit.github.com:crisperit/crisperit-skills.git (fetch/push)
```
Uses the `crisperit.github.com` SSH host alias (matches the `crisperit` GitHub
account per the user's global CLAUDE.md account-per-tree convention).

**`.claude-plugin/marketplace.json`** (only file in `.claude-plugin/`):
```json
{
  "name": "crisperit-skills",
  "description": "Skills I want to share with you",
  "owner": { "name": "crisperit" },
  "plugins": [
    {
      "name": "crisperit-skills",
      "description": "visual-diff: turn a diff, branch or PR into a markdown recap plus a local interactive HTML review page",
      "version": "0.1.0",
      "source": "./",
      "author": { "name": "crisperit" }
    }
  ]
}
```
The plugin `name` is `crisperit-skills` (not `visual-diff`), but the plugin
**description** hardcodes the string `visual-diff:` at the start — a rename to
`human-review` would need to touch this line
(`.claude-plugin/marketplace.json:10`).

**`install.sh`** (repo root, executable bash):
- Symlinks (not copies) every directory under `skills/*/` into a target skills
  dir, default `$HOME/.claude/skills`, or a caller-supplied path (`$1`).
- Uses `basename` of each `skills/<name>/` dir as the install name — so a
  rename of the on-disk directory `skills/visual-diff/` to
  `skills/human-review/` is **all `install.sh` needs** to install it under the
  new name; the script contains no hardcoded skill name itself.

**Every file:line containing `visual-diff` / `visual_diff` / `visualdiff`**
(ripgrep across the whole repo, case-sensitive `visual-diff` had matches; no
`visual_diff` or case-insensitive `visualdiff` hits found beyond those):

- `.claude-plugin/marketplace.json:10` — plugin description text
- `README.md:4,6,13,14,15,16,27,40` — prose + slash-command examples
- `skills/visual-diff/SKILL.md:2` — YAML frontmatter `name: visual-diff`
- `skills/visual-diff/SKILL.md:3` — the `description:` field (long, contains the
  trigger phrase list)
- `skills/visual-diff/SKILL.md:384,392` — example scratchpad filenames
  (`visual-diff-<slug>.html`, `visual-diff-<slug>.md`)
- `skills/visual-diff/SKILL.md:413,458` — references to the `<!-- visual-diff:kind -->`
  / `<!-- visual-diff:start -->` markers
- `skills/visual-diff/references/graphs.md:126-127` — cache dir name
  `$XDG_CACHE_HOME/visual-diff` / `~/.cache/visual-diff`
- `skills/visual-diff/references/pr-markdown.md:55,243,245,270` — scratchpad
  path example and the `<!-- visual-diff:start/end -->` PR-description markers
- `skills/visual-diff/references/pr-workflow.md:29` — same start/end markers
- `skills/visual-diff/scripts/coupling.py:6` — comment pointing to
  `visual-diff/SKILL.md`
- `skills/visual-diff/scripts/render.py:117` — regex literal
  `<!-- visual-diff:walkthrough-floor (\d+) -->`
- `skills/visual-diff/scripts/sections.py:12,32,36,46,60` — marker constants
  (`SYMBOLS_MARKER`, coupling/layers/structure markers)
- `skills/visual-diff/scripts/structure.py:29` — comment pointing to
  `visual-diff/SKILL.md`
- `skills/visual-diff/scripts/symdelta.py:21,50` — comment + the actual
  `Path(...) / "visual-diff"` cache-dir literal (code, not just comment)
- `skills/visual-diff/scripts/validate_analysis.py:23,361` — comment + marker
  handling
- `skills/visual-diff/scripts/walkthrough.py:345,502,531` — marker literals
  emitted into output
- Test files with literal marker strings that would need updating in lockstep
  with the source they test: `test_markers.py:10`, `test_render.py` (12
  occurrences), `test_sections.py` (3), `test_symdelta.py` (3 — these assert
  the cache-dir path equals `.../visual-diff`), `test_validate_analysis.py`
  (3), `test_walkthrough.py` (4).
- **Directory itself:** `skills/visual-diff/` (the whole tree) would need
  renaming to `skills/human-review/`.

Also note: `skills/visual-diff/references/graphs.md` and `symdelta.py` persist
a **cache directory on disk** named `visual-diff` under `$XDG_CACHE_HOME` or
`~/.cache`; a rename changes the cache path, which is a behavior change (stale
cache under the old name) not just a text substitution.

`docs/` contains only 6 PNG screenshots referenced by `README.md` — no text to
rename there.

No occurrences of `visual_diff` (underscore) or `VisualDiff` (PascalCase)
anywhere in the repo.

## 2. GitHub PR review-comment API facts (verified against docs.github.com and live `gh api` calls)

### POST create a review comment
`POST /repos/{owner}/{repo}/pulls/{pull_number}/comments`
(https://docs.github.com/en/rest/pulls/comments?apiVersion=2022-11-28)

- Required body fields for a single-line comment: **`body`**, **`commit_id`**,
  **`path`**, and — for a line-level comment — **`line`** and **`side`**.
- `position` still exists in the schema but docs mark it: *"This parameter is
  closing down. Use line instead."*
- **`start_line`** / **`start_side`**: only for multi-line comments. Docs:
  *"Required when using multi-line comments unless using in_reply_to. The
  start_line is the first line in the pull request diff that your multi-line
  comment applies to."* `start_side` is *"the starting side of the diff that
  the comment applies to. Can be LEFT or RIGHT."*
- **`subject_type`**: *"The level at which the comment is targeted. Can be one
  of: line, file."* When set to `file`, `line` becomes optional.
- **`in_reply_to`**: *"When specified, all parameters other than body in the
  request body are ignored."* — i.e. supplying an existing comment's id turns
  the call into a threaded reply and line/side/path are inherited from the
  parent, not re-specified.

Confirmed live via `gh api -X GET repos/vercel/next.js/pulls/98670/comments`:
a real reply comment returned `in_reply_to_id: 4014625484` with `line: 185`,
`original_line: 185`, `side: "RIGHT"` matching its parent — i.e. replies carry
the same line/side as the comment they reply to.

### GET list review comments
`GET /repos/{owner}/{repo}/pulls/{pull_number}/comments` (same doc page)

Fields actually present on live objects (confirmed via
`gh api repos/octocat/Hello-World/pulls/comments` and multiple `next.js`/
`react` PRs): `id`, `line`, `original_line`, `position`, `original_position`,
`side`, `in_reply_to_id`, `user.login`, `body`, `created_at`, `diff_hunk`,
plus `subject_type`, `start_line`, `start_side`, `commit_id`,
`original_commit_id`, `path`, `pull_request_review_id`, `html_url`, `url`.

**When `line` is null (outdated comments) — confirmed empirically**, e.g.
`vercel/next.js` PR #98661, comment id `4018096690`:
```json
{"id": 4018096690, "line": null, "original_line": 721,
 "position": 1, "original_position": 60, "side": "RIGHT"}
```
Pattern seen consistently across many outdated comments: once the commented
line is no longer part of the current diff (file changed elsewhere further up
the PR, making the original diff position unmappable to the current one),
`line` goes to `null` while `original_line` keeps the line number at the time
the comment was made. `position` was also observed non-null (`1`) on some
outdated comments even though GitHub's own docs mark `position` as
closing-down/deprecated — treat `position`/`original_position` as unreliable
for new logic; use `line`/`original_line` and treat `line === null` as the
outdated signal.

### Resolving a review thread
No REST endpoint exists for this. It requires the GraphQL
**`resolveReviewThread`** mutation
(https://docs.github.com/en/graphql/reference/mutations#resolvereviewthread).
WebFetch could not pull the live mutation body (page renders via JS / doc
fetch returned only the index), but corroborating community sources
(GitHub Docs changelog and `github/community` discussions, e.g.
https://github.com/orgs/community/discussions/204269 and
https://github.com/orgs/community/discussions/44650) confirm: the mutation
takes a `threadId` (the review thread's GraphQL node id, not the REST comment
id) in `ResolveReviewThreadInput`, returns a thread object with `isResolved`,
and as of a documented changelog addition also accepts an optional
`resolutionReason`. Calling it over the GraphQL API (`gh api graphql`) requires
a token with `repo`/Contents:write-equivalent scope. **There is no REST
equivalent** — a two-way sync that needs to resolve threads must call the
GraphQL endpoint, not the REST comments API.

### Rate limits for a poll-based sync
https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api?apiVersion=2022-11-28
- *"All of these requests count towards your personal rate limit of 5,000
  requests per hour."* (15,000/hour for GitHub Enterprise Cloud orgs — not
  applicable here.)
- Check remaining quota via response headers (`x-ratelimit-remaining` etc.,
  preferred) rather than polling `GET /rate_limit`, per: *"When possible, you
  should use the rate limit response headers instead of calling the API to
  check your rate limit."*
- Implication for a poll loop: a naive per-N-seconds poll of
  `GET .../pulls/{n}/comments` is 1 request/poll and cheap; the token in
  `gh auth status` here is a personal token (5,000/hour), not a GitHub App
  installation token, so no special App-specific limits apply.

## 3. Local environment facts

- `python3 --version` → **Python 3.12.3**
- `gh --version` → **gh version 2.100.0 (2026-09-03)**
- `gh auth status`:
  ```
  github.com
    ✓ Logged in to github.com account cristopher-ozone (keyring) — Active account: true
    ✓ Logged in to github.com account crisperit (keyring) — Active account: false
  ```
  Both accounts are authenticated, but the **currently active** `gh` account in
  this shell is `cristopher-ozone`, not `crisperit`, even though this repo
  (`~/dev/private/crisperit-skills`) is in the `~/dev/private/*` tree that the
  user's global CLAUDE.md maps to the `crisperit` account via the `gh` PATH
  shim. This matters only if a plan step invokes plain `gh` outside the shim's
  cwd-detection path (e.g. from a differently-rooted script or subshell not
  under this repo) — the shim reportedly keys off `$PWD`, so `gh` run with cwd
  inside this repo should still get `crisperit`'s token; not independently
  re-verified here beyond the global `gh auth status` account list above.
- **No repo-local git hooks are configured.** `.git/hooks/` contains only the
  standard `*.sample` files ship with git (all inert, `.sample` suffix).
  `git config --get core.hooksPath` returned nothing (default hooks path).
  No `husky` directory, no `.pre-commit-config.yaml`, and no `package.json`
  (so no `prepare`/husky npm hook either) anywhere in the repo.

## 4. Browser constraints for a served review page

- **`file://` pages and cross-origin fetch:** MDN, Same-origin policy
  (https://developer.mozilla.org/en-US/docs/Web/Security/Same-origin_policy):
  *"Modern browsers usually treat the origin of files loaded using the
  `file:///` scheme as opaque origins... the URL specification states that the
  origin of files is implementation-dependent."* An opaque origin serializes
  as the literal string `null` for the `Origin` request header. MDN, the
  `Access-Control-Allow-Origin` header reference
  (https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Access-Control-Allow-Origin):
  *"the origin of resources that use a non-hierarchical scheme (such as data:
  or file:) ... is serialized as null. However, many browsers will grant such
  documents access to a response with an `Access-Control-Allow-Origin: null`
  header, and any origin can create a hostile document with a null origin.
  Therefore, the null value ... should be avoided."* Net effect: a page opened
  via `file://` **can** technically get a browser to send a `fetch()` to
  `http://127.0.0.1:PORT`, but the response will only be readable by the page
  if the server sends a matching `Access-Control-Allow-Origin` — and MDN
  explicitly advises against answering a `null` Origin with
  `Access-Control-Allow-Origin: null`, because *any* origin (including a
  malicious one) can also present as `null`. A `file://`-based two-way-sync
  page therefore cannot safely rely on CORS alone to restrict who can call the
  local sync server.
- **CORS requirements for the local server itself:** MDN CORS guide
  (https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CORS): the server
  must return `Access-Control-Allow-Origin` naming the caller's exact origin
  (e.g. `http://127.0.0.1:<page-port>`) or `*` for non-credentialed requests;
  *"When responding to a credentialed request, the server must not specify the
  `*` wildcard ... but must instead specify an explicit origin."* Non-simple
  requests (custom headers, non-GET/POST, JSON content types beyond the CORS
  "simple" set) trigger a preflight `OPTIONS` that must also be answered with
  `Access-Control-Allow-Methods` / `Access-Control-Allow-Headers`.
- **DNS-rebinding mitigation for a localhost server:** no single canonical MDN
  page covers this (MDN's CORS/security docs don't discuss DNS rebinding by
  name); corroborating sources are security write-ups rather than a spec, e.g.
  Compass Security's DNS-rebinding explainer
  (https://blog.compass-security.com/2021/02/the-good-old-dns-rebinding/) and
  a DNS-rebinding-vs-MCP-servers writeup
  (https://www.straiker.ai/blog/agentic-danger-dns-rebinding-exposing-your-internal-mcp-servers/).
  Consensus mitigation: the local server must validate the **`Host`** header
  (reject anything except `127.0.0.1`/`localhost`(:port)) and/or validate the
  **`Origin`** header on every request against an allowlist, since a
  DNS-rebinding attack changes what the attacker's page can reach but does not
  let the attacker forge the `Host`/`Origin` header sent by the real browser.
  A capability token embedded in the URL (bound to that run) is the other
  commonly cited mitigation, used in addition to header checks, not instead of
  them.

## Sources
- https://docs.github.com/en/rest/pulls/comments?apiVersion=2022-11-28
- https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api?apiVersion=2022-11-28
- https://docs.github.com/en/graphql/reference/mutations#resolvereviewthread
- https://github.com/orgs/community/discussions/204269
- https://github.com/orgs/community/discussions/44650
- https://developer.mozilla.org/en-US/docs/Web/Security/Same-origin_policy
- https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Access-Control-Allow-Origin
- https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CORS
- https://blog.compass-security.com/2021/02/the-good-old-dns-rebinding/
- https://www.straiker.ai/blog/agentic-danger-dns-rebinding-exposing-your-internal-mcp-servers/
- Live `gh api` calls against `octocat/Hello-World`, `facebook/react`, and
  `vercel/next.js` PR comment endpoints (run 2026-09-16) for empirical field
  behavior.
