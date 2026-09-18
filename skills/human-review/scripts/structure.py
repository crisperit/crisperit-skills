#!/usr/bin/env python3
"""Show how code structures relate, and how those relations moved between two git refs.

Usage: python3 structure.py --repo <path> --base <ref> --head <ref> [--files <p1> ...]

Where coupling.py answers "which files depend on which", this answers "which classes and
functions depend on which", the granularity coupling actually lives at. Nodes are top-level
classes and functions grouped by file; edges are `extends` (a base class), `implements` (an
interface) and `uses` (a reference to another structure's name in the body).

Python files go through the stdlib `ast`, so every edge there is exact rather than
pattern-matched. Every other language goes through a tree-sitter grammar and a generic
node-type heuristic; for the languages that carry an import map (currently TypeScript and
JavaScript, see parse_generic_import_map), a reference is resolved through that file's own
imports first, authoritatively: an import naming a file this diff never parsed drops the
edge rather than falling through to a same-named guess. Only a name absent from the import
map falls back to a same-file, then a same-named, match -- otherwise a real risk of pointing
at the wrong same-named target. Heritage clauses (`extends`/`implements`) are only extracted
for grammars confirmed to expose a `class_heritage` node -- currently TypeScript and
JavaScript; every other language still shows only `uses`. A file whose extension has no
reachable grammar still lands in skipped.unsupported.

An `extends`/`implements` target commonly lives one hop past the diff-and-coupling
neighbourhood this module otherwise parses (an interface in `src/ports/`, say); see
modules_for_ref for the one extra hop fetched for that case, marked "context" rather than
"unchanged" so a reader doesn't read it as edited.

Stdlib only at import time; tree-sitter is an optional runtime dependency loaded lazily, with
a fallback path if it's missing (see _get_parser). No network. See human-review/SKILL.md for
how this fits the recap flow.
"""

import argparse
import ast
import functools
import glob
import importlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from coupling import (  # noqa: E402  shares ref reading and module resolution
    edges_for_ref,
    git_show,
    is_noise_file,
    is_test_path,
    python_package_parts,
    resolve_base,
    resolve_js_import,
    resolve_python_module,
    run_git,
    short_labels,
    TEST_PATH_RE,
)

MAX_NODES = 40
MAX_EDGES = 60
MAX_MEMBERS = 6
# Caps modules_for_ref's one-hop heritage widening: fan-out there is unbounded (every
# distinct base/implements target across the whole neighbourhood), and each one costs a
# git show plus a tree-sitter parse.
MAX_HERITAGE_REFS = 20

# Extension -> (tree_sitter_<module> suffix, language_<variant> suffix or None for a bare
# language()). Grammars bundled with graphify; see _get_parser for resolution order.
GENERIC_LANG = {
    ".ts": ("typescript", "typescript"),
    ".tsx": ("typescript", "tsx"),
    ".js": ("javascript", None),
    ".jsx": ("javascript", None),
    ".mjs": ("javascript", None),
    ".cjs": ("javascript", None),
    ".go": ("go", None),
    ".rs": ("rust", None),
    ".java": ("java", None),
    ".rb": ("ruby", None),
    ".kt": ("kotlin", None),
    ".kts": ("kotlin", None),
    ".swift": ("swift", None),
    ".scala": ("scala", None),
    ".c": ("c", None),
    ".h": ("c", None),
    ".cpp": ("cpp", None),
    ".cc": ("cpp", None),
    ".hpp": ("cpp", None),
    ".cs": ("c_sharp", None),
    ".php": ("php", "php"),
    ".lua": ("lua", None),
    ".ex": ("elixir", None),
    ".exs": ("elixir", None),
    ".jl": ("julia", None),
    # bash deliberately absent: coupling.NOISE_EXTENSIONS claims .sh/.bash first, so a shell
    # file never reaches this map.
    ".zig": ("zig", None),
    ".ps1": ("powershell", None),
    ".m": ("objc", None),
}

