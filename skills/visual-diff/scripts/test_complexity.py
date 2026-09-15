import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import complexity

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


def test_a_function_with_no_branches_is_one():
    assert cc(complexity._cc_generic("x.go", GO))["Plain"] == 1


def test_every_decision_point_counts_once():
    # 1 + if + && + for + two non-default cases
    assert cc(complexity._cc_generic("x.go", GO))["Branchy"] == 6


def test_go_methods_sharing_a_name_are_kept_apart_by_receiver():
    counts = cc(complexity._cc_generic("x.go", GO))
    assert counts["A.Get"] == 2
    assert counts["B.Get"] == 2
    assert "Get" not in counts


def test_default_arm_is_not_a_decision():
    without_default = GO.replace("\tdefault:\n\t\treturn 4\n", "")
    assert (cc(complexity._cc_generic("x.go", without_default))["Branchy"]
            == cc(complexity._cc_generic("x.go", GO))["Branchy"])


def test_python_counts_boolop_loops_handlers_and_comprehension_guards():
    # 1 + if + and + for + except + comprehension + its guard
    assert cc(complexity._cc_python(PY))["branchy"] == 7


def test_python_methods_are_qualified_by_class():
    counts = cc(complexity._cc_python(PY))
    assert counts["K.method"] == 2  # 1 + ternary
    assert "method" not in counts


def test_a_closure_is_charged_for_its_own_branches_not_its_parent():
    counts = cc(complexity._cc_python(PY))
    assert counts["K.outer"] == 1
    assert counts["K.outer.inner"] == 2  # 1 + while


def test_unparseable_python_is_unsupported_rather_than_zero():
    assert complexity._cc_python("def (:::") is None


def test_a_file_with_no_grammar_is_unsupported_rather_than_zero():
    assert complexity._cc_generic("notes.txt", "hello") is None


def test_depth_is_zero_for_a_flat_function():
    assert depth(complexity._cc_generic("x.go", GO))["Plain"] == 0
    assert depth(complexity._cc_python(PY))["plain"] == 0


def test_depth_is_counted_through_real_nesting():
    # for -> if -> if
    assert depth(complexity._cc_generic("x.go", GO_NESTED))["Nested"] == 3
    # if -> for -> except
    assert depth(complexity._cc_python(PY))["branchy"] == 3


def test_a_switch_with_several_cases_is_one_level_not_one_per_case():
    assert depth(complexity._cc_generic("x.go", GO))["Branchy"] == 1


def test_bool_operators_do_not_add_depth():
    # Branchy's `a > 1 && b` sits inside a single if with nothing else nested.
    assert depth(complexity._cc_generic("x.go", GO))["Branchy"] == 1


def test_an_else_if_chain_does_not_stack_in_the_generic_tier():
    assert depth(complexity._cc_generic("x.go", GO_CHAINED))["Chained"] == 1


def test_an_elif_chain_does_not_stack_in_the_python_tier():
    assert depth(complexity._cc_python(PY_CHAINED))["chained"] == 1


def test_an_else_if_chain_does_not_stack_when_wrapped_in_an_else_clause():
    # TS/JS nest the continuation inside an `else_clause` rather than putting it directly in
    # the `alternative` field the way Go does, so this exercises the extra unwrapping step.
    assert depth(complexity._cc_generic("x.ts", TS_CHAINED))["chained"] == 1


def test_an_unrelated_if_inside_a_final_else_still_nests_normally():
    assert depth(complexity._cc_generic("x.ts", TS_UNRELATED_IF_IN_FINAL_ELSE))["f"] == 2


def test_a_closures_depth_is_not_charged_to_its_enclosing_function():
    counts = depth(complexity._cc_python(PY))
    assert counts["K.outer"] == 0
    assert counts["K.outer.inner"] == 1


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


def test_summary_line_leads_with_where_the_worst_function_stands(monkeypatch):
    _stub(monkeypatch, {"a.go": _counts(Hairy=42)}, {"a.go": _counts(Hairy=43)})
    out = complexity.analyse(".", "base", "head", ["a.go"])

    assert complexity.summary_line(out) == "worst Hairy at 43 (+1 here)"


