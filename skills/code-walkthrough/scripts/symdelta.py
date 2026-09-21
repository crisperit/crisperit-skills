#!/usr/bin/env python3
"""Build the symbol delta graph between two git refs: which functions/methods were added,
removed, or had their call edges change, package-aware.

Usage: python3 symdelta.py --repo <path> --base <ref> --head <ref> > symdelta.json

Two extractor tiers, dispatched by detect_language() on the diff's own changed files: Go
(extractors/go/main.go, go/packages) and an LSP tier shared by TypeScript/TSX, Python and Rust
(extractors/lsp/extract.py, driving typescript-language-server / pyright / rust-analyzer over
callHierarchy). Anything else, or a repo where the chosen language's tool is missing, bails out
early (language: null, with a reason) rather than guessing edges from another language's symbols
or degrading silently. A mixed-language diff picks whichever language touched the most files and
says so in that reason.

Both tiers need a real checkout to resolve symbols against -- go/packages and the LSP servers
can't work off two loose file trees -- so this creates two detached worktrees (base at the
merge-base commit, matching what `base...head` itself diffs; head at its tip) and always cleans
them up, even on failure.

Stdlib only except for shelling out to `git`, the vendored `go` extractor, and (for the LSP tier)
the relevant language server over raw stdio JSON-RPC. See code-walkthrough/SKILL.md for how this fits
the recap flow.
"""

import argparse
import json
import os
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
from links import resolve_base, run_git  # noqa: E402  one owner for git helpers (subprocess + merge-base)
from validate_analysis import is_test_path  # noqa: E402  one owner for test-path classification

EXTRACTOR_DIR = SCRIPT_DIR / "extractors" / "go"


CACHE_NAME = "code-walkthrough"


def _first_existing(path):
    """The nearest ancestor of `path` that exists, `path` itself included."""
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _resolve_cache_dir(environ, home, tmp=None):
    """Where the compiled Go extractor and its module/build caches live.

    XDG_CACHE_HOME wins outright. Otherwise `~/.cache/code-walkthrough`, unless that is not
    writable -- an agent sandbox that allowlists writes usually does not include `~/.cache` --
    in which case the temp dir. Falling back beats failing: the cost is a rebuild per boot
    instead of per machine, and the alternative was the whole symbols section going missing.

    Takes environ/home/tmp as arguments, rather than reading os.environ/Path.home() itself, so
    the fallback chain is testable without patching globals.
    """
    xdg = environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / CACHE_NAME
    home_cache = home / ".cache" / CACHE_NAME
    if os.access(_first_existing(home_cache), os.W_OK):
        return home_cache
    return Path(tmp or tempfile.gettempdir()) / CACHE_NAME


CACHE_DIR = _resolve_cache_dir(os.environ, Path.home())
EXTRACTOR_BIN = CACHE_DIR / "symdelta-go-extractor"

LSP_EXTRACTOR_SCRIPT = SCRIPT_DIR / "extractors" / "lsp" / "extract.py"

# Which language's extractor a changed file routes to. Anything else stays unrecognised: never
# fall back to name matching for a language with no extractor here.
LANG_EXTENSIONS = {
    ".go": "go", ".ts": "typescript", ".tsx": "typescript", ".py": "python", ".rs": "rust",
}

RENAME_BRACE_RE = re.compile(r"^ rename (.*)\{(.*) => (.*)\}(.*) \(\d+%\)$")
RENAME_PLAIN_RE = re.compile(r"^ rename (.+) => (.+) \(\d+%\)$")


ROOT_PKG = "(root)"


def pkg_of(file_path):
    """Directory portion of file_path as a package id; "(root)" for a file with no directory of
    its own (e.g. main.go at the repo root), matching coupling.py's sentinel. The empty string
    posixpath.dirname() gives instead would coerce falsy on the front-end (a root symbol
    rendering with no parent) and let a root symbol's id start with a bare ":"."""
    return posixpath.dirname(file_path) or ROOT_PKG


def _join_brace(pre, mid, suf):
    """pre + mid + suf, collapsing the doubled "/" that appears when mid is empty (a directory
    dropped entirely, e.g. "corelib/ratelimit/{token_bucket => }/token_bucket.go" renaming to
    "corelib/ratelimit/token_bucket.go", not "corelib/ratelimit//token_bucket.go")."""
    return re.sub(r"/{2,}", "/", pre + mid + suf)


def parse_rename_map(summary_text):
    """new-path -> old-path, from `git diff --summary`'s rename lines (both the common-prefix
    brace form and the no-common-prefix plain form)."""
    rename_map = {}
    for line in summary_text.splitlines():
        if not line.startswith(" rename "):
            continue
        m = RENAME_BRACE_RE.match(line)
        if m:
            pre, old, new, suf = m.groups()
            rename_map[_join_brace(pre, new, suf)] = _join_brace(pre, old, suf)
            continue
        m = RENAME_PLAIN_RE.match(line)
        if m:
            old, new = m.groups()
            rename_map[new] = old
    return rename_map


