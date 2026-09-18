#!/usr/bin/env python3
"""Show how changed files' in-repo dependencies moved between two git refs.

Usage: python3 coupling.py --repo <path> --base <ref> --head <ref> [--files <p1> <p2> ...]

Stdlib only, no network, no third-party imports. See human-review/SKILL.md for
how this fits into the recap flow.
"""

import argparse
import ast
import json
import posixpath
import re
import subprocess
import sys
from pathlib import PurePosixPath

SUPPORTED_EXTENSIONS = {
    ".py": "python",
    ".js": "js",
    ".jsx": "js",
    ".ts": "js",
    ".tsx": "js",
    ".go": "go",
}

# Files no reader expects import-based coupling from: dotfiles, plus data,
# docs, lockfile, image and shell-script extensions. Anything else unsupported
# (.rb, .rs, .java, ...) still surfaces as a real blind spot.
NOISE_EXTENSIONS = {
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".lock", ".cfg", ".ini",
    ".csv", ".sql", ".sh", ".bash", ".zsh",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp",
}


def is_noise_file(path):
    basename = path.rsplit("/", 1)[-1]
    if basename.startswith("."):
        return True
    return posixpath.splitext(basename)[1].lower() in NOISE_EXTENSIONS


# Whole path segments that mark a location as a test dir, and filename conventions that mark
# a file as a test, across ecosystems (not just JS/TS + Python). Ported from graphify's
# paths.py (_TEST_DIR_SEGMENTS / _TEST_FILENAME_PATTERNS / _is_test_path), not imported: that
# is a private name inside a uv-tool venv, and this module is stdlib-only by contract, so a
# graphify version bump renaming it would silently break test filtering here.
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

# Re-exported from structure.py, whose rank() still needs it; derived from
# _TEST_DIR_SEGMENTS so the two never drift apart. Anchored to (^|/) and a closing "/" so it
# matches only a whole path segment, never a substring ("src/contest.py" doesn't match).
TEST_PATH_RE = re.compile(
    r"(^|/)(" + "|".join(sorted(_TEST_DIR_SEGMENTS)) + r")/", re.IGNORECASE
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

JS_IMPORT_PATTERNS = [
    re.compile(r"""\bimport\s+(?:[\w${}*\s,]+\s+from\s+)?['"]([^'"]+)['"]"""),
    re.compile(r"""\bexport\s+(?:\*|\{[^}]*\}|[\w\s,]+)?\s*from\s+['"]([^'"]+)['"]"""),
    re.compile(r"""\brequire\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""\bimport\(\s*['"]([^'"]+)['"]\s*\)"""),
]

GO_BLOCK_RE = re.compile(r"import\s*\(([^)]*)\)", re.S)
GO_BLOCK_PATH_RE = re.compile(r'(?:\w+\s+)?"([^"]+)"')
GO_LINE_RE = re.compile(r'import\s+(?:\w+\s+)?"([^"]+)"')
GO_MODULE_RE = re.compile(r"^\s*module\s+(\S+)", re.M)


def run_git(repo, args):
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


def git_show(repo, ref, path):
    """Return a file's content at ref, or None if it doesn't exist there."""
    result = run_git(repo, ["show", f"{ref}:{path}"])
    if result.returncode != 0:
        return None
    return result.stdout


def list_repo_files(repo, ref):
    result = run_git(repo, ["ls-tree", "-r", "--name-only", ref])
    if result.returncode != 0:
        raise RuntimeError(f"git ls-tree failed for ref {ref!r}: {result.stderr.strip()}")
    return set(result.stdout.splitlines())


def detect_lang(path):
    for ext, lang in SUPPORTED_EXTENSIONS.items():
        if path.endswith(ext):
            return lang
    return None


def graph_node(path):
    """The name a file appears under in the coupling graph. Go resolves an import to a package
    directory, so a Go file is its package; every other language resolves to a file, so the file
    is its own node. One place decides this, because `changed_set` has to be mapped through the
    same rule before "did this change touch that node" can be asked."""
    if detect_lang(path) == "go":
        return path.rsplit("/", 1)[0] if "/" in path else "(root)"
    return path


def changed_nodes_of(changed_files):
    return {graph_node(p) for p in changed_files if detect_lang(p)}


def resolve_python_module(parts, files_set):
    if not parts:
        return None
    base = "/".join(parts)
    for candidate in (base + ".py", base + "/__init__.py"):
        if candidate in files_set:
            return candidate
    return None


def python_package_parts(path, level):
    """Directory components of the package `level` dots resolves to."""
    parts = path.split("/")[:-1]
    drop = level - 1
    if drop > 0:
        parts = parts[: max(0, len(parts) - drop)]
    return parts


def parse_python_edges(path, content, files_set):
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None
    edges = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                resolved = resolve_python_module(alias.name.split("."), files_set)
                if resolved:
                    edges.add((path, resolved))
        elif isinstance(node, ast.ImportFrom):
            base_parts = python_package_parts(path, node.level) if node.level > 0 else []
            mod_parts = base_parts + (node.module.split(".") if node.module else [])
            # Try each name as a submodule first; fall back to the module itself only if
            # none resolve, e.g. "from pkg.mod import some_function" (an attribute import).
            any_submodule = False
            for alias in node.names:
                if alias.name == "*":
                    continue
                resolved = resolve_python_module(mod_parts + [alias.name], files_set)
                if resolved:
                    edges.add((path, resolved))
                    any_submodule = True
            if not any_submodule and mod_parts:
                resolved = resolve_python_module(mod_parts, files_set)
                if resolved:
                    edges.add((path, resolved))
    return edges


def resolve_js_import(spec, importer_path, files_set):
    if not spec.startswith("."):
        return None  # bare specifier: third-party/node_modules, noise for coupling
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(importer_path), spec))
    candidates = [joined]
    for ext in (".ts", ".tsx", ".js", ".jsx"):
        candidates.append(joined + ext)
    # NodeNext and ESM TypeScript write the emitted extension, so `./thing.js` in a .ts file
    # means the file thing.ts. Without this every relative import in such a repo resolves to
    # nothing and the whole graph comes out empty.
    stem, ext = posixpath.splitext(joined)
    if ext in (".js", ".jsx", ".mjs", ".cjs"):
        candidates += [stem + swap for swap in (".ts", ".tsx", ".mts", ".cts", "")]
    for ext in (".ts", ".tsx", ".js", ".jsx"):
        candidates.append(posixpath.join(joined, "index" + ext))
    for candidate in candidates:
        if candidate in files_set:
            return candidate
    return None


