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
  spawn_worker.py --name <task-slug> --continue [--note "..."] [--model <name>] [--bypass] [--force] [--dry-run]

--continue starts a fresh session in an existing worktree whose worker ran out of context
(or died). It reads the previous session's transcript from disk and writes a digest next to
the brief (handoff-digest.md), so the new worker starts from a page, not from the whole log.
If the old worker wrote its own handoff.md (the wrap-up rule), the new one reads that too.
With handoff.bin set, the Stop hook of jev-handoff is installed into every worker
worktree, and --continue names the previous session's working set before everything else.
--continue refuses (exit 2) while Orca still reports an agent pane in the worktree; close the
old terminal first, or pass --force.

The brief folder also gets a manager.json naming the session and terminal that started the
worker, so later sessions can tell who owns a package instead of guessing.

Before starting the agent it marks the worktree path as trusted in ~/.claude.json (a backup
goes to ~/.claude.json.orca-flow.bak), and after sending it reads the terminal back: exit code 2
means the agent had already quit and the prompt reached nobody.

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
RESERVED = {"brief.md", "common.md", "handoff.md", "handoff-digest.md", "manager.json", "continues.txt"}


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
    handoff_rule = render_handoff(cfg)
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
           .replace("{{RUN_RULE}}", run_rule)
           .replace("{{HANDOFF_RULE}}", handoff_rule))
    return re.sub(r"\n{3,}", "\n\n", out)


def render_handoff(cfg):
    """The "Handing off and continuing" section. With handoff.enabled false the manager never
    wraps a worker up, so only the part about being a continued session stays: --continue
    still restarts a worker that died."""
    fresh = ("If **you** are a fresh session continuing someone else's work, the prompt that started "
             "you said so: read the handoff files it named, check `git status` and `git log` yourself "
             "before trusting them, and don't redo finished work.\n")
    fresh += working_set_rule(cfg)
    if not cfgmod.handoff_enabled(cfg):
        return "## Continuing\n\n" + fresh
    msg = (cfg.get("handoff") or {}).get("wrap_up_message") or cfgmod.DEFAULTS["handoff"]["wrap_up_message"]
    lead = msg.split(":")[0].strip() if ":" in msg[:30] else msg[:30]
    return (
        "## Handing off and continuing\n\n"
        "Your context is finite and you can't see how full it is; the manager can. If the manager "
        f"sends you a line starting with `{lead}`, stop building and, in this order:\n\n"
        "1. Commit what you have, even if unfinished (`WIP:` prefix in the message), and push.\n"
        "2. Write `handoff.md` next to the brief: what's done, what's left (as a checklist), which\n"
        "   functions you touched, any decision you made and why, anything the next session must not\n"
        "   redo. Keep it under a page.\n"
        "3. `orca worktree set --worktree active --comment \"HANDOFF: <one line>\" --json`, then stop.\n\n"
        "A fresh session continues from the brief plus your `handoff.md`. " + fresh)


def working_set_rule(cfg):
    """The worker-rules paragraph on the jev-handoff working set; empty when handoff.bin is
    unset, so repos without jev-handoff get the same rules as before."""
    if not cfgmod.handoff_settings(cfg)["bin"]:
        return ""
    return ("\nIf your start prompt named a jev-handoff working set (a `handoff.md` under the jev-handoff "
            "state folder, not the one next to the brief), it is the record of the previous session, "
            "including what the user actually said. Check it before asking the user anything the previous "
            "session may already have asked. Don't paste it into your replies.\n")


def read_continues(brief_dir):
    """How many times this package has been continued so far. Kept in continues.txt next to
    the brief, so it survives manager sessions."""
    try:
        with open(os.path.join(brief_dir, "continues.txt"), encoding="utf-8") as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def write_continues(brief_dir, n):
    with open(os.path.join(brief_dir, "continues.txt"), "w", encoding="utf-8") as f:
        f.write(f"{n}\n")