# tree-sitter node types are near-identical across grammars, so one set of regexes covers
# all of them. The suffix is optional because some grammars use bare names (Ruby: "class",
# "method") instead of a "_declaration"/"_definition" tail.
_NODE_SUFFIX = r"(_(declaration|definition|specifier|item|spec|statement|expression))?$"
TYPE_RE = {
    # Java's method_invocation matches both call and func; check call first or every call
    # site gets misfiled as a definition.
    "call": re.compile(r"^(call|method_invocation|invocation)" + _NODE_SUFFIX + r"|_call$"),
    "func": re.compile(
        r"^(function|method|func|fn|constructor|subroutine|singleton_method|"
        r"method_invocation)" + _NODE_SUFFIX
    ),
    "class": re.compile(
        r"^(class|struct|interface|trait|impl|object|enum|module|record|protocol|type)"
        + _NODE_SUFFIX
    ),
}
NAME_FIELDS = ("name", "declarator", "function", "type", "pattern")


def _kind_of(node_type):
    if TYPE_RE["call"].search(node_type):
        return "call"
    if TYPE_RE["func"].search(node_type):
        return "func"
    if TYPE_RE["class"].search(node_type):
        return "class"
    return None


def _label(node, src):
    """A node's name, or None. For a dotted callee (`c.upload()`) this keeps only the tail,
    matching by method name alone since the generic path can't resolve `c` to a type."""
    for field in NAME_FIELDS:
        child = node.child_by_field_name(field)
        if child is not None:
            text = src[child.start_byte : child.end_byte].decode(errors="replace")
            return text.split("(")[0].strip().rsplit(".", 1)[-1]
    for child in node.children:
        if "identifier" in child.type or child.type in ("type_identifier", "constant"):
            return src[child.start_byte : child.end_byte].decode(errors="replace")
    return None


def _graphify_site_packages():
    matches = sorted(glob.glob(
        str(Path.home() / ".local/share/uv/tools/graphifyy/lib/python3.*/site-packages")
    ))
    return matches[-1] if matches else None


def _language_fn(module, variant):
    """tree_sitter_<lang> modules expose a bare `language()`, except multi-grammar packages
    (tree_sitter_typescript: language_typescript/language_tsx; tree_sitter_php: language_php/
    language_php_only), which need the variant-suffixed one."""
    if hasattr(module, "language"):
        return module.language
    if variant and hasattr(module, f"language_{variant}"):
        return getattr(module, f"language_{variant}")
    for name in dir(module):
        if name.startswith("language_") and callable(getattr(module, name)):
            return getattr(module, name)
    return None


@functools.lru_cache(maxsize=None)
def _get_parser(lang, variant):
    """A tree-sitter Parser for `lang`, or None. Tries a real install first, then graphify's
    bundled grammars (in a python 3.12 venv, compatible with this interpreter, but a version
    bump there could silently stop working), then gives up."""
    if os.environ.get("VISUAL_DIFF_NO_TREE_SITTER"):  # test seam for the no-parser path
        return None
    try:
        from tree_sitter_language_pack import get_parser
        return get_parser(variant or lang)
    except Exception:
        pass

    try:
        import tree_sitter
    except ImportError:
        site_packages = _graphify_site_packages()
        if not site_packages:
            return None
        sys.path.insert(0, site_packages)
        try:
            import tree_sitter
        except ImportError:
            return None

    try:
        module = importlib.import_module(f"tree_sitter_{lang}")
        language_fn = _language_fn(module, variant)
        if language_fn is None:
            return None
        return tree_sitter.Parser(tree_sitter.Language(language_fn()))
    except Exception:
        return None


def generic_parser_for(path):
    info = GENERIC_LANG.get(Path(path).suffix.lower())
    return _get_parser(*info) if info else None


def detect_lang(path):
    """Which parse path a file takes: "python" (ast), "generic" (tree-sitter), or None."""
    if path.endswith(".py"):
        return "python"
    return "generic" if generic_parser_for(path) else None


# Extensions whose import syntax parse_generic_import_map knows how to read. A language
# outside this set gets no import map and keeps the same-name-match behaviour it always had.
GENERIC_IMPORT_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}