def parse_js_edges(path, content, files_set):
    try:
        edges = set()
        for pattern in JS_IMPORT_PATTERNS:
            for match in pattern.finditer(content):
                resolved = resolve_js_import(match.group(1), path, files_set)
                if resolved:
                    edges.add((path, resolved))
        return edges
    except Exception:
        return None


def resolve_go_import(import_path, module_prefix, files_set):
    if not module_prefix or not import_path.startswith(module_prefix):
        return None
    rel_dir = import_path[len(module_prefix) :].lstrip("/")
    if not rel_dir:
        return None
    # Go imports name a package (directory), not a file; use the dir as the node.
    if any(f == rel_dir or f.startswith(rel_dir + "/") for f in files_set):
        return rel_dir
    return None


def parse_go_edges(path, content, files_set, go_module_prefix):
    try:
        edges = set()
        specs = []
        for block in GO_BLOCK_RE.findall(content):
            specs.extend(GO_BLOCK_PATH_RE.findall(block))
        specs.extend(GO_LINE_RE.findall(content))
        for spec in specs:
            resolved = resolve_go_import(spec, go_module_prefix, files_set)
            # Both ends are packages. A Go import names a package, never a file, so recording
            # the importing *file* against an imported *directory* put two granularities in one
            # graph: `uid.go` would sit next to `corelib/ratelimit/uid`, the directory it lives in.
            # It also made "both ends changed" impossible to satisfy, since a directory is never
            # a path in the diff. The cost is that Go tests fold into the package they sit
            # beside, so hiding tests is coarse for Go; that is how Go lays code out.
            if resolved:
                edges.add((graph_node(path), resolved))
        return {(a, b) for a, b in edges if a != b}
    except Exception:
        return None


def parse_edges(lang, path, content, files_set, go_module_prefix):
    if lang == "python":
        return parse_python_edges(path, content, files_set)
    if lang == "js":
        return parse_js_edges(path, content, files_set)
    if lang == "go":
        return parse_go_edges(path, content, files_set, go_module_prefix)
    return set()


def go_module_prefix_at(repo, ref):
    content = git_show(repo, ref, "go.mod")
    if not content:
        return None
    match = GO_MODULE_RE.search(content)
    return match.group(1) if match else None


