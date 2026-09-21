import type { TraceEvent } from './state'
import { projectRunTrace } from './trace'

export type RunGroup = { events: TraceEvent[]; entries: ReturnType<typeof projectRunTrace> }

/** 每个订阅拥有一个索引；追加事件只重算受影响的 Run，重放或切换会话时重建。 */
export function createTraceIndex() {
  let previous: TraceEvent[] = []
  let groups = new Map<string, RunGroup>()
  return (events: TraceEvent[]) => {
    const append =
      previous.length <= events.length && previous.every((event, i) => event === events[i])
    const next = append ? new Map(groups) : new Map<string, RunGroup>()
    const changed = new Map<string, TraceEvent[]>()
    for (const event of events.slice(append ? previous.length : 0)) {
      if (!event.run_id) continue
      let bucket = changed.get(event.run_id)
      if (!bucket) {
        bucket = [...(next.get(event.run_id)?.events ?? [])]
        changed.set(event.run_id, bucket)
      }
      bucket.push(event)
    }
    for (const [id, items] of changed)
      next.set(id, { events: items, entries: projectRunTrace(items) })
    previous = events
    groups = next
    return next
  }
}
