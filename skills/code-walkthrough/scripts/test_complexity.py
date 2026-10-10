import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import complexity  # noqa: E402
import cw_testlib  # noqa: E402

GO = """package x

type A struct{}
type B struct{}

func Plain() int { return 1 }

func Branchy(a int, b bool) int {
	if a > 1 && b {
		return 1
	}
	for i := 0; i < 3; i++ {
	}
	switch a {
	case 1:
		return 2
	case 2:
		return 3
	default:
		return 4
	}
}

func (a *A) Get() int {
	if true {
		return 1
	}
	return 2
}

func (b B) Get() int {
	for i := 0; i < 9; i++ {
	}
	return 3
}
"""

PY = """
def plain():
    return 1


def branchy(xs, flag):
    if flag and xs:
        for x in xs:
            try:
                pass
            except ValueError:
                pass
    return [x for x in xs if x]


class K:
    def method(self, n):
        return 1 if n else 2

    def outer(self):
        def inner(n):
            while n:
                n -= 1
        return inner
"""


GO_CHAINED = """package x

func Chained(a int) int {
	if a == 1 {
		return 1
	} else if a == 2 {
		return 2
	} else if a == 3 {
		return 3
	} else {
		return 4
	}
}
"""

GO_NESTED = """package x

func Nested(xs []int) int {
	for _, x := range xs {
		if x > 0 {
			if x > 10 {
				return x
			}
		}
	}
	return 0
}
"""

PY_CHAINED = """
def chained(a):
    if a == 1:
        return 1
    elif a == 2:
        return 2
    elif a == 3:
        return 3
    else:
        return 4
"""

TS_CHAINED = """function chained(a: number): number {
  if (a === 1) {
    return 1;
  } else if (a === 2) {
    return 2;
  } else if (a === 3) {
    return 3;
  } else {
    return 4;
  }
}
"""

# TS/JS wrap an else-if continuation in an `else_clause` node one layer deeper than Go's plain
# nested if, so a final else that genuinely holds its own unrelated if must not be mistaken
# for a chain continuation once that wrapper is unwrapped.
TS_UNRELATED_IF_IN_FINAL_ELSE = """function f(a: number): number {
  if (a === 1) {
    return 1;
  } else {
    if (a === 2) {
      return 2;
    }
    return 3;
  }
}
"""


def cc(counts):
    return {name: entry["cc"] for name, entry in counts.items()}


def depth(counts):
    return {name: entry["depth"] for name, entry in counts.items()}


def _need_grammar(path):
    """complexity resolves its own parser (language pack, tree_sitter_<lang>, graphify's venv),
    so skip on exactly that resolution rather than on structure.py's."""
    if complexity.generic_parser_for(path) is None:
        cw_testlib.skip(f"no tree-sitter grammar for {path}")


def test_cc_go_counts():
    _need_grammar("x.go")
    counts = cc(complexity._cc_generic("x.go", GO))

    assert counts["Plain"] == 1
    assert counts["Branchy"] == 6  # 1 + if + && + for + two non-default cases
    assert counts["A.Get"] == 2
    assert counts["B.Get"] == 2
    assert "Get" not in counts


def test_default_arm_is_not_a_decision():
    _need_grammar("x.go")
    without_default = GO.replace("\tdefault:\n\t\treturn 4\n", "")
    assert (cc(complexity._cc_generic("x.go", without_default))["Branchy"]
            == cc(complexity._cc_generic("x.go", GO))["Branchy"])


def test_cc_python_counts():
    counts = cc(complexity._cc_python(PY))
    depths = depth(complexity._cc_python(PY))

    assert counts["branchy"] == 7  # 1 + if + and + for + except + comprehension + its guard
    assert counts["K.method"] == 2  # 1 + ternary
    assert "method" not in counts
    assert counts["K.outer"] == 1
    assert counts["K.outer.inner"] == 2  # 1 + while
    assert depths["K.outer"] == 0
    assert depths["K.outer.inner"] == 1


