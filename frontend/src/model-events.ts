import { textValue } from './event-data'
import type { TraceEvent } from './state'

/** 展示层只消费统一文本事件，原始供应商帧留在日志中。 */
export function modelTextDelta(event: TraceEvent): string {
  return event.type === 'model.delta' ? textValue(event.payload.content) : ''
}