def test_summary_line_says_so_when_the_worst_function_did_not_move(monkeypatch):
    _stub(monkeypatch, {"a.go": _counts(Hairy=43)}, {"a.go": _counts(Hairy=43)})
    out = complexity.analyse(".", "base", "head", ["a.go"])

    assert complexity.summary_line(out) == "worst Hairy at 43 (unchanged here)"


def test_summary_line_counts_the_files_it_could_not_read(monkeypatch):
    _stub(monkeypatch, {"a.go": _counts(Hairy=42)}, {"a.go": _counts(Hairy=43)})
    out = complexity.analyse(".", "base", "head", ["a.go"])
    out["unsupported"] = ["x.min.js", "y.min.js"]

    assert complexity.summary_line(out).endswith("; 2 files not measured")


def test_summary_line_stays_silent_below_the_noteworthy_depth(monkeypatch):
    entry = {"Hairy": _entry(43, 3)}
    _stub(monkeypatch, {"a.go": entry}, {"a.go": entry})
    out = complexity.analyse(".", "base", "head", ["a.go"])

    assert "nested" not in complexity.summary_line(out)


def test_summary_line_reports_depth_at_the_noteworthy_threshold(monkeypatch):
    _stub(monkeypatch, {"a.go": {"Hairy": _entry(42, 4)}}, {"a.go": {"Hairy": _entry(43, 4)}})
    out = complexity.analyse(".", "base", "head", ["a.go"])

    assert complexity.summary_line(out) == "worst Hairy at 43 (+1 here), nested 4 deep"


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


def test_a_genuinely_deleted_function_is_marked_removed(monkeypatch):
    _stub(monkeypatch, {"a.go": _counts(Gone=4)}, {"a.go": {}})
    entry = complexity.analyse(".", "base", "head", ["a.go"])["files"]["a.go"]
    symbol = entry["symbols"][0]

    assert symbol["name"] == "Gone"
    assert symbol["existed"] is True
    assert symbol["removed"] is True


def test_a_new_function_is_not_marked_removed(monkeypatch):
    _stub(monkeypatch, {"a.go": {}}, {"a.go": _counts(Fresh=3)})
    entry = complexity.analyse(".", "base", "head", ["a.go"])["files"]["a.go"]
    symbol = entry["symbols"][0]

    assert symbol["existed"] is False
    assert symbol["removed"] is False


def test_moved_elsewhere_finds_the_name_in_another_touched_file():
    owners = {"Observe": {"a.go", "b.go"}}
    assert complexity._moved_elsewhere("Observe", "b.go", owners) == ["a.go"]


def test_moved_elsewhere_is_empty_when_no_other_file_has_the_name():
    owners = {"Observe": {"b.go"}}
    assert complexity._moved_elsewhere("Observe", "b.go", owners) == []


def test_moved_elsewhere_does_not_cross_match_a_different_receivers_same_named_method():
    # A.String and B.String are distinct qualified names; B.String existing elsewhere must
    # not paper over A.String's own disappearance.
    owners = {"B.String": {"b.go"}}
    assert complexity._moved_elsewhere("A.String", "a.go", owners) == []


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


def test_a_function_absent_everywhere_stays_removed_when_other_files_are_in_scope(monkeypatch):
    _stub(monkeypatch,
          {"a.go": _counts(Gone=4), "b.go": _counts(Unrelated=1)},
          {"a.go": {}, "b.go": _counts(Unrelated=1)})
    entry = complexity.analyse(".", "base", "head", ["a.go", "b.go"])["files"]["a.go"]
    symbol = entry["symbols"][0]

    assert symbol["name"] == "Gone"
    assert symbol["removed"] is True
    assert symbol["delta"] == -4


def test_same_named_methods_on_different_receivers_do_not_cross_match(monkeypatch):
    # A.String going missing from a.go must not be masked as "moved" just because a
    # different receiver's same-named method (B.String) exists in b.go: cross-matching on
    # the bare name would hide a real deletion behind a look-alike.
    _stub(monkeypatch,
          {"a.go": _counts(**{"A.String": 3}), "b.go": _counts(**{"B.String": 2})},
          {"a.go": {}, "b.go": _counts(**{"B.String": 2})})
    entry = complexity.analyse(".", "base", "head", ["a.go", "b.go"])["files"]["a.go"]
    symbol = entry["symbols"][0]

    assert symbol["name"] == "A.String"
    assert symbol["removed"] is True


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
