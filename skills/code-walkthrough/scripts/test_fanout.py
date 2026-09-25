#!/usr/bin/env python3
"""Self-check for fanout.py. Assert-based, no framework."""

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fanout import batch, do_split, merge, rename_map, split_chunks  # noqa: E402


def _file_diff(path, hunks):
    body = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
    for i in range(hunks):
        body += f"@@ -{i * 10 + 1},2 +{i * 10 + 1},3 @@\n ctx\n+added\n"
    return body


class _Args:
    """Stand-in for the argparse.Namespace do_split reads its options from."""

    def __init__(self, diff, out, max_lines=400, max_batches=8):
        self.diff = diff
        self.out = out
        self.max_lines = max_lines
        self.max_batches = max_batches


DIFF = _file_diff("a.py", 1) + _file_diff("big.py", 30) + _file_diff("c.py", 1)

RENAME_DIFF = (
    "diff --git a/old.py b/new.py\n"
    "similarity index 90%\n"
    "rename from old.py\n"
    "rename to new.py\n"
    "--- a/old.py\n"
    "+++ b/new.py\n"
    "@@ -1,1 +1,1 @@\n"
    "-x\n"
    "+y\n"
)

# The rename's old side, "foo.py", also appears as its own real, unrenamed file earlier in
# the same diff -- a coincidence the remap must not be fooled by.
COLLIDING_RENAME_DIFF = _file_diff("foo.py", 1) + (
    "diff --git a/foo.py b/bar.py\n"
    "similarity index 90%\n"
    "rename from foo.py\n"
    "rename to bar.py\n"
    "--- a/foo.py\n"
    "+++ b/bar.py\n"
    "@@ -1,1 +1,1 @@\n"
    "-x\n"
    "+y\n"
)


def test_split_covers_every_file_once_and_keeps_its_own_text():
    chunks = split_chunks(DIFF)

    assert [path for path, _ in chunks] == ["a.py", "big.py", "c.py"]
    assert "".join(chunk for _, chunk in chunks) == DIFF
    for path, chunk in chunks:
        assert chunk.startswith(f"diff --git a/{path} ")


def test_split_handles_a_deleted_file():
    deleted = "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-bye\n"

    chunks = split_chunks(DIFF + deleted)

    assert [path for path, _ in chunks][-1] == "gone.py"


def test_batching_splits_on_the_budget_and_never_straddles_a_file():
    batches = batch(split_chunks(DIFF), max_lines=20, max_batches=8)

    assert len(batches) > 1
    seen = [path for group in batches for path, _ in group]
    assert seen == ["a.py", "big.py", "c.py"]
    assert all(len(group) >= 1 for group in batches)


def test_max_batches_caps_the_fanout():
    many = "".join(_file_diff(f"f{i}.py", 5) for i in range(40))

    batches = batch(split_chunks(many), max_lines=10, max_batches=4)

    assert len(batches) <= 4


def test_merge_orders_by_the_diff_and_carries_prose():
    with tempfile.TemporaryDirectory() as tmp:
        one = Path(tmp) / "f1.json"
        two = Path(tmp) / "f2.json"
        # Deliberately out of diff order, and c.py before a.py inside its fragment.
        two.write_text(json.dumps({"files": [
            {"path": "c.py", "role": "r", "hunks": []},
            {"path": "a.py", "role": "r", "hunks": []},
        ]}))
        one.write_text(json.dumps({"files": [{"path": "big.py", "role": "r", "hunks": []}]}))

        result = merge(DIFF, [str(two), str(one)], {
            "target": "main...HEAD", "overview": "w", "flow_mermaid": "",
        })

        assert [f["path"] for f in result["files"]] == ["a.py", "big.py", "c.py"]
        assert result["target"] == "main...HEAD"
        assert result["overview"] == "w"


def test_merge_keeps_an_invented_file_so_the_gate_can_name_it():
    with tempfile.TemporaryDirectory() as tmp:
        frag = Path(tmp) / "f.json"
        frag.write_text(json.dumps({"files": [{"path": "ghost.py", "role": "r", "hunks": []}]}))

        result = merge(DIFF, [str(frag)], {})

        assert [f["path"] for f in result["files"]] == ["ghost.py"]


