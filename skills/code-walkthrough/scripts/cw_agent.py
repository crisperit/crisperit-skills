"""Trust and the shared edit worktree for the thread agent's Full mode. Imported by cw_server
and, lazily, by cw_run's rebuild: cw_task and cw_ask must never import it back."""

import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import cw_ask
import cw_store
import cw_task
import links
import state


def repo_name(meta, d=None):
    """owner/name on github.com: gh_repo for a PR, else read from the origin remote. With `d` the
    answer is cached in meta so a status poll does not spawn git each time."""
    if meta.get("gh_repo"):
        return meta["gh_repo"]
    if "origin_repo" in meta:
        return meta["origin_repo"] or None
    if not meta.get("repo"):
        return None
    url = links.repo_web_url(meta["repo"])
    name = state._repo_from_links({"repo_url": url}) if urlparse(url).hostname == "github.com" else None
    meta["origin_repo"] = name or ""
    if d is not None:
        cw_store.update_meta(d, lambda m: m.update(origin_repo=name or ""))
    return name


def _lowered(value):
    return {x.lower() for x in value if isinstance(x, str)} if isinstance(value, list) else set()


def trusted_by(meta, config, d=None):
    """Why Full is available for this walkthrough's repo: "own_pr", "repo", "org", or None. Never
    calls out; own_pr is the value cw_run.run cached in meta."""
    if meta.get("sibling_of"):
        return None
    if meta.get("own_pr") is True:
        return "own_pr"
    cfg = config.get("trust")
    cfg = cfg if isinstance(cfg, dict) else {}
    repos, orgs = _lowered(cfg.get("repos")), _lowered(cfg.get("orgs"))
    if not repos and not orgs:
        return None
    repo = (repo_name(meta, d) or "").lower()
    if not repo:
        return None
    if repo in repos:
        return "repo"
    return "org" if repo.split("/", 1)[0] in orgs else None


def trust(meta, config, d=None):
    return trusted_by(meta, config, d) is not None


FILES_CAP = 200


class BranchChanged(RuntimeError):
    pass


class CWBusy(cw_store.CWError):
    """The walkthrough's edit lock is held by a Full turn or a discard."""


def agent_stale(meta):
    """The agent branch was last based on a head the walkthrough has since moved past."""
    agent = meta.get("agent")
    return bool(agent) and agent.get("base") != meta.get("head")


def has_edits(d, meta):
    """The agent's tree differs from what it was based on. A stale branch is compared with its own
    base: the PR's new commits are not agent edits. A git failure counts as edits (keep)."""
    agent = meta.get("agent")
    if not agent:
        return False
    ref = agent.get("base") or meta["head"]
    return cw_task._git(meta["repo"], ["diff", "--quiet", ref, agent["sha"]]).returncode != 0


def is_dirty(d):
    """Uncommitted work in the worktree, e.g. a daemon crash mid-turn."""
    result = cw_task._git(Path(d) / "agent", ["status", "--porcelain"])
    return result.returncode != 0 or bool(result.stdout.strip())


def _adopt(repo, wt, branch):
    """The worktree record of a directory left behind without meta['agent'], or None when it is
    not a valid worktree of this repo on the agent branch."""
    try:
        link = (wt / ".git").read_text()
        cw_task._check_git_link(repo, wt, link)
        if cw_task._git_ok(wt, ["rev-parse", "--abbrev-ref", "HEAD"]).strip() != branch:
            return None
        return {"branch": branch, "sha": cw_task._git_ok(wt, ["rev-parse", "HEAD"]).strip(), "link": link}
    except (OSError, RuntimeError, cw_task.GitLinkChanged):
        return None


