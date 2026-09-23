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
                   [--report-to <session name>] [--notify <queue terminal handle>] [--dry-run] [--force]
  handover.py status <pr>                   # what the queue has done with it (for Monitor loops: prints one word)
Usage (queue):
  handover.py list [--all]                  # pending handovers in arrival order; warns when a rotation is due
  handover.py take <pr>                     # mark it as being merged by this session
  handover.py done <pr> --sha <short sha> [--report "..."] [--no-deploy] [--no-count]
  handover.py back <pr> --reason "..."      # sent back without merging
  handover.py queue start [--session <name>] | queue show | queue retire [--reason "..."]
"""
import argparse
import datetime
import json
import os
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


def state_path():
    return os.path.join(queue_dir(), "state.json")


def read_state():
    if not os.path.isfile(state_path()):
        return {"active": None, "retired": []}
    with open(state_path(), encoding="utf-8") as f:
        return json.load(f)


def write_state(st):
    tmp = f"{state_path()}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, state_path())


def me(session=None):
    return {"session": session or os.environ.get("ORCA_FLOW_SESSION") or None,
            "terminal": os.environ.get("ORCA_TERMINAL_HANDLE") or None,
            "cwd": os.getcwd()}


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
            f"gh pr merge {a.pr} --{method}. Pass --force to write the handover anyway.")
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
    if a.dry_run or os.environ.get("ORCA_FLOW_DRY_RUN") == "1":
        print(json.dumps({"ok": True, "dry_run": True, "would_write": path_for(a.pr), "handover": h, "line": line},
                         ensure_ascii=False, indent=1))
        return
    write(a.pr, h)
    print(line)
    print(f"(written to {path_for(a.pr)})")
    if a.notify:
        code, out, err = run([ORCA, "terminal", "send", "--terminal", a.notify, "--text", line, "--enter",
                              "--wait-submit", "15", "--json"])
        print("notified the queue terminal" if code == 0 else f"! notify failed: {err[-300:] or out[-300:]}")
    st = read_state()
    if not st.get("active"):
        print("! no queue session is registered (queue/state.json). Start one, or tell the user none is running.")


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
        print(f"{h['status']:<9} #{h['pr']:<5} {h['sent_at']}  {h['branch']}  head {h['head'][:12]}  "
              f"migration: {', '.join(h['migration']) or 'none'}  pending: {h.get('pending') or 'none'}{head_note}")
    if not shown:
        print("nothing pending.")
    st = read_state()
    act = st.get("active")
    if act:
        n = act.get("batches", 0)
        due = "  ROTATE: this queue has done its share; retire it and start a fresh one." if n >= ROTATE_AFTER else ""
        print(f"\nqueue: {act.get('session') or act.get('terminal') or '?'} since {act['started_at']}, {n} batch(es) done{due}")
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


def cmd_queue(a):
    st = read_state()
    if a.action == "show":
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return
    if a.action == "start":
        if st.get("active"):
            act = st["active"]
            if not a.force:
                die("a queue is already registered; retire it first, or --force if it's really gone", active=act)
            st["retired"].append({**act, "retired_at": now(), "reason": "replaced with --force"})
        st["active"] = {**me(a.session), "started_at": now(), "batches": 0}
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
    s.add_argument("--notify", metavar="TERMINAL", help="also send the line to the queue's terminal handle")
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
    q.add_argument("--reason")
    q.add_argument("--force", action="store_true")
    a = p.parse_args()
    {"send": cmd_send, "status": cmd_status, "list": cmd_list, "take": cmd_take, "done": cmd_done,
     "back": cmd_back, "queue": cmd_queue}[a.cmd](a)


if __name__ == "__main__":
    main()
