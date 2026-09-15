# crisperit-skills

Agent skills I use day to day, packaged so other people can install them.

Right now there is one: **visual-diff**.

## visual-diff

Takes a git diff, a commit range, a branch or a GitHub PR and produces two things:

- a markdown recap you can paste into the PR description
- a self-contained local HTML review page with a mermaid flow diagram, an
  expandable relations graph (modules down to files and classes), an annotated
  diff walkthrough, and per-line comments you can copy back into the PR

Nothing gets published. The page stays a local file unless you ask for it to be
hosted.

Invoke it with `/visual-diff`, or just ask for a visual diff of your changes.

### Prerequisites

- `python3` (stdlib only, no pip install)
- `git`
- `gh`, only if you point it at a PR or want it to post comments
- optional: `go` for the Go symbol extractor, and any LSP server you already
  have for richer symbol graphs. It falls back to plain diff parsing without them.

## Install

### Claude Code

```
/plugin marketplace add crisperit/skills
/plugin install crisperit-skills
```

### Any other harness

Clone the repo and symlink the skills into wherever your harness reads them from:

```
git clone https://github.com/crisperit/skills.git
cd skills
./install.sh ~/.claude/skills        # default if you pass nothing
./install.sh ~/.config/some-harness/skills
```

`install.sh` symlinks rather than copies, so `git pull` updates what you have
installed. A skill is just a directory with a `SKILL.md` in it, so anything that
reads that format will pick it up.

## License

MIT