def ensure_worktree(d, meta):
    """The one worktree every Full turn of this walkthrough edits, created on first use."""
    wt = Path(d) / "agent"
    if meta.get("agent") and wt.exists():
        if agent_stale(meta):
            _merge_head(d, meta, wt)
        return wt
    repo, branch = meta["repo"], cw_task.branch_name(meta, "agent")
    cw_task._git(repo, ["worktree", "prune"])
    agent = _adopt(repo, wt, branch) if wt.exists() else None
    if agent is None:
        if wt.exists():
            cw_task.remove_worktree(repo, wt)
        have_branch = cw_task._git(repo, ["rev-parse", "--verify", "-q", f"refs/heads/{branch}"]).returncode == 0
        args = (["worktree", "add", str(wt), branch] if have_branch
                else ["worktree", "add", "-b", branch, str(wt), meta["head"]])
        cw_task._git_ok(repo, args)
        agent = {"branch": branch, "sha": cw_task._git_ok(wt, ["rev-parse", "HEAD"]).strip(),
                 "link": (wt / ".git").read_text()}
    agent["base"] = meta["head"]
    cw_store.update_meta(d, lambda m: m.update(agent=agent))
    meta["agent"] = agent
    return wt


def _merge_head(d, meta, wt):
    """Bring the agent branch up to the walkthrough's new head, so a Full turn never runs on stale
    code. A conflict is aborted, leaving the worktree as it was."""
    repo, agent = meta["repo"], meta["agent"]
    cw_task._check_git_link(repo, wt, agent["link"])
    if cw_task._git_ok(wt, ["rev-parse", "--abbrev-ref", "HEAD"]).strip() != agent["branch"]:
        raise BranchChanged("the agent worktree is no longer on its branch")
    if cw_task._git_ok(wt, ["status", "--porcelain"]).strip():
        cw_task._commit_all(repo, wt, "agent edits left uncommitted")
    name = cw_task._git(repo, ["config", "user.name"]).stdout.strip() or "Code Walkthrough"
    email = cw_task._git(repo, ["config", "user.email"]).stdout.strip() or "code-walkthrough@localhost"
    # cw_task._git allows no merge, so this repeats its hardening.
    git = ["git", "-C", str(wt), *cw_task._HARDEN, *cw_task._filter_off(wt),
           "-c", f"user.name={name}", "-c", f"user.email={email}", "-c", "commit.gpgsign=false"]
    env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1", "GIT_CONFIG_NOSYSTEM": "1"}
    merged = subprocess.run([*git, "merge", "--no-edit", meta["head"]], capture_output=True, text=True,
                            errors="replace", env=env)
    if merged.returncode != 0:
        subprocess.run([*git, "merge", "--abort"], capture_output=True, env=env)
        raise cw_store.CWError("The PR changed since the agent edited it and the changes conflict",
                               remedy="discard the agent edits (Discard all agent edits) and ask again")
    sha, head = cw_task._git_ok(wt, ["rev-parse", "HEAD"]).strip(), meta["head"]
    cw_store.update_meta(d, lambda m: m["agent"].update(sha=sha, base=head))
    agent.update(sha=sha, base=head)


def discard_all(d, meta):
    """Drop every agent edit: the shared worktree, its branch, and the meta record. The user's own
    checkout is never touched. Callers hold the edit lock."""
    agent = meta.get("agent")
    if agent:
        branch = cw_task.branch_name(meta, "agent")
        if agent.get("branch") != branch:
            raise cw_store.CWError("the agent record does not name this walkthrough's branch")
        cw_task.remove_worktree(meta["repo"], Path(d) / "agent", branch)
    cw_store.update_meta(d, lambda m: m.pop("agent", None))
    meta.pop("agent", None)


def discard_edits(d, oid, on_event=None):
    """Discard the agent's edits on behalf of one `edits` outcome. Files go first and the
    outcomes are marked after, so a failed removal leaves everything retryable. Callers hold the
    edit lock."""
    current = next((o for o in cw_ask.read_outcomes(d) if o["oid"] == oid), None)
    if current is None:
        raise cw_ask.OutcomeError("outcome not found", "not_found")
    if current["state"] != "done":
        raise cw_ask.OutcomeError(f"outcome is {current['state']}, not done", "conflict")
    meta = cw_store.read_meta(d) or {}
    branch = (meta.get("agent") or {}).get("branch")
    discard_all(d, meta)
    folded = cw_ask.outcome_action(d, oid, "discard", None, on_event=on_event)
    for o in cw_ask.read_outcomes(d):
        if (o["outcome"] == "edits" and o["state"] == "done" and o["oid"] != oid
                and branch and (o["payload"] or {}).get("branch") == branch):
            cw_ask.outcome_action(d, o["oid"], "discard", None, on_event=on_event)
    return folded


