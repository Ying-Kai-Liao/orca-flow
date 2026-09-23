#!/usr/bin/env python3
"""State of this repo's Orca worktrees: inventory, one worker's status, what can be removed.

Why: cleanup used to mean writing a throwaway inventory script into the scratchpad, which
vanished with the session. And judging "idle" by whether the local branch is merged into the
base branch is wrong: a worktree can sit on the base branch with its PR still open and under
review. So this reads PR state, working-tree cleanliness and agent activity together. HEAD
always comes from git, never from Orca's cache, which lags behind the branch.

Usage:
  worktrees.py inventory [--json] [--no-fetch]   # one line per worktree, then: agents waiting on the user, open PRs nobody handed over
  worktrees.py status <name>          # one word: in-review / blocked / handoff / other workspaceStatus; for Monitor loops
  worktrees.py context [<name>]       # context estimate per worker session, flags the ones over worker.context_warn
  worktrees.py handoff <name>         # write briefs/<name>/handoff-digest.md from the worker's transcript and print it
  worktrees.py overlap <path...>      # open PRs / worktrees touching these paths, plus migration numbers already taken
  worktrees.py cleanup [--idle-hours 3] [--no-fetch]
  worktrees.py cleanup --apply a,b [--dry-run]   # removes only the named ones that are still candidates

"Merged" is judged three ways, because a worktree's branch name is not reliable: the PR
found by branch name, else a PR whose head equals the worktree's HEAD, else HEAD already
being an ancestor of the base branch (a renamed branch or a detached HEAD after merge).
"Idle" uses the worktree's and its agents' own last-activity times, not the terminal's
last output: an open shell prompt repaints constantly and made every worktree look busy.

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
import board_rules  # noqa: E402
import config as cfgmod  # noqa: E402
import transcript  # noqa: E402

ORCA = os.environ.get("ORCA_CLI_COMMAND", "orca")
BUSY_STATES = {"working", "permission"}
CFG = cfgmod.load()
KEEP = set(CFG.get("keep_worktrees") or [])
BASE = CFG.get("base_branch") or "origin/main"
REMOTE = BASE.split("/")[0] if "/" in BASE else "origin"
CTX_WINDOW = int(CFG["worker"].get("context_window") or 200000)
CTX_WARN = float(CFG["worker"].get("context_warn") or 0.35)
TRANSCRIPTS = CFG["worker"].get("transcripts_dir")


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
    by_head = {}
    if pr_ok:
        for pr in json.loads(out):
            prev = prs.get(pr["headRefName"])
            if prev is None or pr["number"] > prev["number"]:
                prs[pr["headRefName"]] = pr
            by_head.setdefault(pr["headRefOid"], pr)

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
        on_base = False
        if head and ahead == 0:
            code, _, _ = run(["git", "-C", repo_root, "merge-base", "--is-ancestor", head, BASE])
            on_base = code == 0
        p = ps.get(w.get("id"), {})
        agents = p.get("agents") or []
        states = {p.get("status")} | {ag.get("state") for ag in agents}
        # Not lastOutputAt: a terminal sitting at a shell prompt repaints all the time.
        last = max([w.get("lastActivityAt") or 0] + [ag.get("updatedAt") or 0 for ag in agents])
        pr = prs.get(branch) or (by_head.get(head) if head else None)
        # One rule set with board.py. Only panes Orca itself reports as waiting (or at a
        # permission prompt) are listed; questions inferred from the message text are
        # board.py's job, where they can be demoted when old.
        waiting = [(ag.get("lastAssistantMessage") or "").strip() for ag in agents
                   if ag.get("state") in board_rules.WAITING_STATES
                   and board_rules.classify(board_rules.make_row(p, ag, now_ms))[0] == "needs_human"]
        ctx = None
        if exists:
            f = transcript.latest_transcript(path, TRANSCRIPTS)
            if f:
                m = transcript.measure(f)
                ctx = {"tokens": m["tokens_estimate"], "pct": round(m["tokens_estimate"] / CTX_WINDOW, 2),
                       "turns": m["assistant_turns"], "compactions": m["compactions"], "file": f}
        rows.append({
            "name": os.path.basename(path), "id": w.get("id"), "path": path, "branch": branch,
            "exists": exists, "dirty": dirty, "ahead": ahead, "head": head, "on_base": on_base,
            "busy": bool(states & BUSY_STATES), "status": p.get("status"),
            "idle_hours": round((now_ms - last) / 3.6e6, 1) if last else None,
            "workspace_status": w.get("workspaceStatus"), "comment": w.get("comment") or "",
            "pr": pr, "pr_known": pr_ok, "waiting": waiting, "ctx": ctx,
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
    if r["on_base"] and r["branch"] and r["branch"].split("/")[-1] != BASE.split("/")[-1]:
        # A feature branch whose HEAD is already on the base branch: merged, PR just not
        # found by name (renamed branch). A worktree still on the base branch itself is
        # different: that's "never started", judged by idle time below.
        return True, f"HEAD is already on {BASE} (no PR matched the branch name)"
    if r["on_base"] and not r["branch"]:
        return True, f"detached HEAD already on {BASE}"
    if r["idle_hours"] is not None and r["idle_hours"] >= idle_hours:
        return True, f"no commits, idle {r['idle_hours']:.0f}h"
    return False, "no commits, but active recently"


def cmd_status(name):
    _, repo_id = repo_context()
    for w in orca("worktree", "list", "--repo", f"id:{repo_id}").get("worktrees", []):
        if os.path.basename(w.get("path", "")) == name or w.get("displayName") == name:
            comment = w.get("comment") or ""
            up = comment.upper()
            print("blocked" if up.startswith("BLOCKED") else "handoff" if up.startswith("HANDOFF") else (w.get("workspaceStatus") or "unknown"))
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


def fmt_ctx(c):
    if not c:
        return "ctx -"
    flag = "!" if c["pct"] >= CTX_WARN else " "
    return f"ctx {c['tokens'] // 1000:>3}k {int(c['pct'] * 100):>3}%{flag}"


def unhanded_prs(repo_root):
    """Open, non-draft PRs with no handover file. The queue only sees what it's handed, so
    a finished PR can sit for days while everyone assumes it shipped."""
    code, out, _ = run(["gh", "pr", "list", "--state", "open", "--limit", "100",
                        "--json", "number,title,headRefName,isDraft,updatedAt"], cwd=repo_root)
    if code:
        return None
    qdir = os.path.join(cfgmod.common_dir(repo_root) or "", "orca-flow", "queue")
    rows = []
    for pr in json.loads(out):
        if pr.get("isDraft"):
            continue
        f = os.path.join(qdir, f"{pr['number']}.json")
        status = None
        if os.path.isfile(f):
            try:
                with open(f, encoding="utf-8") as fh:
                    status = json.load(fh).get("status") or "?"
            except (ValueError, OSError):
                status = "?"
        rows.append({"number": pr["number"], "title": pr["title"], "branch": pr["headRefName"],
                     "updated": pr["updatedAt"], "handover": status})
    return rows


def pr_row(u):
    """An unhanded_prs() entry as a board row with no agent pane, so classify decides it.
    unhanded_prs() already dropped drafts; the list only holds open PRs."""
    return {"pr": {"number": u["number"], "state": "OPEN", "isDraft": False}, "handover": u["handover"],
            "state": None, "comment": ""}


def cmd_context(rows, name):
    for r in sorted(rows, key=lambda r: -(r["ctx"] or {}).get("pct", -1)):
        if name and r["name"] != name:
            continue
        c = r["ctx"]
        if not c:
            print(f"{r['name']:<32} no transcript found")
            continue
        print(f"{r['name']:<32} {fmt_ctx(c)}  {c['turns']} turns, {c['compactions']} compaction(s)  {c['file']}")
    print(f"\nestimate against a {CTX_WINDOW // 1000}k window; '!' = over {int(CTX_WARN * 100)}% (worker.context_warn). "
          f"Over the line: send the worker the wrap-up line, then spawn_worker.py --continue.")


def cmd_handoff(name):
    repo_root, repo_id = repo_context()
    wt = next((w for w in orca("worktree", "list", "--repo", f"id:{repo_id}").get("worktrees", [])
               if os.path.basename(w.get("path", "")) == name or w.get("displayName") == name), None)
    if not wt:
        die(f"no worktree called {name}")
    path = wt["path"]
    f = transcript.latest_transcript(path, TRANSCRIPTS)
    if not f:
        die(f"no transcript found for {path}", looked_in=transcript.project_dir(path, TRANSCRIPTS))
    text = transcript.render_digest(transcript.digest(f), path, transcript.git_summary(path, BASE))
    brief_dir = os.path.join(cfgmod.common_dir(repo_root), "orca-flow", "briefs", name)
    os.makedirs(brief_dir, exist_ok=True)
    out = os.path.join(brief_dir, "handoff-digest.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    print(f"(written to {out})")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    inv = sub.add_parser("inventory")
    inv.add_argument("--json", action="store_true")
    inv.add_argument("--no-fetch", action="store_true")
    st = sub.add_parser("status")
    st.add_argument("name")
    cx = sub.add_parser("context")
    cx.add_argument("name", nargs="?")
    cx.add_argument("--no-fetch", action="store_true")
    ho = sub.add_parser("handoff")
    ho.add_argument("name")
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
    if a.cmd == "handoff":
        cmd_handoff(a.name)
        return

    rows, notes = collect(fetch=not a.no_fetch)
    for n in notes:
        print(f"! {n}")

    if a.cmd == "context":
        cmd_context(rows, a.name)
        return

    if a.cmd == "inventory":
        repo_root, _ = repo_context()
        unhanded = unhanded_prs(repo_root)
        if a.json:
            print(json.dumps({"worktrees": rows, "open_prs": unhanded}, ensure_ascii=False, indent=1))
            return
        for r in sorted(rows, key=lambda r: r["name"]):
            pr = f"#{r['pr']['number']} {r['pr']['state']}" if r["pr"] else ("on-base" if r["on_base"] else "-")
            print(f"{r['name']:<32} {str(r['status']):<10} {str(r['workspace_status']):<12} PR {pr:<12} "
                  f"ahead {str(r['ahead']):<3} dirty {str(r['dirty']):<3} idle {str(r['idle_hours']):<5}h {fmt_ctx(r['ctx'])}  {r['comment']}")
        waiting = [(r["name"], m) for r in rows for m in r["waiting"]]
        if waiting:
            print("\nWaiting on the user (an agent asked something and stopped):")
            for name, m in waiting:
                tail = m[-300:].replace("\n", " ")
                print(f"  {name}: …{tail}" if len(m) > 300 else f"  {name}: {tail}")
        if unhanded is None:
            print("\n! gh pr list failed; open PRs not checked")
        else:
            missing = [u for u in unhanded if board_rules.classify(pr_row(u))[0] == "unhanded_pr"]
            if missing:
                print("\nOpen PRs with no handover file (the queue doesn't know about these):")
                for u in missing:
                    back = "  [sent back by the queue]" if u["handover"] else ""
                    print(f"  #{u['number']} {u['branch']}  {u['title'][:60]}  (updated {u['updated'][:10]}){back}")
            done = [u for u in unhanded if u["handover"] and u not in missing]
            if done:
                print("\nOpen PRs already handed over: " + ", ".join(f"#{u['number']} ({u['handover']})" for u in done))
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
