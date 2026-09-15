#!/usr/bin/env python3
"""Per-function cyclomatic complexity at two refs, for the files a diff touched.

  python3 complexity.py --repo . --base master --head HEAD --diff raw.diff > complexity.json

Counts decision points rather than walking a real control-flow graph: `1 + branches` over each
function's own subtree, the textbook McCabe approximation every linter uses. The node names it
matches differ between grammars, so an absolute number is only comparable within one language.
One function's delta between two refs is the part that holds, and that is all the recap reports.

Python goes through `ast` because the vendored grammar set has no Python parser; everything else
reuses structure.py's tree-sitter parsers, so the languages covered here are exactly the ones
the rest of the recap already covers.

A file's total is the sum over its functions. Module-level branching is left out on purpose:
attributing it needs a synthetic owner that no other section has a node for, and in the
languages this runs on there is almost none of it.

Stdlib only except for shelling out to `git` and the tree-sitter grammars structure.py loads.
"""

import argparse
import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from coupling import git_show, is_noise_file, resolve_base  # noqa: E402  shared git helpers
from fanout import rename_map  # noqa: E402  one owner for parsing "rename from/to" headers
from links import line_range  # noqa: E402  one owner for a hunk's new-side line span
from structure import (  # noqa: E402  one owner for parsers and for "what is a function"
    _kind_of,
    _label,
    detect_lang,
    generic_parser_for,
)
from validate_analysis import parse_hunks  # noqa: E402  one owner for diff parsing

# Matched by exact node type, not by regex on a suffix: Go spells one loop as both
# `for_statement` and an inner `for_clause`, and a prefix rule would count it twice.
# A default arm is the fall-through rather than a decision, so `default_case`/`switch_default`
# are deliberately absent.
BRANCH_TYPES = frozenset({
    "if_statement", "if_expression", "if_let_expression", "elsif", "elif_clause",
    "else_if_clause", "unless_statement",
    "for_statement", "for_expression", "for_in_statement", "for_of_statement",
    "enhanced_for_statement", "while_statement", "while_expression", "do_statement",
    "repeat_statement", "loop_expression", "foreach_statement",
    "expression_case", "type_case", "communication_case", "switch_case", "case_clause",
    "match_arm", "when_clause",
    "catch_clause", "rescue", "except_clause",
    "ternary_expression", "conditional_expression",
})

# Short-circuit operators are their own anonymous node in every grammar checked, so the operator
# text is the node type and there is no need to dig into binary_expression's operator field.
# `?.` is left out though it does branch: idiomatic TS puts it on almost every access, which
# would swamp the count with noise a reviewer cannot act on.
BOOL_OPS = frozenset({"&&", "||", "and", "or", "??"})

_PY_BRANCH = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp,
              ast.match_case)
_PY_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)

# Three levels is the commonly drawn line for "too deep" (the Linux kernel style guide's
# three-tab rule is the usual citation), so four is the first depth worth a reviewer's eye.
NOTEWORTHY_DEPTH = 4


def _receiver_type(node, src):
    """A method's receiver type, or None. Go puts its methods at the top level, so two methods
    named Get on different structs collide on the bare name and one would be charged for both
    bodies. The class trail already disambiguates languages that nest methods."""
    receiver = node.child_by_field_name("receiver")
    if receiver is None:
        return None
    # Depth varies with the receiver's form: `(a A)` puts the type two levels down, `(a *A)`
    # wraps it in a pointer_type first, and a generic receiver adds another.
    stack = list(receiver.children)
    while stack:
        candidate = stack.pop(0)
        if candidate.type in ("type_identifier", "constant"):
            return src[candidate.start_byte : candidate.end_byte].decode(errors="replace")
        stack.extend(candidate.children)
    return None