def test_merge_rejects_the_same_file_from_two_fragments():
    with tempfile.TemporaryDirectory() as tmp:
        one = Path(tmp) / "f1.json"
        two = Path(tmp) / "f2.json"
        entry = {"files": [{"path": "a.py", "role": "r", "hunks": []}]}
        one.write_text(json.dumps(entry))
        two.write_text(json.dumps(entry))

        try:
            merge(DIFF, [str(one), str(two)], {})
        except RuntimeError as exc:
            assert "a.py" in str(exc)
        else:
            raise AssertionError("duplicate file across fragments was accepted")


def test_merge_reports_one_unparseable_fragment_by_path():
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "bad.json"
        bad.write_text("not json")

        try:
            merge(DIFF, [str(bad)], {})
        except RuntimeError as exc:
            assert str(bad) in str(exc)
        else:
            raise AssertionError("unparseable fragment was accepted")


def test_merge_reports_both_unparseable_fragments_not_just_the_first():
    with tempfile.TemporaryDirectory() as tmp:
        bad1 = Path(tmp) / "bad1.json"
        bad2 = Path(tmp) / "bad2.json"
        bad1.write_text("not json")
        bad2.write_text('{"files": [')

        try:
            merge(DIFF, [str(bad1), str(bad2)], {})
        except RuntimeError as exc:
            assert str(bad1) in str(exc)
            assert str(bad2) in str(exc)
        else:
            raise AssertionError("unparseable fragments were accepted")


def test_merge_still_succeeds_when_every_fragment_is_valid():
    with tempfile.TemporaryDirectory() as tmp:
        frag = Path(tmp) / "f.json"
        frag.write_text(json.dumps({"files": [{"path": "a.py", "role": "r", "hunks": []}]}))

        result = merge(DIFF, [str(frag)], {"target": "t"})

        assert result["target"] == "t"
        assert [f["path"] for f in result["files"]] == ["a.py"]


def test_merge_carries_verdict_and_section_notes_through():
    with tempfile.TemporaryDirectory() as tmp:
        frag = Path(tmp) / "f.json"
        frag.write_text(json.dumps({"files": [{"path": "a.py", "role": "r", "hunks": []}]}))
        notes = {"explorer": "e"}  # merge passes section_notes through opaquely, any keys do

        result = merge(DIFF, [str(frag)], {"verdict": "v", "section_notes": notes})

        assert result["verdict"] == "v"
        assert result["section_notes"] == notes


def test_merge_carries_groups_through_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        frag = Path(tmp) / "f.json"
        frag.write_text(json.dumps({"files": [{"path": "a.py", "role": "r", "hunks": []}]}))
        groups = [{"title": "t", "paths": ["a.py"]}]

        result = merge(DIFF, [str(frag)], {"groups": groups})

        assert result["groups"] == groups


def test_merge_omits_groups_key_when_prose_has_none():
    with tempfile.TemporaryDirectory() as tmp:
        frag = Path(tmp) / "f.json"
        frag.write_text(json.dumps({"files": [{"path": "a.py", "role": "r", "hunks": []}]}))

        result = merge(DIFF, [str(frag)], {})

        assert "groups" not in result


def test_merge_defaults_verdict_and_section_notes_when_prose_omits_them():
    with tempfile.TemporaryDirectory() as tmp:
        frag = Path(tmp) / "f.json"
        frag.write_text(json.dumps({"files": [{"path": "a.py", "role": "r", "hunks": []}]}))

        result = merge(DIFF, [str(frag)], {})

        assert result["verdict"] == ""
        # A dict, not a string, matching PROSE_DEFAULTS's falsy shape for this key.
        assert result["section_notes"] == {}


def test_rename_map_finds_the_old_to_new_pairing():
    assert rename_map(RENAME_DIFF) == {"old.py": "new.py"}


def test_rename_map_ignores_files_that_were_not_renamed():
    assert rename_map(DIFF) == {}


def test_merge_canonicalizes_a_fragments_pre_rename_path():
    with tempfile.TemporaryDirectory() as tmp:
        frag = Path(tmp) / "f.json"
        frag.write_text(json.dumps({"files": [{"path": "old.py", "role": "r", "hunks": []}]}))

        result = merge(RENAME_DIFF, [str(frag)], {})

        assert [f["path"] for f in result["files"]] == ["new.py"]


