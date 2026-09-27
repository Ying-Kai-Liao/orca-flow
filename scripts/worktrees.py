#!/usr/bin/env python3
"""State of this repo's Orca worktrees: inventory, one worker's status, what can be removed.

Why: cleanup used to mean writing a throwaway inventory script into the scratchpad, which
vanished with the session. And judging "idle" by whether the local branch is merged into the
base branch is wrong: a worktree can sit on the base branch with its PR still open and under
review. So this reads PR state, working-tree cleanliness and agent activity together. HEAD
always comes from git, never from Orca's cache, which lags behind the branch.

Usage:
  worktrees.py inventory [--json] [--no-fetch]   # one line per worktree grouped by manager, the merge queue, then: agents waiting on the user, open PRs nobody handed over
  worktrees.py status <name>          # one word: in-review / blocked / handoff / other workspaceStatus; for Monitor loops
  worktrees.py context [<name>]       # context estimate per worker session, flags the ones over worker.context_warn
  worktrees.py handoff <name>         # write briefs/<name>/handoff-digest.md from the worker's transcript and print it
  worktrees.py overlap <path...>      # open PRs / worktrees touching these paths, plus migration numbers already taken
  worktrees.py cleanup [--idle-hours N] [--no-fetch]   # N defaults to cleanup.idle_hours (3)
  worktrees.py cleanup --apply a,b [--dry-run]   # removes only the named ones that are still candidates
  worktrees.py cleanup --auto [--dry-run]        # removes every candidate and closes finished manager terminals

"Merged" is judged three ways, because a worktree's branch name is not reliable: the PR
found by branch name, else a PR whose head equals the worktree's HEAD, else HEAD already
being an ancestor of the base branch (a renamed branch or a detached HEAD after merge).
"Idle" uses the worktree's and its agents' own last-activity times, not the terminal's
last output: an open shell prompt repaints constantly and made every worktree look busy.

Kept regardless: the merge queue's worktree, anything in keep_worktrees, and $ORCA_FLOW_KEEP.

--auto acts without a list for the user: it removes exactly what `cleanup` would list as
REMOVABLE (the same decide()), and closes a manager's terminal only when
managers.safe_to_close() proves it finished and unimportant. Only for when the user asked
for it or cleanup.auto is true; see references/manager.md, Cleanup.
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
import handover as handovermod  # noqa: E402
import managers as mgrmod  # noqa: E402
import transcript  # noqa: E402

ORCA = os.environ.get("ORCA_CLI_COMMAND", "orca")
BUSY_STATES = {"working", "permission"}
CFG = cfgmod.load()
KEEP = set(CFG.get("keep_worktrees") or [])
BASE = CFG.get("base_branch") or "origin/main"
REMOTE = BASE.split("/")[0] if "/" in BASE else "origin"
CTX_WINDOW = int(CFG["worker"].get("context_window") or 200000)
# worker.context_warn may be a fraction of the window or a token count; compare in tokens.
CTX_WARN_TOKENS = cfgmod.context_warn_tokens(CFG)
HANDOFF = cfgmod.handoff_enabled(CFG)
TRANSCRIPTS = CFG["worker"].get("transcripts_dir")
BOARD = CFG.get("board") or {}
_idle = (CFG.get("cleanup") or {}).get("idle_hours")
IDLE_HOURS = float(3 if _idle is None else _idle)
HANDOFF_STATE = cfgmod.handoff_settings(CFG)["state_dir"]
# With no jev-handoff configured, inventory, context and cleanup behave exactly as before it
# existed: no hf column, no untracked-file exception.
HANDOFF_BIN = cfgmod.handoff_settings(CFG)["bin"]
# Untracked files spawn_worker.py itself puts into a worktree (jev-handoff's Stop hook, and the
# .bak install-hook keeps when the file already existed). Counting them as uncommitted work
# would keep every worker worktree out of cleanup forever. Only untracked ("??") entries are
# excused: a tracked settings file that changed is a real change.
OWN_UNTRACKED = {".claude/settings.local.json", ".claude/settings.local.json.bak"}


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


def dirty_count(path, own_hook=None):
    """Uncommitted changes in a worktree, or None if git status fails. With jev-handoff
    configured (own_hook, default: handoff.bin is set), the hook's settings file is excused,
    and --untracked-files=all is needed for that: a new untracked .claude/ folder would
    otherwise show as one "?? .claude/" line that can't be told apart from real work."""
    own_hook = bool(HANDOFF_BIN) if own_hook is None else own_hook
    code, out, _ = run(["git", "-C", path, "status", "--porcelain"] + (["--untracked-files=all"] if own_hook else []))
    if code:
        return None
    return len([l for l in out.splitlines()
                if l.strip() and not (own_hook and l.startswith("?? ") and l[3:].strip('"') in OWN_UNTRACKED)])


