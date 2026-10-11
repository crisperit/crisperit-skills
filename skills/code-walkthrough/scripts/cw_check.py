#!/usr/bin/env python3
"""The code-task worker's run_check tool: an allowlisted test/build command, run without a shell
inside a bubblewrap sandbox (no network, read-only filesystem, only the task worktree and a
scratch dir writable). The allowlist is hygiene; the sandbox is the boundary, and without bwrap
the command is not run.

Stdlib only.
"""

import functools
import json
import os
import resource
import shlex
import signal
import subprocess
import time
from pathlib import Path

MAX_CHECKS = 20
MAX_FILE = 256 * 2**20
TO_MODEL = 4000
RECORDED = 2000
_META = set(";&|<>$`\n\r()\0")
_NODE = ("npm", "pnpm", "yarn")
PREFIXES = (
    [("go", c) for c in ("test", "build", "vet")]
    + [("cargo", c) for c in ("test", "build", "check")]
    + [(m, "test") for m in _NODE] + [(m, "run", c) for m in _NODE for c in ("test", "build", "lint", "typecheck")]
    + [("pytest",), ("python3", "-m", "pytest")]
    + [("make", c) for c in ("test", "build", "check", "lint")]
)
# Toolchains that live under $HOME, which the sandbox otherwise hides.
_TOOLCHAINS = ("go", ".cargo/registry", ".cargo/bin", ".rustup", ".nvm")


def validate(command):
    """argv for an allowed command; ValueError says why not."""
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command is required")
    if _META & set(command):
        raise ValueError("shell syntax is not allowed; give one plain command")
    argv = shlex.split(command)
    if not any(tuple(argv[:len(p)]) == p for p in PREFIXES):
        raise ValueError("not an allowed command; use go/cargo/npm/pnpm/yarn test or build, pytest or make test")
    return argv


def read_checks(path):
    out = []
    try:
        for line in Path(path).read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


@functools.lru_cache(maxsize=1)
def _isolation():
    try:
        probe = ["bwrap", "--unshare-net", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"]
        return subprocess.run(probe, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _modcache():
    gopath = os.environ.get("GOPATH") or str(Path.home() / "go")
    return os.environ.get("GOMODCACHE") or str(Path(gopath.split(os.pathsep)[0]) / "pkg" / "mod")


def _prefix(wt, scratch):
    home = Path.home()
    cmd = ["bwrap", "--unshare-net", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--cap-drop", "ALL",
           "--die-with-parent", "--new-session",
           "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", str(home)]
    # /run holds unix sockets (docker, ssh-agent, dbus) that --unshare-net does not cut off
    hidden = ["/run"] + [p for p in ("/var/run", "/mnt", "/media") if Path(p).is_dir() and not Path(p).is_symlink()]
    for p in hidden:
        cmd += ["--tmpfs", p]
    for rel in _TOOLCHAINS:
        if (home / rel).is_dir():
            cmd += ["--ro-bind", str(home / rel), str(home / rel)]
    cmd += ["--bind", wt, wt, "--bind", scratch, scratch, "--chdir", wt, "--"]
    return cmd


def _env(scratch):
    s = Path(scratch)
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TERM": os.environ.get("TERM", "dumb"), "HOME": str(s / "home"), "TMPDIR": str(s / "tmp"),
        "XDG_CACHE_HOME": str(s / "cache"), "GOCACHE": str(s / "gocache"), "GOPATH": str(s / "gopath"),
        "GOMODCACHE": _modcache(), "GOFLAGS": "-buildvcs=false", "GOPROXY": "off", "GOSUMDB": "off",
        "GOTOOLCHAIN": "local", "CARGO_TARGET_DIR": str(s / "cargo-target"), "CARGO_NET_OFFLINE": "true",
        "npm_config_cache": str(s / "npm"), "npm_config_offline": "true", "PYTHONDONTWRITEBYTECODE": "1",
    }
    if (Path.home() / ".rustup").is_dir():
        env["RUSTUP_HOME"] = str(Path.home() / ".rustup")
    for sub in ("home", "tmp", "cache"):
        (s / sub).mkdir(parents=True, exist_ok=True)
    return env


def run(argv, wt, scratch, timeout):
    """(exit code or None, text). None means the command did not complete."""
    if not _isolation():
        return None, "bwrap is required to run checks and is not available; the command was not run"
    wt, scratch = str(Path(wt).resolve()), str(Path(scratch).resolve())
    Path(scratch).mkdir(parents=True, exist_ok=True)
    log = Path(scratch) / "out.log"
    timed_out = False
    with open(log, "wb") as f:
        proc = subprocess.Popen(_prefix(wt, scratch) + argv, cwd=wt, env=_env(scratch), stdin=subprocess.DEVNULL,
                                stdout=f, stderr=subprocess.STDOUT, start_new_session=True,
                                preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE, MAX_FILE)))
        try:
            code = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            code = proc.wait()
    with open(log, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - TO_MODEL))
        tail = f.read().decode(errors="replace")
    return (None, f"timed out after {timeout}s\n{tail}") if timed_out else (code, tail)


def run_check(command, wt, scratch, log, timeout):
    """(ok, text) for the tool reply; every call but one over the cap is appended to `log`."""
    if len(read_checks(log)) >= MAX_CHECKS:
        return False, f"at most {MAX_CHECKS} checks per task; finish up"
    started = time.time()
    try:
        code, text = run(validate(command), wt, scratch, timeout)
    except ValueError as e:
        code, text = None, f"refused: {e}"
    reply = f"exit {code}\n{text}" if code is not None else text
    ok = code == 0
    record = {"command": str(command)[:300], "exit": code, "ok": ok, "tail": reply[-RECORDED:],
              "seconds": round(time.time() - started, 1)}
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return ok, reply
