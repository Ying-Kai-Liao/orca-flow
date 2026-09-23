"""Tests for the first-run pieces: init.py, spawn_worker's trust and post-send check, and the
no-queue refusal in handover.py.

Run: python3 -m unittest discover -s tests

Everything runs against temp directories: a throwaway git repo, a throwaway claude.json, and
a fake `orca` (init.orca patched), so nothing here touches the real Orca or ~/.claude.json.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, SCRIPTS)
import init  # noqa: E402
import spawn_worker  # noqa: E402

CLEAN_ENV = {k: v for k, v in os.environ.items()
             if k not in ("ORCA_FLOW_CONFIG", "ORCA_FLOW_REPO", "ORCA_FLOW_DRY_RUN")}


def git_repo(tmp, files=None):
    root = os.path.join(tmp, "repo")
    os.makedirs(root)
    subprocess.run(["git", "init", "-q", "-b", "main", root], check=True)
    for rel, text in (files or {}).items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    return os.path.realpath(root)


class FakeOrca:
    def __init__(self, repos=None, fail_add=False):
        self.repos = list(repos or [])
        self.calls = []
        self.fail_add = fail_add

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("repo", "list"):
            return {"repos": [{"id": str(i), "path": p} for i, p in enumerate(self.repos)]}
        if args[:2] == ("repo", "add"):
            if self.fail_add:
                raise init.OrcaError("orca reported failure")
            self.repos.append(args[args.index("--path") + 1])
            return {"repo": {}}
        raise AssertionError(f"unexpected orca call {args}")


def run_init(root, orca, *flags):
    out = io.StringIO()
    with mock.patch.dict(os.environ, CLEAN_ENV, clear=True), mock.patch.object(init, "orca", orca), \
            contextlib.redirect_stdout(out):
        code = init.main(["--repo", root, "--json", *flags])
    return code, json.loads(out.getvalue())


def statuses(res):
    return {s["step"]: s["status"] for s in res["steps"]}


class InitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def read_cfg(self, root):
        with open(os.path.join(root, "orca-flow.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_fresh_repo(self):
        root = git_repo(self.tmp.name, {"tests/test_x.py": ""})
        orca = FakeOrca()
        code, res = run_init(root, orca)
        self.assertEqual(code, 0)
        self.assertEqual(statuses(res), {"repo": "ok", "orca": "ok", "config": "ok", "shared": "ok"})
        self.assertIn(("repo", "add", "--path", root), orca.calls)
        cfg = self.read_cfg(root)
        self.assertEqual(cfg["base_branch"], "main")  # no origin remote
        self.assertEqual(cfg["language"], "English")
        self.assertEqual(cfg["worker"]["test_command"], init.UNITTEST)
        self.assertIsNone(cfg["worker"]["full_check_command"])
        self.assertIs(cfg["merge_queue"]["enabled"], True)
        for d in ("briefs", "queue", "bin"):
            self.assertTrue(os.path.isdir(os.path.join(root, ".git", "orca-flow", d)))
        self.assertIn("worker.full_check_command", res["next"])
        self.assertIn("with a merge queue", res["next"])

    def test_second_run_skips_everything(self):
        root = git_repo(self.tmp.name)
        orca = FakeOrca()
        run_init(root, orca)
        code, res = run_init(root, orca)
        self.assertEqual(code, 0)
        self.assertTrue(all(s.startswith("skipped") for k, s in statuses(res).items() if k != "repo"), res)

    def test_already_registered(self):
        root = git_repo(self.tmp.name)
        orca = FakeOrca(repos=[root])
        _, res = run_init(root, orca)
        self.assertEqual(statuses(res)["orca"], "skipped (already registered)")
        self.assertFalse([c for c in orca.calls if c[:2] == ("repo", "add")])

    def test_orca_failure_is_reported_and_other_steps_run(self):
        root = git_repo(self.tmp.name)
        code, res = run_init(root, FakeOrca(fail_add=True))
        self.assertEqual(code, 1)
        self.assertFalse(res["ok"])
        self.assertEqual(statuses(res)["orca"], "failed")
        self.assertEqual(statuses(res)["config"], "ok")

    def test_existing_config_is_never_overwritten(self):
        root = git_repo(self.tmp.name)
        with open(os.path.join(root, "orca-flow.json"), "w", encoding="utf-8") as f:
            f.write('{"worker": {"test_command": "make t"}}\n')
        _, res = run_init(root, FakeOrca(), "--no-queue", "--test-command", "other")
        self.assertEqual(statuses(res)["config"], "skipped (already exists)")
        self.assertEqual(self.read_cfg(root), {"worker": {"test_command": "make t"}})

    def test_existing_config_in_dot_claude_counts(self):
        root = git_repo(self.tmp.name, {".claude/orca-flow.json": "{}\n"})
        _, res = run_init(root, FakeOrca())
        self.assertEqual(statuses(res)["config"], "skipped (already exists)")
        self.assertFalse(os.path.exists(os.path.join(root, "orca-flow.json")))

    def test_force_config_keeps_hand_filled_values_and_applies_flags(self):
        root = git_repo(self.tmp.name)
        with open(os.path.join(root, "orca-flow.json"), "w", encoding="utf-8") as f:
            json.dump({"language": "Traditional Chinese", "worker": {"test_command": "make t"},
                       "merge_queue": {"enabled": False}, "notes": "mine"}, f)
        _, res = run_init(root, FakeOrca(), "--force-config", "--full-check", "make all")
        self.assertEqual(statuses(res)["config"], "ok")
        cfg = self.read_cfg(root)
        self.assertEqual(cfg["language"], "Traditional Chinese")
        self.assertEqual(cfg["worker"]["test_command"], "make t")
        self.assertEqual(cfg["worker"]["full_check_command"], "make all")
        self.assertIs(cfg["merge_queue"]["enabled"], False)  # not turned back on by omission
        self.assertEqual(cfg["notes"], "mine")

    def test_no_queue(self):
        root = git_repo(self.tmp.name)
        _, res = run_init(root, FakeOrca(), "--no-queue", "--full-check", "make all")
        cfg = self.read_cfg(root)
        self.assertIs(cfg["merge_queue"]["enabled"], False)
        self.assertEqual(cfg["merge_queue"]["merge_method"], "squash")
        self.assertIn("without a queue", res["next"])
        self.assertNotIn("merge_queue.targets", res["next"])

    def test_dry_run_changes_nothing(self):
        root = git_repo(self.tmp.name)
        orca = FakeOrca()
        code, res = run_init(root, orca, "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(statuses(res), {"repo": "ok", "orca": "would (dry-run)", "config": "would (dry-run)",
                                         "shared": "would (dry-run)"})
        self.assertFalse(os.path.exists(os.path.join(root, "orca-flow.json")))
        self.assertFalse(os.path.exists(os.path.join(root, ".git", "orca-flow")))
        self.assertEqual(orca.calls, [("repo", "list")])

    def test_base_branch_from_origin_head(self):
        root = git_repo(self.tmp.name)
        subprocess.run(["git", "-C", root, "remote", "add", "origin", "https://example.invalid/r.git"], check=True)
        subprocess.run(["git", "-C", root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk"],
                       check=True)
        self.assertEqual(init.detect_base(root), "origin/trunk")

    def test_base_branch_current_branch_with_origin(self):
        root = git_repo(self.tmp.name)
        subprocess.run(["git", "-C", root, "remote", "add", "origin", "https://example.invalid/r.git"], check=True)
        self.assertEqual(init.detect_base(root), "origin/main")

    def test_test_command_guess(self):
        cases = [({"package.json": '{"scripts": {"test": "vitest"}}'}, "npm test"),
                 ({"package.json": '{"scripts": {}}'}, None),
                 ({"pyproject.toml": ""}, init.UNITTEST),
                 ({"package.json": '{"scripts": {"test": "jest"}}', "tests/a.py": ""}, None),
                 ({}, None)]
        for i, (files, want) in enumerate(cases):
            root = git_repo(os.path.join(self.tmp.name, str(i)), files)
            self.assertEqual(init.detect_test_command(root)[0], want, files)


class EnsureTrustedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cj = os.path.join(self.tmp.name, ".claude.json")
        spawn_worker._backed_up.clear()
        self.addCleanup(spawn_worker._backed_up.clear)

    def write(self, data):
        with open(self.cj, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def read(self, path=None):
        with open(path or self.cj, encoding="utf-8") as f:
            return json.load(f)

    def test_missing_file(self):
        note = spawn_worker.ensure_trusted("/w/a", self.cj)
        self.assertIn("skipped", note)
        self.assertFalse(os.path.exists(self.cj))

    def test_invalid_json_is_left_alone(self):
        self.write("{not json")
        self.assertIn("skipped", spawn_worker.ensure_trusted("/w/a", self.cj))
        with open(self.cj, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{not json")

    def test_new_entry(self):
        self.write({"numStartups": 3, "projects": {"/other": {"hasTrustDialogAccepted": False}}})
        spawn_worker.ensure_trusted("/w/a", self.cj)
        data = self.read()
        self.assertEqual(data["projects"]["/w/a"],
                         {"allowedTools": [], "mcpServers": {}, "hasTrustDialogAccepted": True})
        self.assertEqual(data["numStartups"], 3)
        self.assertIs(data["projects"]["/other"]["hasTrustDialogAccepted"], False)

    def test_existing_entry_only_gets_the_flag(self):
        self.write({"projects": {"/w/a": {"allowedTools": ["Bash"], "lastCost": 1.5}}})
        spawn_worker.ensure_trusted("/w/a", self.cj)
        self.assertEqual(self.read()["projects"]["/w/a"],
                         {"allowedTools": ["Bash"], "lastCost": 1.5, "hasTrustDialogAccepted": True})

    def test_already_trusted_does_not_write(self):
        self.write({"projects": {"/w/a": {"hasTrustDialogAccepted": True}}})
        self.assertIn("already", spawn_worker.ensure_trusted("/w/a", self.cj))
        self.assertFalse(os.path.exists(self.cj + ".orca-flow.bak"))

    def test_backup_created_once_per_run(self):
        original = {"projects": {}}
        self.write(original)
        spawn_worker.ensure_trusted("/w/a", self.cj)
        spawn_worker.ensure_trusted("/w/b", self.cj)
        self.assertEqual(self.read(self.cj + ".orca-flow.bak"), original)
        self.assertEqual(set(self.read()["projects"]), {"/w/a", "/w/b"})


PROMPT = ('You are the worker for the "init" package. First read /repo/.git/orca-flow/briefs/init/common.md '
          '(the rules for every worker here), then /repo/.git/orca-flow/briefs/init/brief.md')

TAIL_DELIVERED = [
    "╭───────────────────────────────────────────────╮",
    "│ ✻ Welcome to Claude Code!                     │",
    "╰───────────────────────────────────────────────╯",
    '> You are the worker for the "init" package. First read /repo/.git/orca-flow/briefs/init/common',
    ".md (the rules for every worker here), then /repo/.git/orca-flow/briefs/init/brief.md",
    "",
    "✻ Reading common.md… (esc to interrupt)",
    "────────────────────────────────────────────────",
    "❯ ",
    "────────────────────────────────────────────────",
    "  ? for shortcuts",
]
TAIL_NO_ECHO = [
    "────────────────────────────────────────────────",
    "❯ ",
    "────────────────────────────────────────────────",
    "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
]
TAIL_EXITED = [
    " Accessing workspace:",
    " /Users/me/orca/workspaces/proj/init",
    " Quick safety check: Is this a project you created or one you trust?",
    " ❯ 1. Yes, I trust this folder",
    "   2. No, exit",
    "",
    "me@host init % ",
    "",
]


class ClassifyTailTest(unittest.TestCase):
    def test_delivered(self):
        self.assertEqual(spawn_worker.classify_tail(TAIL_DELIVERED, PROMPT)[0], "delivered")

    def test_tui_without_echo(self):
        self.assertEqual(spawn_worker.classify_tail(TAIL_NO_ECHO, PROMPT)[0], "accepted, not confirmed")

    def test_exited_at_trust_dialog(self):
        verdict, note = spawn_worker.classify_tail(TAIL_EXITED, PROMPT)
        self.assertEqual(verdict, "worker exited")
        self.assertIn("init.py", note)

    def test_exited_without_dialog_has_no_init_hint(self):
        verdict, note = spawn_worker.classify_tail(["Bye!", "me@host:~/w/init$"], PROMPT)
        self.assertEqual(verdict, "worker exited")
        self.assertNotIn("init.py", note)

    def test_empty_tail(self):
        self.assertEqual(spawn_worker.classify_tail([], PROMPT)[0], "accepted, not confirmed")


class HandoverNoQueueTest(unittest.TestCase):
    def send(self, cfg, *extra):
        with tempfile.TemporaryDirectory() as tmp:
            root = git_repo(tmp)
            path = os.path.join(tmp, "cfg.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
            # Past the check, gh pr view fails in this remote-less repo, with a different error.
            env = dict(CLEAN_ENV, ORCA_FLOW_CONFIG=path, ORCA_FLOW_REPO=root)
            r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "handover.py"), "send", "7", *extra],
                               capture_output=True, text=True, env=env)
            return r.returncode, r.stdout + r.stderr

    def test_refused_when_disabled(self):
        code, out = self.send({"merge_queue": {"enabled": False, "merge_method": "rebase"}})
        self.assertEqual(code, 1)
        self.assertIn("no merge queue", out)
        self.assertIn("gh pr merge 7 --rebase", out)

    def test_force_goes_past_the_check(self):
        code, out = self.send({"merge_queue": {"enabled": False}}, "--force")
        self.assertNotIn("no merge queue", out)

    def test_enabled_by_default(self):
        code, out = self.send({})
        self.assertNotIn("no merge queue", out)


if __name__ == "__main__":
    unittest.main()
