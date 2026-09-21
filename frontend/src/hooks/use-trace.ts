import { objectValue, numberValue } from '../event-data'
import { useMemo } from 'react'
import { statusOf, type TraceEvent } from '@/state'
import { traceCounts, type TraceEntry } from '@/trace'
import { createTraceIndex } from '@/trace-index'

export type RunView = {
  id: string
  number: number
  startedAt: string
  startEvent: TraceEvent
  endEvent?: TraceEvent
  status: string
  resumed: boolean
  entries: TraceEntry[]
}

// 事件解释集中于投影层；组件不需要判断终态事件或假设索引项一定存在。
export function useTrace(events: TraceEvent[], runFilter: string) {
  const index = useMemo(createTraceIndex, [])
  const groups = useMemo(() => index(events), [index, events])
  return useMemo(() => {
    const runs: RunView[] = []
    for (const event of events) {
      if (event.type !== 'run.started' || !event.run_id) continue
      const group = groups.get(event.run_id)
      if (!group) continue
      runs.push({
        id: event.run_id,
        number: runs.length + 1,
        startedAt: event.timestamp,
        startEvent: event,
        resumed: event.payload.resumed === true,
        endEvent: group.events.findLast((item) =>
          ['run.finished', 'run.failed', 'run.canceled', 'run.interrupted'].includes(item.type),
        ),
        status: statusOf(group.events),
        entries: group.entries,
      })
    }
    const visibleRuns = runs.filter((run) => runFilter === 'all' || run.id === runFilter)
    const visibleEvents = runFilter === 'all' ? events : (groups.get(runFilter)?.events ?? [])
    const traceEntries = visibleRuns.flatMap((run) => run.entries)
    const counts = traceCounts(traceEntries)
    const tokens = visibleEvents
      .filter((event) => event.type === 'model.response')
      .reduce(
        (sum, event) =>
          sum +
          (numberValue(objectValue(objectValue(event.payload.completion).usage).total_tokens) ??
            numberValue(objectValue(event.payload.usage).total_tokens) ??
            0),
        0,
      )
    return { runs, visibleRuns, visibleEvents, traceEntries, counts, tokens }
  }, [events, groups, runFilter])
}
export type TraceProjection = ReturnType<typeof useTrace>
