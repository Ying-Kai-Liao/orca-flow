#!/usr/bin/env python3
"""Resolve orca-flow's per-project configuration.

Why a config file at all: the flow (manager / worker / merge queue, one owner per
shared resource) is the same everywhere, but the commands are not. One project runs
`npx vitest`, the next runs `pytest`; one deploys through a GitHub Action, the next
over ssh. Baking those into the skill is what made the first version usable in exactly
one repository.

Everything is optional. With no config file the skill still spawns workers, tracks
worktrees and serialises test runs; it just won't claim to know how your project is
tested or deployed, and it says so instead of guessing.

Lookup order (first file wins):
  1. $ORCA_FLOW_CONFIG
  2. <repo>/.claude/orca-flow.json
  3. <repo>/orca-flow.json
  4. <git-common-dir>/orca-flow/config.json     (local, never committed)
The local file is also laid over whichever of 1-3 was found, so a value you don't want in
the repo (or a personal preference) can live there without copying the rest.

The repo is found from this script's location, not from the caller's cwd, so a manager
sitting in a worktree and a worker sitting in another one read the same file.

Usage:
  config.py show [--json]          # resolved config, defaults filled in
  config.py get worker.model       # one value; scalars print bare, for shell use
  config.py path                   # which files were loaded (empty if none)
  config.py keys [--json]          # every key: current value, default, what it does
  config.py set <key> <value> [--local] [--append] [--force] [--dry-run]
  config.py unset <key> [--local] [--dry-run]
  config.py check                  # type-check the files; exit 1 on errors
  config.py init [--force]         # write a starter config into <repo>/.claude/

`set` parses the value by the key's type (true/false, numbers, JSON for lists) and writes it
into the config file that is already in use, or <repo>/orca-flow.json if there is none.
--local writes <git-common-dir>/orca-flow/config.json instead, which is never committed and
wins over the repo's file. --append adds one item to a list key. Unknown keys are refused
unless --force, so a typo doesn't silently do nothing.

For a repo that has never run orca-flow, use scripts/init.py instead: it also registers the
repo with Orca, detects the base branch and test command, and creates the shared directory.
"""
import argparse
import copy
import difflib
import json
import os
import subprocess
import sys

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    # Free text, pasted into worker instructions: the language briefs, commit messages
    # and code comments should be written in. Match the codebase, not the user's chat.
    "language": "English",
    "base_branch": "origin/main",
    "worker": {
        "model": "opus",
        # Absolute or repo-relative path to your own worker rules. Default: assets/common.md.
        "rules_file": None,
        # Cheap checks every worker runs before opening a PR, e.g. ["npx tsc --noEmit", "npm run lint"].
        "checks": [],
        # How to run a few test files. "{files}" is replaced with the paths the worker picked.
        "test_command": None,
        # How many test runs may go at once on this machine (test-lock.sh).
        "test_slots": 1,
        # The whole suite. Workers never run it; the merge queue runs it once per batch.
        "full_check_command": None,
        # Directory of numbered migrations, if the project has one. Enables clash detection.
        "migrations_dir": None,
        # How to start the app to look at a UI change, e.g. "npm run dev".
        "run_command": None,
        # Extra project rules, one bullet per string, appended to the worker rules.
        "extra_rules": [],
        # Files too big to read whole (repo-relative). Workers are told to read only the
        # functions they touch in these; briefs must give entry points with line ranges.
        "big_files": [],
        # Above this many lines, any file counts as big for the rule above.
        "big_file_lines": 1500,
        # Context budget: window size the estimate is measured against, and where worktrees.py
        # flags a session: a fraction of the window (0.35) or an absolute token count (70000).
        "context_window": 200000,
        "context_warn": 0.35,
        # Where Claude Code keeps transcripts; null = ~/.claude/projects (or $CLAUDE_CONFIG_DIR).
        "transcripts_dir": None,
        # Start workers with --permission-mode bypassPermissions without passing --bypass.
        # Only for managers that themselves run with bypassPermissions.
        "bypass_permissions": False,
    },
    # What happens when a worker's context fills up (see the context budget in SKILL.md).
    "handoff": {
        # false: workers are never wrapped up for context; they run to the end and rely on
        # the agent's own compaction. The context column is still shown, and --continue still
        # restarts a worker that died.
        "enabled": True,
        # --continue writes handoff-digest.md from the old session's transcript.
        "digest": True,
        # The one line the manager sends to a flagged worker. The worker rules quote it.
        "wrap_up_message": "WRAP UP: commit WIP, push, write handoff.md next to the brief, set the card to HANDOFF, stop.",
        # A package continued more often than this was too big; --continue warns.
        "max_continues": 2,
    },
    "merge_queue": {
        # false: a small repo with no queue session. The manager reviews and merges PRs itself,
        # handover.py send refuses, and open PRs are not reported as unhanded.
        "enabled": True,
        # How a manager merges when there is no queue: gh pr merge --<method> (squash, merge, rebase).
        "merge_method": "squash",
        "worktree_name": "merge-queue",
        # A hand-written status file only the queue may edit (e.g. "NOW.md"). null = none.
        "state_file": None,
        # Deploy targets, in order. Each: {"name", "deploy": [...], "health_url", "backup": [...], "verify": [...]}
        "targets": [],
        # Retire the queue session after this many batches (its context grows with each one)
        # and start a fresh one from the handover files.
        "rotate_after": 10,
        # archive_status.py keeps this many newest entries in the state file; the rest move
        # to archive_file (default: <state file stem>-archive.md).
        "state_file_keep": 10,
        "archive_file": None,
        # When expensive post-deploy verification (a real end-to-end run) is worth its cost.
        # The queue never runs it on its own; a manager asks for it per PR.
        "heavy_verification": "only for a PR with a migration or an outbound side effect (mail, push, third-party calls), and only when a manager asks for it in the handover",
    },
    "main_checkout": {
        # The PreToolUse hook that keeps the main checkout on its default branch.
        "guard": True,
        "allow_files": [],
        "allow_prefixes": [".claude/"],
    },
    # Worktrees the cleanup command never proposes removing.
    "keep_worktrees": [],
    "board": {
        # A working pane with no update for longer than this many minutes is "stale".
        "stale_min": 30,
        # A question left unanswered for longer than this many minutes is demoted to "done".
        "question_max_min": 240,
        # Extra phrases that mark a last paragraph as asking the user to decide, and extra
        # negations that cancel them. Added to the built-in English ones; lowercase. Use them
        # when the agents in this repo write in another language.
        "decision_phrases": [],
        "negations": [],
    },
    "cleanup": {
        # A worktree with no commits counts as abandoned after this many idle hours.
        "idle_hours": 3,
    },
}

