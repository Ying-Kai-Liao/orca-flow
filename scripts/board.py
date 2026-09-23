#!/usr/bin/env python3
"""Attention board: one row per agent pane across every Orca worktree on this host.

Why: parallel sessions fail on coordination, not code. A finished PR sits unhanded because
nobody noticed the worker was done; a worker asking a decision in prose looks exactly like a
worker that finished. `orca worktree ps` has the raw state; this adds a derived `attention`
per pane (board_rules.classify) and sorts so what needs a person comes first.

It only reads: `orca worktree ps`, `orca repo list`, `gh pr list` and the handover queue
files. It never writes to Orca. Polling only, since Orca has no event stream.

Usage:
  board.py [--json] [--repo <name>] [--stale-min 30] [--no-gh]
  board.py --watch [--interval 20]     # prints only rows whose attention changed, timestamped
  board.py --write                     # also writes <git-common-dir>/orca-flow/board.json of this repo

Rules and the row schema: references/board.md.
"""
import argparse
import datetime
import json
import os
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_rules  # noqa: E402
import config as cfgmod  # noqa: E402

ORCA = os.environ.get("ORCA_CLI_COMMAND", "orca")
# Branches a PR is never opened from; skipping them saves a gh call per untouched repo.
BASE_NAMES = {"main", "master", "develop", "trunk"}


class OrcaError(Exception):
    pass


def die(msg, **extra):
    print(json.dumps({"ok": False, "error": msg, **extra}, ensure_ascii=False, indent=1))
    sys.exit(1)


def run(cmd, cwd=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    except (OSError, ValueError) as e:
        return 1, "", str(e)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def orca(*args):
    cmd = [ORCA, *args, "--json"]
    code, out, err = run(cmd)
    try:
        data = json.loads(out)
    except ValueError:
        raise OrcaError(f"orca returned no JSON: {shlex.join(cmd)}: {err[-500:]}")
    if not data.get("ok"):
        raise OrcaError(f"orca reported failure: {shlex.join(cmd)}: {json.dumps(data, ensure_ascii=False)[:500]}")
    return data.get("result") or {}


def git_common_dir(path):
    code, out, _ = run(["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"])
    return out if code == 0 and out else None


def repo_prs(path):
    """{branch: pr} for one repo, newest PR per branch; None when gh can't answer."""
    code, out, _ = run(["gh", "pr", "list", "--state", "all", "--limit", "200",
                        "--json", "number,state,isDraft,headRefName,title"], cwd=path)
    if code:
        return None
    prs = {}
    try:
        items = json.loads(out or "[]")
    except ValueError:
        return None
    for pr in items:
        prev = prs.get(pr["headRefName"])
        if prev is None or pr["number"] > prev["number"]:
            prs[pr["headRefName"]] = pr
    return prs


def handover_status(common, number):
    """The queue file's status, "?" if unreadable, None if there is no file."""
    if not common or not number:
        return None
    f = os.path.join(common, "orca-flow", "queue", f"{int(number)}.json")
    if not os.path.isfile(f):
        return None
    try:
        with open(f, encoding="utf-8") as fh:
            return json.load(fh).get("status") or "?"
    except (ValueError, OSError):
        return "?"


def collect(stale_min, repo_filter=None, use_gh=True):
    """(rows, notes). Rows are already classified and sorted."""
    notes = []
    ps = orca("worktree", "ps", "--limit", "1000")
    if ps.get("truncated"):
        notes.append(f"orca worktree ps was truncated ({ps.get('totalCount')} total); some worktrees are missing")
    worktrees = [w for w in ps.get("worktrees") or [] if (w.get("hostId") or "local") == "local"]
    if repo_filter:
        worktrees = [w for w in worktrees if w.get("repo") == repo_filter]
    repos = {r["id"]: r for r in orca("repo", "list").get("repos") or [] if r.get("id")}

    by_repo = {}
    for w in worktrees:
        by_repo.setdefault(w.get("repoId"), []).append(w)
    prs, commons = {}, {}
    for repo_id, wts in by_repo.items():
        repo = repos.get(repo_id) or {}
        root = repo.get("path") or wts[0].get("path")
        if repo.get("kind") == "folder" or not root or not os.path.isdir(root):
            continue
        commons[repo_id] = git_common_dir(root)
        branches = {(w.get("branch") or "").removeprefix("refs/heads/") for w in wts} - BASE_NAMES - {""}
        if use_gh and branches:
            prs[repo_id] = repo_prs(root)
            if prs[repo_id] is None:
                notes.append(f"gh pr list failed for {repo.get('displayName') or root}; using Orca's linked PR")

    now_ms = time.time() * 1000
    rows = []
    for w in worktrees:
        branch = (w.get("branch") or "").removeprefix("refs/heads/")
        pr = None
        found = (prs.get(w.get("repoId")) or {}).get(branch)
        if found:
            pr = {"number": found["number"], "state": found["state"], "isDraft": found.get("isDraft"),
                  "title": found.get("title")}
        elif w.get("linkedPR") and w["linkedPR"].get("number"):
            # Orca's link has no draft flag; isDraft None means "not known", which the rules
            # treat as not a draft.
            lp = w["linkedPR"]
            pr = {"number": lp["number"], "state": (lp.get("state") or "").upper() or None, "isDraft": None,
                  "title": lp.get("title")}
        handover = handover_status(commons.get(w.get("repoId")), pr and pr["number"])
        # A worktree with no agent pane still gets one row: a BLOCKED card or an unhanded PR
        # matters whether or not a session is open on it.
        for ag in (w.get("agents") or [None]):
            row = board_rules.make_row(w, ag, now_ms, pr=pr, handover=handover)
            row["attention"], row["reason"] = board_rules.classify(row, stale_min=stale_min)
            rows.append(row)
    return sort_rows(rows), notes


def sort_rows(rows):
    """Grouped by repo, the repo with the most urgent row first; inside a repo by urgency,
    then the longest in that state first."""
    best = {}
    for r in rows:
        best[r["repo"]] = min(best.get(r["repo"], 99), board_rules.PRIORITY[r["attention"]])
    return sorted(rows, key=lambda r: (best[r["repo"]], str(r["repo"]), board_rules.PRIORITY[r["attention"]],
                                       -(r["minutes_in_state"] or 0), r["worktree"] or ""))


def fmt_min(m):
    if m is None:
        return "-"
    return f"{m / 60:.1f}h" if m >= 90 else f"{int(m)}m"


def fmt_row(r):
    pr = f"#{r['pr']['number']} {r['pr']['state']}" if r["pr"] else "-"
    msg = r["last_message"].replace("\n", " ")[:80]
    if r["attention"] == "needs_human" and len(r.get("last_message_tail") or "") > 80:
        # The question sits at the end of the message, not in its first line.
        msg = "…" + r["last_message_tail"].replace("\n", " ")[-80:]
    comment = f"  [{r['comment'][:50]}]" if r["comment"] else ""
    return (f"  {r['attention']:<12} {str(r['worktree']):<28} {str(r['state'] or '-'):<8} {fmt_min(r['minutes_in_state']):>6} "
            f"PR {pr:<12} {r['reason']}{comment}" + (f"\n{'':<15}» {msg}" if msg else ""))


def print_board(rows):
    repo = object()
    for r in rows:
        if r["repo"] != repo:
            repo = r["repo"]
            print(f"\n{repo}")
        print(fmt_row(r))


def write_board(rows, notes):
    common = cfgmod.common_dir()
    if not common:
        die("--write needs to run inside a git repository")
    d = os.path.join(common, "orca-flow")
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, "board.json")
    tmp = f"{out}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload(rows, notes), f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, out)  # atomic: a reader (jev-handoff, a Monitor) may open it any time
    return out