def _cc_generic(path, content):
    """{qualified name: {"cc", "lines", "depth"}} through tree-sitter, or None when the file has
    no grammar or will not parse. Branches land on the innermost enclosing function, so a
    closure is charged for its own decisions instead of inflating the function that defines it.

    `lines` is the function's whole span, body included, so a hunk anywhere inside it marks the
    function as one this diff touched. `depth` is the deepest nesting of BRANCH_TYPES reached
    anywhere in the function's own subtree, 0 when it has none."""
    parser = generic_parser_for(path)
    if parser is None:
        return None
    src = content.encode("utf-8", errors="replace")
    out = {}

    def visit(node, owner, trail, depth, chained):
        kind = _kind_of(node.type)
        name = _label(node, src) if kind in ("func", "class") else None
        next_owner, next_trail, next_depth = owner, trail, depth
        if kind == "func" and name:
            receiver = _receiver_type(node, src)
            next_trail = trail + ([receiver] if receiver else []) + [name]
            next_owner = ".".join(next_trail)
            # A name that still collides gets its line appended rather than merging two
            # functions into one inflated count.
            if next_owner in out:
                next_owner = f"{next_owner}:{node.start_point[0] + 1}"
            out[next_owner] = {"cc": 1, "depth": 0,
                               "lines": [node.start_point[0] + 1, node.end_point[0] + 1]}
            next_depth = 0
        elif kind == "class" and name:
            next_trail = trail + [name]
        elif owner and (node.type in BRANCH_TYPES or node.type in BOOL_OPS):
            out[owner]["cc"] += 1
            if node.type in BRANCH_TYPES:
                # A chained else-if is the same decision as the if that opened it, not one
                # level deeper, or a flat if/else-if/else-if would read as depth 3.
                if not chained:
                    next_depth = depth + 1
                out[owner]["depth"] = max(out[owner]["depth"], next_depth)
        # The alternative field continues the chain rather than nesting in it: directly, for a
        # grammar's own elif-like type or (Go, Ruby) a plain if; through one wrapper with no
        # fields of its own (TS/JS's `else_clause`) for the rest, so unwrap that one layer too.
        alt = node.child_by_field_name("alternative") if node.type in BRANCH_TYPES else None
        for child in node.children:
            if alt is not None and child == alt:
                if child.type in BRANCH_TYPES:
                    visit(child, next_owner, next_trail, next_depth, True)
                else:
                    for grandchild in child.children:
                        visit(grandchild, next_owner, next_trail, next_depth,
                              grandchild.type in BRANCH_TYPES)
            else:
                visit(child, next_owner, next_trail, next_depth, False)

    try:
        visit(parser.parse(src).root_node, None, [], 0, False)
    except RecursionError:  # pathological nesting, e.g. minified or generated code
        return None
    except Exception:
        return None
    return out


def _py_weight(node):
    if isinstance(node, ast.BoolOp):
        return len(node.values) - 1
    if isinstance(node, ast.comprehension):
        return 1 + len(node.ifs)
    return 1 if isinstance(node, _PY_BRANCH) else 0


def _max_depth(node, depth):
    """Deepest nesting of _PY_BRANCH reached in node's subtree, given that node itself already
    stands at `depth`. `ast` has no elif node: it is an `ast.If` that is the sole element of
    its parent's `orelse`, and it must not stack with the if that opened it or a flat
    if/elif/elif chain would read as depth 3."""
    best = depth
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (*_PY_FUNC, ast.ClassDef)):
            continue
        chained = (isinstance(node, ast.If) and isinstance(child, ast.If)
                   and node.orelse == [child])
        child_depth = depth + 1 if isinstance(child, _PY_BRANCH) and not chained else depth
        best = max(best, _max_depth(child, child_depth))
    return best


def _cc_python(content):
    """{qualified name: {"cc", "lines", "depth"}} through `ast`. Same attribution rule as
    _cc_generic: a nested def gets its own entry and does not count toward its parent."""
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError):
        return None
    out = {}

    def count(node):
        total = 0
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (*_PY_FUNC, ast.ClassDef)):
                continue
            total += _py_weight(child) + count(child)
        return total

    def walk(node, trail):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _PY_FUNC):
                name = ".".join(trail + [child.name])
                out[name] = {"cc": 1 + count(child), "depth": _max_depth(child, 0),
                             "lines": [child.lineno, child.end_lineno or child.lineno]}
                walk(child, trail + [child.name])
            elif isinstance(child, ast.ClassDef):
                walk(child, trail + [child.name])
            else:
                walk(child, trail)

    walk(tree, [])
    return out