def edges_for_ref(repo, ref, changed_files, changed_set, unparseable):
    """All edges touching a changed file: changed files' own imports, plus
    one hop of files elsewhere in the repo that import a changed file."""
    files_set = list_repo_files(repo, ref)
    go_module_prefix = go_module_prefix_at(repo, ref)
    edges = set()

    for path in changed_files:
        lang = detect_lang(path)
        if lang is None:
            continue
        content = git_show(repo, ref, path)
        if content is None:
            continue  # absent at this ref: an empty side, not an error
        result = parse_edges(lang, path, content, files_set, go_module_prefix)
        if result is None:
            unparseable.add(path)
            continue
        edges |= result

    # A Go node is its package, so "does this edge land on something the diff touched" has to be
    # asked in node space, not against the raw changed file paths.
    changed_graph_nodes = changed_nodes_of(changed_set)
    for other in files_set - changed_set:
        lang = detect_lang(other)
        if lang is None:
            continue
        content = git_show(repo, ref, other)
        if content is None:
            continue
        result = parse_edges(lang, other, content, files_set, go_module_prefix)
        if not result:
            continue
        edges |= {(imp, tgt) for imp, tgt in result if tgt in changed_graph_nodes}

    return edges


# Directory segments that only name the source root, not a real module boundary; module_of
# strips the same list.
SKIP_PREFIX_SEGMENTS = ("src", "lib", "packages", "apps", "internal", "pkg", "cmd")

# Same budget structure.py has drawn to all along. A 135-node, 201-edge file graph (measured on
# a large Go repo's PR) is not a map anyone reads, it is a wall of crossing lines.
MAX_NODES = 40
MAX_EDGES = 60

# dagre miscomputes cluster ordering past roughly a dozen subgraphs on a wide graph and throws
# "Cannot set properties of undefined (setting 'order')", which mermaid turns into a raw
# "Syntax error in text" box where the diagram should be. Measured on that same graph: 12
# subgraphs render, 20 do not, with node and edge counts held constant. Past the cap the
# smallest modules lose their box and sit loose instead, which costs a grouping line and keeps
# the diagram.
MAX_CLUSTERS = 12


def module_of(path):
    """The file's real containing directory, every SKIP_PREFIX_SEGMENTS-stripped segment kept:
    in Go, Java, Python and TS the directory IS the package, so a module is the whole thing,
    not a guessed-depth truncation of it. A file at the repo root is its own module."""
    segments = path.split("/")[:-1]
    while segments and segments[0].lower() in SKIP_PREFIX_SEGMENTS:
        segments = segments[1:]
    if not segments:
        return path.rsplit("/", 1)[0] if "/" in path else "(root)"
    return "/".join(segments)


# Sentinel filename for a folded types/constants node. No extension, so it can never
# collide with a real source file or match detect_lang().
TYPE_CONST_NODE_NAME = "__types-constants__"

TYPE_CONST_JS_EXTS = (".ts", ".tsx", ".js", ".jsx")
TYPE_CONST_JS_SUFFIXES = (".type", ".types", ".constant", ".constants")
TYPE_CONST_PY_GO_NAMES = {"types.py", "constants.py", "types.go", "constants.go"}
TYPE_CONST_PY_GO_SUFFIXES = ("_types.py", "_constants.py", "_types.go", "_constants.go")


def is_type_or_const_file(path):
    """Cheap filename match for leaf type/constant files, folded into one node per module:
    `*.type(s).ts`/`.d.ts` in any SUPPORTED_EXTENSIONS JS spelling, plus the equivalent
    Python and Go filenames. Not a content-based type detector."""
    basename = path.rsplit("/", 1)[-1]
    if basename in TYPE_CONST_PY_GO_NAMES or basename.endswith(TYPE_CONST_PY_GO_SUFFIXES):
        return True
    for ext in TYPE_CONST_JS_EXTS:
        if basename.endswith(ext):
            stem = basename[: -len(ext)]
            return stem.endswith(TYPE_CONST_JS_SUFFIXES) or basename.endswith(".d.ts")
    return False