def handoff_of(tfile, state_dir=None):
    """The jev-handoff working set of the session that wrote tfile: {path, age_s}, or None.
    A file read only; jev-handoff itself is never called from here."""
    if not tfile:
        return None
    sid = os.path.splitext(os.path.basename(tfile))[0]
    p = os.path.join(state_dir or HANDOFF_STATE, sid, "handoff.md")
    try:
        return {"path": p, "age_s": int(time.time() - os.path.getmtime(p))}
    except OSError:
        return None


def collect(fetch):
    """(rows, notes, seen). seen keeps what collect already asked Orca and gh, for cleanup
    --auto: {"ps": `orca worktree ps` worktrees, "prs": gh's PR list, or None when gh failed}."""
    repo_root, repo_id = repo_context()
    notes = []
    if fetch:
        code, _, err = run(["git", "fetch", "--quiet", REMOTE], cwd=repo_root)
        if code:
            notes.append(f"git fetch failed, {BASE} may be stale: {err[-200:]}")

    worktrees = orca("worktree", "list", "--repo", f"id:{repo_id}").get("worktrees", [])
    ps_list = orca("worktree", "ps").get("worktrees", [])
    ps = {w.get("worktreeId"): w for w in ps_list}

    prs = {}
    code, out, err = run(["gh", "pr", "list", "--state", "all", "--limit", "200",
                          "--json", "number,state,headRefName,headRefOid,title"], cwd=repo_root)
    pr_ok = code == 0
    if not pr_ok:
        notes.append(f"gh pr list failed, PR state unknown (so nothing will be a cleanup candidate): {err[-200:]}")
    by_head = {}
    pr_list = json.loads(out) if pr_ok else None
    if pr_ok:
        for pr in pr_list:
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
            dirty = dirty_count(path)
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
                   and board_rules.classify(board_rules.make_row(p, ag, now_ms),
                                            stale_min=BOARD.get("stale_min", board_rules.STALE_MIN),
                                            question_max_min=BOARD.get("question_max_min", board_rules.QUESTION_MAX_MIN),
                                            phrases=BOARD.get("decision_phrases") or (),
                                            negations=BOARD.get("negations") or ())[0] == "needs_human"]
        ctx = handoff = None
        if exists:
            f = transcript.latest_transcript(path, TRANSCRIPTS)
            handoff = handoff_of(f) if HANDOFF_BIN else None
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
            **({"handoff": handoff} if HANDOFF_BIN else {}),
        })
    return rows, notes, {"ps": ps_list, "prs": pr_list}


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
    flag = "!" if c["tokens"] >= CTX_WARN_TOKENS else " "
    return f"ctx {c['tokens'] // 1000:>3}k {int(c['pct'] * 100):>3}%{flag}"


def fmt_hf(h):
    """Age of the current session's jev-handoff working set: 3m, 2h, 1d, or - when none."""
    if not h:
        return "hf -"
    s = max(0, h["age_s"])
    return "hf " + (f"{s // 60}m" if s < 3600 else f"{s // 3600}h" if s < 86400 else f"{s // 86400}d")


def hf_cell(r, on=None):
    """The hf column between ctx and what follows it, or the two spaces that were there
    before the column existed, so output without jev-handoff is unchanged."""
    on = bool(HANDOFF_BIN) if on is None else on
    return f" {fmt_hf(r.get('handoff')):<6} " if on else "  "


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


def pr_row(u, no_queue=False):
    """An unhanded_prs() entry as a board row with no agent pane, so classify decides it.
    unhanded_prs() already dropped drafts; the list only holds open PRs."""
    return {"pr": {"number": u["number"], "state": "OPEN", "isDraft": False}, "handover": u["handover"],
            "state": None, "comment": "", "no_queue": no_queue}


def live_terminals(notes):
    """managers.terminal_index of `orca terminal list`, or None with a note: unlike orca(),
    a failure here must not stop the inventory, it only leaves liveness unknown."""
    code, out, err = run([ORCA, "terminal", "list", "--limit", "1000", "--json"])
    try:
        data = json.loads(out)
    except ValueError:
        data = {}
    if code or not data.get("ok"):
        notes.append(f"orca terminal list failed, manager and queue terminals not checked: {(err or out)[-200:]}")
        return None
    return mgrmod.terminal_index((data.get("result") or {}).get("terminals"))


