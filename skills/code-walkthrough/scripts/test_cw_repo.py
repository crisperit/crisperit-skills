import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import cw_repo  # noqa: E402
import cw_server  # noqa: E402
import cw_store  # noqa: E402
import cw_testlib  # noqa: E402


class FakeRun:
    def __init__(self, size="204800", fail=None):
        self.calls, self.size, self.fail = [], size, fail

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if argv[:3] == ["gh", "repo", "clone"]:
            Path(argv[4]).mkdir(parents=True)
        rc = 1 if self.fail and self.fail in argv else 0
        out = self.size if argv[:2] == ["gh", "api"] else ""
        return subprocess.CompletedProcess(argv, rc, out, "boom" if rc else "")

    def argvs(self, head):
        return [a for a, _ in self.calls if a[:len(head)] == head]


@pytest.fixture
def home(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "t1")
    monkeypatch.setenv("GITHUB_TOKEN", "t2")
    with cw_testlib.temp_home() as h:
        yield h


def test_absolute_path_passes_through(home):
    run = FakeRun()
    assert cw_repo.resolve({"repo": "/some/where", "pr": 3}, {}, run) == "/some/where"
    assert run.calls == []


def test_config_map_wins_over_cache_and_cache_hit_fetches_pr_and_base(home, tmp_path):
    cache = cw_repo.cache_root() / "acme" / "widgets"
    cache.mkdir(parents=True)
    mine = tmp_path / "mine"
    mine.mkdir()
    run = FakeRun()
    params = {"repo": "Acme/Widgets", "pr": 7, "base": "origin/main"}
    assert cw_repo.resolve(params, {"repos": {"acme/widgets": str(mine)}}, run) == str(mine)
    assert cw_repo.resolve(params, {}, run) == str(cache)
    fetch = run.argvs(["git", "-C", str(cache), "fetch"])
    assert fetch == [["git", "-C", str(cache), "fetch", "origin", "+refs/pull/7/head:refs/cw/pull/7",
                      "+refs/heads/main:refs/remotes/origin/main"]]
    run.calls.clear()
    cw_repo.resolve({"repo": "acme/widgets", "base": "main"}, {}, run)
    assert run.calls == []


def test_bad_mapped_dir_is_refused(home):
    with pytest.raises(cw_store.CWError) as e:
        cw_repo.resolve({"repo": "a/b"}, {"repos": {"a/b": "relative"}}, FakeRun())
    assert "repos.a/b" in e.value.remedy


def test_missing_clone_is_refused_with_size_and_question(home):
    run = FakeRun()
    with pytest.raises(cw_store.CWError) as e:
        cw_repo.resolve({"repo": "acme/widgets"}, {"trust": {"repos": ["acme/other"]}}, run)
    assert "not cloned" in str(e.value)
    assert "ask the user" in e.value.remedy and "200 MB" in e.value.remedy and "clone: true" in e.value.remedy
    assert run.argvs(["gh", "repo"]) == []


@pytest.mark.parametrize("params,config", [
    ({"repo": "acme/widgets", "clone": True}, {}),
    ({"repo": "Acme/Widgets"}, {"trust": {"repos": ["acme/widgets"]}}),
])
def test_clone_is_blobless_with_gh_credentials_and_no_token(home, params, config):
    run = FakeRun()
    path = cw_repo.resolve(params, config, run)
    final = cw_repo.cache_root() / "acme" / "widgets"
    assert path == str(final) and final.is_dir()
    assert not [p for p in final.parent.iterdir() if p.name.startswith(".tmp-")]
    (clone, kw), = [c for c in run.calls if c[0][:3] == ["gh", "repo", "clone"]]
    assert clone[2:3] == ["clone"] and clone[3] == params["repo"] and clone[5:] == ["--", "--filter=blob:none", "--no-checkout"]
    assert Path(clone[4]).parent == final.parent and Path(clone[4]) != final
    for _, kw in run.calls:
        assert "GH_TOKEN" not in kw["env"] and "GITHUB_TOKEN" not in kw["env"]
    cfg = [a[-1] for a in run.argvs(["git", "-C"]) if "config" in a]
    assert cfg == ["", "!gh auth git-credential"]


def test_failed_clone_leaves_nothing_behind(home):
    run = FakeRun(fail="--no-checkout")
    with pytest.raises(cw_store.CWError):
        cw_repo.resolve({"repo": "acme/widgets", "clone": True}, {}, run)
    assert list(cw_repo.cache_root().rglob("*")) == [cw_repo.cache_root() / "acme"]


@pytest.mark.parametrize("bad", ["acme", "acme/..", "acme/.", "-acme/x", "acme/-x", "a/b/c", "ac me/x",
                                 "acme/x;rm", "acme/x\n", "", None, 5])
def test_bad_names_are_rejected_without_a_call(home, bad):
    run = FakeRun()
    with pytest.raises(cw_store.CWError):
        cw_repo.resolve({"repo": bad, "clone": True}, {"trust": {"repos": [str(bad)]}}, run)
    assert run.calls == []


def test_prune_evicts_only_unreferenced_old_cache_clones(home):
    root = cw_repo.cache_root()
    old, kept, fresh = root / "a" / "old", root / "a" / "kept", root / "b" / "fresh"
    for d in (old, kept, fresh):
        d.mkdir(parents=True)
    ancient = time.time() - 90 * 86400
    for d in (old, kept):
        os.utime(d, (ancient, ancient))
    cw_repo.prune({kept.resolve()}, time.time() - 30 * 86400)
    assert not old.exists()
    assert kept.exists() and fresh.exists()


def test_server_prune_keeps_clone_referenced_by_a_walkthrough(home, monkeypatch):
    root = cw_repo.cache_root()
    unref, ref = root / "a" / "unref", root / "a" / "ref"
    for d in (unref, ref):
        d.mkdir(parents=True)
        os.utime(d, (1, 1))
    wd = cw_store.walkthrough_dir("repo-abcdef", "cmp-00000001", create=True)
    cw_store.write_json(wd / "meta.json", {"id": "cmp-00000001", "repo": str(ref),
                                           "updated_at": cw_store.now_iso()})
    monkeypatch.setenv("CW_PRUNE_DAYS", "30")
    cw_server._prune()
    assert ref.exists() and not unref.exists()


def test_base_ref_with_trailing_newline_is_rejected(home):
    cache = cw_repo.cache_root() / "acme" / "widgets"
    cache.mkdir(parents=True)
    run = FakeRun()
    with pytest.raises(cw_store.CWError):
        cw_repo.resolve({"repo": "acme/widgets", "base": "origin/main\n"}, {}, run)
    assert run.calls == []


def test_clone_and_fetch_run_in_the_cache_root(home):
    run = FakeRun()
    cw_repo.resolve({"repo": "acme/widgets", "clone": True, "pr": 4}, {}, run)
    assert run.calls and all(kw["cwd"] == cw_repo.cache_root() for _, kw in run.calls)


def test_prune_survives_a_clone_that_vanishes_mid_scan(home, monkeypatch):
    root = cw_repo.cache_root()
    gone, old = root / "a" / "gone", root / "a" / "old"
    for d in (gone, old):
        d.mkdir(parents=True)
        os.utime(d, (1, 1))
    real = Path.stat

    def flaky(self, *a, **kw):
        if self == gone:
            raise FileNotFoundError(str(self))
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", flaky)
    cw_repo.prune(set(), time.time())
    assert not old.exists()
