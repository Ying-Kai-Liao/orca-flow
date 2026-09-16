# Merge queue

One session at a time owns merging, the full check, deploying and the status file. Two sessions
doing this together overwrite each other's deployed version, and each also runs the full suite,
which is what exhausts the machine.

The queue works from a clean worktree, by default one named `merge-queue` that holds no work of
its own: resetting it loses nothing, and it stays clean, so a check that only stamps a clean tree
will stamp it and a deploy script that refuses a dirty tree will accept it. The main checkout
usually doesn't qualify, because the user's stray files keep it dirty.

**Everything project-specific — the full check, deploy targets, backups, health URLs, the status
file — comes from the config.** Read it first and use exactly what it says:
```
python3 scripts/config.py show
```
If a step isn't configured, say so instead of inventing a command. Deploying with a guessed
command is worse than not deploying.

Contents: finding the running queue · handing a PR over · starting a queue · the loop ·
deploying · reporting back

## Finding the running queue (always do this first)

The queue is recognised by what it is doing, not by where it runs. It has run as an ordinary
session in the main checkout, with no `merge-queue` worktree existing at all. Check all three
sources:

1. **ListAgents.** Look for a session whose name or summary mentions merge, deploy, queue,
   合併 or 部署.
2. **`orca worktree ps --json`.** Check every worktree's `comment` and `preview`, and each
   agent's `prompt`, for "merge queue", "merge and deploy", 合併佇列, 部署.
3. **`orca terminal list --json`.** Look for terminal titles such as "merge-queue".

If you find a queue, even one busy with another batch, hand the PR to it. It adds the PR to its
current batch or runs it next. Start a new queue only if none of the three sources shows one.

## Handing a PR over (managers)

Send this as a single line: a newline can submit the text early. Fields are separated by ` | `.
```
[merge-queue] PR #<n> <branch> head <full sha> | migration: <none | file> | decisions pending: <none | list> | worker verified: <checks> ✅ <test files>, full suite not run | after deploy check: <none | what> | report to: <your session name from ListAgents>
```
- **`head`** is the commit you reviewed, taken from `gh pr view <n> --json headRefOid`. The queue
  merges that commit and nothing newer.
- **Sending:** use SendMessage to the queue's session. If the queue has no session you can
  message, use `orca terminal send --terminal <its handle> --text "<line>" --enter --wait-submit 15 --json`.
- **After handing over,** leave the PR's branch alone: no pushes, no rebases. If the worker needs
  another push, send the queue a new `head`.
- **If no queue is running:** the user saying "merge and deploy" is enough to start one (next
  section). Tell the user you did.

## Starting a queue

```
orca worktree create --repo id:<repoId> --name merge-queue --no-parent --setup run --json        # skip if it exists
orca terminal create --worktree path:<repo worktrees dir>/merge-queue --title merge-queue --command "claude --model opus [--permission-mode bypassPermissions]" --json
orca terminal wait --terminal <h> --for tui-idle --timeout-ms 120000 --json      # send only if satisfied is true
orca terminal send --terminal <h> --text "You are this repo's merge queue. Use the orca-flow skill, read references/merge-queue.md and follow it; wait for managers to send you PRs." --enter --wait-submit 15 --json
```
- **Permission mode:** add `--permission-mode bypassPermissions` only if the manager starting the
  queue runs in bypass mode itself. The queue runs deploy commands, and a permission prompt
  nobody sees will stall it.

## The loop (queue)

Work in batches. A batch is every PR that arrived while the previous batch was deploying. Merge
the PRs one at a time, then run the full check and deploy once for the whole batch.

### 1. Start clean

```
git status --porcelain                 # must be empty; if not, find out what it is before touching it
git fetch origin && git reset --hard <base branch>
```
If the project has a migrations directory (`worker.migrations_dir`), record which duplicate
numbers already exist, so an old duplicate that's already deployed doesn't look like a new clash:
```
ls <migrations dir> | grep -oE '^[0-9]+' | sort | uniq -d > /tmp/dup-before.txt
```

