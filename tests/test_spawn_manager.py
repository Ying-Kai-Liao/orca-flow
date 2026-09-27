"""Tests for spawn_manager.py: the plan, the managers/<slug>/ record, duplicate refusal and --list.

Run: python3 -m unittest discover -s tests

Each test runs the script as a subprocess against a throwaway git repo ($ORCA_FLOW_REPO), a
throwaway claude.json ($CLAUDE_CONFIG_DIR) and a fake `orca` ($ORCA_CLI_COMMAND) that logs
every call and answers from a JSON file, so nothing here touches the real Orca.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "spawn_manager.py")

# The fake orca: terminal list answers from $FAKE_ORCA_STATE's "terminals" (or fails when it
# says so), create hands out term_new, wait is always satisfied, and read shows the TUI with
# whatever was last sent, so the post-send check reports "delivered".
FAKE_ORCA = r'''#!/usr/bin/env python3
import json, os, sys
state_path = os.environ["FAKE_ORCA_STATE"]
with open(state_path) as f:
    state = json.load(f)
args = sys.argv[1:]
with open(os.environ["FAKE_ORCA_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
def ok(result):
    print(json.dumps({"ok": True, "result": result}))
cmd = args[:2]
if cmd == ["terminal", "list"]:
    if state.get("list_fails"):
        print(json.dumps({"ok": False, "error": {"code": "runtime_unavailable"}}))
    else:
        ok({"terminals": [{"handle": h} for h in state.get("terminals", [])], "truncated": False})
elif cmd == ["terminal", "create"]:
    ok({"terminal": {"handle": "term_new"}})
elif cmd == ["terminal", "wait"]:
    ok({"satisfied": True})
elif cmd == ["terminal", "send"]:
    state["sent"] = args[args.index("--text") + 1]
    with open(state_path, "w") as f:
        json.dump(state, f)
    ok({"accepted": True})
elif cmd == ["terminal", "read"]:
    ok({"tail": ["────", "❯ " + state.get("sent", ""), "────", "? for shortcuts"]})
else:
    print(json.dumps({"ok": False, "error": {"code": "unexpected " + " ".join(args)}}))
'''


class SpawnManagerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.root = os.path.join(self.tmp, "repo")
        subprocess.run(["git", "init", "-q", "-b", "main", self.root], check=True)
        self.managers = os.path.join(self.root, ".git", "orca-flow", "managers")
        self.brief = os.path.join(self.tmp, "task.md")
        with open(self.brief, "w") as f:
            f.write("# Fix the login page\n")
        self.state = os.path.join(self.tmp, "orca-state.json")
        self.log = os.path.join(self.tmp, "orca.log")
        self.set_orca(terminals=[])
        fake = os.path.join(self.tmp, "orca")
        with open(fake, "w") as f:
            f.write(FAKE_ORCA)
        os.chmod(fake, 0o755)
        claude_dir = os.path.join(self.tmp, "claude")
        os.makedirs(claude_dir)
        with open(os.path.join(claude_dir, ".claude.json"), "w") as f:
            json.dump({"projects": {}}, f)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("ORCA_FLOW_CONFIG", "ORCA_FLOW_DRY_RUN", "ORCA_TERMINAL_HANDLE")}
        self.env.update(ORCA_FLOW_REPO=self.root, ORCA_CLI_COMMAND=fake, FAKE_ORCA_STATE=self.state,
                        FAKE_ORCA_LOG=self.log, CLAUDE_CONFIG_DIR=claude_dir)

    def set_orca(self, **state):
        with open(self.state, "w") as f:
            json.dump(state, f)

    def run_script(self, *args):
        """(exit code, stdout)."""
        r = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, env=self.env)
        return r.returncode, r.stdout

    def run_json(self, *args):
        code, out = self.run_script(*args)
        return code, json.loads(out)

    def calls(self):
        try:
            with open(self.log) as f:
                return [json.loads(l) for l in f]
        except OSError:
            return []

    def seed(self, slug, **fields):
        d = os.path.join(self.managers, slug)
        os.makedirs(d, exist_ok=True)
        rec = {"slug": slug, "source": None, "source_id": None, "terminal": f"term_{slug}", "session": None,
               "status": "running", "started_at": "2026-09-27T10:00:00+08:00", **fields}
        with open(os.path.join(d, "manager.json"), "w") as f:
            json.dump(rec, f)
        return rec

    def record(self, slug):
        with open(os.path.join(self.managers, slug, "manager.json")) as f:
            return json.load(f)

    def test_dry_run_prints_the_plan_and_writes_nothing(self):
        code, res = self.run_json("--name", "asana-1234", "--brief", self.brief, "--source", "asana",
                                  "--source-id", "1234", "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["dir"], os.path.join(self.managers, "asana-1234"))
        self.assertEqual(res["title"], "manager:asana-1234")
        self.assertEqual(res["command"], "claude --model opus")
        self.assertTrue(res["prompt"].startswith('You are the orca-flow manager "asana-1234". Read '))
        self.assertIn(f"Keep progress in {os.path.join(self.managers, 'asana-1234', 'notes.md')}.", res["prompt"])
        self.assertNotIn("\n", res["prompt"])
        self.assertIn(f"--worktree path:{self.root}", res["commands"][0])
        self.assertFalse(os.path.exists(self.managers))
        # Only the read-only liveness check ran.
        self.assertEqual({tuple(c[:2]) for c in self.calls()}, {("terminal", "list")})

    def test_dry_run_from_env(self):
        self.env["ORCA_FLOW_DRY_RUN"] = "1"
        code, res = self.run_json("--name", "task-a", "--brief", self.brief)
        self.assertEqual(code, 0)
        self.assertTrue(res["dry_run"])
        self.assertFalse(os.path.exists(self.managers))

    def test_full_run_writes_running_record_and_sends_once(self):
        self.env["ORCA_TERMINAL_HANDLE"] = "term_dispatcher"
        code, res = self.run_json("--name", "asana-1234", "--brief", self.brief, "--source", "asana",
                                  "--source-id", "1234", "--dispatcher", "disp-1", "--bypass")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["terminal"], "term_new")
        self.assertEqual(res["delivery"], "delivered")
        creates = [c for c in self.calls() if c[:2] == ["terminal", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertIn(f"path:{self.root}", creates[0])
        self.assertIn("manager:asana-1234", creates[0])
        self.assertIn("claude --model opus --permission-mode bypassPermissions", creates[0])
        self.assertEqual(len([c for c in self.calls() if c[:2] == ["terminal", "send"]]), 1)
        d = os.path.join(self.managers, "asana-1234")
        rec = self.record("asana-1234")
        self.assertEqual(rec["status"], "running")
        self.assertEqual(rec["terminal"], "term_new")
        self.assertEqual((rec["source"], rec["source_id"], rec["session"], rec["model"]), ("asana", "1234", None, "opus"))
        self.assertEqual(rec["dispatcher"], {"session": "disp-1", "terminal": "term_dispatcher"})
        self.assertEqual(rec["brief"], os.path.join(d, "brief.md"))
        self.assertEqual(rec["main_checkout"], self.root)
        with open(rec["brief"]) as f:
            self.assertEqual(f.read(), "# Fix the login page\n")
        with open(rec["notes"]) as f:
            self.assertEqual(f.read(), "# Manager notes: asana-1234\n")

    def test_notes_are_kept_when_a_dead_record_is_replaced(self):
        self.seed("task-a", status="done")
        with open(os.path.join(self.managers, "task-a", "notes.md"), "w") as f:
            f.write("# earlier progress\n")
        code, res = self.run_json("--name", "task-a", "--brief", self.brief)
        self.assertEqual(code, 0, res)
        self.assertIn("replaced", res)
        with open(os.path.join(self.managers, "task-a", "notes.md")) as f:
            self.assertEqual(f.read(), "# earlier progress\n")

    def test_refuses_a_live_slug(self):
        rec = self.seed("task-a")
        self.set_orca(terminals=["term_task-a"])
        code, res = self.run_json("--name", "task-a", "--brief", self.brief)
        self.assertEqual(code, 1)
        self.assertFalse(res["ok"])
        self.assertEqual(res["existing"][0]["existing"], rec)
        self.assertEqual(self.record("task-a"), rec)
        self.assertFalse([c for c in self.calls() if c[:2] == ["terminal", "create"]])

    def test_refuses_a_live_source_id_under_another_slug(self):
        self.seed("old-name", source="asana", source_id="1234")
        self.set_orca(terminals=["term_old-name"])
        code, res = self.run_json("--name", "new-name", "--brief", self.brief, "--source", "asana",
                                  "--source-id", "1234", "--dry-run")
        self.assertEqual(code, 1)
        self.assertEqual(res["existing"][0]["slug"], "old-name")
        # Another id from the same source is fine.
        code, _ = self.run_json("--name", "new-name", "--brief", self.brief, "--source", "asana",
                                "--source-id", "999", "--dry-run")
        self.assertEqual(code, 0)

    def test_unknown_liveness_refuses(self):
        self.seed("task-a")
        self.set_orca(list_fails=True)
        code, res = self.run_json("--name", "task-a", "--brief", self.brief, "--dry-run")
        self.assertEqual(code, 1)
        self.assertIsNone(res["existing"][0]["live"])

    def test_force_overrides(self):
        self.seed("task-a")
        self.seed("other", source="asana", source_id="7")
        self.set_orca(terminals=["term_task-a", "term_other"])
        code, res = self.run_json("--name", "task-a", "--brief", self.brief, "--source", "asana",
                                  "--source-id", "7", "--force")
        self.assertEqual(code, 0, res)
        self.assertEqual({r["slug"] for r in res["forced_past"]}, {"task-a", "other"})
        self.assertEqual(self.record("task-a")["terminal"], "term_new")

    def test_dead_record_is_replaced_without_force(self):
        # Status running, but Orca no longer has the terminal.
        old = self.seed("task-a")
        code, res = self.run_json("--name", "task-a", "--brief", self.brief)
        self.assertEqual(code, 0, res)
        self.assertEqual(res["replaced"]["previous"], old)
        self.assertIn("replaced", res["replaced"]["note"])
        self.assertEqual(self.record("task-a")["status"], "running")

    def test_argument_checks(self):
        empty = os.path.join(self.tmp, "empty.md")
        open(empty, "w").close()
        for args, msg in [(["--name", "Bad_Name", "--brief", self.brief], "--name must be"),
                          (["--name", "task-a", "--brief", empty], "brief missing or empty"),
                          (["--name", "task-a", "--brief", self.brief, "--source-id", "1"], "--source-id needs --source"),
                          (["--name", "task-a", "--brief", self.brief, "--model", "claude-fable-5-1"], "Fable")]:
            code, res = self.run_json(*args, "--dry-run")
            self.assertEqual(code, 1, args)
            self.assertIn(msg, res["error"])

    def test_source_must_be_configured_when_sources_exist(self):
        with open(os.path.join(self.root, "orca-flow.json"), "w") as f:
            json.dump({"sources": {"asana": "docs/asana.md"}, "manager": {"model": "sonnet", "bypass_permissions": True}}, f)
        code, res = self.run_json("--name", "task-a", "--brief", self.brief, "--source", "linear", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("unknown source linear", res["error"])
        code, res = self.run_json("--name", "task-a", "--brief", self.brief, "--source", "asana", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(res["command"], "claude --model sonnet --permission-mode bypassPermissions")
        code, res = self.run_json("--name", "task-a", "--brief", self.brief, "--model", "haiku", "--no-bypass", "--dry-run")
        self.assertEqual(res["command"], "claude --model haiku")

    def test_list_text_and_json_with_an_unreadable_record(self):
        self.seed("task-a", source="asana", source_id="1", session="mgr-a")
        self.seed("task-b", status="done")
        os.makedirs(os.path.join(self.managers, "broken"))
        with open(os.path.join(self.managers, "broken", "manager.json"), "w") as f:
            f.write("{not json")
        self.set_orca(terminals=["term_task-a", "term_task-b"])
        code, out = self.run_script("--list")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("broken  (unreadable manager.json"))
        self.assertEqual(lines[1], "task-a  asana:1  running  live:yes  terminal:term_task-a  session:mgr-a  "
                                   "started:2026-09-27T10:00:00+08:00")
        self.assertIn("task-b  -  done  live:no", lines[2])
        code, res = self.run_json("--list", "--json")
        rows = {r["slug"]: r for r in res["managers"]}
        self.assertEqual((rows["task-a"]["live"], rows["task-b"]["live"], rows["broken"]["live"]), (True, False, None))
        self.assertIn("unreadable", rows["broken"]["note"])
        self.assertEqual(rows["task-a"]["source_id"], "1")


if __name__ == "__main__":
    unittest.main()