def test_merge_catches_a_duplicate_hidden_behind_the_old_name():
    with tempfile.TemporaryDirectory() as tmp:
        one = Path(tmp) / "f1.json"
        two = Path(tmp) / "f2.json"
        one.write_text(json.dumps({"files": [{"path": "old.py", "role": "r", "hunks": []}]}))
        two.write_text(json.dumps({"files": [{"path": "new.py", "role": "r", "hunks": []}]}))

        try:
            merge(RENAME_DIFF, [str(one), str(two)], {})
        except RuntimeError as exc:
            assert "new.py" in str(exc)
        else:
            raise AssertionError("rename-disguised duplicate was accepted")


def test_merge_does_not_remap_when_the_old_name_is_a_real_file_too():
    with tempfile.TemporaryDirectory() as tmp:
        frag = Path(tmp) / "f.json"
        frag.write_text(json.dumps({"files": [
            {"path": "foo.py", "role": "r", "hunks": []},
            {"path": "bar.py", "role": "r", "hunks": []},
        ]}))

        result = merge(COLLIDING_RENAME_DIFF, [str(frag)], {})

        assert [f["path"] for f in result["files"]] == ["foo.py", "bar.py"]


def test_split_writes_seed_fragments_and_lists_them_in_the_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        diff_path = Path(tmp) / "raw.diff"
        diff_path.write_text(DIFF)
        out_dir = Path(tmp) / "out"

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            do_split(_Args(str(diff_path), str(out_dir), max_lines=20, max_batches=8))
        manifest = json.loads(buf.getvalue())

        assert manifest[0]["seed"] == str(out_dir / "fragment-1.seed.json")
        seed = json.loads(Path(manifest[0]["seed"]).read_text())
        assert seed["files"][0]["path"] == "a.py"
        assert seed["files"][0]["role"] == ""
        assert seed["files"][0]["hunks"] == [{"header": "@@ -1,2 +1,3 @@", "note": ""}]


def test_split_seed_marks_a_binary_file_with_no_hunks():
    binary_diff = (
        "diff --git a/logo.png b/logo.png\n"
        "index 1111111..2222222 100644\n"
        "Binary files a/logo.png and b/logo.png differ\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        diff_path = Path(tmp) / "raw.diff"
        diff_path.write_text(binary_diff)
        out_dir = Path(tmp) / "out"

        with contextlib.redirect_stdout(io.StringIO()):
            do_split(_Args(str(diff_path), str(out_dir)))

        seed = json.loads((out_dir / "fragment-1.seed.json").read_text())
        assert seed["files"] == [{"path": "logo.png", "role": "", "hunks": []}]


if __name__ == "__main__":
    tests = [
        test_split_covers_every_file_once_and_keeps_its_own_text,
        test_split_handles_a_deleted_file,
        test_batching_splits_on_the_budget_and_never_straddles_a_file,
        test_max_batches_caps_the_fanout,
        test_merge_orders_by_the_diff_and_carries_prose,
        test_merge_keeps_an_invented_file_so_the_gate_can_name_it,
        test_merge_rejects_the_same_file_from_two_fragments,
        test_merge_reports_one_unparseable_fragment_by_path,
        test_merge_reports_both_unparseable_fragments_not_just_the_first,
        test_merge_still_succeeds_when_every_fragment_is_valid,
        test_merge_carries_verdict_and_section_notes_through,
        test_merge_carries_groups_through_unchanged,
        test_merge_omits_groups_key_when_prose_has_none,
        test_merge_defaults_verdict_and_section_notes_when_prose_omits_them,
        test_rename_map_finds_the_old_to_new_pairing,
        test_rename_map_ignores_files_that_were_not_renamed,
        test_merge_canonicalizes_a_fragments_pre_rename_path,
        test_merge_catches_a_duplicate_hidden_behind_the_old_name,
        test_merge_does_not_remap_when_the_old_name_is_a_real_file_too,
        test_split_writes_seed_fragments_and_lists_them_in_the_manifest,
        test_split_seed_marks_a_binary_file_with_no_hunks,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
