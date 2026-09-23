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

**The handover is a file, not a message.** `<git-common-dir>/orca-flow/queue/<pr>.json`,
written by `handover.py`. Any queue session reads the same directory, so it doesn't matter which
session is the queue today, whether it was restarted, or what it's called. A chat message is a
courtesy on top; the file is the record.

Contents: finding the running queue · handing a PR over · starting a queue · the loop ·
rotating the queue · deploying · heavy verification · reporting back

## Finding the running queue (managers, always first)

```
python3 scripts/handover.py queue show        # queue/state.json: the registered session, since when, batches done
```
If `active` is set, a queue is registered: hand the PR over (next section) and, if you want it
picked up now rather than at the queue's next look, `--notify` its terminal handle. If `active`
is null, check the old way before starting one, because a queue may be running unregistered:
ListAgents (a session whose name or summary mentions merge, deploy or queue), `orca worktree ps
--json` (comments and prompts), `orca terminal list --json` (a terminal titled merge-queue). If
you find one, tell it to run `handover.py queue start`. Start a new queue only if none exists.

## Handing a PR over (managers)

```
python3 scripts/handover.py send <pr> --pending "<none | decisions the user still has to make>" --verified "<what the worker ran, and that the full suite was not>" --after-deploy "<none | what to check>" --note "<one line for the status file>" --report-to <your session name, from ListAgents read just now> [--notify <queue terminal handle>]
```
- The script copies the **head** from `gh pr view`, so the queue merges the commit that exists,
  not one typed from memory. Review at that head: `gh pr view <n> --json headRefOid` and read the
  diff at that commit, not the one you looked at two pushes ago.
- It refuses drafts and closed PRs, marks migration files (new, or modified old ones), and prints
  the one-line message. Sending that line by SendMessage too is fine; the file is what counts.
- **After handing over,** leave the PR's branch alone: no pushes, no rebases. If the worker has to
  push again, run `send` again; the file records the new head and keeps the old one in history.
- **Waiting for the result:** `handover.py status <pr>` prints `pending`, `taken`, `done <sha>`,
  `returned — <reason>` or `unhanded`. A Monitor until-loop can wait on it.
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
- **The queue's first command** is `python3 scripts/handover.py queue start --session <its name from ListAgents>`.
  Until then managers see no registered queue and may start another.

## The loop (queue)

Work in batches. A batch is every PR that arrived while the previous batch was deploying. Merge
the PRs one at a time, then run the full check and deploy once for the whole batch.

```
python3 scripts/handover.py list --check      # pending handovers in arrival order; --check asks GitHub whether each head is still the PR's head
```
Take each PR you're about to merge (`handover.py take <pr>`), so a second queue session or a
manager can see it's in progress. When `list` says ROTATE, finish the batch, then see "Rotating
the queue".

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
- **Before merging:** the PR's `state` must be `OPEN`, and `headRefOid` must equal the `head` in
  the handover file. If the head moved, `handover.py back <pr> --reason "head moved"` and tell the
  manager, rather than merging unreviewed commits.

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

Keep it short: when it has more than `merge_queue.state_file_keep` entries, run
`python3 scripts/archive_status.py --dry-run`, then without `--dry-run`, and commit the result
with the status update. Everyone reads this file from the top; nobody needs a month of history in
it.

### 7. Close the handovers and report back

For each PR in the batch:
```
python3 scripts/handover.py done <pr> --sha <short sha> --report "<target> health ✅ | full check: N tests passed | backup: <dump or none>"
```
(`--no-count` on all but one PR of a multi-PR batch, so the batch counter counts batches;
`--no-deploy` when the batch was pushed but not deployed, so the report doesn't claim a deploy.) It
prints the report line; also send it by SendMessage to the session named in the file if that
session still exists. For a PR you didn't merge: `handover.py back <pr> --reason "..."`. Then
`orca worktree set --worktree active --comment "<PRs> deployed <short sha>" --json`.

## Rotating the queue

A queue session's context grows by a few thousand tokens per batch, mostly from its own commands
and reports, so after `merge_queue.rotate_after` batches it is near the ceiling. That is normal.
When `handover.py list` says ROTATE and the current batch is deployed:

1. `python3 scripts/handover.py queue retire --reason "rotation after N batches"`
2. Set the card: `orca worktree set --worktree active --comment "queue retired; start a new one" --json`
3. Tell the user (or the manager who is around) in one line, and stop taking handovers.

The next queue starts as in "Starting a queue", registers itself, and runs `handover.py list`.
Nothing is lost: pending handovers are files, the deployed state is in the status file, and
the retired session never has to be consulted. Don't hand a PR to a retired session, and don't
keep one alive "just in case".

## Heavy verification

A real end-to-end run against a deployed environment costs far more than the merge itself. The
queue never runs one on its own initiative. Do it when the handover's "after deploy check" asks
for it, and the project's `merge_queue.heavy_verification` says when that's worth it (by default:
a migration or an outbound side effect such as mail, push notifications or third-party calls).
Everything else gets the health check and the cheap "after deploy check" the manager listed.

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

`handover.py done` / `back` write the result into the handover file, which is what the manager
polls. On top of that, send the printed line with SendMessage to the session named in the file,
as one line:
```
[merge-queue] PR #<n> merged and deployed <short sha> | <target> health ✅ | <target> health ✅ | full check: <N> tests passed | migration: <none | file, backed up to <dump>> | decisions pending: <none | list>
```
- **If that session is gone:** the file already has it; also put the line on the PR as a comment
  (`gh pr comment <n>`) so it's visible from GitHub.
- **If a PR was sent back without merging:** say why and what needs to change.
