---
name: orca-flow
description: Run a repo's changes through Orca worktrees. A manager session writes briefs and starts workers in new worktrees. Workers open PRs. One merge-queue session merges, runs the full check and deploys. Use when the user wants work done in a worktree or in parallel ("fix X in a worktree", 開 worktree 修, 開 worker, parallel workers), hands over several tasks at once, says "merge and deploy" or 合併部署, asks what the workers are doing, or wants idle worktrees cleaned up. Also use it before changing code in the repo's main checkout. Workers started by this flow follow the common.md next to their brief instead. Not for Orca browser automation or general Orca CLI questions; use orca-cli for those.
---

# Orca flow

Three roles, one owner per shared resource. That's the whole idea: parallel agent sessions
collide over merging, deploying and shared status files, so exactly one session owns each.

| Role | Where | Does | Never |
|---|---|---|---|
| **Manager** | main checkout | writes briefs, starts workers, reviews PRs, hands approved PRs to the queue | edits feature code, merges, deploys |
| **Worker** | `<task>` worktree | builds one task, runs targeted tests, pushes a PR | merges, deploys, runs the full suite, starts other workers |
| **Merge queue** | its own clean worktree | merges, runs the full check once per batch, deploys, updates the status file | builds features |

The main checkout stays on the base branch. Changes happen in Orca worktrees. If the
`main_checkout_guard.py` hook is installed, Edit and Write in the main checkout are blocked
except for the configured allow list. If it blocks you, move the work into a worktree — don't
route around it with Bash (`sed -i`, heredocs).

**Project settings** live in an `orca-flow.json` (see `references/configuration.md`). It says
how this project is tested, checked and deployed. Read it before you claim any of that:
```
python3 scripts/config.py show
```
If a value isn't configured, it isn't known — find out or ask, rather than assuming a command.

**Paths:**
- **Scripts** live in this skill's `scripts/`; run them by absolute path. Anything that changes
  state takes `--dry-run`, and `ORCA_FLOW_DRY_RUN=1` does the same.
- **Shared state** lives in `<git-common-dir>/orca-flow/` (i.e. `<repo>/.git/orca-flow/`). Every
  worktree can see it, and it never makes a checkout dirty.

**Orca addressing:**
- **Always pass `--terminal <handle>`.** Without it, Orca picks whichever terminal is focused in
  the app, and that is usually someone else's. Your own handle is `$ORCA_TERMINAL_HANDLE`.
- **`--worktree active` is fine,** because it resolves from your current directory.

## Manager: from request to workers

1. **Check the work isn't already done.** Look at `git log --oneline -30 <base branch>`,
   `gh pr list --state all --limit 30` and the relevant code. Requests often arrive after
   someone has already shipped them. If it's done, tell the user what shipped (commit or PR)
   instead of starting workers.
2. **Split by files touched, not by feature.** Two workers editing the same part of one big file
   will conflict at merge time. Overlapping tasks become one package, or run one after the
   other. Don't guess overlaps from PR titles. List the files each task will touch, then run:
   ```
   python3 scripts/worktrees.py overlap <file-or-dir> ...
   ```
   It prints every open PR and worktree (committed or uncommitted) touching those paths, plus
   migration numbers already taken outside the base branch. Copy the relevant lines into the
   brief's scope section.
3. **Write one brief per task** from `assets/brief-template.md`, in the project's language
   (`language` in the config).
   - Include background, acceptance criteria, what's out of scope, and decisions already made.
   - Name existing code that looks similar but is a different feature — a CSV *import* sitting
     next to the new CSV *export*, say. Naming it keeps the worker from reusing or breaking it.
   - Workers can't see this conversation, so any image the user pasted must be passed with
     `--attach`.
   - Save the brief anywhere; the script copies it.