def build_extractor():
    """Compile the vendored extractor into CACHE_DIR, reusing an existing binary that's newer
    than its source. A dedicated, writable module/build cache means the first build (which
    needs network to fetch golang.org/x/tools et al) doesn't fight the sandbox's read-only
    ~/.cache/go-build and module cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sources = [EXTRACTOR_DIR / "main.go", EXTRACTOR_DIR / "go.mod", EXTRACTOR_DIR / "go.sum"]
    if EXTRACTOR_BIN.exists():
        bin_mtime = EXTRACTOR_BIN.stat().st_mtime
        if all(s.stat().st_mtime <= bin_mtime for s in sources if s.exists()):
            return

    env = os.environ.copy()
    env["GOCACHE"] = str(CACHE_DIR / "build-cache")
    env["GOMODCACHE"] = str(CACHE_DIR / "mod-cache")
    try:
        result = subprocess.run(
            ["go", "build", "-o", str(EXTRACTOR_BIN), "."],
            cwd=str(EXTRACTOR_DIR),
            capture_output=True,
            text=True,
            env=env,
            timeout=600,  # generous: covers the cold-cache fetch above; still bounds a build
            # hung on a black-holed module proxy so it can't wedge the script forever
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("building the Go extractor timed out after 600s")
    if result.returncode != 0:
        stderr = result.stderr.strip()
        network_errors = ("dial tcp", "no such host", "network is unreachable", "i/o timeout")
        if any(s in stderr.lower() for s in network_errors):
            raise RuntimeError(
                "building the Go extractor needs network access the first time, to fetch "
                f"golang.org/x/tools and its deps: {stderr}"
            )
        raise RuntimeError(f"building the Go extractor failed: {stderr}")


def run_extractor(worktree_path):
    """Run the compiled extractor against one worktree. A generous 600s timeout guards against
    a hung binary (go/packages loading a pathological module graph) on a huge monorepo -- 3s on
    1255 files today, but never assume that scales linearly."""
    env = os.environ.copy()
    env["GOCACHE"] = str(CACHE_DIR / "build-cache")
    env["GOMODCACHE"] = str(CACHE_DIR / "mod-cache")
    env["GOFLAGS"] = "-mod=mod"
    env["GOPROXY"] = "off"
    env["GOSUMDB"] = "off"
    try:
        result = subprocess.run(
            [str(EXTRACTOR_BIN), str(worktree_path)],
            capture_output=True, text=True, env=env, timeout=600,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Go extractor timed out after 600s on {worktree_path}")
    if result.returncode != 0:
        raise RuntimeError(f"Go extractor failed on {worktree_path}: {result.stderr.strip()}")
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def add_worktree(repo, commit, path):
    result = run_git(repo, ["worktree", "add", "--detach", str(path), commit])
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed for {commit!r}: {result.stderr.strip()}")


def remove_worktree(repo, path):
    run_git(repo, ["worktree", "remove", "--force", str(path)])


def detect_language(repo, base, head):
    """Which language's extractor to run, by majority of changed files (LANG_EXTENSIONS).
    Returns (language, reason): language is None when nothing recognised changed, in which case
    reason explains that; otherwise reason is None unless more than one language's files
    changed, in which case it names the tie-break so a mixed diff's pick is never silent."""
    diff = run_git(repo, ["diff", "--name-only", f"{base}..{head}"])
    if diff.returncode != 0:
        raise RuntimeError(f"git diff failed: {diff.stderr.strip()}")

    counts = Counter()
    for f in diff.stdout.splitlines():
        if not f:
            continue
        lang = LANG_EXTENSIONS.get(posixpath.splitext(f)[1])
        if lang:
            counts[lang] += 1

    if not counts:
        supported = ", ".join(sorted(LANG_EXTENSIONS))
        return None, f"no supported files ({supported}) changed between {base} and {head}"
    if len(counts) == 1:
        return next(iter(counts)), None

    # Sort by count desc, language name asc: deterministic regardless of the diff's own file
    # order, so a tie always resolves to the same winner run to run (Counter.most_common()
    # otherwise breaks ties by insertion order, i.e. by chance).
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    detail = ", ".join(f"{n} {lang}" for lang, n in ranked)
    top_count = ranked[0][1]
    tied = [lang for lang, n in ranked if n == top_count]
    winner = tied[0]
    if len(tied) > 1:
        return winner, (
            f"mixed-language diff ({detail}); tied at {top_count} changed files each, picked "
            f"{winner} (alphabetical, not a real majority)"
        )
    return winner, f"mixed-language diff ({detail}); picked {winner} (most changed files)"