def write_manager(brief_dir, session):
    """Who started this worker. Worker sessions, briefs and Orca cards don't name their
    manager, which is how two managers ended up driving the same PR."""
    rec = {"session": session, "terminal": os.environ.get("ORCA_TERMINAL_HANDLE"), "cwd": os.getcwd(),
           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with open(os.path.join(brief_dir, "manager.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
        f.write("\n")


def claude_json_path():
    # Claude Code keeps .claude.json in $CLAUDE_CONFIG_DIR when that is set, else in $HOME.
    d = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.join(os.path.expanduser(d), ".claude.json") if d else os.path.expanduser("~/.claude.json")


_backed_up = set()


def ensure_trusted(path, claude_json=None):
    """Mark one worktree path as trusted in Claude Code's config, so the worker doesn't stop
    at the "Accessing workspace … Quick safety check" dialog.

    Why per exact path: Claude Code keys trust by the absolute cwd, and every Orca worktree
    is a new path. The dialog ate the prompt, Claude quit, and the send still reported
    accepted. Only the one flag is set; a new entry also gets the two empty containers
    Claude Code writes itself. Returns a one-line note saying what happened."""
    cj = claude_json or claude_json_path()
    path = os.path.abspath(path)
    if not os.path.isfile(cj):
        return f"skipped trust: {cj} does not exist (start claude once by hand)"
    try:
        with open(cj, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return f"skipped trust: {cj} is not valid JSON ({e.__class__.__name__}); not touching it"
    if not isinstance(data, dict):
        return f"skipped trust: {cj} is not a JSON object; not touching it"
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        return f"skipped trust: {cj} has a non-object \"projects\"; not touching it"
    entry = projects.get(path)
    if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
        return f"already trusted: {path}"
    if not isinstance(entry, dict):
        entry = projects[path] = {"allowedTools": [], "mcpServers": {}}
    entry["hasTrustDialogAccepted"] = True
    # Claude Code sessions rewrite this file all the time; tmp + rename means a reader never
    # sees half a file, and the window for losing their write is one read-modify-write.
    tmp = f"{cj}.tmp-{os.getpid()}"
    try:
        if cj not in _backed_up:
            # Once per run: a second worker spawned by the same run must not replace the
            # untouched original with a copy that already has the first worker's entry.
            shutil.copy2(cj, cj + ".orca-flow.bak")
            _backed_up.add(cj)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, cj)
    except OSError as e:
        # A trust failure must not stop the spawn: the post-send check still catches a worker
        # that quit at the dialog.
        try:
            os.remove(tmp)
        except OSError:
            pass
        return f"skipped trust: could not write {cj} ({e.__class__.__name__}: {e})"
    return f"trusted {path}"


TRUST_DIALOG = ("accessing workspace", "quick safety check")
# Claude Code's TUI: the input box rules, its "❯" prompt, the status hints under it.
TUI_MARKERS = ("────", "❯", "? for shortcuts", "esc to interrupt", "bypass permissions")
# A bare shell prompt: "user@host dir %", "user@host:~/dir$", or just "%" / "$" / "#".
SHELL_PROMPT = re.compile(r"^(\([^)]*\)\s*)?([\w.+-]+@[\w.-]+\S*(\s+\S+)*\s*)?[%$#]$")


def _squash(text):
    return re.sub(r"\s+", "", text or "")


def classify_tail(lines, prompt):
    """(verdict, note) for the terminal tail read right after the prompt was sent.

    delivered: the TUI is up and shows the prompt (compared without whitespace, because the
    TUI wraps it). worker exited: the last non-empty line is a shell prompt, so the agent
    quit and the prompt went to the shell or nowhere. Anything else is "accepted, not
    confirmed", the wording the send receipt always had."""
    rows = [l.rstrip() for l in lines or [] if l and l.strip()]
    tail_text = "\n".join(rows)
    low = tail_text.lower()
    # The dialog can still be on screen (not yet answered) or already behind a shell prompt;
    # either way the manager needs to know the path wasn't trusted.
    hint = (". The trust dialog is in the tail: this worktree path isn't trusted. Run scripts/init.py if "
            "the repo was never set up, and check the spawn output's trust note"
            if all(t in low for t in TRUST_DIALOG) else "")
    if rows and SHELL_PROMPT.match(rows[-1].strip()):
        return "worker exited", "the last line is a bare shell prompt: the agent is not running" + hint
    if any(m in tail_text for m in TUI_MARKERS):
        if _squash(prompt)[:60] and _squash(prompt)[:60] in _squash(tail_text):
            return "delivered", "the TUI shows the prompt" + hint
        return "accepted, not confirmed", "the TUI is up but the prompt isn't visible in the tail" + hint
    return "accepted, not confirmed", "no TUI or shell prompt recognised in the tail" + hint


def read_tail(handle, limit=25):
    """The terminal's last lines, or None. Not orca(): a failed read here must not end the
    run, the prompt has already been sent."""
    r = subprocess.run([ORCA, "terminal", "read", "--terminal", handle, "--limit", str(limit), "--json"],
                       capture_output=True, text=True)
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return None
    tail = find_key(data.get("result") or {}, "tail") if data.get("ok") else None
    return tail if isinstance(tail, list) else None


def bin_ok(hs):
    """One check for every caller: a bin that exists but can't be executed would pass
    find_handoff and then fail every refresh."""
    return bool(hs["bin"]) and os.path.isfile(hs["bin"]) and os.access(hs["bin"], os.X_OK)


def hook_state_dir_warning(hs):
    """install-hook can't carry a state dir: the hook writes to $JEV_HANDOFF_STATE_DIR or
    jev-handoff's default, while --continue reads handoff.state_dir. When they differ the
    working set is never found, so say so on every install."""
    env = os.environ.get("JEV_HANDOFF_STATE_DIR")
    writes = os.path.realpath(os.path.expanduser(env or cfgmod.DEFAULTS["handoff"]["state_dir"]))
    if writes == os.path.realpath(hs["state_dir"]):
        return ""
    return (f". WARNING: the hook writes to {writes} ({'$JEV_HANDOFF_STATE_DIR' if env else 'the jev-handoff default'}) "
            f"but handoff.state_dir is {hs['state_dir']}; set JEV_HANDOFF_STATE_DIR to it where workers run, "
            f"or change handoff.state_dir")


def handoff_hook(hs, wt_path, dry=False):
    """Install jev-handoff's Stop hook into the worktree's .claude/settings.local.json, or a
    note saying why not; None when the feature is off.

    Why per worktree: Claude Code reads project hooks from the session's own checkout, and
    settings.local.json is gitignored, so a hook in the main checkout never runs in a worker.
    Must run before terminal create: the agent reads its settings once, at startup."""
    if not hs["bin"] or not hs["hook"]:
        return None
    if not wt_path:
        return "skipped hook: the worktree has no path"
    if not bin_ok(hs):
        return f"skipped hook: {hs['bin']} is missing or not executable (handoff.bin)"
    warn = hook_state_dir_warning(hs)
    cmd = [hs["bin"], "install-hook", "--settings", os.path.join(wt_path, ".claude", "settings.local.json")]
    if dry:
        return "would run " + shlex.join(cmd) + warn
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"hook install failed: {e.__class__.__name__}: {e}"
    if r.returncode != 0:
        return f"hook install failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}"
    # install-hook prints a diff, then one summary line ("updated …" / "already installed in …").
    return "hook: " + ((r.stdout.strip().splitlines() or [""])[-1]) + warn


def refresh_handoff(hs, tfile, task, dry=False):
    """Bring the working set up to date with the transcript's last lines (the Stop hook has an
    8 s budget and may have been cut off, or the session died mid-turn). A note, or None.
    Failures are only noted: a stale working set is still better than none."""
    if not tfile or not bin_ok(hs):
        return None
    cmd = [hs["bin"], "--state-dir", hs["state_dir"], "run", "--transcript", tfile, "--task", task]
    if dry:
        # A dry run only reads: the working set is reported as it is on disk now.
        return "would run " + shlex.join(cmd)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"refresh failed, using the working set as it was: {e.__class__.__name__}"
    if r.returncode != 0:
        return f"refresh failed (exit {r.returncode}), using the working set as it was: {(r.stderr or r.stdout).strip()[-300:]}"
    return "refreshed: " + ((r.stdout.strip().splitlines() or [""])[-1])


def find_handoff(hs, tfile):
    """(info, reason): the previous session's jev-handoff working set, or None and why not.
    Named "working set" throughout, to keep it apart from the worker-written handoff.md next
    to the brief."""
    if not hs["bin"]:
        return None, "feature off (handoff.bin is not set)"
    if not bin_ok(hs):
        return None, f"bin not found or not executable: {hs['bin']}"
    if not tfile:
        return None, "no transcript for the previous session, so no session id"
    sid = os.path.splitext(os.path.basename(tfile))[0]
    path = os.path.join(hs["state_dir"], sid, "handoff.md")
    if not os.path.isfile(path):
        return None, f"no working set at {path} (hook not installed in that session?)"
    items = bunches = None
    try:
        with open(os.path.join(hs["state_dir"], sid, "handoff.json"), encoding="utf-8") as f:
            data = json.load(f)
        items, bunches = len(data.get("items") or []), len(data.get("intents") or [])
    except (OSError, ValueError, AttributeError):
        pass
    return {"path": path, "items": items, "bunches": bunches,
            "age_s": int(time.time() - os.path.getmtime(path))}, None


def working_set_for(worktree_path, cfg, title, dry=False):
    """Everything --continue needs about the previous session's working set, in one dict:
    path/items/bunches/age_s (None when there is none), reason (why none), refresh (note on
    the `<bin> run`), read_first (the prompt text naming it, empty when there is none)."""
    hs = cfgmod.handoff_settings(cfg)
    tfile = transcript.latest_transcript(worktree_path, cfg["worker"].get("transcripts_dir")) if worktree_path else None
    refresh = refresh_handoff(hs, tfile, title, dry)
    info, reason = find_handoff(hs, tfile)
    ws = {"path": None, "items": None, "bunches": None, "age_s": None, **(info or {}),
          "reason": reason, "refresh": refresh, "read_first": ""}
    if info:
        ws["read_first"] = handoff_reading(info, hs["max_inline_lines"]) + "Then read "
    return ws


def handoff_report(ws):
    """The JSON result's view: handoff {path, items, bunches, age_s}, or null plus the reason."""
    out = {"handoff": {k: ws[k] for k in ("path", "items", "bunches", "age_s")} if ws["path"] else None}
    if not ws["path"]:
        out["handoff_reason"] = ws["reason"]
    if ws["refresh"]:
        out["handoff_refresh"] = ws["refresh"]
    return out


def line_count(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)


def handoff_reading(info, max_lines):
    """How the successor reads the working set. Only the trajectory and the last two bunches
    are read up front: the rest is older evidence, and reading a long file whole spends the
    context the restart was meant to free."""
    n = line_count(info["path"])
    how = ("read its Trajectory section and the items of the last two bunches (under Working set, "
           "everything from the second-to-last `user` item on)")
    if n > max_lines:
        how += f"; it is {n} lines, so don't read it whole, grep it when a question comes up"
    return (f"{info['path']} (the previous session's working set, kept verbatim by jev-handoff): {how}. "
            f"Treat the user turns in it as the record of what the user decided. ")


def drop_last_messages(digest_text, handoff_path):
    """The digest's "last messages" come from the transcript tail; the working set holds the
    same turns verbatim, scored. Two versions of the same conversation invite the successor
    to trust the wrong one, so the digest keeps only files and git state."""
    head = digest_text.split("\n## Its last messages", 1)[0].rstrip()
    return head + f"\n\n(Last messages omitted: the working set at `{handoff_path}` has them verbatim.)\n"


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
    # The receipt says the text was typed, not that an agent read it: a worker that quit at
    # the trust dialog still reported accepted. The tail tells the two apart. Never re-send.
    time.sleep(3)
    tail = read_tail(handle)
    if tail is None:
        verdict, why = "accepted, not confirmed", "could not read the terminal back"
    else:
        verdict, why = classify_tail(tail, prompt)
    if verdict == "worker exited":
        last = [l for l in tail if l.strip()][-10:]
        print(json.dumps({"ok": False, "error": f"worker exited: {why}", **base_info, "last_lines": last,
                          "note": "The prompt was not re-sent. Start the agent in that terminal again and send the prompt by hand."},
                         ensure_ascii=False, indent=1))
        sys.exit(2)
    print(json.dumps({
        "ok": True,
        **base_info,
        "accepted": find_key(receipt, "accepted"),
        "delivery": verdict,
        "delivery_note": why,
        "receipt": receipt,
        "note": "accepted without turn_started only means the start wasn't observed yet. Don't re-send.",
    }, ensure_ascii=False, indent=1))


def live_agents(wt_id):
    """Agent panes Orca still reports in that worktree. Any pane counts, "done" included: a
    done pane is a Claude TUI still sitting at its prompt, and a second session started
    beside it means two agents editing one checkout."""
    w = next((w for w in orca("worktree", "ps").get("worktrees", []) if w.get("worktreeId") == wt_id), None)
    return [ag for ag in (w or {}).get("agents") or []]


def continue_worker(a, cfg, wt, brief_dir, brief_path, common_path, test_lock, agent_cmd, base, dry):
    """Fresh session, same worktree, same brief. The previous session's transcript is on
    disk; a digest of it (files edited, last messages, git state) goes next to the brief."""
    path = wt.get("path")
    if not os.path.isfile(brief_path):
        die(f"no brief at {brief_path}; this worktree wasn't started by spawn_worker.py. Pass --brief on a fresh spawn instead.")
    live = [] if a.force else live_agents(wt.get("id"))
    if live:
        print(json.dumps({"ok": False, "error": f"{a.name} still has {len(live)} live agent pane(s); close the old terminal first",
                          "agents": [{"state": ag.get("state"), "paneKey": ag.get("paneKey")} for ag in live],
                          "note": f"orca terminal list --worktree id:{wt.get('id')} --json, then orca terminal close --terminal <handle> --json, "
                                  "then run --continue again. --force starts the new session anyway."},
                         ensure_ascii=False, indent=1))
        sys.exit(2)
    ho = cfg.get("handoff") or {}
    want_digest = ho.get("digest") is not False
    tfile = transcript.latest_transcript(path, cfg["worker"].get("transcripts_dir")) if want_digest else None
    # jev-handoff's working set, when configured: named first, and the digest drops its last messages.
    ws = working_set_for(path, cfg, brief_title(brief_path), dry)
    digest_path = os.path.join(brief_dir, "handoff-digest.md")
    handoff_path = os.path.join(brief_dir, "handoff.md")
    has_own = os.path.isfile(handoff_path)
    # Counted only once the prompt is sent (below): a continue that fails to start isn't one.
    count = read_continues(brief_dir) + 1
    limit = ho.get("max_continues")
    warning = (f"this package has now been continued {count} times (handoff.max_continues is {limit}): "
               f"it is too big for one worker; split what's left into new packages"
               if isinstance(limit, int) and count > limit else None)
    reads = ([f"{handoff_path} (the previous worker's own handoff)"] if has_own else []) \
        + ([f"{digest_path} (a digest of what it did, generated from its transcript)"] if want_digest else [])
    prompt = (
        f"You are continuing the \"{a.name}\" package after the previous worker session ended. First read "
        + ws["read_first"] + f"{common_path} (the rules), then {brief_path} (the package)"
        + (", then " + " and ".join(reads) if reads else "")
        + ". Check the worktree's git state yourself before trusting any of it. Don't redo finished work. "
        + (f"From the manager: {a.note} " if a.note else "")
        + "Then carry on to completion and report as the rules' last sections describe."
    )
    base_info = {"name": a.name, "worktree_id": wt.get("id"), "path": path, "brief_dir": brief_dir,
                 "continued": True, "continues": count, "previous_transcript": tfile, "handoff_md": has_own,
                 **handoff_report(ws)}
    if warning:
        base_info["warning"] = warning
    if dry:
        hook = handoff_hook(cfgmod.handoff_settings(cfg), path, dry=True)
        print(json.dumps({"ok": True, "dry_run": True, **base_info,
                          "trust": f"would trust {path}",
                          **({"handoff_hook": hook} if hook else {}),
                          "would_write": digest_path if want_digest else None,
                          "commands": [shlex.join([ORCA, "terminal", "create", "--worktree", f"id:{wt.get('id')}", "--title", "worker (cont.)", "--command", agent_cmd, "--json"]),
                                       shlex.join([ORCA, "terminal", "send", "--terminal", "<handle>", "--text", prompt, "--enter", "--wait-submit", "20", "--json"])]},
                         ensure_ascii=False, indent=1))
        return
    if not want_digest:
        text = None
    elif tfile:
        text = transcript.render_digest(transcript.digest(tfile), path, transcript.git_summary(path, base))
        if ws["path"]:
            text = drop_last_messages(text, ws["path"])
    else:
        text = ("# Handoff digest\n\nNo transcript was found for the previous session, so there is nothing to "
                "summarise. Work from the brief, handoff.md if present, and the git state:\n\n```\n"
                + transcript.git_summary(path, base) + "\n```\n")
    if text is not None:
        with open(digest_path, "w", encoding="utf-8") as f:
            f.write(text)
    # Re-render the rules: the config may have changed since the first spawn.
    with open(common_path, "w", encoding="utf-8") as f:
        f.write(render_rules(cfg, test_lock, base))
    write_manager(brief_dir, a.manager)
    orca("worktree", "set", "--worktree", f"id:{wt.get('id')}", "--comment", f"continued: {brief_title(brief_path)}", "--workspace-status", "in-progress", context=base_info)
    base_info["trust"] = ensure_trusted(path) if path else "skipped trust: the worktree has no path"
    # The new session keeps its own working set, so a second restart has one too.
    hook = handoff_hook(cfgmod.handoff_settings(cfg), path)
    if hook:
        base_info["handoff_hook"] = hook
    start_agent(wt.get("id"), agent_cmd, prompt, base_info, title="worker (cont.)")
    write_continues(brief_dir, count)


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
    p.add_argument("--bypass", action="store_true", default=None,
                   help="run the worker with bypassPermissions (only if this session is too); default: worker.bypass_permissions")
    p.add_argument("--no-bypass", dest="bypass", action="store_false", help="override worker.bypass_permissions: true")
    p.add_argument("--force", action="store_true", help="with --continue: start even if Orca still reports an agent pane in the worktree")
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
    bypass = a.bypass if a.bypass is not None else cfg["worker"].get("bypass_permissions") is True
    if bypass and not a.agent_command:
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
            "trust": "would trust <worktree path> in " + claude_json_path(),
            **({"handoff_hook": h} if (h := handoff_hook(cfgmod.handoff_settings(cfg), "<worktree path>", dry=True)) else {}),
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
    # Before terminal create: the agent reads trust once, at startup.
    base_info["trust"] = ensure_trusted(wt_path) if wt_path else "skipped trust: worktree create returned no path"
    hook = handoff_hook(cfgmod.handoff_settings(cfg), wt_path)
    if hook:
        base_info["handoff_hook"] = hook

    start_agent(wt_id, agent_cmd, prompt, base_info)


if __name__ == "__main__":
    main()
