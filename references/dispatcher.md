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
4. **Show the user the list once,** one line per task: id, title, start or skip, and why. Wait
   for their OK. More managers means more workers: above ~3, suggest batching.
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

You don't write to the task source yourself unless the user asks; managers propose their
write-backs to the user as the source doc says.
