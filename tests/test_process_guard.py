"""Tests for process_guard.py, the PreToolUse hook that blocks kills reaching other sessions or Orca.

Run: python3 -m unittest tests/test_process_guard.py

The denied commands are the ones from real incidents; the allowed ones are look-alikes that a
cruder match would block.
"""
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, SCRIPTS)
import process_guard as guard  # noqa: E402


def run_hook(payload):
    """stdout of main() for a hook payload (a dict, or raw text)."""
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    out = io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(raw)), redirect_stdout(out):
        code = guard.main()
    assert code == 0
    return out.getvalue()


def bash(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp"}


class DeniedTest(unittest.TestCase):
    def assertDenied(self, command, reason=None):
        got = guard.check(command)
        self.assertIsNotNone(got, f"should be denied: {command!r}")
        if reason:
            self.assertEqual(got, reason, command)

    def test_incident_pkill_with_options_after_pattern(self):
        self.assertDenied('pkill -f "node --import tsx src/server.ts" -n -u $(id -u)', guard.PKILL_REASON)

    def test_pkill_and_killall_any_form(self):
        for c in ['pkill -f "tsx src/server.ts"', "pkill node", "killall node", "killall -9 Orca",
                  "/usr/bin/pkill -f vite", "sudo pkill -f vite", "pkill -f vite || true",
                  "cd web && pkill -f 'next dev'", "npm run build; killall node",
                  "echo stopping\npkill -f vite", "nohup pkill -f vite", "FOO=1 pkill x",
                  "pgrep -f vite | xargs pkill", "timeout 5 pkill -f vite",
                  "if pkill -f vite; then echo ok; fi", "(pkill -f vite)", "{ pkill -f vite; }",
                  "echo $(pkill -f vite)", "bash -c 'pkill -f vite'", "zsh -lc \"killall node\"",
                  "eval 'pkill -f vite'"]:
            self.assertDenied(c, guard.PKILL_REASON)

    def test_lsof_without_listen_feeding_kill(self):
        for c in ["lsof -ti tcp:3000 | xargs kill", "lsof -ti:3000 | xargs kill -9",
                  "lsof -t -i :5173 | grep -v $$ | xargs kill", "kill $(lsof -ti:3000)",
                  "kill -9 $(lsof -t -i tcp:3000)", 'kill "$(lsof -ti :3000)"',
                  "kill `lsof -ti:3000`", "lsof -ti:3000 | xargs -r kill -9",
                  "lsof -ti:3000 | xargs -n 1 kill"]:
            self.assertDenied(c, guard.LSOF_REASON)

    def test_kill_everything_or_init(self):
        for c in ["kill -9 -1", "kill -1 -1", "kill -- -1", "kill -s KILL -1", "kill 0", "kill -9 1",
                  "kill -TERM 0", "kill 1234 -1"]:
            self.assertDenied(c, guard.PID_REASON)

    def test_hook_output_is_a_deny(self):
        out = json.loads(run_hook(bash("pkill -f vite")))["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertIn("-sTCP:LISTEN", out["permissionDecisionReason"])


class AllowedTest(unittest.TestCase):
    def assertAllowed(self, command):
        self.assertIsNone(guard.check(command), f"should be allowed: {command!r}")

    def test_kill_by_pid(self):
        for c in ["kill 12345", "kill -9 12345", "kill -TERM 4242 4243", "kill %1", "kill $PID",
                  "kill $!", "kill -1 12345", "kill -l", "kill 12345 2>/dev/null || true",
                  "kill 12345 2>&1", "kill 10 11"]:
            self.assertAllowed(c)

    def test_kill_listening_server(self):
        for c in ["kill $(lsof -tiTCP:3000 -sTCP:LISTEN)", "kill -9 $(lsof -ti tcp:3000 -sTCP:LISTEN)",
                  "lsof -tiTCP:3000 -sTCP:LISTEN | xargs kill", "kill $(lsof -t -iTCP:3000 -s TCP:LISTEN)",
                  'kill "$(lsof -tiTCP:5173 -sTCP:LISTEN)"']:
            self.assertAllowed(c)

    def test_lsof_alone_is_fine(self):
        for c in ["lsof -i :3000", "lsof -ti:3000", "lsof -ti:3000 | head"]:
            self.assertAllowed(c)

    def test_words_that_are_not_commands(self):
        for c in ["grep pkill file", "grep -rn 'pkill' scripts", 'echo "pkill"', "echo pkill killall",
                  "man pkill", "which killall", "rg 'kill -9 -1' .", 'git commit -m "block pkill"',
                  "cat scripts/process_guard.py | grep killall", "python3 -m unittest tests/test_process_guard.py",
                  "# pkill -f vite", "echo done # then pkill", "ls skill-pkill-notes",
                  "echo 'lsof -ti:3000 | xargs kill'", "echo kill 0", "printf '%s\\n' 'kill -9 -1'",
                  "seq 1 3 | xargs echo kill", "pgrep -fl vite", "ps aux | grep vite"]:
            self.assertAllowed(c)

    def test_heredoc_body_is_data(self):
        self.assertAllowed("cat > notes.md <<'EOF'\npkill -f vite\nkillall node\nkill -9 -1\nEOF\necho ok")
        self.assertAllowed("cat <<-EOF\n\tpkill -f vite\n\tEOF")

    def test_commit_message_heredoc(self):
        self.assertAllowed('git commit -m "$(cat <<\'EOF\'\nDon\'t pkill; it\'s blocked now (kill 0 too)\nEOF\n)"')

    def test_arithmetic_and_redirections(self):
        for c in ["echo $((1 + 2))", "make test > out.log 2>&1", "node server.js &> log &",
                  "echo $(date) >> log"]:
            self.assertAllowed(c)


class HookInputTest(unittest.TestCase):
    def test_non_bash_tool_or_missing_command_is_allowed(self):
        self.assertEqual(run_hook({"tool_name": "Edit", "tool_input": {"file_path": "/x", "command": "pkill x"}}), "")
        self.assertEqual(run_hook({"tool_name": "Bash", "tool_input": {}}), "")
        self.assertEqual(run_hook({"tool_name": "Bash"}), "")
        self.assertEqual(run_hook({"tool_name": "Bash", "tool_input": {"command": 3}}), "")
        self.assertEqual(run_hook("not json"), "")
        self.assertEqual(run_hook("[]"), "")

    def test_allowed_command_prints_nothing(self):
        self.assertEqual(run_hook(bash("kill 12345")), "")

    def test_parser_error_allows(self):
        with mock.patch.object(guard, "check", side_effect=RuntimeError("boom")):
            self.assertEqual(run_hook(bash("pkill x")), "")

    def test_as_a_subprocess(self):
        r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "process_guard.py")],
                           input=json.dumps(bash("killall node")), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
