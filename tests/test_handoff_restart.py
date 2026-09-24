"""Tests for the jev-handoff restart path: hook install per worktree, the --continue prompt and
digest, the handoff null reasons, the inventory hf column, and cleanup ignoring the hook file.

Run: python3 -m unittest tests/test_handoff_restart.py

jev-handoff is a fake shell script that logs its arguments and, for `run`, copies a fixture
working set into a temp state dir. Orca calls and the agent start are patched out.
"""
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, SCRIPTS)
import config as cfgmod  # noqa: E402
import spawn_worker  # noqa: E402
import transcript  # noqa: E402
import worktrees  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"
FIXTURE_MD = "# Handoff: x\n\n## Trajectory (the intent of each bunch, and decisions)\n\n1. [#0] build it\n\n## Working set\n\n### [#0] user\n\nbuild it\n"
FIXTURE_JSON = {"intents": [{"index": 0}, {"index": 5}], "items": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}

FAKE_BIN = """#!/bin/sh
echo "$@" >> "{log}"
state=""; tr=""; settings=""
while [ $# -gt 0 ]; do
  case "$1" in
    --state-dir) state="$2"; shift;;
    --transcript) tr="$2"; shift;;
    --settings) settings="$2"; shift;;
  esac
  shift
done
if [ -n "$settings" ]; then
  mkdir -p "$(dirname "$settings")"
  echo '{{"hooks": {{"Stop": []}}}}' > "$settings"
  echo "updated $settings"
fi
if [ -n "$tr" ]; then
  sid=$(basename "$tr" .jsonl)
  mkdir -p "$state/$sid"
  cp "{fixtures}/handoff.md" "{fixtures}/handoff.json" "$state/$sid/"
  echo "updated: K=3 items -> $state/$sid/handoff.md"
fi
"""


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.state = os.path.join(self.tmp, "state")
        self.projects = os.path.join(self.tmp, "projects")
        self.log = os.path.join(self.tmp, "bin.log")
        fixtures = os.path.join(self.tmp, "fixtures")
        os.makedirs(fixtures)
        with open(os.path.join(fixtures, "handoff.md"), "w") as f:
            f.write(FIXTURE_MD)
        with open(os.path.join(fixtures, "handoff.json"), "w") as f:
            json.dump(FIXTURE_JSON, f)
        self.bin = os.path.join(self.tmp, "jev-handoff")
        with open(self.bin, "w") as f:
            f.write(FAKE_BIN.format(log=self.log, fixtures=fixtures))
        os.chmod(self.bin, os.stat(self.bin).st_mode | stat.S_IEXEC)
        self.wt = os.path.join(self.tmp, "wt")
        os.makedirs(self.wt)
        git(self.wt, "init", "-q", "-b", "main")
        git(self.wt, "commit", "-q", "--allow-empty", "-m", "init")

    def cfg(self, **handoff):
        h = {"bin": self.bin, "state_dir": self.state}
        h.update(handoff)
        cfg = cfgmod.merge(cfgmod.DEFAULTS, {"worker": {"transcripts_dir": self.projects}, "handoff": h})
        cfg.update(repo_root=self.tmp, config_file=None, project="p")
        return cfg

    def write_transcript(self):
        d = transcript.project_dir(self.wt, self.projects)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, SID + ".jsonl")
        lines = [{"type": "user", "message": {"content": "build it"}},
                 {"type": "assistant", "message": {"content": [{"type": "text", "text": "PREVIOUS LAST WORDS"}]}}]
        with open(p, "w") as f:
            f.write("\n".join(json.dumps(l) for l in lines) + "\n")
        return p

    def calls(self):
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except OSError:
            return []


class HookTest(Base):
    def test_hook_installed_into_worktree_local_settings(self):
        note = spawn_worker.handoff_hook(cfgmod.handoff_settings(self.cfg()), self.wt)
        settings = os.path.join(self.wt, ".claude", "settings.local.json")
        self.assertTrue(os.path.isfile(settings))
        self.assertIn("updated", note)
        self.assertEqual(self.calls(), [f"install-hook --settings {settings}"])

    def test_hook_dry_run_only_names_the_command(self):
        note = spawn_worker.handoff_hook(cfgmod.handoff_settings(self.cfg()), self.wt, dry=True)
        self.assertTrue(note.startswith("would run "))
        self.assertEqual(self.calls(), [])

    def test_hook_off(self):
        self.assertIsNone(spawn_worker.handoff_hook(cfgmod.handoff_settings(self.cfg(bin=None)), self.wt))
        self.assertIsNone(spawn_worker.handoff_hook(cfgmod.handoff_settings(self.cfg(hook=False)), self.wt))

    def test_hook_bin_missing_is_a_note(self):
        note = spawn_worker.handoff_hook(cfgmod.handoff_settings(self.cfg(bin=self.bin + "-gone")), self.wt)
        self.assertIn("skipped hook", note)


