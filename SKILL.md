---
name: orca-flow
description: Orchestrate coding agents through Orca worktrees, one owner per shared resource. A dispatcher picks tasks from a task source and starts one manager per task; a manager writes briefs and starts workers in new worktrees; workers open PRs; one merge-queue session merges, runs the full check and deploys. Use when the user wants work done in a worktree or in parallel ("fix X in a worktree", "start a worker", parallel workers), hands over several tasks at once, wants tasks from a source worked on with a manager each, says "merge and deploy", asks what the workers or managers are doing, wants idle worktrees cleaned up, or wants to change the flow's settings (handoff on/off, context limit, test commands, board thresholds). Also use it before changing code in the repo's main checkout. Workers started by this flow follow the common.md next to their brief instead. For threaded messages between agents, blocking ask/reply, task DAGs or decision gates, prefer Orca's orchestration skill. Not for Orca browser automation or general Orca CLI or terminal questions; use orca-cli for those.
---

# Orca flow

Four roles, one owner per shared resource. That's the whole idea: parallel agent sessions
collide over merging, deploying and shared status files, so exactly one session owns each.

| Role | Where | Does | Never |
|---|---|---|---|
| **Dispatcher** | main checkout | picks tasks from a task source, starts one manager per task, tracks them | writes worker briefs, starts workers, edits code |
| **Manager** | main checkout | writes briefs, starts workers, reviews PRs, hands approved PRs to the queue | edits feature code, merges, deploys |
| **Worker** | `<task>` worktree | builds one task, runs targeted tests, pushes a PR | merges, deploys, runs the full suite, starts other workers |
| **Merge queue** | its own clean worktree | merges, runs the full check once per batch, deploys, updates the status file | builds features |
| **Manager, no queue** (`merge_queue.enabled: false`) | main checkout | the manager's work, plus reviewing and merging PRs itself | edits feature code |

The main checkout stays on the base branch. Changes happen in Orca worktrees. If the
`main_checkout_guard.py` hook is installed, Edit and Write in the main checkout are blocked
except for the configured allow list. If it blocks you, move the work into a worktree — don't
route around it with Bash (`sed -i`, heredocs).

## Which file do I read

Read this file, then only your role's file:
- **Dispatcher** (the user wants tasks from a source worked on, one manager each): `references/dispatcher.md`
- **Manager** (the user asked for work, or `spawn_manager.py` started you): `references/manager.md`
- **Worker** (`spawn_worker.py` started you): the `common.md` next to your brief. Follow that file, not this one.
- **Merge queue**, or you're about to merge or deploy anything: `references/merge-queue.md`

Also: every config key in `references/configuration.md`, the board's rules in
`references/board.md`, the task-source contract in `references/sources.md`.

## Settings

**Project settings** live in an `orca-flow.json` (see `references/configuration.md`). It says
how this project is tested, checked and deployed, and how the flow itself behaves. Read it
before you claim any of that:
```
python3 scripts/config.py show                  # everything, resolved
python3 scripts/config.py keys                  # every key: value, default, what it does
```
If a value isn't configured, it isn't known — find out or ask, rather than assuming a command.

**Changing a setting** (the user asks to turn handoff off, raise the context limit, add a
check, …): use `config.py set`, never a hand edit of the JSON, and run `check` after.
```
python3 scripts/config.py set <key> <value> [--local] [--dry-run]   # e.g. set handoff.enabled false
python3 scripts/config.py set worker.checks "npm run lint" --append  # add one item to a list
python3 scripts/config.py unset <key> [--local]                     # back to the default
python3 scripts/config.py check
```
It writes the repo's config file, never the local one; `--local` writes the untracked
`<git-common-dir>/orca-flow/config.json` for personal preferences. Tell the user which file
changed. Changes reach sessions started afterwards; running workers keep their rules.

**Model:** never run workers, managers, the queue or subagents on Fable. Pass `--model opus`
(or the configured `worker.model` / `manager.model`) to every Agent subagent call too; a
subagent with no model inherits the caller's.

## Paths, context, addressing

- **Scripts** live in this skill's `scripts/`; run them by absolute path. Anything that changes
  state takes `--dry-run`, and `ORCA_FLOW_DRY_RUN=1` does the same.
- **Shared state** lives in `<git-common-dir>/orca-flow/` (i.e. `<repo>/.git/orca-flow/`). Every
  worktree can see it, and it never makes a checkout dirty.
- **Context budget.** Every session has a ceiling, and hitting it is routine, not an emergency:
  a manager's state is its `notes.md`, a worker's its branch plus a handoff file, the queue's
  the handover files plus the status file, and each can be restarted from those. Reads are what
  fill a context: nobody reads big files or the status file whole.
- **Always pass `--terminal <handle>`** to Orca. Without it, Orca picks whichever terminal is
  focused in the app, and that is usually someone else's. Your own handle is
  `$ORCA_TERMINAL_HANDLE`. **`--worktree active` is fine,** because it resolves from your
  current directory.
- **Status file** (`merge_queue.state_file`, if the project has one): only the queue writes it.

## After a crash or resume

When Orca restarts or sessions are resumed, every session gets a new name, so messages, handovers
and `manager.json` point at names that no longer exist. Each role fixes its own:
- **Manager:** read your new name from ListAgents and write it into `manager.json` `session`.
  Re-point your handovers: `python3 scripts/handover.py retarget --all-from <old name>
  --report-to <new name>`. Reread `notes.md`, then check every relayed requirement in it is
  being worked on; a relay that came only by message is gone.
- **Queue:** `python3 scripts/handover.py queue retire --reason "session renamed after restart"`,
  then `python3 scripts/handover.py queue start --session <new name>`.
- **Everyone:** Monitors died with the old session. Re-arm each one you had (handover status
  loops, worker waits); a "previous session ended" notice means the same.
- **Dispatcher:** `spawn_manager.py --list` and `board.py` to see which managers came back; tell
  each live one to do the above. A manager whose terminal is gone is dead: restart it with
  `--force`.

## Tests

Test suites that boot a database are memory-hungry, and several worktrees running them at once
is what kills a laptop. Wrap every test run in the lock:
```
bash scripts/test-lock.sh <the project's test command>
bash scripts/test-lock.sh --status
```
`worker.test_slots` (or `ORCA_FLOW_TEST_SLOTS`) sets how many runs can go at once, default 1.
Waiting in the queue can take longer than 2 minutes, so use `run_in_background` or a longer
timeout.

## Known pitfalls

- **Untracked files:** never move the user's untracked files out of the way to get a clean tree.
  Use a clean worktree instead.
- **Test count:** quote it from the run that passed.
- **Big files:** a file of thousands of lines read whole is half a context gone. Briefs give
  entry points with line ranges; workers grep and read ranges. `worker.big_files` names the
  known ones.
- **Updating the main checkout:** `git fetch`, then `git merge --ff-only <base branch>`, and only
  when it has no local commits. Otherwise leave it and tell the user.
- **Orca flags:** if a flag is unclear, run `orca <command> --help` or `orca skills get orca-cli`.
  Don't guess. Check `ok` in every `--json` response before reading `result`.
- **Prompts to a TUI:** one line, sent once, only after it's idle; never re-send after a failed
  send — the prompt may have landed. Read the terminal instead.
