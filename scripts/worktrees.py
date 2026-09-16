#!/usr/bin/env python3
"""State of this repo's Orca worktrees: inventory, one worker's status, what can be removed.

Why: cleanup used to mean writing a throwaway inventory script into the scratchpad, which
vanished with the session. And judging "idle" by whether the local branch is merged into the
base branch is wrong: a worktree can sit on the base branch with its PR still open and under
review. So this reads PR state, working-tree cleanliness and agent activity together. HEAD
always comes from git, never from Orca's cache, which lags behind the branch.

Usage:
  worktrees.py inventory [--json] [--no-fetch]
  worktrees.py status <name>          # one word: in-review / blocked / other workspaceStatus; for Monitor loops
  worktrees.py overlap <path...>      # open PRs / worktrees touching these paths, plus migration numbers already taken
  worktrees.py cleanup [--idle-hours 3] [--no-fetch]
  worktrees.py cleanup --apply a,b [--dry-run]   # removes only the named ones that are still candidates

Kept regardless: the merge queue's worktree, anything in keep_worktrees, and $ORCA_FLOW_KEEP.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfgmod  # noqa: E402

ORCA = os.environ.get("ORCA_CLI_COMMAND", "orca")
BUSY_STATES = {"working", "permission"}
CFG = cfgmod.load()
KEEP = set(CFG.get("keep_worktrees") or [])
BASE = CFG.get("base_branch") or "origin/main"
REMOTE = BASE.split("/")[0] if "/" in BASE else "origin"


def die(msg, **extra):
    print(json.dumps({"ok": False, "error": msg, **extra}, ensure_ascii=False, indent=1))
    sys.exit(1)


def run(cmd, cwd=None):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def orca(*args):
    cmd = [ORCA, *args, "--json"]
    code, out, err = run(cmd)
    try:
        data = json.loads(out)
    except ValueError:
        die(f"orca returned no JSON: {shlex.join(cmd)}", stderr=err[-2000:])
    if not data.get("ok"):
        die(f"orca reported failure: {shlex.join(cmd)}", response=data)
    return data.get("result") or {}


def repo_context():
    repo_root = CFG.get("repo_root")
    if not repo_root:
        die("not inside a git repository")
    repos = orca("repo", "list").get("repos", [])
    repo = next((r for r in repos if os.path.realpath(r.get("path", "")) == os.path.realpath(repo_root)), None)
    if not repo:
        die(f"Orca has no repo at {repo_root}")
    return repo_root, repo["id"]


def collect(fetch):
    repo_root, repo_id = repo_context()
    notes = []
    if fetch:
        code, _, err = run(["git", "fetch", "--quiet", REMOTE], cwd=repo_root)
        if code:
            notes.append(f"git fetch failed, {BASE} may be stale: {err[-200:]}")

    worktrees = orca("worktree", "list", "--repo", f"id:{repo_id}").get("worktrees", [])
    ps = {w.get("worktreeId"): w for w in orca("worktree", "ps").get("worktrees", [])}

    prs = {}
    code, out, err = run(["gh", "pr", "list", "--state", "all", "--limit", "200",
                          "--json", "number,state,headRefName,headRefOid,title"], cwd=repo_root)
    pr_ok = code == 0
    if not pr_ok:
        notes.append(f"gh pr list failed, PR state unknown (so nothing will be a cleanup candidate): {err[-200:]}")
    else:
        for pr in json.loads(out):
            prev = prs.get(pr["headRefName"])
            if prev is None or pr["number"] > prev["number"]:
                prs[pr["headRefName"]] = pr

    now_ms = time.time() * 1000
    rows = []
    for w in worktrees:
        if w.get("isMainWorktree"):
            continue
        path = w.get("path", "")
        branch = (w.get("branch") or "").removeprefix("refs/heads/")
        exists = os.path.isdir(path)
        dirty = ahead = None
        head = w.get("head")
        if exists:
            code, out, _ = run(["git", "-C", path, "status", "--porcelain"])
            dirty = len([l for l in out.splitlines() if l.strip()]) if code == 0 else None
            code, out, _ = run(["git", "-C", path, "rev-parse", "HEAD"])
            if code == 0:
                head = out
        if head:
            code, out, _ = run(["git", "-C", repo_root, "rev-list", "--count", f"{BASE}..{head}"])
            ahead = int(out) if code == 0 and out.isdigit() else None
        p = ps.get(w.get("id"), {})
        states = {p.get("status")} | {ag.get("state") for ag in p.get("agents") or []}
        last = max(w.get("lastActivityAt") or 0, p.get("lastOutputAt") or 0)
        rows.append({
            "name": os.path.basename(path), "id": w.get("id"), "path": path, "branch": branch,
            "exists": exists, "dirty": dirty, "ahead": ahead, "head": head,
            "busy": bool(states & BUSY_STATES), "status": p.get("status"),
            "idle_hours": round((now_ms - last) / 3.6e6, 1) if last else None,
            "workspace_status": w.get("workspaceStatus"), "comment": w.get("comment") or "",
            "pr": prs.get(branch), "pr_known": pr_ok,
        })
    return rows, notes


def decide(r, idle_hours):
    """(removable?, reason). When in doubt, keep: deleting someone's work is far worse
    than leaving an extra worktree around."""
    if r["name"] in KEEP:
        return False, "on the keep list"
    if not r["exists"]:
        return True, "folder is gone; only Orca's record is left"
    if r["dirty"] is None:
        return False, "can't read git status"
    if r["dirty"]:
        return False, f"{r['dirty']} uncommitted change(s)"
    if r["busy"]:
        return False, f"an agent is still running ({r['status']})"
    if r["comment"].upper().startswith("BLOCKED"):
        return False, "worker reported BLOCKED; the user should see why first"
    if not r["pr_known"]:
        return False, "PR state unknown"
    pr = r["pr"]
    if pr:
        n = pr["number"]
        if pr["state"] == "MERGED":
            if r["ahead"] == 0 or r["head"] == pr["headRefOid"]:
                return True, f"PR #{n} merged"
            return False, f"PR #{n} merged, but there are later local commits"
        if pr["state"] == "CLOSED":
            return False, f"PR #{n} closed without merging; ask the user"
        return False, f"PR #{n} still open"
    if r["ahead"]:
        return False, f"{r['ahead']} commit(s) with no PR"
    if r["idle_hours"] is not None and r["idle_hours"] >= idle_hours:
        return True, f"no commits, idle {r['idle_hours']:.0f}h"
    return False, "no commits, but active recently"


def cmd_status(name):
    _, repo_id = repo_context()
    for w in orca("worktree", "list", "--repo", f"id:{repo_id}").get("worktrees", []):
        if os.path.basename(w.get("path", "")) == name or w.get("displayName") == name:
            comment = w.get("comment") or ""
            print("blocked" if comment.upper().startswith("BLOCKED") else (w.get("workspaceStatus") or "unknown"))
            return
    print("missing")


def changed_paths(path):
    """What a worktree changed relative to the base branch: committed plus uncommitted
    (including untracked)."""
    committed = uncommitted = []
    code, out, _ = run(["git", "-C", path, "diff", "--name-only", f"{BASE}...HEAD"])
    if code == 0:
        committed = [l for l in out.splitlines() if l.strip()]
    code, out, _ = run(["git", "-C", path, "status", "--porcelain", "--untracked-files=all"])
    if code == 0:
        uncommitted = [l[3:].split(" -> ")[-1].strip('"') for l in out.splitlines() if len(l) > 3]
    return committed, uncommitted


def cmd_overlap(paths, fetch):
    """Why this exists: managers used to guess from PR titles whether two packages would
    collide, and kept missing a PR that also touched the same file, or a migration number
    an uncommitted worktree had already taken. The file list is a fact you can look up."""
    repo_root, repo_id = repo_context()
    if fetch:
        run(["git", "fetch", "--quiet", REMOTE], cwd=repo_root)
    wanted = [p.strip().removeprefix("./").rstrip("/") for p in paths if p.strip()]
    mig_dir = (CFG["worker"].get("migrations_dir") or "").rstrip("/")

    def hits(files):
        return sorted({f for f in files for w in wanted if f == w or f.startswith(w + "/")})

    def is_migration(f):
        return bool(mig_dir) and f.startswith(mig_dir + "/")

    migrations = []
    code, out, err = run(["gh", "pr", "list", "--state", "open", "--limit", "100",
                          "--json", "number,title,headRefName,files"], cwd=repo_root)
    if code:
        print(f"! gh pr list failed, open PRs were not checked: {err[-200:]}")
    else:
        for pr in json.loads(out):
            files = [f["path"] for f in pr.get("files") or []]
            h = hits(files)
            if h:
                print(f"PR #{pr['number']} {pr['title'][:40]}: {', '.join(h)}")
            migrations += [(f, f"PR #{pr['number']}") for f in files if is_migration(f)]

    for w in orca("worktree", "list", "--repo", f"id:{repo_id}").get("worktrees", []):
        path = w.get("path", "")
        if w.get("isMainWorktree") or not os.path.isdir(path):
            continue
        name = os.path.basename(path)
        committed, uncommitted = changed_paths(path)
        for label, files in (("committed", committed), ("uncommitted", uncommitted)):
            h = hits(files)
            if h:
                print(f"worktree {name} ({label}): {', '.join(h)}")
            migrations += [(f, f"worktree {name} ({label})") for f in files if is_migration(f)]

    if mig_dir:
        code, out, _ = run(["git", "-C", repo_root, "ls-tree", "--name-only", BASE, f"{mig_dir}/"])
        on_base = set(out.splitlines()) if code == 0 else set()
        taken = sorted({(f, where) for f, where in migrations if f not in on_base})
        if taken:
            print(f"migrations taken outside {BASE}:")
            for f, where in taken:
                print(f"  {f}  <- {where}")
    if wanted:
        print("(anything not listed above does not overlap)")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    inv = sub.add_parser("inventory")
    inv.add_argument("--json", action="store_true")
    inv.add_argument("--no-fetch", action="store_true")
    st = sub.add_parser("status")
    st.add_argument("name")
    ov = sub.add_parser("overlap")
    ov.add_argument("paths", nargs="*", help="files or directories this package will touch (repo-relative)")
    ov.add_argument("--no-fetch", action="store_true")
    cl = sub.add_parser("cleanup")
    cl.add_argument("--apply", metavar="NAMES", help="comma-separated worktree names; removes only these")
    cl.add_argument("--idle-hours", type=float, default=3)
    cl.add_argument("--no-fetch", action="store_true")
    cl.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    if a.cmd == "status":
        cmd_status(a.name)
        return
    if a.cmd == "overlap":
        cmd_overlap(a.paths, fetch=not a.no_fetch)
        return

    rows, notes = collect(fetch=not a.no_fetch)
    for n in notes:
        print(f"! {n}")

    if a.cmd == "inventory":
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
            return
        for r in sorted(rows, key=lambda r: r["name"]):
            pr = f"#{r['pr']['number']} {r['pr']['state']}" if r["pr"] else "-"
            print(f"{r['name']:<32} {str(r['status']):<10} {str(r['workspace_status']):<12} PR {pr:<12} "
                  f"ahead {str(r['ahead']):<3} dirty {str(r['dirty']):<3} idle {r['idle_hours']}h  {r['comment']}")
        return

    dry = a.dry_run or os.environ.get("ORCA_FLOW_DRY_RUN") == "1"
    candidates = {}
    for r in sorted(rows, key=lambda r: r["name"]):
        ok, why = decide(r, a.idle_hours)
        print(f"{'REMOVABLE' if ok else 'keep     '}  {r['name']:<32} {why}")
        if ok:
            candidates[r["name"]] = r
    if not a.apply:
        print(f"\n{len(candidates)} removable." + (f" After the user agrees: --apply {','.join(candidates)}" if candidates else ""))
        return

    # Only the named ones, and only if still candidates: state can change between showing
    # the list to the user and getting an answer.
    wanted = [n.strip() for n in a.apply.split(",") if n.strip()]
    for name in wanted:
        r = candidates.get(name)
        if r is None:
            print(f"skipping {name}: not a candidate right now (see the reason above)")
            continue
        cmd = ["worktree", "rm", "--worktree", f"id:{r['id']}", "--force"]
        if dry:
            print("DRY-RUN:", shlex.join([ORCA, *cmd, "--json"]))
            continue
        # --force only after confirming above that the tree is clean; Orca still keeps
        # branches it can't prove are merged.
        orca(*cmd)
        print(f"removed {name}")


if __name__ == "__main__":
    main()
