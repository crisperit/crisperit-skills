#!/usr/bin/env python3
"""Shared helpers for the daemon, MCP bridge and run driver: paths under $CODE_WALKTHROUGH_HOME,
atomic JSON read/write, a per-meta.json in-process lock, config loading and the one daemon lock
that keeps a single `cw_server.py serve` alive at a time.

Stdlib only, no network.
"""

import copy
import fcntl
import functools
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from state import _stable_id  # noqa: E402  one owner of the localStorage id

PROTOCOL = 1


@functools.lru_cache(maxsize=1)
def code_id():
    """Tells a daemon from an older install apart from the bridge's own code; a path would break when the same version is installed in two places."""
    root = Path(__file__).resolve().parent.parent
    digest = hashlib.sha256()
    for sub in ("scripts", "assets", "prompts"):
        for f in sorted((root / sub).rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and not f.name.startswith("test_"):
                digest.update(f.relative_to(root).as_posix().encode())
                digest.update(f.read_bytes())
    return digest.hexdigest()[:16]


ID_RE = re.compile(r"^(pr-\d+-[0-9a-f]{4}|cmp-[0-9a-f]{8})$")
KEY_RE = re.compile(r"^[A-Za-z0-9._-]+-[0-9a-f]{6}$")
SLUG_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")  # the slug becomes a filename
REF_OPEN_RE = re.compile(r"(`{3,})ref (.*)")


def replace_ref_fences(text, repl):
    """Replace each closed ```ref fence with repl(header); an unclosed open stays text.

    A line scanner, not a regex: many unclosed opens made the regex quadratic."""
    lines = text.split("\n")
    out = []
    i = 0
    min_failed = None  # a run of this length found no closer, so no longer run can either
    while i < len(lines):
        m = REF_OPEN_RE.fullmatch(lines[i])
        if m and (min_failed is None or len(m.group(1)) < min_failed):
            n = len(m.group(1))
            for j in range(i + 1, len(lines)):
                tail = lines[j].rstrip(" \t")
                if len(tail) >= n and not tail.strip("`"):
                    out.append(repl(m.group(2)))
                    i = j + 1
                    break
            else:
                min_failed = n
                out.append(lines[i])
                i += 1
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def strip_ref_fences(text):
    return replace_ref_fences(text, lambda _header: "")


DEFAULTS = {
    "profiles": {},
    "roles": {},
    "max_concurrency": 4,
    "timeout_s": 300,
    "max_conversation_tokens": 200000,
    "small_diff_lines": 1500,
    "batch_max_lines": 400,
}

_META_LOCKS = {}
_META_LOCKS_GUARD = threading.Lock()

_DIR_LOCKS = {}
_DIR_LOCKS_GUARD = threading.Lock()


class CWError(Exception):
    def __init__(self, message, remedy=None):
        super().__init__(message)
        self.remedy = remedy


def home():
    env = os.environ.get("CODE_WALKTHROUGH_HOME")
    path = Path(env) if env else Path.home() / ".code-walkthrough"
    if not path.exists():
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    return path


def store_root():
    return home() / "w"


def server_json_path():
    return home() / "server.json"


def lock_path():
    return home() / "server.lock"


def log_path():
    return home() / "server.log"


def config_path():
    return home() / "config.json"


def token_path():
    return home() / "token"


TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,}$")


def load_or_create_token():
    """Load the daemon auth token from token_path(), minting and persisting a fresh one if
    missing or malformed. Kept stable across daemon restarts so a page tab opened before a
    restart can still save its drafts."""
    path = token_path()
    try:
        token = path.read_text().strip()
    except OSError:
        token = ""
    if TOKEN_RE.fullmatch(token):
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            os.chmod(path, 0o600)
        return token
    return _mint_token(path)


def _mint_token(path):
    token = secrets.token_urlsafe(32)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_key(toplevel):
    basename = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(toplevel)) or "repo"
    digest = hashlib.sha256(toplevel.encode()).hexdigest()[:6]
    return f"{basename}-{digest}"


def walkthrough_id(target, pr=None, repo=None):
    return _stable_id(repo, pr, target)


def walkthrough_dir(key, wid, create=False):
    if not KEY_RE.fullmatch(key) or not ID_RE.fullmatch(wid):
        raise ValueError(f"bad key or id: {key!r}/{wid!r}")
    base = store_root().resolve()
    resolved = (base / key / wid).resolve()
    if base not in resolved.parents:
        raise ValueError(f"escapes store root: {key!r}/{wid!r}")
    if create:
        resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, obj, mode=None):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise


def update_meta(d, fn):
    """Mutate meta.json under a per-directory lock. In-process only: the daemon is the one
    writer, so this does not need to span processes (ponytail: upgrade to flock if that stops
    being true)."""
    d = Path(d)
    key = str(d)
    with _META_LOCKS_GUARD:
        lock = _META_LOCKS.setdefault(key, threading.Lock())
    with lock:
        meta = read_meta(d) or {}
        fn(meta)
        meta["updated_at"] = now_iso()
        write_json(d / "meta.json", meta)
        return copy.deepcopy(meta)


def read_meta(d):
    return read_json(Path(d) / "meta.json", default=None)


def dir_lock(d):
    """Per-walkthrough-directory in-process lock, module dict keyed by str(d) like
    update_meta's own lock. Held by cw_run._final_build from prepare through render, and by
    phase 5's /post* server handlers -- lives here, not in cw_run, so cw_server tests that
    inject a fake runner still serialize against the same lock."""
    key = str(d)
    with _DIR_LOCKS_GUARD:
        return _DIR_LOCKS.setdefault(key, threading.Lock())


def usage_total(meta):
    prompt_tokens = 0
    completion_tokens = 0
    cost_usd = None
    for role_usage in (meta or {}).get("usage", {}).values():
        prompt_tokens += role_usage.get("prompt_tokens", 0)
        completion_tokens += role_usage.get("completion_tokens", 0)
        role_cost = role_usage.get("cost_usd")
        if role_cost is not None:
            cost_usd = (cost_usd or 0.0) + role_cost
    return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "cost_usd": cost_usd}


def _deep_merge(base, override):
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config():
    path = config_path()
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    data = read_json(path, default=None)
    if data is None:
        raise CWError(f"invalid config: {path}", remedy=f"fix {path}")
    return _deep_merge(DEFAULTS, data)


def role_profile(config, role):
    roles = config.get("roles", {})
    name = roles.get(role)
    if name is None and role == "escalate":
        name = roles.get("prose")
    if name is None and role == "thread":
        name = roles.get("ask")
    if name is None:
        return None
    profile = config.get("profiles", {}).get(name)
    kind = (profile or {}).get("kind", "openai")
    if kind not in ("openai", "claude-code"):
        raise CWError(
            f"profile {name!r}: unknown kind {kind!r}",
            remedy=f"kind is openai or claude-code in {config_path()}",
        )
    need = ("model",) if kind == "claude-code" else ("base_url", "model")
    missing = [key for key in need if not (profile or {}).get(key)]
    if missing:
        raise CWError(
            f"profile {name!r} for role {role!r} has no {'/'.join(missing)}",
            remedy=f"fix profiles.{name} in {config_path()}",
        )
    return {**profile, "name": name}


def acquire_daemon_lock():
    """A lock file holding a dead pid but no live flock is simply unlocked, so the next
    daemon takes it over (8.6): the pid field is diagnostics only, the kernel-held flock is
    the real lock and a killed process always drops it."""
    f = open(lock_path(), "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f