def complexity_at(repo, ref, path):
    """({name: {"cc", "lines", "depth"}}, status) for one file at one ref. status is "ok",
    "absent" when the ref does not have the file, or "unsupported" when it has it but nothing
    here can read it."""
    content = git_show(repo, ref, path)
    if content is None:
        return {}, "absent"
    if is_noise_file(path):
        return {}, "unsupported"
    counts = _cc_python(content) if detect_lang(path) == "python" else _cc_generic(path, content)
    if counts is None:
        return {}, "unsupported"
    return counts, "ok"


def _moved_elsewhere(name, path, owners_at_head):
    """Other head-side paths, within the files this diff touched, where the qualified name
    `name` still exists -- [] when there are none, i.e. when `name` is plausibly just gone.
    Matches on the exact qualified name `_cc_generic`/`_cc_python` already produce, so a
    receiver-qualified method like `A.String` cannot cross-match `B.String`."""
    return sorted(owners_at_head.get(name, set()) - {path})


def _touched(entry, hunk_ranges):
    """Did the diff land inside this function? Overlap against the new-side hunk ranges, so a
    43-branch function three screens away from the one line this diff changed is not reported
    as something the change touched."""
    if entry is None:
        return False
    start, end = entry["lines"]
    return any(h_start <= end and h_end >= start for h_start, h_end in hunk_ranges)