_IMPORT_CLAUSE_RE = re.compile(r"""\bimport\s+(?:type\s+)?(.+?)\s+from\s+['"]([^'"]+)['"]""")
_REQUIRE_DESTRUCTURE_RE = re.compile(
    r"""\b(?:const|let|var)\s*\{([^}]+)\}\s*=\s*require\(\s*['"]([^'"]+)['"]\s*\)"""
)
_REQUIRE_DEFAULT_RE = re.compile(
    r"""\b(?:const|let|var)\s+(\w+)\s*=\s*require\(\s*['"]([^'"]+)['"]\s*\)"""
)


def _import_clause_names(clause):
    """Local names bound by an ES import clause, the part between `import` and `from`:
    a default (`Default`), a namespace (`* as ns`), named imports (`{ A, B as C }`), or a
    default plus named imports together."""
    clause = clause.strip()
    if clause.startswith("*"):
        match = re.match(r"\*\s+as\s+(\w+)", clause)
        return [match.group(1)] if match else []
    names = []
    brace = re.search(r"\{([^}]*)\}", clause)
    default_part = (clause[: brace.start()] if brace else clause).strip().rstrip(",").strip()
    if re.fullmatch(r"\w+", default_part):
        names.append(default_part)
    if brace:
        for item in brace.group(1).split(","):
            item = item.strip()
            local = item.rsplit(" as ", 1)[-1].strip() if item else ""
            if local:
                names.append(local)
    return names


def parse_generic_import_map(path, content, files_set):
    """local name -> resolved file path, from this file's own JS/TS import statements. Reuses
    coupling.resolve_js_import, so a bare/third-party specifier resolves to nothing and a
    reference through it falls back to same-file or same-named matching instead."""
    bindings = {}
    for clause, spec in _IMPORT_CLAUSE_RE.findall(content):
        resolved = resolve_js_import(spec, path, files_set)
        if resolved:
            for name in _import_clause_names(clause):
                bindings[name] = resolved
    for names_blob, spec in _REQUIRE_DESTRUCTURE_RE.findall(content):
        resolved = resolve_js_import(spec, path, files_set)
        if resolved:
            for item in names_blob.split(","):
                local = item.split(":")[-1].strip()
                if local:
                    bindings[local] = resolved
    for name, spec in _REQUIRE_DEFAULT_RE.findall(content):
        resolved = resolve_js_import(spec, path, files_set)
        if resolved:
            bindings[name] = resolved
    return bindings


def _heritage_clause_names(clause, src):
    """Name(s) inside one extends/implements clause node: TypeScript gives it a `value`
    field (`extends Base<T>`); JavaScript (see _heritage_names) has no such field, so fall
    back to any identifier child (`implements A, B`)."""
    value = clause.child_by_field_name("value")
    if value is not None:
        return [src[value.start_byte : value.end_byte].decode(errors="replace")]
    return [
        src[c.start_byte : c.end_byte].decode(errors="replace")
        for c in clause.children
        if c.type in ("identifier", "type_identifier")
    ]


def _heritage_names(node, src):
    """(extends, implements) raw name lists from a class node's heritage clause. TypeScript
    nests `extends_clause`/`implements_clause` inside `class_heritage`; JavaScript has no
    `implements`, and `class_heritage` there is itself the extends clause. Any other grammar
    has no `class_heritage` child here, so this returns ([], [])."""
    heritage = next((c for c in node.children if c.type == "class_heritage"), None)
    if heritage is None:
        return [], []
    sub_clauses = [
        c for c in heritage.children if c.type in ("extends_clause", "implements_clause")
    ]
    if not sub_clauses:
        return _heritage_clause_names(heritage, src), []
    extends, implements = [], []
    for clause in sub_clauses:
        target = extends if clause.type == "extends_clause" else implements
        target.extend(_heritage_clause_names(clause, src))
    return extends, implements


def _heritage_target_name(text):
    """Strip a generic type argument (`Base<T>` -> `Base`) and keep only the tail of a
    dotted/namespaced name, matching how _label already treats a dotted callee."""
    return text.split("<", 1)[0].strip().rsplit(".", 1)[-1]


