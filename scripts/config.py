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

The repo is found from this script's location, not from the caller's cwd, so a manager
sitting in a worktree and a worker sitting in another one read the same file.

Usage:
  config.py show [--json]          # resolved config, defaults filled in
  config.py get worker.model       # one value; scalars print bare, for shell use
  config.py path                   # which file was loaded (empty if none)
  config.py init [--force]         # write a starter config into <repo>/.claude/
"""
import argparse
import copy
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
    },
    "merge_queue": {
        "worktree_name": "merge-queue",
        # A hand-written status file only the queue may edit (e.g. "NOW.md"). null = none.
        "state_file": None,
        # Deploy targets, in order. Each: {"name", "deploy": [...], "health_url", "backup": [...], "verify": [...]}
        "targets": [],
    },
    "main_checkout": {
        # The PreToolUse hook that keeps the main checkout on its default branch.
        "guard": True,
        "allow_files": [],
        "allow_prefixes": [".claude/"],
    },
    # Worktrees the cleanup command never proposes removing.
    "keep_worktrees": [],
}


def repo_root():
    """The main checkout this skill belongs to, found through git's common dir."""
    r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                       capture_output=True, text=True, cwd=SKILL_DIR)
    if r.returncode != 0:
        # The skill may live outside any repo (~/.claude/skills). Fall back to the caller.
        r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None
    return os.path.dirname(r.stdout.strip())


def common_dir(root=None):
    root = root or repo_root()
    if not root:
        return None
    r = subprocess.run(["git", "-C", root, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def config_path(root=None):
    root = root or repo_root()
    env = os.environ.get("ORCA_FLOW_CONFIG")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if not root:
        return None
    candidates = [os.path.join(root, ".claude", "orca-flow.json"), os.path.join(root, "orca-flow.json")]
    cd = common_dir(root)
    if cd:
        candidates.append(os.path.join(cd, "orca-flow", "config.json"))
    return next((c for c in candidates if os.path.isfile(c)), None)


def merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(root=None):
    """Resolved config. Unknown keys are kept, so a project can carry its own notes."""
    root = root or repo_root()
    path = config_path(root)
    raw = {}
    if path:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    cfg = merge(DEFAULTS, raw)
    cfg["repo_root"] = root
    cfg["config_file"] = path
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


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sh = sub.add_parser("show")
    sh.add_argument("--json", action="store_true")
    g = sub.add_parser("get")
    g.add_argument("key", help="dotted key, e.g. worker.test_command")
    sub.add_parser("path")
    ini = sub.add_parser("init")
    ini.add_argument("--force", action="store_true")
    a = p.parse_args()

    if a.cmd == "path":
        print(config_path() or "")
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
