#!/usr/bin/env python3
"""Regression check for the toml/Cargo.lock highlighting gap: the vendored bundle ships no
`toml` grammar, only `ini` (TOML's canonical hljs alias), so the map must route both there --
and the bundle must actually ship `ini`, or the mapping highlights nothing."""

import pathlib

ASSETS = pathlib.Path(__file__).parent.parent / "assets"
TEMPLATE = ASSETS / "diff-review-template.html"
HLJS_BUNDLE = ASSETS / "highlight.min.js"


def _lang_map_body():
    template = TEMPLATE.read_text()
    start = template.index("const HLJS_LANG_BY_NAME={") + len("const HLJS_LANG_BY_NAME={")
    end = template.index("};", start)
    return template[start:end]


def test_lang_map_routes_toml_and_cargo_lock_to_ini():
    body = _lang_map_body()

    assert "toml:'ini'" in body
    assert "'cargo.lock':'ini'" in body


def test_bundle_ships_the_ini_grammar():
    # Bundle registers grammars from `grmr_<name>` keys; `ini` is the key this map's
    # toml/cargo.lock entries rely on.
    assert "grmr_ini" in HLJS_BUNDLE.read_text()