def pr_of_task(task, rows_by_name, pr_list):
    """The PR of a worker's task: its worktree row's PR while the worktree exists; after that,
    the newest PR whose branch is <task> or ends in /<task> (spawn_worker.py names branches
    <gitUsername>/<task>). None when nothing matches."""
    r = rows_by_name.get(task)
    if r is not None:
        return r["pr"]
    hits = [pr for pr in pr_list or [] if pr["headRefName"] == task or pr["headRefName"].endswith("/" + task)]
    return max(hits, key=lambda pr: pr["number"]) if hits else None


def pane_facts(m, terminals, ps_list, now_ms, no_queue):
    """{"busy", "attention", "reason"} for the agent in a manager's terminal, or None when Orca
    shows no agent in that pane. The pane row is built and classified the way board.py builds
    a manager's row, so auto-cleanup and the board can't disagree about it."""
    pane = ((terminals or {}).get(m.get("terminal")) or {}).get("pane")
    if not pane:
        return None
    for wt in ps_list or []:
        for ag in wt.get("agents") or []:
            if ag.get("paneKey") != pane:
                continue
            role = {"kind": "manager", "name": m.get("slug"), "status": m.get("status"),
                    "terminal": m.get("terminal"), "terminal_state": m.get("terminal_state")}
            row = board_rules.make_row(wt, ag, now_ms, no_queue=no_queue, role=role)
            att, why = board_rules.classify(row, stale_min=BOARD.get("stale_min", board_rules.STALE_MIN),
                                            question_max_min=BOARD.get("question_max_min", board_rules.QUESTION_MAX_MIN),
                                            phrases=BOARD.get("decision_phrases") or (),
                                            negations=BOARD.get("negations") or ())
            return {"busy": ag.get("state") in BUSY_STATES, "attention": att, "reason": why}
    return None


def read_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def terminal_tail(handle, limit=200):
    """The terminal's last lines as one string, or None. Only matched against secret patterns
    in memory; never printed or written anywhere."""
    code, out, _ = run([ORCA, "terminal", "read", "--terminal", handle, "--limit", str(limit), "--json"])
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if code or not data.get("ok"):
        return None
    tail = (data.get("result") or {}).get("tail")
    if isinstance(tail, list):
        return "\n".join(str(l) for l in tail)
    return tail if isinstance(tail, str) else None


def cmd_cleanup_auto(rows, seen, idle_hours, dry):
    """Remove every worktree decide() calls removable, then close every manager terminal
    managers.safe_to_close() allows. One line per action and per skip, with the reason.
    A failed orca call is reported and the rest continues: one stuck worktree must not
    leave every finished manager open."""
    for r in sorted(rows, key=lambda r: r["name"]):
        ok, why = decide(r, idle_hours)
        if not ok:
            print(f"skip    {r['name']}: {why}")
            continue
        cmd = [ORCA, "worktree", "rm", "--worktree", f"id:{r['id']}", "--force", "--json"]
        if dry:
            print(f"DRY-RUN would remove {r['name']} ({why}): {shlex.join(cmd)}")
            continue
        code, out, err = run(cmd)
        print(f"removed {r['name']} ({why})" if code == 0 else f"FAILED  remove {r['name']}: {(err or out)[-200:]}")

    repo_root, _ = repo_context()
    common = cfgmod.common_dir(repo_root)
    notes = []
    terminals = live_terminals(notes)
    for n in notes:
        print(f"! {n}")
    rows_by_name = {r["name"]: r for r in rows}
    tasks = sorted(set(rows_by_name) | set(mgrmod.brief_tasks(common)))
    managers, _ = mgrmod.assign(common, tasks, terminals, notes)
    own = os.environ.get("ORCA_TERMINAL_HANDLE")
    no_queue = not cfgmod.queue_enabled(CFG)
    now_ms = time.time() * 1000
    for m in managers:
        label = f"manager:{m['name']}"
        if m.get("recorded") and m.get("status") == mgrmod.CLOSED:
            continue
        worker_prs = None if seen["prs"] is None else {
            t: pr_of_task(t, rows_by_name, seen["prs"]) for t in m["workers"]}
        pane = pane_facts(m, terminals, seen["ps"], now_ms, no_queue)
        notes_text = read_file(os.path.join(common, "orca-flow", "managers", m["slug"], "notes.md")) \
            if m.get("recorded") else None
        # The tail is read only for a manager that passes everything else (an empty tail stands
        # in for it until then): reading every terminal would be slow and shows nothing new.
        tail = None
        if mgrmod.safe_to_close(m, worker_prs, pane, own, notes_text, "")[0]:
            tail = terminal_tail(m["terminal"])
        ok, why = mgrmod.safe_to_close(m, worker_prs, pane, own, notes_text, tail)
        if not ok:
            print(f"skip    {label}: {why}")
            continue
        cmd = [ORCA, "terminal", "close", "--terminal", m["terminal"], "--json"]
        if dry:
            print(f"DRY-RUN would close {label} terminal {m['terminal']} ({why}): {shlex.join(cmd)}")
            continue
        code, out, err = run(cmd)
        if code:
            print(f"FAILED  close {label}: {(err or out)[-200:]}")
            continue
        try:
            mgrmod.mark_closed(common, m["slug"])
        except (OSError, ValueError) as e:
            # The terminal is closed either way; without the mark the board will call it dead.
            print(f"closed  {label} terminal {m['terminal']} ({why}); "
                  f"FAILED to mark the record closed ({e.__class__.__name__}), the board may show it as dead")
            continue
        print(f"closed  {label} terminal {m['terminal']} ({why}); record marked closed")


