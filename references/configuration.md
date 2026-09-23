# Configuration

Everything project-specific lives in one JSON file. The flow works without it; the parts that
need to know your commands simply say "not configured" instead of guessing.

**Where it's looked for**, first match wins:
1. `$ORCA_FLOW_CONFIG`
2. `<repo>/.claude/orca-flow.json`
3. `<repo>/orca-flow.json`
4. `<git-common-dir>/orca-flow/config.json` — untracked, for values you don't want in the repo

In a repo that has never used the skill, run `python3 scripts/init.py` (below). Otherwise
start one with `python3 scripts/config.py init`, check what resolved with
`python3 scripts/config.py show`, and read a single value with
`python3 scripts/config.py get worker.test_command`.

**Which repo is "the repo":** when the skill lives inside a project (`.claude/skills/orca-flow`),
its own location decides, so the caller's cwd doesn't matter. When it's installed standalone — a
clone in `~/.claude/skills/`, which is a git repo of its own — that would resolve to the skill's
repository, so the caller's current directory decides instead. A cwd inside a linked worktree
resolves to the main checkout, because worktrees share one git common dir. `$ORCA_FLOW_REPO`
overrides both.

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
| `worker.big_files` | `[]` | Files workers must never read whole; briefs give entry points with line ranges for them. |
| `worker.big_file_lines` | `1500` | Above this many lines any file counts as big. |
| `worker.context_window` | `200000` | Window the context estimate is measured against (`worktrees.py context`). |
| `worker.context_warn` | `0.35` | Fraction of the window at which a worker is flagged and the manager wraps it up and `--continue`s it. |
| `worker.transcripts_dir` | `~/.claude/projects` | Where Claude Code writes session transcripts, if not the default (`$CLAUDE_CONFIG_DIR` is honoured). |
| `merge_queue.enabled` | `true` | `false` for a repo with no queue session: the manager reviews and merges PRs itself, `handover.py send` refuses (unless `--force`), and `worktrees.py inventory` / `board.py` don't report open PRs as `unhanded_pr`. Only an explicit `false` turns it off. |
| `merge_queue.merge_method` | `"squash"` | With no queue, how the manager merges: `gh pr merge <pr> --<method>` (`squash`, `merge` or `rebase`). |
| `merge_queue.worktree_name` | `"merge-queue"` | The clean worktree the queue works from. |
| `merge_queue.state_file` | none | A hand-written status file only the queue may edit (e.g. `NOW.md`). Workers are told to leave it alone, and the main-checkout guard allows it. |
| `merge_queue.targets` | `[]` | Deploy targets, in order (below). |
| `merge_queue.rotate_after` | `10` | Batches after which `handover.py list` tells the queue to retire and a fresh session takes over. |
| `merge_queue.state_file_keep` | `10` | Entries `archive_status.py` keeps in the status file; older ones move to the archive. |
| `merge_queue.archive_file` | `<stem>-archive.md` | Where archived status entries go, next to the status file. |
| `merge_queue.heavy_verification` | migrations or outbound side effects, on request | When an expensive end-to-end check after deploy is worth running. The queue never runs one unasked. |
| `main_checkout.guard` | `true` | Whether the PreToolUse hook blocks edits to the main checkout. |
| `main_checkout.allow_files` | `[]` | Files still editable there. `state_file` is added automatically. |
| `main_checkout.allow_prefixes` | `[".claude/"]` | Path prefixes still editable there. |

## First run: `init.py`

```
python3 scripts/init.py [--repo <path>] [--test-command "…"] [--full-check "…"] [--language …]
                        [--model …] [--no-queue] [--force-config] [--dry-run] [--json]
```
Idempotent; one line per step, each `ok`, `skipped (already …)` or `would (dry-run)`:
1. resolves the repo root and base branch (`origin/HEAD` if the remote has one, else the
   current branch);
2. registers the repo with Orca (`orca repo add`) if `orca repo list` doesn't have it;
3. writes `<repo>/orca-flow.json` if no config file exists anywhere in the lookup order.
   `test_command` is guessed only when there's one obvious answer (`package.json` with a
   `test` script → `npm test`; `pyproject.toml` or `tests/` → `python3 -m unittest discover -s
   tests`), else left null. An existing file is never touched unless `--force-config`, which
   prints a diff and rewrites it: values already in the file win over detected defaults, flags
   win over both, and unknown keys are kept. (So `--force-config` never turns a queue back on;
   edit `merge_queue.enabled` by hand for that.)
4. creates `<git-common-dir>/orca-flow/{briefs,queue,bin}`;
5. prints `next:` with the values still null and whether the repo runs with or without a queue.

Claude Code's "Quick safety check" trust is per exact directory, and every worktree is a new
one, so `init.py` doesn't set it: `spawn_worker.py` marks each new worktree path as trusted in
`~/.claude.json` (`$CLAUDE_CONFIG_DIR/.claude.json` if set) before starting the agent, backing
the file up once per run to `~/.claude.json.orca-flow.bak`.

### A small repo with no queue

```json
{
  "language": "English",
  "base_branch": "origin/main",
  "worker": {
    "model": "opus",
    "test_command": "python3 -m unittest {files}",
    "full_check_command": "python3 -m unittest discover -s tests"
  },
  "merge_queue": { "enabled": false, "merge_method": "squash" }
}
```
The manager hands no PRs over. For each approved PR it runs the full check from a clean
worktree on the PR's head, then `gh pr merge <pr> --squash`.

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
| `{{BIG_FILE_RULE}}` | the "don't read big files whole" rule, naming `worker.big_files` |
| `{{STATE_FILE_RULE}}`, `{{STATE_FILE_INLINE}}` | "don't touch the status file", or nothing |
| `{{EXTRA_RULES}}` | `worker.extra_rules`, one bullet each |

Check the result before spawning ten workers with it:
```
python3 scripts/spawn_worker.py --name probe --brief some-brief.md --dry-run
```