def test_unsupported_sources_return_none():
    assert complexity._cc_python("def (:::") is None
    assert complexity._cc_generic("notes.txt", "hello") is None


def test_depth_is_zero_for_a_flat_function():
    _need_grammar("x.go")
    assert depth(complexity._cc_generic("x.go", GO))["Plain"] == 0
    assert depth(complexity._cc_python(PY))["plain"] == 0


def test_depth_is_counted_through_real_nesting():
    _need_grammar("x.go")
    # for -> if -> if
    assert depth(complexity._cc_generic("x.go", GO_NESTED))["Nested"] == 3
    # if -> for -> except
    assert depth(complexity._cc_python(PY))["branchy"] == 3


def test_a_switch_and_its_bool_operators_add_one_level_not_one_per_case():
    _need_grammar("x.go")
    # Branchy's `a > 1 && b` sits inside a single if with nothing else nested.
    assert depth(complexity._cc_generic("x.go", GO))["Branchy"] == 1


# TS/JS wrap an else-if continuation in an `else_clause` node one layer deeper than Go's plain
# nested if, so the TS rows exercise the extra unwrapping step.
@pytest.mark.parametrize("path, source, name, expected", [
    ("x.go", GO_CHAINED, "Chained", 1),
    ("x.py", PY_CHAINED, "chained", 1),
    ("x.ts", TS_CHAINED, "chained", 1),
    ("x.ts", TS_UNRELATED_IF_IN_FINAL_ELSE, "f", 2),
], ids=["go-else-if", "python-elif", "ts-else-clause", "ts-unrelated-if-in-final-else"])
def test_depth_else_if_chains(path, source, name, expected):
    if path.endswith(".py"):
        counts = complexity._cc_python(source)
    else:
        _need_grammar(path)
        counts = complexity._cc_generic(path, source)

    assert depth(counts)[name] == expected


def _counts(**by_name):
    """{name: {"cc", "lines"}} with every function on its own line, so a caller that passes no
    hunk ranges sees them all as touched."""
    return {name: {"cc": n, "lines": [i + 1, i + 1]} for i, (name, n) in enumerate(by_name.items())}


def _stub(monkeypatch, before, after):
    monkeypatch.setattr(complexity, "complexity_at",
                        lambda _repo, ref, path: ({"base": before, "head": after}[ref][path], "ok"))


def _entry(cc, depth, line=1):
    return {"cc": cc, "depth": depth, "lines": [line, line]}


def test_peak_is_where_the_worst_touched_function_now_stands(monkeypatch):
    # The sum would call this file +3 and rank it above one holding a single 40-branch
    # function, which is the aggregation this replaced.
    _stub(monkeypatch, {"a.go": _counts(Big=40)},
          {"a.go": _counts(Big=40, NewOne=1, NewTwo=1, NewThree=1)})
    entry = complexity.analyse(".", "base", "head", ["a.go"])["files"]["a.go"]

    assert entry["peak"]["name"] == "Big"
    assert entry["peak"]["after"] == 40
    assert entry["delta"] == 3


def test_jump_ignores_new_functions_whose_delta_is_just_their_own_size(monkeypatch):
    _stub(monkeypatch, {"a.go": _counts(Old=4)}, {"a.go": _counts(Old=5, Fresh=9)})
    entry = complexity.analyse(".", "base", "head", ["a.go"])["files"]["a.go"]

    assert entry["jump"]["name"] == "Old"
    assert entry["jump"]["delta"] == 1


def test_jump_reports_a_fall_as_readily_as_a_rise(monkeypatch):
    _stub(monkeypatch, {"a.go": _counts(Hairy=20)}, {"a.go": _counts(Hairy=6)})
    entry = complexity.analyse(".", "base", "head", ["a.go"])["files"]["a.go"]

    assert entry["jump"]["delta"] == -14


def test_jump_is_none_when_only_new_functions_appeared(monkeypatch):
    _stub(monkeypatch, {"a.go": {}}, {"a.go": _counts(Fresh=3)})
    entry = complexity.analyse(".", "base", "head", ["a.go"])["files"]["a.go"]

    assert entry["jump"] is None
    assert entry["peak"]["name"] == "Fresh"


