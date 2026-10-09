#!/usr/bin/env python3
"""PreToolUse hook for Bash: block process kills that reach other sessions' processes, or Orca.

Why: several worktrees, their managers and the merge queue run the same command lines (dev
servers, watchers, test runs) on one machine, and Orca itself is a process tree under the
same user. Kills chosen by name or port hit all of them:
- `pkill -f "node --import tsx src/server.ts" -n -u 501`: BSD pkill stops reading options at
  the first pattern, so `-n`, `-u` and `501` became patterns too. They matched Orca and every
  login shell; every session died.
- `pkill -f "tsx src/server.ts"` matches every worktree's dev server, not just this one's.
- `lsof -ti tcp:3000 | xargs kill` lists the port's clients as well as its server, and Orca's
  built-in browser (Orca Helper … NetworkService) is a client of every dev server it shows.
- `kill -9 -1` signals every process the user owns.

Denied: pkill and killall in any form; `kill` fed by an lsof call without -sTCP:LISTEN (as
`$(…)` or through `| xargs kill`); kill of PID -1, 0 or 1. Everything else is allowed,
including `kill <pid>` and `kill $(lsof -tiTCP:<port> -sTCP:LISTEN)`.

The parse is deliberately small and conservative: it only looks at words in command position
(after `;`, `&&`, `||`, `|`, newlines, inside `$(…)`/backticks, behind sudo/xargs/env and
friends, and in `sh -c` strings). A missed kill is better than blocking `grep pkill file` or
`echo "pkill"`; the skill text covers what slips through.

Install (settings.json):
  {"hooks": {"PreToolUse": [{"matcher": "Bash",
     "hooks": [{"type": "command", "command": "python3 /abs/path/to/scripts/process_guard.py"}]}]}}
"""
import json
import os
import re
import sys

KEYWORDS = {"{", "}", "!", "if", "then", "else", "elif", "do", "while", "until", "time", "coproc"}
# Commands that run their arguments as a command; options are skipped up to the real command.
WRAPPERS = {"sudo", "doas", "nohup", "exec", "command", "builtin", "nice", "caffeinate",
            "timeout", "gtimeout", "env", "xargs", "stdbuf"}
