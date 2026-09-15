#!/usr/bin/env python3
"""Self-check for layers.py. Each test builds its own throwaway git repo
under tempfile.TemporaryDirectory() and leaves nothing behind."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import layers  # noqa: E402

SCRIPT_PATH = Path(__file__).parent / "layers.py"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Layers Test",
    "GIT_AUTHOR_EMAIL": "layers-test@example.com",
    "GIT_COMMITTER_NAME": "Layers Test",
    "GIT_COMMITTER_EMAIL": "layers-test@example.com",
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


def _init_repo(repo):
    _git(repo, "init", "-q", "-b", "main")


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _run_layers(repo, head, coupling_path=None):
    cmd = [sys.executable, str(SCRIPT_PATH), "--repo", str(repo), "--head", head]
    if coupling_path:
        cmd += ["--coupling", coupling_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, f"layers.py failed: {result.stderr}"
    return json.loads(result.stdout)


def test_hexagonal_named_dirs_never_produce_a_subgraph():
    # domain, application, adapters and infrastructure would all have been real LAYER_KEYWORDS
    # hits under the removed grouping; the map is flat now regardless of directory names.
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "domain/order/o.py", "x = 1\n")
        _write(repo, "application/checkout/c.py", "import domain.order.o\n")
        _write(repo, "adapters/http/h.py", "import application.checkout.c\n")
        _write(repo, "infrastructure/db/d.py", "import adapters.http.h\n")
        head = _commit(repo, "head")

        result = _run_layers(repo, head)

        assert "subgraph" not in result["mermaid"]


def test_nodes_marker_counts_and_edges_still_emitted():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "alpha/a1.py", "import beta.b1\n")
        _write(repo, "alpha/a2.py", "x = 1\n")
        _write(repo, "beta/b1.py", "x = 1\n")
        head = _commit(repo, "head")

        coupling = {"changed_nodes": ["alpha/a1.py"], "added": [], "removed": [], "unchanged": []}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(coupling, f)
            coupling_path = f.name
        try:
            data = _run_layers(repo, head, coupling_path)
        finally:
            os.unlink(coupling_path)

        assert data["edges"] == [{"from": "alpha", "to": "beta", "count": 1}]
        assert data["touched"] == ["alpha"]
        assert 'alpha *<br/>2 files' in data["mermaid"]
        assert 'beta<br/>1 file' in data["mermaid"]
        assert set(data["ids"].values()) == {"alpha", "beta"}


def test_output_carries_no_layer_classification_keys():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "domain/o.py", "import adapters.a\n")
        _write(repo, "adapters/a.py", "x = 1\n")
        head = _commit(repo, "head")

        result = _run_layers(repo, head)

        assert "inversions" not in result
        assert "layered" not in result
        assert "unknown_layer" not in result


def test_go_package_directory_nodes_keep_their_own_name_in_module_edges():
    # aggregate() must resolve edge ends through module_of_node, not module_of: a Go edge's
    # ends are already package directories (coupling.graph_node), and module_of alone would
    # strip "beta" as if it were a filename, folding it into "(root)" instead of naming it.
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _init_repo(repo)
        _write(repo, "go.mod", "module example.com/m\n")
        _write(repo, "alpha/a1.go", 'package alpha\nimport "example.com/m/beta"\n')
        _write(repo, "alpha/a2.go", "package alpha\n")
        _write(repo, "beta/b1.go", "package beta\n")
        head = _commit(repo, "head")

        coupling = {"changed_nodes": ["alpha"], "added": [], "removed": [], "unchanged": []}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(coupling, f)
            coupling_path = f.name
        try:
            data = _run_layers(repo, head, coupling_path)
        finally:
            os.unlink(coupling_path)

        assert data["edges"] == [{"from": "alpha", "to": "beta", "count": 1}]
        assert data["touched"] == ["alpha"]
        assert 'alpha *<br/>2 files' in data["mermaid"]
        assert 'beta<br/>1 file' in data["mermaid"]
        assert set(data["ids"].values()) == {"alpha", "beta"}


if __name__ == "__main__":
    tests = [
        test_hexagonal_named_dirs_never_produce_a_subgraph,
        test_nodes_marker_counts_and_edges_still_emitted,
        test_output_carries_no_layer_classification_keys,
        test_go_package_directory_nodes_keep_their_own_name_in_module_edges,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
