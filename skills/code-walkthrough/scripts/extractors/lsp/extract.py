#!/usr/bin/env python3
"""TypeScript/Python/Rust call-graph extractor: the LSP tier of symdelta.py's per-language
extractor contract. Emits the same JSONL edge shape the Go extractor (extractors/go/main.go)
does -- {"FromFile","FromSym","ToFile","ToSym"}, paths repo-relative, methods
receiver-qualified ("Class.method") -- so symdelta.py's rename-pairing, move-collapse and graph
assembly stay language agnostic.

Usage:
  python3 extract.py <worktree_root> [--lang <name>]                          # capability check only
  python3 extract.py <worktree_root> [--lang <name>] <file> [<file> ...]      # extract edges
  python3 extract.py <worktree_root> [--lang <name>] --files-list <path>      # files, one per line

`--lang` defaults to "typescript" and selects a server profile from LANGUAGES.

Unlike the Go extractor, which loads the whole module because go/packages makes that cheap,
this only queries the given (changed) files, in both directions: callHierarchy/outgoingCalls
catches new calls the changed code makes, incomingCalls catches new calls into it from code
that did not itself change. An edge between two unchanged symbols cannot have changed, so the
union is complete for this feature's edge rule.

Talks to the language server over raw stdio JSON-RPC via lsp_client.py (vendored from an
earlier experiment). Stdlib only.
"""
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lsp_client import LSPClient, uri  # noqa: E402

# LSP SymbolKind. Only Method and Function are queried for call hierarchy: filtering by kind
# before prepareCallHierarchy is what keeps an object-literal property that happens to share a
# method's name (a different SymbolKind) from producing a false-empty result.
KIND_METHOD = 6
KIND_FUNCTION = 12
QUERY_KINDS = {KIND_METHOD, KIND_FUNCTION}

# SymbolKinds that can act as a Class.method-style container when resolving a call target's
# enclosing symbol (see _find_enclosing_container).
KIND_ENUM = 10
KIND_INTERFACE = 11
KIND_CLASS = 5
KIND_STRUCT = 23
# rust-analyzer's documentSymbol wraps a struct's methods in a separate "impl Foo" node, not the
# struct node itself (whose own range doesn't span its impl blocks); measured, that node reports
# kind=19 (LSP SymbolKind.Object), not Class/Struct.
KIND_OBJECT = 19
CONTAINER_KINDS = {KIND_CLASS, KIND_INTERFACE, KIND_ENUM, KIND_STRUCT, KIND_OBJECT}

# Per-language server profile:
#   server_cmd        argv to spawn the language server
#   language_id       LSP languageId sent on didOpen
#   vendor_dirs       top-level dirs under root treated as dependency/build output, not source
#   first_file_retries  retry budget for the first file's documentSymbol/prepareCallHierarchy
#                        warmup (a cold project load, or -- rust-analyzer -- a cold crate index)
#   qualify           how a call target (a CallHierarchyItem we have no tree for) is qualified:
#     "detail"     the container name is in item["detail"] (typescript-language-server)
#     "enclosing"  no usable detail; resolve the container by fetching documentSymbol for the
#                  item's own uri and finding the innermost Class/Struct/Interface/Enum symbol
#                  whose range contains it (pyright, rust-analyzer)
LANGUAGES = {
    "typescript": {
        "server_cmd": ["typescript-language-server", "--stdio"],
        "language_id": "typescript",
        "vendor_dirs": ("node_modules",),
        "first_file_retries": 30,  # tsserver's first project load, seen up to ~30s in testing
        "qualify": "detail",
    },
    "python": {
        "server_cmd": ["pyright-langserver", "--stdio"],
        "language_id": "python",
        "vendor_dirs": (".venv", "venv", "site-packages", "__pypackages__"),
        "first_file_retries": 15,
        "qualify": "enclosing",
    },
    "rust": {
        "server_cmd": ["rust-analyzer"],
        "language_id": "rust",
        "vendor_dirs": ("target",),
        "first_file_retries": 120,  # rust-analyzer's cold crate-graph index, measured worst case
        "qualify": "enclosing",
    },
}

