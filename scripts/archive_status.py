#!/usr/bin/env python3
"""Move old entries out of the hand-written status file into an archive next to it.

Why: the status file is read by every worker (its top), and read and edited by the queue
every batch. Left alone it grows by one dense entry per deploy; at 800 lines it costs a
third of a session's context to read, for entries nobody will act on. The file's job is
"what git can't tell you about now", so only the newest entries belong in it.

An entry is a top-level bullet (`- ` at column 0) plus its indented continuation lines.
Everything before the first entry (title, comments) stays. The newest `keep` entries stay.
The rest are prepended to the archive file under a dated heading, oldest last, so the
archive reads newest-first like the status file itself.

Usage:
  archive_status.py [--keep 10] [--dry-run]
Config: merge_queue.state_file (required), merge_queue.state_file_keep (default 10),
        merge_queue.archive_file (default: <state file stem>-archive.md next to it).
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfgmod  # noqa: E402


def split_entries(text):
    lines = text.splitlines(keepends=True)
    head, entries, cur = [], [], None
    for line in lines:
        if line.startswith("- "):
            if cur is not None:
                entries.append(cur)
            cur = [line]
        elif cur is None:
            head.append(line)
        else:
            cur.append(line)
    if cur is not None:
        entries.append(cur)
    return "".join(head), ["".join(e) for e in entries]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--keep", type=int)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    cfg = cfgmod.load()
    mq = cfg["merge_queue"]
    state_file = mq.get("state_file")
    if not state_file:
        print(json.dumps({"ok": False, "error": "merge_queue.state_file is not configured"}))
        sys.exit(1)
    # The state file is edited in the checkout you're standing in (the queue's clean
    # worktree), not in the main checkout the config resolves to: worktrees share one
    # git common dir, and the main checkout is often stale or dirty.
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    root = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else cfg["repo_root"]
    src = os.path.join(root, state_file)
    if not os.path.isfile(src):
        print(json.dumps({"ok": False, "error": f"{src} not found"}))
        sys.exit(1)
    keep = a.keep if a.keep is not None else int(mq.get("state_file_keep") or 10)
    stem, ext = os.path.splitext(state_file)
    archive_rel = mq.get("archive_file") or f"{stem}-archive{ext or '.md'}"
    dst = os.path.join(root, archive_rel)

    with open(src, encoding="utf-8") as f:
        text = f.read()
    head, entries = split_entries(text)
    if len(entries) <= keep:
        print(f"{state_file}: {len(entries)} entries, keep {keep}; nothing to archive.")
        return
    moving = entries[keep:]
    pointer = f"<!-- older entries: {archive_rel} -->\n"
    if archive_rel not in head:
        # Put the pointer right after the leading comment block / title, before the first entry.
        head = head.rstrip("\n") + "\n" + pointer + "\n"
    new_state = head + "".join(entries[:keep])
    stamp = datetime.date.today().isoformat()
    block = f"## Archived {stamp} ({len(moving)} entries)\n\n" + "".join(moving).rstrip("\n") + "\n\n"
    if os.path.isfile(dst):
        with open(dst, encoding="utf-8") as f:
            old = f.read()
        title_end = old.find("\n\n") + 2 if old.startswith("#") else 0
        new_archive = old[:title_end] + block + old[title_end:]
    else:
        new_archive = (f"# {os.path.basename(stem)} archive\n\n<!-- Entries moved out of {state_file} by "
                       f"archive_status.py. Newest block first; within a block, newest first. -->\n\n" + block)

    dry = a.dry_run or os.environ.get("ORCA_FLOW_DRY_RUN") == "1"
    summary = {"ok": True, "dry_run": dry, "state_file": state_file, "kept": keep, "archived": len(moving),
               "archive_file": archive_rel, "state_file_lines": (text.count("\n"), new_state.count("\n"))}
    if dry:
        summary["first_archived_entry"] = moving[0].splitlines()[0][:100]
        summary["last_kept_entry"] = entries[keep - 1].splitlines()[0][:100]
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return
    with open(dst, "w", encoding="utf-8") as f:
        f.write(new_archive)
    with open(src, "w", encoding="utf-8") as f:
        f.write(new_state)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
