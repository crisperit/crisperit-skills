#!/usr/bin/env bash
# Symlink every skill in this repo into a harness skills directory.
# Symlinks, not copies, so `git pull` updates what is installed.
set -euo pipefail

target="${1:-$HOME/.claude/skills}"
src="$(cd "$(dirname "${BASH_SOURCE[0]}")/skills" && pwd)"

mkdir -p "$target"
for skill in "$src"/*/; do
  name="$(basename "$skill")"
  ln -sfn "${skill%/}" "$target/$name"
  echo "linked $name -> $target/$name"
done