def manager_word(m):
    """live / hidden / dead / gone / done / closed / ? for a manager header. dead is a
    managers/ record that never said done; gone is an interactive manager whose session
    ended, which is normal; closed is one cleanup --auto closed."""
    if m.get("recorded") and m.get("status") in mgrmod.FINISHED:
        return m["status"]
    state = m.get("terminal_state")
    if state is None:
        return "?"
    if state == "gone":
        return "dead" if mgrmod.is_dead(m) else "gone"
    return state


def manager_header(m):
    src = f"{m['source']}:{m['source_id'] or '-'}" if m.get("source") else "-"
    return (f"{m['name']}  {m.get('status') or '-'}  {src}  {manager_word(m)}  {m.get('terminal') or '-'}"
            + ("" if m["recorded"] else "  (no managers/ record)"))


def queue_line(common, terminals, notes):
    """(line, json) for the repo's registered merge queue, read with handover.py's own
    state reader. (None, None) when no queue is registered."""
    try:
        act = (handovermod.read_state(common) or {}).get("active") if common else None
    except (OSError, ValueError) as e:
        notes.append(f"queue/state.json unreadable ({e.__class__.__name__}); merge queue not shown")
        return None, None
    if not isinstance(act, dict):
        return None, None
    state = mgrmod.terminal_state(act.get("terminal"), terminals)
    word = state or "?"
    info = {"session": act.get("session"), "terminal": act.get("terminal"), "batches": act.get("batches", 0),
            "started_at": act.get("started_at"), "terminal_state": state}
    return (f"merge queue: {act.get('session') or '-'}  batches {info['batches']}  {word}  "
            f"{act.get('terminal') or '-'}"), info


def fmt_inventory_row(r, indent=""):
    pr = f"#{r['pr']['number']} {r['pr']['state']}" if r["pr"] else ("on-base" if r["on_base"] else "-")
    return (f"{indent}{r['name']:<32} {str(r['status']):<10} {str(r['workspace_status']):<12} PR {pr:<12} "
            f"ahead {str(r['ahead']):<3} dirty {str(r['dirty']):<3} idle {str(r['idle_hours']):<5}h {fmt_ctx(r['ctx'])}{hf_cell(r)}{r['comment']}")


