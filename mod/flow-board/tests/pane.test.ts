import type { AgentInfo } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

const PANE = { component: 'Pane', props: { title: 'Flow' } as never, requestId: 'flow' } as const

const AGENTS: AgentInfo[] = [
  { id: 'w1', name: 'fix-login', description: 'Fix the login bug', type: 'flow-board:worker', status: 'running' },
  { id: 'w2', name: 'docs', description: 'Update docs', type: 'flow-board:worker', status: 'completed' },
]

test('the pane lists workers, shows what one did, and goes back', async ($, on) => {
  mock.clock(on, { now: 1_000_000 })
  on('agent.list', () => ({ value: AGENTS }))
  on('agent.spawn', ($, e) => ({ model: 'sonnet', agentId: e.description === 'Update docs' ? 'w2' : 'w1' }))
  on('tool.call', () => ({ result: 'ok' }))
  on('turn.complete', ($, e) => ({ text: e.answer }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))

  await $.agent.spawn({ prompt: 'brief 1', description: 'Fix the login bug', subagentType: 'flow-board:worker' } as never)
  await $.agent.spawn({ prompt: 'brief 2', description: 'Update docs', subagentType: 'flow-board:worker' } as never)
  await $.tool.call({ tool: 'Edit', file_path: 'src/login.ts', agentId: 'w1' } as never)
  await $.tool.call({ tool: 'Read', file_path: 'MAIN-ONLY.md' } as never)
  await $.turn.complete({ answer: 'Done. PR #7 is open.', agentId: 'w2', durationMs: 1, isAborted: false, turnId: 't', reason: 'answer' } as never)

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'flow-board', surface, ...PANE })
    expect(await ui.find({ type: 'Text', text: /2 agents · 1 live/ })).toBeDefined()
    expect(await ui.find({ text: /Edit src\/login\.ts/ })).toBeDefined()
    expect(await ui.find({ text: /MAIN-ONLY/ })).toBeUndefined()

    await ui.press({ key: 'w2' })
    expect(await ui.find({ type: 'Text', text: /PR #7 is open/ })).toBeDefined()

    await ui.press({ key: 'back' })
    expect(await ui.find({ type: 'Text', text: /2 agents · 1 live/ })).toBeDefined()
    await ui.unmount()
  }
})
