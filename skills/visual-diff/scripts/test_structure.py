#!/usr/bin/env python3
"""Self-check for structure.py. Each test builds a throwaway git repo and cleans up."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import structure as structure_mod  # noqa: E402

SCRIPT_PATH = Path(__file__).parent / "structure.py"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Structure Test",
    "GIT_AUTHOR_EMAIL": "structure-test@example.com",
    "GIT_COMMITTER_NAME": "Structure Test",
    "GIT_COMMITTER_EMAIL": "structure-test@example.com",
}


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env={**os.environ, **GIT_ENV},
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


def _write(repo, rel_path, content):
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _run(repo, base, head, files=None):
    cmd = [sys.executable, str(SCRIPT_PATH), "--repo", str(repo), "--base", base, "--head", head]
    if files:
        cmd += ["--files", *files]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, f"structure.py failed: {result.stderr}"
    return json.loads(result.stdout)


def _pairs(entries):
    return {(tuple(e["from"]), tuple(e["to"]), e["kind"]) for e in entries}


def test_inheritance_across_files_is_an_extends_edge():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, "base.py", "class Base:\n    def run(self):\n        pass\n")
        _write(repo, "child.py", "from base import Base\n\n\nclass Child:\n    pass\n")
        base = _commit(repo, "base")
        _write(repo, "child.py", "from base import Base\n\n\nclass Child(Base):\n    pass\n")
        head = _commit(repo, "head")

        result = _run(repo, base, head, ["child.py"])

        assert (("child.py", "Child"), ("base.py", "Base"), "extends") in _pairs(result["added"])
        assert result["changed"] is True
        assert "==>|extends|" in result["mermaid"]

        # "ids" is what lets a click on a mermaid node resolve back to a real path:symbol
        # (see sections.py's explorer), so every mermaid node id must map to one.
        assert set(result["ids"].values()) == {"child.py:Child", "base.py:Base"}
        for node_id, name in result["ids"].items():
            assert f'{node_id}["' in result["mermaid"]
            path, symbol = name.split(":", 1)
            assert path in result["mermaid"] and symbol in result["mermaid"]


def test_function_call_across_files_is_a_uses_edge():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, "helpers.py", "def helper():\n    return 1\n")
        _write(repo, "caller.py", "from helpers import helper\n\n\ndef go():\n    return 0\n")
        base = _commit(repo, "base")
        _write(repo, "caller.py", "from helpers import helper\n\n\ndef go():\n    return helper()\n")
        head = _commit(repo, "head")

        result = _run(repo, base, head, ["caller.py"])

        assert (("caller.py", "go"), ("helpers.py", "helper"), "uses") in _pairs(result["added"])


def test_removed_reference_is_a_removed_edge():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, "helpers.py", "def helper():\n    return 1\n")
        _write(repo, "caller.py", "from helpers import helper\n\n\ndef go():\n    return helper()\n")
        base = _commit(repo, "base")
        _write(repo, "caller.py", "def go():\n    return 0\n")
        head = _commit(repo, "head")

        result = _run(repo, base, head, ["caller.py"])

        assert (("caller.py", "go"), ("helpers.py", "helper"), "uses") in _pairs(result["removed"])
        # `uses` arrows are deliberately unlabelled: the word would repeat on nearly every
        # arrow. Only `extends` carries a label.
        assert "-.->" in result["mermaid"]
        assert "|uses|" not in result["mermaid"]


def test_module_alias_attribute_resolves_to_the_symbol():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, "pkg/__init__.py", "")
        _write(repo, "pkg/conf.py", "def setting():\n    return 1\n")
        _write(repo, "app.py", "import pkg.conf as conf\n\n\ndef go():\n    return 0\n")
        base = _commit(repo, "base")
        _write(repo, "app.py", "import pkg.conf as conf\n\n\ndef go():\n    return conf.setting()\n")
        head = _commit(repo, "head")

        result = _run(repo, base, head, ["app.py"])

        assert (("app.py", "go"), ("pkg/conf.py", "setting"), "uses") in _pairs(result["added"])


def test_unchanged_relations_still_draw_a_diagram():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, "helpers.py", "def helper():\n    return 1\n")
        _write(repo, "caller.py", "from helpers import helper\n\n\ndef go():\n    return helper()\n")
        base = _commit(repo, "base")
        _write(repo, "caller.py", "from helpers import helper\n\n\ndef go():\n    return helper() + 1\n")
        head = _commit(repo, "head")

        result = _run(repo, base, head, ["caller.py"])

        assert result["changed"] is False
        assert "-->" in result["mermaid"]
        assert result["note"]


def test_class_node_lists_its_methods():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, "base.py", "class Base:\n    pass\n")
        _write(repo, "svc.py", "from base import Base\n\n\nclass Svc:\n    def load(self):\n        pass\n\n    def save(self):\n        pass\n")
        base = _commit(repo, "base")
        _write(repo, "svc.py", "from base import Base\n\n\nclass Svc(Base):\n    def load(self):\n        pass\n\n    def save(self):\n        pass\n")
        head = _commit(repo, "head")

        result = _run(repo, base, head, ["svc.py"])

        assert "load()" in result["mermaid"]
        assert "save()" in result["mermaid"]
        assert 'subgraph F' in result["mermaid"]


def test_deleted_class_with_no_relations_still_shows_as_gone():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, "suite.py", "class Keeper:\n    pass\n\n\nclass Doomed:\n    def check(self):\n        assert True\n")
        base = _commit(repo, "base")
        _write(repo, "suite.py", "class Keeper:\n    pass\n")
        head = _commit(repo, "head")

        result = _run(repo, base, head, ["suite.py"])

        assert result["structures_removed"] == [["suite.py", "Doomed"]]
        assert result["changed"] is True
        assert "Doomed (gone)" in result["mermaid"]
        assert "check()" in result["mermaid"]


def test_added_function_with_no_relations_shows_as_new():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, "mod.py", "def old():\n    pass\n")
        base = _commit(repo, "base")
        _write(repo, "mod.py", "def old():\n    pass\n\n\ndef fresh():\n    pass\n")
        head = _commit(repo, "head")

        result = _run(repo, base, head, ["mod.py"])

        assert result["structures_added"] == [["mod.py", "fresh"]]
        assert "fresh() (new)" in result["mermaid"]


def test_non_python_change_is_reported_as_unsupported():
    # .proto has no tree-sitter grammar in the generic path's language map, so this stays
    # genuinely unsupported rather than exercising the generic parse path.
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, "app.proto", "message A { int32 x = 1; }\n")
        base = _commit(repo, "base")
        _write(repo, "app.proto", "message A { int32 x = 2; }\n")
        head = _commit(repo, "head")

        result = _run(repo, base, head, ["app.proto"])

        assert result["skipped"]["unsupported"] == ["app.proto"]
        assert result["mermaid"] == ""


def test_syntax_error_is_unparseable_and_does_not_crash():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, "bad.py", "def f():\n    pass\n")
        base = _commit(repo, "base")
        _write(repo, "bad.py", "def f(:\n    pass\n")
        head = _commit(repo, "head")

        result = _run(repo, base, head, ["bad.py"])

        assert result["skipped"]["unparseable"] == ["bad.py"]


def test_short_labels_shortest_unique_tail_after_the_move():
    """short_labels moved to coupling.py; pins its behaviour here so the move itself didn't
    change what it returns."""
    solo = structure_mod.short_labels(["src/adapters/mcp/shared/define-tool.spec.ts"])
    assert solo["src/adapters/mcp/shared/define-tool.spec.ts"] == "define-tool.spec.ts"

    colliding = structure_mod.short_labels([
        "src/adapters/mcp/shared/define-tool.spec.ts",
        "src/adapters/http/define-tool.spec.ts",
    ])
    assert colliding["src/adapters/mcp/shared/define-tool.spec.ts"] == "shared/define-tool.spec.ts"
    assert colliding["src/adapters/http/define-tool.spec.ts"] == "http/define-tool.spec.ts"


def test_is_test_path_reexported_from_coupling():
    """Moved to coupling.py, which owns path conventions; structure.py re-exports it so its
    own rank() and any existing caller keep working unchanged."""
    assert structure_mod.is_test_path("src/foo.test.ts")
    assert not structure_mod.is_test_path("src/foo.ts")


def test_notests_variant_drops_edges_touching_a_test_file():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, "helpers.py", "def helper():\n    return 1\n")
        _write(repo, "caller.py", "from helpers import helper\n\n\ndef go():\n    return 0\n")
        _write(
            repo, "caller.test.py",
            "from helpers import helper\n\n\ndef test_go():\n    return 0\n",
        )
        base = _commit(repo, "base")
        _write(repo, "caller.py", "from helpers import helper\n\n\ndef go():\n    return helper()\n")
        _write(
            repo, "caller.test.py",
            "from helpers import helper\n\n\ndef test_go():\n    return helper()\n",
        )
        head = _commit(repo, "head")

        result = _run(repo, base, head, ["caller.py", "caller.test.py"])

        assert (("caller.py", "go"), ("helpers.py", "helper"), "uses") in _pairs(result["added"])
        assert (("caller.test.py", "test_go"), ("helpers.py", "helper"), "uses") \
            in _pairs(result["added"])

        assert (("caller.py", "go"), ("helpers.py", "helper"), "uses") \
            in _pairs(result["added_notests"])
        assert (("caller.test.py", "test_go"), ("helpers.py", "helper"), "uses") \
            not in _pairs(result["added_notests"])
        assert "caller.test.py" not in result["mermaid_notests"]
        assert "caller.py" in result["mermaid_notests"]


if __name__ == "__main__":
    tests = [
        test_inheritance_across_files_is_an_extends_edge,
        test_function_call_across_files_is_a_uses_edge,
        test_removed_reference_is_a_removed_edge,
        test_module_alias_attribute_resolves_to_the_symbol,
        test_unchanged_relations_still_draw_a_diagram,
        test_class_node_lists_its_methods,
        test_deleted_class_with_no_relations_still_shows_as_gone,
        test_added_function_with_no_relations_shows_as_new,
        test_non_python_change_is_reported_as_unsupported,
        test_syntax_error_is_unparseable_and_does_not_crash,
        test_short_labels_shortest_unique_tail_after_the_move,
        test_is_test_path_reexported_from_coupling,
        test_notests_variant_drops_edges_touching_a_test_file,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