def type_const_node_for(path):
    """The sentinel node a type/constant file at `path` folds into: one per module, at
    module_of(path)'s own directory, so that call and sections.py's _module_chain() both
    resolve the sentinel to the same module with no special case. The number of leading
    SKIP_PREFIX_SEGMENTS varies per repo, so it's counted here rather than assumed, then every
    remaining real segment is kept after them."""
    segments = path.split("/")[:-1]
    skip = 0
    while skip < len(segments) and segments[skip].lower() in SKIP_PREFIX_SEGMENTS:
        skip += 1
    kept = segments[skip:]
    if not kept:
        # No module directory of its own: mirrors module_of's root-level fallback.
        directory = path.rsplit("/", 1)[0] if "/" in path else ""
        return f"{directory}/{TYPE_CONST_NODE_NAME}" if directory else TYPE_CONST_NODE_NAME
    directory = "/".join(segments[:skip] + kept)
    return f"{directory}/{TYPE_CONST_NODE_NAME}"


def drop_test_edges(edges):
    """Edges with either endpoint a test file removed, for the tests-hidden view. Must run
    before fold_type_const_edges, not after: folding counts a sentinel's files from whichever
    edges it's handed, so filtering afterwards would leave a hidden test's file counted in a
    label nothing points at any more."""
    return {(a, b) for a, b in edges if not (is_test_path(a) or is_test_path(b))}


def fold_type_const_edges(edges):
    """Collapse every edge endpoint that is a type/constant file onto its module's sentinel
    node, dropping any edge that folds into a self-loop. Returns the folded edge set plus,
    per sentinel, the real file paths it stands in for, so a caller can label it with a file
    count."""
    folded = set()
    files_by_sentinel = {}
    for importer, imported in edges:
        a, b = importer, imported
        if is_type_or_const_file(a):
            a = type_const_node_for(a)
            files_by_sentinel.setdefault(a, set()).add(importer)
        if is_type_or_const_file(b):
            b = type_const_node_for(b)
            files_by_sentinel.setdefault(b, set()).add(imported)
        if a != b:
            folded.add((a, b))
    return folded, files_by_sentinel


def type_const_label(path, counts):
    """The `types & constants (N files)` override label for a folded node, or None when
    `path` isn't one. `counts[path]` is how many real files folded into it."""
    n = (counts or {}).get(path)
    if n is None:
        return None
    return f"types & constants ({n} file{'' if n == 1 else 's'})"


def module_of_node(node):
    """module_of() for a graph node, which is a file for most languages but a package directory
    for Go. module_of drops the last segment as a filename, so a directory node would lose its
    own name: the package `exchange` came out as "(root)" and `corelib/ratelimit/uid` as
    `corelib/ratelimit`. Appending a placeholder gives the real last segment something to be
    stripped instead, the same trick layers.py already uses for layer_of(module + "/x").

    A node is a directory when its last segment has no extension. A directory literally named
    `v1.2` would be read as a file; nothing in this graph is named that way, and the alternative
    is threading the language through every grouping call site."""
    last = node.rsplit("/", 1)[-1]
    if "." in last or last == TYPE_CONST_NODE_NAME:
        return module_of(node)
    return module_of(node + "/x")


def short_labels(paths):
    """Shortest tail of each path that is still unique, so a subgraph title reads as
    `define-tool.spec.ts` instead of a full `src/adapters/mcp/shared/...` line that pushes
    every box off the side of the diagram."""
    labels = {}
    for path in paths:
        parts = path.split("/")
        for depth in range(1, len(parts) + 1):
            candidate = "/".join(parts[-depth:])
            if sum(1 for p in paths if p.endswith(candidate)) == 1:
                labels[path] = candidate
                break
        else:
            labels[path] = path
    return labels


def cap_clusters(grouped, max_clusters=MAX_CLUSTERS):
    """(grouped, loose) with at most max_clusters boxes kept, biggest first; the nodes from the
    dropped boxes come back as loose nodes rather than being removed. See MAX_CLUSTERS: past
    the cap dagre stops laying the graph out at all. Shared by every builder that draws
    subgraphs, so one cap governs all of them."""
    if len(grouped) <= max_clusters:
        return grouped, []
    ranked = sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    loose = [node for _name, nodes in ranked[max_clusters:] for node in nodes]
    return dict(ranked[:max_clusters]), loose


def _diagram_rank(edge, changed_set):
    """Changed-file edges first, then edges that moved, then production before tests, so the
    cap cuts context and test scaffolding rather than the thing the reader opened the recap
    for. Same ordering structure.py:rank() uses, on this module's edge shape."""
    src, dst, state = edge
    touches = (src in changed_set) + (dst in changed_set)
    tests = is_test_path(src) + is_test_path(dst)
    return (-touches, 0 if state != "unchanged" else 1, tests, src, dst)


