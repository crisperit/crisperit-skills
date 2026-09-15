#!/usr/bin/env python3
"""Self-check for links.py. Assert-based, no framework, no network."""

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from links import build, file_anchor, line_range, repo_web_url  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Links Test",
    "GIT_AUTHOR_EMAIL": "links-test@example.com",
    "GIT_COMMITTER_NAME": "Links Test",
    "GIT_COMMITTER_EMAIL": "links-test@example.com",
}

DIFF = """diff --git a/pkg/thing.py b/pkg/thing.py
--- a/pkg/thing.py
+++ b/pkg/thing.py
@@ -10,3 +12,5 @@ def go():
 ctx
+added
@@ -40,2 +50,1 @@ def stop():
-gone
diff --git a/dropped.py b/dropped.py
--- a/dropped.py
+++ /dev/null
@@ -1,3 +0,0 @@
-bye
"""


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env={**os.environ, **GIT_ENV},
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


def _repo(tmp, remote):
    repo = Path(tmp)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "remote", "add", "origin", remote)
    (repo / "f.txt").write_text("x\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


def test_file_anchor_is_sha256_of_the_path():
    # Verified against a live GitHub PR page: the anchor was diff-<this hash>.
    path = "acceptance/testdata/repo/repo-archive-unarchive.txtar"

    assert file_anchor(path) == "diff-" + hashlib.sha256(path.encode()).hexdigest()


def test_line_range_reads_the_new_side_and_skips_pure_deletions():
    assert line_range("@@ -10,3 +12,5 @@") == (12, 16)
    assert line_range("@@ -40,2 +50,1 @@") == (50, 50)
    assert line_range("@@ -1,3 +0,0 @@") is None
    assert line_range("@@ -1 +7 @@") == (7, 7)


def test_ssh_and_https_remotes_both_become_a_web_url():
    for remote in ("git@github.com:o/r.git", "https://github.com/o/r.git",
                   "ssh://git@github.com/o/r", "https://token@github.com/o/r"):
        with tempfile.TemporaryDirectory() as tmp:
            assert repo_web_url(str(_repo(tmp, remote))) == "https://github.com/o/r"


def test_enterprise_host_is_kept():
    with tempfile.TemporaryDirectory() as tmp:
        repo = _repo(tmp, "git@git.corp.example:team/app.git")

        assert repo_web_url(str(repo)) == "https://git.corp.example/team/app"


def test_urls_are_built_per_file_and_per_hunk():
    with tempfile.TemporaryDirectory() as tmp:
        repo = _repo(tmp, "git@github.com:o/r.git")

        result = build(str(repo), DIFF, "HEAD", "19")

        sha = result["head_sha"]
        first = result["files"][0]
        assert first["path"] == "pkg/thing.py"
        assert first["diff_url"] == f"https://github.com/o/r/pull/19/files#{file_anchor('pkg/thing.py')}"
        assert first["blob_url"] == f"https://github.com/o/r/blob/{sha}/pkg/thing.py"
        assert first["hunks"][0]["url"].endswith("#L12-L16")
        assert first["hunks"][1]["url"].endswith("#L50")
        assert result["pr_url"] == "https://github.com/o/r/pull/19"
        # A deleted file has no new-side lines, so no line link.
        assert result["files"][1]["path"] == "dropped.py"
        assert result["files"][1]["hunks"][0]["url"] == ""


def test_no_pr_number_means_no_pr_links_but_still_blob_links():
    with tempfile.TemporaryDirectory() as tmp:
        repo = _repo(tmp, "git@github.com:o/r.git")

        result = build(str(repo), DIFF, "HEAD", None)

        assert result["pr_url"] == ""
        assert result["files"][0]["diff_url"] == ""
        assert result["files"][0]["blob_url"].startswith("https://github.com/o/r/blob/")


def test_unpushed_head_is_flagged_so_dead_links_can_be_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        repo = _repo(tmp, "git@github.com:o/r.git")

        assert build(str(repo), DIFF, "HEAD", "19")["head_pushed"] is False


def test_no_remote_means_no_links_at_all():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        (repo / "f.txt").write_text("x\n")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-q", "-m", "first")

        result = build(str(repo), DIFF, "HEAD", "19")

        assert result["repo_url"] == ""
        assert result["files"][0]["blob_url"] == ""


def test_a_path_with_a_space_is_encoded():
    with tempfile.TemporaryDirectory() as tmp:
        repo = _repo(tmp, "git@github.com:o/r.git")
        spaced = 'diff --git "a/my file.py" "b/my file.py"\n--- "a/my file.py"\n+++ "b/my file.py"\n@@ -1,1 +1,2 @@\n+x\n'

        result = build(str(repo), spaced, "HEAD", "19")

        assert "my%20file.py" in result["files"][0]["blob_url"]


if __name__ == "__main__":
    tests = [
        test_file_anchor_is_sha256_of_the_path,
        test_line_range_reads_the_new_side_and_skips_pure_deletions,
        test_ssh_and_https_remotes_both_become_a_web_url,
        test_enterprise_host_is_kept,
        test_urls_are_built_per_file_and_per_hunk,
        test_no_pr_number_means_no_pr_links_but_still_blob_links,
        test_unpushed_head_is_flagged_so_dead_links_can_be_skipped,
        test_no_remote_means_no_links_at_all,
        test_a_path_with_a_space_is_encoded,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
