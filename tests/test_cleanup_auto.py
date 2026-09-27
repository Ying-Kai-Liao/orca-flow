"""worktrees.py cleanup --auto and managers.safe_to_close, on fixture folders (no Orca, gh or git calls)."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import board_rules  # noqa: E402
import managers as mgrmod  # noqa: E402
import worktrees  # noqa: E402

NOTES = "# Manager notes: asana-12\n\n- PR #40 merged and deployed.\n"
MERGED = {"number": 40, "state": "MERGED", "headRefName": "me/w1", "headRefOid": "abc"}
OPEN = {"number": 41, "state": "OPEN", "headRefName": "me/w2", "headRefOid": "def"}


def mgr(**kw):
    base = {"name": "asana-12", "slug": "asana-12", "status": "done", "terminal": "term_m", "recorded": True,
            "terminal_state": "live", "workers": ["w1"]}
    base.update(kw)
    return base


QUIET = {"busy": False, "attention": "idle", "reason": "Orca state is idle"}


def check(m=None, worker_prs=None, pane=QUIET, own="term_me", notes=NOTES, tail="Done. PR #40 deployed."):
    return mgrmod.safe_to_close(mgr() if m is None else m, {"w1": MERGED} if worker_prs is None else worker_prs,
                                pane, own, notes, tail)


class SafeToCloseTest(unittest.TestCase):
    """Each condition failing on its own, from a manager that is otherwise fully safe."""

    def test_fully_safe(self):
        ok, why = check()
        self.assertTrue(ok, why)
        self.assertTrue(check(mgr(status="handed-over"))[0])
        self.assertTrue(check(mgr(status="handed-over", workers=["w1", "w2"]),
                              {"w1": MERGED, "w2": {**OPEN, "state": "CLOSED"}})[0])

    def test_not_a_record(self):
        ok, why = check(mgr(recorded=False, slug=None))
        self.assertFalse(ok)
        self.assertIn("not a managers/ record", why)

    def test_status_not_finished(self):
        for status in ("running", "starting", None, "closed"):
            self.assertFalse(check(mgr(status=status))[0], status)

    def test_handed_over_with_open_pr(self):
        ok, why = check(mgr(status="handed-over", workers=["w1", "w2"]), {"w1": MERGED, "w2": OPEN})
        self.assertFalse(ok)
        self.assertIn("#41 is OPEN", why)

    def test_handed_over_with_worker_without_pr(self):
        self.assertFalse(check(mgr(status="handed-over"), {"w1": None})[0])

    def test_handed_over_with_pr_state_unknown(self):
        ok, why = mgrmod.safe_to_close(mgr(status="handed-over"), None, QUIET, "term_me", NOTES, "")
        self.assertFalse(ok)
        self.assertIn("unknown", why)

    def test_handed_over_with_no_workers(self):
        self.assertFalse(check(mgr(status="handed-over", workers=[]), {})[0])

    def test_done_does_not_look_at_prs(self):
        self.assertTrue(check(mgr(status="done"), {"w1": OPEN})[0])

    def test_hidden_terminal_is_never_closed(self):
        ok, why = check(mgr(terminal_state="hidden"))
        self.assertFalse(ok)
        self.assertIn("orphaned", why)

    def test_gone_or_unknown_terminal(self):
        self.assertFalse(check(mgr(terminal_state="gone"))[0])
        self.assertFalse(check(mgr(terminal_state=None))[0])

    def test_own_terminal(self):
        ok, why = check(own="term_m")
        self.assertFalse(ok)
        self.assertIn("own terminal", why)

    def test_no_agent_in_pane(self):
        self.assertFalse(check(pane=None)[0])

    def test_busy(self):
        self.assertFalse(check(pane={**QUIET, "busy": True})[0])

    def test_board_attention(self):
        for att in ("needs_human", "blocked", "unhanded_pr", "hidden", "stale", "working", "unknown", "handoff"):
            self.assertFalse(check(pane={**QUIET, "attention": att})[0], att)
        self.assertTrue(check(pane={**QUIET, "attention": "done"})[0])

    def test_notes_missing_or_header_only(self):
        self.assertFalse(check(notes=None)[0])
        ok, why = check(notes="# Manager notes: asana-12\n\n")
        self.assertFalse(ok)
        self.assertIn("only its header", why)

    def test_tail_unreadable(self):
        self.assertFalse(check(tail=None)[0])

    def test_tail_with_secret(self):
        for tail in ("export OPENAI_API_KEY=abc123", "GITHUB_TOKEN: ghx", "sk-abcdefghijklmnop",
                     "-----BEGIN OPENSSH PRIVATE KEY-----", "password=hunter2"):
            ok, why = check(tail=tail)
            self.assertFalse(ok, tail)
            self.assertEqual(why, "may show a secret: tell the user")

    def test_ordinary_output_is_not_a_secret(self):
        for tail in ("context tokens: 50k", "Keyboard shortcuts", "PR #40 merged; the key point is done."):
            self.assertFalse(mgrmod.looks_secret(tail), tail)


class ClosedManagerTest(unittest.TestCase):
    """A manager closed by cleanup --auto is finished, not dead."""

    def test_is_not_dead_or_live(self):
        m = mgr(status="closed", terminal_state="gone")
        self.assertFalse(mgrmod.is_dead(m))
        self.assertFalse(mgrmod.is_live(m, {}))
        self.assertTrue(mgrmod.is_dead(mgr(status="handed-over", terminal_state="gone")))

    def test_board_rules_do_not_flag_it(self):
        role = {"kind": "manager", "name": "asana-12", "status": "closed", "terminal": "term_m", "terminal_state": "gone"}
        row = board_rules.make_row({"isMainWorktree": True}, None, 0, role=role)
        self.assertNotEqual(board_rules.classify(row)[0], "manager_dead")
        self.assertNotEqual(board_rules.classify(dict(row, role={**role, "terminal_state": "hidden"}))[0], "hidden")

    def test_inventory_word(self):
        self.assertEqual(worktrees.manager_word(mgr(status="closed", terminal_state="gone")), "closed")

    def test_board_gives_it_no_row(self):
        import board
        role_targets = board.role_targets([mgr(status="closed", terminal_state="gone")], None, {})
        self.assertEqual(role_targets, [])


def wt_row(name, pr=None, **kw):
    base = {"name": name, "id": f"repo1::/w/{name}", "path": f"/w/{name}", "branch": f"me/{name}", "exists": True,
            "dirty": 0, "ahead": 0, "head": "abc", "on_base": False, "busy": False, "status": "done",
            "idle_hours": 1.0, "workspace_status": "in-review", "comment": "", "pr": pr, "pr_known": True,
            "waiting": [], "ctx": None}
    base.update(kw)
    return base


class CleanupAutoTest(unittest.TestCase):
    """cmd_cleanup_auto end to end, with Orca faked through run() and live_terminals()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.common = self.tmp.name
        self.calls = []

    def write(self, rel, data):
        path = os.path.join(self.common, "orca-flow", rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        return path

    def manager(self, slug, terminal, status="done", notes=NOTES):
        self.write(f"managers/{slug}/manager.json", {"slug": slug, "terminal": terminal, "status": status})
        if notes is not None:
            self.write(f"managers/{slug}/notes.md", notes)

    def record(self, slug):
        with open(os.path.join(self.common, "orca-flow", "managers", slug, "manager.json"), encoding="utf-8") as f:
            return json.load(f)

    def fake_run(self, tails):
        def run(cmd, cwd=None):
            self.calls.append(cmd)
            if cmd[1:3] == ["terminal", "read"]:
                handle = cmd[cmd.index("--terminal") + 1]
                return 0, json.dumps({"ok": True, "result": {"tail": tails.get(handle, ["Done."])}}), ""
            return 0, json.dumps({"ok": True, "result": {}}), ""
        return run

    def auto(self, rows, terminals, ps_agents, prs=(), dry=False, tails=None, own="term_me"):
        ps = [{"worktreeId": "repo1::/w/main", "path": "/w/main", "isMainWorktree": True, "comment": "",
               "agents": ps_agents}]
        out = io.StringIO()
        with mock.patch.object(worktrees, "run", side_effect=self.fake_run(tails or {})), \
                mock.patch.object(worktrees, "repo_context", return_value=("/w/main", "repo1")), \
                mock.patch.object(worktrees.cfgmod, "common_dir", return_value=self.common), \
                mock.patch.object(worktrees, "live_terminals", return_value=terminals), \
                mock.patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": own}), redirect_stdout(out):
            worktrees.cmd_cleanup_auto(rows, {"ps": ps, "prs": list(prs)}, 3, dry)
        return out.getvalue()

    def actions(self, verb):
        return [c for c in self.calls if c[1:3] == verb]

    def setup_one_safe_manager(self):
        self.manager("asana-12", "term_m")
        self.write("briefs/w1/manager.json", {"terminal": "term_m", "session": "s"})
        terminals = {"term_m": {"orphaned": False, "pane": "tabM:leafM"}}
        agents = [{"paneKey": "tabM:leafM", "state": "done", "lastAssistantMessage": "All deployed."}]
        return terminals, agents

    def test_removes_exactly_the_decide_candidates_and_closes_a_safe_manager(self):
        terminals, agents = self.setup_one_safe_manager()
        rows = [wt_row("w1", pr=MERGED), wt_row("w2", pr=OPEN), wt_row("w3", dirty=2, pr=MERGED),
                wt_row("w4", pr=None, idle_hours=10.0)]
        expected = sorted(r["name"] for r in rows if worktrees.decide(r, 3)[0])
        out = self.auto(rows, terminals, agents, prs=[MERGED, OPEN])
        removed = sorted(c[c.index("--worktree") + 1].split("/")[-1] for c in self.actions(["worktree", "rm"]))
        self.assertEqual(removed, expected)
        self.assertEqual(expected, ["w1", "w4"])
        self.assertIn("skip    w2: PR #41 still open", out)
        self.assertIn("skip    w3: 2 uncommitted change(s)", out)
        self.assertEqual([c[c.index("--terminal") + 1] for c in self.actions(["terminal", "close"])], ["term_m"])
        rec = self.record("asana-12")
        self.assertEqual((rec["status"], rec["closed_from"]), ("closed", "done"))
        self.assertIn("terminal_closed_at", rec)
        self.assertIn("closed  manager:asana-12", out)

    def test_dry_run_does_nothing(self):
        terminals, agents = self.setup_one_safe_manager()
        before = self.record("asana-12")
        out = self.auto([wt_row("w1", pr=MERGED)], terminals, agents, prs=[MERGED], dry=True)
        self.assertEqual(self.actions(["worktree", "rm"]), [])
        self.assertEqual(self.actions(["terminal", "close"]), [])
        self.assertEqual(self.record("asana-12"), before)
        self.assertIn("DRY-RUN would remove w1", out)
        self.assertIn("DRY-RUN would close manager:asana-12", out)

    def test_secret_in_tail_is_skipped_and_not_printed(self):
        terminals, agents = self.setup_one_safe_manager()
        out = self.auto([], terminals, agents, tails={"term_m": ["export API_KEY=supersecretvalue"]})
        self.assertEqual(self.actions(["terminal", "close"]), [])
        self.assertIn("may show a secret: tell the user", out)
        self.assertNotIn("supersecretvalue", out)
        self.assertEqual(self.record("asana-12")["status"], "done")

    def test_handed_over_worker_already_removed_uses_pr_by_branch(self):
        terminals, agents = self.setup_one_safe_manager()
        self.manager("asana-12", "term_m", status="handed-over")
        out = self.auto([], terminals, agents, prs=[MERGED])
        self.assertEqual(len(self.actions(["terminal", "close"])), 1, out)
        self.calls.clear()
        self.manager("asana-12", "term_m", status="handed-over")
        out = self.auto([], terminals, agents, prs=[{**MERGED, "state": "OPEN"}])
        self.assertEqual(self.actions(["terminal", "close"]), [])
        self.assertIn("is OPEN", out)

    def test_hidden_own_and_interactive_terminals_are_left_alone(self):
        self.manager("hidden-1", "term_h")
        self.manager("mine", "term_me")
        self.write("briefs/w9/manager.json", {"terminal": "term_i", "session": "interactive"})
        terminals = {"term_h": {"orphaned": True, "pane": "tabH:leafH"},
                     "term_me": {"orphaned": False, "pane": "tabMe:leafMe"},
                     "term_i": {"orphaned": False, "pane": "tabI:leafI"}}
        agents = [{"paneKey": p, "state": "done"} for p in ("tabH:leafH", "tabMe:leafMe", "tabI:leafI")]
        out = self.auto([], terminals, agents)
        self.assertEqual(self.actions(["terminal", "close"]), [])
        self.assertIn("skip    manager:hidden-1: terminal term_h is orphaned", out)
        self.assertIn("skip    manager:mine: it is this session's own terminal", out)
        self.assertIn("skip    manager:interactive: not a managers/ record", out)

    def test_already_closed_manager_is_quiet(self):
        self.manager("old", "term_old", status="closed")
        out = self.auto([], {}, [])
        self.assertNotIn("old", out)


class PrOfTaskTest(unittest.TestCase):
    def test_row_first_then_branch_suffix(self):
        self.assertEqual(worktrees.pr_of_task("w1", {"w1": wt_row("w1", pr=OPEN)}, [MERGED]), OPEN)
        self.assertEqual(worktrees.pr_of_task("w1", {}, [MERGED, {**MERGED, "number": 50}])["number"], 50)
        self.assertIsNone(worktrees.pr_of_task("w1", {}, [{**MERGED, "headRefName": "me/w10"}]))


class MarkClosedTest(unittest.TestCase):
    def test_keeps_the_rest_of_the_record(self):
        with tempfile.TemporaryDirectory() as common:
            d = os.path.join(common, "orca-flow", "managers", "a")
            os.makedirs(d)
            with open(os.path.join(d, "manager.json"), "w", encoding="utf-8") as f:
                json.dump({"slug": "a", "terminal": "t", "status": "handed-over", "source": "asana"}, f)
            mgrmod.mark_closed(common, "a", at="2026-09-27T10:00:00+00:00")
            [m] = mgrmod.load_managers(common)
            self.assertEqual((m["status"], m["closed_from"], m["terminal_closed_at"], m["source"]),
                             ("closed", "handed-over", "2026-09-27T10:00:00+00:00", "asana"))
            self.assertEqual(os.listdir(d), ["manager.json"])


if __name__ == "__main__":
    unittest.main()
