# Configuration

Everything project-specific lives in one JSON file. The flow works without it; the parts that
need to know your commands simply say "not configured" instead of guessing.

**Where it's looked for**, first match wins:
1. `$ORCA_FLOW_CONFIG`
2. `<repo>/.claude/orca-flow.json`
3. `<repo>/orca-flow.json`
4. `<git-common-dir>/orca-flow/config.json` — untracked, for values you don't want in the repo

The local file (4) is also laid over whichever of 1-3 was found, key by key, so it can hold
just the few values that are personal or secret while the rest stays in the committed file.

In a repo that has never used the skill, run `python3 scripts/init.py` (below). Otherwise
start one with `python3 scripts/config.py init`.

## Changing settings: `config.py`

```
config.py show [--json]            # the resolved config, defaults filled in
config.py keys [--json]            # every key: current value, default, one-line description; * = set in a config file
config.py get <key>                # one value; scalars print bare, for shell use
config.py path                     # the files that were read, in order
config.py set <key> <value> [--local] [--append] [--force] [--dry-run]
config.py unset <key> [--local] [--dry-run]
config.py check                    # type-check the files; exit 1 on errors
```

- **`set`** parses the value by the key's type: `true`/`false` (also yes/no, on/off),
  numbers, JSON for lists and objects, `null` to clear. Strings need no quoting beyond the
  shell's. It writes the file already in use, or `<repo>/orca-flow.json` if there is none,
  and prints which file changed.
- **`--local`** writes `<git-common-dir>/orca-flow/config.json` instead. Use it for personal
  preferences (model, context limit, bypass) and for anything that mustn't be committed.
  When a repo-file `set` is hidden by the local file, `set` says so.
- **`--append`** adds one item to a list key: `set worker.checks "npm run lint" --append`.
- **Unknown keys and wrong types are refused**, with a suggestion for a near miss, so a typo
  can't silently do nothing. `--force` writes them anyway.
- **`unset`** removes the key from that file, so the default (or the other file) applies again.
- Changes apply to what starts afterwards. A running worker keeps the rules it was started
  with; `spawn_worker.py --continue` re-renders them.

Examples:
```
python3 scripts/config.py set handoff.enabled false                 # never wrap workers up for context
python3 scripts/config.py set worker.context_window 1000000 --local # you run 1M-context models
python3 scripts/config.py set worker.context_warn 400000 --local    # flag at 400k tokens instead of a fraction
python3 scripts/config.py set board.stale_min 60
python3 scripts/config.py set cleanup.idle_hours 12
python3 scripts/config.py unset worker.model --local
```

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
| `worker.context_window` | `200000` | Window the context estimate is measured against (`worktrees.py context`). Raise it for 1M-context models. |
| `worker.context_warn` | `0.35` | Where a worker is flagged `!`: a fraction of the window (`<= 1`, e.g. `0.35`) or an absolute token count (`> 1`, e.g. `70000`). With handoff on, the manager then wraps it up and `--continue`s it. |
| `worker.transcripts_dir` | `~/.claude/projects` | Where Claude Code writes session transcripts, if not the default (`$CLAUDE_CONFIG_DIR` is honoured). |
| `worker.bypass_permissions` | `false` | Start workers with `bypassPermissions` without passing `--bypass` (`--no-bypass` overrides). Only for a manager that runs that way itself. |
| `handoff.enabled` | `true` | Whether flagged workers are wrapped up and continued in a fresh session. `false`: they run to the end on the agent's own compaction, the worker rules drop the wrap-up section, and `--continue` is only for a worker that died. |
| `handoff.digest` | `true` | Whether `--continue` writes `handoff-digest.md` from the old session's transcript. |
| `handoff.wrap_up_message` | `"WRAP UP: commit WIP, push, …"` | The one line the manager sends a flagged worker. The worker rules quote the part before the colon, so keep a short prefix like `WRAP UP:`. |
| `handoff.max_continues` | `2` | After this many `--continue`s of one package, `spawn_worker.py` adds a `warning`: the package is too big, split it. |
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
| `board.stale_min` | `30` | Minutes without an update before a working pane shows as `stale`. `board.py --stale-min` overrides. |
| `board.question_max_min` | `240` | Minutes after which an unanswered question in an agent's message is demoted to `done`. |
| `board.decision_phrases` | `[]` | Extra phrases (lowercase) that mark a last paragraph as asking the user to decide, added to the built-in English ones. For agents that write in another language. |
| `board.negations` | `[]` | Extra negations (lowercase) that cancel a decision phrase right after them. |
| `cleanup.idle_hours` | `3` | Idle hours before a worktree with no commits becomes a cleanup candidate. `worktrees.py cleanup --idle-hours` overrides. |

## First run: `init.py`

```
python3 scripts/init.py [--repo <path>] [--test-command "…"] [--full-check "…"] [--language …]
                        [--model …] [--no-queue] [--force-config] [--dry-run] [--json]
```
Idempotent; one line per step, each `ok`, `skipped (already …)` or `would (dry-run)`:
1. resolves the repo root (`--repo`, else `$ORCA_FLOW_REPO`, else the current directory;
   never the skill's own repo) and base branch (`origin/HEAD` if the remote has one, else the
   current branch);
2. registers the repo with Orca (`orca repo add`) if `orca repo list` doesn't have it. A dry
   run makes no orca call;
3. writes `<repo>/orca-flow.json` if no config file exists anywhere in the lookup order.
   The test commands are guessed only when there's one obvious answer: `package.json` with a
   `test` script → `npm test -- {files}` / full check `npm test`; `pyproject.toml` or `tests/`
   → `python3 -m unittest {files}` / full check `python3 -m unittest discover -s tests`.
   Otherwise both stay null. An existing file is never touched unless `--force-config`, which
   prints a diff and rewrites it: values already in the file win over detected defaults, flags
   win over both, and unknown keys are kept. (So `--force-config` never turns a queue back on;
   edit `merge_queue.enabled` by hand for that.) An existing file that isn't valid JSON is
   reported as `failed (invalid JSON)` and left alone, with or without `--force-config`.
4. creates `<git-common-dir>/orca-flow/{briefs,queue,bin}`;
5. prints `next:` with the values still null (fill them with `config.py set`) and whether the repo runs with or without a queue.

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
| `{{HANDOFF_RULE}}` | the "Handing off and continuing" section (quoting `handoff.wrap_up_message`), or just the part about being a continued session when `handoff.enabled` is false |

Check the result before spawning ten workers with it:
```
python3 scripts/spawn_worker.py --name probe --brief some-brief.md --dry-run
```
