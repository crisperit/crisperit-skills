#!/usr/bin/env python3
"""Build a script-generated structure view: real classes/interfaces/functions touched by a
change, with new/changed/moved/removed/unchanged state, call edges and column ranks.

Usage: python3 structure.py --repo <path> --base <ref> --head <ref> --symdelta <symdelta.json>
    [--analysis <analysis.json>] --out <structure.json>

symdelta.json's own node `state` is unreliable for existence: it answers "did the LSP see a
call edge appear", not "did this symbol exist on base", so a modified function that only
gains a new caller comes out "new" even though it was already there (see defineTool /
Server.start on PR #6). This script answers existence itself, per changed file, by parsing
the file at both refs and diffing declared symbols directly -- classes, interfaces and
top-level functions/methods, never local closures. A symbol whose name reappears in a
different file, same kind, with either the same body or a shared member (the other side of a
same-name pair neither ref's diff --name-status treated as a rename) comes out "moved" rather
than "new"+"removed"; two unrelated symbols that merely share a name stay removed+new.

Two parse tiers: TypeScript/JS through a lazily-loaded tree-sitter grammar (bundled with
graphify; see _get_ts_parser), Go through a tiny stdlib-only helper program under
extractors/go/structure (go/parser over one file's content via stdin, no go/packages, no
module resolution). Call edges come from symdelta.json, rolled up from symbol to component
level; columns reuse walkthrough.caller_depth over the touched files. Anything else, or a
repo where the chosen language's tool is missing, bails out early (language: null, with a
reason), the same pattern symdelta.py itself follows.

Stdlib only for the git/diff/JSON plumbing; tree-sitter is an optional runtime dependency
loaded lazily, with a fallback path if it's missing.
"""

import argparse
import functools
import glob
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
from links import resolve_base, run_git  # noqa: E402  one owner for git helpers (subprocess + merge-base)
from validate_analysis import is_test_path  # noqa: E402  one owner for test-path classification
from walkthrough import caller_depth  # noqa: E402  one owner for the caller-first column order
import symdelta  # noqa: E402  reuse its writable-cache resolution for the compiled Go helper

MAX_COMPONENTS = 30

TS_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
GO_EXT = ".go"

GO_STRUCTURE_SRC_DIR = SCRIPT_DIR / "extractors" / "go" / "structure"
GO_STRUCTURE_BIN = symdelta.CACHE_DIR / "structure-go-extractor"

_WS_RE = re.compile(r"\s+")


def git_show(repo, ref, path):
    """A file's content at ref, or None if it doesn't exist there."""
    result = run_git(repo, ["show", f"{ref}:{path}"])
    if result.returncode != 0:
        return None
    return result.stdout


def changed_files(repo, base, head, paths=()):
    """[(status, old_path, new_path)] from `git diff --name-status -M`. status is one of
    "A", "M", "D", "R"; old/new is None on the side that doesn't apply. Rename detection only
    catches a genuine content-similarity rename -- a symbol moved into an otherwise-rewritten
    file (see the module docstring) still shows up as a plain D+A pair here, so build_components
    does its own name-based move detection rather than relying on this.

    Two-dot, not three-dot: `base` arrives already resolved to the merge-base (analyse() does
    that before calling here), and explain mode's empty-baseline `base` shares no history with
    `head` at all, which three-dot rejects with "no merge base". Two-dot on the resolved value
    matches old three-dot behaviour for a real ref pair too (same convention as symdelta.py's
    analyse()).

    `paths` is an optional pathspec -- repo-relative files or directories -- scoping the diff
    itself. Needed in explain mode, where the empty baseline otherwise makes every file in the
    repo a changed file (see references/explain-mode.md)."""
    result = run_git(repo, ["diff", "--name-status", "-M", f"{base}..{head}", "--", *paths])
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr.strip()}")
    out = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R"):
            out.append(("R", parts[1], parts[2]))
        elif status == "A":
            out.append(("A", None, parts[1]))
        elif status == "D":
            out.append(("D", parts[1], None))
        else:
            out.append(("M", parts[1], parts[1]))
    return out


def detect_lang(path):
    ext = Path(path).suffix.lower()
    if ext in TS_EXTS:
        return "typescript"
    if ext == GO_EXT:
        return "go"
    return None


# ---------- TypeScript / JS, via tree-sitter ----------