def payload(rows, notes):
    return {"at": stamp(), "rows": rows, "notes": notes}


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def changes(prev, cur):
    """Lines for panes whose attention differs between two ticks ({key: row} each)."""
    lines = []
    for key, r in cur.items():
        old = prev.get(key)
        if old is None or old["attention"] != r["attention"]:
            was = old["attention"] if old else "new"
            lines.append(f"{r['repo']}/{r['worktree']}: {was} -> {r['attention']} ({r['reason']})")
    for key, old in prev.items():
        if key not in cur:
            lines.append(f"{old['repo']}/{old['worktree']}: {old['attention']} -> gone")
    return lines


def watch(a):
    """Print the board once, then only the rows whose attention changed. A row is one pane;
    a pane that disappears is printed as gone."""
    prev = None
    try:
        while True:
            try:
                rows, notes = collect(a.stale_min, a.repo, use_gh=not a.no_gh)
            except OrcaError as e:
                print(f"{stamp()} ! {e}", flush=True)
                time.sleep(a.interval)
                continue
            if a.write:
                write_board(rows, notes)
            cur = {(r["worktree_id"], r["pane"]): r for r in rows}
            if prev is None:
                print(f"{stamp()} board, {len(rows)} rows")
                for n in notes:
                    print(f"! {n}")
                print_board(rows)
            else:
                at = stamp()
                for line in changes(prev, cur):
                    print(f"{at} {line}")
            sys.stdout.flush()
            prev = cur
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print()
        sys.exit(0)


def main():
    p = argparse.ArgumentParser(description="Attention board over Orca's live agent state.")
    p.add_argument("--json", action="store_true")
    p.add_argument("--repo", help="only this Orca repo (its display name)")
    p.add_argument("--stale-min", type=float, default=board_rules.STALE_MIN,
                   help="a working pane with no update for longer than this is stale (default 30)")
    p.add_argument("--watch", action="store_true")
    p.add_argument("--interval", type=float, default=20)
    p.add_argument("--write", action="store_true", help="also write <git-common-dir>/orca-flow/board.json")
    p.add_argument("--no-gh", action="store_true", help="skip gh; PRs come only from Orca's linked PR")
    a = p.parse_args()
    if a.interval <= 0:
        die("--interval must be positive")

    if a.watch:
        watch(a)
        return
    try:
        rows, notes = collect(a.stale_min, a.repo, use_gh=not a.no_gh)
    except OrcaError as e:
        die(str(e))
    if a.write:
        out = write_board(rows, notes)
        if not a.json:
            print(f"(written to {out})")
    if a.json:
        print(json.dumps(payload(rows, notes), ensure_ascii=False, indent=1))
        return
    for n in notes:
        print(f"! {n}")
    if not rows:
        print("no worktrees" + (f" in repo {a.repo}" if a.repo else ""))
        return
    print_board(rows)


if __name__ == "__main__":
    main()
