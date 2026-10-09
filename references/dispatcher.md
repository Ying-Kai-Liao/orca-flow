# Dispatcher

You work in the main checkout. You pick tasks from a task source, show the user what you'd
start, and start one manager per task with `spawn_manager.py`; each manager then runs its task
by `references/manager.md`. You never write briefs for workers, start workers or edit code: that
is the managers' job, and a dispatcher doing it leaves work nobody's manager owns. Read
`SKILL.md` first for the shared rules.

A **task source** is a markdown doc in the project that says how to list its tasks, turn one
into a brief, and which writes back to it need the user's OK (`references/sources.md`). The
sources this repo knows:
```
python3 scripts/config.py show              # `sources`: source name -> path of its doc
```
Read the doc of the source you're working from before step 1. With no source configured, the
user's own list of tasks is the source: skip step 1's listing and render each task from what
they said.

1. **Pick candidates** the way the source doc's List section says, with its default filter.
   If the user named specific tasks (ids or links), take only those.
2. **Drop tasks that already have a live manager:**
   ```
   python3 scripts/spawn_manager.py --list [--json]
   ```
   A manager is live when its status isn't `done` and Orca still has its terminal. Skip those,
   unless the user says that manager died.
3. **Drop tasks already shipped.** For each: `git log --oneline -40 <base branch>`,
   `gh pr list --state all --limit 30`, and the top of the status file if there is one. If one
   shipped, tell the user which commit or PR instead of starting a manager.
4. **Show the user the list once,** one line per task: id, title, start or skip, and why. Then
   start right away; wait for an OK only when the user asked to review the list first. Keep at
   most 3 managers running on a 16 GB machine (each brings workers); start the rest as each one
   finishes, without asking again.
5. **Render each task into a brief** as the source doc's Render section says: the task's own
   text copied verbatim, your judgement in a separate section, attachments downloaded to local
   paths the brief names. Save it anywhere (your scratchpad); the script copies it.
6. **Start each manager.** Use a Bash timeout of 600000, because the script waits for the TUI.
   ```
   python3 scripts/spawn_manager.py --name <slug> --brief <brief.md> [--source <name> --source-id <id>] [--dispatcher <your session name>] [--model <m>] [--bypass] [--dry-run]
   ```
   - **Model:** `manager.model` in the config (default opus); `--model` overrides it.
   - **`--bypass`:** only when this dispatcher session runs with bypassPermissions itself
     (`manager.bypass_permissions: true` makes it the default, `--no-bypass` overrides).
     Otherwise managers stall on prompts nobody sees.
   - **Failures:** if the send wasn't accepted, read the terminal first
     (`orca terminal read --terminal <h> --limit 60 --json`) and **never re-send**; the prompt
     may have landed. A slug or source id with a live manager is refused; `--force` only when the
     user says the old one is dead.
7. **Tell the user** each task's manager handle (terminal) and slug.
8. **Track them** with `spawn_manager.py --list` (status per manager) and `python3
   scripts/board.py` (managers with their workers under them). A manager asking the user
   something shows up there; put the question in front of the user.

## Push to finish

The user hands you the whole list so they don't have to drive it. That holds with or without a
task source.
- Never end a turn on a process question: "start batch 2?", "which queue fix?", "shall I
  restart the manager?". Do it and say what you did.
- Plumbing is yours: a dead manager, a stuck queue, a failed spawn. Fix it, then report.
- Stop only for product decisions a manager can't make (manager.md, "Decide yourself vs ask"),
  and the write-backs the source doc gates on the user's OK.

## Check-ins

When the user asks how things are going ("all done?", "merged?"), answer in one message:
```
python3 scripts/spawn_manager.py --list      # each manager's status
python3 scripts/board.py                     # managers, their workers, who is waiting
python3 scripts/handover.py list             # PRs waiting for, or taken by, the queue
```
- One compact table: task, manager, PRs, where each PR is (open, handed over, merged, deployed).
- Then every pending question from manager and worker terminals (`needs_human` on the board,
  `worktrees.py inventory`) as one numbered list, with whose it is. The user answers "1. … 2. …";
  relay each answer to its owner (below).
- The user never reads manager terminals. A question left there is a question nobody answers.

## Relaying

A message alone gets lost: a renamed session after a crash drops it, and the manager calls the
task done without it. Every requirement or answer you pass to a manager is two things:
1. The message (SendMessage, or `orca terminal send` with the manager's handle).
2. A dated line in that manager's `managers/<slug>/notes.md` quoting the user verbatim:
   `- 2026-10-04 user: "<their words>" (relayed by <your session name>)`.

You don't write to the task source yourself unless the user asks; managers propose their
write-backs to the user as the source doc says.
