#!/usr/bin/env python3
"""Start the merge queue session in a visible Orca terminal, or its successor on rotation.

Usage:
  spawn_queue.py [--model <m>] [--bypass|--no-bypass] [--replace] [--dry-run]

Why a script: starting a queue used to be four hand-typed orca commands, and rotation said
only "start the next one", so continuations happened ad hoc. One was resumed by hand with
`claude --resume` in a shell; Orca then reported its terminal orphaned (the process runs,
no pane shows it), and nobody could see the queue that was merging. This script always
starts a fresh session in a new Orca terminal in the queue worktree
(merge_queue.worktree_name, created if missing). A queue is never resumed: its state is the
handover files, so a fresh session has nothing to catch up on.

One queue at a time. The registered queue (handover.py queue show) is looked up in
`orca terminal list`:
- live, with a pane: refused, even with --replace. It is working; let it rotate itself.
- hidden (Orca calls the terminal orphaned: process alive, no pane), or unknown: refused
  unless --replace, because its process may still merge. --replace closes its terminal (its
  scrollback is lost, so ask the user first), retires it, then starts the new one.
- gone: retired ("terminal gone") and replaced, no --replace needed.
Any other agent terminal in the queue worktree counts as an unregistered queue (one that
hasn't run `queue start` yet, say) and is treated like a hidden one.

The prompt is one line, sent once after the TUI reports idle (spawn_worker.start_agent).
The new session registers itself with `handover.py queue start`.

It waits for the TUI (up to ~6 minutes), so call it with a Bash timeout of 600000.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfgmod  # noqa: E402
import handover  # noqa: E402
import managers  # noqa: E402
import spawn_manager  # noqa: E402
import spawn_worker  # noqa: E402
from spawn_worker import die, orca, start_agent  # noqa: E402

HANDOVER = os.path.join(spawn_worker.SKILL_DIR, "scripts", "handover.py")
TITLE = "merge-queue"


def show_terminal(handle):
    """The terminal's entry from `orca terminal show`, or None when Orca doesn't know it."""
    try:
        r = subprocess.run([spawn_worker.ORCA, "terminal", "show", "--terminal", handle, "--json"],
                           capture_output=True, text=True)
        data = json.loads(r.stdout)
    except (OSError, ValueError):
        return None
    if not data.get("ok"):
        return None
    res = data.get("result") or {}
    return res.get("terminal") if isinstance(res.get("terminal"), dict) else res


def classify(active, terminals, complete):
    """(state, why) for the registered queue, in the board's words (managers.terminal_state):
    live / hidden (orphaned: process alive, no pane) / gone, or unknown."""
    handle = active.get("terminal")
    if not handle:
        return "unknown", "the registration has no terminal handle, so Orca can't be asked about it"
    idx = managers.terminal_index(terminals)
    if handle not in idx and not complete:
        # A truncated list proves nothing; ask for the one handle.
        t = show_terminal(handle)
        if t is not None:
            idx[handle] = {"orphaned": bool(t.get("orphaned"))}
    state = managers.terminal_state(handle, idx)
    why = {"gone": "Orca no longer has its terminal",
           "hidden": "its terminal is orphaned in Orca (process alive, no pane), and it may still merge"}.get(state)
    return state, why


def main():
    p = argparse.ArgumentParser(description="Start the merge queue in a visible Orca terminal")
    p.add_argument("--model", help="agent model; defaults to merge_queue.model in the config")
    p.add_argument("--bypass", action="store_true", default=None,
                   help="run the queue with bypassPermissions (only if this session is too); default: worker.bypass_permissions")
    p.add_argument("--no-bypass", dest="bypass", action="store_false", help="override worker.bypass_permissions: true")
    p.add_argument("--replace", action="store_true",
                   help="close a hidden or unknown queue's terminal, retire it, and start a new one (ask the user first)")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    dry = a.dry_run or os.environ.get("ORCA_FLOW_DRY_RUN") == "1"

    cfg = cfgmod.load()
    if not cfg["repo_root"]:
        die("not inside a git repository")
    if not cfgmod.queue_enabled(cfg):
        die("this repo has no merge queue (merge_queue.enabled is false); managers merge PRs themselves")
    mq = cfg["merge_queue"]
    wt_name = mq.get("worktree_name") or "merge-queue"
    model = a.model or mq.get("model") or "opus"
    # Same rule as managers: the queue never runs on Fable, whatever the config says.
    if "fable" in model.lower():
        die(f"the merge queue doesn't run on Fable ({model}); pass another --model or change merge_queue.model")
    bypass = a.bypass if a.bypass is not None else cfg["worker"].get("bypass_permissions") is True
    agent_cmd = f"claude --model {model}" + (" --permission-mode bypassPermissions" if bypass else "")
    # One line: multi-line text sent to a TUI can submit at the first newline.
    prompt = ("You are this repo's merge queue. Use the orca-flow skill, read references/merge-queue.md and follow it. "
              f"First run: python3 {HANDOVER} queue start --session <your ListAgents name>, then handover.py list.")

    repo_root = cfg["repo_root"]
    repo = next((r for r in orca("repo", "list").get("repos", [])
                 if os.path.realpath(r.get("path", "")) == os.path.realpath(repo_root)), None)
    if not repo:
        die(f"Orca has no repo at {repo_root}. Add it in Orca first.")
    wt = next((w for w in orca("worktree", "list", "--repo", f"id:{repo['id']}").get("worktrees", [])
               if os.path.basename(w.get("path", "")) == wt_name or w.get("displayName") == wt_name), None)
    create_args = ["worktree", "create", "--repo", f"id:{repo['id']}", "--name", wt_name, "--no-parent", "--setup", "run",
                   "--comment", "merge queue"]

    terminals, complete = spawn_manager.live_terminals()
    if terminals is None:
        # Nothing can be closed or started without Orca, and "no queue" can't be proven.
        die("orca terminal list failed, so whether a queue is running is unknown; is Orca running?")

    common_dir = cfgmod.common_dir(repo_root)
    if not common_dir:
        die(f"no git common dir for {repo_root}")
    # With the common dir, read_state creates nothing: --dry-run must write nothing.
    active = handover.read_state(common_dir).get("active")
    state, why = classify(active, terminals, complete) if active else ("none", None)
    handle = (active or {}).get("terminal")
    # On rotation the caller is the retiring queue, in this same worktree; it stops once its
    # successor is up, so its own terminal is neither a rival nor something to close.
    own = os.environ.get("ORCA_TERMINAL_HANDLE")
    # An agent in the queue worktree that isn't the registered queue: a queue that hasn't
    # run `queue start` yet, or one resumed by hand. Two queues must never run at once.
    others = [t for t in terminals if wt and t.get("worktreeId") == wt.get("id")
              and t.get("handle") not in (handle, own) and t.get("agentIdentity")]

    queue = {"registered": active, "state": state}
    if why:
        queue["why"] = why
    if others:
        queue["other_agents"] = [{"handle": t.get("handle"), "title": t.get("title"), "orphaned": bool(t.get("orphaned"))}
                                 for t in others]

    if state == "live" and handle == own:
        die("you are the registered queue; run handover.py queue retire first, then spawn_queue.py", queue=queue)
    if state == "live":
        die(f"a queue is running: {active.get('session') or '?'}, {handle}. It rotates itself; don't start a second one.",
            queue=queue)
    blocking = state in ("hidden", "unknown") or others
    if blocking and not a.replace:
        what = []
        if state in ("hidden", "unknown"):
            what.append(f"the registered queue ({active.get('session') or '?'}, {handle or 'no handle'}) is {state}: {why}")
        if others:
            what.append(f"{len(others)} other agent terminal(s) run in the {wt_name} worktree with no queue registered for them")
        die("; ".join(what) + ". It must be stopped before another queue starts. Show the user, and with their OK "
            "run again with --replace (it closes those terminals; their scrollback is lost).", queue=queue)

    # What will be closed and retired, in order.
    close = [t.get("handle") for t in others]
    if state == "hidden" or (state == "unknown" and handle):
        close.insert(0, handle)
    retire = {"gone": "terminal gone", "hidden": "replaced by spawn_queue",
              "unknown": "replaced by spawn_queue"}.get(state)

    plan = {"worktree": {"name": wt_name, "id": (wt or {}).get("id"), "path": (wt or {}).get("path"),
                         "exists": bool(wt)},
            "queue": queue, "close": close, "retire": retire, "command": agent_cmd, "title": TITLE, "prompt": prompt}
    # Not a refusal: a queue with none of these still merges. The manager tells the user once
    # what it can't do, so nobody assumes a check or a deploy happened.
    missing = [k for k, v in (("worker.full_check_command", cfg["worker"].get("full_check_command")),
                              ("merge_queue.targets", mq.get("targets")),
                              ("merge_queue.state_file", mq.get("state_file"))) if not v]
    if missing:
        plan["unconfigured"] = missing
        plan["warning"] = (f"{', '.join(missing)} not set: the queue merges but "
                           + "; ".join(w for k, w in (("worker.full_check_command", "runs no full check"),
                                                      ("merge_queue.targets", "deploys nothing"),
                                                      ("merge_queue.state_file", "keeps no status file")) if k in missing)
                           + ". Tell the user and offer values for config.py set.")

    if dry:
        print(json.dumps({"ok": True, "dry_run": True, **plan,
                          "trust": f"would trust {(wt or {}).get('path') or '<worktree path>'} in {spawn_worker.claude_json_path()}",
                          "commands": ([] if wt else [shlex.join([spawn_worker.ORCA, *create_args, "--json"])])
                          + [shlex.join([spawn_worker.ORCA, "terminal", "close", "--terminal", h, "--json"]) for h in close]
                          + ([shlex.join([sys.executable, HANDOVER, "queue", "retire", "--reason", retire])] if retire else [])
                          + [shlex.join([spawn_worker.ORCA, "terminal", "create", "--worktree", f"id:{(wt or {}).get('id') or '<worktree.id>'}",
                                         "--title", TITLE, "--command", agent_cmd, "--json"]),
                             shlex.join([spawn_worker.ORCA, "terminal", "send", "--terminal", "<handle>", "--text", prompt,
                                         "--enter", "--wait-submit", "20", "--json"])]},
                         ensure_ascii=False, indent=1))
        return

    if not wt:
        wt = orca(*create_args).get("worktree") or {}
        if not wt.get("id"):
            die("worktree create returned no worktree.id", worktree=wt)
    plan["worktree"].update(id=wt.get("id"), path=wt.get("path"))
    base_info = {**plan}
    # Before terminal create: the agent reads trust once, at startup, and the trust dialog
    # eats the prompt.
    base_info["trust"] = spawn_worker.ensure_trusted(wt["path"]) if wt.get("path") else "skipped trust: no worktree path"

    for h in close:
        # "Only if it still exists": it may have exited since the list was read.
        if h in {t.get("handle") for t in spawn_manager.live_terminals()[0] or []} or show_terminal(h):
            orca("terminal", "close", "--terminal", h, context=base_info)
    if close:
        # A close that reports ok may still leave the process running (an orphaned terminal
        # is exactly one Orca has lost track of). Retiring it and starting another then means
        # two queues merging, so confirm each handle is gone before anything else changes.
        after, complete = spawn_manager.live_terminals()
        listed = None if after is None else {t.get("handle") for t in after}
        for h in close:
            if listed is None:
                still = True
            else:
                still = h in listed or (not complete and show_terminal(h) is not None)
            if still:
                die(f"close did not stop {h}; nothing was retired or started. Check `orca terminal list --json` "
                    f"for {h} (and, if it is still there, stop its claude process by hand, e.g. from Activity "
                    "Monitor or `ps`), then run spawn_queue.py again."
                    + (" (orca terminal list failed after the close, so it couldn't be confirmed.)" if listed is None else ""),
                    **base_info)
    if retire:
        r = subprocess.run([sys.executable, HANDOVER, "queue", "retire", "--reason", retire], capture_output=True, text=True)
        if r.returncode:
            die("handover.py queue retire failed; nothing was started", stdout=r.stdout[-1000:], stderr=r.stderr[-1000:],
                **base_info)
        base_info["retired"] = r.stdout.strip()

    start_agent(wt["id"], agent_cmd, prompt, base_info, title=TITLE, role="queue")


if __name__ == "__main__":
    main()
