#!/usr/bin/env python3
"""Start one manager session in the main checkout for one task, and record it.

Usage:
  spawn_manager.py --name <slug> --brief <file> [--source <name> --source-id <id>] [--dispatcher <session>]
                   [--model <m>] [--bypass|--no-bypass] [--force] [--dry-run]
  spawn_manager.py --list [--json]

A dispatcher starts one manager per task; the manager then runs orca-flow's manager flow
(briefs, workers, handover). Its state lives in <git-common-dir>/orca-flow/managers/<slug>/:
brief.md (a copy of --brief), notes.md (the manager's own log, never overwritten, so a
successor can take over from it) and manager.json (who, where, which terminal, status).
The board reads manager.json, so its shape is a contract; see the fields in record().

The manager runs in the main checkout, not a worktree: it only writes briefs and starts
workers, and the main checkout is the one place every worktree's state is visible from.

The prompt is one line, sent once, only after the TUI reports idle (the same start_agent
spawn_worker.py uses). Never re-send it by hand without reading the terminal first.

One live manager per slug, and per --source + --source-id: a second one is refused (exit 1,
with the existing record) unless --force. "Live" means its status isn't done and Orca still
has its terminal; a record that isn't live is replaced without --force.

It waits for the TUI (up to ~6 minutes), so call it with a Bash timeout of 600000.
"""
import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfgmod  # noqa: E402
import spawn_worker  # noqa: E402
from spawn_worker import die, start_agent  # noqa: E402


def managers_root(common_dir):
    return os.path.join(common_dir, "orca-flow", "managers")


def read_record(slug_dir):
    """(record, None) or (None, why it couldn't be read)."""
    path = os.path.join(slug_dir, "manager.json")
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
    except FileNotFoundError:
        return None, "no manager.json"
    except (OSError, ValueError) as e:
        return None, f"unreadable manager.json ({e.__class__.__name__})"
    if not isinstance(rec, dict):
        return None, "manager.json is not a JSON object"
    return rec, None


def write_record(path, rec):
    # The board reads these files while managers run; tmp + rename means it never sees half a file.
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def all_records(root):
    """[(slug, record or None, note or None)] for every folder under managers/."""
    out = []
    for slug in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        d = os.path.join(root, slug)
        if os.path.isdir(d):
            rec, why = read_record(d)
            out.append((slug, rec, why))
    return out


def live_terminals():
    """(terminals Orca reports, complete?) or (None, False) when orca can't be asked.
    Not spawn_worker.orca(): that exits on failure, and --list must still print.
    spawn_queue.py reads the entries themselves, for their orphaned flag and worktree."""
    try:
        r = subprocess.run([spawn_worker.ORCA, "terminal", "list", "--json"], capture_output=True, text=True)
        data = json.loads(r.stdout)
    except (OSError, ValueError):
        return None, False
    if not data.get("ok"):
        return None, False
    res = data.get("result") or {}
    return [t for t in res.get("terminals") or [] if isinstance(t, dict)], not res.get("truncated")


def live_handles():
    """(handles Orca reports, complete?) or (None, False) when orca can't be asked."""
    terminals, complete = live_terminals()
    if terminals is None:
        return None, False
    return {t.get("handle") for t in terminals}, complete


def is_live(rec, handles, complete):
    """True / False, or None when Orca couldn't say (not running, or a truncated list)."""
    if rec.get("status") == "done" or not rec.get("terminal"):
        return False
    if handles is None:
        return None
    if rec["terminal"] in handles:
        return True
    return False if complete else None


def list_managers(root, as_json):
    handles, complete = live_handles()
    rows = []
    for slug, rec, why in all_records(root):
        if rec is None:
            rows.append({"slug": slug, "live": None, "note": why})
        else:
            rows.append({**rec, "slug": rec.get("slug") or slug, "live": is_live(rec, handles, complete)})
    if as_json:
        out = {"ok": True, "managers": rows}
        if handles is None:
            out["note"] = "orca terminal list failed; live is null"
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    if not rows:
        print(f"no managers under {root}")
        return
    yn = {True: "yes", False: "no", None: "?"}
    for r in rows:
        if "note" in r and "status" not in r:
            print(f"{r['slug']}  ({r['note']})")
            continue
        src = f"{r.get('source')}:{r.get('source_id')}" if r.get("source") else "-"
        print(f"{r['slug']}  {src}  {r.get('status')}  live:{yn[r['live']]}  "
              f"terminal:{r.get('terminal') or '-'}  session:{r.get('session') or '-'}  started:{r.get('started_at') or '-'}")
    if handles is None:
        print("(orca terminal list failed, so live is unknown)")