class RulesTest(Base):
    def test_working_set_rule_only_when_configured(self):
        on = spawn_worker.render_handoff(self.cfg())
        self.assertIn("jev-handoff working set", on)
        self.assertIn("Don't paste it into your replies", on)
        self.assertNotIn("jev-handoff", spawn_worker.render_handoff(self.cfg(bin=None)))
        # handoff.enabled false keeps only the continuing part, which still needs the rule.
        self.assertIn("jev-handoff working set", spawn_worker.render_handoff(self.cfg(enabled=False)))

    def test_keys_are_in_the_schema(self):
        for k in ("bin", "state_dir", "hook", "max_inline_lines"):
            self.assertIn(f"handoff.{k}", cfgmod.SCHEMA)
            self.assertIn(k, cfgmod.DEFAULTS["handoff"])


class WorkingSetTest(Base):
    def test_null_reasons(self):
        ws = spawn_worker.working_set_for(self.wt, self.cfg(bin=None), "t")
        self.assertIsNone(spawn_worker.handoff_report(ws)["handoff"])
        self.assertIn("feature off", ws["reason"])

        ws = spawn_worker.working_set_for(self.wt, self.cfg(bin=self.bin + "-gone"), "t")
        self.assertIn("bin not found", ws["reason"])

        ws = spawn_worker.working_set_for(self.wt, self.cfg(), "t")
        self.assertIn("no transcript", ws["reason"])

        # A transcript, but a dry run doesn't refresh, so no file exists yet.
        self.write_transcript()
        ws = spawn_worker.working_set_for(self.wt, self.cfg(), "t", dry=True)
        self.assertIn("no working set", ws["reason"])
        self.assertTrue(ws["refresh"].startswith("would run "))
        self.assertEqual(self.calls(), [])
        rep = spawn_worker.handoff_report(ws)
        self.assertIsNone(rep["handoff"])
        self.assertIn("handoff_reason", rep)

    def test_refresh_then_found(self):
        tfile = self.write_transcript()
        ws = spawn_worker.working_set_for(self.wt, self.cfg(), "My brief")
        self.assertEqual(self.calls(), [f"--state-dir {self.state} run --transcript {tfile} --task My brief"])
        rep = spawn_worker.handoff_report(ws)["handoff"]
        self.assertEqual(rep["path"], os.path.join(self.state, SID, "handoff.md"))
        self.assertEqual((rep["items"], rep["bunches"]), (3, 2))
        self.assertGreaterEqual(rep["age_s"], 0)

    def test_long_file_is_grepped_not_read(self):
        self.write_transcript()
        short = spawn_worker.working_set_for(self.wt, self.cfg(), "t")["read_first"]
        self.assertNotIn("don't read it whole", short)
        long_ = spawn_worker.working_set_for(self.wt, self.cfg(max_inline_lines=3), "t")["read_first"]
        self.assertIn("don't read it whole, grep it", long_)


