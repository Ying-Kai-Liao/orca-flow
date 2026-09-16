#!/usr/bin/env python3
"""PreToolUse hook: block Edit/Write against the main checkout, so it stays on its base branch.

Why a hook and not a line in the skill: in one evening a user asked seven times for the work
to happen in a worktree, and the text rule was skipped every time. The harness runs a hook;
the model can't talk its way past it.

What it judges is which checkout the target file belongs to, not the session's cwd: sessions
inside a worktree load the same settings.json, and they must be free to edit their own files
while still being blocked from writing to the main checkout by absolute path.

Allowed through:
- any file in a linked worktree (its git-dir differs from the git-common-dir)
- files in other repos, or outside any working tree (memory, scratchpad, inside .git/)
- in the main checkout: main_checkout.allow_files and allow_prefixes from the config
  (default: .claude/, plus the merge queue's state file if one is configured)
- everything, once the user creates <git-common-dir>/orca-flow/allow-main-edits. That's the
  user's escape hatch, so creating that file with Edit/Write is itself always blocked —
  otherwise the refusal message would be a tutorial on getting around the hook.

Only Edit/Write-shaped tools reach this hook. `sed -i` and heredocs in Bash don't; the skill
text covers those.

Install (settings.json):
  {"hooks": {"PreToolUse": [{"matcher": "Edit|Write|NotebookEdit",
     "hooks": [{"type": "command", "command": "python3 /abs/path/to/scripts/main_checkout_guard.py"}]}]}}
Set main_checkout.guard to false in the orca-flow config to turn it off without editing settings.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfgmod  # noqa: E402


def git(cwd, *args):
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def nearest_dir(path):
    probe = os.path.dirname(path)
    while not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    return probe


def deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))
    return 0


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        # Allow on unreadable input: a broken hook blocking every edit is worse than one miss.
        return 0

    tool_input = data.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not target:
        return 0
    cwd = data.get("cwd") or os.getcwd()
    target = os.path.realpath(os.path.join(cwd, os.path.expanduser(target)))

    project = os.environ.get("CLAUDE_PROJECT_DIR") or cwd
    project_common = git(project, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not project_common:
        return 0
    flag = os.path.realpath(os.path.join(project_common, "orca-flow", "allow-main-edits"))
    if target == flag:
        return deny("That override switch is the user's to create. Tell the user what you need; don't create it for them.")

    try:
        cfg = cfgmod.load(root=os.path.dirname(project_common))
    except Exception:
        cfg = cfgmod.DEFAULTS
    main_cfg = cfg.get("main_checkout") or {}
    if not main_cfg.get("guard", True):
        return 0
    allowed_files = set(main_cfg.get("allow_files") or [])
    allowed_prefixes = tuple(main_cfg.get("allow_prefixes") or [".claude/"])

    probe = nearest_dir(target)
    if probe is None:
        return 0
    out = git(probe, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", "--show-toplevel")
    if not out or len(out.splitlines()) < 3:
        return 0  # not inside a working tree (including inside .git/)
    git_dir, common_dir, toplevel = out.splitlines()[:3]
    if os.path.realpath(git_dir) != os.path.realpath(common_dir):
        return 0  # linked worktree
    if os.path.realpath(common_dir) != os.path.realpath(project_common):
        return 0  # another repo's main checkout; not this rule's business

    rel = os.path.relpath(target, os.path.realpath(toplevel))
    if rel in allowed_files or rel.startswith(allowed_prefixes):
        return 0
    if os.path.exists(flag):
        return 0

    allowed = ", ".join(sorted(allowed_files | set(allowed_prefixes))) or "nothing"
    return deny(
        f"{toplevel} is the main checkout and stays on its base branch; build changes in an Orca "
        f"worktree (the orca-flow skill). Doing it yourself: `orca worktree create --repo id:<repoId> "
        f"--name <task> --no-parent --json`, then edit through the new worktree's absolute paths. "
        f"Handing it to a worker: scripts/spawn_worker.py. Only {allowed} may be edited here. "
        f"Don't switch to Bash to write the file instead. If the user explicitly wants the main "
        f"checkout edited directly, ask them to create the override switch themselves (it's "
        f"documented in the skill's hook notes)."
    )


if __name__ == "__main__":
    sys.exit(main())
