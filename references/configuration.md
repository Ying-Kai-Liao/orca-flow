# Configuration

Everything project-specific lives in one JSON file. The flow works without it; the parts that
need to know your commands simply say "not configured" instead of guessing.

**Where it's looked for**, first match wins:
1. `$ORCA_FLOW_CONFIG`
2. `<repo>/.claude/orca-flow.json`
3. `<repo>/orca-flow.json`
4. `<git-common-dir>/orca-flow/config.json` — untracked, for values you don't want in the repo

Start one with `python3 scripts/config.py init`, check what resolved with
`python3 scripts/config.py show`, and read a single value with
`python3 scripts/config.py get worker.test_command`.

The repo is found from the skill's own location, falling back to the current directory when the
skill is installed outside a repo (`~/.claude/skills/orca-flow`).

## Keys

| Key | Default | What it does |
|---|---|---|
| `project` | repo directory name | Name used in worker rules. |
| `language` | `"English"` | The language briefs, commit messages and code comments are written in. Match the codebase, not the chat. |
| `base_branch` | `"origin/main"` | What worktrees branch from, what "ahead" and overlap are measured against, what the queue pushes to. |
| `keep_worktrees` | `[]` | Worktrees cleanup never proposes removing. The merge queue's worktree is always kept; `$ORCA_FLOW_KEEP` adds more. |
| `worker.model` | `"opus"` | Model for spawned workers. `spawn_worker.py --model` overrides it; `--agent-command` replaces the whole command for a non-Claude agent. |
| `worker.rules_file` | `assets/common.md` | Your own worker rules, if the default ones don't fit. Same placeholders (see below). |
| `worker.checks` | `[]` | Cheap checks every worker runs before opening a PR, e.g. `["npx tsc --noEmit", "npm run lint"]`. |
| `worker.test_command` | none | How to run a few test files. `{files}` is replaced with what the worker picked. |
| `worker.test_slots` | `1` | How many test runs may go at once on this machine. `$ORCA_FLOW_TEST_SLOTS` overrides. |
| `worker.full_check_command` | none | The whole suite. Workers are told not to run it; the queue runs it once per batch. |
| `worker.migrations_dir` | none | Directory of numbered migrations. Enables clash detection in `worktrees.py overlap` and the queue's duplicate check. |
| `worker.run_command` | none | How to start the app, for looking at a UI change before claiming it works. |
| `worker.extra_rules` | `[]` | Extra bullets appended to the worker rules. Project-specific traps go here. |
| `merge_queue.worktree_name` | `"merge-queue"` | The clean worktree the queue works from. |
| `merge_queue.state_file` | none | A hand-written status file only the queue may edit (e.g. `NOW.md`). Workers are told to leave it alone, and the main-checkout guard allows it. |
| `merge_queue.targets` | `[]` | Deploy targets, in order (below). |
| `main_checkout.guard` | `true` | Whether the PreToolUse hook blocks edits to the main checkout. |
| `main_checkout.allow_files` | `[]` | Files still editable there. `state_file` is added automatically. |
| `main_checkout.allow_prefixes` | `[".claude/"]` | Path prefixes still editable there. |

### Deploy targets

```json
"targets": [
  {
    "name": "staging",
    "deploy": ["gh workflow run deploy.yml -f target=staging"],
    "health_url": "https://staging.example.com/health",
    "verify": ["check that the admin page still loads"]
  },
  {
    "name": "production",
    "backup": ["ssh deploy@prod 'pg_dump -Fc app > /backups/pre-$(date +%Y%m%dT%H%M%SZ).dump'"],
    "deploy": ["bash scripts/deploy.sh production"],
    "health_url": "https://app.example.com/health",
    "verify": []
  }
]
```
- `backup` runs before deploying when the batch contains a migration, and its result is checked
  before going further.
- `health_url` should report the deployed commit, so the queue can compare it with what it pushed.
- `verify` is free text: extra checks a human would do.

Keep secrets out of this file if it's committed — ssh key paths and hostnames are fine, tokens
are not. For values you'd rather not commit, use `<git-common-dir>/orca-flow/config.json`, which
is inside `.git/` and therefore never committed or visible to `git status`.

## Placeholders in the worker rules

`spawn_worker.py` renders `assets/common.md` (or your `worker.rules_file`) and copies the result
next to the brief. Available placeholders:

| Placeholder | Becomes |
|---|---|
| `{{PROJECT}}`, `{{LANGUAGE}}`, `{{BASE}}` | config values; `{{PR_BASE}}` is the base branch without its remote prefix |
| `{{TEST_LOCK}}` | absolute path to the copied `test-lock.sh` |
| `{{CHECKS_RULE}}`, `{{TEST_RULE}}`, `{{FULL_CHECK_RULE}}`, `{{RUN_RULE}}` | rendered verification rules, or a sensible fallback when the command isn't configured |
| `{{MIGRATION_RULE}}` | migration rules, or nothing if there's no migrations directory |
| `{{STATE_FILE_RULE}}`, `{{STATE_FILE_INLINE}}` | "don't touch the status file", or nothing |
| `{{EXTRA_RULES}}` | `worker.extra_rules`, one bullet each |

Check the result before spawning ten workers with it:
```
python3 scripts/spawn_worker.py --name probe --brief some-brief.md --dry-run
```