def test_a_function_the_diff_never_reached_is_not_reported_as_touched(monkeypatch):
    counts = {"a.go": {"Far": {"cc": 40, "lines": [200, 260]},
                       "Near": {"cc": 2, "lines": [10, 14]}}}
    _stub(monkeypatch, counts, counts)
    entry = complexity.analyse(".", "base", "head", ["a.go"],
                               {"a.go": [(9, 12)]})["files"]["a.go"]

    assert entry["peak"]["name"] == "Near"


def test_symbols_lists_only_touched_functions_that_moved(monkeypatch):
    _stub(monkeypatch, {"a.go": _counts(Keep=3, Fell=9, Gone=4)},
          {"a.go": _counts(Keep=3, Fell=2, New=7)})
    entry = complexity.analyse(".", "base", "head", ["a.go"])["files"]["a.go"]

    assert [(s["name"], s["delta"]) for s in entry["symbols"]] == [
        ("New", 7), ("Gone", -4), ("Fell", -7)]
    assert entry["delta"] == -4


def test_worst_is_the_highest_standing_function_across_the_whole_diff(monkeypatch):
    _stub(monkeypatch, {"a.go": _counts(Small=2), "b.go": _counts(Huge=30)},
          {"a.go": _counts(Small=9), "b.go": _counts(Huge=31)})
    out = complexity.analyse(".", "base", "head", ["a.go", "b.go"])

    # a.go moved by 7 against b.go's 1, so a delta-ranked worst would pick the wrong one.
    assert out["worst"]["name"] == "Huge"
    assert out["worst"]["path"] == "b.go"


def test_worst_is_none_when_nothing_touched_branches_at_all(monkeypatch):
    _stub(monkeypatch, {"a.go": {}}, {"a.go": {}})

    assert complexity.analyse(".", "base", "head", ["a.go"])["worst"] is None


def test_a_file_absent_from_both_refs_is_skipped_not_counted(monkeypatch):
    monkeypatch.setattr(complexity, "complexity_at", lambda *_a: ({}, "absent"))
    out = complexity.analyse(".", "base", "head", ["gone.go"])
    assert out["files"] == {} and out["unsupported"] == []


def test_a_file_one_side_cannot_read_is_reported_not_silently_zero(monkeypatch):
    monkeypatch.setattr(complexity, "complexity_at",
                        lambda _repo, ref, _path: ({}, "ok" if ref == "base" else "unsupported"))
    out = complexity.analyse(".", "base", "head", ["a.min.js"])
    assert out["files"] == {} and out["unsupported"] == ["a.min.js"]


def _unreadable(out):
    out["unsupported"] = ["x.min.js", "y.min.js"]
    return out


@pytest.mark.parametrize("before, after, tweak, check", [
    (_counts(Hairy=42), _counts(Hairy=43), None,
     lambda s: s == "worst Hairy at 43 (+1 here)"),
    (_counts(Hairy=43), _counts(Hairy=43), None,
     lambda s: s == "worst Hairy at 43 (unchanged here)"),
    (_counts(Hairy=42), _counts(Hairy=43), _unreadable,
     lambda s: s.endswith("; 2 files not measured")),
    ({"Hairy": _entry(43, 3)}, {"Hairy": _entry(43, 3)}, None,
     lambda s: "nested" not in s),
    ({"Hairy": _entry(42, 4)}, {"Hairy": _entry(43, 4)}, None,
     lambda s: s == "worst Hairy at 43 (+1 here), nested 4 deep"),
], ids=["leads-with-worst", "unchanged-worst", "unreadable-files", "silent-below-depth", "depth-at-threshold"])
def test_summary_line(monkeypatch, before, after, tweak, check):
    _stub(monkeypatch, {"a.go": before}, {"a.go": after})
    out = complexity.analyse(".", "base", "head", ["a.go"])
    if tweak:
        out = tweak(out)

    assert check(complexity.summary_line(out))