def _graphify_site_packages():
    matches = sorted(glob.glob(
        str(Path.home() / ".local/share/uv/tools/graphifyy/lib/python3.*/site-packages")
    ))
    return matches[-1] if matches else None


@functools.lru_cache(maxsize=None)
def _get_ts_parser():
    """A tree-sitter Parser for TypeScript, or None. Tries a real install first, then
    graphify's bundled grammar (in a python 3.12 venv, compatible with this interpreter, but a
    version bump there could silently stop working), then gives up."""
    if os.environ.get("STRUCTURE_NO_TREE_SITTER"):  # test seam for the no-parser path
        return None
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
        module = importlib.import_module("tree_sitter_typescript")
        language_fn = getattr(module, "language_typescript", None)
        if language_fn is None:
            return None
        return tree_sitter.Parser(tree_sitter.Language(language_fn()))
    except Exception:
        return None


def _text(node, src):
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _body_hash(node, src):
    """A body-shape fingerprint that ignores whitespace-only differences."""
    normalized = _WS_RE.sub(" ", _text(node, src)).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _strip_generic(name):
    """`Base<T>` -> `Base`, and keep only the tail of a dotted/namespaced name."""
    return name.split("<", 1)[0].strip().rsplit(".", 1)[-1]


def _heritage_clause_names(clause, src):
    value = clause.child_by_field_name("value")
    if value is not None:
        return [_text(value, src)]
    return [
        _text(c, src) for c in clause.children if c.type in ("identifier", "type_identifier")
    ]


def _class_heritage(node, src):
    """(extends, implements) name lists for a class_declaration/abstract_class_declaration."""
    heritage = next((c for c in node.children if c.type == "class_heritage"), None)
    if heritage is None:
        return [], []
    extends, implements = [], []
    for clause in heritage.children:
        if clause.type == "extends_clause":
            extends.extend(_heritage_clause_names(clause, src))
        elif clause.type == "implements_clause":
            implements.extend(_heritage_clause_names(clause, src))
    return extends, implements


def _interface_extends(node, src):
    clause = next((c for c in node.children if c.type == "extends_type_clause"), None)
    if clause is None:
        return []
    return [_text(c, src) for c in clause.children if c.type in ("identifier", "type_identifier")]


def _members(body, member_node_type, src):
    """{member_name: body_hash}. body is a class_body/interface_body node, or None."""
    result = {}
    if body is None:
        return result
    for child in body.children:
        if child.type == member_node_type:
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                result[_text(name_node, src)] = _body_hash(child, src)
    return result


def _add_class_or_interface(node, kind, components, src):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(name_node, src)
    if kind == "class":
        extends, implements = _class_heritage(node, src)
        body = next((c for c in node.children if c.type == "class_body"), None)
        members = _members(body, "method_definition", src)
    else:
        extends, implements = _interface_extends(node, src), []
        body = next((c for c in node.children if c.type == "interface_body"), None)
        members = _members(body, "method_signature", src)
    components[name] = {
        "kind": kind,
        "hash": _body_hash(node, src),
        "members": members,
        "extends": [_strip_generic(n) for n in extends],
        "implements": [_strip_generic(n) for n in implements],
    }


def _add_function(node, components, src):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(name_node, src)
    components[name] = {
        "kind": "function", "hash": _body_hash(node, src), "members": {},
        "extends": [], "implements": [],
    }


def parse_ts_module(content):
    """{name: {kind, hash, members, extends, implements}} for top-level classes, interfaces
    and functions declared in `content` -- never a local closure or nested function, since
    only the program's own direct children (and one export_statement layer over them) are
    ever visited. Returns None when no tree-sitter TypeScript grammar is available."""
    parser = _get_ts_parser()
    if parser is None:
        return None
    src = content.encode("utf-8", errors="replace")
    try:
        tree = parser.parse(src)
    except Exception:
        return None
    components = {}
    for child in tree.root_node.children:
        actual = child
        if child.type == "export_statement":
            actual = next(
                (c for c in child.children if c.type not in ("export", "default")), child
            )
        if actual.type in ("class_declaration", "abstract_class_declaration"):
            _add_class_or_interface(actual, "class", components, src)
        elif actual.type == "interface_declaration":
            _add_class_or_interface(actual, "interface", components, src)
        elif actual.type == "function_declaration":
            _add_function(actual, components, src)
        elif actual.type == "lexical_declaration":
            for decl in actual.children:
                if decl.type != "variable_declarator":
                    continue
                value = decl.child_by_field_name("value")
                name_node = decl.child_by_field_name("name")
                if value is None or name_node is None:
                    continue
                # A factory-call assignment (`const x = defineTool({...})`) is not itself a
                # function declaration, so it's excluded here.
                if value.type not in ("arrow_function", "function_expression"):
                    continue
                components[_text(name_node, src)] = {
                    "kind": "function", "hash": _body_hash(value, src), "members": {},
                    "extends": [], "implements": [],
                }
    return components


