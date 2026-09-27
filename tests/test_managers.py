"""managers.py: who manages which worker, read from fixture folders (no Orca calls)."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import managers as mgrmod  # noqa: E402


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.common = self.tmp.name

    def write(self, rel, data):
        path = os.path.join(self.common, "orca-flow", rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        return path

    def manager(self, slug, terminal, session=None, status="running", **kw):
        self.write(f"managers/{slug}/manager.json",
                   {"slug": slug, "source": "asana", "source_id": "12", "terminal": terminal, "session": session,
                    "status": status, **kw})

    def worker(self, task, terminal=None, session=None):
        self.write(f"briefs/{task}/manager.json", {"session": session, "terminal": terminal, "cwd": "/x", "at": "t"})

    def by_name(self, managers):
        return {m["name"]: m for m in managers}


class AssignTest(Fixture):
    def test_grouping_by_terminal(self):
        self.manager("asana-12", "term_a", session="mgr-a")
        # The worker recorded a different session name than the record (names get reused or
        # renamed); the terminal handle still ties them together.
        self.worker("w1", terminal="term_a", session="something-else")
        managers, owner = mgrmod.assign(self.common, ["w1"])
        self.assertEqual(owner, {"w1": "asana-12"})
        self.assertEqual(self.by_name(managers)["asana-12"]["workers"], ["w1"])

    def test_terminal_wins_over_session(self):
        self.manager("a", "term_a", session="shared")
        self.manager("b", "term_b", session="shared")
        self.worker("w1", terminal="term_b", session="shared")
        _, owner = mgrmod.assign(self.common, ["w1"])
        self.assertEqual(owner["w1"], "b")

    def test_fallback_by_session(self):
        self.manager("asana-12", "term_new", session="mgr-a")
        self.worker("w1", terminal="term_old", session="mgr-a")
        _, owner = mgrmod.assign(self.common, ["w1"])
        self.assertEqual(owner["w1"], "asana-12")

    def test_no_manager_bucket(self):
        self.manager("asana-12", "term_a")
        self.worker("w2", terminal=None, session=None)  # a record naming nobody
        _, owner = mgrmod.assign(self.common, ["w1", "w2"])
        self.assertEqual(owner, {"w1": None, "w2": None})

    def test_manager_with_no_workers_still_listed(self):
        self.manager("idle-one", "term_a")
        managers, owner = mgrmod.assign(self.common, [])
        self.assertEqual(owner, {})
        m = self.by_name(managers)["idle-one"]
        self.assertEqual((m["workers"], m["recorded"], m["status"], m["source"], m["source_id"]),
                         ([], True, "running", "asana", "12"))

    def test_interactive_manager_named_by_session_and_shared(self):
        self.worker("w1", terminal="term_x", session="orca-flow-c7")
        self.worker("w2", terminal="term_x", session="orca-flow-c7")
        managers, owner = mgrmod.assign(self.common, ["w1", "w2"])
        self.assertEqual(owner, {"w1": "orca-flow-c7", "w2": "orca-flow-c7"})
        self.assertEqual(len(managers), 1)
        self.assertEqual((managers[0]["recorded"], managers[0]["workers"]), (False, ["w1", "w2"]))

    def test_recorded_managers_before_interactive_ones(self):
        self.worker("w1", terminal="term_x", session="aaa")
        self.manager("zzz", "term_z")
        managers, _ = mgrmod.assign(self.common, ["w1"])
        self.assertEqual([m["name"] for m in managers], ["zzz", "aaa"])

    def test_no_orca_flow_dir(self):
        self.assertEqual(mgrmod.assign(self.common, ["w1"]), ([], {"w1": None}))
        self.assertEqual(mgrmod.assign(None, ["w1"]), ([], {"w1": None}))

    def test_slug_falls_back_to_folder_name(self):
        self.write("managers/from-folder/manager.json", {"terminal": "t"})
        self.assertEqual([m["slug"] for m in mgrmod.load_managers(self.common)], ["from-folder"])


class UnreadableTest(Fixture):
    def test_unreadable_records_are_skipped_with_a_note(self):
        self.manager("good", "term_g")
        self.write("managers/broken/manager.json", "{not json")
        self.write("managers/list/manager.json", "[1, 2]")
        os.makedirs(os.path.join(self.common, "orca-flow", "managers", "empty"))
        self.write("managers/stray.txt", "not a folder")
        self.write("briefs/w1/manager.json", "{nope")
        notes = []
        managers, owner = mgrmod.assign(self.common, ["w1"], notes=notes)
        self.assertEqual([m["name"] for m in managers], ["good"])
        self.assertEqual(owner, {"w1": None})
        joined = "\n".join(notes)
        for part in ("broken", "list", "empty", os.path.join("briefs", "w1")):
            self.assertIn(part, joined)
        self.assertNotIn("stray", joined)


class TerminalTest(Fixture):
    TERMS = mgrmod.terminal_index([
        {"handle": "term_live", "tabId": "tab1", "leafId": "leaf1", "orphaned": False},
        {"handle": "term_hidden", "tabId": "tab2", "leafId": "leaf2", "orphaned": True},
        {"handle": "term_nopane", "orphaned": False},
    ])

    def test_index_maps_handle_to_pane_key(self):
        self.assertEqual(self.TERMS["term_live"], {"orphaned": False, "pane": "tab1:leaf1"})
        self.assertIsNone(self.TERMS["term_nopane"]["pane"])

    def test_terminal_state(self):
        ts = mgrmod.terminal_state
        self.assertEqual([ts("term_live", self.TERMS), ts("term_hidden", self.TERMS), ts("term_x", self.TERMS)],
                         ["live", "hidden", "gone"])
        self.assertIsNone(ts("term_live", None))
        self.assertIsNone(ts(None, self.TERMS))

    def test_live_and_dead(self):
        self.manager("up", "term_live")
        self.manager("hid", "term_hidden")
        self.manager("died", "term_x", status="running")
        self.manager("finished", "term_x", status="done")
        self.manager("done-open", "term_live", status="done")
        m = self.by_name(mgrmod.assign(self.common, [], self.TERMS)[0])
        got = {k: (v["live"], v["terminal_state"], mgrmod.is_dead(v)) for k, v in m.items()}
        self.assertEqual(got, {"up": (True, "live", False), "hid": (True, "hidden", False),
                               "died": (False, "gone", True), "finished": (False, "gone", False),
                               "done-open": (False, "live", False)})

    def test_unknown_terminals_never_dead(self):
        self.manager("died", "term_x")
        m = mgrmod.assign(self.common, [], None)[0][0]
        self.assertEqual((m["live"], m["terminal_state"], mgrmod.is_dead(m)), (None, None, False))

    def test_interactive_manager_gone_is_not_dead(self):
        self.worker("w1", terminal="term_x", session="s")
        m = mgrmod.assign(self.common, ["w1"], self.TERMS)[0][0]
        self.assertFalse(mgrmod.is_dead(m))


class InventoryWordsTest(Fixture):
    def test_manager_word_and_queue_line(self):
        import worktrees
        self.manager("died", "term_x")
        self.worker("w1", terminal="term_y", session="s")
        self.write("queue/state.json", {"active": {"session": "mq", "terminal": "term_hidden", "batches": 3},
                                        "retired": []})
        terms = TerminalTest.TERMS
        managers, _ = mgrmod.assign(self.common, ["w1"], terms)
        self.assertEqual([worktrees.manager_word(m) for m in managers], ["dead", "gone"])
        self.assertIn("(no managers/ record)", worktrees.manager_header(managers[1]))
        line, info = worktrees.queue_line(self.common, terms, [])
        self.assertEqual(line, "merge queue: mq  batches 3  hidden  term_hidden")
        self.assertEqual(info["terminal_state"], "hidden")

    def test_no_queue_registered_and_nothing_created(self):
        import worktrees
        self.assertEqual(worktrees.queue_line(self.common, None, []), (None, None))
        # board.py reads every repo's queue: reading must not create the queue folder.
        self.assertFalse(os.path.exists(os.path.join(self.common, "orca-flow", "queue")))

    def test_unreadable_queue_state_is_a_note(self):
        import worktrees
        self.write("queue/state.json", "{bad")
        notes = []
        self.assertEqual(worktrees.queue_line(self.common, None, notes), (None, None))
        self.assertEqual(len(notes), 1)


if __name__ == "__main__":
    unittest.main()
