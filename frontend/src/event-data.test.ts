import { describe, expect, it } from 'vitest'
import { evidenceValues, messageValue, numberValue } from './event-data'
import { modelTextDelta } from './model-events'
import type { TraceEvent } from './state'

describe('动态事件投影边界', () => {
  it('保留证据扩展，过滤无引用 ID 的记录', () => {
    expect(evidenceValues([null, { id: 7 }, { id: 'doc:1', title: '规则', score: 0.9 }])).toEqual([
      { id: 'doc:1', title: '规则', score: 0.9 },
    ])
  })
  it('不将非法消息和用量当作有效显示数据', () => {
    expect(messageValue(['invalid'])).toBeUndefined()
    expect(numberValue('12')).toBeUndefined()
    expect(numberValue(Infinity)).toBeUndefined()
  })
  it('原始供应商帧不作为展示文本', () => {
    const event: TraceEvent = {
      schema_version: 1,
      event_id: 'event-1',
      session_id: 'session-1',
      seq: 1,
      run_id: 'run-1',
      call_id: 'call-1',
      parent_call_id: null,
      timestamp: '2026-09-15T00:00:00Z',
      type: 'model.chunk',
      payload: { kind: 'wire', data: '{"choices":[{"delta":{"content":42}}]}' },
    }
    expect(modelTextDelta(event)).toBe('')
  })
})
