#!/usr/bin/env python3
"""Check an analysis.json against raw.diff so no file or hunk can be skipped.

Usage:
  python3 validate_analysis.py --diff raw.diff --analysis analysis.json
  python3 validate_analysis.py --diff raw.diff --analysis analysis.json --rendered out.md
  python3 validate_analysis.py --diff raw.diff --analysis analysis.json --recap

raw.diff is the source of truth: every file it touches must appear in analysis.json, and
every `@@` hunk of that file must appear under it. A hunk's note may be blank -- churn
detection turned out not to be reliably decidable from the diff alone (see EMPTY_NOTE_FLOOR
below), so the gate no longer second-guesses which hunks earn one. A non-blank note must
share an identifier with its own hunk's added/removed lines, so a note can't be invented
without reading the hunk. Across the whole diff, too many blank notes still fails: see
EMPTY_NOTE_FLOOR.
With --rendered, also checks the produced recap mentions every file path.

--recap is for the cheap prose-only mode, whose analysis.json has no files[] entries at
all: it skips every per-file and per-hunk check and instead requires "groups" to cover
every path in the diff exactly once, since groups is the only thing carrying per-file
coverage on that path.

Schema (see human-review/SKILL.md step 2):

  {
    "target": "master...HEAD",
    "what_changed": "2 to 4 sentences on what the change accomplishes and why",
    "how_it_works": "machinery a cold reader needs, or \"\" when nothing needs it",
    "flow_mermaid": "flowchart LR ... , or \"\" when there is no flow worth drawing",
    "files": [
      {
        "path": "pkg/thing.py",
        "role": "one line: what this file's change is for",
        "hunks": [{"header": "@@ -12,7 +12,9 @@", "note": "one line on this hunk"}]
      }
    ],
    "groups": [
      {
        "title": "Hot-reload the rate-limit config",
        "why": "optional one line on why this theme reads here",
        "paths": ["pkg/thing.py"]
      }
    ]
  }

A binary or mode-only change has an empty "hunks" list; the file entry is still
required, so the reader can tell "nothing to show" from "forgot to look".

"groups" is optional and carries the reading order: which files form one theme, and
which theme a reviewer should read first. Only the grouping and the group order are
judgment; the order within a group is derived from the import graph. A file left out
of every group still renders, in a trailing catch-all, so a forgotten group cannot
hide it from the reader.

Stdlib only, no network. Exits 1 and prints one plain line per problem.
"""

import argparse
import json
import re
import sys
from pathlib import PurePosixPath

DIFF_GIT = re.compile(r'^diff --git (?:"a/(.+)"|a/(\S+)) (?:"b/(.+)"|b/(\S+))$')
HUNK_PREFIX = re.compile(r"^(@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@)")
# The post-image blob sha, used by state.py to invalidate a file's hunk hashes when anything
# in the file changes, not just the edited hunk.
INDEX_LINE = re.compile(r"^index [0-9a-fA-F]+\.\.([0-9a-fA-F]+)")
REQUIRED_KEYS = ("target", "what_changed", "how_it_works", "flow_mermaid", "files")
# --recap has no files[] at all (see module docstring), so it only owes the prose keys.
PROSE_KEYS = tuple(key for key in REQUIRED_KEYS if key != "files")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
MIN_IDENTIFIER_LEN = 4
# Per-hunk churn detection (rename/export/import-repoint/qualifier-drop vs. "real" change)
# is not reliably decidable from a language-independent diff read: on a measured 45-file
# analysis it produced 49 false rejections, each one genuine churn a subset-of-tokens test
# misread as new (a type rename, a package substitution, a bare added import line). A global
# floor catches the failure mode that's actually worth catching -- an agent blanking almost
# every note -- for the cost of one division instead of per-hunk cleverness.
EMPTY_NOTE_FLOOR = 0.8
# Syntax in more than one of C-family/Python/Go, so matching one proves nothing about a hunk.
# Extend only with another word that's syntax in two or more of those; a per-language table
# belongs in symdelta.py, not here.
STOPWORDS = frozenset({
    "return", "else", "elif", "null", "nil", "none", "true", "false", "this", "self",
    "func", "function", "class", "struct", "import", "from", "with", "case", "break",
    "continue", "default", "error", "string", "void", "bool", "type",
})
# A role that is only one of these repeats what the diff header, the +/- counts and the
# rendered "Moved from" marker already show. See _is_bare_verb_role for the match rule.
BANNED_ROLE_WORDS = frozenset({
    "moved", "deleted", "modified", "updated", "changed", "added", "removed", "renamed",
    "refactored",
})
# Sole owner of test-path classification: walkthrough.py's hunk ordering and symdelta.py's
# graph edge filtering both import this, so a change to what counts as a test path can't
# drift between the two.
_TEST_DIR_SEGMENTS = frozenset({"tests", "test", "spec", "specs", "__tests__"})
_TEST_FILENAME_PATTERNS = (
    re.compile(r"^test_.*", re.IGNORECASE),  # pytest / unittest
    re.compile(r".*_test\..+$", re.IGNORECASE),  # Go / Ruby / Python suffix
    re.compile(r".*\.test\..+$", re.IGNORECASE),  # JS/TS (jest, vitest)
    re.compile(r".*\.spec\..+$", re.IGNORECASE),  # JS/TS (jasmine, karma)
    re.compile(r".*_spec\..+$", re.IGNORECASE),  # RSpec
    re.compile(r".*\.e2e-spec\..+$", re.IGNORECASE),  # e.g. mcp-auth.e2e-spec.ts
    re.compile(r".*\.tests\.ps1$", re.IGNORECASE),  # PowerShell Pester
    re.compile(r"^conftest\.py$", re.IGNORECASE),  # pytest fixtures
    # Java/C#/Swift: an uppercase-led Test(s) right before the extension, so lowercase
    # mid-word hits like "greatest.cs"/"contest.java" do not match.
    re.compile(r".*Test\.java$"),
    re.compile(r".*Tests\.java$"),
    re.compile(r".*Tests\.cs$"),
    re.compile(r".*Tests\.swift$"),
)


