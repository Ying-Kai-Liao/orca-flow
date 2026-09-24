"""Tests for config.py's set / unset / check / keys, the local overlay, and the settings that
change what spawn_worker.py renders (handoff, bypass, context_warn).

Run: python3 -m unittest discover -s tests

Everything runs against a throwaway git repo named by $ORCA_FLOW_REPO.
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
import config as cfgmod  # noqa: E402
import spawn_worker  # noqa: E402


class ConfigTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(os.path.join(self.tmp.name, "repo"))
        subprocess.run(["git", "init", "-q", "-b", "main", self.root], check=True)
        env = {k: v for k, v in os.environ.items() if k not in ("ORCA_FLOW_CONFIG", "ORCA_FLOW_DRY_RUN")}
        env["ORCA_FLOW_REPO"] = self.root
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.local = os.path.join(self.root, ".git", "orca-flow", "config.json")

    def cli(self, *args):
        """(exit code, stdout) of config.py main() with these arguments."""
        out = io.StringIO()
        code = 0
        with mock.patch.object(sys, "argv", ["config.py", *args]), contextlib.redirect_stdout(out):
            try:
                cfgmod.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
                if isinstance(e.code, str):
                    out.write(e.code)
        return code, out.getvalue()

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)


class SetTest(ConfigTestBase):
    def test_set_creates_repo_file_and_parses_types(self):
        self.assertEqual(self.cli("set", "handoff.enabled", "false")[0], 0)
        self.cli("set", "worker.context_warn", "0.5")
        self.cli("set", "worker.context_window", "1000000")
        self.cli("set", "worker.test_command", "pytest {files}")
        data = self.read(os.path.join(self.root, "orca-flow.json"))
        self.assertEqual(data, {"handoff": {"enabled": False},
                                "worker": {"context_warn": 0.5, "context_window": 1000000,
                                           "test_command": "pytest {files}"}})

    def test_set_writes_the_file_in_use(self):
        dot = os.path.join(self.root, ".claude", "orca-flow.json")
        self.write(dot, {"language": "English", "custom": 1})
        self.cli("set", "board.stale_min", "45")
        self.assertEqual(self.read(dot), {"language": "English", "custom": 1, "board": {"stale_min": 45}})
        self.assertFalse(os.path.exists(os.path.join(self.root, "orca-flow.json")))

    def test_local_overlays_repo_file(self):
        self.cli("set", "worker.model", "opus")
        self.cli("set", "worker.model", "sonnet", "--local")
        self.assertEqual(self.read(self.local), {"worker": {"model": "sonnet"}})
        cfg = cfgmod.load()
        self.assertEqual(cfg["worker"]["model"], "sonnet")
        self.assertEqual(cfg["config_files"], [os.path.join(self.root, "orca-flow.json"), self.local])
        code, out = self.cli("set", "worker.model", "haiku")
        self.assertIn("and it wins", out)

    def test_append_and_unset(self):
        self.cli("set", "worker.checks", "npm run lint", "--append")
        self.cli("set", "worker.checks", "npx tsc --noEmit", "--append")
        path = os.path.join(self.root, "orca-flow.json")
        self.assertEqual(self.read(path)["worker"]["checks"], ["npm run lint", "npx tsc --noEmit"])
        self.cli("unset", "worker.checks")
        self.assertEqual(self.read(path), {"worker": {}})
        self.assertEqual(cfgmod.load()["worker"]["checks"], [])

    def test_refuses_unknown_key_and_bad_type(self):
        code, out = self.cli("set", "worker.contex_warn", "0.5")
        self.assertNotEqual(code, 0)
        self.assertIn("worker.context_warn", out)
        code, out = self.cli("set", "handoff.enabled", "maybe")
        self.assertNotEqual(code, 0)
        code, out = self.cli("set", "worker.test_slots", "1.5")
        self.assertNotEqual(code, 0)
        code, out = self.cli("set", "merge_queue.merge_method", "fast-forward")
        self.assertNotEqual(code, 0)
        code, out = self.cli("set", "handoff.digest", "true", "--append")
        self.assertNotEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, "orca-flow.json")))
        self.assertEqual(self.cli("set", "my.note", "hello", "--force")[0], 0)

    def test_dry_run_writes_nothing(self):
        code, out = self.cli("set", "handoff.enabled", "false", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would set handoff.enabled = false", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "orca-flow.json")))


class CheckTest(ConfigTestBase):
    def test_check_reports_errors_and_unknown_keys(self):
        self.write(os.path.join(self.root, "orca-flow.json"),
                   {"worker": {"context_warn": "high", "test_slots": True}, "notes": "x",
                    "merge_queue": {"targets": [{"name": "prod"}]}})
        code, out = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("error: worker.context_warn", out)
        self.assertIn("error: worker.test_slots", out)
        self.assertIn("error: merge_queue.targets[0]", out)
        self.assertIn("note: notes", out)

    def test_check_clean(self):
        self.cli("set", "handoff.max_continues", "3")
        self.assertEqual(self.cli("check")[0], 0)

    def test_keys_marks_changed(self):
        self.cli("set", "cleanup.idle_hours", "12")
        code, out = self.cli("keys")
        line = next(l for l in out.splitlines() if "cleanup.idle_hours" in l)
        self.assertTrue(line.startswith("*"))


class SettingsTest(ConfigTestBase):
    def test_context_warn_fraction_or_tokens(self):
        cfg = cfgmod.load()
        self.assertEqual(cfgmod.context_warn_tokens(cfg), 70000)
        cfg["worker"]["context_warn"] = 90000
        self.assertEqual(cfgmod.context_warn_tokens(cfg), 90000)

    def test_handoff_rule_follows_config(self):
        cfg = cfgmod.load()
        on = spawn_worker.render_rules(cfg, "/x/test-lock.sh", "origin/main")
        self.assertIn("## Handing off and continuing", on)
        self.assertIn("starting with `WRAP UP`", on)
        self.assertNotIn("{{", on)
        cfg["handoff"]["wrap_up_message"] = "STOP NOW: commit, push, write handoff.md, stop."
        self.assertIn("starting with `STOP NOW`", spawn_worker.render_rules(cfg, "/x", "origin/main"))
        cfg["handoff"]["enabled"] = False
        off = spawn_worker.render_rules(cfg, "/x", "origin/main")
        self.assertNotIn("Handing off", off)
        self.assertIn("## Continuing", off)

    def test_continue_counter(self):
        d = os.path.join(self.tmp.name, "brief")
        os.makedirs(d)
        self.assertEqual(spawn_worker.bump_continues(d, dry=True), 1)
        self.assertEqual(spawn_worker.bump_continues(d, dry=False), 1)
        self.assertEqual(spawn_worker.bump_continues(d, dry=False), 2)


if __name__ == "__main__":
    unittest.main()