def test_summary_line_is_empty_when_nothing_was_measured():
    assert complexity.summary_line(None) == ""
    assert complexity.summary_line({"files": {}}) == ""


def test_a_renamed_files_unchanged_function_reads_as_zero_delta_not_a_move(monkeypatch):
    diff_text = "diff --git a/old.go b/new.go\n"
    _stub(monkeypatch, {"old.go": _counts(Foo=3)}, {"new.go": _counts(Foo=3)})
    entry = complexity.analyse(".", "base", "head", ["new.go"], diff_text=diff_text)["files"]["new.go"]

    assert entry["delta"] == 0
    assert entry["jump"] is None
    assert entry["symbols"] == []


def test_a_renamed_files_grown_function_reports_its_true_delta(monkeypatch):
    diff_text = "diff --git a/old.go b/new.go\n"
    _stub(monkeypatch, {"old.go": _counts(Foo=3)}, {"new.go": _counts(Foo=5)})
    entry = complexity.analyse(".", "base", "head", ["new.go"], diff_text=diff_text)["files"]["new.go"]

    assert entry["delta"] == 2
    assert entry["jump"]["name"] == "Foo"
    assert entry["jump"]["delta"] == 2


@pytest.mark.parametrize("before, after, expected", [
    ({"a.go": _counts(Gone=4)}, {"a.go": {}},
     {"name": "Gone", "existed": True, "removed": True}),
    ({"a.go": {}}, {"a.go": _counts(Fresh=3)},
     {"existed": False, "removed": False}),
    ({"a.go": _counts(Gone=4), "b.go": _counts(Unrelated=1)},
     {"a.go": {}, "b.go": _counts(Unrelated=1)},
     {"name": "Gone", "removed": True, "delta": -4}),
    # A.String going missing must not be masked as "moved" because a different receiver's
    # same-named method exists in b.go: matching on the bare name would hide a real deletion.
    ({"a.go": _counts(**{"A.String": 3}), "b.go": _counts(**{"B.String": 2})},
     {"a.go": {}, "b.go": _counts(**{"B.String": 2})},
     {"name": "A.String", "removed": True}),
], ids=["deleted", "new", "absent-everywhere", "look-alike-receiver"])
def test_removed_vs_new_function_flags(monkeypatch, before, after, expected):
    _stub(monkeypatch, before, after)
    entry = complexity.analyse(".", "base", "head", list(before))["files"]["a.go"]
    symbol = entry["symbols"][0]

    assert {key: symbol[key] for key in expected} == expected


def test_a_split_functions_move_is_detected_without_a_rename_header(monkeypatch):
    # Git only calls it a rename above its similarity threshold; a split shows up as a plain
    # delete plus a plain add with no rename header, so there is no diff_text for the
    # existing rename resolution to use. This is that gap: Observe is gone from b.go's own
    # head but still alive at head in a.go, which this diff also touched, so it's a move.
    _stub(monkeypatch,
          {"a.go": _counts(Observe=2), "b.go": _counts(Observe=2)},
          {"a.go": _counts(Observe=2), "b.go": {}})
    entry = complexity.analyse(".", "base", "head", ["a.go", "b.go"])["files"]["b.go"]

    assert entry["symbols"] == []
    assert entry["jump"] is None
    assert entry["peak"] is None


def test_missing_diff_text_falls_back_to_same_path_and_reads_a_move_as_new(monkeypatch):
    # Mirrors the bug this fixes: without diff_text there is no rename to resolve, so a file
    # genuinely renamed to new.go is compared against itself at base, where it doesn't exist,
    # and its moved function reads as brand new rather than as removed-and-added.
    def fake(_repo, ref, path):
        data = {"base": {}, "head": {"new.go": _counts(Foo=3)}}
        return (data[ref].get(path, {}), "ok" if path in data[ref] else "absent")
    monkeypatch.setattr(complexity, "complexity_at", fake)

    entry = complexity.analyse(".", "base", "head", ["new.go"])["files"]["new.go"]
    symbol = entry["symbols"][0]

    assert symbol["existed"] is False
    assert symbol["removed"] is False
