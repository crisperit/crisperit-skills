#!/usr/bin/env python3
"""Split raw.diff into per-file batches for parallel analysis, then merge the results.

  python3 fanout.py split --diff raw.diff --out <dir> [--max-lines 400] [--max-batches 8]
  python3 fanout.py merge --diff raw.diff --prose prose.json \
      --fragments <dir>/fragment-1.json ... --out analysis.json

split writes <dir>/batch-N.diff, one contiguous slice of raw.diff per batch, plus a
<dir>/fragment-N.seed.json skeleton (files/hunks pre-filled from the diff, notes blank) so
the subagent never retypes an `@@` header, and prints a JSON manifest on stdout. Each batch
goes to its own subagent; a batch is small enough to analyse without repo-wide context,
which is why a cheap fast model can do it.

merge concatenates the fragments each subagent wrote back into one analysis.json, ordered
the way raw.diff orders files, with the prose keys from prose.json. A fragment entry that
names a file by its pre-rename path is canonicalized to the new-side path first, since a
cheap model often only sees one side of a rename. It does not check coverage:
validate_analysis.py does that, and it must be run on the merged file.

Fragment shape, one per batch: {"files": [ <analysis.json files[] entries> ]}
prose.json: {"target", "overview", "flow_mermaid", "verdict", "section_notes", "groups"?}
"groups" is carried through only when prose.json has the key at all, never defaulted to ""
like the other prose keys: an empty-string "groups" fails validate_analysis.py's "must be a
list" check, where a missing key is a no-op there and falls into walkthrough.py's catch-all.

Stdlib only, no network.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from validate_analysis import (  # noqa: E402  shares the path-resolution rules
    DIFF_GIT,
    parse_diff,
    parse_hunks,
)

BOUNDARY = re.compile(r"^diff --git ", re.M)
PROSE_KEYS = ("target", "overview", "flow_mermaid", "verdict", "section_notes")
# render.py reads the prose keys with `analysis.get(key) or <empty>`, so each default here has
# to match that falsy shape: section_notes is a dict, the rest are strings. merge() carries
# section_notes through opaquely regardless of what's in it or whether anything reads it yet.
PROSE_DEFAULTS = {"section_notes": {}}


def split_chunks(text):
    """Return [(path, chunk text)] in diff order.

    Boundaries and paths are zipped rather than re-parsed: parse_diff already owns the
    a/ versus b/ side rules for deletes and renames, and duplicating them here is how
    the two drift apart. A count mismatch means the diff has a `diff --git` line this
    module does not understand, so fail loudly instead of mispairing a chunk.
    """
    order, _ = parse_diff(text)
    starts = [m.start() for m in BOUNDARY.finditer(text)]
    if len(starts) != len(order):
        raise RuntimeError(
            f"cannot split: {len(starts)} diff headers but {len(order)} resolved paths"
        )
    bounds = starts + [len(text)]
    return [(order[i], text[bounds[i] : bounds[i + 1]]) for i in range(len(order))]


def batch(chunks, max_lines, max_batches):
    """Greedy contiguous fill. A file never straddles two batches: its notes read better
    written against the whole file's diff."""
    total = sum(chunk.count("\n") for _, chunk in chunks)
    budget = max(max_lines, -(-total // max_batches)) if max_batches > 0 else max_lines
    batches, current, current_lines = [], [], 0
    for path, chunk in chunks:
        lines = chunk.count("\n")
        if current and current_lines + lines > budget:
            batches.append(current)
            current, current_lines = [], 0
        current.append((path, chunk))
        current_lines += lines
    if current:
        batches.append(current)
    return batches


def rename_map(diff_text):
    """Map each renamed file's old-side path to its new-side (canonical) path.

    Reuses validate_analysis's own `diff --git a/X b/Y` matcher instead of a second regex,
    so old/new resolution can't drift from parse_hunks's rules.
    """
    renames = {}
    for line in diff_text.splitlines():
        match = DIFF_GIT.match(line)
        if not match:
            continue
        old = match.group(1) or match.group(2)
        new = match.group(3) or match.group(4)
        if old != new:
            renames[old] = new
    return renames


def do_split(args):
    text = Path(args.diff).read_text(errors="replace")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    _, diff_files = parse_hunks(text)
    manifest = []
    for i, group in enumerate(batch(split_chunks(text), args.max_lines, args.max_batches), 1):
        batch_path = out_dir / f"batch-{i}.diff"
        seed_path = out_dir / f"fragment-{i}.seed.json"
        body = "".join(chunk for _, chunk in group)
        batch_path.write_text(body)
        seed = {"files": [
            {
                "path": path,
                "role": "",
                # A blank "note" is legal here, not just a placeholder: validate_analysis.py
                # accepts it as final for a churn hunk (rename/export/import-repoint/qualifier-drop).
                "hunks": [{"header": h["header"], "note": ""} for h in diff_files[path]["hunks"]],
            }
            for path, _ in group
        ]}
        seed_path.write_text(json.dumps(seed, indent=2) + "\n")
        manifest.append({
            "batch": str(batch_path),
            "fragment": str(out_dir / f"fragment-{i}.json"),
            "seed": str(seed_path),
            "files": [path for path, _ in group],
            "lines": body.count("\n"),
        })

    print(json.dumps(manifest, indent=2))
    return 0


def merge(diff_text, fragment_paths, prose):
    order, _ = parse_diff(diff_text)
    order_set = set(order)
    renames = rename_map(diff_text)
    entries, duplicates, bad = {}, [], []
    for fragment_path in fragment_paths:
        try:
            data = json.loads(Path(fragment_path).read_text())
        except json.JSONDecodeError as exc:
            # Keep scanning instead of raising immediately, so one run reports every bad
            # fragment, not just the first.
            bad.append(f"{fragment_path}: {exc}")
            continue
        files = data["files"] if isinstance(data, dict) else data
        for entry in files:
            path = entry.get("path")
            if path in renames and path not in order_set:
                # A cheap model often names the pre-rename path; canonicalize before the
                # duplicate check below, so two fragments naming the same file under
                # different names still collide.
                path = renames[path]
                entry["path"] = path
            if path in entries:
                duplicates.append(path)
            entries[path] = entry
    if bad:
        raise RuntimeError("\n".join(bad))
    if duplicates:
        raise RuntimeError(f"same file in two fragments: {', '.join(sorted(set(duplicates)))}")

    ordered = [entries.pop(path) for path in order if path in entries]
    # Anything the fragments invented is kept, not dropped, so the gate can name it.
    ordered += list(entries.values())
    prose_out = {key: prose.get(key, PROSE_DEFAULTS.get(key, "")) for key in PROSE_KEYS}
    if "groups" in prose:
        prose_out["groups"] = prose["groups"]
    return {**prose_out, "files": ordered}


def do_merge(args):
    result = merge(
        Path(args.diff).read_text(errors="replace"),
        args.fragments,
        json.loads(Path(args.prose).read_text()),
    )
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    print(f"merged {len(result['files'])} file entries into {args.out}")
    return 0


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)

    split_parser = sub.add_parser("split")
    split_parser.add_argument("--diff", required=True)
    split_parser.add_argument("--out", required=True)
    split_parser.add_argument("--max-lines", type=int, default=400)
    split_parser.add_argument("--max-batches", type=int, default=8)
    split_parser.set_defaults(func=do_split)

    merge_parser = sub.add_parser("merge")
    merge_parser.add_argument("--diff", required=True)
    merge_parser.add_argument("--prose", required=True)
    merge_parser.add_argument("--fragments", nargs="+", required=True)
    merge_parser.add_argument("--out", required=True)
    merge_parser.set_defaults(func=do_merge)

    args = parser.parse_args()
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
