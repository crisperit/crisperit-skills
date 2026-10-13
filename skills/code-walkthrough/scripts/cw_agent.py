"""Trust and the shared edit worktree for the thread agent's Full mode. Imported only by
cw_server: cw_task and cw_ask must never import it back."""

from pathlib import Path

import cw_ask
import cw_store
import cw_task
import links
import state


def repo_name(meta):
    """owner/name: gh_repo for a PR, else read from the origin remote (one git call)."""
    if meta.get("gh_repo"):
        return meta["gh_repo"]
    if not meta.get("repo"):
        return None
    return state._repo_from_links({"repo_url": links.repo_web_url(meta["repo"])})


def trust(meta, config):
    """Whether Full is available for this walkthrough's repo. Never calls out; own_pr is the
    value cw_run.run cached in meta."""
    if meta.get("sibling_of"):
        return False
    if meta.get("own_pr") is True:
        return True
    cfg = config.get("trust") or {}
    if not cfg.get("repos") and not cfg.get("orgs"):
        return False
    repo = (repo_name(meta) or "").lower()
    if repo in {r.lower() for r in cfg.get("repos") or []}:
        return True
    return repo.split("/", 1)[0] in {o.lower() for o in cfg.get("orgs") or []} if repo else False


class BranchChanged(RuntimeError):
    pass


def ensure_worktree(d, meta):
    """The one worktree every Full turn of this walkthrough edits, created on first use."""
    wt = Path(d) / "agent"
    if meta.get("agent") and wt.exists():
        return wt
    repo, branch = meta["repo"], cw_task.branch_name(meta, "agent")
    have_branch = cw_task._git(repo, ["rev-parse", "--verify", "-q", f"refs/heads/{branch}"]).returncode == 0
    args = ["worktree", "add", str(wt), branch] if have_branch else ["worktree", "add", "-b", branch, str(wt), meta["head"]]
    cw_task._git_ok(repo, args)
    agent = {"branch": branch, "sha": cw_task._git_ok(wt, ["rev-parse", "HEAD"]).strip(),
             "link": (wt / ".git").read_text()}
    cw_store.update_meta(d, lambda m: m.update(agent=agent))
    meta["agent"] = agent
    return wt


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
    return {"sha": sha, "parent": parent, "branch": agent["branch"], "files": list(stat), "stat": stat}


def after_turn(d, meta, record, on_event=None):
    """Commit the Full turn's edits and record them as an `edits` outcome; returns the summary or None."""
    lines = (record.get("comment") or "").strip().splitlines()
    edits = commit(d, meta, (lines[0][:72] if lines else "agent edits"), record["qid"])
    if edits:
        cw_ask.append_outcome(d, record["qid"], record["thread_id"], "edits", "done", edits, on_event)
    return edits