def cmd_context(rows, name):
    for r in sorted(rows, key=lambda r: -(r["ctx"] or {}).get("pct", -1)):
        if name and r["name"] != name:
            continue
        c = r["ctx"]
        if not c:
            print(f"{r['name']:<32} no transcript found")
            continue
        print(f"{r['name']:<32} {fmt_ctx(c)}{hf_cell(r)}{c['turns']} turns, {c['compactions']} compaction(s)  {c['file']}")
    print(f"\nestimate against a {CTX_WINDOW // 1000}k window; '!' = over {CTX_WARN_TOKENS // 1000}k "
          f"({CTX_WARN_TOKENS * 100 // CTX_WINDOW}%, worker.context_warn).")
    if HANDOFF and cfgmod.handoff_settings(CFG)["bin"]:
        # The jev-handoff working set is already current, so no wrap-up line; and --continue
        # refuses while the old pane is still live, so close it first.
        print("Over the line (handoff.bin is set, send no wrap-up line): orca terminal close --terminal <handle> --json, "
              "then spawn_worker.py --name <task> --continue.")
    elif HANDOFF:
        msg = (CFG.get("handoff") or {}).get("wrap_up_message") or cfgmod.DEFAULTS["handoff"]["wrap_up_message"]
        print(f"Over the line: orca terminal send --terminal <handle> --text {shlex.quote(msg)} --enter --wait-submit 15 --json, "
              f"then spawn_worker.py --name <task> --continue.")
    else:
        print("handoff.enabled is false: flagged workers keep running and rely on the agent's own compaction.")


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
    how = cl.add_mutually_exclusive_group()
    how.add_argument("--apply", metavar="NAMES", help="comma-separated worktree names; removes only these")
    how.add_argument("--auto", action="store_true",
                     help="remove every candidate and close finished manager terminals, without asking")
    cl.add_argument("--idle-hours", type=float, default=IDLE_HOURS, help=f"default: cleanup.idle_hours ({IDLE_HOURS:g})")
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

    rows, notes, seen = collect(fetch=not a.no_fetch)
    for n in notes:
        print(f"! {n}")

    if a.cmd == "context":
        cmd_context(rows, a.name)
        return

    if a.cmd == "inventory":
        repo_root, _ = repo_context()
        unhanded = unhanded_prs(repo_root)
        no_queue = not cfgmod.queue_enabled(CFG)
        common = cfgmod.common_dir(repo_root)
        mnotes = []
        terminals = live_terminals(mnotes)
        managers, owner_of = mgrmod.assign(common, [r["name"] for r in rows], terminals, mnotes)
        for r in rows:
            r["manager"] = owner_of.get(r["name"])
        qline, qinfo = queue_line(common, terminals, mnotes)
        if a.json:
            print(json.dumps({"worktrees": rows, "open_prs": unhanded, "merge_queue": not no_queue,
                              "managers": managers, "queue": qinfo, "notes": notes + mnotes},
                             ensure_ascii=False, indent=1))
            return
        for n in mnotes:
            print(f"! {n}")
        ordered = sorted(rows, key=lambda r: r["name"])
        if not managers:
            # No manager anywhere: the same flat list as before managers existed.
            for r in ordered:
                print(fmt_inventory_row(r))
        else:
            for m in managers:
                print(f"\n{manager_header(m)}")
                mine = [r for r in ordered if r["manager"] == m["name"]]
                for r in mine:
                    print(fmt_inventory_row(r, "  "))
                if not mine:
                    print("  (no workers)")
            loose = [r for r in ordered if r["manager"] is None]
            if loose:
                print("\n(no manager)")
                for r in loose:
                    print(fmt_inventory_row(r, "  "))
        if qline:
            print(f"\n{qline}")
        waiting = [(r["name"], m) for r in rows for m in r["waiting"]]
        if waiting:
            print("\nWaiting on the user (an agent asked something and stopped):")
            for name, m in waiting:
                tail = m[-300:].replace("\n", " ")
                print(f"  {name}: …{tail}" if len(m) > 300 else f"  {name}: {tail}")
        if unhanded is None:
            print("\n! gh pr list failed; open PRs not checked")
        else:
            missing = [u for u in unhanded if board_rules.classify(pr_row(u, no_queue))[0] == "unhanded_pr"]
            if missing:
                print("\nOpen PRs with no handover file (the queue doesn't know about these):")
                for u in missing:
                    back = "  [sent back by the queue]" if u["handover"] else ""
                    print(f"  #{u['number']} {u['branch']}  {u['title'][:60]}  (updated {u['updated'][:10]}){back}")
            done = [u for u in unhanded if u["handover"] and u not in missing]
            if no_queue and unhanded:
                print("\nOpen PRs (no merge queue in this repo; review and merge them yourself): "
                      + ", ".join(f"#{u['number']} {u['branch']}" for u in unhanded))
            elif done:
                print("\nOpen PRs already handed over: " + ", ".join(f"#{u['number']} ({u['handover']})" for u in done))
        return

    dry = a.dry_run or os.environ.get("ORCA_FLOW_DRY_RUN") == "1"
    if a.auto:
        cmd_cleanup_auto(rows, seen, a.idle_hours, dry)
        return
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
