# crisperit-skills

Agent skills I use on real work, packaged so you can install them. One so far,
code-walkthrough.

## code-walkthrough

Point it at a git diff, a commit range, a branch, a GitHub PR, or an area of
code with no change to it at all, "explain the auth flow", "how does billing
work". It produces a self-contained HTML walkthrough page you open locally.

```
/code-walkthrough                          # working tree plus staged changes
/code-walkthrough this branch              # main...HEAD
/code-walkthrough 123                      # that PR
/code-walkthrough HEAD~3..HEAD
/code-walkthrough explain the auth flow    # diffed against an empty baseline
```

### Why

Agents write code faster than anyone can read it now, so reading and
responding is the bottleneck, not writing. A unified diff is a bad surface to
read on, and it is the surface everyone defaults to.

What you mark on the page comes back as instructions. Hand the whole set to
the agent to act on, or post them to the PR. That loop is the point.

When I review a PR by hand I do the same three things every time, and the
diff helps with none of them:

**1. I estimate coupling by reading what imports what.** That is how you
find the blast radius of a change, and doing it by hand across seven files
is slow and easy to get wrong. code-walkthrough derives the import and call
graph by parsing the code itself, not by asking a model to guess it from
the diff text, and draws it at two levels. Packages first:

![Package level relations](docs/images/coupling-packages.png)

Then the same graph at symbol level, so you can see which new function actually
has fan-out and which are leaves:

![Symbol level relations](docs/images/coupling-symbols.png)

**2. I group the changed files by feature and read them in that order.** A
GitHub file list is alphabetical, which is never the order the change makes
sense in. So code-walkthrough groups the files into themes, puts the theme
a reviewer needs first at the top, and orders files inside a group caller
before callee.

![Walkthrough grouped by feature](docs/images/reading-order.png)

**3. I take notes per line, then send them somewhere.** Comments sit on the
diff lines they are about. The page has no server, so nothing posts itself.
The Comments panel gives you two exits: Copy for agent, which hands the whole
set to the assistant to act on, and Copy gh command, which prints the gh
calls to post them as PR review comments yourself. Nothing leaves the page
unless you copy it.

![Commenting on a diff line](docs/images/line-comment.png)

Anything else a reviewer works out in their head could go on the page instead.
If you have a habit like these three, open an issue.

### The rest of the page

A facts strip, a summary, and a mermaid flow diagram of the change:

![Top of the walkthrough page](docs/images/overview.png)

Each file carries a one-line role, each hunk a note on what the code does, above
the diff itself:

![Annotated file in the walkthrough](docs/images/annotated-file.png)

The screenshots above come from a small demo Python service, on a branch adding
recurring expenses and a budget forecast.

Nothing is published. The page stays a local file unless you ask for it to be
hosted.

### Prerequisites

- `python3`, standard library only, nothing to install
- `git`
- `gh`, only to target a PR or post comments
- optional, for the graphs: `tree-sitter-language-pack`, `go`, or a language
  server, depending on the language. See below.

### Language support

Everything built from the diff itself works in any language: the grouping and
reading order, the annotated walkthrough, per-line comments and the flow
diagram.

The graphs have to parse the code. Python, Go, JavaScript and TypeScript need
nothing installed. `pip install tree-sitter-language-pack` adds the symbol graph
for Rust, Java, Ruby, Kotlin, Swift, Scala, C, C++, C#, PHP, Lua, Elixir and
Julia. The interactive symbol level graph on the page needs more: `go` on
PATH for Go, or `pyright-langserver`, `typescript-language-server` or
`rust-analyzer` for Python, TypeScript and Rust. Where a parser is missing the
graph sections are skipped and the page says so, nothing else changes.

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
