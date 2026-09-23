#!/usr/bin/env python3
"""Create an Orca worktree, start a Claude worker in it, and hand it the brief.

Why this is a script and not four commands in the skill text: managers used to start
workers three different ways, each parsing the JSON by hand. That produced KeyError:
'result' (there is no result when ok is false) and, worse, prompts sent before the TUI
was ready, which are simply swallowed. The steps never vary, so they belong in code.

The brief, its attachments, the worker rules and the test lock are copied into
<git-common-dir>/orca-flow/briefs/<name>/:
- not the scratchpad: it's wiped when a session ends, and the rules file is worth keeping
- not the working tree: it would dirty git status, which blocks clean-tree checks
- the git common dir is visible from every worktree, attachments included

Usage:
  spawn_worker.py --name <task-slug> --brief <brief.md> [--attach img ...] [--base <ref>]
                  [--comment text] [--model <name>] [--manager <session name>] [--bypass] [--dry-run]
  spawn_worker.py --name <task-slug> --continue [--note "..."] [--model <name>] [--bypass] [--dry-run]

--continue starts a fresh session in an existing worktree whose worker ran out of context
(or died). It reads the previous session's transcript from disk and writes a digest next to
the brief (handoff-digest.md), so the new worker starts from a page, not from the whole log.
If the old worker wrote its own handoff.md (the wrap-up rule), the new one reads that too.

The brief folder also gets a manager.json naming the session and terminal that started the
worker, so later sessions can tell who owns a package instead of guessing.

It waits for the TUI (up to ~6 minutes), so call it with a Bash timeout of 600000.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfgmod  # noqa: E402
import transcript  # noqa: E402

ORCA = os.environ.get("ORCA_CLI_COMMAND", "orca")
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESERVED = {"brief.md", "common.md", "handoff.md", "handoff-digest.md", "manager.json"}


def die(msg, **extra):
    print(json.dumps({"ok": False, "error": msg, **extra}, ensure_ascii=False, indent=1))
    sys.exit(1)


def sh(cmd, cwd=None):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        die(f"command failed: {shlex.join(cmd)}", stderr=r.stderr.strip()[-2000:])
    return r.stdout.strip()


def orca(*args, context=None):
    cmd = [ORCA, *args, "--json"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    extra = dict(context or {})
    try:
        data = json.loads(r.stdout)
    except ValueError:
        die(f"orca returned no JSON: {shlex.join(cmd)}", stdout=r.stdout[-2000:], stderr=r.stderr[-2000:], **extra)
    # Check ok before reading result: on ok:false there is no result key at all.
    if not data.get("ok"):
        die(f"orca reported failure: {shlex.join(cmd)}", response=data, **extra)
    return data.get("result") or {}


def find_key(obj, key):
    """First value named key anywhere in a nested JSON blob. Orca versions nest the
    terminal handle differently, so don't hard-code the path to it."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            found = find_key(v, key)
            if found is not None:
                return found
    return None


