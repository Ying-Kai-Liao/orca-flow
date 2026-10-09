// One agent of this session as the board shows it: the roster row from
// $.agent.list(), plus what the mod saw it do.
export type AgentRow = {
  id: string
  name?: string
  description: string
  type: string
  status: string
  parentId?: string
}

export type Activity = {
  startedAt: number
  lastAt: number
  // The newest last, at most LOG_MAX lines: "Edit src/app.ts", "Bash npm test".
  log: string[]
  // The last turn's final text: a worker's report, or the question it ended on.
  answer?: string
}

declare module 'claude-code' {
  interface PluginState {
    'flow-board': {
      roster: AgentRow[]
      activity: Record<string, Activity>
      selected: string | null
      now: number
    }
  }
}
