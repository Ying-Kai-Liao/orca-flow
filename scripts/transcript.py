#!/usr/bin/env python3
"""Read a Claude Code session transcript from disk: how full its context is, and what it did.

Why: a worker can't be trusted to notice its own context filling up, and the moment it
would need to hand off is the moment it's least able to write a good handoff. But the
transcript is on disk, one JSONL per session, under ~/.claude/projects/<mangled cwd>/.
So the manager can read a worker's context size without the worker cooperating, and a
replacement worker can be given a digest of what the previous one did instead of the
whole log.

What counts toward context: user text, assistant text, tool-call inputs and tool results
since the last compaction. Thinking blocks are not carried forward, so they're skipped.
Images cost a roughly fixed amount each, not their byte size. The estimate is rough on
purpose (bytes, not a tokenizer); it's for "is this session near the ceiling", not billing.

Usage:
  transcript.py measure <worktree-path> [--json]
  transcript.py digest  <worktree-path> [--json]
  transcript.py path    <worktree-path>            # which file would be read
"""
import argparse
import glob
import json
import os
import re
import sys

IMAGE_TOKENS = 1500  # a screenshot; the real figure depends on size but this is the order of magnitude


def projects_dir(override=None):
    if override:
        return os.path.expanduser(override)
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def project_dir(worktree_path, override=None):
    # Claude Code names the folder after the cwd with every non-alphanumeric character
    # replaced by a dash: /Users/me/orca/ws/repo/task -> -Users-me-orca-ws-repo-task
    mangled = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(worktree_path))
    return os.path.join(projects_dir(override), mangled)


