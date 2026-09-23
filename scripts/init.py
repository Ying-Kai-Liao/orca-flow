#!/usr/bin/env python3
"""First run of orca-flow in a repo: everything a manager used to do by hand before the
first worker could start.

Why: the first time the skill was used outside its home project, every failure was a
first-run one. The repo wasn't registered with Orca, so no worktree could be created; there
was no orca-flow.json, so nobody knew the test command; and a repo with no merge queue was
treated as if it had one. This script does those steps once, and is safe to run again:
each step says ok, skipped (already …) or would (dry-run).

Worker trust (Claude Code's per-path "Quick safety check" dialog) is not done here: trust
is keyed by the exact worktree path, which doesn't exist until spawn_worker.py creates it,
so spawn_worker.py sets it for each worktree.

Usage:
  init.py [--repo <path>] [--test-command "…"] [--full-check "…"] [--language …] [--model …]
          [--no-queue] [--force-config] [--dry-run] [--json]
"""
import argparse
import difflib
import json
import os
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfgmod  # noqa: E402

ORCA = os.environ.get("ORCA_CLI_COMMAND", "orca")
UNITTEST = "python3 -m unittest discover -s tests"


class OrcaError(Exception):
    pass


def run(cmd, cwd=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    except OSError as e:
        return 1, "", str(e)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def orca(*args):
    cmd = [ORCA, *args, "--json"]
    code, out, err = run(cmd)
    try:
        data = json.loads(out)
    except ValueError:
        raise OrcaError(f"orca returned no JSON: {shlex.join(cmd)}: {err[-300:]}")
    if not data.get("ok"):
        raise OrcaError(f"orca reported failure: {shlex.join(cmd)}: {json.dumps(data, ensure_ascii=False)[:300]}")
    return data.get("result") or {}


def detect_base(root):
    """origin's default branch if the remote says, else the current branch (an empty repo
    has no origin/HEAD yet, and its branch may not have a commit)."""
    code, out, _ = run(["git", "-C", root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"])
    if code == 0 and out.startswith("refs/remotes/"):
        return out.removeprefix("refs/remotes/")
    code, out, _ = run(["git", "-C", root, "symbolic-ref", "--quiet", "--short", "HEAD"])
    branch = out if code == 0 and out else "main"
    code, _, _ = run(["git", "-C", root, "remote", "get-url", "origin"])
    return f"origin/{branch}" if code == 0 else branch


def detect_test_command(root):
    """(command or None, why). Only a guess nobody could argue with: one ecosystem, one
    obvious runner. Two candidates means a human picks."""
    found = []
    pkg = os.path.join(root, "package.json")
    if os.path.isfile(pkg):
        try:
            with open(pkg, encoding="utf-8") as f:
                scripts = (json.load(f) or {}).get("scripts") or {}
        except (OSError, ValueError):
            scripts = {}
        if scripts.get("test"):
            found.append(("npm test", "package.json has a test script"))
    if os.path.isfile(os.path.join(root, "pyproject.toml")) or os.path.isdir(os.path.join(root, "tests")):
        found.append((UNITTEST, "pyproject.toml or tests/ found"))
    if len(found) == 1:
        return found[0]
    if found:
        return None, "ambiguous (" + "; ".join(w for _, w in found) + "), set it by hand"
    return None, "no package.json test script, pyproject.toml or tests/ found"


def build_config(a, base, test_cmd):
    queue = not a.no_queue
    mq = {"enabled": queue}
    if queue:
        mq.update({"worktree_name": "merge-queue", "targets": []})
    else:
        mq["merge_method"] = "squash"
    return {
        "language": a.language or "English",
        "base_branch": base,
        "worker": {
            "model": a.model or cfgmod.DEFAULTS["worker"]["model"],
            "test_command": a.test_command or test_cmd,
            "full_check_command": a.full_check,
        },
        "merge_queue": mq,
    }


def explicit(a):
    """Only the values given as flags; with --force-config these win over the old file."""
    out = {}
    if a.language:
        out["language"] = a.language
    w = {k: v for k, v in (("model", a.model), ("test_command", a.test_command),
                           ("full_check_command", a.full_check)) if v}
    if w:
        out["worker"] = w
    if a.no_queue:
        out["merge_queue"] = {"enabled": False, "merge_method": "squash"}
    return out


def overlay(base, top):
    """base with top's non-null values laid over it, recursively; keys only base has stay."""
    out = dict(base or {})
    for k, v in (top or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = overlay(out[k], v)
        elif v is not None or k not in out:
            out[k] = v
    return out


def rewrite(generated, old, flags):
    """--force-config: detected defaults, then the old file, then the flags. Otherwise a
    rerun to add --full-check would wipe a test command filled in by hand, or turn a
    --no-queue repo's queue back on. To turn the queue back on, edit the file."""
    return overlay(overlay(generated, old), flags)


def dump(cfg):
    return json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"


def nulls(cfg):
    out = []
    for key in ("worker.test_command", "worker.full_check_command"):
        if cfgmod.dig(cfg, key) is None:
            out.append(key)
    if cfgmod.queue_enabled(cfg) and not cfgmod.dig(cfg, "merge_queue.targets"):
        out.append("merge_queue.targets")
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="First run of orca-flow in a repo (idempotent)")
    p.add_argument("--repo", help="the repo to set up; default: the one the skill resolves to (see config.py)")
    p.add_argument("--test-command", help='how to run a few test files, e.g. "npx vitest run {files}"')
    p.add_argument("--full-check", help="the whole suite / full check the queue (or you) runs before merging")
    p.add_argument("--language", help="language for briefs, commits and code comments (default English)")
    p.add_argument("--model", help="worker model (default opus)")
    p.add_argument("--no-queue", action="store_true", help="no merge-queue session: the manager merges PRs itself")
    p.add_argument("--force-config", action="store_true", help="rewrite an existing orca-flow.json (prints the diff)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    dry = a.dry_run or os.environ.get("ORCA_FLOW_DRY_RUN") == "1"

    steps = []
    failed = False

    def step(name, status, detail=""):
        steps.append({"step": name, "status": status, "detail": detail})
        if not a.json:
            print(f"{len(steps)}. {name:<8} {status}" + (f"  {detail}" if detail else ""))

    # 1. repo root and base branch
    root = cfgmod._root_from(os.path.abspath(os.path.expanduser(a.repo))) if a.repo else cfgmod.repo_root()
    if not root:
        step("repo", "failed", f"not a git repository: {a.repo or os.getcwd()}")
        return finish(a, steps, None, True)
    base = detect_base(root)
    step("repo", "ok", f"{root} (base {base})")

    # 2. register with Orca
    try:
        repos = orca("repo", "list").get("repos") or []
        if any(os.path.realpath(r.get("path") or "") == os.path.realpath(root) for r in repos):
            step("orca", "skipped (already registered)")
        elif dry:
            step("orca", "would (dry-run)", f"orca repo add --path {root}")
        else:
            orca("repo", "add", "--path", root)
            step("orca", "ok", "registered")
    except OrcaError as e:
        failed = True
        step("orca", "failed", str(e))

    # 3. config
    existing = cfgmod.config_path(root)
    test_cmd, test_why = detect_test_command(root)
    new = build_config(a, base, test_cmd)
    dest = existing or os.path.join(root, "orca-flow.json")
    if existing and not a.force_config:
        with open(existing, encoding="utf-8") as f:
            cfg = json.load(f)
        step("config", "skipped (already exists)", f"{existing}; --force-config to rewrite it")
    else:
        old_text = ""
        if existing:
            with open(existing, encoding="utf-8") as f:
                old_text = f.read()
            new = rewrite(new, json.loads(old_text or "{}"), explicit(a))
        cfg = new
        text = dump(new)
        if existing and not a.json:
            sys.stdout.writelines(difflib.unified_diff(old_text.splitlines(True), text.splitlines(True),
                                                       existing, existing + " (new)"))
        if dry:
            step("config", "would (dry-run)", f"write {dest}")
        elif existing and old_text == text:
            step("config", "skipped (already up to date)", dest)
        else:
            tmp = f"{dest}.tmp-{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, dest)
            step("config", "ok", f"{'rewrote' if existing else 'wrote'} {dest}"
                 + ("" if a.test_command else f"; test_command: {test_cmd or 'null'} ({test_why})"))

    # 4. shared directory
    common = cfgmod.common_dir(root)
    shared = os.path.join(common, "orca-flow") if common else None
    subdirs = [os.path.join(shared, d) for d in ("briefs", "queue", "bin")] if shared else []
    if not shared:
        failed = True
        step("shared", "failed", "git rev-parse --git-common-dir failed")
    elif all(os.path.isdir(d) for d in subdirs):
        step("shared", "skipped (already exists)", shared)
    elif dry:
        step("shared", "would (dry-run)", f"create {shared}/{{briefs,queue,bin}}")
    else:
        for d in subdirs:
            os.makedirs(d, exist_ok=True)
        step("shared", "ok", shared)

    return finish(a, steps, cfg, failed)


def finish(a, steps, cfg, failed):
    nxt = None
    if cfg is not None:
        todo = nulls(cfg)
        mode = ("with a merge queue (one queue session merges and deploys)" if cfgmod.queue_enabled(cfg)
                else "without a queue (the manager reviews, runs the full check and merges)")
        nxt = (("fill by hand: " + ", ".join(todo) if todo else "nothing left to fill")
               + f"; this repo runs {mode}. Worker trust is set per worktree by spawn_worker.py.")
    if a.json:
        print(json.dumps({"ok": not failed, "steps": steps, "next": nxt}, ensure_ascii=False, indent=1))
    elif nxt:
        print(f"next: {nxt}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
