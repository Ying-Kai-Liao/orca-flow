"""Tests for board_rules.classify / make_row and board.py's sort order.

Run: python3 -m unittest discover -s tests

The fixtures are shaped like `orca worktree ps --json` output (same keys). The real sample
that the rules were written against is private and stays out of this public repo; if it is
present locally ($ORCA_FLOW_PS_SAMPLE, or the brief folder), one extra test runs every pane in
it through the rules.
"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import board_rules  # noqa: E402
from board_rules import classify, make_row  # noqa: E402

NOW = 1790186954552  # ms, same epoch as the sample
MIN = 60000


def wt(**kw):
    base = {"worktreeId": "repo1::/w/feature", "repoId": "repo1", "hostId": "local", "repo": "peace-guardian",
            "path": "/w/feature", "branch": "refs/heads/me/feature", "isMainWorktree": False,
            "workspaceStatus": "in-progress", "comment": "", "unread": False, "linkedPR": None, "agents": []}
    base.update(kw)
    return base


def agent(state="done", msg="", started_min=10, updated_min=None, **kw):
    base = {"paneKey": "tab:pane", "state": state, "agentType": "claude", "prompt": "do the thing",
            "lastAssistantMessage": msg, "toolName": "Bash", "stateStartedAt": NOW - started_min * MIN,
            "updatedAt": NOW - (started_min if updated_min is None else updated_min) * MIN}
    base.update(kw)
    return base


def row(w=None, a="default", pr=None, handover=None):
    return make_row(w or wt(), agent() if a == "default" else a, NOW, pr=pr, handover=handover)


OPEN_PR = {"number": 146, "state": "OPEN", "isDraft": False, "title": "docs"}


class ClassifyTest(unittest.TestCase):
    def assertAttention(self, r, expected):
        attention, reason = classify(r)
        self.assertEqual(attention, expected, reason)
        self.assertIn(attention, board_rules.ATTENTION)
        self.assertTrue(reason)

    # needs_human
    def test_orca_waiting(self):
        self.assertAttention(row(a=agent("waiting", "Working on it.")), "needs_human")

    def test_permission_prompt(self):
        self.assertAttention(row(a=agent("permission")), "needs_human")

    def test_question_at_end(self):
        self.assertAttention(row(a=agent(msg="Two can go.\n\nShould I remove these five worktrees?")), "needs_human")

    def test_question_behind_markup_and_fullwidth(self):
        self.assertAttention(row(a=agent(msg="**你原本指的是哪一張？**")), "needs_human")

    def test_decision_phrase_without_final_period(self):
        self.assertAttention(row(a=agent(msg="兩個方案都寫好了。\n\nA 還是 B,需要你拍板")), "needs_human")
        self.assertAttention(row(a=agent(msg="Both are ready.\n\nShould I merge A or B")), "needs_human")

    # review item 1: a last paragraph ending with a period is a report; negated phrases don't count
    def test_decision_phrase_in_paragraph_ending_with_period_is_done(self):
        self.assertAttention(row(a=agent(msg="這個需要你拍板。")), "done")
        self.assertAttention(row(a=agent(msg="Deployed #148. Per the 2026-09-23 拍板, no per-batch approval.")), "done")

    def test_negated_decision_phrase_is_done(self):
        self.assertAttention(row(a=agent(msg="#148 已部署到 demo 與 production,這次不需要你拍板。")), "done")
        self.assertAttention(row(a=agent(msg="#148 已部署,不需要你拍板")), "done")
        self.assertAttention(row(a=agent(msg="Deployed; no need for your call")), "done")

    def test_negation_only_cancels_its_own_phrase(self):
        msg = "不需要你拍板 the deploy, but please confirm the new key works"
        self.assertAttention(row(a=agent(msg=msg)), "needs_human")

    # review item 2: an unanswered question demotes to done after question_max_min
    def test_old_question_is_demoted(self):
        attention, reason = classify(row(a=agent(msg="Should I remove them?", started_min=300)))
        self.assertEqual(attention, "done")
        self.assertEqual(reason, "asked 5h ago, no answer; demoted")

    def test_question_at_threshold_is_not_demoted(self):
        self.assertAttention(row(a=agent(msg="Should I remove them?", started_min=240)), "needs_human")

    def test_question_max_min_is_configurable(self):
        r = row(a=agent(msg="Should I remove them?", started_min=61))
        self.assertEqual(classify(r, question_max_min=60)[0], "done")

    def test_orca_waiting_is_never_demoted(self):
        self.assertAttention(row(a=agent("waiting", "Should I?", started_min=5000)), "needs_human")

    # review item 3: code blocks and bracketed asides don't make a question
    def test_question_in_closing_brackets_is_done(self):
        self.assertAttention(row(a=agent(msg="Done. (Tests pass?)")), "done")
        self.assertAttention(row(a=agent(msg="已部署。（測試過了嗎？）")), "done")

    def test_question_in_fenced_code_is_done(self):
        msg = "Merged and deployed.\n\n```\n$ curl /health?verbose\nok?\n```"
        self.assertAttention(row(a=agent(msg=msg)), "done")

    def test_question_before_fenced_code_still_counts(self):
        msg = "Should I run this?\n\n```\nrm -rf build\n```"
        self.assertAttention(row(a=agent(msg=msg)), "needs_human")

    def test_decision_phrase_only_counts_in_last_paragraph(self):
        msg = "Earlier I asked which do you want.\n\nYou picked B, and it is live on production."
        self.assertAttention(row(a=agent(msg=msg)), "done")

    def test_question_long_message_beyond_200_chars(self):
        msg = "x" * 5000 + "\n\nWhich one do you want?"
        r = row(a=agent(msg=msg))
        self.assertEqual(len(r["last_message"]), 200)
        self.assertAttention(r, "needs_human")

    def test_waiting_beats_blocked(self):
        self.assertAttention(row(w=wt(comment="BLOCKED: no key"), a=agent("waiting")), "needs_human")

    # not needs_human: the merge queue's and workers' status reports
    def test_status_report_ending_with_period_is_done(self):
        msg = "#147 is on main at `e2b3b00`. Nothing is waiting in the queue and no PRs are open."
        self.assertAttention(row(a=agent(msg=msg)), "done")

    def test_confirmed_in_report_is_not_a_request(self):
        self.assertAttention(row(a=agent(msg="I confirmed the timer didn't start.")), "done")

    def test_working_agent_with_old_question_is_working(self):
        self.assertAttention(row(a=agent("working", "Should I remove them?", updated_min=1)), "working")

    # blocked / handoff
    def test_blocked(self):
        self.assertAttention(row(w=wt(comment="BLOCKED: brief contradicts the code")), "blocked")

    def test_blocked_beats_question(self):
        self.assertAttention(row(w=wt(comment="blocked:x"), a=agent(msg="Which do you want?")), "blocked")

    def test_handoff(self):
        self.assertAttention(row(w=wt(comment="HANDOFF: half done")), "handoff")

    # unhanded_pr
    def test_unhanded_pr(self):
        self.assertAttention(row(pr=OPEN_PR), "unhanded_pr")

    def test_unhanded_pr_lowercase_state_from_orca_link(self):
        self.assertAttention(row(pr={"number": 5, "state": "open", "isDraft": None}), "unhanded_pr")

    def test_handed_over_pr_is_not_unhanded(self):
        self.assertAttention(row(pr=OPEN_PR, handover="pending"), "done")

    # review item 4: a PR the queue sent back needs handing over again
    def test_returned_handover_is_unhanded(self):
        attention, reason = classify(row(pr=OPEN_PR, handover="returned"))
        self.assertEqual(attention, "unhanded_pr")
        self.assertIn("sent back", reason)

    def test_taken_and_done_handover_are_handed(self):
        for status in ("pending", "taken", "done"):
            self.assertEqual(classify(row(pr=OPEN_PR, handover=status))[0], "done", status)

    def test_unreadable_handover_file_counts_as_handed(self):
        self.assertAttention(row(pr=OPEN_PR, handover="?"), "done")

    def test_draft_pr_is_not_unhanded(self):
        self.assertAttention(row(pr=dict(OPEN_PR, isDraft=True)), "done")

    def test_merged_pr_is_not_unhanded(self):
        self.assertAttention(row(pr=dict(OPEN_PR, state="MERGED")), "done")

    def test_pr_with_working_agent_is_working(self):
        self.assertAttention(row(a=agent("working", updated_min=1), pr=OPEN_PR), "working")

    def test_pr_without_agent_pane(self):
        self.assertAttention(row(a=None, pr=OPEN_PR), "unhanded_pr")

    # a repo with merge_queue.enabled false has nobody to hand a PR to
    def test_no_queue_open_pr_is_done(self):
        r = make_row(wt(), agent(), NOW, pr=OPEN_PR, no_queue=True)
        attention, reason = classify(r)
        self.assertEqual(attention, "done")
        self.assertIn("no queue in this repo", reason)

    def test_no_queue_pr_without_agent_pane_is_done(self):
        self.assertAttention(make_row(wt(), None, NOW, pr=OPEN_PR, no_queue=True), "done")

    def test_no_queue_does_not_hide_questions_or_blocked(self):
        self.assertAttention(make_row(wt(), agent(msg="Which one?"), NOW, pr=OPEN_PR, no_queue=True), "needs_human")
        self.assertAttention(make_row(wt(comment="BLOCKED: x"), agent(), NOW, pr=OPEN_PR, no_queue=True), "blocked")

    def test_no_queue_working_agent_is_working(self):
        r = make_row(wt(), agent("working", updated_min=1), NOW, pr=OPEN_PR, no_queue=True)
        self.assertAttention(r, "working")

    def test_inventory_pr_row_no_queue(self):
        import worktrees
        u = {"number": 9, "handover": None}
        self.assertEqual(classify(worktrees.pr_row(u))[0], "unhanded_pr")
        self.assertEqual(classify(worktrees.pr_row(u, no_queue=True))[0], "done")

    # stale / working
    def test_stale(self):
        self.assertAttention(row(a=agent("working", started_min=90, updated_min=31)), "stale")

    def test_not_stale_at_threshold(self):
        self.assertAttention(row(a=agent("working", started_min=90, updated_min=30)), "working")

    def test_stale_min_is_configurable(self):
        r = row(a=agent("working", updated_min=11))
        self.assertEqual(classify(r, stale_min=10)[0], "stale")

    # done / idle / unknown
    def test_done(self):
        self.assertAttention(row(), "done")

    def test_empty_message_done(self):
        self.assertAttention(row(a=agent(msg=None)), "done")

    def test_idle_no_pane(self):
        r = row(a=None)
        self.assertAttention(r, "idle")
        self.assertEqual(classify(r)[1], "no agent pane")

    def test_idle_state(self):
        self.assertAttention(row(a=agent("idle")), "idle")

    def test_unknown_state(self):
        self.assertAttention(row(a=agent("exploded")), "unknown")

    def test_plain_dict_without_optional_keys(self):
        # jev-handoff may hand in rows it built itself; missing keys must not crash.
        self.assertEqual(classify({})[0], "idle")
        self.assertEqual(classify({"state": "working"})[0], "working")

    def test_classify_is_pure(self):
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")), \
                mock.patch("builtins.open", side_effect=AssertionError("no file IO")):
            classify(row(pr=OPEN_PR))
            classify(row(a=agent("waiting")))


class MakeRowTest(unittest.TestCase):
    def test_fields(self):
        r = row(a=agent("working", "hello", started_min=12, updated_min=2, workingMode="plan"), pr=OPEN_PR)
        self.assertEqual(r["repo"], "peace-guardian")
        self.assertEqual(r["worktree"], "feature")
        self.assertEqual(r["branch"], "me/feature")
        self.assertEqual(r["column"], "in-progress")
        self.assertEqual(r["state"], "working")
        self.assertEqual(r["working_mode"], "plan")
        self.assertEqual(r["minutes_in_state"], 12.0)
        self.assertEqual(r["minutes_since_update"], 2.0)
        self.assertEqual(r["last_message"], "hello")
        self.assertEqual(r["pr"]["number"], 146)
        json.dumps(r)  # plain values only

    def test_no_agent(self):
        r = row(a=None)
        self.assertIsNone(r["pane"])
        self.assertIsNone(r["minutes_in_state"])


class SortTest(unittest.TestCase):
    def test_repo_with_urgent_row_first_and_urgent_rows_first(self):
        import board
        rows = []
        for repo, att, name in [("a", "done", "a1"), ("b", "working", "b1"), ("b", "needs_human", "b2"),
                                ("a", "idle", "a2")]:
            rows.append({"repo": repo, "attention": att, "worktree": name, "minutes_in_state": 1})
        self.assertEqual([r["worktree"] for r in board.sort_rows(rows)], ["b2", "b1", "a1", "a2"])

    def test_questions_newest_first_others_oldest_first(self):
        import board
        rows = [{"repo": "a", "attention": att, "worktree": name, "minutes_in_state": m}
                for att, name, m in [("needs_human", "q_old", 200), ("needs_human", "q_new", 5),
                                     ("done", "d_new", 5), ("done", "d_old", 200)]]
        self.assertEqual([r["worktree"] for r in board.sort_rows(rows)], ["q_new", "q_old", "d_old", "d_new"])


class PaneLabelTest(unittest.TestCase):
    def test_short_pane(self):
        import board
        self.assertEqual(board.short_pane("7004aec6-b8f8:14bf1748-17e7"), "7004:14bf")
        self.assertEqual(board.short_pane(None), "-")

    def test_label_only_when_worktree_has_several_panes(self):
        import board
        one = wt(worktreeId="r::/w/one", path="/w/one", agents=[agent(paneKey="aaaa1111:bbbb2222")])
        two = wt(worktreeId="r::/w/two", path="/w/two",
                 agents=[agent(paneKey="cccc1111:dddd2222"), agent(paneKey="cccc1111:eeee3333")])
        ps = {"worktrees": [one, two], "truncated": False}
        fake = {("worktree", "ps", "--limit", "1000"): ps, ("repo", "list"): {"repos": []}}
        with mock.patch.object(board, "orca", side_effect=lambda *a: fake[a]):
            rows, _ = board.collect(30, use_gh=False)
        self.assertEqual(sorted(r["label"] for r in rows), ["one", "two@cccc:dddd", "two@cccc:eeee"])


class BoardNoQueueTest(unittest.TestCase):
    def test_repo_config_decides_per_repo(self):
        import board
        a = wt(worktreeId="a::/w/a", repoId="a", path="/w/a", agents=[agent()])
        b = wt(worktreeId="b::/w/b", repoId="b", path="/w/b", agents=[agent()],
               linkedPR={"number": 4, "state": "open", "title": "x"})
        a["linkedPR"] = {"number": 3, "state": "open", "title": "x"}
        fake = {("worktree", "ps", "--limit", "1000"): {"worktrees": [a, b]},
                ("repo", "list"): {"repos": [{"id": "a", "path": "/tmp"}, {"id": "b", "path": "/"}]}}
        with mock.patch.object(board, "orca", side_effect=lambda *x: fake[x]), \
                mock.patch.object(board, "git_common_dir", return_value=None), \
                mock.patch.object(board, "repo_queue_enabled", side_effect=lambda root: root != "/tmp"):
            rows, _ = board.collect(30, use_gh=False)
        got = {r["worktree"]: (r["attention"], r["no_queue"]) for r in rows}
        self.assertEqual(got, {"a": ("done", True), "b": ("unhanded_pr", False)})


class WatchDiffTest(unittest.TestCase):
    def test_only_changed_new_and_gone_rows(self):
        import board

        def r(name, att):
            return {"repo": "a", "worktree": name, "attention": att, "reason": "why"}
        prev = {1: r("same", "done"), 2: r("flip", "working"), 3: r("gone", "idle")}
        cur = {1: r("same", "done"), 2: r("flip", "needs_human"), 4: r("new", "working")}
        self.assertEqual(board.changes(prev, cur),
                         ["a/flip: working -> needs_human (why)", "a/new: new -> working (why)", "a/gone: idle -> gone"])
        self.assertEqual(board.changes(cur, cur), [])


def sample_path():
    env = os.environ.get("ORCA_FLOW_PS_SAMPLE")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run(["git", "-C", here, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                       capture_output=True, text=True)
    return os.path.join(r.stdout.strip(), "orca-flow", "briefs", "board", "ps-sample.json") if r.returncode == 0 else ""


@unittest.skipUnless(os.path.isfile(sample_path()), "private ps sample not present")
class RealSampleTest(unittest.TestCase):
    def test_every_pane_classifies(self):
        with open(sample_path(), encoding="utf-8") as f:
            data = json.load(f)
        self.assertTrue(data["ok"])
        seen = {}
        for w in data["result"]["worktrees"]:
            for a in w.get("agents") or [None]:
                r = make_row(w, a, NOW)
                attention, _ = classify(r)
                self.assertIn(attention, board_rules.ATTENTION)
                seen.setdefault(os.path.basename(w["path"]), set()).add(attention)
        # The queue's final status report ends with a period: it must not ask for a human.
        self.assertEqual(seen.get("merge-queue"), {"done"})


if __name__ == "__main__":
    unittest.main()