DOC_SYMBOL_LATER_FILE_RETRIES = 5
DOC_SYMBOL_RETRY_DELAY = 1.0

# Kept for backward compatibility with callers/tests pinned to the typescript-only constants.
SERVER_CMD = LANGUAGES["typescript"]["server_cmd"]
DOC_SYMBOL_FIRST_FILE_RETRIES = LANGUAGES["typescript"]["first_file_retries"]

_RUST_IMPL_RE = re.compile(r"^impl(?:<[^>]*>)?\s+(.+)$")


def _npm_global_bin_dir():
    """Where `npm i -g` would have put a binary, or None if npm itself isn't here. Both
    typescript-language-server and pyright (LANGUAGE_SERVER_REMEDY's npm installs, in
    symdelta.py) land there; a real install that never made it onto PATH is a different failure
    than one that never happened, and check_language_server tells the two apart below."""
    try:
        result = subprocess.run(["npm", "prefix", "-g"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return str(Path(result.stdout.strip()) / "bin")


# Per-language lookup for the PATH-trap check: where the server would already be sitting if
# installed but not on PATH. No entry for rust -- rustup's own layout isn't a single well-known
# directory the way npm's global bin dir is, so it keeps the plain "not found" reason.
KNOWN_INSTALL_DIRS = {
    "typescript": _npm_global_bin_dir,
    "python": _npm_global_bin_dir,
}


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _rust_container_name(raw):
    """impl Greeter -> Greeter; impl SomeTrait for Greeter -> Greeter. Matches the Go
    extractor's receiver-qualified convention, which rust-analyzer's own documentSymbol/detail
    strings don't."""
    if not raw:
        return raw
    m = _RUST_IMPL_RE.match(raw)
    if not m:
        return raw
    body = m.group(1)
    if " for " in body:
        body = body.rsplit(" for ", 1)[1]
    return body.strip()


def _uri_to_relpath(file_uri, root, vendor_dirs=("node_modules",)):
    """None for anything that isn't the repo's own source: outside `root` entirely, or inside a
    vendor/build directory -- e.g. TS lib internals like `Array.push` under `node_modules` --
    matching the Go extractor's own filter to module-owned files (there, anything outside the
    module's own import path)."""
    if not file_uri.startswith("file://"):
        return None
    path = file_uri[len("file://"):]
    rel = posixpath.relpath(path, str(root))
    if rel.startswith(".."):
        return None
    for vendor_dir in vendor_dirs:
        if rel == vendor_dir or rel.startswith(vendor_dir + "/"):
            return None
    return rel


def _symbol_name(item):
    """Class.method for a Method item -- typescript-language-server puts the container's name
    in `detail` -- bare name otherwise. Matches the Go extractor's receiver-qualified
    convention (OutcomeStatus.String) exactly. Only valid for the "detail" qualify strategy;
    python/rust call targets go through extract_edges's "enclosing" path instead."""
    if item.get("kind") == KIND_METHOD and item.get("detail"):
        return f"{item['detail']}.{item['name']}"
    return item["name"]


def _qualify_from_tree(sym, parent_name, lang):
    """Class.method for a documentSymbol entry we found by walking our own file's tree:
    `parent_name` is the immediate ancestor's name, taken uniformly for all three languages
    (measured: documentSymbol nests a method under its class/struct/impl-block parent in
    typescript, python and rust alike, unlike the CallHierarchyItem shape _symbol_name reads)."""
    if sym.get("kind") == KIND_METHOD and parent_name:
        container = _rust_container_name(parent_name) if lang == "rust" else parent_name
        return f"{container}.{sym['name']}"
    return sym["name"]


def _flatten_symbols_with_parent(symbols, parent_name=None):
    """documentSymbol's hierarchy, flattened to every (Method/Function symbol, immediate parent
    name) pair, regardless of nesting depth."""
    out = []
    for s in symbols:
        if s.get("kind") in QUERY_KINDS:
            out.append((s, parent_name))
        if s.get("children"):
            out.extend(_flatten_symbols_with_parent(s["children"], s.get("name")))
    return out


def _flatten_symbols(symbols):
    """Every Method/Function entry in documentSymbol's hierarchy, regardless of nesting depth."""
    return [sym for sym, _parent in _flatten_symbols_with_parent(symbols)]


def _range_contains(outer, inner):
    outer_start = (outer["start"]["line"], outer["start"]["character"])
    outer_end = (outer["end"]["line"], outer["end"]["character"])
    inner_start = (inner["start"]["line"], inner["start"]["character"])
    inner_end = (inner["end"]["line"], inner["end"]["character"])
    return outer_start <= inner_start and outer_end >= inner_end


def _find_enclosing_container(symbols, target_range, best=None):
    """Innermost Class/Struct/Interface/Enum symbol (by name) whose range contains
    `target_range`, walked depth-first so a nested match overwrites its outer container."""
    for s in symbols:
        if s.get("kind") in CONTAINER_KINDS and _range_contains(s.get("range", {}), target_range):
            best = s["name"]
        if s.get("children"):
            best = _find_enclosing_container(s["children"], target_range, best)
    return best


def check_language_server(repo, lang="typescript"):
    """Is the configured language server on PATH, and does it advertise callHierarchyProvider?
    Checked directly against `repo` -- capabilities don't depend on which ref is checked out --
    so a missing tool or capability is caught before any worktree gets created. Returns
    (ok, reason); reason is None when ok."""
    profile = LANGUAGES[lang]
    tool = profile["server_cmd"][0]
    if shutil.which(tool) is None:
        locate = KNOWN_INSTALL_DIRS.get(lang)
        install_dir = locate() if locate else None
        if install_dir and (Path(install_dir) / tool).exists():
            return False, (
                f"{tool} is installed in {install_dir} but that directory is not on PATH; "
                f'add it, e.g. export PATH="{install_dir}:$PATH"'
            )
        return False, f"{tool} not found on PATH"

    client = LSPClient(profile["server_cmd"], cwd=str(repo))
    try:
        resp = client.request("initialize", {
            "processId": os.getpid(),
            "rootUri": uri(str(repo)),
            # The same capabilities extract_edges declares: some servers only advertise
            # callHierarchyProvider back when the client claims call-hierarchy support in its
            # own request, verified empirically for typescript-language-server -- an empty
            # capabilities dict here gets `callHierarchyProvider: None` even on a server that
            # supports it fine.
            "capabilities": {
                "textDocument": {
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    "callHierarchy": {"dynamicRegistration": False},
                }
            },
        }, timeout=60)
    except TimeoutError as exc:
        return False, f"{tool} did not respond to initialize: {exc}"
    finally:
        client.shutdown()

    caps = resp.get("result", {}).get("capabilities", {})
    if not caps.get("callHierarchyProvider"):
        reason = f"{tool} does not advertise callHierarchyProvider"
        # A missing/broken node_modules (e.g. a worktree whose symlink from link_node_modules
        # is dangling) makes typescript-language-server silently drop callHierarchyProvider
        # instead of erroring, which otherwise reads as a server/tooling problem.
        if lang == "typescript" and not (Path(repo) / "node_modules").exists():
            reason += (
                f"; {repo}/node_modules is missing or a broken link, so it could not load "
                "TypeScript"
            )
        return False, reason
    return True, None


def extract_edges(root, rel_files, lang="typescript"):
    """One LSP session over `root` (a worktree at one ref), querying outgoing and incoming
    calls for every Method/Function symbol in `rel_files`. Returns deduplicated
    {"FromFile","FromSym","ToFile","ToSym"} dicts, repo-relative paths."""
    profile = LANGUAGES[lang]
    root = Path(root)
    if not rel_files:
        return []

    client = LSPClient(profile["server_cmd"], cwd=str(root))
    try:
        client.request("initialize", {
            "processId": os.getpid(),
            "rootUri": uri(str(root)),
            "capabilities": {
                "textDocument": {
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    "callHierarchy": {"dynamicRegistration": False},
                }
            },
            "workspaceFolders": [{"uri": uri(str(root)), "name": root.name}],
        }, timeout=60)
        client.notify("initialized", {})

        existing = [rel for rel in rel_files if (root / rel).exists()]
        for rel in existing:
            path = root / rel
            client.notify("textDocument/didOpen", {
                "textDocument": {
                    "uri": uri(str(path)), "languageId": profile["language_id"], "version": 1,
                    "text": _read(path),
                }
            })

        seen = set()
        edges = []
        doc_symbol_cache = {}
        # Tallied to tell a real failure (server resolved nothing) apart from a normal empty
        # diff (everything resolved, just no changed edges) -- see the raises after the loop.
        files_with_symbols = 0
        symbols_tried = 0
        symbols_with_callhierarchy = 0
        # Two separate readiness gates, each set True the first time its own request returns a
        # real result: rust-analyzer answers documentSymbol before prepareCallHierarchy is warm
        # (chq_ready), and prepareCallHierarchy itself before outgoingCalls/incomingCalls have
        # finished cross-file semantic resolution (calls_ready) -- measured, one extra ~1s retry
        # for a call into another file. After a gate flips, an empty result there means "no such
        # call", not "not warm yet", so retries stop.
        chq_ready = False
        calls_ready = False
        calls_ready_retries = 10  # generous relative to the ~1 retry measured; bounds worst case
        char_offset = 1 if lang == "typescript" else 0

        def add_edge(from_file, from_sym, to_file, to_sym):
            key = (from_file, from_sym, to_file, to_sym)
            if key not in seen:
                seen.add(key)
                edges.append({
                    "FromFile": from_file, "FromSym": from_sym,
                    "ToFile": to_file, "ToSym": to_sym,
                })

        def cached_doc_symbols(file_uri):
            if file_uri not in doc_symbol_cache:
                resp = client.request("textDocument/documentSymbol",
                                       {"textDocument": {"uri": file_uri}}, timeout=15)
                doc_symbol_cache[file_uri] = resp.get("result") or []
            return doc_symbol_cache[file_uri]

        def qualify_target(target_item):
            if profile["qualify"] == "detail":
                return _symbol_name(target_item)
            container = _find_enclosing_container(
                cached_doc_symbols(target_item["uri"]), target_item["range"])
            if lang == "rust":
                container = _rust_container_name(container)
            if container:
                return f"{container}.{target_item['name']}"
            return target_item["name"]

        for i, rel in enumerate(existing):
            file_uri = uri(str(root / rel))

            # The first file pays the server's project-load warmup; later files are already warm.
            retries = profile["first_file_retries"] if i == 0 else DOC_SYMBOL_LATER_FILE_RETRIES
            doc_syms = None
            for _ in range(retries):
                resp = client.request("textDocument/documentSymbol",
                                       {"textDocument": {"uri": file_uri}}, timeout=15)
                result = resp.get("result")
                if result:
                    doc_syms = result
                    break
                time.sleep(DOC_SYMBOL_RETRY_DELAY)
            if not doc_syms:
                continue
            doc_symbol_cache[file_uri] = doc_syms
            files_with_symbols += 1

            for sym, parent_name in _flatten_symbols_with_parent(doc_syms):
                # Query at selectionRange, not range: range.start lands off-token (e.g. on a
                # decorator or modifier) and silently returns empty.
                pos = sym["selectionRange"]["start"]
                symbols_tried += 1
                attempts = 1 if chq_ready else retries
                items = []
                for attempt in range(attempts):
                    prep = client.request("textDocument/prepareCallHierarchy", {
                        "textDocument": {"uri": file_uri},
                        "position": {"line": pos["line"], "character": pos["character"] + char_offset},
                    }, timeout=15)
                    items = prep.get("result") or []
                    if items:
                        chq_ready = True
                        break
                    if attempt < attempts - 1:
                        time.sleep(DOC_SYMBOL_RETRY_DELAY)
                if not items:
                    continue
                symbols_with_callhierarchy += 1
                item = items[0]
                from_sym = _qualify_from_tree(sym, parent_name, lang)

                call_attempts = 1 if calls_ready else calls_ready_retries
                out_result = []
                for attempt in range(call_attempts):
                    out = client.request("callHierarchy/outgoingCalls", {"item": item}, timeout=15)
                    out_result = out.get("result") or []
                    if out_result:
                        calls_ready = True
                        break
                    if attempt < call_attempts - 1:
                        time.sleep(DOC_SYMBOL_RETRY_DELAY)
                for call in out_result:
                    to = call["to"]
                    if to.get("kind") not in QUERY_KINDS:
                        continue
                    to_file = _uri_to_relpath(to["uri"], root, profile["vendor_dirs"])
                    if to_file is None:
                        continue
                    add_edge(rel, from_sym, to_file, qualify_target(to))

                call_attempts = 1 if calls_ready else calls_ready_retries
                inc_result = []
                for attempt in range(call_attempts):
                    inc = client.request("callHierarchy/incomingCalls", {"item": item}, timeout=15)
                    inc_result = inc.get("result") or []
                    if inc_result:
                        calls_ready = True
                        break
                    if attempt < call_attempts - 1:
                        time.sleep(DOC_SYMBOL_RETRY_DELAY)
                for call in inc_result:
                    frm = call["from"]
                    if frm.get("kind") not in QUERY_KINDS:
                        continue
                    from_file = _uri_to_relpath(frm["uri"], root, profile["vendor_dirs"])
                    if from_file is None:
                        continue
                    add_edge(from_file, qualify_target(frm), rel, from_sym)

        # existing empty is the base side of an all-new diff (rel_files filtered to what's on
        # disk at this ref) -- already legitimate, so only raise when there was something to
        # resolve and the server came back empty for all of it.
        if existing and files_with_symbols == 0:
            raise RuntimeError(
                f"{lang}: {' '.join(profile['server_cmd'])} opened {len(existing)} file(s) and "
                "returned no documentSymbol result for any of them"
            )
        if symbols_tried and symbols_with_callhierarchy == 0:
            raise RuntimeError(
                f"{lang}: documentSymbol resolved but prepareCallHierarchy returned nothing for "
                f"all {symbols_tried} symbol(s) tried"
            )

        return edges
    finally:
        client.shutdown()


def _parse_args(argv):
    if not argv:
        return None, []
    root = argv[0]
    if len(argv) >= 3 and argv[1] == "--files-list":
        rel_files = [line.strip() for line in Path(argv[2]).read_text().splitlines() if line.strip()]
        return root, rel_files
    return root, argv[1:]


def _extract_lang_flag(argv, default="typescript"):
    """Pulls a `--lang <name>` pair out of argv wherever it appears, returning (lang, rest).
    Kept separate from _parse_args so the existing positional/--files-list contract there never
    has to learn about --lang."""
    if "--lang" in argv:
        idx = argv.index("--lang")
        return argv[idx + 1], argv[:idx] + argv[idx + 2:]
    return default, argv


def main():
    lang, argv = _extract_lang_flag(sys.argv[1:])
    root, rel_files = _parse_args(argv)
    if root is None:
        print("usage: extract.py <root> [--lang <name>] [file ...] | "
              "extract.py <root> [--lang <name>] --files-list <path>", file=sys.stderr)
        return 1

    ok, reason = check_language_server(root, lang)
    if not ok:
        print(reason, file=sys.stderr)
        return 1

    try:
        edges = extract_edges(root, rel_files, lang)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    for edge in edges:
        print(json.dumps(edge))
    return 0


if __name__ == "__main__":
    sys.exit(main())