def run_in_process_group(cmd, timeout, what):
    """Run `cmd` as its own session leader and kill the whole process group on timeout, not just
    `cmd` itself. A plain subprocess.run(..., timeout=...) only SIGKILLs the direct child; when
    that child is extract.py, which spawns typescript-language-server as ITS OWN child, killing
    only extract.py orphans tsserver -- extract.py's `finally: client.shutdown()` never gets to
    run, since the SIGKILL is instant. start_new_session=True makes `cmd` a process-group leader
    that tsserver inherits, so os.killpg reaps both."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            # A dead group leader doesn't mean the group is dead -- a grandchild (e.g. tsserver)
            # can outlive it, holding the same stdout/stderr pipes. Kill what we still can.
            proc.kill()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass  # a surviving descendant is still holding the pipes open; discard and move on
        raise RuntimeError(f"{what} timed out after {timeout}s")
    return proc.returncode, out, err


LANGUAGE_SERVER_REMEDY = {
    "typescript": "npm i -g typescript typescript-language-server",
    "python": "npm i -g pyright",
    "rust": "rustup component add rust-analyzer",
}


def check_typescript_tooling(repo, lang="typescript"):
    """Preflight-only invocation of the LSP extractor (no files given): does `lang`'s language
    server exist and advertise callHierarchyProvider? Checked before any worktree gets created,
    so a missing tool fails fast rather than mid-extraction."""
    returncode, _, err = run_in_process_group(
        [sys.executable, str(LSP_EXTRACTOR_SCRIPT), str(repo), "--lang", lang],
        timeout=60, what=f"{lang} language-server capability check",
    )
    if returncode != 0:
        return False, err.strip() or f"{lang} language-server check failed"
    return True, None


def run_ts_extractor(worktree_path, rel_files, lang="typescript"):
    """Shell out to the LSP extractor for one worktree (one ref). `rel_files` go through a temp
    list file rather than argv, so a large changed-file set never risks ARG_MAX."""
    if not rel_files:
        return []
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("\n".join(rel_files))
        files_list_path = fh.name
    try:
        returncode, out, err = run_in_process_group(
            [sys.executable, str(LSP_EXTRACTOR_SCRIPT), str(worktree_path),
             "--lang", lang, "--files-list", files_list_path],
            timeout=600, what=f"LSP extractor on {worktree_path}",
        )
    finally:
        os.unlink(files_list_path)
    if returncode != 0:
        raise RuntimeError(f"LSP extractor failed on {worktree_path}: {err.strip()}")
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def link_node_modules(repo, worktree_path):
    """typescript-language-server needs node_modules for type resolution, but a git worktree
    only contains tracked files and node_modules is gitignored. Symlinking the main checkout's
    copy in (read-only import resolution, never written to) avoids an `npm ci` per worktree --
    only valid when package.json/package-lock.json didn't change between base and head, which
    dependency_files_changed() enforces before this is ever called."""
    src = Path(repo) / "node_modules"
    dst = Path(worktree_path) / "node_modules"
    if src.is_dir() and not dst.exists():
        os.symlink(src, dst, target_is_directory=True)


def _no_worktree_prep(repo, worktree_path):
    pass


# Per-language worktree prep, run once per ref before its extractor call. Empirically checked
# (throwaway repo + bare `git worktree add`, no node_modules/.venv): pyright resolves intra-repo
# imports from the files on disk alone, no .venv symlink needed, so python is a no-op like rust.
# rust: each worktree gets its own `target/`, so rust-analyzer double-indexes the crate graph
# instead of sharing one -- untested optimisation, add CARGO_TARGET_DIR sharing only if that
# proves too slow, since it risks cargo lock contention between the two worktrees.
WORKTREE_PREP = {
    "typescript": link_node_modules,
    "python": _no_worktree_prep,
    "rust": _no_worktree_prep,
}


DEPENDENCY_FILES = {"package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
PYTHON_DEPENDENCY_FILES = {"pyproject.toml", "requirements.txt", "poetry.lock", "Pipfile.lock", "uv.lock"}
RUST_DEPENDENCY_FILES = {"Cargo.toml", "Cargo.lock"}
DEPENDENCY_FILES_BY_LANG = {
    "typescript": DEPENDENCY_FILES, "python": PYTHON_DEPENDENCY_FILES, "rust": RUST_DEPENDENCY_FILES,
}

DEPENDENCY_REASON = {
    "typescript": (
        "package.json or a JS lockfile changed between base and head; node_modules is shared "
        "between both worktrees (see link_node_modules), so the resulting call graph would be "
        "unreliable"
    ),
    "python": (
        "pyproject.toml or a Python lockfile changed between base and head; a resolver change "
        "between base and head would make the resulting call graph unreliable"
    ),
    "rust": (
        "Cargo.toml or Cargo.lock changed between base and head; a resolver change between base "
        "and head would make the resulting call graph unreliable"
    ),
}


def dependency_files_changed(repo, base, head, lang="typescript"):
    """True when a `lang` dependency manifest or lockfile changed anywhere between base and
    head. For typescript, link_node_modules() symlinks the main checkout's node_modules into
    BOTH worktrees, so base and head are resolved against head's dependencies; tsserver returns
    an empty result for a symbol it can't resolve instead of erroring, so a dependency change
    would silently drop real base edges and fabricate spurious new/gone ones. Refusing here is
    cheaper than guessing."""
    diff = run_git(repo, ["diff", "--name-only", f"{base}..{head}"])
    if diff.returncode != 0:
        raise RuntimeError(f"git diff failed: {diff.stderr.strip()}")
    changed_basenames = {posixpath.basename(f) for f in diff.stdout.splitlines() if f}
    return bool(changed_basenames & DEPENDENCY_FILES_BY_LANG[lang])


PACKAGE_JSON_DEP_KEYS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
# peerDependencies deliberately included: npm 7+ installs peers by default, so a missing one is
# a real stale-tree symptom. optionalDependencies deliberately excluded: npm may legitimately
# skip installing one (a platform-specific binary that doesn't match this OS), so a directory
# absent for that reason is not staleness and must not trigger the "npm ci" remedy.
NODE_MODULES_PRESENCE_KEYS = ("dependencies", "devDependencies", "peerDependencies")


def _read_package_json(repo, ref):
    """Parsed package.json at `ref`, or None when it's absent or fails to parse as JSON -- both
    fold into the same conservative "can't compare" signal for callers, since a partially
    committed manifest tells them nothing more than a ref that predates package.json entirely."""
    result = run_git(repo, ["show", f"{ref}:package.json"])
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _union_deps(data, keys):
    """The set of names declared across every section in `keys` of a parsed package.json.
    A set, not a name->spec dict: callers here only ever need membership, and collapsing to a
    set sidesteps the same last-section-wins trap _dep_specs_by_name exists to avoid below --
    there's just nothing here for it to be wrong about, since no spec value is kept."""
    names = set()
    for key in keys:
        names.update((data.get(key) or {}).keys())
    return names


def _dep_specs_by_name(data, keys):
    """name -> the set of every distinct spec string declared for it across `keys`'s sections of
    a parsed package.json. A set, not a dict.update() merge: the latter lets whichever section is
    iterated last silently overwrite an earlier section's spec for the same name, so a package
    declared in two sections with genuinely different specs (dependencies bumped, devDependencies
    left stale behind it, say) could read as unchanged purely because of section order. A name
    repeated across sections with the identical spec still collapses to one set element, so that
    case reads as unchanged rather than as some kind of ambiguity."""
    specs = defaultdict(set)
    for key in keys:
        for name, spec in (data.get(key) or {}).items():
            specs[name].add(spec)
    return specs