def latest_transcript(worktree_path, override=None):
    """Newest session file for that cwd, or None. Subagent transcripts live in
    subfolders, so only the top level is considered."""
    d = project_dir(worktree_path, override)
    files = glob.glob(os.path.join(d, "*.jsonl"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def _text_of(content):
    if isinstance(content, str):
        return content, 0
    text, images = [], 0
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            text.append(b.get("text", ""))
        elif t == "image":
            images += 1
        elif t == "tool_result":
            inner, img = _text_of(b.get("content"))
            text.append(inner)
            images += img
    return "".join(text), images


def _tokens(s):
    # ASCII runs ~4 chars per token; CJK and other non-ASCII closer to 1 char per token.
    ascii_n = sum(1 for ch in s if ord(ch) < 128)
    return ascii_n / 4 + (len(s) - ascii_n)


def _iter(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def measure(path):
    """Context estimate for one transcript, counted since the last compaction."""
    tokens = 0.0
    images = 0
    turns = 0
    compactions = 0
    tool_names = {}
    first_ts = last_ts = None
    session_id = None
    for o in _iter(path):
        t = o.get("type")
        if o.get("isCompactSummary") or o.get("subtype") == "compact_boundary":
            # Everything before this point was replaced by a summary; start over from it.
            tokens = 0.0
            images = 0
            compactions += 1
        if t not in ("user", "assistant"):
            continue
        session_id = session_id or o.get("sessionId")
        ts = o.get("timestamp")
        first_ts = first_ts or ts
        last_ts = ts or last_ts
        m = o.get("message") or {}
        content = m.get("content")
        if t == "assistant":
            turns += 1
        if isinstance(content, str):
            tokens += _tokens(content)
            continue
        for b in content or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                tokens += _tokens(b.get("text", ""))
            elif bt == "tool_use":
                tool_names[b.get("id")] = b.get("name")
                tokens += _tokens(json.dumps(b.get("input"), ensure_ascii=False))
            elif bt == "tool_result":
                txt, img = _text_of(b.get("content"))
                tokens += _tokens(txt)
                images += img
            elif bt == "image":
                images += 1
    tokens += images * IMAGE_TOKENS
    return {
        "file": path,
        "session_id": session_id,
        "tokens_estimate": int(tokens),
        "images": images,
        "assistant_turns": turns,
        "compactions": compactions,
        "started_at": first_ts,
        "last_at": last_ts,
        "mtime": os.path.getmtime(path),
    }


def digest(path, max_messages=4, max_chars=1200):
    """What the session did, for a replacement worker: files it edited, commands that
    changed state, the last few things it said, and the last thing it was told."""
    edited = []
    commits = []
    last_assistant = []
    last_user = None
    turns = 0
    tools = {}
    for o in _iter(path):
        t = o.get("type")
        if t not in ("user", "assistant"):
            continue
        content = (o.get("message") or {}).get("content")
        if t == "assistant":
            turns += 1
            for b in content or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    last_assistant.append(b["text"].strip())
                    last_assistant = last_assistant[-max_messages:]
                elif b.get("type") == "tool_use":
                    name, inp = b.get("name"), b.get("input") or {}
                    tools[b.get("id")] = name
                    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit") and inp.get("file_path"):
                        if inp["file_path"] not in edited:
                            edited.append(inp["file_path"])
                    elif name == "Bash":
                        cmd = inp.get("command") or ""
                        if re.search(r"\bgit (commit|push)\b", cmd):
                            commits.append(cmd.strip()[:160])
        else:
            if isinstance(content, str):
                if content.strip() and not content.startswith("<"):
                    last_user = content.strip()
            else:
                for b in content or []:
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip() \
                            and not b["text"].lstrip().startswith("<"):
                        last_user = b["text"].strip()
    return {
        "file": path,
        "assistant_turns": turns,
        "files_edited": edited,
        "git_commands": commits[-8:],
        "last_assistant_messages": [m[:max_chars] for m in last_assistant],
        "last_user_message": (last_user or "")[:max_chars],
    }


def render_digest(d, worktree_path=None, git_summary=None):
    lines = ["# Handoff digest (generated from the previous session's transcript)", ""]
    lines.append(f"- Transcript: `{d['file']}` ({d['assistant_turns']} assistant turns). Grep it for specifics; don't read it whole.")
    if worktree_path:
        lines.append(f"- Worktree: `{worktree_path}`")
    if git_summary:
        lines += ["", "## Git state now", "", "```", git_summary.strip(), "```"]
    lines += ["", "## Files the previous session edited", ""]
    lines += [f"- `{f}`" for f in d["files_edited"]] or ["- (none recorded)"]
    if d["git_commands"]:
        lines += ["", "## Commit / push commands it ran", ""] + [f"- `{c}`" for c in d["git_commands"]]
    lines += ["", "## Its last messages (oldest first)", ""]
    for m in d["last_assistant_messages"]:
        lines += ["> " + m.replace("\n", "\n> "), ""]
    if d["last_user_message"]:
        lines += ["## The last thing it was told", "", "> " + d["last_user_message"].replace("\n", "\n> "), ""]
    return "\n".join(lines).rstrip() + "\n"


def git_summary(worktree_path, base="origin/main"):
    import subprocess
    out = []
    for label, cmd in (("HEAD", ["git", "log", "--oneline", "-1"]),
                       ("branch", ["git", "branch", "--show-current"]),
                       ("status", ["git", "status", "--short"]),
                       (f"commits ahead of {base}", ["git", "log", "--oneline", f"{base}..HEAD"]),
                       (f"files changed vs {base}", ["git", "diff", "--stat", f"{base}...HEAD"])):
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=worktree_path)
        body = r.stdout.strip() if r.returncode == 0 else f"({r.stderr.strip()[-200:]})"
        out.append(f"{label}:\n{body or '(none)'}")
    return "\n\n".join(out)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("measure", "digest", "path"):
        s = sub.add_parser(name)
        s.add_argument("worktree_path")
        s.add_argument("--json", action="store_true")
        s.add_argument("--projects-dir", help="override ~/.claude/projects")
    a = p.parse_args()
    f = latest_transcript(a.worktree_path, a.projects_dir)
    if a.cmd == "path":
        print(f or "")
        return
    if not f:
        print(json.dumps({"ok": False, "error": f"no transcript under {project_dir(a.worktree_path, a.projects_dir)}"}))
        sys.exit(1)
    if a.cmd == "measure":
        m = measure(f)
        print(json.dumps(m, indent=1) if a.json else
              f"{m['tokens_estimate']} tokens (est.), {m['assistant_turns']} turns, {m['images']} images, "
              f"{m['compactions']} compaction(s)  {f}")
        return
    d = digest(f)
    print(json.dumps(d, ensure_ascii=False, indent=1) if a.json else render_digest(d, a.worktree_path, git_summary(a.worktree_path)))


if __name__ == "__main__":
    main()
