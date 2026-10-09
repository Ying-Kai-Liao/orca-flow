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

    def test_append_does_not_copy_the_other_file(self):
        self.write(os.path.join(self.root, "orca-flow.json"), {})
        self.cli("set", "worker.checks", '["local-only"]', "--local")
        self.cli("set", "worker.checks", "npm run lint", "--append")
        self.assertEqual(self.read(os.path.join(self.root, "orca-flow.json"))["worker"]["checks"], ["npm run lint"])

    def test_context_warn_fractional_tokens_rejected(self):
        self.assertNotEqual(self.cli("set", "worker.context_warn", "1.5")[0], 0)
        self.assertEqual(self.cli("set", "worker.context_warn", "90000")[0], 0)

    def test_symlinked_config_stays_a_link(self):
        real = os.path.join(self.tmp.name, "shared.json")
        self.write(real, {})
        link = os.path.join(self.root, "orca-flow.json")
        os.symlink(real, link)
        self.cli("set", "board.stale_min", "45")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(self.read(real), {"board": {"stale_min": 45}})

    def test_dry_run_writes_nothing(self):
        code, out = self.cli("set", "handoff.enabled", "false", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would set handoff.enabled = false", out)
        self.assertIn('+    "enabled": false', out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "orca-flow.json")))


class ReviewFixesTest(ConfigTestBase):
    def test_null_takes_the_default(self):
        self.cli("set", "main_checkout.guard", "null")
        self.cli("set", "cleanup.idle_hours", "null")
        cfg = cfgmod.load()
        self.assertIs(cfg["main_checkout"]["guard"], True)
        self.assertEqual(cfg["cleanup"]["idle_hours"], 3)
        self.assertIsNone(cfg["worker"]["test_command"])  # a null default stays null

    def test_plain_set_never_writes_the_local_file(self):
        self.cli("set", "worker.model", "sonnet", "--local")
        self.cli("set", "language", "French")
        self.assertEqual(self.read(self.local), {"worker": {"model": "sonnet"}})
        self.assertEqual(self.read(os.path.join(self.root, "orca-flow.json")), {"language": "French"})

    def test_check_fails_on_a_section_that_is_not_an_object(self):
        self.write(os.path.join(self.root, "orca-flow.json"), {"board": [1], "worker": "x"})
        code, out = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("error: board: must be an object", out)
        self.assertIn("error: worker: must be an object", out)

    def test_zero_is_a_value(self):
        cfg = cfgmod.load()
        cfg["worker"]["context_warn"] = 0
        self.assertEqual(cfgmod.context_warn_tokens(cfg), 0)


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

    def test_sources_must_be_an_object_of_paths(self):
        os.makedirs(os.path.join(self.root, "docs"))
        open(os.path.join(self.root, "docs", "asana.md"), "w").close()
        self.write(os.path.join(self.root, "orca-flow.json"),
                   {"sources": {"asana": "docs/asana.md", "linear": "docs/linear.md", "bad": 3}})
        code, out = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("error: sources.bad: expected a path string", out)
        self.assertIn("note: sources.linear: docs/linear.md does not exist", out)
        self.assertNotIn("sources.asana", out)  # a source name is not an unknown key
        self.write(os.path.join(self.root, "orca-flow.json"), {"sources": ["docs/asana.md"]})
        self.assertIn("error: sources: expected object", self.cli("check")[1])

    def test_manager_defaults(self):
        cfg = cfgmod.load()
        self.assertEqual(cfg["manager"], {"model": "opus", "bypass_permissions": False})
        self.assertEqual(cfg["sources"], {})
        _, out = self.cli("keys")
        for key in ("manager.model", "manager.bypass_permissions", "sources"):
            self.assertIn(key, out)

    def test_keys_marks_changed(self):
        self.cli("set", "cleanup.idle_hours", "12")
        code, out = self.cli("keys")
        line = next(l for l in out.splitlines() if "cleanup.idle_hours" in l)
        self.assertTrue(line.startswith("*"))


