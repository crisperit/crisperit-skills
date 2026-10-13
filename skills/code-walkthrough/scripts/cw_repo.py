"""Turn walkthrough_start's `repo` (an absolute path or owner/name) into a local checkout path,
cloning into the cache only with the user's consent. `run` is injectable for tests."""

import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import cw_store

NAME_RE = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._][A-Za-z0-9._-]{0,99})$")
REF_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]*$")
CLONE_TIMEOUT_S = 280


def cache_root():
    return cw_store.home() / "repos" / "github.com"


def _env():
    return {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}


def _fail(message, remedy):
    return cw_store.CWError(message, remedy=remedy)


def _size_mb(name, run, env, cwd):
    try:
        out = run(["gh", "api", f"repos/{name}", "--jq", ".size"], env=env, cwd=cwd, capture_output=True,
                  text=True, timeout=30)
        return f"{max(1, round(int(out.stdout.strip()) / 1024))} MB" if out.returncode == 0 else "unknown size"
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return "unknown size"


def _clone(name, final, run, env, cwd):
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.parent / f".tmp-{final.name}-{uuid.uuid4().hex[:8]}"
    try:
        steps = [
            (["gh", "repo", "clone", name, str(tmp), "--", "--filter=blob:none", "--no-checkout"], CLONE_TIMEOUT_S),
            (["git", "-C", str(tmp), "config", "credential.https://github.com.helper", ""], 30),
            (["git", "-C", str(tmp), "config", "--add", "credential.https://github.com.helper",
              "!gh auth git-credential"], 30),
        ]
        for argv, timeout in steps:
            out = run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
            if out.returncode != 0:
                raise _fail(f"clone of {name} failed: {(out.stderr or '').strip()[:300]}",
                            "check gh auth status and that you can read the repo, then retry")
        try:
            os.rename(tmp, final)
        except OSError:
            if not final.is_dir():
                raise
    except (OSError, subprocess.TimeoutExpired) as e:
        raise _fail(f"clone of {name} failed: {e}", "check gh auth status, then retry") from e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fetch(path, params, run, env, cwd):
    specs = []
    pr = params.get("pr")
    if isinstance(pr, int) and not isinstance(pr, bool) and pr > 0:
        specs.append(f"+refs/pull/{pr}/head:refs/cw/pull/{pr}")
    base = params.get("base")
    if isinstance(base, str) and base.startswith("origin/"):
        branch = base[len("origin/"):]
        if not REF_RE.fullmatch(branch) or ".." in branch:
            raise _fail(f"bad base: {base!r}", "pass base as origin/<branch>")
        specs.append(f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    if not specs:
        return
    out = run(["git", "-C", str(path), "fetch", "origin", *specs], env=env, cwd=cwd, capture_output=True,
              text=True, timeout=CLONE_TIMEOUT_S)
    if out.returncode != 0:
        raise _fail(f"git fetch failed in {path}: {(out.stderr or '').strip()[:300]}",
                    "check gh auth status and network, then retry")


def resolve(params, config, run=subprocess.run):
    repo = params.get("repo")
    if isinstance(repo, str) and os.path.isabs(repo):
        return repo
    match = NAME_RE.fullmatch(repo) if isinstance(repo, str) else None
    if not match or match.group(2) in (".", ".."):
        raise _fail(f"bad repo: {repo!r}", "pass an absolute path or owner/name")
    name = repo.lower()
    env = _env()
    cwd = cache_root()
    cwd.mkdir(parents=True, exist_ok=True)
    # gh runs as the default/global account: the per-tree PATH shim does not apply under ~/.code-walkthrough.
    mapped = {k.lower(): v for k, v in (config.get("repos") or {}).items()}.get(name)
    final = cache_root() / match.group(1).lower() / match.group(2).lower()
    if mapped is not None:
        if not isinstance(mapped, str) or not os.path.isabs(mapped) or not Path(mapped).is_dir():
            raise _fail(f"repos.{repo} is not an existing absolute directory", f"fix repos.{repo} in config.json")
        path = Path(mapped)
    elif final.is_dir():
        path = final
    elif params.get("clone") is True or name in {r.lower() for r in (config.get("trust") or {}).get("repos") or []}:
        _clone(repo, final, run, env, cwd)
        path = final
    else:
        raise _fail(
            f"{repo} is not cloned on this machine",
            f"ask the user: clone {repo} (about {_size_mb(repo, run, env, cwd)}, from gh api repos/{repo} --jq .size) "
            f"into {final}? On yes call walkthrough_start again with clone: true; "
            "on no follow references/static.md with gh pr diff",
        )
    _fetch(path, params, run, env, cwd)
    return str(path)


def prune(referenced, cutoff):
    """Remove cache clones (and abandoned temp dirs) that no walkthrough's meta.repo points at
    and that were last touched before cutoff. `referenced` is a set of resolved Paths."""
    root = cache_root()
    if not root.is_dir():
        return
    for owner in root.iterdir():
        for clone in owner.iterdir() if owner.is_dir() else ():
            if clone.resolve() in referenced:
                continue
            try:
                touched = max(p.stat().st_mtime for p in (clone, clone / ".git" / "FETCH_HEAD") if p.exists())
                if touched < cutoff:
                    shutil.rmtree(clone, ignore_errors=True)
            except (OSError, ValueError):
                continue
        if owner.is_dir() and not any(owner.iterdir()):
            owner.rmdir()
