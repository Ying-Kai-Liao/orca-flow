#!/usr/bin/env python3
"""Hand a PR to the merge queue as a file, not a chat message.

Why: the handover used to be a one-line message addressed by session name. Session names
changed under managers, two managers handed the same PR to different queues, a SHA typed
from memory pointed at nothing, retired queue sessions kept receiving handovers, and
finished PRs sat for days because nobody sent the line. A file in the shared
<git-common-dir>/orca-flow/queue/ directory fixes all of those at once: the head is
copied from `gh pr view` by the script, the file names the manager, any queue session
(including a freshly started one) reads the same directory, and a PR without a file is
visibly un-handed in `worktrees.py inventory`.

The queue keeps a small state file too (queue/state.json): which session is the queue,
how many batches it has done, when it retired. A queue session's context grows with every
batch, so retiring at a fixed count is routine, and the next queue starts from the files
with nothing to catch up on.

Usage (manager):
  handover.py send <pr> [--pending "..."] [--verified "..."] [--after-deploy "..."] [--note "..."]
                   [--report-to <session name>] [--notify <terminal> | --no-notify] [--dry-run] [--force]
  handover.py status <pr>                   # what the queue has done with it (for Monitor loops: prints one word)
  handover.py retarget <pr> --report-to <session>             # after a crash/resume renamed your session
  handover.py retarget --all-from <old> --report-to <new>
Usage (queue):
  handover.py list [--all]                  # pending handovers in arrival order; warns when a rotation is due
  handover.py take <pr>                     # mark it as being merged by this session
  handover.py done <pr> --sha <short sha> [--report "..."] [--no-deploy] [--no-count]
  handover.py back <pr> --reason "..."      # sent back without merging
  handover.py queue start [--session <name>] [--terminal <handle>] | queue show | queue retire [--reason "..."]
  handover.py prune [--older-than 14d] [--apply]   # dry run by default; archives finished handovers and old log lines
"""
import argparse
import datetime
import json
import os
import re
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfgmod  # noqa: E402

ORCA = os.environ.get("ORCA_CLI_COMMAND", "orca")
CFG = cfgmod.load()
ROTATE_AFTER = int(CFG["merge_queue"].get("rotate_after") or 10)


def die(msg, **extra):
    print(json.dumps({"ok": False, "error": msg, **extra}, ensure_ascii=False, indent=1))
    sys.exit(1)


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd, cwd=None):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def queue_dir():
    cd = cfgmod.common_dir(CFG.get("repo_root"))
    if not cd:
        die("not inside a git repository")
    d = os.path.join(cd, "orca-flow", "queue")
    os.makedirs(d, exist_ok=True)
    return d


def path_for(pr):
    return os.path.join(queue_dir(), f"{int(pr)}.json")


def read(pr):
    p = path_for(pr)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def write(pr, data):
    p = path_for(pr)
    tmp = f"{p}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, p)  # atomic: the queue may be listing the directory right now
    with open(os.path.join(queue_dir(), "log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": now(), "pr": data["pr"], "status": data["status"], "head": data.get("head"),
                            "by": data.get("last_by")}, ensure_ascii=False) + "\n")


def state_path(common=None):
    # With common (another repo's git common dir): board.py reads every repo's queue and must
    # not create folders, which queue_dir() does.
    if common:
        return os.path.join(common, "orca-flow", "queue", "state.json")
    return os.path.join(queue_dir(), "state.json")


def read_state(common=None):
    p = state_path(common)
    if not os.path.isfile(p):
        return {"active": None, "retired": []}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def write_state(st):
    tmp = f"{state_path()}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, state_path())


def me(session=None, terminal=None):
    # ORCA_FLOW_SESSION is rarely set (90 of 114 real records had session: null), so the
    # terminal handle is often the only identity there is; who() shows it in that case.
    return {"session": session or os.environ.get("ORCA_FLOW_SESSION") or None,
            "terminal": terminal or os.environ.get("ORCA_TERMINAL_HANDLE") or None,
            "cwd": os.getcwd()}


def who(ident):
    """A person-readable name for a me() record: the session, else the terminal handle."""
    ident = ident or {}
    return ident.get("session") or ident.get("terminal") or "?"


