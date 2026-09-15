#!/usr/bin/env python3
"""Self-check for coupling.py. Each test builds its own throwaway git repo
under tempfile.TemporaryDirectory() and leaves nothing behind."""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import coupling  # noqa: E402
from coupling import is_test_path  # noqa: E402

SCRIPT_PATH = Path(__file__).parent / "coupling.py"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Coupling Test",
    "GIT_AUTHOR_EMAIL": "coupling-test@example.com",
    "GIT_COMMITTER_NAME": "Coupling Test",
    "GIT_COMMITTER_EMAIL": "coupling-test@example.com",
}


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


def _write(repo, rel_path, content):
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _init_repo(repo):
    _git(repo, "init", "-q", "-b", "main")


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _run_coupling(repo, base, head, files):
    cmd = [
        sys.executable, str(SCRIPT_PATH),
        "--repo", str(repo), "--base", base, "--head", head,
        "--files", *files,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, f"coupling.py failed: {result.stderr}"
    return json.loads(result.stdout)


def test_import_added_appears_only_in_added():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.py", "x = 1\n")
        _write(repo, "b.py", "y = 2\n")
        base = _commit(repo, "base")
        _write(repo, "a.py", "import b\nx = 1\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["a.py"])

        assert result["added"] == [["a.py", "b.py"]]
        assert result["removed"] == []
        assert result["unchanged"] == []
        assert result["changed"] is True


def test_import_removed_appears_only_in_removed():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.py", "import b\nx = 1\n")
        _write(repo, "b.py", "y = 2\n")
        base = _commit(repo, "base")
        _write(repo, "a.py", "x = 1\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["a.py"])

        assert result["removed"] == [["a.py", "b.py"]]
        assert result["added"] == []
        assert result["unchanged"] == []
        assert result["changed"] is True


def test_import_present_at_both_refs_is_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.py", "import b\n")
        _write(repo, "b.py", "y = 2\n")
        base = _commit(repo, "base")
        _write(repo, "a.py", "import b\nz = 3\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["a.py"])

        assert result["unchanged"] == [["a.py", "b.py"]]
        assert result["added"] == []
        assert result["removed"] == []
        assert result["changed"] is False
        # Relations still drawn: the diagram is the file-relation view, not a delta view.
        assert "-->" in result["mermaid"]


def test_stdlib_and_third_party_imports_produce_no_edge():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.py", "x = 1\n")
        base = _commit(repo, "base")
        _write(repo, "a.py", "import os\nimport requests\nx = 1\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["a.py"])

        assert result["added"] == []
        assert result["removed"] == []
        assert result["unchanged"] == []
        assert result["changed"] is False


def test_relative_import_resolves_to_sibling_path():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "pkg/__init__.py", "")
        _write(repo, "pkg/sibling.py", "s = 1\n")
        _write(repo, "pkg/a.py", "x = 1\n")
        base = _commit(repo, "base")
        _write(repo, "pkg/a.py", "from . import sibling\nx = 1\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["pkg/a.py"])

        assert result["added"] == [["pkg/a.py", "pkg/sibling.py"]]


def test_unrelated_change_leaves_coupling_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.py", "import b\nx = 1\n")
        _write(repo, "b.py", "y = 2\n")
        base = _commit(repo, "base")
        _write(repo, "a.py", "import b\nx = 2\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["a.py"])

        assert result["changed"] is False
        assert 'N0["a.py"]' in result["mermaid"]


def test_no_edges_at_all_yields_no_diagram():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "lonely.py", "x = 1\n")
        base = _commit(repo, "base")
        _write(repo, "lonely.py", "x = 2\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["lonely.py"])

        assert result["mermaid"] == ""


def test_syntax_error_file_is_unparseable_and_does_not_crash():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "bad.py", "x = 1\n")
        base = _commit(repo, "base")
        _write(repo, "bad.py", "def f(:\n    pass\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["bad.py"])

        assert result["skipped"]["unparseable"] == ["bad.py"]
        assert result["changed"] is False


def test_unsupported_code_extension_is_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "lib.rb", "x = 1\n")
        base = _commit(repo, "base")
        _write(repo, "lib.rb", "x = 2\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["lib.rb"])

        assert result["skipped"]["unsupported"] == ["lib.rb"]
        assert result["changed"] is False


def test_dotfiles_and_doc_extensions_excluded_from_unsupported():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, ".gitignore", "*.pyc\n")
        _write(repo, "notes.md", "hello\n")
        _write(repo, "update.sh", "echo hi\n")
        base = _commit(repo, "base")
        _write(repo, ".gitignore", "*.pyc\n*.log\n")
        _write(repo, "notes.md", "hello again\n")
        _write(repo, "update.sh", "echo bye\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, [".gitignore", "notes.md", "update.sh"])

        assert result["skipped"]["unsupported"] == []
        assert result["changed"] is False


def test_nodenext_js_extension_resolves_to_the_typescript_file():
    """`import './port.js'` inside a .ts file means port.ts, the NodeNext convention."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "src/port.ts", "export const p = 1\n")
        _write(repo, "src/app.ts", "export const a = 1\n")
        base = _commit(repo, "base")
        _write(repo, "src/app.ts", "import { p } from './port.js'\nexport const a = p\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["src/app.ts"])

        assert result["added"] == [["src/app.ts", "src/port.ts"]]


def test_base_is_the_merge_base_not_the_base_branch_tip():
    """A commit that landed on main after the fork must not read as this branch's work."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.py", "x = 1\n")
        _write(repo, "b.py", "y = 2\n")
        _write(repo, "c.py", "z = 3\n")
        _commit(repo, "fork point")

        _git(repo, "checkout", "-q", "-b", "feature")
        _write(repo, "a.py", "import b\nx = 1\n")
        _commit(repo, "branch work")

        _git(repo, "checkout", "-q", "main")
        _write(repo, "a.py", "import c\nx = 1\n")
        _commit(repo, "someone else's work on main")

        result = _run_coupling(repo, "main", "feature", ["a.py"])

        assert result["added"] == [["a.py", "b.py"]]
        assert result["removed"] == [], "main's own commit leaked in as a removal"


def test_mermaid_output_is_well_formed():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.py", "import b\nimport c\n")
        _write(repo, "b.py", "pass\n")
        _write(repo, "c.py", "pass\n")
        _write(repo, "d.py", "pass\n")
        base = _commit(repo, "base")
        _write(repo, "a.py", "import b\nimport d\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["a.py"])

        assert result["added"] == [["a.py", "d.py"]]
        assert result["removed"] == [["a.py", "c.py"]]
        assert result["unchanged"] == [["a.py", "b.py"]]

        mermaid = result["mermaid"]
        assert mermaid.startswith("flowchart LR")
        assert re.search(r"N\d+ ==> N\d+", mermaid)
        assert re.search(r"N\d+ -\.-> N\d+", mermaid)
        assert re.search(r"N\d+ --> N\d+", mermaid)

        node_lines = re.findall(r'(N\d+)\["([^"]*)"\]', mermaid)
        assert len(node_lines) == 4
        for node_id, _label in node_lines:
            assert re.fullmatch(r"N\d+", node_id)
        assert {label for _id, label in node_lines} == {"a.py", "b.py", "c.py", "d.py"}

        # "ids" is what lets a click on a mermaid node resolve back to a real path (see
        # sections.py's explorer), so it must map each id to the same path the node carries.
        assert result["ids"] == {node_id: label for node_id, label in node_lines}


def test_build_mermaid_labels_use_short_unique_tail_not_full_path():
    """A basename collision must resolve to distinct short labels, not the full path; `ids`
    still resolves each node back to its real full path (see build_mermaid)."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        for pkg in ("src", "src/adapters", "src/adapters/mcp", "src/lib"):
            _write(repo, f"{pkg}/__init__.py", "")
        _write(repo, "src/adapters/mcp/helper.py", "def go():\n    pass\n")
        _write(repo, "src/lib/helper.py", "def go():\n    pass\n")
        _write(repo, "main.py", "pass\n")
        base = _commit(repo, "base")
        _write(repo, "main.py", "import src.adapters.mcp.helper\nimport src.lib.helper\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["main.py"])

        mermaid, ids = result["mermaid"], result["ids"]
        label_by_id = dict(re.findall(r'(N\d+)\["([^"]*)"\]', mermaid))

        mcp_id = next(i for i, p in ids.items() if p == "src/adapters/mcp/helper.py")
        lib_id = next(i for i, p in ids.items() if p == "src/lib/helper.py")

        # Shortened to the tail that disambiguates the basename collision, never the full path.
        assert label_by_id[mcp_id] == "mcp/helper.py"
        assert label_by_id[lib_id] == "lib/helper.py"
        assert label_by_id[mcp_id] != label_by_id[lib_id]
        assert set(ids.values()) == {
            "main.py", "src/adapters/mcp/helper.py", "src/lib/helper.py",
        }


def test_build_mermaid_groups_file_nodes_into_module_subgraphs():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "adapters/__init__.py", "")
        _write(repo, "adapters/mcp/__init__.py", "")
        _write(repo, "adapters/mcp/a.py", "from . import b\n")
        _write(repo, "adapters/mcp/b.py", "pass\n")
        _write(repo, "main.py", "import adapters.mcp.a\n")
        base = _commit(repo, "base")
        _write(repo, "main.py", "import adapters.mcp.a\n# noop\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["main.py", "adapters/mcp/a.py"])

        mermaid, ids = result["mermaid"], result["ids"]
        assert 'subgraph G0["adapters/mcp"]' in mermaid

        block = re.search(r'subgraph G0\["adapters/mcp"\](.*?)\n  end', mermaid, re.S)
        assert block, "expected a closed subgraph block for the module"
        mcp_id = next(i for i, p in ids.items() if p == "adapters/mcp/a.py")
        assert f'{mcp_id}["' in block.group(1)

        # A repo-root file has no module to fold into, so it sits outside any subgraph, at
        # the same indent as the flowchart/subgraph lines rather than a grouped node's.
        root_id = next(i for i, p in ids.items() if p == "main.py")
        assert f'\n  {root_id}["' in mermaid
        assert f'\n    {root_id}["' not in mermaid

        assert set(ids.values()) == {"main.py", "adapters/mcp/a.py", "adapters/mcp/b.py"}


def test_several_type_and_constant_files_fold_into_one_node():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "src/app.ts", "export const a = 1\n")
        _write(repo, "src/foo.types.ts", "export type Foo = {}\n")
        _write(repo, "src/bar.constants.ts", "export const BAR = 1\n")
        base = _commit(repo, "base")
        _write(
            repo, "src/app.ts",
            "import { Foo } from './foo.types.js'\n"
            "import { BAR } from './bar.constants.js'\n"
            "export const a = 1\n",
        )
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["src/app.ts"])

        assert result["added"] == [["src/app.ts", "src/__types-constants__"]]
        assert result["type_const_counts"] == {"src/__types-constants__": 2}
        assert '["types & constants (2 files)"]' in result["mermaid"]


def test_two_modules_each_get_their_own_folded_node():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a/app.ts", "export const a = 1\n")
        _write(repo, "a/const.constants.ts", "export const A = 1\n")
        _write(repo, "b/app.ts", "export const b = 1\n")
        _write(repo, "b/const.constants.ts", "export const B = 1\n")
        base = _commit(repo, "base")
        _write(repo, "a/app.ts", "import { A } from './const.constants.js'\nexport const a = 1\n")
        _write(repo, "b/app.ts", "import { B } from './const.constants.js'\nexport const b = 1\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["a/app.ts", "b/app.ts"])

        assert sorted(result["added"]) == [
            ["a/app.ts", "a/__types-constants__"],
            ["b/app.ts", "b/__types-constants__"],
        ]
        assert result["type_const_counts"] == {
            "a/__types-constants__": 1, "b/__types-constants__": 1,
        }


def test_duplicate_edges_into_the_folded_node_are_deduplicated():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "src/app.ts", "export const a = 1\n")
        _write(repo, "src/one.types.ts", "export type One = {}\n")
        _write(repo, "src/two.types.ts", "export type Two = {}\n")
        base = _commit(repo, "base")
        _write(
            repo, "src/app.ts",
            "import { One } from './one.types.js'\n"
            "import { Two } from './two.types.js'\n"
            "export const a = 1\n",
        )
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["src/app.ts"])

        # Two real files both importer-side folded to the same target: exactly one edge, not two.
        assert result["added"] == [["src/app.ts", "src/__types-constants__"]]


def test_folded_node_label_carries_the_file_count():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "src/app.ts", "export const a = 1\n")
        _write(repo, "src/one.types.ts", "export type One = {}\n")
        _write(repo, "src/two.types.ts", "export type Two = {}\n")
        _write(repo, "src/three.types.ts", "export type Three = {}\n")
        base = _commit(repo, "base")
        _write(
            repo, "src/app.ts",
            "import { One } from './one.types.js'\n"
            "import { Two } from './two.types.js'\n"
            "import { Three } from './three.types.js'\n"
            "export const a = 1\n",
        )
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["src/app.ts"])

        assert result["type_const_counts"] == {"src/__types-constants__": 3}
        assert 'types & constants (3 files)' in result["mermaid"]


def test_folded_node_is_absent_from_ids():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "src/app.ts", "export const a = 1\n")
        _write(repo, "src/foo.types.ts", "export type Foo = {}\n")
        base = _commit(repo, "base")
        _write(repo, "src/app.ts", "import { Foo } from './foo.types.js'\nexport const a = 1\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["src/app.ts"])

        assert "src/__types-constants__" not in result["ids"].values()
        assert "src/app.ts" in result["ids"].values()


def test_constants_and_models_subdirectories_fold_into_separate_module_nodes():
    """The directory IS the module now, so two type/constant files in different subdirectories
    of the same parent (the real adapters/mcp/constants + adapters/mcp/models shape) fold into
    two sentinels, one per subdirectory, not one shared by the parent: constants/ and models/
    are each a module of their own."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "src/adapters/mcp/app.ts", "export const a = 1\n")
        _write(repo, "src/adapters/mcp/constants/a.constants.ts", "export const A = 1\n")
        _write(repo, "src/adapters/mcp/constants/b.constants.ts", "export const B = 1\n")
        _write(repo, "src/adapters/mcp/models/c.type.ts", "export type C = {}\n")
        _write(repo, "src/adapters/mcp/models/d.type.ts", "export type D = {}\n")
        base = _commit(repo, "base")
        _write(
            repo, "src/adapters/mcp/app.ts",
            "import { A } from './constants/a.constants.js'\n"
            "import { B } from './constants/b.constants.js'\n"
            "import { C } from './models/c.type.js'\n"
            "import { D } from './models/d.type.js'\n"
            "export const a = 1\n",
        )
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["src/adapters/mcp/app.ts"])

        assert sorted(result["added"]) == [
            ["src/adapters/mcp/app.ts", "src/adapters/mcp/constants/__types-constants__"],
            ["src/adapters/mcp/app.ts", "src/adapters/mcp/models/__types-constants__"],
        ]
        assert result["type_const_counts"] == {
            "src/adapters/mcp/constants/__types-constants__": 2,
            "src/adapters/mcp/models/__types-constants__": 2,
        }
        assert result["mermaid"].count("types & constants") == 2
        assert result["mermaid"].count('["types & constants (2 files)"]') == 2


def test_a_single_type_file_in_a_module_still_folds():
    """Item 3 is always on: even one leaf type file loses its own box, labelled '(1 file)'."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "src/app.ts", "export const a = 1\n")
        _write(repo, "src/foo.types.ts", "export type Foo = {}\n")
        base = _commit(repo, "base")
        _write(repo, "src/app.ts", "import { Foo } from './foo.types.js'\nexport const a = 1\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["src/app.ts"])

        assert result["added"] == [["src/app.ts", "src/__types-constants__"]]
        assert result["type_const_counts"] == {"src/__types-constants__": 1}
        assert 'types & constants (1 file)"]' in result["mermaid"]
        assert "foo.types.ts" not in result["mermaid"]


def test_notests_variant_folds_only_the_surviving_type_files():
    """Filtering must happen before folding: a type file reachable only through a test import
    must not inflate the tests-hidden sentinel's file count (see drop_test_edges)."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "src/app.ts", "export const a = 1\n")
        _write(repo, "src/app.test.ts", "export const t = 1\n")
        _write(repo, "src/prod.types.ts", "export type Prod = {}\n")
        _write(repo, "src/testonly.types.ts", "export type TestOnly = {}\n")
        base = _commit(repo, "base")
        _write(
            repo, "src/app.ts",
            "import { Prod } from './prod.types.js'\nexport const a = 1\n",
        )
        _write(
            repo, "src/app.test.ts",
            "import { TestOnly } from './testonly.types.js'\nexport const t = 1\n",
        )
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["src/app.ts", "src/app.test.ts"])

        assert result["type_const_counts"] == {"src/__types-constants__": 2}
        assert result["type_const_counts_notests"] == {"src/__types-constants__": 1}
        assert "types & constants (2 files)" in result["mermaid"]
        assert "types & constants (1 file)" in result["mermaid_notests"]
        assert "app.test.ts" not in result["mermaid_notests"]


def test_notests_edges_omit_anything_touching_a_test_file():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.py", "x = 1\n")
        _write(repo, "b.py", "y = 2\n")
        _write(repo, "a.test.py", "x = 1\n")
        base = _commit(repo, "base")
        _write(repo, "a.py", "import b\nx = 1\n")
        _write(repo, "a.test.py", "import b\nx = 1\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["a.py", "a.test.py"])

        assert ["a.py", "b.py"] in result["added"]
        assert ["a.test.py", "b.py"] in result["added"]
        assert result["added_notests"] == [["a.py", "b.py"]]
        assert "a.test.py" not in result["mermaid_notests"]
        assert "a.py" in result["mermaid_notests"]


def test_is_test_path_classifies_across_ecosystems():
    hits = [
        "internal/ratelimit/ratelimit_request_test.go",
        "corelib/macros/macros_test.go",
        "app/foo_test.py",
        "src/main/java/FooTest.java",
        "pkg/a/b_test.go",
        "lib/foo_spec.rb",
        "lib/foo_test.rb",
        "Services/FooTests.cs",
        "Sources/FooTests.swift",
        "conftest.py",
        "src/foo.test.ts",
        "src/foo.spec.ts",
        "tests/x.py",
        "spec/y.rb",
    ]
    for path in hits:
        assert is_test_path(path), f"expected test path: {path}"

    non_test = [
        "latest/x.py",
        "src/contest.py",
        "src/greatest/x.py",
        "greatest.cs",
        "contest.java",
        "manifest.py",
    ]
    for path in non_test:
        assert not is_test_path(path), f"expected non-test path: {path}"


def test_go_edges_are_package_to_package_not_file_to_package():
    """A Go import names a package, so recording the importing file against an imported
    directory put two granularities in one graph and made "both ends touched" unsatisfiable."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "go.mod", "module example.com/m\n")
        _write(repo, "pkg/a/a.go", "package a\n")
        _write(repo, "cmd/main.go", 'package main\nimport "example.com/m/pkg/a"\n')
        base = _commit(repo, "base")
        _write(repo, "cmd/main.go", 'package main\nimport "example.com/m/pkg/a"\n// c\n')
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["cmd/main.go"])
        edges = result["added"] + result["removed"] + result["unchanged"]
        assert ["cmd", "pkg/a"] in edges, edges
        assert not any(e[0].endswith(".go") for e in edges), edges


def test_small_graph_keeps_its_one_hop_neighbours():
    """Narrowing is conditional: a diff whose full graph already fits is more useful whole, so
    an untouched neighbour still gets drawn."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "a.py", "import b\n")
        _write(repo, "b.py", "y = 2\n")
        base = _commit(repo, "base")
        _write(repo, "a.py", "import b\nz = 3\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["a.py"])
        assert "-->" in result["mermaid"]
        assert set(result["ids"].values()) == {"a.py", "b.py"}


def test_dense_graph_narrows_to_edges_between_touched_files():
    """Past the budget the overview keeps only edges with a changed file at both ends, so a wide
    fan of untouched neighbours stops being the opening screen."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        # two changed files that import each other, plus enough untouched neighbours to blow the
        # node budget on their own
        _write(repo, "hub.py", "import leaf\n" + "".join(
            f"import n{i}\n" for i in range(45)))
        _write(repo, "leaf.py", "v = 1\n")
        for i in range(45):
            _write(repo, f"n{i}.py", "x = 1\n")
        base = _commit(repo, "base")
        _write(repo, "leaf.py", "v = 2\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["hub.py", "leaf.py"])
        drawn = set(result["ids"].values())
        assert drawn == {"hub.py", "leaf.py"}, drawn
        # the untouched neighbours stay in the JSON, so a node click can still reach them
        all_nodes = {n for e in result["unchanged"] + result["added"] for n in e}
        assert "n0.py" in all_nodes
        assert "too dense to read" in result["note"]


def test_narrowing_falls_back_when_no_edge_has_both_ends_touched():
    """A dense graph where the diff touched exactly one file has no both-ends edge at all;
    drawing nothing would be worse than drawing the wide version."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "hub.py", "".join(f"import n{i}\n" for i in range(45)))
        for i in range(45):
            _write(repo, f"n{i}.py", "x = 1\n")
        base = _commit(repo, "base")
        _write(repo, "hub.py", "".join(f"import n{i}\n" for i in range(45)) + "z = 1\n")
        head = _commit(repo, "head")

        result = _run_coupling(repo, base, head, ["hub.py"])
        assert "-->" in result["mermaid"], "fell back to nothing at all"
        assert result["truncated"] is True


def test_cap_clusters_keeps_the_biggest_boxes_and_looses_the_rest():
    grouped = {f"m{i}": [f"m{i}/f{j}.py" for j in range(i + 1)] for i in range(20)}
    kept, loose = coupling.cap_clusters(grouped, max_clusters=3)
    assert set(kept) == {"m19", "m18", "m17"}
    assert len(loose) == sum(i + 1 for i in range(17))
    # under the cap nothing moves
    small = {"a": ["a/x.py"], "b": ["b/y.py"]}
    assert coupling.cap_clusters(small, max_clusters=3) == (small, [])


if __name__ == "__main__":
    tests = [
        test_import_added_appears_only_in_added,
        test_import_removed_appears_only_in_removed,
        test_import_present_at_both_refs_is_unchanged,
        test_stdlib_and_third_party_imports_produce_no_edge,
        test_relative_import_resolves_to_sibling_path,
        test_unrelated_change_leaves_coupling_unchanged,
        test_no_edges_at_all_yields_no_diagram,
        test_syntax_error_file_is_unparseable_and_does_not_crash,
        test_unsupported_code_extension_is_skipped,
        test_dotfiles_and_doc_extensions_excluded_from_unsupported,
        test_nodenext_js_extension_resolves_to_the_typescript_file,
        test_base_is_the_merge_base_not_the_base_branch_tip,
        test_mermaid_output_is_well_formed,
        test_build_mermaid_labels_use_short_unique_tail_not_full_path,
        test_build_mermaid_groups_file_nodes_into_module_subgraphs,
        test_several_type_and_constant_files_fold_into_one_node,
        test_two_modules_each_get_their_own_folded_node,
        test_duplicate_edges_into_the_folded_node_are_deduplicated,
        test_folded_node_label_carries_the_file_count,
        test_folded_node_is_absent_from_ids,
        test_constants_and_models_subdirectories_fold_into_separate_module_nodes,
        test_a_single_type_file_in_a_module_still_folds,
        test_notests_variant_folds_only_the_surviving_type_files,
        test_notests_edges_omit_anything_touching_a_test_file,
        test_is_test_path_classifies_across_ecosystems,
        test_go_edges_are_package_to_package_not_file_to_package,
        test_small_graph_keeps_its_one_hop_neighbours,
        test_dense_graph_narrows_to_edges_between_touched_files,
        test_narrowing_falls_back_when_no_edge_has_both_ends_touched,
        test_cap_clusters_keeps_the_biggest_boxes_and_looses_the_rest,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