4. **Start each worker.** Use a Bash timeout of 600000, because the script waits up to ~6
   minutes for the TUI.
   ```
   python3 scripts/spawn_worker.py --name <task-slug> --brief <brief.md> [--attach <image> ...] [--bypass] [--dry-run]
   ```
   The script:
   - creates the worktree (`--no-parent --setup run`)
   - renders the worker rules from the config and copies them, the brief, the attachments and
     the test lock into `<git-common-dir>/orca-flow/briefs/<task>/`
   - starts the agent, waits until its TUI is idle, and sends the prompt once
   - prints JSON with the worktree id, path, terminal handle and send receipt

   Rules for starting workers:
   - **Model:** `worker.model` in the config; `--model` overrides it for one worker.
   - **`--bypass`:** pass it only when this manager session also runs with bypassPermissions.
     Otherwise workers stall on prompts nobody sees.
   - **Failures:** report the printed error. If the send step failed, the prompt may have
     arrived anyway. Read the terminal first and never re-send.
5. **Tell the user** each worker's name, worktree path and terminal handle.
6. **Watch the Orca card, not the terminal text.** Workers finish by setting their card to
   `in-review` with the comment `PR #n:…`, or by commenting `BLOCKED:…`. To wait for one
   worker, run a Monitor until-loop on:
   ```
   python3 scripts/worktrees.py status <task-slug>     # prints in-review / blocked / in-progress / missing
   ```
   - **All workers:** `worktrees.py inventory`.
   - **Details:** `orca terminal read --terminal <handle> --limit 60 --json`.
   - **No polling with `sleep`:** a foreground `sleep` is blocked by the harness.
7. **Review each PR,** yourself or with a subagent. Send fixes as **one line**, because a
   newline can submit the text early:
   ```
   orca terminal send --terminal <handle> --text "<review comments>" --enter --wait-submit 15 --json
   ```
8. **Hand approved PRs to the queue.** Follow `references/merge-queue.md` → "Finding the running
   queue" and "Handing a PR over". The running queue may not be in a worktree named
   `merge-queue`, so find it before you start another one.

If the user wants you to make a small change yourself, you still work in a worktree:
- `orca worktree create --repo id:<repoId> --name <task> --no-parent --json`
- edit through its absolute paths
- push, open a PR, hand the PR to the queue

## Worker

If `spawn_worker.py` started you, your rules are in the `common.md` next to your brief. Follow
that file, not this one.

## Merge queue

If you are the queue, or you're about to merge or deploy anything, read
`references/merge-queue.md` first.

## Cleanup

```
python3 scripts/worktrees.py cleanup                 # every worktree with a reason; removes nothing
python3 scripts/worktrees.py cleanup --apply a,b     # removes only the named ones that are still candidates
```
A worktree is a candidate only if all of these hold:
- its PR is merged, or it has no commits and has been idle for a while
- its tree is clean, read from git rather than Orca's cache
- no agent is busy in it
- its comment doesn't say `BLOCKED`
- it isn't on the keep list (the queue's worktree, `keep_worktrees`, `$ORCA_FLOW_KEEP`)

Never remove a worktree that has uncommitted changes.

**Terminals:**
- **Leftovers:** finished Setup tabs and never-used shells can be closed with
  `orca terminal close --terminal <h> --json`, once `orca terminal read` shows nothing is running.
- **Ask first:** worktree removals and terminal closes follow the same rule. Show one list of
  both, each with its reason, and wait for the user's OK. The exception is when the user has
  already named what to remove. A request to "look at what's idle" asks you to look; it doesn't
  approve closing anything. A closed terminal loses its scrollback, which may be the only record
  of what an agent did.
- **Waiting agents:** agent sessions waiting for the user are not idle. List them for the user
  instead.
- **Secrets:** if a terminal shows secret values, name that terminal to the user and suggest
  closing it and rotating the keys it shows. Never copy the values into files or replies.

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
- **Updating the main checkout:** `git fetch`, then `git merge --ff-only <base branch>`, and only
  when it has no local commits. Otherwise leave it and tell the user.
- **Orca flags:** if a flag is unclear, run `orca <command> --help` or `orca skills get orca-cli`.
  Don't guess. Check `ok` in every `--json` response before reading `result`.