def is_test_path(path):
    """Classify a path as a test path: segment-aware and suffix-aware, never
    substring-aware, so "latest/x.py", "src/contest.py" and "src/greatest/x.py" stay
    non-test."""
    if not path:
        return False
    norm = str(path).replace("\\", "/")
    if any(segment.lower() in _TEST_DIR_SEGMENTS for segment in PurePosixPath(norm).parts):
        return True
    filename = PurePosixPath(norm).name
    return any(pattern.match(filename) for pattern in _TEST_FILENAME_PATTERNS)


def parse_hunks(text):
    """Return (ordered paths, {path: file dict}) from a unified diff, bodies included.

    One traversal owns diff path resolution for every consumer, so a rename or a `+++ ` inside
    a hunk body resolves the same way in a generated walkthrough as in the check that the
    walkthrough covered every file.

    A file dict is {"hunks": [{"prefix", "header", "lines": [(kind, raw line)]}],
    "added", "removed", "blob"}, where kind is "a", "d" or "c" and the raw line keeps its
    leading marker, and "blob" is the post-image sha from the `index` line, "" when absent
    (binary/rename-only entries). A `\\ No newline at end of file` marker is dropped: it
    advances neither side of the diff, so emitting it as one of those three would put every
    line comment below it on the wrong line.
    """
    order = []
    files = {}
    a_path = current = None
    prev = ""
    in_hunk = False

    for line in text.splitlines():
        match = DIFF_GIT.match(line)
        if match:
            a_path = match.group(1) or match.group(2)
            current = match.group(3) or match.group(4)
            if current not in files:
                order.append(current)
                files[current] = {"hunks": [], "added": 0, "removed": 0, "blob": ""}
            prev = line
            in_hunk = False
            continue

        # Only a `+++` directly after a `---` is a header; an added line whose content
        # starts with "++ " produces the same three characters inside a hunk body.
        if line.startswith("+++ ") and prev.startswith("--- ") and current is not None:
            if line[4:].strip() == "/dev/null" and a_path and a_path != current:
                order[order.index(current)] = a_path
                files[a_path] = files.pop(current)
                current = a_path
            prev = line
            continue

        if current is not None and not in_hunk:
            match = INDEX_LINE.match(line)
            if match:
                files[current]["blob"] = match.group(1)
                prev = line
                continue

        if current is not None:
            match = HUNK_PREFIX.match(line)
            if match:
                files[current]["hunks"].append(
                    {"prefix": match.group(1), "header": line, "lines": []}
                )
                in_hunk = True
                prev = line
                continue
            if in_hunk:
                if line.startswith("\\"):
                    prev = line
                    continue
                kind = {"+": "a", "-": "d"}.get(line[:1], "c")
                files[current]["hunks"][-1]["lines"].append((kind, line))
                if kind == "a":
                    files[current]["added"] += 1
                elif kind == "d":
                    files[current]["removed"] += 1
        prev = line

    return order, files