# Wrapper options that take a separate value, so the value isn't mistaken for the command.
OPTS_WITH_VALUE = {
    "sudo": {"-u", "-g", "-p", "-C", "-h", "-U", "-r", "-t", "-D"},
    "doas": {"-u", "-C"},
    "nice": {"-n"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "gtimeout": {"-s", "-k", "--signal", "--kill-after"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir"},
    "xargs": {"-I", "-J", "-L", "-n", "-P", "-s", "-E", "-R", "-S", "-d", "-a", "--delimiter",
              "--arg-file", "--max-args", "--max-procs", "--replace"},
    "stdbuf": {"-i", "-o", "-e"},
}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

SAFE_KILL = ("Stop only a process you started, by its PID: `kill <pid>` (the PID from `$!` or from "
             "your own background task), or for a server you started on a port "
             "`kill $(lsof -tiTCP:<port> -sTCP:LISTEN)`. If the process isn't yours, leave it and "
             "use another port.")
PKILL_REASON = (
    "pkill/killall are blocked here. They pick processes by name or pattern, and other worktrees, "
    "managers and the merge queue run the same command lines on this machine; on macOS pkill also "
    "stops reading options at the first pattern, so flags after it become more patterns (that once "
    "matched Orca itself and killed every session). Don't swap in `kill $(pgrep -f …)` either: same "
    "matching. " + SAFE_KILL)
LSOF_REASON = (
    "Blocked: lsof without -sTCP:LISTEN lists the port's clients as well as its server, and Orca's "
    "built-in browser (Orca Helper … NetworkService) is a client of every dev server it shows, so "
    "this kills Orca. Ask lsof for the listening server only: "
    "`kill $(lsof -tiTCP:<port> -sTCP:LISTEN)`. " + SAFE_KILL)
PID_REASON = (
    "Blocked: kill of PID -1 signals every process you own (Orca and every session included), 0 "
    "signals your whole process group, and 1 is launchd. " + SAFE_KILL)


class Cmd:
    def __init__(self, words, subs):
        self.words = words  # quotes removed; a command substitution is left as a placeholder
        self.subs = subs    # source text of each $(…) / `…` / <(…) inside this command


def heredoc_start(s, i):
    """Read a heredoc operator at s[i] ('<<' or '<<-' plus its delimiter word).
    Returns (delimiter, strip_tabs, index after the delimiter)."""
    i += 2
    strip_tabs = s.startswith("-", i)
    i += 1 if strip_tabs else 0
    while i < len(s) and s[i] in " \t":
        i += 1
    m = re.match(r"""(['"]?)([^\s;&|<>()'"]*)\1""", s[i:])
    return m.group(2), strip_tabs, i + m.end()


def skip_heredoc_bodies(s, i, heredocs):
    """From the start of the line after a heredoc operator, skip each body through its delimiter."""
    n = len(s)
    for delim, strip_tabs in heredocs:
        while i < n:
            j = s.find("\n", i)
            line = s[i:] if j < 0 else s[i:j]
            i = n if j < 0 else j + 1
            if (line.lstrip("\t") if strip_tabs else line) == delim:
                break
    return i


def match_paren(s, i):
    """Index of the ')' closing a '(' just before i; len(s) when unbalanced."""
    depth, n = 1, len(s)
    heredocs = []
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        # Heredoc bodies are prose (commit messages): an apostrophe in "don't" must not be
        # taken for a quote, or the closing ')' is never found.
        if s.startswith("<<", i) and not s.startswith("<<<", i):
            delim, strip_tabs, i = heredoc_start(s, i)
            heredocs.append((delim, strip_tabs))
            continue
        if c == "\n" and heredocs:
            i = skip_heredoc_bodies(s, i + 1, heredocs)
            heredocs = []
            continue
        if c in "'\"":
            j = s.find(c, i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def parse(s):
    """Split a shell command line into pipelines of simple commands."""
    pipelines, pipe, words, subs = [], [], [], []
    word = None          # None: no word in progress (so '' from "" still counts as a word)
    skip_word = False    # the next word is a redirection target, not an argument
    heredocs = []
    i, n = 0, len(s)

    def end_word():
        nonlocal word, skip_word
        if word is not None:
            if skip_word:
                skip_word = False
            else:
                words.append(word)
        word = None

    def end_cmd():
        nonlocal words, subs
        end_word()
        if words or subs:
            pipe.append(Cmd(words, subs))
        words, subs = [], []

    def end_pipe():
        nonlocal pipe
        end_cmd()
        if pipe:
            pipelines.append(pipe)
        pipe = []

    while i < n:
        c = s[i]
        if c in " \t":
            end_word()
            i += 1
        elif c == "\n":
            end_pipe()
            # Heredoc bodies are data, not commands: `cat <<EOF` … `pkill` … `EOF` is fine.
            i = skip_heredoc_bodies(s, i + 1, heredocs)
            heredocs = []
        elif c == "#" and word is None:
            j = s.find("\n", i)
            i = n if j < 0 else j
        elif c == "'":
            j = s.find("'", i + 1)
            j = n if j < 0 else j
            word = (word or "") + s[i + 1:j]
            i = j + 1
        elif c == '"':
            buf, i = [], i + 1
            while i < n and s[i] != '"':
                if s[i] == "\\" and i + 1 < n:
                    buf.append(s[i + 1])
                    i += 2
                elif s.startswith("$(", i):
                    j = match_paren(s, i + 2)
                    if not s.startswith("$((", i):
                        subs.append(s[i + 2:j])
                    buf.append("$(…)")
                    i = j + 1
                elif s[i] == "`":
                    j = s.find("`", i + 1)
                    j = n if j < 0 else j
                    subs.append(s[i + 1:j])
                    buf.append("`…`")
                    i = j + 1
                else:
                    buf.append(s[i])
                    i += 1
            word = (word or "") + "".join(buf)
            i += 1
        elif c == "\\":
            if s[i + 1:i + 2] != "\n":  # backslash-newline is a line continuation
                word = (word or "") + s[i + 1:i + 2]
            i += 2
        elif s.startswith("$(", i):
            j = match_paren(s, i + 2)
            if not s.startswith("$((", i):  # $((…)) is arithmetic, not a command
                subs.append(s[i + 2:j])
            word = (word or "") + "$(…)"
            i = j + 1
        elif s.startswith("<(", i) or s.startswith(">(", i):
            end_word()
            j = match_paren(s, i + 2)
            subs.append(s[i + 2:j])
            i = j + 1
        elif c == "`":
            j = s.find("`", i + 1)
            j = n if j < 0 else j
            subs.append(s[i + 1:j])
            word = (word or "") + "`…`"
            i = j + 1
        elif s.startswith("<<", i) and not s.startswith("<<<", i):
            end_word()
            delim, strip_tabs, i = heredoc_start(s, i)
            heredocs.append((delim, strip_tabs))
        elif c in "<>":
            # A redirection: drop the fd number before it and the target after it.
            if word is not None and word.isdigit():
                word = None
            end_word()
            m = re.match(r"[<>]+(&(\d+|-)?)?", s[i:])
            i += m.end()
            if not m.group(2):  # 2>&1 and >&- carry their target in the operator
                skip_word = True
        elif c == "|":
            if s.startswith("||", i):
                end_pipe()
                i += 2
            else:
                end_cmd()
                i += 2 if s.startswith("|&", i) else 1
        elif c == "&":
            if s.startswith("&>", i):
                end_word()
                i += 3 if s.startswith("&>>", i) else 2
                skip_word = True
            else:
                end_pipe()
                i += 2 if s.startswith("&&", i) else 1
        elif c in ";()":
            end_pipe()
            i += 1
        else:
            word = (word or "") + c
            i += 1
    end_pipe()
    return pipelines


def resolve(words):
    """(command words starting at the real command, whether it runs under xargs)."""
    i, via_xargs = 0, False
    while i < len(words):
        w = words[i]
        if w in KEYWORDS or ASSIGNMENT.match(w):
            i += 1
            continue
        name = os.path.basename(w)
        if name not in WRAPPERS:
            return words[i:], via_xargs
        via_xargs = via_xargs or name == "xargs"
        takes_value = OPTS_WITH_VALUE.get(name, set())
        i += 1
        while i < len(words):
            o = words[i]
            if o == "--":
                i += 1
                break
            if name == "env" and ASSIGNMENT.match(o):
                i += 1
            elif o.startswith("-") and len(o) > 1:
                i += 2 if o in takes_value else 1
            elif name in ("timeout", "gtimeout") and re.match(r"^\d", o):
                i += 1  # the duration
                break
            else:
                break
    return [], via_xargs


def lsof_without_listen(cmd):
    words, _ = resolve(cmd.words)
    if not words or os.path.basename(words[0]) != "lsof":
        return False
    # -sTCP:LISTEN, or -s TCP:LISTEN as two words
    return not any("TCP:LISTEN" in w.upper() for w in words[1:])


def kill_targets(args):
    """PIDs a `kill` call signals; the signal spec and options are skipped."""
    pids, i = [], 0
    if args and args[0] in ("-l", "-L"):
        return []
    while i < len(args):
        a = args[i]
        if a == "--":
            return pids + args[i + 1:]
        if i == 0 and a in ("-s", "-n"):
            i += 2
            continue
        if i == 0 and a.startswith("-") and len(a) > 1:
            i += 1  # -9, -KILL, -SIGTERM: the signal; anything after it is a PID, -1 included
            continue
        pids.append(a)
        i += 1
    return pids


def check(command, depth=0):
    """Deny reason for a command line, or None."""
    if depth > 5:
        return None
    for pipeline in parse(command):
        for idx, cmd in enumerate(pipeline):
            for sub in cmd.subs:
                reason = check(sub, depth + 1)
                if reason:
                    return reason
            words, via_xargs = resolve(cmd.words)
            if not words:
                continue
            name, args = os.path.basename(words[0]), words[1:]
            if name in ("pkill", "killall"):
                return PKILL_REASON
            if name in SHELLS or name == "eval":
                script = None
                if name == "eval":
                    script = " ".join(args)
                else:
                    for k, a in enumerate(args):
                        if re.match(r"^-[a-zA-Z]*c[a-zA-Z]*$", a) and k + 1 < len(args):
                            script = args[k + 1]
                            break
                reason = script and check(script, depth + 1)
                if reason:
                    return reason
            if name != "kill":
                continue
            if any(p in ("-1", "0", "1") for p in kill_targets(args)):
                return PID_REASON
            fed_by = [c for sub in cmd.subs for p in parse(sub) for c in p]
            if via_xargs:
                fed_by += pipeline[:idx]
            if any(lsof_without_listen(c) for c in fed_by):
                return LSOF_REASON
    return None


def deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))
    return 0


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        # Allow on unreadable input: a broken hook blocking every command is worse than one miss.
        return 0
    if not isinstance(data, dict) or data.get("tool_name", "Bash") != "Bash":
        return 0
    command = (data.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not command:
        return 0
    try:
        reason = check(command)
    except Exception:
        return 0  # same reasoning: a parser bug must not block unrelated commands
    return deny(reason) if reason else 0


if __name__ == "__main__":
    sys.exit(main())
