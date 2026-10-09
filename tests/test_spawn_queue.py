"""Tests for spawn_queue.py: one queue at a time, in a visible Orca terminal.

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

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "spawn_queue.py")

# The fake orca: repo list has one repo at $FAKE_ORCA_REPO, worktree list answers from the
# state's "worktrees", terminal list from its "terminals" (dicts, as Orca prints them), and
# create / wait / send / read behave like a TUI that went idle and took the prompt.
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
def save():
    with open(state_path, "w") as f:
        json.dump(state, f)
cmd = args[:2]
terminals = state.get("terminals", [])
if cmd == ["repo", "list"]:
    ok({"repos": [{"id": "repo1", "path": os.environ["FAKE_ORCA_REPO"]}]})
elif cmd == ["worktree", "list"]:
    ok({"worktrees": state.get("worktrees", [])})
elif cmd == ["worktree", "ps"]:
    if state.get("ps_fails"):
        print(json.dumps({"ok": False, "error": {"code": "runtime_unavailable"}}))
    else:
        ok({"worktrees": state.get("ps", [])})
elif cmd == ["worktree", "create"]:
    wt = {"id": "wt_new", "path": state["new_worktree_path"], "displayName": args[args.index("--name") + 1]}
    state.setdefault("worktrees", []).append(wt)
    save()
    ok({"worktree": wt})
elif cmd == ["terminal", "list"]:
    if state.get("list_fails"):
        print(json.dumps({"ok": False, "error": {"code": "runtime_unavailable"}}))
    else:
        ok({"terminals": terminals, "truncated": bool(state.get("truncated"))})
elif cmd == ["terminal", "show"]:
    h = args[args.index("--terminal") + 1]
    t = next((t for t in terminals + state.get("unlisted", []) if t["handle"] == h), None)
    print(json.dumps({"ok": True, "result": {"terminal": t}} if t else {"ok": False, "error": {"code": "not_found"}}))
elif cmd == ["terminal", "close"]:
    h = args[args.index("--terminal") + 1]
    if not state.get("close_sticks"):
        state["terminals"] = [t for t in terminals if t["handle"] != h]
        save()
    ok({"closed": True})
elif cmd == ["terminal", "create"]:
    ok({"terminal": {"handle": "term_new"}})
elif cmd == ["terminal", "wait"]:
    ok({"satisfied": True})
elif cmd == ["terminal", "send"]:
    state["sent"] = args[args.index("--text") + 1]
    save()
    ok({"accepted": True})
elif cmd == ["terminal", "read"]:
    ok({"tail": ["────", "❯ " + state.get("sent", ""), "────", "? for shortcuts"]})
else:
    print(json.dumps({"ok": False, "error": {"code": "unexpected " + " ".join(args)}}))
'''


class SpawnQueueTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.root = os.path.join(self.tmp, "repo")
        subprocess.run(["git", "init", "-q", "-b", "main", self.root], check=True)
        self.queue_dir = os.path.join(self.root, ".git", "orca-flow", "queue")
        self.wt_path = os.path.join(self.tmp, "worktrees", "merge-queue")
        os.makedirs(self.wt_path)
        self.wt = {"id": "wt_q", "path": self.wt_path, "displayName": "merge-queue"}
        self.state = os.path.join(self.tmp, "orca-state.json")
        self.log = os.path.join(self.tmp, "orca.log")
        self.set_orca()
        fake = os.path.join(self.tmp, "orca")
        with open(fake, "w") as f:
            f.write(FAKE_ORCA)
        os.chmod(fake, 0o755)
        self.claude_dir = os.path.join(self.tmp, "claude")
        os.makedirs(self.claude_dir)
        with open(os.path.join(self.claude_dir, ".claude.json"), "w") as f:
            json.dump({"projects": {}}, f)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("ORCA_FLOW_CONFIG", "ORCA_FLOW_DRY_RUN", "ORCA_TERMINAL_HANDLE", "ORCA_FLOW_SESSION")}
        self.env.update(ORCA_FLOW_REPO=self.root, ORCA_CLI_COMMAND=fake, FAKE_ORCA_STATE=self.state,
                        FAKE_ORCA_LOG=self.log, FAKE_ORCA_REPO=self.root, CLAUDE_CONFIG_DIR=self.claude_dir)

    def set_orca(self, worktrees=None, terminals=(), **extra):
        with open(self.state, "w") as f:
            json.dump({"worktrees": [self.wt] if worktrees is None else worktrees, "terminals": list(terminals),
                       "new_worktree_path": self.wt_path, **extra}, f)

    def orca_state(self):
        with open(self.state) as f:
            return json.load(f)

    def term(self, handle, orphaned=False, worktree="wt_q", agent="claude", tab=None, leaf=None):
        t = {"handle": handle, "orphaned": orphaned, "worktreeId": worktree, "agentIdentity": agent, "title": "q"}
        if tab:
            t.update(tabId=tab, leafId=leaf)
        return t

    def ps(self, *agents):
        """`orca worktree ps` for the queue worktree: (paneKey, state) pairs."""
        return [{"worktreeId": "wt_q", "agents": [{"paneKey": k, "state": s} for k, s in agents]}]

    def register(self, terminal="term_old", session="queue-old"):
        os.makedirs(self.queue_dir, exist_ok=True)
        st = {"active": {"session": session, "terminal": terminal, "cwd": self.wt_path,
                         "started_at": "2026-09-23T00:00:00Z", "batches": 11}, "retired": []}
        with open(os.path.join(self.queue_dir, "state.json"), "w") as f:
            json.dump(st, f)
        return st["active"]

    def queue_state(self):
        with open(os.path.join(self.queue_dir, "state.json")) as f:
            return json.load(f)

    def run_json(self, *args):
        r = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, env=self.env)
        try:
            return r.returncode, json.loads(r.stdout)
        except ValueError:
            self.fail(f"no JSON (exit {r.returncode}): {r.stdout}\n{r.stderr}")

    def calls(self, *prefix):
        try:
            with open(self.log) as f:
                rows = [json.loads(l) for l in f]
        except OSError:
            return []
        return [c for c in rows if c[:len(prefix)] == list(prefix)]

    def test_dry_run_writes_nothing(self):
        self.register()
        self.set_orca(terminals=[self.term("term_old", orphaned=True)])
        before = self.queue_state()
        code, res = self.run_json("--replace", "--dry-run")
        self.assertEqual(code, 0, res)
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["queue"]["state"], "hidden")
        self.assertEqual(res["close"], ["term_old"])
        self.assertEqual(res["retire"], "replaced by spawn_queue")
        self.assertEqual(res["title"], "merge-queue")
        self.assertEqual(res["command"], "claude --model opus")
        self.assertNotIn("\n", res["prompt"])
        self.assertTrue(res["prompt"].startswith("You are this repo's merge queue. Use the orca-flow skill"))
        self.assertIn("scripts/handover.py queue start --session <your ListAgents name>, then handover.py list.", res["prompt"])
        self.assertEqual(self.queue_state(), before)
        # Only read-only orca calls ran.
        self.assertEqual({tuple(c[:2]) for c in self.calls()},
                         {("repo", "list"), ("worktree", "list"), ("terminal", "list")})
        with open(os.path.join(self.claude_dir, ".claude.json")) as f:
            self.assertEqual(json.load(f), {"projects": {}})

    def test_dry_run_from_env_creates_no_queue_folder(self):
        self.env["ORCA_FLOW_DRY_RUN"] = "1"
        code, res = self.run_json()
        self.assertEqual(code, 0, res)
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["queue"]["state"], "none")
        self.assertFalse(os.path.exists(self.queue_dir))

    def test_warns_about_unconfigured_check_deploy_and_status_file(self):
        code, res = self.run_json("--dry-run")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["unconfigured"],
                         ["worker.full_check_command", "merge_queue.targets", "merge_queue.state_file"])
        self.assertIn("runs no full check; deploys nothing; keeps no status file", res["warning"])
        code, res = self.run_json()
        self.assertEqual(code, 0, res)
        self.assertEqual(res["delivery"], "delivered")
        self.assertIn("deploys nothing", res["warning"])

    def test_no_warning_when_check_deploy_and_status_file_are_set(self):
        with open(os.path.join(self.root, "orca-flow.json"), "w") as f:
            json.dump({"worker": {"full_check_command": "npm test"},
                       "merge_queue": {"state_file": "NOW.md",
                                       "targets": [{"name": "demo", "deploy": ["./deploy.sh demo"]}]}}, f)
        code, res = self.run_json("--dry-run")
        self.assertEqual(code, 0, res)
        self.assertNotIn("warning", res)
        self.assertNotIn("unconfigured", res)

    def test_refuses_when_the_repo_has_no_queue(self):
        with open(os.path.join(self.root, "orca-flow.json"), "w") as f:
            json.dump({"merge_queue": {"enabled": False}}, f)
        code, res = self.run_json("--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("no merge queue", res["error"])
        self.assertEqual(self.calls(), [])

    def test_no_queue_registered_starts_one_and_sends_once(self):
        code, res = self.run_json("--bypass")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["delivery"], "delivered")
        creates = self.calls("terminal", "create")
        self.assertEqual(len(creates), 1)
        self.assertIn("id:wt_q", creates[0])
        self.assertIn("merge-queue", creates[0])
        self.assertIn("claude --model opus --permission-mode bypassPermissions", creates[0])
        self.assertEqual(len(self.calls("terminal", "send")), 1)
        self.assertEqual(self.calls("worktree", "create"), [])
        self.assertEqual(self.calls("terminal", "close"), [])
        with open(os.path.join(self.claude_dir, ".claude.json")) as f:
            self.assertTrue(json.load(f)["projects"][self.wt_path]["hasTrustDialogAccepted"])

    def test_live_queue_refuses_even_with_replace(self):
        active = self.register()
        self.set_orca(terminals=[self.term("term_old")])
        for args in ([], ["--replace"]):
            code, res = self.run_json(*args)
            self.assertEqual(code, 1, args)
            self.assertIn("a queue is running: queue-old, term_old", res["error"])
        self.assertEqual(self.queue_state()["active"], active)
        self.assertEqual(self.calls("terminal", "create"), [])
        self.assertEqual(self.calls("terminal", "close"), [])

    def test_hidden_queue_refuses_without_replace(self):
        active = self.register()
        self.set_orca(terminals=[self.term("term_old", orphaned=True)])
        code, res = self.run_json()
        self.assertEqual(code, 1)
        self.assertIn("is hidden", res["error"])
        self.assertIn("--replace", res["error"])
        self.assertEqual(res["queue"]["state"], "hidden")
        self.assertEqual(self.queue_state()["active"], active)
        self.assertEqual(self.calls("terminal", "close"), [])
        self.assertEqual(self.calls("terminal", "create"), [])

    def test_replace_closes_retires_starts_and_sends_once(self):
        active = self.register()
        self.set_orca(terminals=[self.term("term_old", orphaned=True)])
        code, res = self.run_json("--replace")
        self.assertEqual(code, 0, res)
        self.assertEqual(self.calls("terminal", "close"), [["terminal", "close", "--terminal", "term_old", "--json"]])
        st = self.queue_state()
        self.assertIsNone(st["active"])
        self.assertEqual(st["retired"][-1]["session"], active["session"])
        self.assertEqual(st["retired"][-1]["reason"], "replaced by spawn_queue")
        self.assertEqual(len(self.calls("terminal", "create")), 1)
        self.assertEqual(len(self.calls("terminal", "send")), 1)
        self.assertEqual(res["terminal"], "term_new")
        # Close happened before the new terminal was created.
        order = [tuple(c[:2]) for c in self.calls("terminal")]
        self.assertLess(order.index(("terminal", "close")), order.index(("terminal", "create")))

    def test_close_that_leaves_the_terminal_retires_and_starts_nothing(self):
        active = self.register()
        self.set_orca(terminals=[self.term("term_old", orphaned=True)], close_sticks=True)
        code, res = self.run_json("--replace")
        self.assertEqual(code, 1)
        self.assertIn("close did not stop term_old; nothing was retired or started", res["error"])
        self.assertEqual(len(self.calls("terminal", "close")), 1)
        self.assertEqual(self.queue_state()["active"], active)
        self.assertEqual(self.calls("terminal", "create"), [])

    def test_gone_terminal_is_retired_and_replaced_without_replace(self):
        self.register()
        self.set_orca(terminals=[])
        code, res = self.run_json()
        self.assertEqual(code, 0, res)
        self.assertEqual(res["queue"]["state"], "gone")
        st = self.queue_state()
        self.assertIsNone(st["active"])
        self.assertEqual(st["retired"][-1]["reason"], "terminal gone")
        self.assertEqual(self.calls("terminal", "close"), [])
        self.assertEqual(len(self.calls("terminal", "send")), 1)

    def test_truncated_list_asks_for_the_one_handle(self):
        self.register()
        self.set_orca(terminals=[], truncated=True, unlisted=[self.term("term_old", orphaned=True)])
        code, res = self.run_json("--dry-run")
        self.assertEqual(code, 1)
        self.assertEqual(res["queue"]["state"], "hidden")

    def test_registration_without_handle_is_unknown(self):
        self.register(terminal=None)
        code, res = self.run_json("--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("unknown", res["error"])
        code, res = self.run_json("--replace", "--dry-run")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["close"], [])
        self.assertEqual(res["retire"], "replaced by spawn_queue")

    def test_unregistered_agent_in_the_queue_worktree_blocks(self):
        # E.g. a queue started a minute ago that hasn't run `queue start` yet. A terminal in
        # another worktree, or a plain shell, doesn't count.
        self.set_orca(terminals=[self.term("term_x"), self.term("term_other_wt", worktree="wt_else"),
                                 self.term("term_shell", agent=None)])
        code, res = self.run_json()
        self.assertEqual(code, 1)
        self.assertEqual([t["handle"] for t in res["queue"]["other_agents"]], ["term_x"])
        code, res = self.run_json("--replace")
        self.assertEqual(code, 0, res)
        self.assertEqual(self.calls("terminal", "close"), [["terminal", "close", "--terminal", "term_x", "--json"]])

    def test_rotation_from_the_retiring_queue(self):
        # The retiring queue runs spawn_queue.py from its own terminal in the queue worktree.
        self.env["ORCA_TERMINAL_HANDLE"] = "term_old"
        self.register()
        self.set_orca(terminals=[self.term("term_old")])
        code, res = self.run_json("--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("run handover.py queue retire first", res["error"])
        with open(os.path.join(self.queue_dir, "state.json"), "w") as f:
            json.dump({"active": None, "retired": []}, f)
        code, res = self.run_json()
        self.assertEqual(code, 0, res)
        self.assertNotIn("other_agents", res["queue"])
        self.assertEqual(self.calls("terminal", "close"), [])
        self.assertEqual(len(self.calls("terminal", "send")), 1)

    def retire_leftover(self, handle="term_left"):
        """state.json after a rotation: the old queue retired, nothing active."""
        os.makedirs(self.queue_dir, exist_ok=True)
        with open(os.path.join(self.queue_dir, "state.json"), "w") as f:
            json.dump({"active": None, "retired": [{"session": "queue-old", "terminal": handle,
                                                     "retired_at": "2026-10-08T00:00:00Z", "reason": "rotation"}]}, f)

    def test_idle_retired_leftover_is_closed_without_replace(self):
        self.retire_leftover()
        self.set_orca(terminals=[self.term("term_left", tab="t1", leaf="l1")], ps=self.ps(("t1:l1", "idle")))
        code, res = self.run_json("--dry-run")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["close"], ["term_left"])
        self.assertEqual([t["handle"] for t in res["queue"]["leftovers"]], ["term_left"])
        self.assertNotIn("other_agents", res["queue"])
        self.assertEqual(self.calls("terminal", "close"), [])
        code, res = self.run_json()
        self.assertEqual(code, 0, res)
        self.assertEqual(self.calls("terminal", "close"), [["terminal", "close", "--terminal", "term_left", "--json"]])
        self.assertEqual(len(self.calls("terminal", "create")), 1)

    def test_leftover_that_is_busy_hidden_or_unknown_still_blocks(self):
        self.retire_leftover()
        for terminals, ps, extra in (
                ([self.term("term_left", tab="t1", leaf="l1")], self.ps(("t1:l1", "working")), {}),
                ([self.term("term_left", tab="t1", leaf="l1", orphaned=True)], self.ps(("t1:l1", "idle")), {}),
                ([self.term("term_left")], self.ps(("t1:l1", "idle")), {}),          # no pane to match
                ([self.term("term_left", tab="t1", leaf="l1")], [], {"ps_fails": True})):
            self.set_orca(terminals=terminals, ps=ps, **extra)
            code, res = self.run_json()
            self.assertEqual(code, 1, (terminals, ps, extra))
            self.assertIn("--replace", res["error"])
            self.assertNotIn("leftovers", res["queue"])
        self.assertEqual(self.calls("terminal", "close"), [])

    def test_unretired_idle_agent_still_blocks(self):
        # Idle but never registered as retired: maybe a queue that hasn't run `queue start`.
        self.set_orca(terminals=[self.term("term_x", tab="t1", leaf="l1")], ps=self.ps(("t1:l1", "idle")))
        code, res = self.run_json()
        self.assertEqual(code, 1)
        self.assertEqual([t["handle"] for t in res["queue"]["other_agents"]], ["term_x"])

    def test_start_records_the_new_terminal_for_queue_start(self):
        code, res = self.run_json()
        self.assertEqual(code, 0, res)
        self.assertEqual(self.queue_state()["spawned"]["terminal"], "term_new")

    def test_worktree_created_when_missing(self):
        self.set_orca(worktrees=[])
        code, res = self.run_json("--dry-run")
        self.assertEqual(code, 0, res)
        self.assertFalse(res["worktree"]["exists"])
        self.assertIn("worktree create --repo id:repo1 --name merge-queue --no-parent --setup run", res["commands"][0])
        code, res = self.run_json()
        self.assertEqual(code, 0, res)
        creates = self.calls("worktree", "create")
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0][:10], ["worktree", "create", "--repo", "id:repo1", "--name", "merge-queue",
                                           "--no-parent", "--setup", "run", "--comment"])
        self.assertIn("id:wt_new", self.calls("terminal", "create")[0])
        self.assertEqual(res["worktree"]["path"], self.wt_path)

    def test_fable_refused_and_model_from_config(self):
        code, res = self.run_json("--model", "claude-fable-5-1", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("Fable", res["error"])
        with open(os.path.join(self.root, "orca-flow.json"), "w") as f:
            json.dump({"merge_queue": {"model": "fable"}, "worker": {"bypass_permissions": True}}, f)
        code, res = self.run_json("--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("Fable", res["error"])
        with open(os.path.join(self.root, "orca-flow.json"), "w") as f:
            json.dump({"merge_queue": {"model": "sonnet"}, "worker": {"bypass_permissions": True}}, f)
        code, res = self.run_json("--dry-run")
        self.assertEqual(res["command"], "claude --model sonnet --permission-mode bypassPermissions")
        code, res = self.run_json("--model", "haiku", "--no-bypass", "--dry-run")
        self.assertEqual(res["command"], "claude --model haiku")

    def test_orca_down_refuses(self):
        self.set_orca(list_fails=True)
        code, res = self.run_json("--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("orca terminal list failed", res["error"])


if __name__ == "__main__":
    unittest.main()
