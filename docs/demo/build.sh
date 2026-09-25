#!/usr/bin/env bash
# Rebuilds the lazygit #5702 demo page from the committed analysis.json: clones the pinned
# lazygit commits fresh and reruns the code-walkthrough pipeline against them, deterministically
# and without any API key. The generated HTML itself is never committed.
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $0 <out-dir>" >&2
  exit 1
fi
out_dir=$1

base=9f6db03fea1f25061cb328eb5a04ad612d0a5015
head=c6b8220772dea2f9a470821166f84c05076e19f8
pr=5702
lazygit_url=https://github.com/jesseduffield/lazygit.git

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
skill="$repo_root/skills/code-walkthrough"
analysis="$repo_root/docs/demo/lazygit-5702/analysis.json"

# Ignore ambient git config (credential helpers, url rewrites) so the clone is reproducible.
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

clone="$work/lazygit"
mkdir -p "$clone"
git -C "$clone" init -q
git -C "$clone" remote add origin "$lazygit_url"
# Fetch head by SHA, not the mutable PR ref, so the build depends only on
# pinned commits (GitHub serves any reachable commit by SHA). Still lands in
# a remote-tracking ref so links.py reports head_pushed true.
git -C "$clone" fetch -q --filter=blob:none --no-tags origin \
  "$base" "+$head:refs/remotes/origin/pr-$pr"

diff_file="$work/raw.diff"
git -C "$clone" diff "$base...$head" > "$diff_file"

python3 "$skill/scripts/symdelta.py" --repo "$clone" --base "$base" --head "$head" \
  > "$work/symdelta.json"
cp "$analysis" "$work/analysis.json"

python3 "$skill/scripts/pipeline.py" prepare --dir "$work" --repo "$clone" --base "$base" \
  --head "$head" --pr "$pr"

head_pushed=$(jq -r .head_pushed "$work/links.json")
if [ "$head_pushed" != "true" ]; then
  echo "links.json: head_pushed is false, $head is not on any remote ref" >&2
  exit 1
fi

page=$(python3 "$skill/scripts/pipeline.py" render --dir "$work" --slug lazygit-5702 \
  --title "Code walkthrough: lazygit #5702" | tail -n1)

mkdir -p "$out_dir/demo"
cp "$page" "$out_dir/demo/lazygit-5702.html"
