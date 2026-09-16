# orca-flow

A Claude Code skill for running several coding agents in parallel on one repository, through
[Orca](https://orca.computer) worktrees, without them stepping on each other.

Three roles, one owner per shared resource:

| Role | Where it works | Owns | Never does |
|---|---|---|---|
| **Manager** | the main checkout | briefs, starting workers, reviewing PRs | editing feature code, merging, deploying |
| **Worker** | its own worktree | one package, targeted tests, one PR | merging, deploying, running the full suite, starting other workers |
| **Merge queue** | a clean worktree of its own | merging, the full check, deploying, the status file | building features |

That division is the whole point. Parallel agents don't collide over code nearly as often as
they collide over *shared* things: two sessions merging into the same branch, two deploys racing
to the same server, six worktrees running a database-backed test suite at once on a 16 GB laptop,
three PRs all rewriting the same status file. Each of those has exactly one owner here.

## What's in it

```
SKILL.md                      the flow itself (this is what the model reads)
references/merge-queue.md     merging, verifying and deploying a batch
references/configuration.md   every config key
assets/common.md              worker rules, rendered per project
assets/brief-template.md      what a brief has to contain
scripts/spawn_worker.py       create worktree → start agent → wait for its TUI → send the brief once
scripts/worktrees.py          inventory · status · overlap · cleanup
scripts/test-lock.sh          flock-based queue so N test runs share the machine
scripts/main_checkout_guard.py PreToolUse hook keeping the main checkout on its base branch
scripts/config.py             config loader (show / get / init)
examples/                     ready-made configs
evals/                        eval suite for the skill
```

## Requirements

- the Orca app and its `orca` CLI, with your repo added
- Claude Code (or another agent you can start with a shell command — see `--agent-command`)
- `git`, `gh` (authenticated), `python3`, `bash`

## Install

As a user-level skill, available in every project:

```bash
git clone https://github.com/Ying-Kai-Liao/orca-flow.git ~/.claude/skills/orca-flow
```

Or per project, where you can pin a version:

```bash
git submodule add https://github.com/Ying-Kai-Liao/orca-flow.git .claude/skills/orca-flow
```

Then tell it about the project:

```bash
python3 ~/.claude/skills/orca-flow/scripts/config.py init   # writes <repo>/.claude/orca-flow.json
python3 ~/.claude/skills/orca-flow/scripts/config.py show   # what actually resolved
```

Fill in how your project is tested, checked and deployed. Every key is optional and documented in
[`references/configuration.md`](references/configuration.md); `examples/` has three filled-in
configs. What isn't configured is treated as unknown, not guessed.

### The main-checkout guard (optional, recommended)

A PreToolUse hook that blocks Edit/Write in the main checkout, so changes really do happen in
worktrees. In `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|NotebookEdit",
        "hooks": [
          { "type": "command", "command": "python3 /absolute/path/to/orca-flow/scripts/main_checkout_guard.py" }
        ]
      }
    ]
  }
}
```

It allows `.claude/` and your status file by default, and anything you add to
`main_checkout.allow_files` / `allow_prefixes`. To turn it off without touching settings, set
`main_checkout.guard` to `false`. The user's own escape hatch is
`touch <repo>/.git/orca-flow/allow-main-edits` — which the hook refuses to let an agent create,
because a refusal message that explains how to bypass itself isn't a refusal.

## Using it

Ask for parallel work in plain language ("do these two tickets in worktrees", "merge and deploy",
"what are the workers doing?", "what worktrees can be cleaned up?"). The skill's own description
handles triggering. Under the hood:

```bash
# start a worker on a brief you wrote
python3 scripts/spawn_worker.py --name csv-export --brief /tmp/csv-export.md --attach shot.png
python3 scripts/spawn_worker.py --name csv-export --brief /tmp/csv-export.md --dry-run   # see everything first

# who's doing what
python3 scripts/worktrees.py inventory
python3 scripts/worktrees.py status csv-export          # in-review / blocked / in-progress / missing

# will these two packages collide?
python3 scripts/worktrees.py overlap src/admin.js sql/

# what can be cleaned up (removes nothing until you name names)
python3 scripts/worktrees.py cleanup
python3 scripts/worktrees.py cleanup --apply csv-export,old-spike

# share the machine between test runs
bash scripts/test-lock.sh npx vitest run src/foo.test.ts
bash scripts/test-lock.sh --status
```

Anything that changes state takes `--dry-run`, or `ORCA_FLOW_DRY_RUN=1`.

## Design notes

Most of the rules here are scar tissue, and the scripts say so in their comments:

- **Check `ok` before reading `result`.** An `orca` command that fails has no `result` key at all;
  parsing it by hand produced `KeyError: 'result'` in three different managers.
- **Never send a prompt before the TUI is idle.** It's silently swallowed. `spawn_worker.py` only
  sends on `satisfied: true`, and never re-sends after a failed send — the prompt may have landed.
- **One line per message.** Text sent to a TUI can submit at the first newline, truncating the
  rest of your review comments.
- **Read HEAD from git, not from Orca's cache**, which lags behind the branch.
- **flock, not a mkdir lock.** A mkdir lock has to decide whether the holder died, and two waiters
  deciding at once delete the slot one of them just took — exactly when everything is queued
  after an OOM.
- **Shared state in `<repo>/.git/orca-flow/`.** Visible from every worktree, invisible to
  `git status`, so a clean-tree check still passes.
- **Judge a worktree by its PR, not by its branch.** A worktree sitting on the base branch with an
  open PR is waiting for review, not idle.
- **Ask before removing anything.** "Show me what's idle" is a request to look.

## License

MIT
