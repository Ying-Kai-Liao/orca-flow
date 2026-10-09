# flow-board

orca-flow without Orca. Workers are background subagents of the main Claude Code session, each
in a git worktree of its own, started through the `flow-board:worker` agent type. The manager
stays in the main session and watches them from a pane.

## What you get

- **The Flow pane**: one row per worker with its status. Open it with `/flow`.
- **Click a row** to see that worker's activity (its tool calls) and its final report.
- **Message** starts a message to the selected worker in your prompt; **Back** returns to the list.
- **A status line** with the count of live and ended workers.
- **Toasts** when a worker finishes its turn, asks a question or changes status.

## Requirements

Claude Code 2.1.287 or later (for mods).

## Try it

```bash
claude --plugin-dir mod/flow-board
```

## Check it

```bash
claude plugin validate mod/flow-board
claude plugin test mod/flow-board
```

## Limits

- Workers live only as long as the main session. Close it and they stop.
- The pane shows tool calls and reports, not the full transcript.
- Orca and tmux backends are planned, not built yet.
