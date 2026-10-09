"""worktrees.py wait / tail / tell and the context-window warning, with every orca call faked."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import worktrees  # noqa: E402


def wt(name, status="in-progress", comment=""):
    return {"id": f"repo1::/w/{name}", "path": f"/w/{name}", "displayName": name,
            "workspaceStatus": status, "comment": comment}


class FakeOrca:
    """Answers worktrees.run() for orca commands. polls: one (worktrees, agents_by_name) per
    poll of wait; the last one repeats."""

    def __init__(self, polls=None, terminals=(), tail=("line 1", "line 2"), send=None):
        self.polls = list(polls or [([], {})])
        self.terminals = list(terminals)
        self.tail = list(tail)
        self.send = send if send is not None else {"ok": True, "result": {"accepted": True}}
        self.calls = []
        self.n = 0

    def current(self):
        return self.polls[min(self.n, len(self.polls) - 1)]

    def __call__(self, cmd, cwd=None):
        self.calls.append(cmd)
        verb = cmd[1:3]
        ok = lambda res: (0, json.dumps({"ok": True, "result": res}), "")  # noqa: E731
        if verb == ["worktree", "list"]:
            return ok({"worktrees": self.current()[0]})
        if verb == ["worktree", "ps"]:
            wts, agents = self.current()
            self.n += 1
            return ok({"worktrees": [{"worktreeId": w["id"], "agents": agents.get(w["displayName"], [])} for w in wts]})
        if verb == ["terminal", "list"]:
            return ok({"terminals": self.terminals})
        if verb == ["terminal", "read"]:
            return ok({"terminal": {"handle": cmd[cmd.index("--terminal") + 1], "tail": self.tail}})
        if verb == ["terminal", "send"]:
            return 0, json.dumps(self.send), ""
        return 1, "", "unexpected"

    def sends(self):
        return [c for c in self.calls if c[1:3] == ["terminal", "send"]]


WORKING = [{"paneKey": "tab1:leaf1", "state": "working"}]
IDLE = [{"paneKey": "tab1:leaf1", "state": "idle"}]


def run_cli(fake, argv, common="/nowhere"):
    out = io.StringIO()
    code = 0
    with mock.patch.object(worktrees, "run", side_effect=fake), \
            mock.patch.object(worktrees, "repo_context", return_value=("/w/main", "repo1")), \
            mock.patch.object(worktrees.cfgmod, "common_dir", return_value=common), \
            mock.patch.object(worktrees.time, "sleep"), \
            mock.patch.object(sys, "argv", ["worktrees.py", *argv]), redirect_stdout(out):
        try:
            worktrees.main()
        except SystemExit as e:
            code = e.code
    return code, out.getvalue()


class WaitTest(unittest.TestCase):
    def test_reaches_in_review_after_changes(self):
        fake = FakeOrca([([wt("w1")], {"w1": WORKING}),
                         ([wt("w1")], {"w1": WORKING}),
                         ([wt("w1", "in-review", "PR #5: done")], {"w1": IDLE})])
        code, out = run_cli(fake, ["wait", "w1", "--until", "in-review", "--interval", "1"])
        self.assertEqual(code, 0)
        # One line per change, not per poll.
        self.assertEqual(out.splitlines(), ["w1: in-progress (busy)", "w1: in-review (idle)", "reached: in-review"])

    def test_already_there_returns_at_once(self):
        fake = FakeOrca([([wt("w1", comment="BLOCKED: no access")], {"w1": IDLE})])
        code, out = run_cli(fake, ["wait", "w1", "--until", "done"])
        self.assertEqual(code, 0)
        self.assertIn("w1: blocked (idle)", out)
        self.assertEqual(fake.n, 1)

    def test_every_worker_must_reach_unless_any(self):
        polls = [([wt("w1", "in-review"), wt("w2")], {"w1": IDLE, "w2": WORKING}),
                 ([wt("w1", "in-review"), wt("w2", "in-review")], {"w1": IDLE, "w2": IDLE})]
        code, out = run_cli(FakeOrca(polls), ["wait", "w1", "w2", "--until", "done"])
        self.assertEqual(code, 0)
        self.assertIn("w2: in-review (idle)", out)
        fake = FakeOrca(polls)
        code, out = run_cli(fake, ["wait", "w1", "w2", "--until", "any"])
        self.assertEqual((code, fake.n), (0, 1))

    def test_idle_and_exited(self):
        code, _ = run_cli(FakeOrca([([wt("w1")], {"w1": IDLE})]), ["wait", "w1", "--until", "idle"])
        self.assertEqual(code, 0)
        code, out = run_cli(FakeOrca([([wt("w1")], {})]), ["wait", "w1", "--until", "exited"])
        self.assertEqual(code, 0)
        self.assertIn("w1: in-progress (exited)", out)

    def test_timeout(self):
        fake = FakeOrca([([wt("w1")], {"w1": WORKING})])
        with mock.patch.object(worktrees.time, "time", side_effect=iter(range(1000, 2000, 3))):
            code, out = run_cli(fake, ["wait", "w1", "--until", "done", "--timeout", "10", "--interval", "5"])
        self.assertEqual(code, 1)
        self.assertIn("timeout after 10s", out)

    def test_missing_at_start_and_while_waiting(self):
        code, out = run_cli(FakeOrca([([wt("w1")], {})]), ["wait", "nope", "--until", "done"])
        self.assertEqual((code, out.strip()), (2, "nope: missing"))
        fake = FakeOrca([([wt("w1")], {"w1": WORKING}), ([], {})])
        code, out = run_cli(fake, ["wait", "w1", "--until", "done"])
        self.assertEqual(code, 2)
        self.assertTrue(out.strip().endswith("w1: missing"))

    def test_failed_poll_retries_instead_of_exiting(self):
        fake = FakeOrca([([wt("w1", "in-review")], {"w1": IDLE})])
        real = fake.__call__
        fails = iter([True])

        def flaky(cmd, cwd=None):
            if cmd[1:3] == ["worktree", "ps"] and next(fails, False):
                return 1, "", "boom"
            return real(cmd, cwd)
        code, out = run_cli(flaky, ["wait", "w1", "--until", "done"])
        self.assertEqual(code, 0)
        self.assertIn("! orca call failed; retrying", out)


TERMS = [
    {"handle": "term_shell", "worktreeId": "repo1::/w/w1", "tabId": "tab2", "leafId": "leaf2", "lastOutputAt": 99},
    {"handle": "term_agent", "worktreeId": "repo1::/w/w1", "tabId": "tab1", "leafId": "leaf1", "lastOutputAt": 5},
    {"handle": "term_other", "worktreeId": "repo1::/w/w2", "tabId": "tab3", "leafId": "leaf3", "lastOutputAt": 100},
]


class TailTest(unittest.TestCase):
    def test_reads_the_agent_pane_not_the_busier_shell(self):
        fake = FakeOrca([([wt("w1"), wt("w2")], {"w1": IDLE})], terminals=TERMS)
        code, out = run_cli(fake, ["tail", "w1", "--lines", "7"])
        self.assertEqual(code, 0)
        read = [c for c in fake.calls if c[1:3] == ["terminal", "read"]][0]
        self.assertEqual(read[read.index("--terminal") + 1], "term_agent")
        self.assertEqual(read[read.index("--limit") + 1], "7")
        self.assertIn("line 1\nline 2", out)

    def test_no_agent_falls_back_to_newest_terminal(self):
        fake = FakeOrca([([wt("w1")], {})], terminals=TERMS)
        with mock.patch.object(worktrees, "run", side_effect=fake):
            self.assertEqual(worktrees.worker_terminal(wt("w1")), "term_shell")
            self.assertIsNone(worktrees.worker_terminal(wt("w9")))

    def test_old_tail_shape_still_read(self):
        with mock.patch.object(worktrees, "run", return_value=(0, json.dumps({"ok": True, "result": {"tail": ["a", "b"]}}), "")):
            self.assertEqual(worktrees.terminal_tail("t"), "a\nb")

    def test_missing_worktree(self):
        code, out = run_cli(FakeOrca([([], {})]), ["tail", "w1"])
        self.assertEqual((code, out.strip()), (2, "w1: missing"))


class TellTest(unittest.TestCase):
    def setUp(self):
        self.common = tempfile.mkdtemp()
        self.brief = os.path.join(self.common, "orca-flow", "briefs", "w1")

    def fake(self, **kw):
        return FakeOrca([([wt("w1")], {"w1": IDLE})], terminals=TERMS, **kw)

    def sent_text(self, fake):
        (cmd,) = fake.sends()
        self.assertEqual(cmd[cmd.index("--terminal") + 1], "term_agent")
        self.assertIn("--enter", cmd)
        self.assertEqual(cmd[cmd.index("--wait-submit") + 1], "15")
        return cmd[cmd.index("--text") + 1]

    def test_short_text_sent_as_one_line(self):
        fake = self.fake()
        code, _ = run_cli(fake, ["tell", "w1", "--text", "Rename foo to bar, then push."], self.common)
        self.assertEqual(code, 0)
        self.assertEqual(self.sent_text(fake), "Rename foo to bar, then push.")
        self.assertFalse(os.path.exists(self.brief))

    def test_long_or_multiline_goes_through_numbered_files(self):
        for i, text in enumerate(["1. fix a\n2. fix b", "x" * 301], start=1):
            fake = self.fake()
            code, _ = run_cli(fake, ["tell", "w1", "--text", text], self.common)
            self.assertEqual(code, 0)
            path = os.path.join(self.brief, f"feedback-{i}.md")
            self.assertEqual(self.sent_text(fake), f"Manager feedback: read {path} and act on it.")
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), text + "\n")

    def test_file_argument(self):
        src = os.path.join(self.common, "review.md")
        with open(src, "w", encoding="utf-8") as f:
            f.write("## Review\n- one\n- two\n")
        fake = self.fake()
        code, _ = run_cli(fake, ["tell", "w1", "--file", src], self.common)
        self.assertEqual(code, 0)
        self.assertIn("feedback-1.md", self.sent_text(fake))

    def test_dry_run_writes_and_sends_nothing(self):
        fake = self.fake()
        code, out = run_cli(fake, ["tell", "w1", "--text", "a\nb", "--dry-run"], self.common)
        self.assertEqual(code, 0)
        self.assertEqual(fake.sends(), [])
        self.assertFalse(os.path.exists(self.brief))
        self.assertIn("DRY-RUN", out)

    def test_failed_send_is_not_retried(self):
        fake = self.fake(send={"ok": False, "error": {"code": "terminal_not_writable"}})
        code, out = run_cli(fake, ["tell", "w1", "--text", "hello"], self.common)
        self.assertEqual(code, 1)
        self.assertEqual(len(fake.sends()), 1)
        self.assertIn("NOT re-sent", out)
        self.assertIn("worktrees.py tail w1", out)


class WindowWarningTest(unittest.TestCase):
    def test_only_when_a_session_is_over_the_window(self):
        self.assertIsNone(worktrees.window_warning([150000, 199999], 200000))
        self.assertIsNone(worktrees.window_warning([], 200000))
        msg = worktrees.window_warning([150000, 260000], 200000)
        self.assertIn("1000000", msg)
        self.assertIn("260k", msg)
        self.assertIn("1500000", worktrees.window_warning([1500000], 1000000))

    def test_context_prints_it_once(self):
        rows = [{"name": n, "ctx": {"tokens": t, "pct": t / 200000, "turns": 1, "compactions": 0, "file": "f"}}
                for n, t in (("a", 250000), ("b", 300000))]
        out = io.StringIO()
        with mock.patch.object(worktrees, "CTX_WINDOW", 200000), redirect_stdout(out):
            worktrees.cmd_context(rows, None)
        self.assertEqual(out.getvalue().count("over worker.context_window"), 1)


if __name__ == "__main__":
    unittest.main()