def ts_dependency_incompatible(repo, base, head):
    """Whether base and head declare TypeScript dependencies incompatibly enough that
    link_node_modules()'s single node_modules, shared between both worktrees, can't stand in for
    both. Narrower than the blunt "did a manifest file change" check python/rust still use:
    additions-only and lockfile-only churn never touch what base source already resolved, and the
    blunt check cost one real PR its whole TypeScript symbol graph over 25 unrelated files for two
    package additions. What actually breaks resolution is base source importing something the
    shared (head-side) node_modules no longer matches -- a package head removed, or a spec head
    changed.

    Decided from the declared specs in package.json, not from which files changed, so a
    lockfile-only edit or an unchanged manifest never trips it. Bails when package.json is
    missing or unparseable at either ref: there is nothing to compare, and guessing would be
    worse than refusing.

    Known ceiling, not checked here: an added devDependency can still shift base-side resolution
    two ways this never sees -- ambient global type declarations (`@types/jest` declaring
    `describe`/`it`/`expect` via `declare global`) change what's visible when analysing base too,
    and npm's hoisting can re-resolve a shared transitive package to a different version that
    never appears in package.json at all. Both are properties of node_modules being shared
    between worktrees, not of this function; the fix is an install per worktree, and that's the
    cost link_node_modules exists to avoid paying on every run."""
    base_data, head_data = _read_package_json(repo, base), _read_package_json(repo, head)
    if base_data is None or head_data is None:
        return True, (
            "package.json is missing or unparseable at base or head, so declared TypeScript "
            "dependencies can't be compared"
        )

    base_specs = _dep_specs_by_name(base_data, PACKAGE_JSON_DEP_KEYS)
    head_specs = _dep_specs_by_name(head_data, PACKAGE_JSON_DEP_KEYS)
    removed = sorted(name for name in base_specs if name not in head_specs)
    changed = sorted(
        name for name in base_specs if name in head_specs and head_specs[name] != base_specs[name]
    )
    if not removed and not changed:
        return False, None

    parts = []
    if removed:
        parts.append(f"removed: {', '.join(removed)}")
    if changed:
        parts.append(f"version changed: {', '.join(changed)}")
    return True, (
        f"package.json dependency incompatibility between base and head ({'; '.join(parts)}); "
        "node_modules is shared between both worktrees (see link_node_modules), so the "
        "resulting call graph would be unreliable"
    )


def _missing_node_modules_packages(repo, names):
    """Names with no directory under <repo>/node_modules. A scoped name (e.g. "@scope/name")
    already joins correctly -- Path("node_modules") / "@scope/name" is just a nested directory --
    so no special casing is needed for it."""
    node_modules = Path(repo) / "node_modules"
    return sorted(name for name in names if not (node_modules / name).is_dir())


def _read_package_lock(repo, head):
    """Parsed package-lock.json at `head`, or None when it's absent or unparseable -- the
    staleness check below degrades to presence-only in that case rather than guessing, since a
    yarn or pnpm repo has no npm lockfile to compare against at all, and that must not start
    failing this preflight over a check it was never able to perform."""
    result = run_git(repo, ["show", f"{head}:package-lock.json"])
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _locked_version(lock_data, name):
    """The version package-lock.json resolved `name` to, or None when it has no entry for that
    name. Tries lockfileVersion 2/3's flat "packages" map first (keyed "node_modules/<name>", or
    "node_modules/@scope/name" for a scoped package), then falls back to v1's top-level
    "dependencies" map, so either lockfile shape gets a real comparison rather than one silently
    reading as "no entry" on the other's format."""
    packages = lock_data.get("packages")
    if isinstance(packages, dict):
        entry = packages.get(f"node_modules/{name}")
        if isinstance(entry, dict) and "version" in entry:
            return entry["version"]
    deps_v1 = lock_data.get("dependencies")
    if isinstance(deps_v1, dict):
        entry = deps_v1.get(name)
        if isinstance(entry, dict) and "version" in entry:
            return entry["version"]
    return None


