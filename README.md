# crisperit-skills

Skills I want to share with you. Agent skills I use on real work, packaged so
you can install them.

One skill lives here today: **visual-diff**.

## visual-diff

Point it at a git diff, a commit range, a branch or a GitHub PR. It produces a
markdown recap for the PR description and a self-contained HTML review page you
open locally.

```
/visual-diff                 # working tree plus staged changes
/visual-diff this branch     # main...HEAD
/visual-diff 123             # that PR
/visual-diff HEAD~3..HEAD
```

### Why

The point is not prettier output. The point is making a human review faster and
more thorough, because that is still where real defects get caught.

When I review a PR by hand I do the same three things every time, and all three
are manual work the diff does nothing to help with:

**1. I group the changed files by feature and read them in that order.** A
GitHub file list is alphabetical, which is never the order the change makes
sense in. So visual-diff groups the files into themes, puts the theme a reviewer
needs first at the top, and orders files inside a group caller before callee.

![Walkthrough grouped by feature](docs/images/reading-order.png)

**2. I estimate coupling by reading what imports what.** That is how you find
the blast radius of a change, and doing it by hand across seven files is slow
and easy to get wrong. visual-diff derives the import and call graph from the
code, not from prose, and draws it at two levels. Packages first:

![Package level relations](docs/images/coupling-packages.png)

Then the same graph at symbol level, so you can see which new function actually
has fan-out and which are leaves:

![Symbol level relations](docs/images/coupling-symbols.png)

**3. I take notes per line, then turn them into review comments.** The page lets
you comment on any diff line and copy the whole set back out, ready to post on
the PR.

![Commenting on a diff line](docs/images/line-comment.png)

Anything else a reviewer does in their head is a candidate for being
materialized on the page. If you have a habit like these three, open an issue; a
review aid that saves a human ten minutes per PR is worth building.

### The rest of the page

A facts strip, a summary, and a mermaid flow diagram of the change:

![Top of the review page](docs/images/overview.png)

Each file carries a one-line role, each hunk a note on what the code does, above
the diff itself:

![Annotated file in the walkthrough](docs/images/annotated-file.png)

Nothing is published. The page stays a local file unless you pass `--pr` or ask
for it to be hosted.

### Flags

| Flag | Effect |
|---|---|
| (default) | both outputs, markdown recap and HTML page |
| `--md-only` | skip the HTML page |
| `--html-only` | skip the markdown |
| `--recap-only` | prose only, no graphs, cheapest run |
| `--pr [<number>]` | also write the recap into the PR description |

### Prerequisites

- `python3`, standard library only, nothing to install
- `git`
- `gh`, only to target a PR or post comments
- optional: `go` for the Go symbol extractor, and any language server you
  already run (`pyright-langserver`, `typescript-language-server`,
  `rust-analyzer`) for the symbol level graph. Without them it falls back to
  plain diff parsing.

The screenshots above come from a small demo Python service, on a branch adding
recurring expenses and a budget forecast.

## Install

<details>
<summary><strong>Claude Code</strong></summary>

```
/plugin marketplace add crisperit/crisperit-skills
/plugin install crisperit-skills
```

</details>

<details>
<summary><strong>Any other harness</strong></summary>

A skill is a directory with a `SKILL.md` in it, so anything that reads that
format picks it up.

```bash
git clone https://github.com/crisperit/crisperit-skills.git
cd crisperit-skills
./install.sh                            # ~/.claude/skills
./install.sh ~/.config/some-harness/skills
```

`install.sh` symlinks rather than copies, so `git pull` updates what you have
installed.

</details>

## License

MIT
