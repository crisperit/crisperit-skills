#!/usr/bin/env python3
"""Self-check for pipeline.py. Assert-based, no framework. Uses a tiny temp git repo (the
_git helper pattern from test_links.py) and runs each subcommand for real, since pipeline.py's
whole job is driving the other scripts as subprocesses."""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import pipeline  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Pipeline Test",
    "GIT_AUTHOR_EMAIL": "pipeline-test@example.com",
    "GIT_COMMITTER_NAME": "Pipeline Test",
    "GIT_COMMITTER_EMAIL": "pipeline-test@example.com",
}

HUNK_HEADER = re.compile(r"^(@@ .* @@)", re.M)


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env={**os.environ, **GIT_ENV},
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


def _repo(tmp):
    """A repo with one commit adding foo.py, one changing it -- no remote, so links_ok is
    always false here unless a test adds one."""
    repo = Path(tmp) / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "foo.py").write_text("one\ntwo\nthree\n")
    _git(repo, "add", "foo.py")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "foo.py").write_text("one\ntwo\nthree\nwidget_helper\n")
    _git(repo, "add", "foo.py")
    _git(repo, "commit", "-q", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD").strip()
    return repo, base, head


def _scratch(tmp, repo, base, head, verdict=""):
    """A --dir with raw.diff and a passing analysis.json already in place, as the model would
    leave them before invoking the driver."""
    d = Path(tmp) / "work"
    d.mkdir()
    diff_text = _git(repo, "diff", f"{base}...{head}")
    header = HUNK_HEADER.search(diff_text).group(1)
    (d / "raw.diff").write_text(diff_text)
    analysis = {
        "target": f"{base}...{head}",
        "verdict": verdict,
        "overview": "Adds a widget helper.\n\n- widget_helper introduced",
        "flow_mermaid": "",
        "files": [{
            "path": "foo.py",
            "role": "defines the widget helper",
            "hunks": [{"header": header, "note": "adds widget_helper"}],
        }],
    }
    (d / "analysis.json").write_text(json.dumps(analysis))
    return d


def _render_and_capture_argv(dir_, slug):
    """Run `render` for real, but spy on pipeline.run to capture render.py's own argv --
    pipeline.json alone can't prove --explain/--links actually reached the render.py call."""
    calls = []
    original_run = pipeline.run

    def spy(name, *args, **kwargs):
        calls.append((name, args))
        return original_run(name, *args, **kwargs)

    pipeline.run = spy
    try:
        rc = pipeline.main(["render", "--dir", str(dir_), "--slug", slug])
    finally:
        pipeline.run = original_run
    render_args = next(args for name, args in calls if name == "render.py")
    return rc, render_args


def test_all_on_a_valid_analysis_writes_a_page_and_exits_0():
    with tempfile.TemporaryDirectory() as tmp:
        repo, base, head = _repo(tmp)
        d = _scratch(tmp, repo, base, head, verdict="adds a widget helper")

        rc = pipeline.main(["all", "--dir", str(d), "--repo", str(repo), "--base", base,
                             "--head", head, "--slug", "test"])

        assert rc == 0
        page = (d / "test.html").read_text()
        assert "<!-- code-walkthrough:" in page


def test_a_failing_analysis_exits_1_and_writes_nothing_downstream():
    with tempfile.TemporaryDirectory() as tmp:
        repo, base, head = _repo(tmp)
        d = _scratch(tmp, repo, base, head)
        analysis = json.loads((d / "analysis.json").read_text())
        analysis["files"] = []  # foo.py changed in the diff but missing from files[]
        (d / "analysis.json").write_text(json.dumps(analysis))

        rc = pipeline.main(["all", "--dir", str(d), "--repo", str(repo), "--base", base,
                             "--head", head, "--slug", "test"])

        assert rc != 0
        assert not (d / "structure.json").exists()
        assert not (d / "state.json").exists()
        assert not (d / "test.html").exists()


def test_dir_inside_the_repo_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        repo, base, head = _repo(tmp)
        inside = repo / "scratch"

        rc = pipeline.main(["prepare", "--dir", str(inside), "--repo", str(repo), "--base", base,
                             "--head", head])

        assert rc != 0
        assert not inside.exists()


def test_a_stale_section_is_removed_when_symdelta_is_missing():
    with tempfile.TemporaryDirectory() as tmp:
        repo, base, head = _repo(tmp)
        d = _scratch(tmp, repo, base, head)
        (d / "section-structure.html").write_text("<!-- code-walkthrough:structure -->old\n")
        assert not (d / "symdelta.json").exists()

        rc = pipeline.main(["prepare", "--dir", str(d), "--repo", str(repo), "--base", base,
                             "--head", head])

        assert rc == 0
        assert not (d / "section-structure.html").exists()


def test_no_remote_gives_links_ok_false_and_render_drops_links():
    with tempfile.TemporaryDirectory() as tmp:
        repo, base, head = _repo(tmp)
        d = _scratch(tmp, repo, base, head)

        rc = pipeline.main(["prepare", "--dir", str(d), "--repo", str(repo), "--base", base,
                             "--head", head])

        assert rc == 0
        pipeline_data = json.loads((d / "pipeline.json").read_text())
        assert pipeline_data["links_ok"] is False

        rc, render_args = _render_and_capture_argv(d, "test")
        assert rc == 0
        assert "--links" not in render_args


def test_explain_recorded_by_prepare_reaches_render_via_pipeline_json():
    with tempfile.TemporaryDirectory() as tmp:
        repo, base, head = _repo(tmp)
        d = _scratch(tmp, repo, base, head)

        rc = pipeline.main(["prepare", "--dir", str(d), "--repo", str(repo), "--base", base,
                             "--head", head, "--explain"])

        assert rc == 0
        assert json.loads((d / "pipeline.json").read_text())["explain"] is True

        rc, render_args = _render_and_capture_argv(d, "test")
        assert rc == 0
        assert "--explain" in render_args


def test_links_cached_leaves_a_pre_placed_links_json_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        repo, base, head = _repo(tmp)
        d = _scratch(tmp, repo, base, head)
        cached = {"repo_url": "https://github.com/o/r", "pr_url": "", "head_sha": head,
                   "head_pushed": True, "files": []}
        (d / "links.json").write_text(json.dumps(cached))

        rc = pipeline.main(["prepare", "--dir", str(d), "--repo", str(repo), "--base", base,
                             "--head", head, "--links-cached"])

        assert rc == 0
        assert json.loads((d / "links.json").read_text()) == cached
        assert json.loads((d / "pipeline.json").read_text())["links_ok"] is True


def test_render_with_no_pipeline_json_exits_non_zero():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "work"
        d.mkdir()

        rc = pipeline.main(["render", "--dir", str(d), "--slug", "test"])

        assert rc != 0


if __name__ == "__main__":
    tests = [
        test_all_on_a_valid_analysis_writes_a_page_and_exits_0,
        test_a_failing_analysis_exits_1_and_writes_nothing_downstream,
        test_dir_inside_the_repo_is_refused,
        test_a_stale_section_is_removed_when_symdelta_is_missing,
        test_no_remote_gives_links_ok_false_and_render_drops_links,
        test_explain_recorded_by_prepare_reaches_render_via_pipeline_json,
        test_links_cached_leaves_a_pre_placed_links_json_untouched,
        test_render_with_no_pipeline_json_exits_non_zero,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