def analyse(repo, base, head, paths, hunk_ranges=None, diff_text=None):
    """Per-file, per-function complexity between two refs.

    `hunk_ranges` maps a path to its new-side hunk line spans. Without it every function counts
    as touched, which is only right for a caller that has no diff to hand.

    `diff_text` is the raw diff, read only for its `rename from`/`rename to` headers. A path in
    `paths` that was renamed is measured as base:<old path> against head:<new path> instead of
    same-path, or a function that merely moved reads as one disappearing at base and an
    unrelated one appearing at head. Without diff_text (or for a path that wasn't renamed),
    base and head are compared at the same path, as before.

    Git only calls it a rename above its similarity threshold; a split or merge instead shows
    up as a plain delete plus a plain add, with nothing for `diff_text` to resolve. The
    second pass below catches that case by name: a qualified name gone from its own path at
    head but still present at head in some OTHER path this diff touched has moved, not
    vanished. Scoped to the files the diff actually touched -- a function moved into a file
    the diff didn't touch is invisible here, same blind spot git's own rename detection has.
    """
    hunk_ranges = hunk_ranges or {}
    renamed_from = {new: old for old, new in rename_map(diff_text or "").items()}
    computed, unsupported = {}, []
    for path in sorted(paths):
        before, before_status = complexity_at(repo, base, renamed_from.get(path, path))
        after, after_status = complexity_at(repo, head, path)
        if before_status == "absent" and after_status == "absent":
            continue
        if "unsupported" in (before_status, after_status):
            unsupported.append(path)
            continue
        computed[path] = (before, after)

    owners_at_head = {}
    for path, (_, after) in computed.items():
        for name in after:
            owners_at_head.setdefault(name, set()).add(path)

    files = {}
    for path, (before, after) in computed.items():
        spans = hunk_ranges.get(path) or [(1, 10 ** 9)]
        symbols = []
        for name in sorted(set(before) | set(after)):
            was, now = before.get(name), after.get(name)
            gone_here = was is not None and now is None
            moved_to = _moved_elsewhere(name, path, owners_at_head) if gone_here else []
            symbols.append({
                "name": name,
                "before": (was or {}).get("cc", 0),
                "after": (now or {}).get("cc", 0),
                # A moved function's delta is pinned to 0 so it can never win "jump" or "peak"
                # below and produce a chip: the move is surfaced elsewhere (the walkthrough's
                # rename line, the symbol graph's move edge), and a chip on top would either
                # repeat that or misreport a relocation as a complexity change.
                "delta": 0 if moved_to else (now or {}).get("cc", 0) - (was or {}).get("cc", 0),
                # A function absent at base is new, and its delta is just its own size, which
                # the file's +N line count already told the reader.
                "existed": was is not None,
                # cc is always >= 1 for a function that exists, so "after": 0 is ambiguous
                # between "gone" and "measured as zero" unless this says which explicitly.
                "removed": gone_here and not moved_to,
                "moved_to": moved_to[0] if moved_to else None,
                "touched": _touched(now, spans) or _touched(was, spans),
                "depth": (now or {}).get("depth", 0),
                "depth_before": (was or {}).get("depth", 0),
            })
        touched = [s for s in symbols if s["touched"]]
        files[path] = {
            "before": sum(e["cc"] for e in before.values()),
            "after": sum(e["cc"] for e in after.values()),
            "delta": sum(e["cc"] for e in after.values()) - sum(e["cc"] for e in before.values()),
            # The worst function this diff touched, by where it now stands rather than by how
            # far it moved: a function at 43 wants a reviewer's attention whether this change
            # pushed it by one or by nothing. Summing the file's functions was the first attempt
            # and ranked files backwards, scoring four new one-branch getters above it.
            "peak": max((s for s in touched if s["after"]),
                        key=lambda s: (s["after"], s["name"]), default=None),
            # The furthest-moved function that already existed, in either direction: a
            # refactor that cut one from 20 to 6 is as much of a headline as one that grew.
            # New functions are excluded because their delta is only their own size, which the
            # file's +N line count already gave the reader.
            "jump": max((s for s in touched if s["existed"] and s["delta"]),
                        key=lambda s: (abs(s["delta"]), s["name"]), default=None),
            "symbols": sorted([s for s in touched if s["delta"]],
                              key=lambda s: (-s["delta"], s["name"])),
        }

    peaks = [dict(e["peak"], path=path) for path, e in files.items() if e["peak"]]
    return {
        "total_delta": sum(e["delta"] for e in files.values()),
        # The highest-standing touched function across the whole diff. None means nothing this
        # change touched has any branching at all, which is a result rather than a gap.
        "worst": max(peaks, key=lambda s: (s["after"], s["path"], s["name"]), default=None),
        "files": files,
        "unsupported": unsupported,
    }


def summary_line(data):
    """The recap's one-line complexity fact, or "" when nothing was measured. Lives here rather
    than in render.py so the page and the PR description word it the same way.

    Leads with where the worst touched function now stands. The net delta across the diff used
    to lead and was close to meaningless: it adds a refactor that split one big function to a
    change that grew one, and comes out near zero for both."""
    if not data or not data.get("files"):
        return ""
    worst = data.get("worst")
    if not worst:
        return "nothing this change touched branches at all"
    text = f"worst {worst['name']} at {worst['after']}"
    if worst["delta"]:
        text += f" ({worst['delta']:+d} here)"
    elif not worst["existed"]:
        text += " (new)"
    else:
        text += " (unchanged here)"
    if worst["depth"] >= NOTEWORTHY_DEPTH:
        text += f", nested {worst['depth']} deep"
    skipped = len(data.get("unsupported") or [])
    if skipped:
        text += f"; {skipped} file{'s' if skipped != 1 else ''} not measured"
    return text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--diff", required=True, help="raw.diff, for the file list")
    args = parser.parse_args()

    diff_text = Path(args.diff).read_text(errors="replace")
    order, diff_files = parse_hunks(diff_text)
    ranges = {path: [line_range(h["prefix"]) for h in entry["hunks"] if line_range(h["prefix"])]
              for path, entry in diff_files.items()}
    base = resolve_base(args.repo, args.base, args.head)
    json.dump(analyse(args.repo, base, args.head, order, ranges, diff_text), sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