# One line per key, for `config.py keys` and `config.py check`. The type is what `set`
# parses a value into and what `check` expects; None in the file is always allowed.
SCHEMA = {
    "project": (str, "Name used in worker rules (default: repo directory name)."),
    "language": (str, "Language of briefs, commit messages and code comments. Match the codebase."),
    "base_branch": (str, "What worktrees branch from and the queue pushes to."),
    "keep_worktrees": (list, "Worktrees cleanup never proposes removing."),
    "worker.model": (str, "Model for spawned workers (--model overrides)."),
    "worker.rules_file": (str, "Your own worker rules template instead of assets/common.md."),
    "worker.checks": (list, "Cheap checks every worker runs before opening a PR."),
    "worker.test_command": (str, "Runs a few test files; {files} is replaced."),
    "worker.test_slots": (int, "Test runs allowed at once on this machine."),
    "worker.full_check_command": (str, "The whole suite; only the queue runs it."),
    "worker.migrations_dir": (str, "Numbered migrations directory; enables clash detection."),
    "worker.run_command": (str, "How to start the app to look at a UI change."),
    "worker.extra_rules": (list, "Extra bullets appended to the worker rules."),
    "worker.big_files": (list, "Files workers must never read whole."),
    "worker.big_file_lines": (int, "Above this many lines any file counts as big."),
    "worker.context_window": (int, "Context window the estimate is measured against, in tokens."),
    "worker.context_warn": ((int, float), "Flag a worker at this fraction of the window (<= 1) or token count (> 1)."),
    "worker.transcripts_dir": (str, "Where Claude Code writes transcripts, if not the default."),
    "worker.bypass_permissions": (bool, "Start workers with bypassPermissions by default."),
    "handoff.enabled": (bool, "Wrap up and --continue workers whose context is flagged."),
    "handoff.digest": (bool, "--continue writes handoff-digest.md from the old transcript."),
    "handoff.wrap_up_message": (str, "The line sent to a flagged worker; quoted in the worker rules."),
    "handoff.max_continues": (int, "--continue warns after this many continuations of one package."),
    "merge_queue.enabled": (bool, "false: no queue session; the manager merges itself."),
    "merge_queue.merge_method": (str, "squash, merge or rebase (no-queue merges)."),
    "merge_queue.worktree_name": (str, "The clean worktree the queue works from."),
    "merge_queue.state_file": (str, "Status file only the queue edits, e.g. NOW.md."),
    "merge_queue.targets": (list, "Deploy targets, in order."),
    "merge_queue.rotate_after": (int, "Batches after which the queue retires itself."),
    "merge_queue.state_file_keep": (int, "Entries kept in the status file before archiving."),
    "merge_queue.archive_file": (str, "Where archived status entries go."),
    "merge_queue.heavy_verification": (str, "When an expensive post-deploy check is worth it."),
    "main_checkout.guard": (bool, "Block edits to the main checkout (PreToolUse hook)."),
    "main_checkout.allow_files": (list, "Files still editable in the main checkout."),
    "main_checkout.allow_prefixes": (list, "Path prefixes still editable in the main checkout."),
    "board.stale_min": ((int, float), "Minutes without an update before a working pane is stale."),
    "board.question_max_min": ((int, float), "Minutes after which an unanswered question is demoted."),
    "board.decision_phrases": (list, "Extra phrases that ask the user to decide (lowercase)."),
    "board.negations": (list, "Extra negations that cancel a decision phrase (lowercase)."),
    "cleanup.idle_hours": ((int, float), "Idle hours before a worktree with no commits is a cleanup candidate."),
}
CHOICES = {"merge_queue.merge_method": ("squash", "merge", "rebase")}


