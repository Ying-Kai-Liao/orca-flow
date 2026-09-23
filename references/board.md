# Board: attention state over Orca's live agents

`scripts/board.py` prints one row per agent pane across every Orca worktree on the local
host (all repos, not just this one), grouped by repo, with the rows that need a person first.
A worktree with no agent pane still gets one row, so a `BLOCKED` card or an unhanded PR shows
up even when no session is open on it.

```
python3 scripts/board.py                    # the board
python3 scripts/board.py --json             # same rows as JSON: {"at", "rows", "notes"}
python3 scripts/board.py --repo peace-guardian
python3 scripts/board.py --stale-min 45     # working with no update for > 45 min is stale (default 30)
python3 scripts/board.py --watch [--interval 20]   # prints the board once, then only rows whose attention changed
python3 scripts/board.py --write            # also writes <git-common-dir>/orca-flow/board.json of this repo
python3 scripts/board.py --no-gh            # skip gh; PRs come only from Orca's linked PR
```

It reads `orca worktree ps --json`, `orca repo list --json`, `gh pr list` (one call per repo
that has a non-base branch checked out) and the handover files in
`<git-common-dir>/orca-flow/queue/<pr>.json`. It never writes to Orca and never scrapes
terminal text. `--watch` polls (Orca has no event stream) and only prints; it doesn't notify.
Ctrl-C exits cleanly. `--write` replaces board.json atomically (tmp file + rename).

## The classifier

`scripts/board_rules.py` holds `classify(row, stale_min=30) -> (attention, reason)`. It is pure:
a plain dict in, two strings out, no Orca, gh, git, file or clock access. The separate
`jev-handoff` project imports it and can replace it with a scored model of the same signature.
`worktrees.py inventory` uses it too (its "waiting on the user" and "open PRs with no handover
file" sections), so there is one rule set.

Rules, first match wins:

| # | attention | fires when |
|---|-----------|------------|
| 1 | `needs_human` | Orca state is `waiting` or `permission` |
| 2 | `blocked` | card comment starts with `BLOCKED` (any case) |
| 3 | `handoff` | card comment starts with `HANDOFF` (what `worktrees.py status` prints as `handoff`) |
| 4 | `needs_human` | agent not working, and its last message ends with `?`/`？` (ignoring trailing markup), or its **last paragraph** contains a decision request: 拍板, 請確認, 需要你決定, 你決定, "should I", "which do you want", "which one do you want", "do you want me to", "please/can you/could you confirm", "your call", "let me know which" |
| 5 | `unhanded_pr` | PR open, not a draft, no handover file, agent not working |
| 6 | `stale` | state `working` and `minutes_since_update` > `stale_min` |
| 7 | `working` | state `working` |
| 8 | `done` | state `done` |
| 9 | `idle` | state `idle`, or no agent pane |
| 10 | `unknown` | any other state |

Rule 4 is narrow on purpose: the merge queue and workers end with status reports, and a
report that ends with a period must stay `done`. A bare "confirm" doesn't count ("I confirmed
…" and "confirm the timer didn't start" are both reports). An unreadable handover file (status
`?`) counts as handed over, as it does in `worktrees.py inventory`.

Display order: `needs_human, blocked, unhanded_pr, stale, handoff, unknown, working, done,
idle` (`board_rules.PRIORITY`). Repos are ordered by their most urgent row; inside a repo,
by urgency, then longest in state first.

## Row schema

Built by `board_rules.make_row(ps_worktree, ps_agent_or_None, now_ms, pr=None, handover=None)`.
All values are plain JSON. `classify` reads only `state`, `comment`, `pr`, `handover`,
`minutes_since_update`, `last_message_tail` (falls back to `last_message`) and `pane`; missing
keys are treated as empty.

| key | type | from |
|-----|------|------|
| `host` | str | ps `hostId` (only `local` rows are shown) |
| `repo`, `repo_id` | str | ps `repo`, `repoId` |
| `worktree` | str | last path component of ps `path` |
| `worktree_id`, `path` | str | ps `worktreeId`, `path` |
| `branch` | str \| null | ps `branch` without `refs/heads/` |
| `is_main` | bool | ps `isMainWorktree` |
| `column` | str | ps `workspaceStatus` (the board column) |
| `comment` | str | ps `comment` (the card comment) |
| `unread` | bool | ps `unread` |
| `pr` | {number, state, isDraft, title} \| null | `gh pr list` by branch (newest PR for it); else Orca's `linkedPR` with `isDraft: null`; `state` is upper-case (`OPEN`/`MERGED`/`CLOSED`) |
| `handover` | str \| null | `status` of `queue/<pr>.json`, `?` if unreadable, null if no file |
| `pane` | str \| null | agent `paneKey`; null for a worktree with no agent |
| `agent_type`, `state`, `working_mode`, `tool` | str \| null | agent `agentType`, `state`, `workingMode`, `toolName` |
| `prompt` | str | agent `prompt`, first 200 chars |
| `minutes_in_state` | float \| null | now − agent `stateStartedAt` |
| `minutes_since_update` | float \| null | now − agent `updatedAt` |
| `last_message` | str | agent `lastAssistantMessage`, first 200 chars |
| `last_message_tail` | str | the same message, last 2000 chars (the question is usually at the end) |
| `attention`, `reason` | str | `classify`, added by board.py |

board.json is `{"at": "<UTC ISO>", "rows": [...], "notes": [...]}`; `notes` carries warnings
such as a failed `gh` call or a truncated `ps` page.