def brief_title(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("#").strip()
            if line:
                return line[:60]
    return ""


def atomic_copy(src, dst):
    # Another worker may be executing dst right now (bash reads a script as it runs);
    # overwriting in place would hand it half of the old file and half of the new one.
    tmp = f"{dst}.tmp-{os.getpid()}"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def bullets(items, indent="- "):
    return "".join(f"{indent}{i}\n" for i in items)


def render_rules(cfg, test_lock, base):
    """Fill the worker rules template from the project's config.

    Every project-specific line is optional. When a project hasn't configured how it's
    tested, the worker is told to work it out and run only what's relevant, rather than
    being handed a command that doesn't exist."""
    src = cfg["worker"].get("rules_file") or os.path.join(SKILL_DIR, "assets", "common.md")
    if not os.path.isabs(src):
        src = os.path.join(cfg["repo_root"] or SKILL_DIR, src)
    with open(src, encoding="utf-8") as f:
        text = f.read()

    w = cfg["worker"]
    state_file = cfg["merge_queue"].get("state_file")
    full = w.get("full_check_command")
    test_cmd = w.get("test_command")

    state_rule = ""
    if state_file:
        state_rule = (f"- **Don't edit `{state_file}`.** Every PR would touch the same lines and collide. "
                      f"The merge queue folds your PR description into it.\n")
    migration_rule = ""
    if w.get("migrations_dir"):
        d = w["migrations_dir"]
        migration_rule = (
            f"- Migrations in `{d}/` must be idempotent: assume the whole directory is replayed on "
            f"every boot. Take the next unused number, and check open PRs (`gh pr list`) as well as "
            f"`ls {d}` — another worker may have taken it without merging yet.\n")
    extra = bullets(w.get("extra_rules") or [])
    big_lines = int(w.get("big_file_lines") or 1500)
    big_rule = (f"- **Don't read a file of more than ~{big_lines} lines whole.** Find the functions you'll "
                f"touch and their callers (grep, then read those ranges). The brief lists entry points. A "
                f"whole read of a big file costs a large share of your context and you use almost none of it.\n")
    if w.get("big_files"):
        big_rule += "  Known big files here: " + ", ".join(f"`{f}`" for f in w["big_files"]) + ".\n"
    if state_file:
        state_rule += (f"- Read only the top of `{state_file}` (`head -n 80`), not the whole file. The rest "
                       f"is history.\n")

    if full:
        full_rule = (f"- **Don't run the full suite (`{full}`).** It runs once per batch in the merge "
                     f"queue, after merging. Several worktrees running it at once is what exhausts this "
                     f"machine's memory.\n")
    else:
        full_rule = "- Don't run the project's full test suite; the merge queue runs it once per batch.\n"
    checks_rule = ""
    if w.get("checks"):
        checks_rule = "- Run these before opening the PR:\n" + "".join(f"  - `{c}`\n" for c in w["checks"])
    if test_cmd:
        shown = test_cmd.replace("{files}", "<test files>")
        test_rule = (f"- Run only the tests related to what you changed, always through the test lock so "
                     f"runs queue instead of piling up:\n  ```\n  bash {test_lock} {shown}\n  ```\n"
                     f"  Queueing plus the run itself often exceeds the Bash tool's 2-minute default: use "
                     f"`run_in_background`, or a timeout of 600000.\n")
    else:
        test_rule = (f"- Find how this project runs a single test file and run only the tests related to "
                     f"what you changed. Wrap the run in the test lock so parallel worktrees queue up:\n"
                     f"  ```\n  bash {test_lock} <your test command>\n  ```\n")
    run_rule = ""
    if w.get("run_command"):
        run_rule = (f"- For a UI change, start the app (`{w['run_command']}`) and look at it in Orca's "
                    f"browser before writing that you verified it. If the port is taken, use another one; "
                    f"don't kill a process you didn't start.\n")

    out = (text
           .replace("{{PROJECT}}", cfg.get("project") or "this project")
           .replace("{{BASE}}", base)
           .replace("{{PR_BASE}}", base.removeprefix("origin/"))
           .replace("{{LANGUAGE}}", cfg.get("language") or "English")
           .replace("{{TEST_LOCK}}", test_lock)
           .replace("{{STATE_FILE_INLINE}}", f", plus updates to `{state_file}`," if state_file else "")
           .replace("{{STATE_FILE_RULE}}", state_rule)
           .replace("{{MIGRATION_RULE}}", migration_rule)
           .replace("{{BIG_FILE_RULE}}", big_rule)
           .replace("{{EXTRA_RULES}}", extra)
           .replace("{{FULL_CHECK_RULE}}", full_rule)
           .replace("{{CHECKS_RULE}}", checks_rule)
           .replace("{{TEST_RULE}}", test_rule)
           .replace("{{RUN_RULE}}", run_rule))
    return re.sub(r"\n{3,}", "\n\n", out)


def write_manager(brief_dir, session):
    """Who started this worker. Worker sessions, briefs and Orca cards don't name their
    manager, which is how two managers ended up driving the same PR."""
    rec = {"session": session, "terminal": os.environ.get("ORCA_TERMINAL_HANDLE"), "cwd": os.getcwd(),
           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with open(os.path.join(brief_dir, "manager.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
        f.write("\n")


def start_agent(wt_id, agent_cmd, prompt, base_info, title="worker"):
    term = orca("terminal", "create", "--worktree", f"id:{wt_id}", "--title", title, "--command", agent_cmd, context=base_info)
    handle = find_key(term, "handle")
    if not handle:
        die("terminal create returned no handle; the worktree exists, find it with orca terminal list", **base_info)
    base_info["terminal"] = handle
    # A prompt sent before the TUI is ready is lost. Only send on satisfied:true —
    # a timed-out wait still prints a normal-looking result, so output alone proves nothing.
    for timeout_ms in ("120000", "240000"):
        if find_key(orca("terminal", "wait", "--terminal", handle, "--for", "tui-idle", "--timeout-ms", timeout_ms, context=base_info), "satisfied") is True:
            break
    else:
        die("the worker's TUI never went idle, so the prompt was not sent. Go look at that terminal.", **base_info)
    receipt = orca("terminal", "send", "--terminal", handle, "--text", prompt, "--enter", "--wait-submit", "20",
                   context={**base_info, "warning": "this send failed to report back, but the prompt may have arrived. Read the terminal first; do not re-send."})
    print(json.dumps({
        "ok": True,
        **base_info,
        "accepted": find_key(receipt, "accepted"),
        "receipt": receipt,
        "note": "accepted without turn_started only means the start wasn't observed yet. Don't re-send.",
    }, ensure_ascii=False, indent=1))


def continue_worker(a, cfg, wt, brief_dir, brief_path, common_path, test_lock, agent_cmd, base, dry):
    """Fresh session, same worktree, same brief. The previous session's transcript is on
    disk; a digest of it (files edited, last messages, git state) goes next to the brief."""
    path = wt.get("path")
    if not os.path.isfile(brief_path):
        die(f"no brief at {brief_path}; this worktree wasn't started by spawn_worker.py. Pass --brief on a fresh spawn instead.")
    tfile = transcript.latest_transcript(path, cfg["worker"].get("transcripts_dir"))
    digest_path = os.path.join(brief_dir, "handoff-digest.md")
    handoff_path = os.path.join(brief_dir, "handoff.md")
    has_own = os.path.isfile(handoff_path)
    prompt = (
        f"You are continuing the \"{a.name}\" package after the previous worker session ended. First read "
        f"{common_path} (the rules), then {brief_path} (the package), then "
        + (f"{handoff_path} (the previous worker's own handoff) and " if has_own else "")
        + f"{digest_path} (a digest of what it did, generated from its transcript). Check the worktree's git "
        f"state yourself before trusting either. Don't redo finished work. "
        + (f"From the manager: {a.note} " if a.note else "")
        + "Then carry on to completion and report as the rules' last sections describe."
    )
    base_info = {"name": a.name, "worktree_id": wt.get("id"), "path": path, "brief_dir": brief_dir,
                 "continued": True, "previous_transcript": tfile, "handoff_md": has_own}
    if dry:
        print(json.dumps({"ok": True, "dry_run": True, **base_info,
                          "would_write": digest_path if tfile else None,
                          "commands": [shlex.join([ORCA, "terminal", "create", "--worktree", f"id:{wt.get('id')}", "--title", "worker (cont.)", "--command", agent_cmd, "--json"]),
                                       shlex.join([ORCA, "terminal", "send", "--terminal", "<handle>", "--text", prompt, "--enter", "--wait-submit", "20", "--json"])]},
                         ensure_ascii=False, indent=1))
        return
    if tfile:
        text = transcript.render_digest(transcript.digest(tfile), path, transcript.git_summary(path, base))
    else:
        text = ("# Handoff digest\n\nNo transcript was found for the previous session, so there is nothing to "
                "summarise. Work from the brief, handoff.md if present, and the git state:\n\n```\n"
                + transcript.git_summary(path, base) + "\n```\n")
    with open(digest_path, "w", encoding="utf-8") as f:
        f.write(text)
    # Re-render the rules: the config may have changed since the first spawn.
    with open(common_path, "w", encoding="utf-8") as f:
        f.write(render_rules(cfg, test_lock, base))
    write_manager(brief_dir, a.manager)
    orca("worktree", "set", "--worktree", f"id:{wt.get('id')}", "--comment", f"continued: {brief_title(brief_path)}", "--workspace-status", "in-progress", context=base_info)
    start_agent(wt.get("id"), agent_cmd, prompt, base_info, title="worker (cont.)")


def main():
    p = argparse.ArgumentParser(description="Create an Orca worktree and start a worker in it")
    p.add_argument("--name", required=True, help="lowercase letters, digits and dashes; the branch becomes <gitUsername>/<name>")
    p.add_argument("--brief", help="required unless --continue")
    p.add_argument("--continue", dest="cont", action="store_true", help="new session in an existing worktree, from the old session's transcript")
    p.add_argument("--note", help="with --continue: one line from you for the new worker (what to do first)")
    p.add_argument("--manager", help="your session name (ListAgents, read just now); recorded in manager.json")
    p.add_argument("--attach", nargs="*", default=[], help="screenshots and other files; copied next to the brief")
    p.add_argument("--base", help="defaults to the repo's base branch; pass one only to stack on another branch")
    p.add_argument("--comment", help="the Orca card's status line; defaults to the brief's title")
    p.add_argument("--model", help="agent model; defaults to worker.model in the config")
    p.add_argument("--agent-command", help="full command to start the agent; overrides --model")
    p.add_argument("--bypass", action="store_true", help="run the worker with bypassPermissions (only if this session is too)")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    dry = a.dry_run or os.environ.get("ORCA_FLOW_DRY_RUN") == "1"

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,48}", a.name):
        die("--name must be lowercase letters, digits and dashes (2-49 chars)")
    if a.cont:
        if a.brief or a.attach:
            die("--continue reuses the brief already in the brief folder; don't pass --brief or --attach")
    elif not a.brief:
        die("--brief is required (or --continue)")
    elif not os.path.isfile(a.brief) or os.path.getsize(a.brief) == 0:
        die(f"brief missing or empty: {a.brief}")
    names = [os.path.basename(f) for f in a.attach]
    for f, n in zip(a.attach, names):
        if not os.path.isfile(f):
            die(f"attachment not found: {f}")
        if n in RESERVED:
            die(f"an attachment may not be called {n} (it would overwrite the brief or the rules); rename it")
    if len(set(names)) != len(names):
        die("two attachments share a filename and would overwrite each other; rename them")

    cfg = cfgmod.load()
    if not cfg["repo_root"]:
        die("not inside a git repository")
    base = a.base or cfg.get("base_branch") or "origin/main"

    # Resolve the repo from where this skill lives, so it doesn't matter whether the
    # manager's cwd is the main checkout or some worktree.
    common_dir = sh(["git", "-C", cfg["repo_root"], "rev-parse", "--path-format=absolute", "--git-common-dir"])
    repo_root = cfg["repo_root"]

    repos = orca("repo", "list").get("repos", [])
    repo = next((r for r in repos if os.path.realpath(r.get("path", "")) == os.path.realpath(repo_root)), None)
    if not repo:
        die(f"Orca has no repo at {repo_root}. Add it in Orca first.")
    repo_id = repo["id"]

    existing = next((w for w in orca("worktree", "list", "--repo", f"id:{repo_id}").get("worktrees", [])
                     if os.path.basename(w.get("path", "")) == a.name or w.get("displayName") == a.name), None)
    if existing and not a.cont:
        die(f"a worktree called {a.name} already exists: {existing.get('path')}. Pick another name, message that worktree's terminal, or --continue it.")
    if a.cont and not existing:
        die(f"--continue needs an existing worktree called {a.name}; there is none")

    brief_dir = os.path.join(common_dir, "orca-flow", "briefs", a.name)
    bin_dir = os.path.join(common_dir, "orca-flow", "bin")
    test_lock = os.path.join(bin_dir, "test-lock.sh")
    brief_path = os.path.join(brief_dir, "brief.md")
    common_path = os.path.join(brief_dir, "common.md")

    model = a.model or cfg["worker"].get("model") or "opus"
    agent_cmd = a.agent_command or f"claude --model {model}"
    if a.bypass and not a.agent_command:
        agent_cmd += " --permission-mode bypassPermissions"
    # One line: multi-line text sent to a TUI can submit at the first newline.
    # Deliberately no "done" marker wording here: the prompt stays on screen, and a manager
    # grepping the terminal for that marker would read it as the worker having finished.
    prompt = (
        f"You are the worker for the \"{a.name}\" package. First read {common_path} (the rules for "
        f"every worker here), then {brief_path} (what this package is; screenshots are in the same "
        f"folder), then do it. Work straight through without checking back with me; handle product "
        f"calls the way the rules say. Finish by reporting as the last two sections describe."
    )
    if a.cont:
        continue_worker(a, cfg, existing, brief_dir, brief_path, common_path, test_lock, agent_cmd, base, dry)
        return

    create_args = ["worktree", "create", "--repo", f"id:{repo_id}", "--name", a.name, "--no-parent", "--setup", "run"]
    if a.base:
        create_args += ["--base-branch", a.base]
    create_args += ["--comment", a.comment or f"worker: {brief_title(a.brief)}"]

    if dry:
        print(json.dumps({
            "ok": True,
            "dry_run": True,
            "config_file": cfg["config_file"],
            "base": base,
            "copies": {
                a.brief: brief_path,
                **{f: os.path.join(brief_dir, n) for f, n in zip(a.attach, names)},
                "worker rules (rendered from config)": common_path,
                "scripts/test-lock.sh": test_lock,
            },
            "commands": [
                shlex.join([ORCA, *create_args, "--json"]),
                shlex.join([ORCA, "terminal", "create", "--worktree", "id:<worktree.id>", "--title", "worker", "--command", agent_cmd, "--json"]),
                shlex.join([ORCA, "terminal", "wait", "--terminal", "<handle>", "--for", "tui-idle", "--timeout-ms", "120000", "--json"]),
                shlex.join([ORCA, "terminal", "send", "--terminal", "<handle>", "--text", prompt, "--enter", "--wait-submit", "20", "--json"]),
            ],
            "rules_preview": render_rules(cfg, test_lock, base),
        }, ensure_ascii=False, indent=1))
        return

    # Move an old brief folder of the same name aside (its worktree is gone), so a new
    # worker never reads the previous round's screenshots.
    if os.path.isdir(brief_dir):
        os.replace(brief_dir, f"{brief_dir}.old-{time.strftime('%Y%m%dT%H%M%S')}")
    os.makedirs(brief_dir)
    os.makedirs(bin_dir, exist_ok=True)
    shutil.copyfile(a.brief, brief_path)
    for f, n in zip(a.attach, names):
        shutil.copy2(f, os.path.join(brief_dir, n))
    with open(common_path, "w", encoding="utf-8") as f:
        f.write(render_rules(cfg, test_lock, base))
    atomic_copy(os.path.join(SKILL_DIR, "scripts", "test-lock.sh"), test_lock)
    os.chmod(test_lock, 0o755)
    write_manager(brief_dir, a.manager)

    wt = orca(*create_args).get("worktree") or {}
    wt_id, wt_path = wt.get("id"), wt.get("path")
    if not wt_id:
        die("worktree create returned no worktree.id", worktree=wt)
    base_info = {"name": a.name, "worktree_id": wt_id, "path": wt_path,
                 "branch": (wt.get("branch") or "").removeprefix("refs/heads/"), "brief_dir": brief_dir}

    start_agent(wt_id, agent_cmd, prompt, base_info)


if __name__ == "__main__":
    main()