def _root_from(cwd):
    """The main checkout containing cwd. --git-common-dir is shared by every linked
    worktree, so a manager in the main checkout and a worker in a worktree resolve to
    the same repo."""
    r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                       capture_output=True, text=True, cwd=cwd)
    return os.path.dirname(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def is_standalone(skill_dir):
    """True when skill_dir is the top of its own git checkout (a clone, or a linked worktree
    of one), as opposed to a folder inside some project's checkout."""
    r = subprocess.run(["git", "-C", skill_dir, "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    top = r.stdout.strip() if r.returncode == 0 else ""
    if not top:
        return False
    return (os.path.realpath(top) == os.path.realpath(skill_dir)
            or os.path.isfile(os.path.join(top, "SKILL.md")))


def repo_root():
    """The project this skill is being used on.

    Installed inside a project (.claude/skills/orca-flow), the skill's own location is the
    reliable answer, and it stays right whatever the caller's cwd is. Installed standalone
    (a clone in ~/.claude/skills, which is a git repo of its own), that location would
    resolve to this repository instead of the user's, so fall back to the caller's cwd.
    $ORCA_FLOW_REPO overrides both.

    Standalone is judged by the skill directory's own toplevel, not by the main checkout:
    run from a linked worktree of this repo, the main checkout is somewhere else, and
    comparing against it made every script treat the skill repo as the user's project."""
    env = os.environ.get("ORCA_FLOW_REPO")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    skill_repo = _root_from(SKILL_DIR)
    if skill_repo and not is_standalone(SKILL_DIR):
        return skill_repo
    return _root_from(os.getcwd()) or skill_repo


def common_dir(root=None):
    root = root or repo_root()
    if not root:
        return None
    r = subprocess.run(["git", "-C", root, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def config_path(root=None, use_env=True):
    """use_env=False skips $ORCA_FLOW_CONFIG: for callers that read other repos' configs
    (board.py), where one repo's override must not stand in for every repo's file."""
    root = root or repo_root()
    env = os.environ.get("ORCA_FLOW_CONFIG") if use_env else None
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if not root:
        return None
    candidates = [os.path.join(root, ".claude", "orca-flow.json"), os.path.join(root, "orca-flow.json")]
    cd = common_dir(root)
    if cd:
        candidates.append(os.path.join(cd, "orca-flow", "config.json"))
    return next((c for c in candidates if os.path.isfile(c)), None)


def local_path(root=None):
    """<git-common-dir>/orca-flow/config.json, whether or not it exists."""
    cd = common_dir(root or repo_root())
    return os.path.join(cd, "orca-flow", "config.json") if cd else None


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_raw(root=None, use_env=True):
    """(raw dict, files read): the main config file with the local one laid over it."""
    root = root or repo_root()
    path = config_path(root, use_env=use_env)
    files, raw = [], {}
    if path:
        raw = read_json(path)
        if not isinstance(raw, dict):
            raise ValueError(f"{path} is not a JSON object")
        files.append(path)
    local = local_path(root) if root else None
    if local and os.path.isfile(local) and (not path or os.path.realpath(local) != os.path.realpath(path)):
        over = read_json(local)
        if not isinstance(over, dict):
            raise ValueError(f"{local} is not a JSON object")
        raw = merge(raw, over)
        files.append(local)
    return raw, files


def merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(root=None):
    """Resolved config. Unknown keys are kept, so a project can carry its own notes."""
    root = root or repo_root()
    raw, files = load_raw(root)
    cfg = merge(DEFAULTS, raw)
    cfg["repo_root"] = root
    cfg["config_file"] = files[0] if files else None
    cfg["config_files"] = files
    cfg.setdefault("project", os.path.basename(root) if root else None)
    if not cfg.get("project"):
        cfg["project"] = os.path.basename(root) if root else "project"
    keep = set(cfg.get("keep_worktrees") or [])
    if cfg["merge_queue"].get("worktree_name"):
        keep.add(cfg["merge_queue"]["worktree_name"])
    keep |= {n.strip() for n in os.environ.get("ORCA_FLOW_KEEP", "").split(",") if n.strip()}
    cfg["keep_worktrees"] = sorted(keep)
    if cfg["merge_queue"].get("state_file"):
        allow = list(cfg["main_checkout"].get("allow_files") or [])
        if cfg["merge_queue"]["state_file"] not in allow:
            allow.append(cfg["merge_queue"]["state_file"])
        cfg["main_checkout"]["allow_files"] = allow
    return cfg


def queue_enabled(cfg):
    """Whether this repo has a merge queue. Only an explicit false turns it off, so a config
    written before the key existed keeps its queue."""
    return (cfg.get("merge_queue") or {}).get("enabled") is not False


def handoff_enabled(cfg):
    return (cfg.get("handoff") or {}).get("enabled") is not False


def context_warn_tokens(cfg):
    """worker.context_warn as a token count: a fraction of the window, or tokens as given."""
    w = cfg.get("worker") or {}
    window = int(w.get("context_window") or 200000)
    warn = float(w.get("context_warn") or 0.35)
    return int(warn * window) if warn <= 1 else int(warn)


def dig(cfg, dotted):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


STARTER = {
    "language": "English",
    "base_branch": "origin/main",
    "worker": {
        "checks": ["npm run lint"],
        "test_command": "npx vitest run {files}",
        "test_slots": 1,
        "full_check_command": "npm test",
    },
    "merge_queue": {
        "worktree_name": "merge-queue",
        "state_file": None,
        "targets": [],
    },
}


TRUE = {"true", "yes", "on", "1"}
FALSE = {"false", "no", "off", "0"}


def _type_name(t):
    ts = t if isinstance(t, tuple) else (t,)
    return "/".join({str: "string", int: "integer", float: "number", bool: "true/false",
                     list: "list", dict: "object"}[x] for x in ts)


def parse_value(key, text):
    """The value `set` writes for key, parsed by its type in SCHEMA. Raises ValueError."""
    t = SCHEMA.get(key, (None, ""))[0]
    ts = t if isinstance(t, tuple) else (t,)
    if text.strip().lower() == "null":
        return None
    if bool in ts:
        low = text.strip().lower()
        if low in TRUE:
            return True
        if low in FALSE:
            return False
        raise ValueError(f"{key} takes true or false, not {text!r}")
    if int in ts or float in ts:
        try:
            n = float(text)
        except ValueError:
            raise ValueError(f"{key} takes a number, not {text!r}")
        if int in ts and n.is_integer() and (float not in ts or "." not in text):
            return int(n)
        if float not in ts:
            raise ValueError(f"{key} takes a whole number, not {text!r}")
        return n
    if list in ts or dict in ts or t is None:
        try:
            v = json.loads(text)
        except ValueError:
            if t is None:
                return text  # unknown key (--force): a bare word is a string
            raise ValueError(f"{key} takes JSON ({_type_name(t)}), e.g. '[\"a\", \"b\"]'; use --append to add one item")
        return v
    return text


def type_errors(raw):
    """[(key, message)] for values in a raw config that don't match SCHEMA, plus unknown
    keys as notes (they're kept, so a project can carry its own notes)."""
    errors, notes = [], []

    def walk(node, prefix):
        for k, v in node.items():
            key = f"{prefix}{k}"
            if key in SCHEMA:
                t = SCHEMA[key][0]
                ts = t if isinstance(t, tuple) else (t,)
                ok = v is None or (isinstance(v, ts) and not (isinstance(v, bool) and bool not in ts))
                if not ok:
                    errors.append((key, f"expected {_type_name(t)}, got {json.dumps(v, ensure_ascii=False)}"))
                elif key in CHOICES and v is not None and v not in CHOICES[key]:
                    errors.append((key, f"must be one of {', '.join(CHOICES[key])}"))
            elif isinstance(v, dict) and isinstance(dig(DEFAULTS, key), dict):
                walk(v, key + ".")
            else:
                notes.append((key, "unknown key (kept, but nothing reads it)"))

    walk(raw, "")
    w = raw.get("worker") or {}
    if isinstance(w.get("context_warn"), (int, float)) and w["context_warn"] <= 0:
        errors.append(("worker.context_warn", "must be above 0"))
    for i, t in enumerate((raw.get("merge_queue") or {}).get("targets") or []):
        if not isinstance(t, dict) or not t.get("name") or not isinstance(t.get("deploy"), list):
            errors.append((f"merge_queue.targets[{i}]", 'needs a "name" and a "deploy" list'))
    return errors, notes


def set_in(node, dotted, value, delete=False):
    parts = dotted.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            if delete:
                return False
            child = node[part] = {}
        node = child
    if delete:
        return node.pop(parts[-1], _MISSING) is not _MISSING
    node[parts[-1]] = value
    return True


_MISSING = object()


def write_target(root, local):
    """The file `set`/`unset` edits."""
    if local:
        return local_path(root)
    return config_path(root) or os.path.join(root, "orca-flow.json")


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def cmd_set(a, root, delete=False):
    known = a.key in SCHEMA
    if not known and not getattr(a, "force", False) and not delete:
        near = difflib.get_close_matches(a.key, SCHEMA, n=3, cutoff=0.7) \
            or [k for k in SCHEMA if k.split(".")[-1] == a.key.split(".")[-1]]
        hint = f" Did you mean {', '.join(near)}?" if near else " `config.py keys` lists them."
        sys.exit(f"unknown key {a.key}.{hint} (--force writes it anyway)")
    target = write_target(root, a.local)
    try:
        data = read_json(target) if os.path.isfile(target) else {}
    except ValueError as e:
        sys.exit(f"{target} is not valid JSON ({e}); fix it by hand first")
    if delete:
        if not set_in(data, a.key, None, delete=True):
            print(f"{a.key} is not set in {target}; nothing to do")
            return
        verb = f"unset {a.key}"
    else:
        try:
            value = parse_value(a.key, a.value) if not a.append else a.value
        except ValueError as e:
            sys.exit(str(e))
        if a.append:
            if known and SCHEMA[a.key][0] is not list:
                sys.exit(f"--append works on list keys; {a.key} is {_type_name(SCHEMA[a.key][0])}")
            current = dig(data, a.key)
            if current is None:
                current = list(dig(load(root), a.key) or [])
            if not isinstance(current, list):
                sys.exit(f"{a.key} in {target} is not a list")
            value = current + [a.value]
        errs, _ = type_errors(_nest(a.key, value))
        if errs and not a.force:
            sys.exit(f"{errs[0][0]}: {errs[0][1]}")
        set_in(data, a.key, value)
        verb = f"set {a.key} = {json.dumps(value, ensure_ascii=False)}"
    if a.dry_run or os.environ.get("ORCA_FLOW_DRY_RUN") == "1":
        print(f"would {verb} in {target}")
        return
    write_json(target, data)
    print(f"{verb} in {target}")
    # The local file wins over the repo's; say so when it hides what was just written.
    local = local_path(root)
    if not a.local and local and os.path.isfile(local) and os.path.realpath(local) != os.path.realpath(target):
        shadow = dig(read_json(local), a.key)
        if shadow is not None:
            print(f"note: {local} also sets {a.key} = {json.dumps(shadow, ensure_ascii=False)}, and it wins")


def _has(node, dotted):
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _nest(dotted, value):
    out = {}
    set_in(out, dotted, value)
    return out


def cmd_keys(as_json):
    cfg = load()
    raw, _ = load_raw(cfg["repo_root"])
    rows = []
    for key, (t, desc) in SCHEMA.items():
        rows.append({"key": key, "value": dig(cfg, key), "default": dig(DEFAULTS, key), "type": _type_name(t),
                     "set": _has(raw, key), "description": desc})
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    for r in rows:
        v = json.dumps(r["value"], ensure_ascii=False)
        if len(v) > 50:
            v = v[:47] + "..."
        print(f"{'*' if r['set'] else ' '} {r['key']:<32} {v:<52} {r['description']}")
    print("\n* = set in a config file, otherwise the default. Change one with: config.py set <key> <value> [--local]")
    print("files: " + (", ".join(cfg["config_files"]) or "none (all defaults)"))


def cmd_check():
    root = repo_root()
    try:
        raw, files = load_raw(root)
    except ValueError as e:
        print(f"error: invalid JSON: {e}")
        sys.exit(1)
    errors, notes = type_errors(raw)
    for k, m in errors:
        print(f"error: {k}: {m}")
    for k, m in notes:
        print(f"note: {k}: {m}")
    print(f"{len(errors)} error(s) in " + (", ".join(files) or "no config file (defaults only)"))
    sys.exit(1 if errors else 0)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sh = sub.add_parser("show")
    sh.add_argument("--json", action="store_true")
    g = sub.add_parser("get")
    g.add_argument("key", help="dotted key, e.g. worker.test_command")
    sub.add_parser("path")
    ks = sub.add_parser("keys")
    ks.add_argument("--json", action="store_true")
    st = sub.add_parser("set")
    st.add_argument("key")
    st.add_argument("value")
    st.add_argument("--local", action="store_true", help="write <git-common-dir>/orca-flow/config.json (never committed)")
    st.add_argument("--append", action="store_true", help="add the value as one item to a list key")
    st.add_argument("--force", action="store_true", help="write an unknown key or a value of the wrong type anyway")
    st.add_argument("--dry-run", action="store_true")
    us = sub.add_parser("unset")
    us.add_argument("key")
    us.add_argument("--local", action="store_true")
    us.add_argument("--dry-run", action="store_true")
    sub.add_parser("check")
    ini = sub.add_parser("init")
    ini.add_argument("--force", action="store_true")
    a = p.parse_args()

    if a.cmd == "path":
        root = repo_root()
        print("\n".join(load_raw(root)[1]) if root else "")
        return
    if a.cmd == "keys":
        cmd_keys(a.json)
        return
    if a.cmd == "check":
        cmd_check()
        return
    if a.cmd in ("set", "unset"):
        root = repo_root()
        if not root:
            sys.exit("not inside a git repository")
        cmd_set(a, root, delete=a.cmd == "unset")
        return
    if a.cmd == "init":
        root = repo_root()
        if not root:
            print("not inside a git repository", file=sys.stderr)
            sys.exit(1)
        os.makedirs(os.path.join(root, ".claude"), exist_ok=True)
        dest = os.path.join(root, ".claude", "orca-flow.json")
        if os.path.exists(dest) and not a.force:
            print(f"{dest} already exists (use --force to overwrite)", file=sys.stderr)
            sys.exit(1)
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(STARTER, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(dest)
        return

    cfg = load()
    if a.cmd == "get":
        v = dig(cfg, a.key)
        if v is None:
            return
        print(v if isinstance(v, (str, int, float)) and not isinstance(v, bool)
              else ("true" if v is True else "false" if v is False
                    else json.dumps(v, ensure_ascii=False)))
        return
    print(json.dumps(cfg, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
