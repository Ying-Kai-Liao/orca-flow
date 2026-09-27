# Task sources

A task source is where tasks come from: an issue tracker, a board, a planning doc. orca-flow
doesn't talk to any of them itself. A source is a **markdown doc in the project** (a project
skill's `SKILL.md`, say) that tells the dispatcher and the managers how to use it, with whatever
tools that project has (an MCP server, `gh`, a CLI).

**Registering one:** add its path, relative to the repo, under `sources` in the config:
```
python3 scripts/config.py set sources '{"asana": ".claude/skills/asana-task/SKILL.md"}'
```
The name (`asana`) is what `spawn_manager.py --source` takes and what `manager.json` records.

## What the doc must contain

Three sections, under these headings:

- **List** — how to list candidate tasks (the exact call or command), the default filter (which
  states, which assignee, what's never picked automatically), and what the stable `source_id` is:
  the id that stays the same for the task's lifetime and is passed as `--source-id`.
- **Render** — how to turn one task into a brief: which fields to fetch (description, comments,
  subtasks, due date, link); the task's own text is **copied verbatim**, never rewritten; the
  dispatcher's judgement goes in a separate section of the brief; attachments are downloaded to
  local paths and the brief names those paths (managers pass images on to workers with
  `--attach`).
- **Write-backs** — every write to the source (comments, status moves, completion,
  reassignment) is shown to the user first and waits for their OK; the source is usually shared
  with other people. List which write-backs a manager is expected to propose, and when (a
  question to the task's author, a comment when the PR is handed over, a status move after
  deploy), and which it never makes (usually: marking the task complete).

Anything else project-specific (ids, section names, the user's account) goes in the same doc.

## Skeleton

```markdown
# Tracker tasks (orca-flow source "tracker")

Project id 1234; the user is account 42. Reading needs no OK; every write does.

## List
`tracker tasks --project 1234 --json` (page with `--offset`).
Default: open tasks in "To do" and "Bug" assigned to the user. Never pick "Inbox" or tasks
assigned to someone else. `source_id` = the task's numeric id.

## Render
Fetch `tracker task <id> --json` and `tracker comments <id>`. The brief gets: title, link,
state, due date, the description and comments verbatim (who, when, what), subtasks, and local
paths of attachments downloaded with `tracker download <id> --to <scratchpad>`.
Put your own reading of the task under "Dispatcher's notes".

## Write-backs (show the user, write after OK)
- Questions for the author: a comment, drafted by the manager.
- PR handed over: a comment with the PR link; move "To do" → "In progress".
- Deployed: a comment with how to check it; move to "Testing".
- Never mark a task complete; the user does that after acceptance.
```

The first real source is `asana-task` in peace-guardian: its `SKILL.md` lists tasks from one
Asana project's sections, renders a task with its comments and attachments, and asks the user
before every Asana write.
