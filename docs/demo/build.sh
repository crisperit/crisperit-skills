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
python3 "$skill/scripts/complexity.py" --repo "$clone" --base "$base" --head "$head" \
  --diff "$diff_file" > "$work/complexity.json"
python3 "$skill/scripts/validate_analysis.py" --diff "$diff_file" --analysis "$analysis"
python3 "$skill/scripts/structure.py" --repo "$clone" --base "$base" --head "$head" \
  --symdelta "$work/symdelta.json" --analysis "$analysis" --out "$work/structure.json"
python3 "$skill/scripts/sections.py" --kind structure --data "$work/structure.json" \
  --format html > "$work/section-structure.html"
python3 "$skill/scripts/sections.py" --kind symbols --data "$work/symdelta.json" \
  --format html > "$work/section-symbols.html"

links="$work/links.json"
python3 "$skill/scripts/links.py" --repo "$clone" --diff "$diff_file" --head "$head" --pr "$pr" \
  > "$links"
head_pushed=$(jq -r .head_pushed "$links")
if [ "$head_pushed" != "true" ]; then
  echo "links.json: head_pushed is false, $head is not on any remote ref" >&2
  exit 1
fi

python3 "$skill/scripts/walkthrough.py" --analysis "$analysis" --diff "$diff_file" --format html \
  --symdelta "$work/symdelta.json" --complexity "$work/complexity.json" \
  > "$work/section-walkthrough.html"
python3 "$skill/scripts/state.py" --analysis "$analysis" --diff "$diff_file" --links "$links" \
  --out "$work/state.json"

out_html="$work/out.html"
python3 "$skill/scripts/render.py" --analysis "$analysis" --diff "$diff_file" --format html \
  --template "$skill/assets/diff-review-template.html" \
  --walkthrough "$work/section-walkthrough.html" --state "$work/state.json" \
  --symbols "$work/section-symbols.html" --structure "$work/section-structure.html" \
  --links "$links" --title "Code walkthrough: lazygit #5702" > "$out_html"

python3 "$skill/scripts/validate_analysis.py" --diff "$diff_file" --analysis "$analysis" \
  --rendered "$out_html" --sections "$work"/section-*.html

python3 "$skill/scripts/splice_assets.py" "$out_html" --skill "$skill"

mkdir -p "$out_dir/demo"
cp "$out_html" "$out_dir/demo/lazygit-5702.html"