def conflicts(root, slug, source, source_id):
    """(refusals, replaced): records that block this spawn, and this slug's old record if it
    can simply be replaced. A record whose liveness is unknown blocks: two managers on one
    task is worse than a refusal that --force gets past."""
    handles, complete = live_handles()
    refusals, replaced = [], None
    for other, rec, why in all_records(root):
        if other == slug:
            if rec is None:
                if why != "no manager.json":
                    refusals.append({"slug": slug, "reason": f"this slug has an {why}; look at it first"})
                continue
            live = is_live(rec, handles, complete)
            if live is False:
                replaced = rec
            else:
                refusals.append({"slug": slug, "live": live, "reason": "this slug already has a live manager"
                                 if live else "this slug has a manager and Orca couldn't say whether its terminal is live",
                                 "existing": rec})
        elif rec is not None and source and source_id is not None \
                and rec.get("source") == source and str(rec.get("source_id")) == str(source_id):
            live = is_live(rec, handles, complete)
            if live is not False:
                refusals.append({"slug": other, "live": live, "reason": f"{source}:{source_id} already has a live manager under "
                                 f"another slug" if live else f"{source}:{source_id} has a manager under another slug and "
                                 "Orca couldn't say whether its terminal is live", "existing": rec})
    return refusals, replaced


