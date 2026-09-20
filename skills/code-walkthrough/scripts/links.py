#!/usr/bin/env python3
"""Build GitHub https links for every file and hunk in a diff.

Usage: python3 links.py --repo <path> --diff raw.diff --head <ref> [--pr <number>]

Prints one JSON object so a renderer can turn a file path or a hunk into a link the reader
can click, instead of a path they have to go and find themselves.

Two link shapes, both verified against real GitHub:

  file -> <repo>/pull/<n>/files#diff-<sha256 of the path>
      GitHub anchors each file in the Files changed tab by the sha256 of its path. Checked
      against a live PR page: the computed hash was the only diff- anchor on the page.
  hunk -> <repo>/blob/<head sha>/<path>#L<start>-L<end>
      A permalink at the head commit, with the hunk's new-side line range. It shows full
      surrounding context rather than only the changed lines, which is the point of clicking.

head_pushed is false when the head commit is not on any remote branch yet. Both link shapes
404 in that case, so leave the links out rather than shipping dead ones.

Stdlib only, no network: everything here is computed from the local repo and raw.diff.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent))
from validate_analysis import parse_diff  # noqa: E402  one owner for diff parsing

HUNK_RANGE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
SSH_REMOTE = re.compile(r"^(?:ssh://)?git@([^:/]+)[:/](.+?)(?:\.git)?$")
HTTPS_REMOTE = re.compile(r"^https?://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?$")


def run_git(repo, args):
    import subprocess

    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, errors="replace"
    )


def resolve_base(repo, base, head):
    """The merge base of base and head, matching what `git diff base...head` compares.

    Reading base's own tip instead would attribute anything that landed on the base branch
    since the fork to this change, and hide anything the fork point still had. For a plain
    range like HEAD~3..HEAD the merge base is HEAD~3, so this is a no-op there.
    """
    result = run_git(repo, ["merge-base", base, head])
    if result.returncode != 0:
        return base
    return result.stdout.strip() or base


def repo_web_url(repo):
    """https base URL for the origin remote, or "" when it is not a web host we can shape."""
    result = run_git(repo, ["remote", "get-url", "origin"])
    if result.returncode != 0:
        return ""
    remote = result.stdout.strip()
    for pattern in (SSH_REMOTE, HTTPS_REMOTE):
        match = pattern.match(remote)
        if match:
            host, path = match.group(1), match.group(2).strip("/")
            return f"https://{host}/{path}"
    return ""


def line_range(header):
    """(start, end) on the new side of a hunk, or None for a pure deletion."""
    match = HUNK_RANGE.match(header)
    if not match:
        return None
    start = int(match.group(1))
    count = int(match.group(2)) if match.group(2) is not None else 1
    if count == 0:
        return None
    return start, start + count - 1


def file_anchor(path):
    return "diff-" + hashlib.sha256(path.encode()).hexdigest()


def build(repo, diff_text, head, pr):
    base_url = repo_web_url(repo)
    sha = run_git(repo, ["rev-parse", head]).stdout.strip()
    pushed = bool(run_git(repo, ["branch", "-r", "--contains", sha]).stdout.strip())
    order, hunks = parse_diff(diff_text)

    files = []
    for path in order:
        quoted = quote(path, safe="/")
        entry = {
            "path": path,
            "diff_url": (
                f"{base_url}/pull/{pr}/files#{file_anchor(path)}"
                if base_url and pr else ""
            ),
            "blob_url": f"{base_url}/blob/{sha}/{quoted}" if base_url and sha else "",
            "hunks": [],
        }
        for header in hunks[path]:
            span = line_range(header)
            url = ""
            if entry["blob_url"] and span:
                start, end = span
                url = f"{entry['blob_url']}#L{start}" + (f"-L{end}" if end > start else "")
            entry["hunks"].append({"header": header, "url": url})
        files.append(entry)

    return {
        "repo_url": base_url,
        "pr_url": f"{base_url}/pull/{pr}" if base_url and pr else "",
        "head_sha": sha,
        "head_pushed": pushed,
        "files": files,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--diff", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--pr")
    args = parser.parse_args()

    diff_text = Path(args.diff).read_text(errors="replace")
    print(json.dumps(build(args.repo, diff_text, args.head, args.pr), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
