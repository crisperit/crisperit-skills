#!/usr/bin/env python3
"""Build state.json, the one mutable-layer document the page renders from.

Usage:
  python3 state.py --analysis analysis.json --diff raw.diff --out state.json
  python3 state.py --analysis analysis.json --diff raw.diff --links links.json \
      --prior state.json --out state.json

build(analysis, diff_text, links=None, prior=None) is the one function other scripts should
import rather than re-deriving any of this from scratch. It reuses validate_analysis.parse_hunks,
the single owner of diff parsing (render.py and walkthrough.py already do the same), so a rename
or a hunk boundary can never resolve two different ways between the gate and the page.

Stdlib only, no network.
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from validate_analysis import HUNK_PREFIX, parse_hunks  # noqa: E402  one owner for diff parsing

REPO_URL_RE = re.compile(r'^https?://[^/]+/(.+)$')
PR_URL_RE = re.compile(r'/pull/(\d+)$')


def _repo_from_links(links):
    url = (links or {}).get("repo_url") or ""
    match = REPO_URL_RE.match(url)
    return match.group(1) if match else None


def _pr_from_links(links):
    url = (links or {}).get("pr_url") or ""
    match = PR_URL_RE.search(url)
    return int(match.group(1)) if match else None


def _stable_id(repo, pr, target):
    """Never derived from head_sha: it is the localStorage key, and must survive a rebuild at
    a new head commit. A PR keys on repo+pr; a bare comparison keys on the target string."""
    if pr is not None:
        digest = hashlib.sha256(f"{repo or ''}#{pr}".encode()).hexdigest()[:4]
        return f"pr-{pr}-{digest}"
    digest = hashlib.sha256((target or "").encode()).hexdigest()[:8]
    return f"cmp-{digest}"


def _hunk_hash(hunk, blob):
    """Body lines only, never the @@ header, so a pure line-number shift preserves the hash."""
    body = "\n".join(raw for _kind, raw in hunk["lines"])
    return hashlib.sha256(f"{body}\n{blob}".encode()).hexdigest()[:16]


def _note_lookup(analysis):
    """{(path, prefix): note} from analysis["files"][].hunks[], keyed the same way this
    module keys its own hunks so a walkthrough-written note matches its hunk byte for byte."""
    notes = {}
    for entry in analysis.get("files") or []:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        for hunk in entry.get("hunks") or []:
            if not isinstance(hunk, dict) or not path:
                continue
            match = HUNK_PREFIX.match((hunk.get("header") or "").strip())
            if match:
                notes[(path, match.group(1))] = hunk.get("note") or ""
    return notes


def build(analysis, diff_text, links=None, prior=None):
    order, diff_files = parse_hunks(diff_text)
    roles = {e.get("path"): e.get("role") or "" for e in (analysis.get("files") or [])
             if isinstance(e, dict)}
    note_by_key = _note_lookup(analysis)

    files, hunks = [], []
    for path in order:
        entry = diff_files[path]
        blob = entry.get("blob", "")
        files.append({
            "path": path, "role": roles.get(path, ""), "blob": blob,
            "added": entry["added"], "removed": entry["removed"],
        })
        for hunk in entry["hunks"]:
            hunks.append({
                "id": f"{path}\t{hunk['prefix']}",
                "path": path,
                "prefix": hunk["prefix"],
                "hash": _hunk_hash(hunk, blob),
                "note": note_by_key.get((path, hunk["prefix"]), ""),
            })

    set_hash = hashlib.sha256(
        "\n".join(f"{p}\t{h}" for p, h in sorted((h["path"], h["hash"]) for h in hunks))
        .encode()
    ).hexdigest()

    target = analysis.get("target") or ""
    repo = _repo_from_links(links)
    pr = _pr_from_links(links)

    meta = {
        "id": _stable_id(repo, pr, target),
        "target": target,
        "repo": repo,
        "pr": pr,
        "base_sha": (links or {}).get("base_sha"),
        "head_sha": (links or {}).get("head_sha") or None,
        "head_pushed": bool((links or {}).get("head_pushed")),
        "set_hash": set_hash,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dirty": False,
        "dirty_at": None,
    }

    return {
        "meta": meta,
        "files": files,
        "hunks": hunks,
        "notes": list((prior or {}).get("notes") or []),
        "resolutions": dict((prior or {}).get("resolutions") or {}),
        "groups": list(analysis.get("groups") or []),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--diff", required=True)
    parser.add_argument("--links")
    parser.add_argument("--prior", help="a previous state.json, to carry notes[] forward")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    analysis = json.loads(Path(args.analysis).read_text())
    diff_text = Path(args.diff).read_text(errors="replace")
    links = json.loads(Path(args.links).read_text()) if args.links else None
    prior = json.loads(Path(args.prior).read_text()) if args.prior else None

    state = build(analysis, diff_text, links, prior)
    Path(args.out).write_text(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