def touched_only(added, removed, unchanged, changed_set,
                 max_nodes=MAX_NODES, max_edges=MAX_EDGES):
    """(added, removed, unchanged, scoped) narrowed to edges with a changed node at both ends,
    but only when the full one-hop graph is too big to draw.

    One hop past the diff is what turns a small diff into a wall: 22 changed files became 135
    nodes and 201 edges on a large Go repo's PR, against 8 and 12 when both ends have to be
    touched. It is also genuinely useful on a diff that fits, which is why this is conditional
    rather than always on, and why an empty result falls back rather than drawing nothing. The
    untouched neighbours stay in the emitted JSON either way, so a node click still reaches
    them."""
    full = (added, removed, unchanged)
    nodes = {n for part in full for edge in part for n in edge}
    if len(nodes) <= max_nodes and sum(len(part) for part in full) <= max_edges:
        return (*full, False)

    changed = changed_nodes_of(changed_set)

    def keep(edges):
        return [(a, b) for a, b in edges if a in changed and b in changed]

    scoped = (keep(added), keep(removed), keep(unchanged))
    if not any(scoped):
        return (*full, False)
    return (*scoped, True)


def cap_for_diagram(added, removed, unchanged, changed_set,
                    max_nodes=MAX_NODES, max_edges=MAX_EDGES):
    """(added, removed, unchanged, truncated) trimmed to what a reader can actually follow.

    Kept separate from build_mermaid so the emitted JSON's own edge lists stay complete: the
    cap is a drawing decision, and a consumer counting real coupling should still see all of
    it. Callers that only want a diagram call this first. A safety net now that touched_only()
    does the heavy reduction, not the main lever."""
    ordered = sorted(
        [(a, b, "added") for a, b in added]
        + [(a, b, "removed") for a, b in removed]
        + [(a, b, "unchanged") for a, b in unchanged],
        key=lambda e: _diagram_rank(e, changed_set),
    )
    kept, nodes = [], []
    for edge in ordered:
        fresh = [n for n in edge[:2] if n not in nodes]
        if len(kept) >= max_edges or len(nodes) + len(fresh) > max_nodes:
            continue
        nodes.extend(fresh)
        kept.append(edge)
    by_state = {"added": [], "removed": [], "unchanged": []}
    for a, b, state in kept:
        by_state[state].append((a, b))
    return (
        by_state["added"],
        by_state["removed"],
        by_state["unchanged"],
        len(kept) < len(ordered),
    )


def build_mermaid(added, removed, unchanged, type_const_counts=None):
    nodes = sorted({p for edge in (added + removed + unchanged) for p in edge})
    if not nodes:
        return "", {}
    type_const_counts = type_const_counts or {}
    node_id = {path: f"N{i}" for i, path in enumerate(nodes)}
    labels = short_labels(nodes)

    def label_of(path):
        return type_const_label(path, type_const_counts) or labels[path]

    grouped, rootless = {}, []
    for path in nodes:
        module = module_of_node(path)
        (rootless if module == "(root)" else grouped.setdefault(module, [])).append(path)
    grouped, loose = cap_clusters(grouped)
    rootless.extend(loose)

    lines = ["flowchart LR"]
    for i, module in enumerate(sorted(grouped)):
        lines.append(f'  subgraph G{i}["{module}"]')
        for path in grouped[module]:
            lines.append(f'    {node_id[path]}["{label_of(path)}"]')
        lines.append("  end")
    for path in rootless:
        lines.append(f'  {node_id[path]}["{label_of(path)}"]')

    for a, b in added:
        lines.append(f"  {node_id[a]} ==> {node_id[b]}")
    for a, b in removed:
        lines.append(f"  {node_id[a]} -.-> {node_id[b]}")
    for a, b in unchanged:
        lines.append(f"  {node_id[a]} --> {node_id[b]}")
    # Folded nodes are deliberately unclickable: the page resolves a click through ids, and
    # there is no view behind a pseudo-path to drill into.
    ids = {node_id[path]: path for path in nodes if path not in type_const_counts}
    return "\n".join(lines), ids