### 2. Merge each PR in arrival order

```
gh pr view <n> --json state,headRefOid,title,files
```
- **Before merging:** the PR's `state` must be `OPEN`, and `headRefOid` must equal the `head` the
  manager sent. If the head changed, ask the manager rather than merging unreviewed commits.

```
git merge --no-ff <head sha> -m "Merge PR #<n>: <title>"
ls <migrations dir> | grep -oE '^[0-9]+' | sort | uniq -d | diff /tmp/dup-before.txt - && echo "no new duplicate"
```
- **Migrations:** read any new migration file and confirm it can run twice (`IF NOT EXISTS` /
  `ON CONFLICT`), because the whole directory gets replayed.
- **Duplicate migration number:** renumber it in a follow-up commit only if that number hasn't
  run anywhere yet. Tell the manager, and also the owner of any open PR using the same number.
- **Mechanical conflicts:** resolve them here and keep both sides — two PRs adding rules next to
  each other in the same stylesheet, say.
- **Conflicts that need a logic or product decision:** `git merge --abort`, send the PR back to
  its manager with the file names, and go on to the next PR.

### 3. Verify once for the batch

```
bash <skill-dir>/scripts/test-lock.sh <worker.full_check_command>      # run_in_background; takes minutes
```
- **A failure caused by combining PRs** (one PR's test doesn't know about another PR's new
  field): fix it in a follow-up commit, then run the check again.
- **A real bug in one PR:** take that PR out, then run the check again:
  ```
  git log --first-parent --oneline <base branch>..HEAD      # find "Merge PR #<bad>"
  git reset --hard <that merge commit>^1
  ```
  Then redo the merges that came after it, plus any follow-up fixes that don't depend on the
  removed PR. Send the removed PR back with the failing test output.
- **Stamps:** if the project's check stamps a verified commit, it stamps only a clean tree. Any
  new commit means running it again.
- **Test count:** quote it from the run that passed.

### 4. Push

`git push origin HEAD:<base branch name>`. GitHub marks each PR merged, because its commits are
now on the base branch.
- **Why not `gh pr merge`:** it creates a different merge commit from the one you just verified.
- **If the push is rejected:** fetch, merge the base branch, run the check again, push again.

### 5. Deploy

See the next section.

### 6. Status file

If `merge_queue.state_file` is set, update its top section with:
- what merged, using each PR description's one-line status
- the commit
- where it's deployed
- the open decisions collected from the PR descriptions

Commit and push. A status-file-only commit doesn't need a redeploy. Workers never edit it.

### 7. Report back

Report to each manager (last section), then run
`orca worktree set --worktree active --comment "<PRs> deployed <short sha>" --json`.

## Deploying

Deploy exactly the commit you verified. Run each target in `merge_queue.targets` in the order
they're listed; each one carries its own commands:

- **`backup`** — run these first when the batch contains a migration, and check the result before
  going on. A backup command that produced an empty or unreadable file is a stop sign, not a
  warning.
- **`deploy`** — the deploy commands themselves. If a target deploys through a CI run, confirm
  the run you're watching is for *your* commit (`headSha`); other sessions start runs too.
- **`health_url`** — fetch it afterwards. The commit it reports must equal
  `git rev-parse --short HEAD` of what you deployed.
- **`verify`** — any extra post-deploy checks, plus whatever the managers listed under "after
  deploy check".

A target with no `deploy` commands configured is not a target you know how to deploy: tell the
user instead of improvising.

## Reporting back

Send this with SendMessage to the session named in "report to", as one line:
```
[merge-queue] PR #<n> merged and deployed <short sha> | <target> health ✅ | <target> health ✅ | full check: <N> tests passed | migration: <none | file, backed up to <dump>> | decisions pending: <none | list>
```
- **If that session is gone:** put the same line on the PR as a comment (`gh pr comment <n>`), and
  on the worker's card (`orca worktree set --worktree path:<worker path> --comment ... --json`).
- **If a PR was sent back without merging:** say why and what needs to change.