def main():
    p = argparse.ArgumentParser(description="Start a manager session in the main checkout")
    p.add_argument("--name", help="lowercase letters, digits and dashes; names managers/<slug>/ and the terminal")
    p.add_argument("--brief", help="what this manager is to do; copied to managers/<slug>/brief.md")
    p.add_argument("--source", help="where the task came from (a key of `sources` in the config)")
    p.add_argument("--source-id", help="the task's id in that source; one live manager per source + id")
    p.add_argument("--dispatcher", help="your session name (ListAgents), recorded in manager.json")
    p.add_argument("--model", help="agent model; defaults to manager.model in the config")
    p.add_argument("--bypass", action="store_true", default=None,
                   help="run the manager with bypassPermissions (only if this session is too); default: manager.bypass_permissions")
    p.add_argument("--no-bypass", dest="bypass", action="store_false", help="override manager.bypass_permissions: true")
    p.add_argument("--force", action="store_true", help="start even if a live manager has this slug or source id")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--list", action="store_true", help="the recorded managers, one per line")
    p.add_argument("--json", action="store_true", help="with --list: JSON, with a live field per record")
    a = p.parse_args()
    dry = a.dry_run or os.environ.get("ORCA_FLOW_DRY_RUN") == "1"

    # Same resolution as spawn_worker (config.repo_root): the skill's own project when it is
    # installed inside one, the caller's cwd when it is a standalone clone.
    cfg = cfgmod.load()
    if not cfg["repo_root"]:
        die("not inside a git repository")
    common_dir = cfgmod.common_dir(cfg["repo_root"])
    if not common_dir:
        die(f"no git common dir for {cfg['repo_root']}")
    # The common dir's parent, not repo_root: $ORCA_FLOW_REPO or the cwd may be a linked worktree.
    main_checkout = os.path.dirname(common_dir)
    root = managers_root(common_dir)

    if a.list:
        list_managers(root, a.json)
        return

    if not a.name or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,48}", a.name):
        die("--name must be lowercase letters, digits and dashes (2-49 chars)")
    if not a.brief:
        die("--brief is required")
    if not os.path.isfile(a.brief) or os.path.getsize(a.brief) == 0:
        die(f"brief missing or empty: {a.brief}")
    if a.source_id is not None and not a.source:
        die("--source-id needs --source")
    sources = cfg.get("sources") if isinstance(cfg.get("sources"), dict) else {}
    if a.source and sources and a.source not in sources:
        die(f"unknown source {a.source}; the config's sources are: {', '.join(sorted(sources))}")

    mcfg = cfg.get("manager") or {}
    model = a.model or mcfg.get("model") or "opus"
    # Decided with the user: managers never run on Fable, whatever the config says.
    if "fable" in model.lower():
        die(f"managers don't run on Fable ({model}); pass another --model or change manager.model")
    bypass = a.bypass if a.bypass is not None else mcfg.get("bypass_permissions") is True
    agent_cmd = f"claude --model {model}" + (" --permission-mode bypassPermissions" if bypass else "")

    slug_dir = os.path.join(root, a.name)
    record_path = os.path.join(slug_dir, "manager.json")
    brief_path = os.path.join(slug_dir, "brief.md")
    notes_path = os.path.join(slug_dir, "notes.md")
    title = f"manager:{a.name}"
    # One line: multi-line text sent to a TUI can submit at the first newline.
    prompt = (f"You are the orca-flow manager \"{a.name}\". Read {brief_path}, then load the orca-flow skill and "
              f"follow references/manager.md, section \"If spawn_manager.py started you\". Keep progress in {notes_path}.")

    refusals, replaced = conflicts(root, a.name, a.source, a.source_id)
    if refusals and not a.force:
        die("a manager for this task already exists; check it before starting another (--force overrides)",
            existing=refusals)

    plan = {"name": a.name, "dir": slug_dir, "main_checkout": main_checkout, "command": agent_cmd,
            "title": title, "prompt": prompt}
    if replaced:
        plan["replaced"] = {"note": "the previous record wasn't live (done, or its terminal is gone), so it is replaced; "
                                    "notes.md is kept", "previous": replaced}
    if refusals:
        plan["forced_past"] = refusals
        plan["warning"] = "--force: the other manager's terminal is still open; close it if it shouldn't keep working"

    if dry:
        print(json.dumps({"ok": True, "dry_run": True, **plan,
                          "trust": f"would trust {main_checkout} in {spawn_worker.claude_json_path()}",
                          "copies": {a.brief: brief_path},
                          "notes": notes_path if not os.path.exists(notes_path) else f"{notes_path} (exists, kept)",
                          "commands": [
                              shlex.join([spawn_worker.ORCA, "terminal", "create", "--worktree", f"path:{main_checkout}",
                                          "--title", title, "--command", agent_cmd, "--json"]),
                              shlex.join([spawn_worker.ORCA, "terminal", "wait", "--terminal", "<handle>", "--for", "tui-idle",
                                          "--timeout-ms", "120000", "--json"]),
                              shlex.join([spawn_worker.ORCA, "terminal", "send", "--terminal", "<handle>", "--text", prompt,
                                          "--enter", "--wait-submit", "20", "--json"]),
                          ]}, ensure_ascii=False, indent=1))
        return

    os.makedirs(slug_dir, exist_ok=True)
    if os.path.realpath(a.brief) != os.path.realpath(brief_path):
        shutil.copyfile(a.brief, brief_path)
    if not os.path.exists(notes_path):
        with open(notes_path, "w", encoding="utf-8") as f:
            f.write(f"# Manager notes: {a.name}\n")

    rec = {"slug": a.name, "source": a.source or None, "source_id": a.source_id, "terminal": None,
           "session": None, "model": model, "brief": brief_path, "notes": notes_path,
           "main_checkout": main_checkout,
           "started_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
           "dispatcher": {"session": a.dispatcher, "terminal": os.environ.get("ORCA_TERMINAL_HANDLE")},
           "status": "starting"}
    # Written before terminal create, so a spawn that dies half-way still leaves a trace;
    # with no terminal it isn't live, and the next run replaces it without --force.
    write_record(record_path, rec)

    def created(handle):
        rec["terminal"] = handle
        write_record(record_path, rec)

    def sent():
        rec["status"] = "running"
        write_record(record_path, rec)

    base_info = {**plan, "record": record_path}
    # Before terminal create: the agent reads trust once, at startup, and the trust dialog
    # eats the prompt. Usually a no-op, the main checkout is where the user works.
    base_info["trust"] = spawn_worker.ensure_trusted(main_checkout)
    start_agent(None, agent_cmd, prompt, base_info, title=title, selector=f"path:{main_checkout}", role="manager",
                on_created=created, on_sent=sent)


if __name__ == "__main__":
    main()