def analyse(repo, base, head, files):
    verify = run_git(repo, ["rev-parse", "--verify", f"{base}^{{commit}}"])
    if verify.returncode != 0:
        raise RuntimeError(f"bad ref: {base!r}")
    verify = run_git(repo, ["rev-parse", "--verify", f"{head}^{{commit}}"])
    if verify.returncode != 0:
        raise RuntimeError(f"bad ref: {head!r}")

    base = resolve_base(repo, base, head)

    if files:
        changed_files = list(files)
    else:
        diff = run_git(repo, ["diff", "--name-only", f"{base}...{head}"])
        if diff.returncode != 0:
            raise RuntimeError(f"git diff failed: {diff.stderr.strip()}")
        changed_files = [line for line in diff.stdout.splitlines() if line]

    skipped_unsupported = sorted(
        {f for f in changed_files if detect_lang(f) is None and not is_noise_file(f)}
    )
    changed_set = set(changed_files)

    unparseable = set()
    edges_base_raw = edges_for_ref(repo, base, changed_files, changed_set, unparseable)
    edges_head_raw = edges_for_ref(repo, head, changed_files, changed_set, unparseable)

    def folded(base_edges, head_edges):
        """(added, removed, unchanged, type_const_counts) over one edge-set pair. Folding runs
        before the diff, so every downstream consumer sees the same folded edges and the
        diagram can never disagree with the Numbers: line."""
        eb, files_base = fold_type_const_edges(base_edges)
        eh, files_head = fold_type_const_edges(head_edges)
        counts = {}
        for mapping in (files_base, files_head):
            for sentinel, files in mapping.items():
                counts.setdefault(sentinel, set()).update(files)
        counts = {sentinel: len(files) for sentinel, files in counts.items()}
        return sorted(eh - eb), sorted(eb - eh), sorted(eh & eb), counts

    added, removed, unchanged, type_const_counts = folded(edges_base_raw, edges_head_raw)
    added_nt, removed_nt, unchanged_nt, type_const_counts_nt = folded(
        drop_test_edges(edges_base_raw), drop_test_edges(edges_head_raw)
    )
    changed_flag = bool(added or removed)

    note = ""
    if not changed_flag:
        note = (
            "No import-based coupling changes detected, so the diagram shows the file "
            "relations as they stand at both refs; coupling that moves through non-import "
            "mechanisms (e.g. string references) is not visible to this analysis."
        )

    # Always drawn when there is anything to draw: the graph answers "how do these
    # files relate", which is worth seeing even when no edge moved.
    add_s, rem_s, unch_s, scoped_to_touched = touched_only(
        added, removed, unchanged, changed_set
    )
    drawn = cap_for_diagram(add_s, rem_s, unch_s, changed_set)
    mermaid, ids = build_mermaid(*drawn[:3], type_const_counts=type_const_counts)
    add_nts, rem_nts, unch_nts, _ = touched_only(
        added_nt, removed_nt, unchanged_nt, changed_set
    )
    drawn_nt = cap_for_diagram(add_nts, rem_nts, unch_nts, changed_set)
    mermaid_notests, ids_notests = build_mermaid(
        *drawn_nt[:3], type_const_counts=type_const_counts_nt
    )
    drawn_edges = sum(len(part) for part in drawn[:3])
    all_edges = len(added) + len(removed) + len(unchanged)
    truncated = drawn[3]
    if scoped_to_touched:
        note = (note + " ").strip() + (
            f"Showing the {drawn_edges} relations between files this change touched, of "
            f"{all_edges} that touch it in either direction, because the full graph was too "
            "dense to read. Click a node for its untouched neighbours; the counts below are for "
            "the whole graph."
        )
    if truncated:
        note = (note + " ").strip() + (
            f"Also capped at {MAX_NODES} files and {MAX_EDGES} relations."
        )

    return {
        "changed": changed_flag,
        "added": [list(e) for e in added],
        "removed": [list(e) for e in removed],
        "unchanged": [list(e) for e in unchanged],
        "added_notests": [list(e) for e in added_nt],
        "removed_notests": [list(e) for e in removed_nt],
        "unchanged_notests": [list(e) for e in unchanged_nt],
        "mermaid": mermaid,
        "ids": ids,
        "mermaid_notests": mermaid_notests,
        "ids_notests": ids_notests,
        "type_const_counts": type_const_counts,
        "type_const_counts_notests": type_const_counts_nt,
        "truncated": truncated,
        # The diff's own nodes, so a consumer can tell a changed node from a neighbour that was
        # only pulled in by an edge. layers.py needs exactly this to scope its map.
        "changed_nodes": sorted(changed_nodes_of(changed_set)),
        "skipped": {
            "unsupported": skipped_unsupported,
            "unparseable": sorted(unparseable),
        },
        "note": note,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--files", nargs="+", default=None)
    args = parser.parse_args()

    try:
        result = analyse(args.repo, args.base, args.head, args.files)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
