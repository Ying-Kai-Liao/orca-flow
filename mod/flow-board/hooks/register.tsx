import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Activity, AgentRow } from '../types'

// orca-flow without Orca. Workers are background subagents of this session, each in a git
// worktree of its own (the `flow-board:worker` agent type). The pane in main lists every
// agent the session runs; pressing one shows what it is doing, and its report or question.

const PANE = 'flow'
const POLL_MS = 3000
const LOG_MAX = 40
const ENDED = new Set(['completed', 'failed', 'killed'])
// Display order: what may need a person first, finished agents last.
const ORDER = ['waiting', 'idle', 'running', 'pending', 'failed', 'killed', 'completed']
const GLYPH: Record<string, string> = {
  pending: '○', running: '●', waiting: '◐', idle: '◌', completed: '✓', failed: '✗', killed: '■',
}
const COLOR: Record<string, string> = {
  running: 'cyan', waiting: 'yellow', idle: 'yellow', completed: 'green', failed: 'red', killed: 'red',
}

const roster = atom({ plugin: 'flow-board', key: 'roster' } as const, [] as AgentRow[])
const activity = atom({ plugin: 'flow-board', key: 'activity' } as const, {} as Record<string, Activity>)
const selected = atom({ plugin: 'flow-board', key: 'selected' } as const, null as string | null)
const now = atom({ plugin: 'flow-board', key: 'now' } as const, 0)

const WORKER_PROMPT = `You are an orca-flow worker. You run in a git worktree of your own, on a branch of your own, cut from the repo's default branch. Your task brief is your first message.

How to work:
- Stay in your worktree. Never edit the main checkout or another worktree.
- Read the brief, then the code it touches, before changing anything. Do exactly what the brief asks; note anything else you find instead of fixing it.
- Run the checks the brief names (tests, lint, type-check). If it names none, run the repo's usual test command for the files you changed.
- Commit with a clear message. If the repo has a remote and \`gh\` works, push your branch and open a pull request; otherwise leave the commits on your branch.
- If you need a decision you cannot make from the brief or the code, stop and end your final message with the question, on its own line ending in "?". Your manager will answer by message and you will continue.

Your final message is your report to the manager: the branch, the PR link if any, what changed, which checks ran and their result, and anything left undone or blocked. Keep it short.`

// One line for a tool call: the tool and its most telling argument.
function describeCall(e: Record<string, unknown>): string {
  const tool = String(e.tool ?? '?')
  const arg = [e.file_path, e.command, e.pattern, e.path, e.url, e.description, e.prompt]
    .find(v => typeof v === 'string' && v.length > 0) as string | undefined
  const short = arg === undefined ? '' : ' ' + arg.replace(/\s+/g, ' ').slice(0, 90)
  return tool + short
}

function ago(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000))
  if (s < 60) return `${s}s`
  const m = Math.round(s / 60)
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h${m % 60}m`
}

function labelOf(a: AgentRow): string {
  return a.name ?? a.description ?? a.id.slice(0, 8)
}

function asksQuestion(answer: string | undefined): boolean {
  const last = (answer ?? '').trim().split('\n').pop() ?? ''
  return /[?？][*_`'")\s]*$/.test(last)
}

function sorted(list: AgentRow[]): AgentRow[] {
  const rank = (s: string) => {
    const i = ORDER.indexOf(s)
    return i === -1 ? ORDER.length : i
  }
  return [...list].sort((a, b) => rank(a.status) - rank(b.status))
}

