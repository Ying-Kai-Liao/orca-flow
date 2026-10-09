"""Tests for handover.py: notifying the registered queue, migration clashes, retarget, prune,
stale registrations, and the terminal as identity when no session name is known.

Run: python3 -m unittest tests.test_handover

Each test runs the script as a subprocess against a throwaway git repo ($ORCA_FLOW_REPO), a
fake `gh` first on $PATH and a fake `orca` ($ORCA_CLI_COMMAND). Both log every call and
answer from one JSON state file, so nothing here touches GitHub or the real Orca.
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "handover.py")

# gh pr view <n> --json ...: the PR from the state's "prs", or a failure.
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_STATE"]) as f:
    state = json.load(f)
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(["gh"] + args) + "\n")
if args[:2] == ["pr", "view"] and args[2] in state.get("prs", {}):
    print(json.dumps(state["prs"][args[2]]))
else:
    sys.stderr.write("no such PR\n")
    sys.exit(1)
'''

# orca terminal list / show / send, answered from the state's "terminals".
FAKE_ORCA = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_STATE"]) as f:
    state = json.load(f)
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(["orca"] + args) + "\n")
terminals = state.get("terminals", [])
if args[:2] == ["terminal", "list"]:
    if state.get("list_fails"):
        print(json.dumps({"ok": False, "error": {"code": "runtime_unavailable"}}))
    else:
        print(json.dumps({"ok": True, "result": {"terminals": [{"handle": h} for h in terminals]}}))
elif args[:2] == ["terminal", "show"]:
    h = args[args.index("--terminal") + 1]
    print(json.dumps({"ok": True, "result": {"terminal": {"handle": h}}} if h in terminals
                     else {"ok": False, "error": {"code": "not_found"}}))
elif args[:2] == ["terminal", "send"]:
    print(json.dumps({"ok": True, "result": {"accepted": True}}))
else:
    print(json.dumps({"ok": False, "error": {"code": "unexpected"}}))
    sys.exit(1)
'''


def ago(days):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class HandoverTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.root = os.path.join(self.tmp, "repo")
        subprocess.run(["git", "init", "-q", "-b", "main", self.root], check=True)
        os.makedirs(os.path.join(self.root, "migrations"))
        with open(os.path.join(self.root, "migrations", "063_users.sql"), "w") as f:
            f.write("select 1;\n")
        with open(os.path.join(self.root, "orca-flow.json"), "w") as f:
            json.dump({"base_branch": "main", "worker": {"migrations_dir": "migrations"}}, f)
        git = ["git", "-C", self.root, "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "init"], check=True)
        self.queue_dir = os.path.join(self.root, ".git", "orca-flow", "queue")
        os.makedirs(self.queue_dir)
        self.queue_wt = os.path.join(self.tmp, "merge-queue")
        os.makedirs(self.queue_wt)
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        for name, body in (("gh", FAKE_GH), ("orca", FAKE_ORCA)):
            with open(os.path.join(bin_dir, name), "w") as f:
                f.write(body)
            os.chmod(os.path.join(bin_dir, name), 0o755)
        self.state = os.path.join(self.tmp, "fake-state.json")
        self.log = os.path.join(self.tmp, "fake.log")
        self.prs = {}
        self.set_fake()
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("ORCA_FLOW_CONFIG", "ORCA_FLOW_DRY_RUN", "ORCA_TERMINAL_HANDLE", "ORCA_FLOW_SESSION")}
        self.env.update(ORCA_FLOW_REPO=self.root, ORCA_CLI_COMMAND=os.path.join(bin_dir, "orca"),
                        PATH=bin_dir + os.pathsep + os.environ.get("PATH", ""),
                        FAKE_STATE=self.state, FAKE_LOG=self.log)

    # --- helpers ---

    def set_fake(self, terminals=("term_q",), **extra):
        with open(self.state, "w") as f:
            json.dump({"prs": self.prs, "terminals": list(terminals), **extra}, f)

    def add_pr(self, n, head="a" * 40, files=(), state="OPEN"):
        self.prs[str(n)] = {"number": n, "title": f"PR {n}", "headRefName": f"branch-{n}", "headRefOid": head,
                            "state": state, "isDraft": False, "url": f"https://x/{n}",
                            "files": [{"path": p} for p in files]}
        with open(self.state) as f:
            st = json.load(f)
        st["prs"] = self.prs
        with open(self.state, "w") as f:
            json.dump(st, f)

    def register(self, terminal="term_q", session=None, cwd=None):
        act = {"session": session, "terminal": terminal, "cwd": cwd or self.queue_wt,
               "started_at": "2026-10-01T00:00:00Z", "batches": 0}
        if terminal is None:
            del act["terminal"]  # old state.json files have no terminal field at all
        self.write_state({"active": act, "retired": []})
        return act

    def write_state(self, st):
        with open(os.path.join(self.queue_dir, "state.json"), "w") as f:
            json.dump(st, f)

    def queue_state(self):
        with open(os.path.join(self.queue_dir, "state.json")) as f:
            return json.load(f)

    def handover_file(self, n, status, at, report_to="mgr-a", migration=(), session=None):
        h = {"pr": n, "title": f"PR {n}", "url": "u", "branch": f"branch-{n}", "head": "b" * 40,
             "migration": list(migration), "report_to": report_to, "sent_at": at, "status": status,
             "last_by": {"session": session, "terminal": None, "cwd": "/x"}, "history": []}
        if status == "done":
            h["done_at"] = at
        if status == "returned":
            h["returned_at"] = at
        with open(os.path.join(self.queue_dir, f"{n}.json"), "w") as f:
            json.dump(h, f)
        return h

    def read_file(self, n):
        with open(os.path.join(self.queue_dir, f"{n}.json")) as f:
            return json.load(f)

    def run_cmd(self, *args, env=None):
        r = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True,
                           env={**self.env, **(env or {})}, cwd=self.root)
        return r.returncode, r.stdout + r.stderr

    def calls(self, *prefix):
        try:
            with open(self.log) as f:
                rows = [json.loads(l) for l in f]
        except OSError:
            return []
        return [c for c in rows if c[:len(prefix)] == list(prefix)]

    # --- send: notify ---

    def test_send_notifies_the_registered_queue_without_notify(self):
        self.register()
        self.add_pr(5)
        code, out = self.run_cmd("send", "5", "--report-to", "mgr-a")
        self.assertEqual(code, 0, out)
        sends = self.calls("orca", "terminal", "send")
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][sends[0].index("--terminal") + 1], "term_q")
        self.assertIn("notified the queue terminal term_q", out)
        self.assertEqual(self.read_file(5)["status"], "pending")

    def test_send_twice_with_the_same_head_notifies_once(self):
        self.register()
        self.add_pr(5)
        self.run_cmd("send", "5", "--report-to", "mgr-a")
        code, out = self.run_cmd("send", "5", "--report-to", "mgr-a")
        self.assertEqual(code, 0, out)
        self.assertIn("nothing to do", out)
        self.assertEqual(len(self.calls("orca", "terminal", "send")), 1)

    def test_no_notify_and_explicit_notify(self):
        self.register()
        self.add_pr(5)
        code, out = self.run_cmd("send", "5", "--no-notify")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls("orca"), [])
        self.add_pr(5, head="c" * 40)
        code, out = self.run_cmd("send", "5", "--notify", "term_other")
        self.assertEqual(code, 0, out)
        sends = self.calls("orca", "terminal", "send")
        self.assertEqual([s[s.index("--terminal") + 1] for s in sends], ["term_other"])

    def test_gone_queue_terminal_is_not_notified_and_points_at_spawn_queue(self):
        self.register(terminal="term_dead")
        self.add_pr(5)
        code, out = self.run_cmd("send", "5")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls("orca", "terminal", "send"), [])
        self.assertIn("term_dead", out)
        self.assertIn("no longer exists", out)
        self.assertIn("spawn_queue.py", out)
        self.assertEqual(self.read_file(5)["status"], "pending")

    def test_orca_down_still_writes_and_tries_to_notify(self):
        self.register()
        self.add_pr(5)
        self.set_fake(list_fails=True)
        self.add_pr(5)
        code, out = self.run_cmd("send", "5")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(self.queue_dir, "5.json")))
        self.assertEqual(len(self.calls("orca", "terminal", "send")), 1)

    def test_no_state_file_and_no_active_queue(self):
        self.add_pr(5)
        code, out = self.run_cmd("send", "5")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls("orca"), [])
        self.assertIn("no queue session is registered", out)
        self.assertIn("spawn_queue.py", out)
        self.write_state({"active": None, "retired": [{"terminal": "term_q"}]})
        self.add_pr(5, head="c" * 40)
        code, out = self.run_cmd("send", "5")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls("orca"), [])

    # --- send: migration numbers ---

    def test_migration_number_clash_with_base_and_pending_handover(self):
        self.handover_file(7, "pending", ago(0), migration=["migrations/064_orders.sql"])
        self.handover_file(8, "returned", ago(0), migration=["migrations/065_x.sql"])
        self.add_pr(5, files=["migrations/063_accounts.sql", "migrations/064_items.sql", "migrations/065_y.sql",
                              "migrations/063_users.sql", "src/app.py"])
        code, out = self.run_cmd("send", "5", "--no-notify")
        self.assertEqual(code, 0, out)  # a warning, never a refusal
        self.assertIn("! migration number 063 (migrations/063_accounts.sql) also used by main:migrations/063_users.sql", out)
        self.assertIn("! migration number 064 (migrations/064_items.sql) also used by PR #7 (pending)", out)
        # A returned handover isn't waiting to merge; a modified old migration isn't new.
        self.assertNotIn("065", "\n".join(l for l in out.splitlines() if l.startswith("!")))
        self.assertNotIn("(migrations/063_users.sql (modified))", out)
        self.assertEqual(self.read_file(5)["migration"],
                         ["migrations/063_accounts.sql", "migrations/063_users.sql (modified)",
                          "migrations/064_items.sql", "migrations/065_y.sql"])

    def test_no_clash_warning_for_a_fresh_number(self):
        self.add_pr(5, files=["migrations/070_new.sql"])
        code, out = self.run_cmd("send", "5", "--no-notify")
        self.assertEqual(code, 0, out)
        self.assertNotIn("! migration number", out)

    # --- retarget ---

    def test_retarget_one_and_all_from(self):
        self.handover_file(1, "pending", ago(0), report_to="old")
        self.handover_file(2, "taken", ago(0), report_to="old")
        self.handover_file(3, "done", ago(0), report_to="old")
        self.handover_file(4, "pending", ago(0), report_to="someone")
        code, out = self.run_cmd("retarget", "1", "--report-to", "new")
        self.assertEqual(code, 0, out)
        h = self.read_file(1)
        self.assertEqual(h["report_to"], "new")
        self.assertEqual(h["history"][-1]["report_to"], "old")
        self.assertEqual(h["status"], "pending")
        code, out = self.run_cmd("retarget", "--all-from", "old", "--report-to", "new")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read_file(2)["report_to"], "new")
        self.assertEqual(self.read_file(2)["status"], "taken")
        self.assertEqual(self.read_file(3)["report_to"], "old")
        self.assertEqual(self.read_file(4)["report_to"], "someone")
        self.assertEqual(len(self.read_file(1)["history"]), 1)  # #1 already pointed at new
        code, out = self.run_cmd("retarget", "3", "--report-to", "new")
        self.assertEqual(code, 1)
        self.assertIn("done", out)

    def test_resent_returned_pr_keeps_history(self):
        self.register()
        self.add_pr(5)
        self.run_cmd("send", "5", "--no-notify")
        self.run_cmd("back", "5", "--reason", "head moved")
        self.add_pr(5, head="c" * 40)
        code, out = self.run_cmd("send", "5", "--no-notify")
        self.assertEqual(code, 0, out)
        h = self.read_file(5)
        self.assertEqual(h["status"], "pending")
        self.assertEqual(h["history"][-1]["status"], "returned")

    # --- identity ---

    def test_terminal_is_the_identity_when_no_session_is_known(self):
        self.add_pr(5)
        self.run_cmd("send", "5", "--no-notify")
        code, out = self.run_cmd("take", "5", env={"ORCA_TERMINAL_HANDLE": "term_q"})
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read_file(5)["last_by"]["terminal"], "term_q")
        self.assertIsNone(self.read_file(5)["last_by"]["session"])
        code, out = self.run_cmd("list")
        self.assertIn("by term_q", out)

    def test_queue_start_records_the_spawned_terminal(self):
        self.write_state({"active": None, "retired": [], "spawned": {"terminal": "term_new", "at": ago(0)}})
        code, out = self.run_cmd("queue", "start")
        self.assertEqual(code, 0, out)
        st = self.queue_state()
        self.assertEqual(st["active"]["terminal"], "term_new")
        self.assertNotIn("spawned", st)
        code, out = self.run_cmd("list")
        self.assertIn("queue: term_new since", out)
        code, out = self.run_cmd("queue", "start", "--force", "--terminal", "term_x", env={"ORCA_TERMINAL_HANDLE": "term_env"})
        self.assertEqual(self.queue_state()["active"]["terminal"], "term_x")
        code, out = self.run_cmd("queue", "start", "--force", env={"ORCA_TERMINAL_HANDLE": "term_env"})
        self.assertEqual(self.queue_state()["active"]["terminal"], "term_env")

    # --- queue show ---

    def show(self):
        code, out = self.run_cmd("queue", "show")
        self.assertEqual(code, 0, out)
        return json.loads(out)

    def test_queue_show_flags_stale_registrations(self):
        self.register()
        res = self.show()["active"]
        self.assertFalse(res["stale"])
        self.assertEqual(res["identity"], "term_q")
        self.register(terminal="term_dead")
        res = self.show()
        self.assertTrue(res["active"]["stale"])
        self.assertIn("terminal term_dead no longer exists in Orca", res["active"]["why"])
        self.assertIn("spawn_queue.py", res["hint"])
        self.register(cwd=os.path.join(self.tmp, "removed-worktree"))
        res = self.show()["active"]
        self.assertTrue(res["stale"])
        self.assertIn("no longer exists", res["why"][0])
        # The file itself is not rewritten by show.
        self.assertNotIn("stale", self.queue_state()["active"])

    def test_queue_show_with_old_state_and_without_state(self):
        self.register(terminal=None, session="queue-old")
        res = self.show()["active"]
        self.assertFalse(res["stale"])
        self.assertEqual(res["identity"], "queue-old")
        self.assertIn("unknown", res["terminal"])
        os.remove(os.path.join(self.queue_dir, "state.json"))
        self.assertEqual(self.show(), {"active": None, "retired": []})

    # --- status (parsed by managers' Monitor loops) ---

    def test_status_first_word_unchanged(self):
        self.handover_file(1, "pending", ago(0))
        self.handover_file(2, "returned", ago(0))
        self.assertEqual(self.run_cmd("status", "1")[1].split()[0], "pending")
        self.assertEqual(self.run_cmd("status", "2")[1].split()[0], "returned")
        self.assertEqual(self.run_cmd("status", "9")[1].split()[0], "unhanded")

    # --- prune ---

    def test_prune_dry_run_apply_and_repeat(self):
        self.handover_file(1, "done", ago(30))                 # old and done: archived
        self.handover_file(2, "done", ago(2))                  # recent: kept
        self.handover_file(3, "pending", ago(60))              # never touched
        self.handover_file(4, "taken", ago(60))                # never touched
        self.handover_file(5, "returned", ago(30))             # PR closed since: archived
        self.handover_file(6, "returned", ago(30))             # PR still open: kept
        self.add_pr(5, state="CLOSED")
        self.add_pr(6, state="OPEN")
        log = os.path.join(self.queue_dir, "log.jsonl")
        with open(log, "w") as f:
            f.write(json.dumps({"at": ago(40), "pr": 1, "status": "pending", "by": {"session": None}}) + "\n")
            f.write(json.dumps({"at": ago(1), "pr": 2, "status": "done"}) + "\n")
            f.write("not json\n")
        before = sorted(os.listdir(self.queue_dir))
        code, out = self.run_cmd("prune")
        self.assertEqual(code, 0, out)
        self.assertIn("would archive 2 handover file(s) and 1 log line(s) older than 14d", out)
        self.assertIn("kept #6: returned, PR is OPEN", out)
        self.assertEqual(sorted(os.listdir(self.queue_dir)), before)

        code, out = self.run_cmd("prune", "--older-than", "14d", "--apply")
        self.assertEqual(code, 0, out)
        left = sorted(f for f in os.listdir(self.queue_dir) if f.endswith(".json"))
        self.assertEqual(left, ["2.json", "3.json", "4.json", "6.json"])
        with open(log) as f:
            self.assertEqual(len(f.read().splitlines()), 2)  # the recent line and the unreadable one
        archived = []
        for f in os.listdir(self.queue_dir):
            if f.startswith("archive-"):
                with open(os.path.join(self.queue_dir, f)) as fh:
                    archived += [json.loads(l) for l in fh]
        self.assertEqual(sorted(r["data"]["pr"] for r in archived if r["kind"] == "handover"), [1, 5])
        self.assertEqual([r["data"]["pr"] for r in archived if r["kind"] == "log"], [1])

        code, out = self.run_cmd("prune", "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("archived 0 handover file(s) and 0 log line(s)", out)

    def test_list_reads_old_records_with_null_session(self):
        self.handover_file(1, "pending", ago(1))
        self.handover_file(2, "taken", ago(1))
        code, out = self.run_cmd("list")
        self.assertEqual(code, 0, out)
        self.assertIn("#1", out)
        self.assertIn("taken     #2", out)
        self.assertIn("by ?", out)


if __name__ == "__main__":
    unittest.main()