def _installed_version(repo, name):
    """The "version" field of node_modules/<name>/package.json, or None when that file is
    missing or unparseable -- treated the same as "no entry" by the caller, not as a mismatch,
    since there's nothing here to compare against."""
    pkg_path = Path(repo) / "node_modules" / name / "package.json"
    try:
        data = json.loads(pkg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data.get("version")


def _stale_node_modules_packages(repo, head, present):
    """Which names in `present` (already known to have a node_modules directory) have an
    installed version that disagrees with what package-lock.json resolved head to -- the case a
    bare directory-exists check can't see:
    node_modules/foo left at 1.0.0 satisfies presence for a newly declared foo: ^2.0.0, and the
    graph gets built against the wrong types. Exact-string comparison of two already-resolved
    versions, never semver range resolution -- that's a package manager's job, not this
    preflight's. Returns [] outright when the lockfile itself is unusable (see
    _read_package_lock); a package with no lockfile entry is skipped individually rather than
    counted as either stale or clean, since there's nothing to compare it against either."""
    lock_data = _read_package_lock(repo, head)
    if lock_data is None:
        return []
    stale = []
    for name in sorted(present):
        locked = _locked_version(lock_data, name)
        if locked is None:
            continue
        installed = _installed_version(repo, name)
        if installed is not None and installed != locked:
            stale.append(name)
    return stale


def check_node_modules_coverage(repo, head):
    """Preflight, run before any worktree is created: the main checkout's node_modules --
    link_node_modules() symlinks it in rather than running `npm ci` per worktree -- has to
    actually contain, and match, what head declares, or edges into whatever it's missing or stale
    silently vanish or resolve against the wrong types, with no warning at all. Measured case:
    head had just added @restatedev/restate-sdk, node_modules didn't have it yet, and every
    head-side edge into that package disappeared from the graph with nothing to say why."""
    data = _read_package_json(repo, head)
    names = _union_deps(data, NODE_MODULES_PRESENCE_KEYS) if data is not None else set()
    missing = _missing_node_modules_packages(repo, names)
    present = names - set(missing)
    stale = _stale_node_modules_packages(repo, head, present)

    problems = sorted(set(missing) | set(stale))
    if not problems:
        return True, None
    shown = problems[:5]
    names_str = ", ".join(shown)
    if len(problems) > 5:
        names_str += f", and {len(problems) - 5} more"
    return False, f"node_modules is missing or stale for {names_str}"


def ts_diff_entries(repo, base, head):
    """(status, old_path, new_path) triples from `git diff --name-status -M`; old == new for a
    plain add/modify/delete, so callers never need to branch on shape."""
    result = run_git(repo, ["diff", "--name-status", "-M", f"{base}..{head}"])
    if result.returncode != 0:
        raise RuntimeError(f"git diff --name-status failed: {result.stderr.strip()}")
    entries = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status[0] == "R" and len(parts) == 3:
            entries.append((status, parts[1], parts[2]))
        elif len(parts) == 2:
            entries.append((status, parts[1], parts[1]))
    return entries


def ts_files_by_side(entries, extensions=(".ts", ".tsx")):
    """Which repo-relative paths matching `extensions` exist at base and at head: an added file
    has nothing to query at base, a deleted file has nothing to query at head -- matching what a
    worktree checked out at that ref actually contains on disk."""
    base_files, head_files = [], []
    for status, old, new in entries:
        old_is_ts = posixpath.splitext(old)[1] in extensions
        new_is_ts = posixpath.splitext(new)[1] in extensions
        if status[0] != "A" and old_is_ts:
            base_files.append(old)
        if status[0] != "D" and new_is_ts:
            head_files.append(new)
    return base_files, head_files


def edge_tuple(d):
    return (d["FromFile"], d["FromSym"], d["ToFile"], d["ToSym"])


def compute_new_gone(base_edges, head_edges, rename_map):
    """Step (a), rename pairing: canonicalise head-side paths back to their pre-rename name only
    to decide whether an edge already existed pre-rename. Canonicalisation must stay internal to
    that comparison -- it must never leak into the emitted edge's identity, or a symbol carried
    over by a rename ends up filed under a package that no longer exists at head. new = head -
    base, gone = base - head; `new` is always stored under its real head path, `gone` under its
    real base path (the only place that symbol still exists)."""

    def canon(path):
        return rename_map.get(path, path)

    base_set = {edge_tuple(e) for e in base_edges}

    new, seen = [], set()
    for e in head_edges:
        canon_t = (canon(e["FromFile"]), e["FromSym"], canon(e["ToFile"]), e["ToSym"])
        if canon_t not in base_set and canon_t not in seen:
            new.append(list(edge_tuple(e)))
            seen.add(canon_t)

    head_canon_set = {
        (canon(e["FromFile"]), e["FromSym"], canon(e["ToFile"]), e["ToSym"]) for e in head_edges
    }
    gone, seen = [], set()
    for e in base_edges:
        t = edge_tuple(e)
        if t not in head_canon_set and t not in seen:
            gone.append(list(t))
            seen.add(t)

    return new, gone


def _drop_test_edges(edges):
    """Drop any edge touching a test file, on either end, before it ever reaches move_collapse
    or build_graph: a test-only edge must not count toward the moved tally, become a node, or
    leave any other trace, not just be hidden behind a toggle."""
    return [e for e in edges if not is_test_path(e[0]) and not is_test_path(e[2])]


def _collapse_anchored(new, gone, anchor_idx, moved_idx, tally):
    """Pair each `new` entry with a `gone` entry that shares (FromSym, ToSym) *and* is an exact
    match at `anchor_idx` -- not merely a same-named coincidence. Bare-name matching alone would
    let two unrelated edges that happen to share a (FromSym, ToSym) pair (two different `Config`
    types, `String` on two different receivers, each calling something also named the same)
    collapse against each other. Requiring the non-moved side to match exactly rules that out: a
    pair only collapses when one full end of the edge is provably the same symbol.

    Returns the surviving new/gone lists (order preserved), a moved-package tally when `tally` is
    given (keyed on the side at `moved_idx`), and (base_sid, head_sid, head_file) triples for
    every collapsed match whose `moved_idx` side actually changed package -- symbol-identity
    evidence consumed by build_graph to merge a moved-but-changed symbol into one node, even
    though the collapsed edge itself carries no further news and is dropped here.
    """
    gone_by_key = defaultdict(list)
    for i, e in enumerate(gone):
        gone_by_key[(e[1], e[3], e[anchor_idx])].append(i)

    used_new, used_gone = set(), set()
    move_pairs = []
    for i, ne in enumerate(new):
        bucket = gone_by_key.get((ne[1], ne[3], ne[anchor_idx]))
        if not bucket:
            continue
        gi = bucket.pop()
        ge = gone[gi]
        used_new.add(i)
        used_gone.add(gi)
        base_pkg, head_pkg = pkg_of(ge[moved_idx]), pkg_of(ne[moved_idx])
        if base_pkg != head_pkg:
            if tally is not None:
                tally[(base_pkg, head_pkg)] += 1
            move_pairs.append(
                (
                    sym_id(ge[moved_idx], ge[moved_idx + 1]),
                    sym_id(ne[moved_idx], ne[moved_idx + 1]),
                    ne[moved_idx],
                )
            )

    remaining_new = [e for i, e in enumerate(new) if i not in used_new]
    remaining_gone = [e for i, e in enumerate(gone) if i not in used_gone]
    return remaining_new, remaining_gone, move_pairs


def move_collapse(new, gone):
    """Step (b): an edge present in both new and gone under the same (FromSym, ToSym) pair, whose
    *other* end matches exactly, is a symbol that moved -- not a real call-graph change.

    Two passes, in order: a moved caller (same callee, From side's package differs) is collapsed
    and tallied into moved[]; a moved callee (same caller, To side's package differs) is collapsed
    too, but only caller moves are surfaced in moved[]. An edge whose (FromSym, ToSym) pair
    matches something in the other list but where *neither* end matches exactly is not a provable
    move -- collapsing it on name alone is exactly the cross-match this guards against -- so it is
    left in place as a real change.

    Both passes also hand back which symbol moved from which base package to which head package
    (`moved_symbols`), regardless of whether that pass tallies into moved[] -- a callee move is
    just as much proof of identity as a caller move, it's just not summarised in the rollup.
    build_graph consumes moved_symbols to merge that symbol's base/head nodes into one.
    """
    moved_tally = Counter()
    new, gone, pairs_a = _collapse_anchored(new, gone, anchor_idx=2, moved_idx=0, tally=moved_tally)
    final_new, final_gone, pairs_b = _collapse_anchored(new, gone, anchor_idx=0, moved_idx=2, tally=None)

    moved = [{"from": k[0], "to": k[1], "callsites": v} for k, v in moved_tally.items()]
    moved.sort(key=lambda m: (-m["callsites"], m["from"]))
    return final_new, final_gone, moved, pairs_a + pairs_b


def sym_id(file_path, sym):
    """`{package}:{symbol}` identity. A real package/module (a subdirectory) already groups
    related files, so bare-name collisions there are either a language guarantee (Go forbids
    two same-named top-level declarations in one package) or an accepted display simplification.
    The repo root has no such grouping -- every unrelated top-level file shares the ROOT_PKG
    sentinel -- so two root files declaring the same symbol name would otherwise collapse onto
    one id; qualify with the file's own stem in that case only."""
    pkg = pkg_of(file_path)
    if pkg == ROOT_PKG:
        stem = posixpath.splitext(posixpath.basename(file_path))[0]
        return f"{pkg}:{stem}.{sym}"
    return f"{pkg}:{sym}"


def _case_insensitive_new_match(pkg, name, sym_new):
    """The one id in `sym_new` under `pkg` whose bare name matches `name` case-insensitively but
    not exactly, or None when that isn't unique -- two such candidates (or none) is exactly the
    ambiguity `resolve_symbol_merges` refuses to guess through, leaving its caller to fall back
    to an exact-match-only merge instead."""
    exact = f"{pkg}:{name}"
    candidates = [
        sid for sid in sym_new
        if sid != exact
        and sid.rsplit(":", 1)[0] == pkg
        and sid.rsplit(":", 1)[1].lower() == name.lower()
    ]
    return candidates[0] if len(candidates) == 1 else None


def resolve_symbol_merges(sym_gone, sym_new, gone_file, gone_name, rename_map, moved_pairs):
    """Pair a gone-side symbol id with its new-side id when both are really the same symbol that
    moved package, so build_graph can fold them into a single `changed` node instead of drawing
    one `gone` and one `new`. Returns (merge_map, head_file_by_sid, renamed_from) -- the second
    gives a merge target with no residual edge of its own a file to attach, since it otherwise
    wouldn't have one; the third maps a head id to its pre-merge bare name, for every merge where
    that name actually changed (a Go export-capitalisation change is the case that matters here:
    `incrementChecksTotal` -> `IncrementChecksTotal`), so build_graph can carry it onto the node.

    `gone_name` carries each gone id's real bare symbol name. sym_id() does not encode a
    ROOT_PKG symbol as a plain "{pkg}:{sym}" -- it's "{pkg}:{stem}.{sym}" -- so re-deriving the
    name via `gone_sid.rsplit(":", 1)[1]` would treat "main.Run" as the name itself and build a
    head-side id nothing else ever produces.

    Three sources of evidence, tried in order, first match wins. (1) and (2) apply unconditionally
    -- a symbol that truly moved may have absorbed every one of its edges into a pure-move
    collapse, leaving nothing behind at head *except* its existence, which still earns it one
    node, not zero. (3) is circumstantial, so it requires more:

    1. `moved_pairs`: base/head ids move_collapse already proved identical via an exact edge
       match (a shared (FromSym, ToSym) with the other end also matching).
    2. the git rename map, followed per FILE: `gone_file` is the actual base file a gone
       symbol's own edge came from, so looking up that exact file's rename target picks the one
       destination that really carries it, even when the old directory split across several new
       ones (corelib/ratelimit/internal did, here) -- package-level matching alone can't tell which
       destination is whose. When the exact bare name isn't live at that destination,
       `_case_insensitive_new_match` gets one unambiguous shot at a same-name-but-cased-differently
       id before this falls back to building the (possibly not-yet-live) exact-name id regardless.
    3. a package-level fallback for a file git's rename heuristic didn't recognise on its own
       (its content changed too much, or landed inside an already-existing file) even though its
       directory clearly moved: every OTHER renamed file agrees on a destination package. A bare
       name could coincidentally exist at more than one destination, so this one requires the
       candidate to already be a live `new` id before merging into it -- the same case-insensitive
       fallback applies here too, still gated on the destination package(s) agreed by other renames,
       and still refusing to guess when more than one candidate matches across them.
    """
    merge_map, head_file_by_sid = {}, {}
    for base_sid, head_sid, head_file in moved_pairs or ():
        if base_sid != head_sid and base_sid in sym_gone:
            merge_map[base_sid] = head_sid
            head_file_by_sid[head_sid] = head_file

    inverse_rename = {old_file: new_file for new_file, old_file in (rename_map or {}).items()}
    for gone_sid in sym_gone:
        if gone_sid in merge_map:
            continue
        new_file = inverse_rename.get(gone_file.get(gone_sid))
        pkg = gone_sid.rsplit(":", 1)[0]
        if new_file is not None and pkg_of(new_file) != pkg:
            name = gone_name[gone_sid]
            head_sid = sym_id(new_file, name)
            if head_sid not in sym_new:
                ci_match = _case_insensitive_new_match(pkg_of(new_file), name, sym_new)
                if ci_match is not None:
                    head_sid = ci_match
            merge_map[gone_sid] = head_sid
            head_file_by_sid[head_sid] = new_file

    old_to_new_pkgs = defaultdict(set)
    for new_file, old_file in (rename_map or {}).items():
        old_pkg, new_pkg = pkg_of(old_file), pkg_of(new_file)
        if old_pkg != new_pkg:
            old_to_new_pkgs[old_pkg].add(new_pkg)

    for gone_sid in sym_gone:
        if gone_sid in merge_map:
            continue
        pkg = gone_sid.rsplit(":", 1)[0]
        sym = gone_name[gone_sid]
        new_pkgs = sorted(old_to_new_pkgs.get(pkg, ()))
        matched = None
        for new_pkg in new_pkgs:
            candidate = f"{new_pkg}:{sym}"
            if candidate in sym_new:
                matched = candidate
                break
        if matched is None:
            ci_matches = []
            for new_pkg in new_pkgs:
                ci_match = _case_insensitive_new_match(new_pkg, sym, sym_new)
                if ci_match is not None:
                    ci_matches.append(ci_match)
            if len(ci_matches) == 1:
                matched = ci_matches[0]
        if matched is not None:
            merge_map[gone_sid] = matched

    renamed_from = {}
    for base_sid, head_sid in merge_map.items():
        old_name = base_sid.rsplit(":", 1)[1]
        new_name = head_sid.rsplit(":", 1)[1]
        if old_name != new_name:
            renamed_from[head_sid] = old_name

    return merge_map, head_file_by_sid, renamed_from


def _compress_pkg_chains(nodes):
    """Fold a package node into its child when the parent exists only to hold that one child --
    a pass-through box a reader gains nothing from (e.g. "auction" > "model" > "request" holding
    two symbols becomes one "auction/model/request" box). A node qualifies only when it has
    exactly one child AND that child is itself a package: a lone symbol child fails the kind
    check, and any symbol child at all pushes the child count to 2+ once combined with a package
    sibling, so both "multiple children" and "has a symbol child" are excluded by the same test.

    Node ids never change -- the deepest node in a folded chain already carries the full path as
    its id, so dropping its now-redundant ancestors is enough; symbols keep pointing at it by an
    unchanged `parent`. Only the ancestor pkg nodes disappear, each survivor's `parent` is
    re-pointed past them to the nearest surviving ancestor, and `label`/`depth` are rebuilt from
    that final parent chain (an id-minus-parent-prefix slice recovers the joined label for free,
    compressed or not)."""
    node_by_id = {n["id"]: n for n in nodes}
    children = defaultdict(list)
    for n in nodes:
        if n["parent"] is not None:
            children[n["parent"]].append(n["id"])

    removed = {
        n["id"] for n in nodes
        if n["kind"] == "pkg"
        and len(children.get(n["id"], ())) == 1
        and node_by_id[children[n["id"]][0]]["kind"] == "pkg"
    }

    def true_parent(pid):
        while pid is not None and pid in removed:
            pid = node_by_id[pid]["parent"]
        return pid

    kept = [n for n in nodes if n["id"] not in removed]
    for n in kept:
        if n["kind"] == "pkg":
            n["parent"] = true_parent(n["parent"])

    depth_cache = {}

    def pkg_depth(nid):
        if nid not in depth_cache:
            parent = node_by_id[nid]["parent"]
            depth_cache[nid] = 0 if parent is None else pkg_depth(parent) + 1
        return depth_cache[nid]

    for n in kept:
        if n["kind"] == "pkg":
            n["depth"] = pkg_depth(n["id"])
            n["label"] = n["id"][len(n["parent"]) + 1:] if n["parent"] is not None else n["id"]
        else:
            n["depth"] = pkg_depth(n["parent"]) + 1

    return kept


def build_graph(final_new, final_gone, rename_map=None, moved_pairs=None):
    sym_new, sym_gone, sym_file, gone_file, gone_name = set(), set(), {}, {}, {}
    for edges, bucket in ((final_new, sym_new), (final_gone, sym_gone)):
        for ff, fs, tf, ts in edges:
            for f, s in ((ff, fs), (tf, ts)):
                sid = sym_id(f, s)
                bucket.add(sid)
                sym_file.setdefault(sid, f)
                if bucket is sym_gone:
                    gone_file.setdefault(sid, f)
                    gone_name.setdefault(sid, s)

    merge_map, head_file_by_sid, renamed_from = resolve_symbol_merges(
        sym_gone, sym_new, gone_file, gone_name, rename_map, moved_pairs
    )
    merged_targets = set(merge_map.values())
    # a merge target may have been fully absorbed out of final_new (no residual edge of its own),
    # so it never got a sym_file entry above -- backfill from the file the merge itself resolved
    for head_sid in merged_targets:
        sym_file.setdefault(head_sid, head_file_by_sid.get(head_sid))

    def resolve(sid):
        return merge_map.get(sid, sid)

    def sym_state(sid):
        if sid in merged_targets or (sid in sym_new and sid in sym_gone):
            return "changed"
        return "new" if sid in sym_new else "gone"

    nodes, node_ids = [], set()

    def parent_of(pkg_path):
        return pkg_path.rsplit("/", 1)[0] if "/" in pkg_path else None

    def add_pkg(pkg_path):
        if pkg_path is None or pkg_path in node_ids:
            return
        node_ids.add(pkg_path)
        nodes.append(
            {
                "id": pkg_path,
                "label": pkg_path.rsplit("/", 1)[-1],
                "kind": "pkg",
                "parent": parent_of(pkg_path),
                "depth": pkg_path.count("/"),
            }
        )
        add_pkg(parent_of(pkg_path))

    def add_symbol(sid):
        if sid in node_ids:
            return
        node_ids.add(sid)
        pkg_path, label = sid.rsplit(":", 1)
        add_pkg(pkg_path)
        file_path = sym_file[sid]
        node = {
            "id": sid,
            "label": label,
            "kind": "symbol",
            "parent": pkg_path,
            "depth": pkg_path.count("/") + 1,
            "state": sym_state(sid),
            "file": file_path,
        }
        if sid in renamed_from:
            node["was"] = renamed_from[sid]
        nodes.append(node)

    def make_edges(final_edges, id_prefix, state):
        result = []
        for i, (ff, fs, tf, ts) in enumerate(final_edges):
            sid, tid = resolve(sym_id(ff, fs)), resolve(sym_id(tf, ts))
            add_symbol(sid)
            add_symbol(tid)
            result.append(
                {
                    "id": f"{id_prefix}{i}",
                    "source": sid,
                    "target": tid,
                    "state": state,
                }
            )
        return result

    edges = make_edges(final_new, "e", "new") + make_edges(final_gone, "g", "gone")

    nodes = _compress_pkg_chains(nodes)

    presets = {"packages": [], "symbols": [n["id"] for n in nodes if n["kind"] == "pkg"]}
    counts = {
        "symbols": sum(1 for n in nodes if n["kind"] == "symbol"),
        "packages": sum(1 for n in nodes if n["kind"] == "pkg"),
        "edges_new": len(final_new),
        "edges_gone": len(final_gone),
    }
    return nodes, edges, presets, counts


def _rename_map(repo, base, head):
    summary = run_git(repo, ["diff", "--summary", f"{base}..{head}"])
    if summary.returncode != 0:
        raise RuntimeError(f"git diff --summary failed: {summary.stderr.strip()}")
    return parse_rename_map(summary.stdout)


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def is_empty_base(repo, base):
    """Whether `base` is the empty baseline explain mode diffs against (see SKILL.md). Matched on
    the tree, not the commit: the baseline is an orphan commit wrapping the empty tree, so its own
    sha differs per session while the tree is a constant."""
    out = run_git(repo, ["rev-parse", f"{base}^{{tree}}"])
    return out.returncode == 0 and out.stdout.strip() == EMPTY_TREE


def _run_in_worktrees(repo, base, head, extract_base, extract_head):
    """Create detached worktrees at base and at head, run the given per-ref callables, and
    always clean up -- shared by both language tiers, which differ only in what they run
    inside each worktree. `base` arrives already resolved to the merge-base commit (analyse()
    does that once, up front).

    The empty baseline gets no worktree and no extractor run: a checkout of the empty tree has
    no go.mod, no package.json and no source at all, so every extractor fails on it, which is
    what used to cost explain mode its whole symbols section. Its edge set is empty by
    definition, so there is nothing to fail at."""
    empty_base = is_empty_base(repo, base)
    tmpdir = tempfile.mkdtemp(prefix="symdelta-")
    base_wt, head_wt = Path(tmpdir) / "base", Path(tmpdir) / "head"
    try:
        if not empty_base:
            add_worktree(repo, base, base_wt)
        add_worktree(repo, head, head_wt)
        return ([] if empty_base else extract_base(base_wt)), extract_head(head_wt)
    finally:
        if not empty_base:
            remove_worktree(repo, base_wt)
        remove_worktree(repo, head_wt)
        run_git(repo, ["worktree", "prune"])
        shutil.rmtree(tmpdir, ignore_errors=True)


def _finish(base, head, base_edges, head_edges, rename_map, language, resolver):
    new, gone = compute_new_gone(base_edges, head_edges, rename_map)
    new, gone = _drop_test_edges(new), _drop_test_edges(gone)
    final_new, final_gone, moved, moved_pairs = move_collapse(new, gone)
    nodes, edges, presets, counts = build_graph(final_new, final_gone, rename_map, moved_pairs)
    return {
        "target": f"{base}...{head}",
        "language": language,
        "resolver": resolver,
        "nodes": nodes,
        "edges": edges,
        "moved": moved,
        "presets": presets,
        "counts": counts,
    }


def analyse_go(repo, base, head):
    build_extractor()
    rename_map = _rename_map(repo, base, head)
    base_edges, head_edges = _run_in_worktrees(repo, base, head, run_extractor, run_extractor)
    return _finish(base, head, base_edges, head_edges, rename_map, "go", "go/packages")


def analyse_lsp(repo, base, head, lang):
    """Shared by the three LSP-tier languages (typescript, python, rust); they differ only in
    their file extensions, dependency manifests, worktree prep and which server extract.py
    spawns via --lang."""
    # Against the empty baseline every manifest reads as added, but there is no before side
    # for a dependency change to make incomparable, so the refusals below do not apply.
    if not is_empty_base(repo, base):
        if lang == "typescript":
            # Narrower than python/rust's blunt dependency_files_changed: see
            # ts_dependency_incompatible for why file-level detection over-refuses here.
            incompatible, reason = ts_dependency_incompatible(repo, base, head)
            if incompatible:
                return {"language": None, "reason": reason}
        elif dependency_files_changed(repo, base, head, lang):
            return {"language": None, "reason": DEPENDENCY_REASON[lang]}

    if lang == "typescript":
        ok, reason = check_node_modules_coverage(repo, head)
        if not ok:
            return {"language": None, "reason": reason, "remedy": "npm ci"}

    ok, reason = check_typescript_tooling(repo, lang)
    if not ok:
        return {"language": None, "reason": reason, "remedy": LANGUAGE_SERVER_REMEDY[lang]}

    rename_map = _rename_map(repo, base, head)
    entries = ts_diff_entries(repo, base, head)
    extensions = tuple(ext for ext, l in LANG_EXTENSIONS.items() if l == lang)
    base_files, head_files = ts_files_by_side(entries, extensions)

    def extractor_for(rel_files):
        def run(worktree_path):
            WORKTREE_PREP[lang](repo, worktree_path)
            return run_ts_extractor(worktree_path, rel_files, lang)
        return run

    base_edges, head_edges = _run_in_worktrees(
        repo, base, head, extractor_for(base_files), extractor_for(head_files)
    )
    return _finish(base, head, base_edges, head_edges, rename_map, lang, "lsp/callHierarchy")


def analyse_typescript(repo, base, head):
    return analyse_lsp(repo, base, head, "typescript")


def analyse_python(repo, base, head):
    return analyse_lsp(repo, base, head, "python")


def analyse_rust(repo, base, head):
    return analyse_lsp(repo, base, head, "rust")


ANALYSERS = {
    "go": analyse_go, "typescript": analyse_typescript, "python": analyse_python, "rust": analyse_rust,
}


def analyse(repo, base, head):
    for ref in (base, head):
        verify = run_git(repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
        if verify.returncode != 0:
            raise RuntimeError(f"bad ref: {ref!r}")

    # Resolve once, up front: three-dot needs a merge base, which an orphan baseline (the
    # empty-tree commit "explain a feature" mode diffs against) doesn't have. resolve_base falls
    # back to base unchanged when merge-base fails, so two-dot on the resolved value matches the
    # old three-dot behaviour everywhere else too (git diff A...B == git diff $(merge-base A B) B).
    base = resolve_base(repo, base, head)

    lang, reason = detect_language(repo, base, head)
    if lang is None:
        return {"language": None, "reason": reason}

    result = ANALYSERS[lang](repo, base, head)
    if reason and result.get("language"):
        result["reason"] = reason
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()

    try:
        result = analyse(args.repo, args.base, args.head)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