class LocalListsTest(ConfigTestBase):
    def setUp(self):
        super().setUp()
        self.repo_file = os.path.join(self.root, "orca-flow.json")

    def test_additive_lists_are_repo_plus_local(self):
        self.write(self.repo_file, {"worker": {"extra_rules": ["never print secrets", "read the design docs"],
                                               "checks": ["npm run lint"]},
                                    "keep_worktrees": ["infra"]})
        self.write(self.local, {"worker": {"extra_rules": ["mine", "read the design docs"], "checks": []},
                                "keep_worktrees": None})
        cfg = cfgmod.load()
        self.assertEqual(cfg["worker"]["extra_rules"], ["never print secrets", "read the design docs", "mine"])
        self.assertEqual(cfg["worker"]["checks"], ["npm run lint"])  # local [] adds nothing
        self.assertIn("infra", cfg["keep_worktrees"])  # local null doesn't wipe the repo list
        self.assertEqual(self.cli("get", "worker.extra_rules")[1].strip(),
                         json.dumps(["never print secrets", "read the design docs", "mine"]))

    def test_other_lists_still_replace_and_check_warns(self):
        self.write(self.repo_file, {"merge_queue": {"targets": [{"name": "prod", "deploy": ["a"]}]}})
        self.write(self.local, {"merge_queue": {"targets": [{"name": "staging", "deploy": ["b"]}]},
                                "worker": {"extra_rules": ["mine"]}})
        self.assertEqual([t["name"] for t in cfgmod.load()["merge_queue"]["targets"]], ["staging"])
        code, out = self.cli("check")
        self.assertEqual(code, 0)
        self.assertIn(f"warning: merge_queue.targets: {self.local} replaces the list in {self.repo_file}", out)
        self.assertNotIn("warning: worker.extra_rules", out)

    def test_no_warning_over_an_empty_repo_list(self):
        self.write(self.repo_file, {"merge_queue": {"targets": []}})
        self.write(self.local, {"merge_queue": {"targets": [{"name": "staging", "deploy": ["b"]}]}})
        self.assertNotIn("warning", self.cli("check")[1])

    def test_repo_list_still_replaces_the_default(self):
        self.write(self.repo_file, {"main_checkout": {"allow_prefixes": ["docs/"]}})
        self.write(self.local, {"main_checkout": {"allow_prefixes": ["notes/"]}})
        self.assertEqual(cfgmod.load()["main_checkout"]["allow_prefixes"], ["docs/", "notes/"])

    def test_only_local_file(self):
        self.write(self.local, {"worker": {"extra_rules": ["mine"]}})
        self.assertEqual(cfgmod.load()["worker"]["extra_rules"], ["mine"])

    def test_local_append_touches_only_the_local_list(self):
        self.write(self.repo_file, {"worker": {"extra_rules": ["repo rule"]}})
        self.cli("set", "worker.extra_rules", "mine", "--local", "--append")
        self.cli("set", "worker.extra_rules", "mine too", "--local", "--append")
        self.assertEqual(self.read(self.local), {"worker": {"extra_rules": ["mine", "mine too"]}})
        self.assertEqual(self.read(self.repo_file), {"worker": {"extra_rules": ["repo rule"]}})
        self.assertEqual(cfgmod.load()["worker"]["extra_rules"], ["repo rule", "mine", "mine too"])
        # Without --append, --local replaces the local list only; the repo's items stay.
        self.cli("set", "worker.extra_rules", '["other"]', "--local")
        self.assertEqual(cfgmod.load()["worker"]["extra_rules"], ["repo rule", "other"])
        self.assertIn("and its items are added", self.cli("set", "worker.extra_rules", '["r"]')[1])

    def test_always_tests_key(self):
        self.assertEqual(cfgmod.load()["worker"]["always_tests"], [])
        self.assertEqual(self.cli("set", "worker.always_tests", "tests/test_registry.py", "--append")[0], 0)
        self.assertEqual(cfgmod.load()["worker"]["always_tests"], ["tests/test_registry.py"])
        self.assertIn("worker.always_tests", self.cli("keys")[1])


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
        self.assertEqual(spawn_worker.read_continues(d), 0)
        spawn_worker.write_continues(d, 2)
        self.assertEqual(spawn_worker.read_continues(d), 2)


if __name__ == "__main__":
    unittest.main()