def parse_diff(text):
    """Return (ordered paths, {path: [hunk header prefix]}) from a unified diff."""
    order, files = parse_hunks(text)
    return order, {path: [h["prefix"] for h in entry["hunks"]]
                   for path, entry in files.items()}


def _blank(value):
    return not isinstance(value, str) or not value.strip()


def _is_bare_verb_role(role):
    """True when a role is nothing but one banned change-verb, optionally with trailing
    punctuation ("Moved", "Deleted:", "moved."). A two-word role like "Macro tests" is weak
    but deliberately not caught here: judging length or quality produces false rejections,
    only the single-bare-verb case is safe to reject language-independently."""
    stripped = re.sub(r"[^A-Za-z0-9]+$", "", role.strip())
    return stripped.lower() in BANNED_ROLE_WORDS


def _identifier_pieces(text):
    """Tokenise identifiers, keeping each whole token plus its camelCase/snake_case
    pieces, all lowercased, so tokenBucket, token_bucket and TokenBucket all reduce to
    the same pieces. Deliberately language independent: no per-language split here, that's
    symdelta's job and duplicating it is the thing to avoid.

    Drops anything under MIN_IDENTIFIER_LEN chars or in STOPWORDS, from the whole token and
    from each split piece, so a shared `if` or `err` can no longer stand in for a real match.

    Returns {piece: raw_token}: the first raw token each piece came from, so a caller naming
    a specific identifier doesn't need to re-tokenise.
    """
    pieces = {}
    for token in IDENTIFIER.findall(text):
        parts = [token]
        parts.extend(token.split("_"))
        for part in parts:
            for piece in CAMEL_BOUNDARY.sub(" ", part).split() or [part]:
                lowered = piece.lower()
                if len(lowered) >= MIN_IDENTIFIER_LEN and lowered not in STOPWORDS:
                    pieces.setdefault(lowered, token)
    return pieces


def _identifiers(text):
    return set(_identifier_pieces(text))


def _empty_note_floor(hunks):
    """Fail when too large a share of hunks in the whole diff have an empty note.
    See EMPTY_NOTE_FLOOR for why this replaced per-hunk churn detection."""
    if not hunks:
        return []
    empty = sum(1 for h in hunks if _blank(h.get("note")))
    share = empty / len(hunks)
    if share <= EMPTY_NOTE_FLOOR:
        return []
    return [
        f"{empty}/{len(hunks)} hunks ({share:.0%}) have an empty note, above the "
        f"{EMPTY_NOTE_FLOOR:.0%} floor -- this looks like the fan-out under-annotated; "
        f"annotate the substantive hunks, don't lower the floor"
    ]


def validate(diff_text, analysis, recap=False):
    """Return a list of plain-sentence problems; empty means it passes.

    recap=True is the cheap --recap mode: analysis.json carries no per-file or per-hunk
    detail, so those checks are skipped and "groups" becomes the only thing that has to
    cover every path in the diff.
    """
    order, diff_files = parse_hunks(diff_text)
    diff_hunks = {path: [h["prefix"] for h in entry["hunks"]]
                  for path, entry in diff_files.items()}
    problems = []

    for key in (PROSE_KEYS if recap else REQUIRED_KEYS):
        if key not in analysis:
            problems.append(f"missing top-level key: {key}")
    if _blank(analysis.get("what_changed")):
        problems.append("what_changed is empty")

    if recap:
        groups = analysis.get("groups")
        if groups is None:
            problems.append("recap requires groups to cover every path in the diff")
        problems += _validate_groups(groups, diff_hunks)
        if isinstance(groups, list):
            claimed = {path for group in groups if isinstance(group, dict)
                       for path in (group.get("paths") or []) if isinstance(path, str)}
            problems += [f"{path}: changed in the diff but in no group"
                         for path in order if path not in claimed]
        return problems

    if not isinstance(analysis.get("files"), list):
        problems.append("files must be a list")
        return problems

    all_hunks = []
    entries = {}
    for i, entry in enumerate(analysis["files"]):
        if not isinstance(entry, dict) or _blank(entry.get("path")):
            problems.append(f"files[{i}] has no path")
            continue
        path = entry["path"]
        if path in entries:
            problems.append(f"{path}: listed twice")
        entries[path] = entry

    for path in order:
        entry = entries.get(path)
        if entry is None:
            problems.append(f"{path}: changed in the diff but missing from files[]")
            continue
        role = entry.get("role")
        if _blank(role):
            problems.append(f"{path}: role is empty")
        elif _is_bare_verb_role(role):
            problems.append(f'{path}: role is just "{role.strip()}" -- a role must state '
                             "what the file is for or where its contents went, not just "
                             "repeat the change verb the diff header already shows")
        hunks = entry.get("hunks")
        if not isinstance(hunks, list):
            problems.append(f"{path}: hunks must be a list")
            continue
        body_by_prefix = {h["prefix"]: h for h in diff_files[path]["hunks"]}
        covered = {}
        for hunk in hunks:
            if not isinstance(hunk, dict) or _blank(hunk.get("header")):
                problems.append(f"{path}: a hunk has no header")
                continue
            match = HUNK_PREFIX.match(hunk["header"].strip())
            if not match:
                problems.append(f"{path}: hunk header is not a @@ range: {hunk['header']!r}")
                continue
            header = match.group(1)
            if header in covered:
                problems.append(f"{path}: hunk {header} listed twice")
            covered[header] = hunk
            all_hunks.append(hunk)
            body = body_by_prefix.get(header)
            if not _blank(hunk.get("note")) and body is not None:
                changed = "\n".join(raw for kind, raw in body["lines"] if kind in ("a", "d"))
                hunk_ids = _identifiers(changed)
                if hunk_ids and hunk_ids.isdisjoint(_identifiers(hunk["note"])):
                    problems.append(
                        f"{path}: hunk {header} note shares no identifier with the "
                        f"hunk's changed lines"
                    )
        for header in diff_hunks[path]:
            if header not in covered:
                problems.append(f"{path}: hunk {header} is in the diff but has no note")
        for header in covered:
            if header not in diff_hunks[path]:
                problems.append(f"{path}: hunk {header} is not in the diff")

    for path in entries:
        if path not in diff_hunks:
            problems.append(f"{path}: in files[] but not changed in the diff")

    problems += _validate_groups(analysis.get("groups"), diff_hunks)
    problems += _empty_note_floor(all_hunks)
    return problems


