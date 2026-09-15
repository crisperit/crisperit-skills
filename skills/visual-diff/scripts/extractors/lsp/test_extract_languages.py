#!/usr/bin/env python3
"""Offline self-check for extract.py's Python/Rust support: the LANGUAGES profile table, the
rust `impl` container normalisation, and the enclosing-container qualification path. No
language server is spawned here -- test_extract.py already owns the real-server end-to-end
case for typescript and is skipped when the tool isn't on PATH; this file stays pure-logic so
it never needs that skip."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import extract  # noqa: E402


def test_languages_table_covers_typescript_python_and_rust():
    for lang in ("typescript", "python", "rust"):
        profile = extract.LANGUAGES[lang]
        assert profile["server_cmd"] and isinstance(profile["server_cmd"][0], str)
        assert isinstance(profile["language_id"], str)
        assert isinstance(profile["vendor_dirs"], tuple)
        assert profile["first_file_retries"] > 0
        assert profile["qualify"] in ("detail", "enclosing")


def test_languages_table_qualify_strategy_matches_the_spec():
    assert extract.LANGUAGES["typescript"]["qualify"] == "detail"
    assert extract.LANGUAGES["python"]["qualify"] == "enclosing"
    assert extract.LANGUAGES["rust"]["qualify"] == "enclosing"


def test_rust_container_name_normalises_a_plain_impl_block():
    assert extract._rust_container_name("impl Greeter") == "Greeter"


def test_rust_container_name_normalises_a_trait_impl_block():
    assert extract._rust_container_name("impl SomeTrait for Greeter") == "Greeter"


def test_rust_container_name_leaves_a_non_impl_name_alone():
    assert extract._rust_container_name("Greeter") == "Greeter"


def test_rust_container_name_passes_through_none_and_empty():
    assert extract._rust_container_name(None) is None
    assert extract._rust_container_name("") == ""


def test_qualify_from_tree_qualifies_a_method_via_its_parent_uniformly():
    sym = {"kind": extract.KIND_METHOD, "name": "greet"}
    assert extract._qualify_from_tree(sym, "Greeter", "python") == "Greeter.greet"
    assert extract._qualify_from_tree(sym, "Greeter", "typescript") == "Greeter.greet"


def test_qualify_from_tree_normalises_a_rust_impl_parent():
    sym = {"kind": extract.KIND_METHOD, "name": "greet"}
    assert extract._qualify_from_tree(sym, "impl Greeter", "rust") == "Greeter.greet"
    assert extract._qualify_from_tree(sym, "impl SomeTrait for Greeter", "rust") == "Greeter.greet"


def test_qualify_from_tree_leaves_a_free_function_bare_with_no_parent():
    sym = {"kind": extract.KIND_FUNCTION, "name": "helper"}
    assert extract._qualify_from_tree(sym, None, "python") == "helper"


def test_uri_to_relpath_rejects_python_venv_dirs():
    root = "/repo"
    vendor_dirs = extract.LANGUAGES["python"]["vendor_dirs"]
    assert extract._uri_to_relpath(f"file://{root}/.venv/lib/x.py", root, vendor_dirs) is None
    assert extract._uri_to_relpath(f"file://{root}/src/a.py", root, vendor_dirs) == "src/a.py"


def test_uri_to_relpath_rejects_rust_target_dir():
    root = "/repo"
    vendor_dirs = extract.LANGUAGES["rust"]["vendor_dirs"]
    assert extract._uri_to_relpath(f"file://{root}/target/debug/build/x.rs", root, vendor_dirs) is None
    assert extract._uri_to_relpath(f"file://{root}/src/main.rs", root, vendor_dirs) == "src/main.rs"


def test_find_enclosing_container_picks_the_innermost_match():
    target_range = {"start": {"line": 5, "character": 4}, "end": {"line": 5, "character": 20}}
    doc_syms = [
        {
            "name": "Outer", "kind": extract.KIND_CLASS,
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 20, "character": 0}},
            "children": [
                {
                    "name": "Inner", "kind": extract.KIND_STRUCT,
                    "range": {"start": {"line": 4, "character": 0}, "end": {"line": 10, "character": 0}},
                    "children": [],
                },
            ],
        },
    ]
    assert extract._find_enclosing_container(doc_syms, target_range) == "Inner"


def test_find_enclosing_container_matches_a_rust_impl_block():
    # rust-analyzer wraps a struct's methods in a separate "impl Foo" node (kind=19, Object) --
    # the struct's own node doesn't span the impl block. This must be found and then normalised
    # by the caller (qualify_target in extract_edges), not left bare.
    target_range = {"start": {"line": 7, "character": 4}, "end": {"line": 9, "character": 5}}
    doc_syms = [
        {
            "name": "Greeter", "kind": extract.KIND_STRUCT,
            "range": {"start": {"line": 4, "character": 0}, "end": {"line": 4, "character": 19}},
            "children": [],
        },
        {
            "name": "impl Greeter", "kind": extract.KIND_OBJECT,
            "range": {"start": {"line": 6, "character": 0}, "end": {"line": 10, "character": 1}},
            "children": [
                {
                    "name": "greet", "kind": extract.KIND_METHOD,
                    "range": target_range, "children": [],
                },
            ],
        },
    ]
    container = extract._find_enclosing_container(doc_syms, target_range)
    assert container == "impl Greeter"
    assert extract._rust_container_name(container) == "Greeter"


def test_find_enclosing_container_returns_none_outside_any_container():
    target_range = {"start": {"line": 50, "character": 0}, "end": {"line": 50, "character": 5}}
    doc_syms = [
        {
            "name": "Outer", "kind": extract.KIND_CLASS,
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 20, "character": 0}},
            "children": [],
        },
    ]
    assert extract._find_enclosing_container(doc_syms, target_range) is None


def test_flatten_symbols_with_parent_tracks_the_immediate_ancestor_name():
    doc_syms = [
        {
            "name": "Greeter", "kind": extract.KIND_CLASS,
            "children": [
                {"name": "greet", "kind": extract.KIND_METHOD, "children": []},
            ],
        },
        {"name": "helper", "kind": extract.KIND_FUNCTION},
    ]
    flat = extract._flatten_symbols_with_parent(doc_syms)
    by_name = {s["name"]: parent for s, parent in flat}
    assert by_name == {"greet": "Greeter", "helper": None}


def test_extract_lang_flag_pulls_lang_out_of_argv_regardless_of_position():
    lang, rest = extract._extract_lang_flag(["/repo", "--lang", "rust", "--files-list", "x.txt"])
    assert lang == "rust"
    assert rest == ["/repo", "--files-list", "x.txt"]


def test_extract_lang_flag_defaults_to_typescript_when_absent():
    lang, rest = extract._extract_lang_flag(["/repo", "a.ts"])
    assert lang == "typescript"
    assert rest == ["/repo", "a.ts"]


if __name__ == "__main__":
    tests = [
        test_languages_table_covers_typescript_python_and_rust,
        test_languages_table_qualify_strategy_matches_the_spec,
        test_rust_container_name_normalises_a_plain_impl_block,
        test_rust_container_name_normalises_a_trait_impl_block,
        test_rust_container_name_leaves_a_non_impl_name_alone,
        test_rust_container_name_passes_through_none_and_empty,
        test_qualify_from_tree_qualifies_a_method_via_its_parent_uniformly,
        test_qualify_from_tree_normalises_a_rust_impl_parent,
        test_qualify_from_tree_leaves_a_free_function_bare_with_no_parent,
        test_uri_to_relpath_rejects_python_venv_dirs,
        test_uri_to_relpath_rejects_rust_target_dir,
        test_find_enclosing_container_picks_the_innermost_match,
        test_find_enclosing_container_matches_a_rust_impl_block,
        test_find_enclosing_container_returns_none_outside_any_container,
        test_flatten_symbols_with_parent_tracks_the_immediate_ancestor_name,
        test_extract_lang_flag_pulls_lang_out_of_argv_regardless_of_position,
        test_extract_lang_flag_defaults_to_typescript_when_absent,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
