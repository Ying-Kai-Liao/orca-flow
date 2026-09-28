"""Tests for spawn_worker.py's argument handling, through a dry run.

Run: python3 -m unittest discover -s tests

Each test runs the script as a subprocess against a throwaway git repo ($ORCA_FLOW_REPO) and a
fake `orca` ($ORCA_CLI_COMMAND) that knows that repo and no worktrees, so nothing here touches
the real Orca.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "spawn_worker.py")

FAKE_ORCA = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
def ok(result):
    print(json.dumps({"ok": True, "result": result}))
if args[:2] == ["repo", "list"]:
    ok({"repos": [{"id": "r1", "path": os.environ["ORCA_FLOW_REPO"]}]})
elif args[:2] == ["worktree", "list"]:
    ok({"worktrees": []})
else:
    print(json.dumps({"ok": False, "error": {"code": "unexpected " + " ".join(args)}}))
'''


class SpawnWorkerAttachTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.root = os.path.join(self.tmp, "repo")
        subprocess.run(["git", "init", "-q", "-b", "main", self.root], check=True)
        self.brief = os.path.join(self.tmp, "task.md")
        with open(self.brief, "w") as f:
            f.write("# Fix the login page\n")
        self.shots = []
        for i in range(1, 5):
            p = os.path.join(self.tmp, f"shot{i}.png")
            with open(p, "w") as f:
                f.write("png")
            self.shots.append(p)
        fake = os.path.join(self.tmp, "orca")
        with open(fake, "w") as f:
            f.write(FAKE_ORCA)
        os.chmod(fake, 0o755)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("ORCA_FLOW_CONFIG", "ORCA_FLOW_DRY_RUN", "ORCA_TERMINAL_HANDLE")}
        self.env.update(ORCA_FLOW_REPO=self.root, ORCA_CLI_COMMAND=fake,
                        CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "claude"))

    def dry_run(self, *args):
        r = subprocess.run([sys.executable, SCRIPT, "--name", "login-fix", "--brief", self.brief, *args, "--dry-run"],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout)

    def attached(self, res):
        return [f for f in res["copies"] if f in self.shots]

    def test_repeated_attach_keeps_every_file(self):
        s1, s2, s3, s4 = self.shots
        res = self.dry_run("--attach", s1, "--attach", s2, "--attach", s3, s4)
        self.assertEqual(self.attached(res), self.shots)

    def test_one_attach_with_several_files(self):
        res = self.dry_run("--attach", *self.shots)
        self.assertEqual(self.attached(res), self.shots)

    def test_no_attach(self):
        self.assertEqual(self.attached(self.dry_run()), [])


if __name__ == "__main__":
    unittest.main()