def _validate_groups(groups, diff_hunks):
    """Groups are optional, but a group that exists has to be usable: a title to print, paths
    that are really in the diff, and no path claimed twice. A file in no group is not an error,
    the renderer sweeps those into a catch-all, which is what keeps a partial grouping safe to
    ship."""
    if groups is None:
        return []
    if not isinstance(groups, list):
        return ["groups must be a list"]
    problems, claimed = [], {}
    for i, group in enumerate(groups):
        if not isinstance(group, dict):
            problems.append(f"groups[{i}] is not an object")
            continue
        if _blank(group.get("title")):
            problems.append(f"groups[{i}] has no title")
        paths = group.get("paths")
        if not isinstance(paths, list) or not paths:
            problems.append(f"groups[{i}] has no paths")
            continue
        for path in paths:
            if not isinstance(path, str) or path not in diff_hunks:
                problems.append(f"groups[{i}]: {path!r} is not a file in the diff")
            elif path in claimed:
                problems.append(f"{path}: in both group {claimed[path]} and group {i}")
            else:
                claimed[path] = i
    return problems


def check_rendered(diff_text, rendered):
    order, _ = parse_diff(diff_text)
    return [f"{path}: never mentioned in the rendered recap" for path in order
            if path not in rendered]


def check_sections(section_paths, rendered):
    """Each section file's first line is its `<!-- visual-diff:kind -->` marker, so finding
    that marker in the output proves the section reached it. An empty section file means the
    analysis had nothing to draw, which is not a failure."""
    problems = []
    for section_path in section_paths:
        text = open(section_path, errors="replace").read()
        if not text.strip():
            continue
        marker = text.splitlines()[0].strip()
        if marker not in rendered:
            problems.append(
                f"{marker} section was generated but is missing from the rendered output "
                f"({section_path})"
            )
    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--diff", required=True)
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--rendered")
    parser.add_argument("--sections", nargs="*", default=[])
    parser.add_argument("--recap", action="store_true",
                         help="cheap prose-only mode: skip per-file/per-hunk checks, "
                              "require groups to cover every path instead")
    args = parser.parse_args()

    diff_text = open(args.diff, errors="replace").read()
    try:
        analysis = json.load(open(args.analysis))
    except json.JSONDecodeError as exc:
        print(f"analysis.json is not valid JSON: {exc}", file=sys.stderr)
        return 1

    problems = validate(diff_text, analysis, recap=args.recap)
    if args.rendered:
        rendered = open(args.rendered, errors="replace").read()
        problems += check_rendered(diff_text, rendered)
        problems += check_sections(args.sections, rendered)

    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1

    order, hunks = parse_diff(diff_text)
    print(f"ok: {len(order)} files, {sum(len(h) for h in hunks.values())} hunks covered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
