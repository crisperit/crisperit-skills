#!/usr/bin/env python3
"""Regression check for the resolved-thread banner overflow: `resolvedWrapFor` inserts
`<details class="hr-thread-resolved">` as a child of `pre.diff`, so without its own reset it
inherits that ancestor's monospace, `white-space:pre` and `line-height:0` -- the long
"Resolved by <login> · <path>:<line> · N comments" summary (and any un-rec'd .fb-item row
inside, see buildItem) then never wraps and runs over the code instead."""

import pathlib

TEMPLATE = pathlib.Path(__file__).parent.parent / "assets" / "diff-review-template.html"


def _resolved_wrap_rule():
    template = TEMPLATE.read_text()
    start = template.index("details.hr-thread-resolved{")
    end = template.index("}", start)
    return template[start:end]


def test_resets_white_space_so_text_wraps():
    assert "white-space:normal" in _resolved_wrap_rule()


def test_resets_line_height_collapsed_by_diff_ancestor():
    # .diff{line-height:0} on the ancestor <pre> would otherwise collapse this prose.
    assert "line-height:1.6" in _resolved_wrap_rule()


def test_breaks_the_long_path_with_no_space_to_wrap_at():
    assert "overflow-wrap:anywhere" in _resolved_wrap_rule()


if __name__ == "__main__":
    tests = [
        test_resets_white_space_so_text_wraps,
        test_resets_line_height_collapsed_by_diff_ancestor,
        test_breaks_the_long_path_with_no_space_to_wrap_at,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
