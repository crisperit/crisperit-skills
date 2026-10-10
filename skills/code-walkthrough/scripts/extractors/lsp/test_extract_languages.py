#!/usr/bin/env python3
"""Offline self-check for extract.py's Python/Rust support: the LANGUAGES profile table, the
rust `impl` container normalisation, and the enclosing-container qualification path. No
language server is spawned here; test_extract.py owns the real-server end-to-end case."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import extract  # noqa: E402


def test_languages_table_shape():
    for lang in ("typescript", "python", "rust"):
        profile = extract.LANGUAGES[lang]
        assert profile["server_cmd"] and isinstance(profile["server_cmd"][0], str)
        assert isinstance(profile["language_id"], str)
        assert isinstance(profile["vendor_dirs"], tuple)
        assert profile["first_file_retries"] > 0
        assert profile["call_timeout"] > 0
        assert profile["qualify"] in ("detail", "enclosing")
    # rust-analyzer's incomingCalls does a workspace-wide search and measurably exceeds the
    # other languages' 15s budget on a multi-crate workspace.
    assert extract.LANGUAGES["rust"]["call_timeout"] > extract.LANGUAGES["typescript"]["call_timeout"]


def test_rust_container_name():
    rows = [
        ("impl Greeter", "Greeter"),
        ("impl SomeTrait for Greeter", "Greeter"),
        ("Greeter", "Greeter"),
        (None, None),
        ("", ""),
    ]
    for name, want in rows:
        assert extract._rust_container_name(name) == want, name


def test_qualify_from_tree():
    method = {"kind": extract.KIND_METHOD, "name": "greet"}
    # rust-analyzer reports `impl Foo { fn bar() }` (no `self`) as KIND_FUNCTION, not KIND_METHOD. The
    # callee side (enclosing-container lookup) qualifies it anyway, so this side must match, or the
    # same fn becomes two graph nodes (bare on the caller side, "Foo.bar" on the callee side).
    assoc = {"kind": extract.KIND_FUNCTION, "name": "deserialize"}
    rows = [
        (method, "Greeter", "python", "Greeter.greet"),
        (method, "Greeter", "typescript", "Greeter.greet"),
        (method, "impl Greeter", "rust", "Greeter.greet"),
        (method, "impl SomeTrait for Greeter", "rust", "Greeter.greet"),
        ({"kind": extract.KIND_FUNCTION, "name": "helper"}, None, "python", "helper"),
        (assoc, "impl OctoMind", "rust", "OctoMind.deserialize"),
        (assoc, "impl Store for OctoMind", "rust", "OctoMind.deserialize"),
        # a fn nested in another fn's body has the outer fn's name as parent, never "impl ..."
        ({"kind": extract.KIND_FUNCTION, "name": "inner"}, "outer_fn", "rust", "inner"),
    ]
    for sym, parent, lang, want in rows:
        assert extract._qualify_from_tree(sym, parent, lang) == want, (sym["name"], parent, lang)


def test_find_enclosing_container():
    def rng(start, end):
        return {"start": {"line": start, "character": 0}, "end": {"line": end, "character": 0}}

    inner_target = {"start": {"line": 5, "character": 4}, "end": {"line": 5, "character": 20}}
    nested = [{
        "name": "Outer", "kind": extract.KIND_CLASS, "range": rng(0, 20),
        "children": [{"name": "Inner", "kind": extract.KIND_STRUCT, "range": rng(4, 10), "children": []}],
    }]
    assert extract._find_enclosing_container(nested, inner_target) == "Inner"

    # rust-analyzer wraps a struct's methods in a separate "impl Foo" node (kind=19, Object); the
    # struct's own node doesn't span the impl block. Found here, normalised by qualify_target.
    method_range = {"start": {"line": 7, "character": 4}, "end": {"line": 9, "character": 5}}
    rust = [
        {"name": "Greeter", "kind": extract.KIND_STRUCT, "range": rng(4, 4), "children": []},
        {"name": "impl Greeter", "kind": extract.KIND_OBJECT, "range": rng(6, 10), "children": [
            {"name": "greet", "kind": extract.KIND_METHOD, "range": method_range, "children": []},
        ]},
    ]
    container = extract._find_enclosing_container(rust, method_range)
    assert container == "impl Greeter"
    assert extract._rust_container_name(container) == "Greeter"

    outside = {"start": {"line": 50, "character": 0}, "end": {"line": 50, "character": 5}}
    assert extract._find_enclosing_container(nested, outside) is None


def test_flatten_symbols_with_parent_tracks_the_immediate_ancestor_name():
    doc_syms = [
        {
            "name": "Greeter", "kind": extract.KIND_CLASS,
            "children": [
                {"name": "greet", "kind": extract.KIND_METHOD, "children": []},
                {"name": "prop", "kind": 7, "children": []},  # Property, e.g. an object-literal key
            ],
        },
        {"name": "helper", "kind": extract.KIND_FUNCTION},
    ]
    flat = extract._flatten_symbols_with_parent(doc_syms)
    by_name = {s["name"]: parent for s, parent in flat}
    assert by_name == {"greet": "Greeter", "helper": None}


def test_extract_lang_flag():
    lang, rest = extract._extract_lang_flag(["/repo", "--lang", "rust", "--files-list", "x.txt"])
    assert lang == "rust"
    assert rest == ["/repo", "--files-list", "x.txt"]

    lang, rest = extract._extract_lang_flag(["/repo", "a.ts"])
    assert lang == "typescript"
    assert rest == ["/repo", "a.ts"]