def terminal_exists(handle):
    """True / False, or None when Orca can't be asked. Never raises: callers use it for
    courtesies (notify, stale flags) that must not fail the command they're part of."""
    def ask(*args):
        try:
            r = subprocess.run([ORCA, "terminal", *args, "--json"], capture_output=True, text=True, timeout=30)
            return json.loads(r.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    data = ask("list")
    if not data or not data.get("ok"):
        return None
    res = data.get("result") or {}
    if handle in {t.get("handle") for t in res.get("terminals") or [] if isinstance(t, dict)}:
        return True
    if not res.get("truncated"):
        return False
    # A truncated list proves nothing; ask for the one handle.
    shown = ask("show", "--terminal", handle)
    return bool(shown and shown.get("ok") and (shown.get("result") or {}).get("terminal", True))


def gh_pr(pr):
    code, out, err = run(["gh", "pr", "view", str(pr), "--json",
                          "number,title,headRefName,headRefOid,state,isDraft,files,url"], cwd=CFG.get("repo_root"))
    if code:
        die(f"gh pr view {pr} failed", stderr=err[-500:])
    return json.loads(out)


def migration_files(files):
    """Migration paths the PR touches. A file that already exists on the base branch is
    marked "(modified)": a comment fix in an old migration is not a new migration, but the
    queue should still look at it."""
    d = (CFG["worker"].get("migrations_dir") or "").rstrip("/")
    if not d:
        return []
    base = CFG.get("base_branch") or "origin/main"
    out = []
    for f in sorted(x["path"] for x in files or [] if x["path"].startswith(d + "/")):
        code, _, _ = run(["git", "cat-file", "-e", f"{base}:{f}"], cwd=CFG.get("repo_root"))
        out.append(f"{f} (modified)" if code == 0 else f)
    return out


def migration_number(path):
    m = re.match(r"(\d+)", os.path.basename(path))
    return int(m.group(1)) if m else None


def migration_clashes(pr, migration):
    """`! migration number ...` lines for the PR's new migrations whose number is already on the
    base branch or in another pending/taken handover. A warning, not a refusal: #189 reused
    063 from #186, and the only check ran at `worktrees.py overlap`, before the work existed."""
    new = [m for m in migration if not m.endswith(" (modified)")]
    if not new:
        return []
    d = (CFG["worker"].get("migrations_dir") or "").rstrip("/")
    base = CFG.get("base_branch") or "origin/main"
    used = {}  # number -> [where]
    code, out, _ = run(["git", "ls-tree", "--name-only", base, d + "/"], cwd=CFG.get("repo_root"))
    for f in out.splitlines() if code == 0 else []:
        n = migration_number(f)
        if n is not None:
            used.setdefault(n, []).append(f"{base}:{f}")
    for f in sorted(os.listdir(queue_dir())):
        if not (f.endswith(".json") and f[:-5].isdigit()) or int(f[:-5]) == int(pr):
            continue
        try:
            with open(os.path.join(queue_dir(), f), encoding="utf-8") as fh:
                other = json.load(fh)
        except (OSError, ValueError):
            continue
        if other.get("status") not in ("pending", "taken"):
            continue
        for m in other.get("migration") or []:
            n = migration_number(m.replace(" (modified)", ""))
            if n is not None:
                used.setdefault(n, []).append(f"PR #{other['pr']} ({other['status']}) {m}")
    out = []
    for m in new:
        n = migration_number(m)
        if n is not None and used.get(n):
            digits = re.match(r"\d+", os.path.basename(m)).group(0)
            out.append(f"! migration number {digits} ({m}) also used by {'; '.join(used[n])}. "
                       "Renumber it before the queue merges.")
    return out


def notify_target(a, st):
    """(handle, warning): the terminal to notify, or None with the reason why not.
    --notify wins; --no-notify turns it off; otherwise the active queue's terminal, but only
    if Orca still has it. Managers passing a retired queue's handle by hand left the new
    queue idle for 45 minutes (2026-10-08)."""
    if a.no_notify:
        return None, None
    if a.notify:
        return a.notify, None
    act = st.get("active")
    if not act:
        return None, None  # the "no queue registered" warning below covers it
    handle = act.get("terminal")
    if not handle:
        return None, (f"! the registered queue ({who(act)}) has no terminal handle, so it wasn't notified; "
                      "it sees the file at its next `handover.py list`.")
    exists = terminal_exists(handle)
    if exists is False:
        return None, (f"! the registered queue's terminal {handle} ({who(act)}) no longer exists in Orca, so nobody "
                      "will pick this up. Start a queue: python3 scripts/spawn_queue.py --dry-run, then without it "
                      "(it retires the gone one by itself).")
    # exists None: Orca couldn't be asked. Try anyway; a failed send only prints a warning.
    return handle, None


def one_line(h):
    mig = ", ".join(h["migration"]) if h["migration"] else "none"
    return (f"[merge-queue] PR #{h['pr']} {h['branch']} head {h['head']} | migration: {mig} | "
            f"decisions pending: {h.get('pending') or 'none'} | worker verified: {h.get('verified') or 'see PR'} | "
            f"after deploy check: {h.get('after_deploy') or 'none'} | {h.get('note') or h['title']} | "
            f"report to: {h.get('report_to') or 'handover file'}")


def cmd_send(a):
    # Before gh: a handover file in a repo with no queue is never read, and the manager would
    # wait on it forever.
    if not cfgmod.queue_enabled(CFG) and not a.force:
        method = CFG["merge_queue"].get("merge_method") or "squash"
        die("this repo has no merge queue (merge_queue.enabled is false); review and merge the PR yourself: "
            f"gh pr merge {a.pr} --{method} --delete-branch. Pass --force to write the handover anyway.")
    pr = gh_pr(a.pr)
    if pr["state"] != "OPEN":
        die(f"PR #{a.pr} is {pr['state']}, not OPEN")
    if pr.get("isDraft"):
        die(f"PR #{a.pr} is a draft; mark it ready first")
    existing = read(a.pr)
    h = {
        "pr": pr["number"], "title": pr["title"], "url": pr["url"], "branch": pr["headRefName"],
        "head": pr["headRefOid"], "migration": migration_files(pr.get("files")),
        "pending": a.pending, "verified": a.verified, "after_deploy": a.after_deploy, "note": a.note,
        "report_to": a.report_to, "manager": me(a.report_to), "sent_at": now(),
        "status": "pending", "last_by": me(a.report_to), "history": (existing or {}).get("history", []),
    }
    if existing:
        if existing["status"] in ("pending", "taken") and existing["head"] == h["head"]:
            print(f"already handed over at {existing['sent_at']} with the same head; nothing to do.")
            print(one_line(existing))
            return
        h["history"].append({k: existing.get(k) for k in ("head", "status", "sent_at", "deployed", "reason")})
    line = one_line(h)
    clashes = migration_clashes(a.pr, h["migration"])
    if a.dry_run or os.environ.get("ORCA_FLOW_DRY_RUN") == "1":
        print(json.dumps({"ok": True, "dry_run": True, "would_write": path_for(a.pr), "handover": h, "line": line,
                          "warnings": clashes}, ensure_ascii=False, indent=1))
        return
    write(a.pr, h)
    print(line)
    print(f"(written to {path_for(a.pr)})")
    for c in clashes:
        print(c)
    # The file is the record; everything below is a courtesy and must never fail the send.
    st = read_state()
    handle, warning = notify_target(a, st)
    if handle:
        try:
            code, out, err = run([ORCA, "terminal", "send", "--terminal", handle, "--text", line, "--enter",
                                  "--wait-submit", "15", "--json"])
        except OSError as e:
            code, out, err = 1, "", str(e)
        print(f"notified the queue terminal {handle}" if code == 0 else f"! notify failed: {err[-300:] or out[-300:]}")
    if warning:
        print(warning)
    if not st.get("active"):
        print("! no queue session is registered (queue/state.json). Start one: python3 scripts/spawn_queue.py "
              "(--dry-run first), or tell the user none is running.")


def cmd_status(a):
    h = read(a.pr)
    if not h:
        print("unhanded")
        return
    if a.json:
        print(json.dumps(h, ensure_ascii=False, indent=1))
        return
    print(h["status"] + (f" {h.get('deployed', '')}" if h["status"] == "done" else "") +
          (f" — {h.get('reason', '')}" if h["status"] == "returned" else ""))


def cmd_list(a):
    items = []
    for f in sorted(os.listdir(queue_dir())):
        if f.endswith(".json") and f[:-5].isdigit():
            with open(os.path.join(queue_dir(), f), encoding="utf-8") as fh:
                items.append(json.load(fh))
    items.sort(key=lambda h: h["sent_at"])
    shown = [h for h in items if a.all or h["status"] in ("pending", "taken")]
    for h in shown:
        head_note = ""
        if a.check and h["status"] in ("pending", "taken"):
            live = gh_pr(h["pr"])
            if live["state"] != "OPEN":
                head_note = f"  ! PR is {live['state']}"
            elif live["headRefOid"] != h["head"]:
                head_note = f"  ! head moved to {live['headRefOid'][:12]} (handed: {h['head'][:12]}); ask the manager"
        by = f"  by {who(h.get('last_by'))}" if h["status"] == "taken" else ""
        print(f"{h['status']:<9} #{h['pr']:<5} {h['sent_at']}  {h['branch']}  head {h['head'][:12]}  "
              f"migration: {', '.join(h['migration']) or 'none'}  pending: {h.get('pending') or 'none'}{by}{head_note}")
    if not shown:
        print("nothing pending.")
    st = read_state()
    act = st.get("active")
    if act:
        n = act.get("batches", 0)
        due = "  ROTATE: this queue has done its share; retire it and start a fresh one." if n >= ROTATE_AFTER else ""
        print(f"\nqueue: {who(act)} since {act['started_at']}, {n} batch(es) done{due}")
    else:
        print("\nqueue: none registered (handover.py queue start)")


def cmd_take(a):
    h = read(a.pr) or die(f"no handover for PR #{a.pr}")
    h["status"] = "taken"
    h["taken_at"] = now()
    h["last_by"] = me(a.session)
    write(a.pr, h)
    print(f"taken #{a.pr} head {h['head']}")


def cmd_done(a):
    h = read(a.pr) or die(f"no handover for PR #{a.pr}")
    h.update({"status": "done", "deployed": None if a.no_deploy else a.sha, "merged": a.sha, "report": a.report,
              "done_at": now(), "last_by": me(a.session)})
    write(a.pr, h)
    st = read_state()
    if st.get("active"):
        st["active"]["batches"] = st["active"].get("batches", 0) + (1 if a.count_batch else 0)
        write_state(st)
    what = f"merged {a.sha} (not deployed)" if a.no_deploy else f"merged and deployed {a.sha}"
    line = (f"[merge-queue] PR #{h['pr']} {what} | {a.report or ''} | "
            f"migration: {', '.join(h['migration']) or 'none'} | decisions pending: {h.get('pending') or 'none'}")
    print(line)
    if h.get("report_to"):
        print(f"(also send that line to {h['report_to']} if that session still exists; the file is the record either way)")


def cmd_back(a):
    h = read(a.pr) or die(f"no handover for PR #{a.pr}")
    h.update({"status": "returned", "reason": a.reason, "returned_at": now(), "last_by": me(a.session)})
    write(a.pr, h)
    print(f"[merge-queue] PR #{h['pr']} sent back: {a.reason}")


def cmd_retarget(a):
    """Point pending/taken handovers at a manager's new session name. After the 10-06 Orca
    crash every session was renamed and managers hand-edited queue/<pr>.json six times."""
    if (a.pr is None) == (a.all_from is None):
        die("give a PR number or --all-from <old session>, not both")
    if a.pr is not None:
        h = read(a.pr) or die(f"no handover for PR #{a.pr}")
        if h["status"] not in ("pending", "taken"):
            die(f"PR #{a.pr} is {h['status']}; only pending or taken handovers are retargeted")
        targets = [h]
    else:
        targets = []
        for f in sorted(os.listdir(queue_dir())):
            if f.endswith(".json") and f[:-5].isdigit():
                with open(os.path.join(queue_dir(), f), encoding="utf-8") as fh:
                    h = json.load(fh)
                if h["status"] in ("pending", "taken") and h.get("report_to") == a.all_from:
                    targets.append(h)
    for h in targets:
        old = h.get("report_to")
        if old == a.report_to:
            print(f"#{h['pr']} already reports to {a.report_to}")
            continue
        # History keeps the old name, so the log can still say who was told what.
        h.setdefault("history", []).append({"head": h["head"], "status": h["status"], "sent_at": h.get("sent_at"),
                                            "report_to": old, "retargeted_at": now()})
        h["report_to"] = a.report_to
        h["last_by"] = me(a.report_to)
        write(h["pr"], h)
        print(f"#{h['pr']} report_to {old or 'none'} -> {a.report_to}")
    if not targets:
        print(f"no pending or taken handover reports to {a.all_from}.")


def parse_age(text):
    m = re.fullmatch(r"(\d+)\s*d?", text.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"expected days like 14d, got {text!r}")
    return int(m.group(1))


def parse_at(text):
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def cmd_prune(a):
    """Archive finished handover files and old log lines. Dry run unless --apply. Pending and
    taken handovers are never touched; neither is anything whose date can't be read."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=a.older_than)
    d = queue_dir()
    files, keep_notes = [], []
    for f in sorted(os.listdir(d)):
        if not (f.endswith(".json") and f[:-5].isdigit()):
            continue
        with open(os.path.join(d, f), encoding="utf-8") as fh:
            h = json.load(fh)
        status = h.get("status")
        if status not in ("done", "returned"):
            continue
        at = parse_at(h.get("done_at") if status == "done" else h.get("returned_at")) or parse_at(h.get("sent_at"))
        if not at or at >= cutoff:
            continue
        if status == "returned":
            # A returned PR is still the manager's to re-send while it's open; only one that
            # GitHub closed or merged (superseded by another PR, say) is finished.
            # Needs a decision: whether old returned handovers of still-open PRs should go too.
            try:
                code, out, _ = run(["gh", "pr", "view", str(h["pr"]), "--json", "state"], cwd=CFG.get("repo_root"))
                state = json.loads(out).get("state") if code == 0 else None
            except (OSError, ValueError):
                state = None
            if state in (None, "OPEN"):
                keep_notes.append(f"kept #{h['pr']}: returned, PR is {state or 'unknown on GitHub'}")
                continue
        files.append((f, h, at))
    log_path = os.path.join(d, "log.jsonl")
    old_lines, new_lines = [], []
    if os.path.isfile(log_path):
        with open(log_path, encoding="utf-8") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                try:
                    at = parse_at(json.loads(raw).get("at"))
                except (ValueError, AttributeError):
                    at = None
                (old_lines if at and at < cutoff else new_lines).append((raw if raw.endswith("\n") else raw + "\n", at))
    for f, h, at in files:
        print(f"{'archive' if a.apply else 'would archive'} #{h['pr']} {h['status']} {at:%Y-%m-%d}")
    for n in keep_notes:
        print(n)
    print(f"{'archived' if a.apply else 'would archive'} {len(files)} handover file(s) and {len(old_lines)} log line(s) "
          f"older than {a.older_than}d" + ("" if a.apply else "; run with --apply to do it"))
    if not a.apply or (not files and not old_lines):
        return
    # Archive first, delete after: a crash in between leaves a duplicate, never a loss.
    by_month = {}
    for f, h, at in files:
        by_month.setdefault(f"{at:%Y-%m}", []).append({"kind": "handover", "archived_at": now(), "file": f, "data": h})
    for raw, at in old_lines:
        by_month.setdefault(f"{at:%Y-%m}", []).append({"kind": "log", "archived_at": now(), "data": json.loads(raw)})
    for month, rows in sorted(by_month.items()):
        with open(os.path.join(d, f"archive-{month}.jsonl"), "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    for f, _, _ in files:
        os.remove(os.path.join(d, f))
    if old_lines:
        tmp = f"{log_path}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(raw for raw, _ in new_lines)
        os.replace(tmp, log_path)


def registration_check(act):
    """{"stale": bool, "why": [...]} for the registered queue: its terminal gone from Orca, or
    its worktree removed. outer-convoy's state.json named a queue whose worktree was gone."""
    why = []
    handle = act.get("terminal")
    exists = terminal_exists(handle) if handle else None
    if handle and exists is False:
        why.append(f"terminal {handle} no longer exists in Orca")
    if act.get("cwd") and not os.path.isdir(act["cwd"]):
        why.append(f"worktree {act['cwd']} no longer exists")
    out = {"stale": bool(why), "why": why}
    if not handle:
        out["terminal"] = "unknown: the registration has no terminal handle"
    elif exists is None:
        out["terminal"] = "unknown: orca terminal list failed"
    return out


def cmd_queue(a):
    st = read_state()
    if a.action == "show":
        act = st.get("active")
        if act:
            # Shown, not saved: the file stays what `queue start` wrote.
            st = {**st, "active": {**act, "identity": who(act), **registration_check(act)}}
            if st["active"]["stale"]:
                st["hint"] = ("the registered queue is stale; python3 scripts/spawn_queue.py retires a gone one and "
                              "starts a fresh queue")
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return
    if a.action == "start":
        if st.get("active"):
            act = st["active"]
            if not a.force:
                die("a queue is already registered; retire it first, or --force if it's really gone", active=act)
            st["retired"].append({**act, "retired_at": now(), "reason": "replaced with --force"})
        # spawn_queue.py records the terminal it created, for a session started without
        # $ORCA_TERMINAL_HANDLE; without a handle the queue can't be looked up in Orca.
        spawned = st.pop("spawned", None) or {}
        terminal = a.terminal or os.environ.get("ORCA_TERMINAL_HANDLE") or spawned.get("terminal")
        st["active"] = {**me(a.session, terminal), "started_at": now(), "batches": 0}
        write_state(st)
        print(f"queue registered: {st['active']}")
        return
    if a.action == "retire":
        if not st.get("active"):
            print("no active queue registered.")
            return
        st["retired"].append({**st["active"], "retired_at": now(), "reason": a.reason})
        st["active"] = None
        write_state(st)
        pending = [f for f in os.listdir(queue_dir()) if f.endswith(".json") and f[:-5].isdigit()]
        print("queue retired. Pending handovers stay in the directory; the next queue picks them up with "
              f"`handover.py list`. ({len(pending)} handover file(s) present)")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("pr", type=int)
    s.add_argument("--pending", help="decisions the user still has to make (or 'none')")
    s.add_argument("--verified", help="what the worker ran, e.g. 'tsc ✅ lint ✅ foo.test.ts; full suite not run'")
    s.add_argument("--after-deploy", help="what to check after deploy")
    s.add_argument("--note", help="one line for the status file")
    s.add_argument("--report-to", help="your session name, from ListAgents read just now")
    s.add_argument("--notify", metavar="TERMINAL",
                   help="send the line to this terminal; default: the registered queue's terminal, if Orca still has it")
    s.add_argument("--no-notify", action="store_true", help="don't send the line to any terminal")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force", action="store_true", help="write the handover even though merge_queue.enabled is false")
    st = sub.add_parser("status")
    st.add_argument("pr", type=int)
    st.add_argument("--json", action="store_true")
    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true")
    ls.add_argument("--check", action="store_true", help="ask GitHub whether each pending head is still the PR's head")
    for name in ("take", "done", "back"):
        x = sub.add_parser(name)
        x.add_argument("pr", type=int)
        x.add_argument("--session", help="this queue session's name")
        if name == "done":
            x.add_argument("--sha", required=True)
            x.add_argument("--report", help="health / test count / backup, one line")
            x.add_argument("--no-count", dest="count_batch", action="store_false", help="don't count this as a batch (several PRs in one batch: count once)")
            x.add_argument("--no-deploy", action="store_true", help="merged and pushed only (no app change); the report says so")
        if name == "back":
            x.add_argument("--reason", required=True)
    q = sub.add_parser("queue")
    q.add_argument("action", choices=["start", "show", "retire"])
    q.add_argument("--session")
    q.add_argument("--terminal", help="start: this queue's Orca terminal handle (default: $ORCA_TERMINAL_HANDLE)")
    q.add_argument("--reason")
    q.add_argument("--force", action="store_true")
    r = sub.add_parser("retarget")
    r.add_argument("pr", type=int, nargs="?")
    r.add_argument("--all-from", metavar="OLD_SESSION", help="every pending/taken handover reporting to this session")
    r.add_argument("--report-to", required=True, help="your session name now, from ListAgents read just now")
    pr_ = sub.add_parser("prune")
    pr_.add_argument("--older-than", type=parse_age, default=14, metavar="DAYS", help="e.g. 14d (default)")
    pr_.add_argument("--apply", action="store_true", help="archive and delete; without it, only list")
    a = p.parse_args()
    {"send": cmd_send, "status": cmd_status, "list": cmd_list, "take": cmd_take, "done": cmd_done,
     "back": cmd_back, "queue": cmd_queue, "retarget": cmd_retarget, "prune": cmd_prune}[a.cmd](a)


if __name__ == "__main__":
    main()