# ---------- Go, via a tiny stdlib-only helper program ----------


@functools.lru_cache(maxsize=None)
def _go_structure_available():
    """Builds the helper into symdelta's own cache dir (never in-repo -- that dir is the one
    place already known to be writable under a sandboxed agent, see
    symdelta._resolve_cache_dir), reusing an existing binary newer than its source."""
    if os.environ.get("STRUCTURE_NO_GO"):  # test seam for the no-toolchain path
        return False
    src = GO_STRUCTURE_SRC_DIR / "main.go"
    if not src.exists():
        return False
    if GO_STRUCTURE_BIN.exists() and GO_STRUCTURE_BIN.stat().st_mtime >= src.stat().st_mtime:
        return True
    GO_STRUCTURE_BIN.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["GOCACHE"] = str(symdelta.CACHE_DIR / "build-cache")
    env["GOMODCACHE"] = str(symdelta.CACHE_DIR / "mod-cache")
    try:
        result = subprocess.run(
            ["go", "build", "-o", str(GO_STRUCTURE_BIN), "."],
            cwd=str(GO_STRUCTURE_SRC_DIR), capture_output=True, text=True, env=env, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def parse_go_module(content):
    """Same return shape as parse_ts_module, for one Go file's content. Returns None when the
    `go` toolchain (or the helper program) isn't available."""
    if not _go_structure_available():
        return None
    try:
        result = subprocess.run(
            [str(GO_STRUCTURE_BIN)], input=content, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    components = {}
    for name, comp in raw.items():
        components[name] = {
            "kind": comp.get("kind", "struct"),
            "hash": comp.get("hash", ""),
            "members": comp.get("member_hashes") or {},
            "extends": comp.get("extends") or [],
            "implements": comp.get("implements") or [],
        }
    return components


PARSERS = {"typescript": parse_ts_module, "go": parse_go_module}
RELEVANT_EXTS = {"typescript": TS_EXTS, "go": {GO_EXT}}


# ---------- delta computation ----------


def _member_states(base_members, head_members):
    base_members = base_members or {}
    head_members = head_members or {}
    out = []
    for name in sorted(set(base_members) | set(head_members)):
        if name in base_members and name in head_members:
            state = "unchanged" if base_members[name] == head_members[name] else "changed"
        elif name in head_members:
            state = "new"
        else:
            state = "removed"
        out.append({"name": name, "state": state})
    return out


def build_components(base_modules, head_modules):
    """(components, pos_to_id): components is {id: {...}} in the output component shape plus
    the heritage lists (stripped by the caller before JSON output); pos_to_id maps every
    (file, name) position at either ref to the component id it ended up under, so edge
    resolution can look a symdelta symbol up by its own (file, name) regardless of which side
    of a move it fell on."""
    base_positions = {(p, n) for p, comps in base_modules.items() for n in comps}
    head_positions = {(p, n) for p, comps in head_modules.items() for n in comps}
    matched = head_positions & base_positions
    head_only = sorted(head_positions - matched)
    base_only = sorted(base_positions - matched)

    base_by_name = defaultdict(list)
    for p, n in base_only:
        base_by_name[n].append(p)
    consumed_base = set()
    moves = {}  # (head_path, name) -> base_path
    for hp, n in head_only:
        head_comp = head_modules[hp][n]
        for bp in base_by_name.get(n, []):
            if (bp, n) in consumed_base:
                continue
            base_comp = base_modules[bp][n]
            # Same name in two unrelated files (a coincidental `Validate` in both an old and a
            # brand-new file) must not read as one symbol that moved: require the same kind,
            # plus either an identical body or at least one shared member name, before treating
            # this as the WorkflowsPort case rather than a plain removed+new pair.
            if base_comp["kind"] != head_comp["kind"]:
                continue
            if base_comp["hash"] != head_comp["hash"] and not (
                set(base_comp["members"]) & set(head_comp["members"])
            ):
                continue
            moves[(hp, n)] = bp
            consumed_base.add((bp, n))
            break
    moved_head = set(moves)

    components = {}
    pos_to_id = {}

    def emit(comp_id, name, kind, file, state, members, extends, implements):
        components[comp_id] = {
            "id": comp_id, "name": name, "kind": kind, "file": file, "state": state,
            "members": members, "_extends": extends, "_implements": implements,
        }

    # sorted(): matched is a set, so iterating it raw is PYTHONHASHSEED-dependent, which would
    # make a same-name collision downstream (_implements_edges_by_id) pick a different winner
    # run to run -- same fix sections.py's _structure_implements_edges applies to its own set.
    for p, n in sorted(matched):
        base_comp, head_comp = base_modules[p][n], head_modules[p][n]
        mstates = _member_states(base_comp["members"], head_comp["members"])
        changed = base_comp["hash"] != head_comp["hash"] or any(
            m["state"] != "unchanged" for m in mstates
        )
        comp_id = f"{p}:{n}"
        emit(comp_id, n, head_comp["kind"], p, "changed" if changed else "unchanged",
             mstates, head_comp["extends"], head_comp["implements"])
        pos_to_id[(p, n)] = comp_id

    for (hp, n), bp in moves.items():
        base_comp, head_comp = base_modules[bp][n], head_modules[hp][n]
        comp_id = f"{hp}:{n}"
        emit(comp_id, n, head_comp["kind"], hp, "moved",
             _member_states(base_comp["members"], head_comp["members"]),
             head_comp["extends"], head_comp["implements"])
        pos_to_id[(hp, n)] = comp_id
        pos_to_id[(bp, n)] = comp_id

    for hp, n in head_only:
        if (hp, n) in moved_head:
            continue
        comp = head_modules[hp][n]
        comp_id = f"{hp}:{n}"
        emit(comp_id, n, comp["kind"], hp, "new", _member_states({}, comp["members"]),
             comp["extends"], comp["implements"])
        pos_to_id[(hp, n)] = comp_id

    for bp, n in base_only:
        if (bp, n) in consumed_base:
            continue
        comp = base_modules[bp][n]
        comp_id = f"{bp}:{n}"
        emit(comp_id, n, comp["kind"], bp, "removed", _member_states(comp["members"], {}),
             comp["extends"], comp["implements"])
        pos_to_id[(bp, n)] = comp_id

    return components, pos_to_id


def build_implements(components):
    out = []
    for comp in components.values():
        for target in comp["_extends"]:
            out.append({"from": comp["id"], "to": target, "kind": "extends"})
        for target in comp["_implements"]:
            out.append({"from": comp["id"], "to": target, "kind": "implements"})
    return out


def build_edges(symdelta_data, pos_to_id):
    """Call edges from symdelta.json, aggregated to component level: a method edge
    (`src:Server.start`) rolls up to its class (`Server`) via the node's own dotted label,
    matching walkthrough.caller_depth's id scheme. Only edges whose both ends landed in
    pos_to_id (i.e. both ends are a component this run parsed) survive."""
    nodes_by_id = {
        n["id"]: n for n in (symdelta_data or {}).get("nodes") or []
        if isinstance(n, dict) and n.get("kind") == "symbol"
    }

    def key_for(node_id):
        node = nodes_by_id.get(node_id)
        if not node:
            return None
        file, label = node.get("file"), node.get("label") or ""
        top = label.split(".", 1)[0]
        return (file, top) if file and top else None

    seen, edges = set(), []
    for edge in (symdelta_data or {}).get("edges") or []:
        if not isinstance(edge, dict):
            continue
        src_key, dst_key = key_for(edge.get("source")), key_for(edge.get("target"))
        src_id = pos_to_id.get(src_key) if src_key else None
        dst_id = pos_to_id.get(dst_key) if dst_key else None
        if not src_id or not dst_id or src_id == dst_id or (src_id, dst_id) in seen:
            continue
        seen.add((src_id, dst_id))
        item = {"from": src_id, "to": dst_id}
        if edge.get("state"):
            item["state"] = edge["state"]
        edges.append(item)
    return edges


def load_groups(analysis_path):
    if not analysis_path:
        return None
    with open(analysis_path) as f:
        return json.load(f).get("groups") or []


def assign_groups(components, groups):
    for comp in components.values():
        comp["group"] = None
        if groups is None:
            continue
        for i, group in enumerate(groups):
            if comp["file"] in (group.get("paths") or []):
                comp["group"] = i
                break


def group_titles(groups):
    """[{"index": i, "title": title}] for every analysis.json group carrying a non-blank
    title -- sections.py labels its filter chips from this, one entry short of every `group`
    index a component might carry when a group's own title is missing or blank, so it can fall
    back to the plain ordinal for just that one instead of losing every label to one gap."""
    if not groups:
        return []
    out = []
    for i, group in enumerate(groups):
        title = (group.get("title") or "").strip()
        if title:
            out.append({"index": i, "title": title})
    return out


def assign_columns(components, symdelta_data):
    files = sorted({c["file"] for c in components.values()})
    depth = caller_depth(symdelta_data or {}, files)
    for comp in components.values():
        comp["column"] = depth.get(comp["file"], 0)


def drop_test_components(components, edges, implements):
    """Drop every component whose file is_test_path() classifies as a test, and any edge or
    heritage entry left dangling from it. Run before apply_cap so a batch of test files never
    wins a slot in the 30-component cap at a real component's expense; is_test_path is the
    same classifier symdelta and walkthrough already reuse, so this can't drift from either."""
    kept_ids = {c["id"] for c in components.values() if not is_test_path(c["file"])}
    kept = {cid: c for cid, c in components.items() if cid in kept_ids}
    kept_edges = [e for e in edges if e["from"] in kept_ids and e["to"] in kept_ids]
    kept_implements = [i for i in implements if i["from"] in kept_ids]
    return kept, kept_edges, kept_implements


def drop_unreferenced_unchanged(components, edges, implements):
    """Drop every "unchanged" component with no call or implements/heritage edge connecting it,
    directly or through a chain of other surviving components, to a touched (non-"unchanged")
    one: build_components parses every symbol in a changed file whole, so most of what it emits
    was never part of the change itself -- only a real call or heritage relationship earns it a
    spot as surrounding context. Touched components are never dropped here regardless of degree.

    Not counted in apply_cap's own `dropped` figure -- the MAX_COMPONENTS cap never got a chance
    to consider these, they were noise the parser produced, not overflow the cap had to cut.
    Returns (kept_components, kept_edges, kept_implements), same shape as drop_test_components."""
    adjacency = defaultdict(set)
    for e in edges:
        adjacency[e["from"]].add(e["to"])
        adjacency[e["to"]].add(e["from"])
    for item in _implements_edges_by_id(list(components.values()), implements):
        adjacency[item["from"]].add(item["to"])
        adjacency[item["to"]].add(item["from"])

    kept_ids = {cid for cid, c in components.items() if c.get("state") != "unchanged"}
    frontier = list(kept_ids)
    while frontier:
        next_frontier = []
        for cid in frontier:
            for neighbour in adjacency.get(cid, ()):
                if neighbour in components and neighbour not in kept_ids:
                    kept_ids.add(neighbour)
                    next_frontier.append(neighbour)
        frontier = next_frontier

    kept = {cid: c for cid, c in components.items() if cid in kept_ids}
    kept_edges = [e for e in edges if e["from"] in kept_ids and e["to"] in kept_ids]
    kept_implements = [i for i in implements if i["from"] in kept_ids]
    return kept, kept_edges, kept_implements


def _implements_edges_by_id(components, implements):
    """implements/extends heritage entries carry a target NAME, not a component id (see the
    module-level note on cross-file type resolution); barycenter ordering needs a real id on
    both ends to treat one as a neighbour link, so resolve each target name against `components`
    here, first match wins on a collision, same resolution sections.py's renderer already does
    for drawing the edge itself. A name with no match in this set (an external base class, say)
    contributes no edge, same as it draws none."""
    name_to_id = {}
    for c in components:
        name_to_id.setdefault(c["name"], c["id"])
    out = []
    for item in implements:
        dst = name_to_id.get(item["to"])
        if dst and dst != item["from"]:
            out.append({"from": item["from"], "to": dst})
    return out


def _barycenter_order(columns, edges, sweeps=3):
    """Order each column by the average position of its neighbours in the adjacent column,
    ported from gen_mockup_structure_v4.py's own `_barycenter_order` (the approved mockup this
    view is built from) -- a standard crossing-reduction heuristic, alternating
    left-to-right/right-to-left sweeps over `sweeps` passes so a column's order can react to
    both of its neighbours, not just the one already-settled side. Deterministic: ties break on
    name, and the initial order (before any sweep) is name order too, so a component with no
    resolved neighbour anywhere keeps a stable, alphabetical position instead of wherever a
    dict happened to iterate it from."""
    order = [sorted(col, key=lambda c: c["name"].lower()) for col in columns]
    n = len(order)
    if n <= 1:
        return order

    neighbours = defaultdict(set)
    for e in edges:
        neighbours[e["from"]].add(e["to"])
        neighbours[e["to"]].add(e["from"])

    pos = {}
    for col in order:
        for i, c in enumerate(col):
            pos[c["id"]] = float(i)

    def resweep(col_idx, from_idx):
        col = order[col_idx]
        from_ids = {c["id"] for c in order[from_idx]}

        def barycenter(c):
            near = [pos[nid] for nid in neighbours[c["id"]] if nid in from_ids]
            if not near:
                return pos[c["id"]]
            return sum(near) / len(near)

        col.sort(key=lambda c: (barycenter(c), c["name"].lower()))
        for i, c in enumerate(col):
            pos[c["id"]] = float(i)

    for sweep in range(sweeps):
        rng = range(1, n) if sweep % 2 == 0 else range(n - 2, -1, -1)
        for i in rng:
            resweep(i, i - 1 if sweep % 2 == 0 else i + 1)

    return order


def split_also_touched(components, edges, implements):
    """(boxed, also_touched): also_touched is every component with no call edge and no
    resolved implements/extends edge, sorted by name -- it has nothing to draw a line to or
    from, so it renders as a plain "also touched" note instead of an empty, edge-less box.
    boxed keeps the components' original relative order; the caller still owns sorting it."""
    with_edge_ids = {e["from"] for e in edges} | {e["to"] for e in edges}
    for item in _implements_edges_by_id(components, implements):
        with_edge_ids.add(item["from"])
        with_edge_ids.add(item["to"])
    boxed = [c for c in components if c["id"] in with_edge_ids]
    also_touched = sorted(
        (c for c in components if c["id"] not in with_edge_ids), key=lambda c: c["name"].lower()
    )
    return boxed, also_touched


def assign_rows(components, edges, implements):
    """Set `row` on every component: its 0-based position within its own `column`, ordered by
    _barycenter_order over both call edges and resolved implements/extends edges -- sections.py
    renders a column top-to-bottom in `row` order instead of the arbitrary order components
    happen to list in, so a caller and its nearest callee land close in height too."""
    by_col = defaultdict(list)
    for c in components:
        by_col[c["column"]].append(c)
    columns = [by_col[col] for col in sorted(by_col)]
    row_edges = edges + _implements_edges_by_id(components, implements)
    for col in _barycenter_order(columns, row_edges):
        for i, c in enumerate(col):
            c["row"] = i


def _round_robin_fill(bucket_keys, buckets, kept_ids, cap):
    """Fill `kept_ids` up to `cap` by taking one item at a time from each bucket in
    `bucket_keys` order, so no single bucket can claim every slot before the others get a
    turn. Mutates `kept_ids` in place; stops once a bucket runs dry on every pass."""
    idx = {k: 0 for k in bucket_keys}
    while len(kept_ids) < cap:
        progressed = False
        for k in bucket_keys:
            bucket = buckets[k]
            if idx[k] < len(bucket):
                kept_ids.add(bucket[idx[k]]["id"])
                idx[k] += 1
                progressed = True
                if len(kept_ids) >= cap:
                    break
        if not progressed:
            break


def apply_cap(components, edges, cap=MAX_COMPONENTS):
    """Keep at most `cap` components, the top ones within each group (round robin across
    groups so one large group can't starve the others), ranked by call-edge degree then id.
    The round robin runs over touched (state != "unchanged") components first, then unchanged
    ones for any remaining slots -- so the cap never cuts a touched component while an
    unchanged one survives, even across groups. Drops any edge or heritage entry left
    dangling. Returns (kept_components, kept_edges, dropped_count)."""
    if len(components) <= cap:
        return list(components.values()), edges, 0
    degree = Counter()
    for e in edges:
        degree[e["from"]] += 1
        degree[e["to"]] += 1
    touched_buckets = defaultdict(list)
    unchanged_buckets = defaultdict(list)
    for comp in components.values():
        bucket = unchanged_buckets if comp.get("state") == "unchanged" else touched_buckets
        bucket[comp["group"]].append(comp)
    for buckets in (touched_buckets, unchanged_buckets):
        for bucket in buckets.values():
            bucket.sort(key=lambda c: (-degree.get(c["id"], 0), c["id"]))
    bucket_keys = sorted({comp["group"] for comp in components.values()}, key=lambda k: (k is None, k))
    kept_ids = set()
    _round_robin_fill(bucket_keys, touched_buckets, kept_ids, cap)
    _round_robin_fill(bucket_keys, unchanged_buckets, kept_ids, cap)
    kept = [c for c in components.values() if c["id"] in kept_ids]
    kept_edges = [e for e in edges if e["from"] in kept_ids and e["to"] in kept_ids]
    return kept, kept_edges, len(components) - len(kept)


def _empty_result(reason):
    return {"language": None, "reason": reason, "components": [], "also_touched": [],
            "edges": [], "implements": [], "dropped": 0}


def analyse(repo, base, head, symdelta_path, analysis_path, paths=()):
    for ref in (base, head):
        if run_git(repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"]).returncode != 0:
            raise RuntimeError(f"bad ref: {ref!r}")
    resolved_base = resolve_base(repo, base, head)

    diffs = changed_files(repo, resolved_base, head, paths)
    base_paths = {old for _, old, _ in diffs if old}
    head_paths = {new for _, _, new in diffs if new}
    all_paths = base_paths | head_paths
    if not all_paths:
        return _empty_result("no changed files between the given refs")

    lang_counts = Counter(lang for lang in (detect_lang(p) for p in all_paths) if lang)
    if not lang_counts:
        return _empty_result("no changed files in a supported language (typescript/go)")
    language = lang_counts.most_common(1)[0][0]
    reason = None
    if len(lang_counts) > 1:
        reason = f"mixed-language diff; showing {language} only ({dict(lang_counts)})"

    parser_fn = PARSERS[language]
    relevant_ext = RELEVANT_EXTS[language]

    def parse_side(paths, ref):
        modules = {}
        for path in sorted(paths):
            if Path(path).suffix.lower() not in relevant_ext:
                continue
            content = git_show(repo, ref, path)
            if content is None:
                continue
            parsed = parser_fn(content)
            if parsed is not None:
                modules[path] = parsed
        return modules

    base_modules = parse_side(base_paths, resolved_base)
    head_modules = parse_side(head_paths, head)
    if not base_modules and not head_modules:
        tool = "tree-sitter typescript grammar" if language == "typescript" else "go toolchain"
        return _empty_result(f"no {language} parser available ({tool} missing)")

    components, pos_to_id = build_components(base_modules, head_modules)
    implements = build_implements(components)
    for comp in components.values():
        comp.pop("_extends", None)
        comp.pop("_implements", None)

    symdelta_data = {}
    if symdelta_path:
        with open(symdelta_path) as f:
            symdelta_data = json.load(f)

    edges = build_edges(symdelta_data, pos_to_id)
    groups = load_groups(analysis_path)
    assign_groups(components, groups)
    assign_columns(components, symdelta_data)

    components, edges, implements = drop_test_components(components, edges, implements)
    components, edges, implements = drop_unreferenced_unchanged(components, edges, implements)

    kept, kept_edges, dropped = apply_cap(components, edges)
    kept_ids = {c["id"] for c in kept}
    implements = [i for i in implements if i["from"] in kept_ids]

    boxed, also_touched = split_also_touched(kept, kept_edges, implements)
    assign_rows(boxed, kept_edges, implements)
    boxed_ids = {c["id"] for c in boxed}
    implements = [i for i in implements if i["from"] in boxed_ids]
    boxed.sort(key=lambda c: (c["column"], c["file"], c["name"]))

    result = {
        "language": language, "components": boxed,
        "also_touched": [c["name"] for c in also_touched],
        "edges": kept_edges, "implements": implements, "dropped": dropped,
    }
    if reason:
        result["reason"] = reason
    titles = group_titles(groups)
    if titles:
        result["groups"] = titles
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--symdelta", required=True)
    parser.add_argument("--analysis", default=None)
    parser.add_argument("--paths", nargs="*", default=[],
                         help="repo-relative files or directories to scope the diff to; "
                              "needed in explain mode, where the empty baseline otherwise "
                              "makes every file in the repo a changed file. Unused for a real "
                              "ref pair, where the delta is already the scope.")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    try:
        result = analyse(args.repo, args.base, args.head, args.symdelta, args.analysis,
                          args.paths)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    Path(args.out).write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