def commit(d, meta, title, qid):
    """Commit whatever the turn left behind. None when HEAD did not move since the last turn."""
    wt, repo, agent = Path(d) / "agent", meta["repo"], meta["agent"]
    cw_task._check_git_link(repo, wt, agent["link"])
    if cw_task._git_ok(wt, ["rev-parse", "--abbrev-ref", "HEAD"]).strip() != agent["branch"]:
        raise BranchChanged("the agent worktree is no longer on its branch")
    if cw_task._git_ok(wt, ["status", "--porcelain"]).strip():
        cw_task._commit_all(repo, wt, f"{title}\n\nCode Walkthrough turn {qid}")
    sha, parent = cw_task._git_ok(wt, ["rev-parse", "HEAD"]).strip(), agent["sha"]
    if sha == parent:
        return None
    stat = {}
    for line in cw_task._git_ok(wt, ["diff", "--numstat", "--no-renames", f"{parent}..{sha}"]).splitlines():
        add, dele, path = line.split("\t", 2)
        stat[path] = f"+{add if add.isdigit() else 0} -{dele if dele.isdigit() else 0}"
    cw_store.update_meta(d, lambda m: m["agent"].update(sha=sha))
    agent["sha"] = sha
    out = {"sha": sha, "parent": parent, "branch": agent["branch"]}
    if len(stat) > FILES_CAP:
        stat, out["truncated"] = dict(list(stat.items())[:FILES_CAP]), True
    return {**out, "files": list(stat), "stat": stat}


def after_turn(d, meta, record, on_event=None, start_sibling=None):
    """Commit the Full turn's edits and record them as an `edits` outcome; returns the summary or None.
    A reload the turn requested is answered here: the daemon, not the page, marks it done or failed."""
    lines = (record.get("comment") or "").strip().splitlines()
    try:
        edits = commit(d, meta, (lines[0][:72] if lines else "agent edits"), record["qid"])
        if edits:
            cw_ask.append_outcome(d, record["qid"], record["thread_id"], "edits", "done", edits, on_event)
        for o in _requested_reloads(d, record["qid"]):
            _answer_reload(d, meta, o, edits, on_event, start_sibling)
    except Exception as e:
        for o in _requested_reloads(d, record["qid"]):
            cw_ask.mark_outcome(d, o["oid"], "failed", on_event, {"reason": (str(e) or type(e).__name__)[:200]})
        raise
    return edits


def _requested_reloads(d, qid):
    return [o for o in cw_ask.read_outcomes(d)
            if o["outcome"] == "reload" and o["state"] == "requested" and o["qid"] == qid]


def fail_interrupted_reloads(d):
    """A reload still requested at daemon start: its turn died with the old daemon."""
    for o in cw_ask.read_outcomes(d):
        if o["outcome"] == "reload" and o["state"] == "requested":
            cw_ask.mark_outcome(d, o["oid"], "failed", payload={"reason": "interrupted"}, expect=("requested",))


def _answer_reload(d, meta, o, edits, on_event, start_sibling):
    agent = meta["agent"]
    sha = edits["sha"] if edits else (agent["sha"] if has_edits(d, meta) else None)
    if not sha or not start_sibling:
        cw_ask.mark_outcome(d, o["oid"], "failed", on_event, {"reason": "no edits to show"})
        return
    try:
        res = start_sibling(oid=o["oid"], sha=sha, task_id=f"e-{sha[:7]}", branch=agent["branch"],
                            title="Agent edits")
    except cw_store.CWError as e:
        cw_ask.mark_outcome(d, o["oid"], "failed", on_event, {"reason": str(e)})
        return
    cw_ask.mark_outcome(d, o["oid"], "done", on_event,
                        {"sibling": {"key": res["key"], "id": res["id"], "url": res["url"]}})