// Reads the roster from the engine, tells the person when an agent finishes or stops on a
// question, and sets the status line. `now` is written every time so the ages redraw.
async function refresh($: EngineInterface): Promise<void> {
  const [list, t, before, acts] = await Promise.all([
    $.agent.list(), $.clock.now(), read($, roster), read($, activity),
  ])
  const rows: AgentRow[] = list.map(a => ({
    id: a.id, name: a.name, description: a.description, type: a.type,
    status: a.status, parentId: a.parentId,
  }))
  const was = new Map(before.map(a => [a.id, a.status]))
  for (const a of rows) {
    const prev = was.get(a.id)
    if (prev === undefined || prev === a.status || ENDED.has(prev)) continue
    if (ENDED.has(a.status) || a.status === 'idle') {
      const asks = asksQuestion(acts[a.id]?.answer)
      void $.ui.toast(`${labelOf(a)}: ${asks ? 'asks a question' : a.status === 'idle' ? 'finished its turn' : a.status}`)
    }
  }
  if (JSON.stringify(rows) !== JSON.stringify(before)) await update($, roster, () => rows)
  await update($, now, () => t)

  const live = rows.filter(a => !ENDED.has(a.status))
  const done = rows.length - live.length
  $.ui.status(rows.length === 0 ? undefined
    : `flow: ${live.length} live${done ? ` · ${done} ended` : ''} · /flow`)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'flow',
      description: 'Show every worker of this session in a pane: status, activity, reports',
    })
    await $.agent.register({
      name: 'worker',
      description:
        'An orca-flow worker: implements one task brief in a git worktree of its own and opens a PR. ' +
        'Pass the whole brief as the prompt and a short task slug as the name. Use for coding tasks to run in parallel.',
      prompt: WORKER_PROMPT,
      isolation: 'worktree',
      background: true,
    })

    // The roster is the engine's ($.agent.list); the mod polls it.
    $.clock.every(POLL_MS, () => void refresh($))

    return next(e)
  })

  on('command.run', { command: 'flow' }, async $ => {
    await $.ui.open({ id: PANE, title: 'Flow', focus: true })
    return { text: 'Flow pane opened.' }
  })

  on('agent.spawn', async ($, e, next) => {
    const started = await next(e)
    if (started.agentId !== undefined) {
      const t = await $.clock.now()
      const id = started.agentId
      await update($, activity, acts => ({
        ...acts, [id]: { startedAt: t, lastAt: t, log: [`started: ${e.description}`] },
      }))
      await refresh($)
      void $.ui.open({ id: PANE, title: 'Flow' })
    }
    return started
  }).catch(($, e, next) => next(e))

  // A subagent's tool calls carry its agentId: keep the latest few as its activity log.
  on('tool.call', async ($, e, next) => {
    const id = e.agentId
    if (id !== undefined) {
      const t = await $.clock.now()
      const line = describeCall(e as unknown as Record<string, unknown>)
      await update($, activity, acts => {
        const a = acts[id] ?? { startedAt: t, lastAt: t, log: [] }
        return { ...acts, [id]: { ...a, lastAt: t, log: [...a.log, line].slice(-LOG_MAX) } }
      })
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  on('turn.complete', async ($, e, next) => {
    const id = e.agentId
    if (id !== undefined) {
      const t = await $.clock.now()
      await update($, activity, acts => {
        const a = acts[id] ?? { startedAt: t, lastAt: t, log: [] }
        return { ...acts, [id]: { ...a, lastAt: t, answer: e.answer } }
      })
      await refresh($)
    }
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const [list, acts, pick, t] = await Promise.all([
      read($, roster), read($, activity), read($, selected), read($, now),
    ])
    const rows = e.viewport?.rows ?? 24
    const agent = list.find(a => a.id === pick)

    if (agent !== undefined) {
      const act = acts[agent.id]
      const answer = (act?.answer ?? '').trim()
      const room = Math.max(3, rows - 12)
      return (
        <Box flexDirection="column">
          <Box flexDirection="row" gap={1}>
            <Button key="back" hotkey="b" onPress={() => update($, selected, () => null)}>Back</Button>
            <Button key="msg" hotkey="m" onPress={() => $.prompt.fill({
              text: `Send a message to the worker "${labelOf(agent)}": `, mode: 'replace',
            })}>Message</Button>
          </Box>
          <Text bold color={COLOR[agent.status]}>
            {GLYPH[agent.status] ?? '?'} {labelOf(agent)} <Text dimColor>{agent.type} · {agent.status}
            {act ? ` · started ${ago(t - act.startedAt)} ago` : ''}</Text>
          </Text>
          <Text dimColor>{agent.description}</Text>
          <Text bold>Activity</Text>
          {(act?.log ?? []).length === 0 && <Text dimColor>Nothing seen yet.</Text>}
          {(act?.log ?? []).slice(-room).map(line => <Text wrap="truncate-end">{line}</Text>)}
          {answer !== '' && <Text bold color={asksQuestion(answer) ? 'yellow' : undefined}>
            {asksQuestion(answer) ? 'Asks' : 'Last report'}
          </Text>}
          {answer !== '' && <Text>{answer.length > 1200 ? '…' + answer.slice(-1200) : answer}</Text>}
        </Box>
      )
    }

    const live = list.filter(a => !ENDED.has(a.status)).length
    return (
      <Box flexDirection="column">
        <Text dimColor>{list.length} agents · {live} live · press one to see it</Text>
        {list.length === 0 && <Text dimColor>No workers yet. Ask Claude to start one, e.g. "start a worker to fix X".</Text>}
        {sorted(list).slice(0, Math.max(1, rows - 3)).map(a => {
          const act = acts[a.id]
          const last = asksQuestion(act?.answer) && !['running', 'pending'].includes(a.status)
            ? 'asks: ' + (act?.answer ?? '').trim().split('\n').pop()
            : act?.log[act.log.length - 1] ?? ''
          return (
            <Button key={a.id} dimColor={ENDED.has(a.status)} onPress={() => update($, selected, () => a.id)}>
              <Text color={COLOR[a.status]}>{GLYPH[a.status] ?? '?'}</Text> {labelOf(a)}
              <Text dimColor> {act ? ago(t - act.lastAt) : ''} {last.slice(0, 80)}</Text>
            </Button>
          )
        })}
      </Box>
    )
  })
}