def parse_module_generic(path, content, files_set):
    """Best-effort structures for a non-Python file: same return shape as parse_module, plus
    an import map (see parse_generic_import_map) for the languages that have one. Calls are
    attributed to the nearest top-level class or function, mirroring parse_module's owners."""
    parser = generic_parser_for(path)
    if parser is None:
        return None
    src = content.encode("utf-8", errors="replace")
    classes, functions, calls_by_owner = {}, {}, {}

    def visit(node, owner):
        kind = _kind_of(node.type)
        name = _label(node, src) if kind else None
        next_owner = owner
        if kind == "call" and owner and name:
            calls_by_owner.setdefault(owner, []).append(name)
        elif kind == "func" and name:
            if owner is None:
                functions.setdefault(name, {})
                next_owner = name
            elif owner in classes:
                classes[owner]["members"].append(name)
        elif kind == "class" and name and owner is None:
            if name not in classes:
                extends, implements = _heritage_names(node, src)
                classes[name] = {
                    "members": [],
                    "bases": [_heritage_target_name(n) for n in extends],
                    "implements": [_heritage_target_name(n) for n in implements],
                }
            next_owner = name
        for child in node.children:
            visit(child, next_owner)

    try:
        tree = parser.parse(src)
        visit(tree.root_node, None)
    except RecursionError:  # pathological nesting, e.g. minified/generated code
        return None
    except Exception:
        return None
    import_map = (
        parse_generic_import_map(path, content, files_set)
        if Path(path).suffix.lower() in GENERIC_IMPORT_EXTS
        else {}
    )
    return {"path": path, "classes": classes, "functions": functions,
            "calls_by_owner": calls_by_owner, "import_map": import_map, "generic": True}


def _known_by_name(modules):
    """name -> sorted [(path, name), ...] over every parsed module, the last-resort lookup
    when neither an import nor the reference's own file can place it."""
    known = {}
    for path, module in modules.items():
        for name in list(module["classes"]) + list(module["functions"]):
            known.setdefault(name, []).append((path, name))
    for candidates in known.values():
        candidates.sort()
    return known


def _resolve_generic_symbol(name, path, module, modules, known_by_name):
    """(path, symbol) for `name` as referenced inside `module`. This file's own import map is
    authoritative when it names a declaring path for `name`: resolved if that path was parsed
    and declares the symbol, dropped (None) otherwise. An unparsed import target has no node
    in this diff-scoped graph, and falling through to the blind scan below would risk landing
    on an unrelated same-named symbol -- the exact false edge this resolution order exists to
    kill (see the module docstring). Only when the import map has no entry for `name` does
    same-file, then a same-named match anywhere in the diff, apply."""
    import_map = module.get("import_map", {})
    if name in import_map:
        target = modules.get(import_map[name])
        if target and (name in target["classes"] or name in target["functions"]):
            return (import_map[name], name)
        return None
    if name in module["classes"] or name in module["functions"]:
        return (path, name)
    candidates = known_by_name.get(name)
    return candidates[0] if candidates else None


def generic_uses_edges(modules, known_by_name):
    """"uses" edges for generic modules, resolved through _resolve_generic_symbol."""
    edges = {}
    for path, module in modules.items():
        for owner, called in module.get("calls_by_owner", {}).items():
            src = (path, owner)
            for name in called:
                dst = _resolve_generic_symbol(name, path, module, modules, known_by_name)
                if dst and dst != src:
                    edges[(*src, *dst)] = "uses"
    return edges


def generic_heritage_edges(modules, known_by_name):
    """extends/implements edges for generic classes (see _heritage_names), resolved the same
    way generic_uses_edges resolves a call. Python's own extends comes from structure_edges
    instead, so a Python module is skipped here."""
    edges = {}
    for path, module in modules.items():
        if not module.get("generic"):
            continue
        for name, info in module["classes"].items():
            src = (path, name)
            for base in info.get("bases", []):
                dst = _resolve_generic_symbol(base, path, module, modules, known_by_name)
                if dst and dst != src:
                    edges[(*src, *dst)] = "extends"
            for iface in info.get("implements", []):
                dst = _resolve_generic_symbol(iface, path, module, modules, known_by_name)
                if dst and dst != src:
                    edges.setdefault((*src, *dst), "implements")  # extends wins a collision
    return edges