class ContinueTest(Base):
    def run_continue(self, cfg, dry=False):
        brief_dir = os.path.join(self.tmp, "briefs", "pkg")
        os.makedirs(brief_dir, exist_ok=True)
        brief = os.path.join(brief_dir, "brief.md")
        with open(brief, "w") as f:
            f.write("# pkg: do the thing\n")
        sent = {}

        def fake_start(wt_id, agent_cmd, prompt, base_info, title="worker"):
            sent.update(prompt=prompt, info=base_info)

        a = types.SimpleNamespace(name="pkg", note=None, manager=None)
        out = io.StringIO()
        with mock.patch.object(spawn_worker, "orca", return_value={}), \
                mock.patch.object(spawn_worker, "start_agent", fake_start), \
                mock.patch.object(spawn_worker, "ensure_trusted", return_value="trusted"), \
                contextlib.redirect_stdout(out):
            spawn_worker.continue_worker(a, cfg, {"id": "w1", "path": self.wt}, brief_dir, brief,
                                         os.path.join(brief_dir, "common.md"), "/lock", "claude", "main", dry)
        if dry:
            return json.loads(out.getvalue()), brief_dir
        with open(os.path.join(brief_dir, "handoff-digest.md")) as f:
            sent["digest"] = f.read()
        return sent, brief_dir

    def test_prompt_names_working_set_first(self):
        self.write_transcript()
        sent, brief_dir = self.run_continue(self.cfg())
        p = sent["prompt"]
        hf = os.path.join(self.state, SID, "handoff.md")
        self.assertLess(p.index(hf), p.index(os.path.join(brief_dir, "common.md")))
        self.assertLess(p.index(hf), p.index(os.path.join(brief_dir, "brief.md")))
        self.assertLess(p.index(hf), p.index("handoff-digest.md"))
        self.assertIn("Trajectory section and the items of the last two bunches", p)
        self.assertIn("record of what the user decided", p)
        self.assertNotIn("\n", p)
        self.assertEqual(sent["info"]["handoff"]["path"], hf)
        self.assertIn("updated", sent["info"]["handoff_hook"])
        self.assertTrue(os.path.isfile(os.path.join(self.wt, ".claude", "settings.local.json")))

    def test_digest_drops_last_messages_with_working_set(self):
        self.write_transcript()
        sent, _ = self.run_continue(self.cfg())
        self.assertNotIn("PREVIOUS LAST WORDS", sent["digest"])
        self.assertNotIn("## Its last messages", sent["digest"])
        self.assertIn("## Git state now", sent["digest"])
        self.assertIn("Last messages omitted", sent["digest"])

    def test_feature_off_keeps_todays_prompt_and_digest(self):
        self.write_transcript()
        sent, brief_dir = self.run_continue(self.cfg(bin=None))
        self.assertIn("PREVIOUS LAST WORDS", sent["digest"])
        self.assertTrue(sent["prompt"].split("First read ", 1)[1].startswith(os.path.join(brief_dir, "common.md")))
        self.assertIsNone(sent["info"]["handoff"])
        self.assertIn("feature off", sent["info"]["handoff_reason"])
        self.assertNotIn("handoff_hook", sent["info"])
        self.assertEqual(self.calls(), [])

    def test_dry_run_reports_handoff_block_and_writes_nothing(self):
        self.write_transcript()
        res, brief_dir = self.run_continue(self.cfg(), dry=True)
        self.assertIsNone(res["handoff"])
        self.assertIn("no working set", res["handoff_reason"])
        self.assertTrue(res["handoff_hook"].startswith("would run "))
        self.assertFalse(os.path.exists(os.path.join(brief_dir, "handoff-digest.md")))
        self.assertEqual(self.calls(), [])


class WorktreesTest(Base):
    def test_hf_column(self):
        self.assertEqual(worktrees.fmt_hf(None), "hf -")
        self.assertEqual(worktrees.fmt_hf({"age_s": 180}), "hf 3m")
        self.assertEqual(worktrees.fmt_hf({"age_s": 7300}), "hf 2h")
        self.assertEqual(worktrees.fmt_hf({"age_s": 90000}), "hf 1d")

    def test_handoff_of_reads_the_current_sessions_file(self):
        tfile = self.write_transcript()
        self.assertIsNone(worktrees.handoff_of(tfile, self.state))
        self.assertIsNone(worktrees.handoff_of(None, self.state))
        d = os.path.join(self.state, SID)
        os.makedirs(d)
        with open(os.path.join(d, "handoff.md"), "w") as f:
            f.write(FIXTURE_MD)
        old = time.time() - 600
        os.utime(os.path.join(d, "handoff.md"), (old, old))
        h = worktrees.handoff_of(tfile, self.state)
        self.assertEqual(h["path"], os.path.join(d, "handoff.md"))
        self.assertEqual(worktrees.fmt_hf(h), "hf 10m")

    def test_cleanup_ignores_the_hook_settings_file(self):
        # A global excludes file (this machine's ignores settings.local.json) would hide the case.
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull}):
            spawn_worker.handoff_hook(cfgmod.handoff_settings(self.cfg()), self.wt)
            with open(os.path.join(self.wt, ".claude", "settings.local.json.bak"), "w") as f:
                f.write("{}")
            out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=self.wt,
                                 capture_output=True, text=True).stdout
            self.assertIn(".claude/settings.local.json", out)
            self.assertEqual(worktrees.dirty_count(self.wt), 0)
            row = {"name": "x", "exists": True, "dirty": worktrees.dirty_count(self.wt), "busy": False,
                   "comment": "", "pr_known": True, "pr": None, "ahead": 0, "on_base": False,
                   "branch": "main", "idle_hours": 10, "status": None}
            self.assertTrue(worktrees.decide(row, 3)[0])
            # Anything else untracked is still work.
            with open(os.path.join(self.wt, ".claude", "notes.md"), "w") as f:
                f.write("x")
            self.assertEqual(worktrees.dirty_count(self.wt), 1)


if __name__ == "__main__":
    unittest.main()