def _dotted(node):
    """Source text of a dotted name node, or None for anything else."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def parse_module(path, content, files_set):
    """Top-level structures, plus the maps needed to resolve a name to another module."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None

    module_aliases = {}  # local name -> module path, from `import pkg.mod [as m]`
    symbols = {}  # local name -> (module path, symbol), from `from pkg.mod import Thing`
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                resolved = resolve_python_module(alias.name.split("."), files_set)
                if resolved:
                    module_aliases[alias.asname or alias.name] = resolved
        elif isinstance(node, ast.ImportFrom):
            base = python_package_parts(path, node.level) if node.level > 0 else []
            mod_parts = base + (node.module.split(".") if node.module else [])
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                submodule = resolve_python_module(mod_parts + [alias.name], files_set)
                if submodule:
                    module_aliases[local] = submodule
                    continue
                owner = resolve_python_module(mod_parts, files_set)
                if owner:
                    symbols[local] = (owner, alias.name)

    classes, functions = {}, {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes[node.name] = {
                "node": node,
                "bases": [d for d in (_dotted(b) for b in node.bases) if d],
                "members": [
                    child.name
                    for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                ],
            }
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = {"node": node}

    return {
        "path": path,
        "classes": classes,
        "functions": functions,
        "module_aliases": module_aliases,
        "symbols": symbols,
    }


def resolve_name(module, dotted):
    """Resolve a dotted name as written in `module` to a (path, symbol) pair, or None."""
    if not dotted:
        return None
    head, _, rest = dotted.partition(".")
    if head in module["module_aliases"]:
        # `mod.Thing` names Thing inside that module; a bare `mod` names no structure.
        tail = rest.split(".")[0] if rest else ""
        return (module["module_aliases"][head], tail) if tail else None
    if head in module["symbols"]:
        return module["symbols"][head]
    if head in module["classes"] or head in module["functions"]:
        return (module["path"], head)
    return None


def structure_edges(modules):
    """{(src_path, src_sym, dst_path, dst_sym): kind} over the parsed modules."""
    known = {
        (m["path"], name)
        for m in modules.values()
        for name in list(m["classes"]) + list(m["functions"])
    }
    edges = {}

    def add(src, dst, kind):
        if dst not in known or src == dst:
            return
        key = (*src, *dst)
        if kind == "extends" or key not in edges:
            edges[key] = kind

    for module in modules.values():
        if module.get("generic"):
            continue  # generic modules have no ast node to walk; see generic_uses_edges
        owners = [(name, info, "class") for name, info in module["classes"].items()]
        owners += [(name, info, "function") for name, info in module["functions"].items()]
        for name, info, kind in owners:
            src = (module["path"], name)
            if kind == "class":
                for base in info["bases"]:
                    resolved = resolve_name(module, base)
                    if resolved:
                        add(src, resolved, "extends")
            for node in ast.walk(info["node"]):
                if isinstance(node, ast.Attribute):
                    resolved = resolve_name(module, _dotted(node))
                elif isinstance(node, ast.Name):
                    resolved = resolve_name(module, node.id)
                else:
                    continue
                if resolved:
                    add(src, resolved, "uses")
    return edges


def _heritage_ref_paths(modules):
    """File paths named by a class's own extends/implements clause, resolved through that
    file's own imports (generic) or module_aliases/symbols (Python) whether or not the
    target is already parsed -- the candidate set for modules_for_ref's one extra hop."""
    paths = set()
    for module in modules.values():
        if module.get("generic"):
            import_map = module.get("import_map", {})
            for info in module["classes"].values():
                for name in info.get("bases", []) + info.get("implements", []):
                    target = import_map.get(name)
                    if target:
                        paths.add(target)
        else:
            for info in module["classes"].values():
                for base in info["bases"]:
                    resolved = resolve_name(module, base)
                    if resolved:
                        paths.add(resolved[0])
    return paths


def _parse_structure_at(repo, ref, path, files_set, unparseable, unsupported):
    """One file's structures at `ref`: Python through parse_module (exact), everything else
    through parse_module_generic. None for anything absent, noise, or ungrammared, recording
    the reason into `unparseable`/`unsupported` when parsing itself is what failed."""
    content = git_show(repo, ref, path)
    if content is None:
        return None
    if detect_lang(path) == "python":
        parsed = parse_module(path, content, files_set)
        if parsed is None:
            unparseable.add(path)
        return parsed
    if is_noise_file(path):
        return None
    parsed = parse_module_generic(path, content, files_set)
    if parsed is None:
        unsupported.add(path)
    return parsed


def modules_for_ref(repo, ref, changed_files, changed_set, unparseable, unsupported):
    """Parse the changed files plus their one file hop in either direction. Then one more
    hop, heritage only: an extends/implements target commonly lives outside even that
    neighbourhood (an interface in src/ports/, say, imported by a file that is itself only a
    coupling neighbour, not a changed file). Such a path comes back separately as
    `context_paths`, for analyse() to mark as context rather than change.

    Returns (modules, context_paths, files_set, heritage_capped); files_set lets analyse()
    reuse this ref's repo file listing when it fetches a context path at the other ref too.
    heritage_capped is True when more distinct heritage targets were found than
    MAX_HERITAGE_REFS allows fetching, so analyse() can tell a reader some context nodes were
    left out rather than silently showing fewer.
    """
    file_edges = edges_for_ref(repo, ref, changed_files, changed_set, set())
    neighbourhood = set(changed_files)
    for importer, imported in file_edges:
        neighbourhood.update({importer, imported})

    files_set = set(run_git(repo, ["ls-tree", "-r", "--name-only", ref]).stdout.splitlines())

    modules = {}
    for path in sorted(neighbourhood):
        parsed = _parse_structure_at(repo, ref, path, files_set, unparseable, unsupported)
        if parsed is not None:
            modules[path] = parsed

    context_paths = set()
    all_heritage_refs = sorted(_heritage_ref_paths(modules) - set(modules))
    heritage_capped = len(all_heritage_refs) > MAX_HERITAGE_REFS
    for path in all_heritage_refs[:MAX_HERITAGE_REFS]:
        parsed = _parse_structure_at(repo, ref, path, files_set, unparseable, unsupported)
        if parsed is not None:
            modules[path] = parsed
            context_paths.add(path)

    return modules, context_paths, files_set, heritage_capped


def members_of(module_maps, path, symbol):
    """Members from the first ref that still has this structure, so one that was deleted
    is still drawn with the methods it had."""
    for modules in module_maps:
        module = modules.get(path)
        if not module:
            continue
        if symbol in module["classes"]:
            return module["classes"][symbol]["members"]
        if symbol in module["functions"]:
            return []
    return None


def structures_in(modules):
    return {
        (path, name)
        for path, module in modules.items()
        for name in list(module["classes"]) + list(module["functions"])
    }


def rank(edge, changed_set):
    """Changed-file edges first, then changed relations, then production code before tests,
    so truncation cuts context and test scaffolding rather than the thing the reader opened
    the recap for. A test calling a helper is real coupling, just the least interesting kind
    when the diagram is already too big to read."""
    src_path, _, dst_path, _, _, state = edge
    touches = (src_path in changed_set) + (dst_path in changed_set)
    tests = is_test_path(src_path) + is_test_path(dst_path)
    return (-touches, 0 if state != "unchanged" else 1, tests, src_path, dst_path)


def build_mermaid(edges, module_maps, changed_set, structure_states):
    """flowchart LR, one subgraph per file, same line-style vocabulary as coupling.py."""
    ordered = sorted(edges, key=lambda e: rank(e, changed_set))
    kept, nodes = [], []
    for edge in ordered:
        src, dst = (edge[0], edge[1]), (edge[2], edge[3])
        fresh = [n for n in (src, dst) if n not in nodes]
        if len(kept) >= MAX_EDGES or len(nodes) + len(fresh) > MAX_NODES:
            continue
        nodes.extend(fresh)
        kept.append(edge)
    truncated = len(kept) < len(edges)

    # A structure that gained or lost no relation is still a structure change worth seeing,
    # so an added or removed one is drawn even with nothing attached to it. A context node
    # always arrives via a heritage edge already, so it's excluded here.
    for node in sorted(
        n for n, state in structure_states.items() if state not in ("unchanged", "context")
    ):
        if node in nodes:
            continue
        if len(nodes) >= MAX_NODES:
            truncated = True
            continue
        nodes.append(node)

    if not nodes:
        return "", False, {}

    by_file = {}
    for path, symbol in nodes:
        by_file.setdefault(path, []).append(symbol)

    node_id = {node: f"S{i}" for i, node in enumerate(nodes)}
    titles = short_labels(sorted(by_file))
    lines = ["flowchart LR"]
    for i, (path, symbols) in enumerate(sorted(by_file.items())):
        lines.append(f'  subgraph F{i}["{titles[path]}"]')
        for symbol in sorted(symbols):
            members = members_of(module_maps, path, symbol)
            # [] is a top-level function, so it gets the call parens; a non-empty list is a
            # class, so its methods go under the name; None means neither ref could tell.
            label = f"{symbol}()" if members == [] else symbol
            label += {
                "added": " (new)", "removed": " (gone)", "context": " (context)",
            }.get(structure_states.get((path, symbol), "unchanged"), "")
            if members:
                label += "<br/>" + "<br/>".join(f"{m}()" for m in members[:MAX_MEMBERS])
                if len(members) > MAX_MEMBERS:
                    label += f"<br/>+{len(members) - MAX_MEMBERS} more"
            lines.append(f'    {node_id[(path, symbol)]}["{label}"]')
        lines.append("  end")

    arrow = {"added": "==>", "removed": "-.->", "unchanged": "-->"}
    for src_path, src_sym, dst_path, dst_sym, kind, state in kept:
        src = node_id[(src_path, src_sym)]
        dst = node_id[(dst_path, dst_sym)]
        # `extends`/`implements` are labelled; `uses` is the overwhelming majority, so
        # labelling it too would stamp the same word on every arrow, which the legend already
        # says once and which crowds the boxes it sits between.
        label = {"extends": "|extends|", "implements": "|implements|"}.get(kind, "")
        lines.append(f"  {src} {arrow[state]}{label} {dst}")
    ids = {node_id[node]: ":".join(node) for node in nodes}
    return "\n".join(lines), truncated, ids


def analyse(repo, base, head, files):
    for ref in (base, head):
        if run_git(repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"]).returncode != 0:
            raise RuntimeError(f"bad ref: {ref!r}")

    base = resolve_base(repo, base, head)

    if files:
        changed_files = list(files)
    else:
        diff = run_git(repo, ["diff", "--name-only", f"{base}...{head}"])
        if diff.returncode != 0:
            raise RuntimeError(f"git diff failed: {diff.stderr.strip()}")
        changed_files = [line for line in diff.stdout.splitlines() if line]
    changed_set = set(changed_files)

    unparseable = set()
    unsupported = set()
    modules_base, context_base, files_set_base, heritage_capped_base = modules_for_ref(
        repo, base, changed_files, changed_set, unparseable, unsupported
    )
    modules_head, context_head, files_set_head, heritage_capped_head = modules_for_ref(
        repo, head, changed_files, changed_set, unparseable, unsupported
    )

    # A path only one ref's heritage widening happened to need (e.g. an interface a class
    # gained mid-diff) is fetched at the other ref too, so it reads as context rather than
    # as a structure the diff added or removed.
    for path in sorted((context_base | context_head) - set(modules_base)):
        parsed = _parse_structure_at(
            repo, base, path, files_set_base, unparseable, unsupported
        )
        if parsed is not None:
            modules_base[path] = parsed
            context_base.add(path)
    for path in sorted((context_base | context_head) - set(modules_head)):
        parsed = _parse_structure_at(
            repo, head, path, files_set_head, unparseable, unsupported
        )
        if parsed is not None:
            modules_head[path] = parsed
            context_head.add(path)

    known_by_name_base = _known_by_name(modules_base)
    edges_base = structure_edges(modules_base)
    edges_base.update(generic_uses_edges(modules_base, known_by_name_base))
    edges_base.update(generic_heritage_edges(modules_base, known_by_name_base))
    known_by_name_head = _known_by_name(modules_head)
    edges_head = structure_edges(modules_head)
    edges_head.update(generic_uses_edges(modules_head, known_by_name_head))
    edges_head.update(generic_heritage_edges(modules_head, known_by_name_head))

    added = sorted(set(edges_head) - set(edges_base))
    removed = sorted(set(edges_base) - set(edges_head))
    unchanged = sorted(set(edges_head) & set(edges_base))

    states = (
        [(*e, edges_head[e], "added") for e in added]
        + [(*e, edges_base[e], "removed") for e in removed]
        + [(*e, edges_head[e], "unchanged") for e in unchanged]
    )

    at_base, at_head = structures_in(modules_base), structures_in(modules_head)
    structures_added = sorted(at_head - at_base)
    structures_removed = sorted(at_base - at_head)
    structure_states = {node: "unchanged" for node in at_base | at_head}
    structure_states.update({node: "added" for node in structures_added})
    structure_states.update({node: "removed" for node in structures_removed})

    # A path fetched only for its extends/implements target (see modules_for_ref) is context,
    # not a change -- but never downgrade a node the diff itself added or removed.
    context_paths = context_base | context_head
    for node in structure_states:
        if node[0] in context_paths and structure_states[node] == "unchanged":
            structure_states[node] = "context"

    mermaid, truncated, ids = build_mermaid(
        states, [modules_head, modules_base], changed_set, structure_states
    )

    # Tests-hidden variant: filter the same edge keys and isolated-node states build_mermaid
    # already computed, rather than a second matcher or a second base/head diff.
    def _not_test_edge(edge_key):
        return not (is_test_path(edge_key[0]) or is_test_path(edge_key[2]))

    added_nt = [e for e in added if _not_test_edge(e)]
    removed_nt = [e for e in removed if _not_test_edge(e)]
    unchanged_nt = [e for e in unchanged if _not_test_edge(e)]
    states_nt = (
        [(*e, edges_head[e], "added") for e in added_nt]
        + [(*e, edges_base[e], "removed") for e in removed_nt]
        + [(*e, edges_head[e], "unchanged") for e in unchanged_nt]
    )
    structure_states_nt = {
        node: state for node, state in structure_states.items() if not is_test_path(node[0])
    }
    mermaid_notests, truncated_notests, ids_notests = build_mermaid(
        states_nt, [modules_head, modules_base], changed_set, structure_states_nt
    )

    def as_json(edge_keys, source):
        return [
            {"from": [k[0], k[1]], "to": [k[2], k[3]], "kind": source[k]}
            for k in edge_keys
        ]

    skipped = sorted(unsupported & changed_set)
    moved = bool(added or removed or structures_added or structures_removed)
    note = ""
    if not moved and unchanged:
        note = (
            "No structure appeared, disappeared or changed who it relates to, so the "
            "diagram shows the relations as they stand at both refs."
        )
    if truncated:
        note = (note + " ").strip() + (
            f"Diagram capped at {MAX_NODES} structures and {MAX_EDGES} relations, showing "
            "the ones closest to the changed files first."
        )
    if heritage_capped_base or heritage_capped_head:
        note = (note + " ").strip() + (
            f"More than {MAX_HERITAGE_REFS} extends/implements targets pointed outside the "
            "parsed files; some context nodes were left out."
        )

    return {
        "changed": moved,
        "added": as_json(added, edges_head),
        "removed": as_json(removed, edges_base),
        "unchanged": as_json(unchanged, edges_head),
        "added_notests": as_json(added_nt, edges_head),
        "removed_notests": as_json(removed_nt, edges_base),
        "unchanged_notests": as_json(unchanged_nt, edges_head),
        "structures_added": [list(node) for node in structures_added],
        "structures_removed": [list(node) for node in structures_removed],
        "mermaid": mermaid,
        "ids": ids,
        "mermaid_notests": mermaid_notests,
        "ids_notests": ids_notests,
        "truncated": truncated,
        "truncated_notests": truncated_notests,
        "skipped": {
            "unsupported": skipped,
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
        print(json.dumps(analyse(args.repo, args.base, args.head, args.files)))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
