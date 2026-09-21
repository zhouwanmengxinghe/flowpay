/* 前端回归测试：验证 state 的状态或交互；模拟事件用于复现后端协议。 */
import { createTraceIndex } from './trace-index'
import { describe, expect, it } from 'vitest'
import { chatMessages, liveSegments, liveText, liveTools, mergeEvents, statusOf } from './state'
import type { TraceEvent } from './state'

const event = (
  seq: number,
  type: string,
  payload: Record<string, any> = {},
  call = 'model_1',
): TraceEvent => ({
  seq,
  type,
  payload,
  schema_version: 1,
  event_id: `evt_${seq}`,
  session_id: 'session_1',
  run_id: 'run_1',
  call_id: call,
  parent_call_id: null,
  timestamp: '2026-09-09T00:00:00Z',
})

describe('durable event projections', () => {
  it('deduplicates reconnect replay and preserves event ordering', () => {
    const original = [event(1, 'run.started'), event(2, 'model.request')]
    expect(mergeEvents(original, [event(3, 'model.chunk'), original[1]]).map((e) => e.seq)).toEqual(
      [1, 2, 3],
    )
  })
  it('restores waiting state and hides chunks after authoritative answer commit', () => {
    const events = [
      event(1, 'run.started'),
      event(2, 'model.request'),
      event(3, 'model.delta', { content: '请补充' }),
    ]
    expect(liveText(events)).toBe('请补充')
    const completed = [
      ...events,
      event(4, 'message.assistant', { content: '请补充' }),
      event(5, 'run.finished', { status: 'waiting_user' }),
    ]
    expect(statusOf(completed)).toBe('waiting_user')
    expect(liveText(completed)).toBe('')
  })
  it('retains interrupted partial output without claiming completion', () => {
    const events = [
      event(1, 'run.started'),
      event(2, 'model.request'),
      event(3, 'model.delta', { content: '部分' }),
      event(4, 'run.interrupted'),
    ]
    expect(statusOf(events)).toBe('interrupted')
    expect(liveText(events)).toBe('部分')
  })
})

it('uses normalized text once while retaining original wire events', () => {
  const events = [
    event(1, 'run.started'),
    event(2, 'model.request', {}),
    event(3, 'model.chunk', {
      kind: 'wire',
      data: JSON.stringify({ choices: [{ delta: { content: '回答' } }] }),
    }),
    event(4, 'model.delta', { content: '回答' }),
  ]
  expect(liveText(events)).toBe('回答')
  expect(liveText([...events, event(5, 'message.assistant', { content: '回答' })])).toBe('')
})

it('reuses completed Run projections and rebuilds on history replacement', () => {
  const index = createTraceIndex()
  const first = [event(1, 'run.started'), event(2, 'message.user', { content: '一' })]
  const original = index(first).get('run_1')
  const next = [...first, { ...event(3, 'run.started'), run_id: 'run_2' }]
  expect(index(next).get('run_1')).toBe(original)
  const replaced = [{ ...first[0], run_id: 'other' }]
  expect([...index(replaced).keys()]).toEqual(['other'])
})

it('keeps internal assessment streams out of chat and resumes streaming the final answer', () => {
  const events = [
    event(1, 'run.started'),
    event(2, 'model.request', {}),
    event(3, 'model.delta', { content: '先搜索' }),
    event(4, 'model.request', { output: 'internal' }, 'assessment'),
    event(5, 'model.delta', { content: '{"quality":"partial"}' }, 'assessment'),
  ]
  expect(liveText(events)).toBe('')
  expect(liveText([...events, event(6, 'run.canceled')])).toBe('')
  expect(
    liveText([
      ...events,
      event(6, 'model.request', { output: 'answer' }, 'final'),
      event(7, 'model.delta', { content: '文档说明' }, 'final'),
    ]),
  ).toBe('文档说明')
})

it('ignores retired answer events and raw supplier chunks', () => {
  const events = [
    event(1, 'run.started'),
    event(2, 'model.request', { output: 'answer' }),
    event(3, 'model.chunk', { kind: 'content_delta', text: 'retired' }),
    event(4, 'answer.delta', { content: 'retired' }),
  ]
  expect(liveText(events)).toBe('')
})

it('hides a rejected Markdown draft until the model produces a new response', () => {
  const events = [
    event(1, 'run.started'),
    event(2, 'model.request', { output: 'answer' }, 'draft'),
    event(3, 'model.delta', { content: '未完成的正文' }, 'draft'),
    event(4, 'answer.rejected', { message: '回答未完整结束' }, 'draft'),
  ]
  expect(liveText(events)).toBe('')
  expect(liveText([...events, event(5, 'run.failed')])).toBe('')
  expect(
    liveText([
      ...events,
      event(5, 'model.request', { output: 'answer' }, 'repair'),
      event(6, 'model.delta', { content: '当前资料无法确认。' }, 'repair'),
    ]),
  ).toBe('当前资料无法确认。')
})

it('按轮汇总本轮资料，检索命中与整篇读回都算在内', () => {
  const messages = chatMessages([
    event(1, 'knowledge.response', {
      results: [
        { id: 'doc:A', title: 'A' },
        { id: 'doc:B', title: 'B' },
      ],
    }),
    event(2, 'document.response', { passages: [{ id: 'doc:C#c001' }, { id: 'doc:C#c002' }] }),
    event(3, 'message.assistant', {
      content: '回答',
      evidence: [{ id: 'doc:A' }, { id: 'doc:B' }, { id: 'doc:C#c001' }, { id: 'doc:C#c002' }],
    }),
  ])
  expect(messages[0].payload.available.map((item) => item.id)).toEqual([
    'doc:A',
    'doc:B',
    'doc:C#c001',
    'doc:C#c002',
  ])
})

it('追问没有新检索时，往轮片段仍列在本轮资料里', () => {
  const messages = chatMessages([
    { ...event(1, 'knowledge.response', { results: [{ id: 'doc:A' }] }), run_id: 'run_1' },
    {
      ...event(2, 'message.assistant', { content: '答一', evidence: [{ id: 'doc:A' }] }),
      run_id: 'run_1',
    },
    { ...event(3, 'knowledge.response', { results: [{ id: 'doc:C' }] }), run_id: 'run_2' },
    {
      ...event(4, 'message.assistant', {
        content: '追问答',
        evidence: [{ id: 'doc:A' }, { id: 'doc:C' }],
      }),
      run_id: 'run_2',
    },
  ])
  expect(messages[0].payload.available.map((item) => item.id)).toEqual(['doc:A'])
  expect(messages[1].payload.available.map((item) => item.id)).toEqual(['doc:C', 'doc:A'])
})

it('用户消息不带资料清单', () => {
  const messages = chatMessages([
    event(1, 'message.user', { content: '问题' }),
    event(2, 'knowledge.response', { results: [{ id: 'doc:A' }] }),
    event(3, 'message.assistant', { content: '回答', evidence: [{ id: 'doc:A' }] }),
  ])
  expect(messages[0].payload.available).toEqual([])
  expect(messages[1].payload.available.map((item) => item.id)).toEqual(['doc:A'])
})

it('按轮汇总 token 用量，内部判断调用单独计数', () => {
  // usage 形状取自 DeepSeek 的真实响应，含它自己的缓存命中字段。
  const response = (callId: string, prompt: number, completion: number, cached = 0) => ({
    ...event(0, 'model.response', {
      completion: {
        usage: {
          prompt_tokens: prompt,
          completion_tokens: completion,
          total_tokens: prompt + completion,
          prompt_tokens_details: { cached_tokens: cached },
        },
      },
    }),
    call_id: callId,
  })
  const messages = chatMessages([
    { ...event(1, 'model.request', { output: 'internal' }), call_id: 'route' },
    response('route', 140, 1),
    { ...event(3, 'model.request', { output: 'answer' }), call_id: 'answer' },
    event(4, 'embedding.response', { usage: { prompt_tokens: 38, total_tokens: 38 } }),
    response('answer', 808, 245, 640),
    event(6, 'message.assistant', { content: '回答', evidence: [] }),
  ])
  expect(messages[0].payload.usage).toEqual({
    promptTokens: 948,
    completionTokens: 246,
    cachedTokens: 640,
    modelCalls: 2,
    internalCalls: 1,
    embeddingTokens: 38,
    embeddingCalls: 1,
  })
})

it('用户消息不带用量，跨轮用量不互相污染', () => {
  const usageEvent = (seq: number, runId: string, prompt: number) => ({
    ...event(seq, 'model.response', {
      completion: { usage: { prompt_tokens: prompt, completion_tokens: 1 } },
    }),
    run_id: runId,
  })
  const messages = chatMessages([
    { ...event(1, 'message.user', { content: '问题一' }), run_id: 'run_1' },
    usageEvent(2, 'run_1', 100),
    { ...event(3, 'message.assistant', { content: '答一', evidence: [] }), run_id: 'run_1' },
    usageEvent(4, 'run_2', 500),
    { ...event(5, 'message.assistant', { content: '答二', evidence: [] }), run_id: 'run_2' },
  ])
  expect(messages[0].payload.usage).toBeUndefined()
  expect(messages[1].payload.usage?.promptTokens).toBe(100)
  expect(messages[2].payload.usage?.promptTokens).toBe(500)
})

describe('progress during the gap between model calls', () => {
  // 模型每调一次工具就重开一次回答，liveText 随之清空；这里保证空窗期仍能说清进度。
  const investigating = [
    event(1, 'run.started'),
    event(2, 'model.request', {}, 'model_1'),
    event(3, 'model.delta', { content: '我先查一下支付记录' }, 'model_1'),
    event(4, 'tool.started', { name: 'query_payments' }, 'call_a'),
    event(5, 'tool.started', { name: 'query_events' }, 'call_b'),
  ]

  it('names the tools still running while the answer text is empty', () => {
    expect(liveTools(investigating)).toEqual({
      active: ['query_payments', 'query_events'],
      finished: [],
    })
  })

  it('reports finished tools once the next model call wipes the streamed text', () => {
    const events = [
      ...investigating,
      event(6, 'tool.result', { name: 'query_payments' }, 'call_a'),
      event(7, 'tool.result', { name: 'query_events' }, 'call_b'),
      event(8, 'model.request', {}, 'model_2'),
    ]
    // 新的模型调用一到，上一段正文就不再展示——这正是"字突然消失"的时刻。
    expect(liveText(events)).toBe('')
    expect(liveTools(events)).toEqual({
      active: [],
      finished: ['query_payments', 'query_events'],
    })
  })

  it('does not repeat a tool that was called several times', () => {
    const events = [
      ...investigating,
      event(6, 'tool.started', { name: 'query_payments' }, 'call_c'),
    ]
    expect(liveTools(events).active).toEqual(['query_payments', 'query_events'])
  })
})

describe('text streamed before each tool call', () => {
  // 模型每调一次工具就重开一次回答。早先说过的话不该从页面上消失，
  // 但也不能和最终回答连成一段——那会读成一个虚假的完整答案。
  const events = [
    event(1, 'run.started'),
    event(2, 'model.request', {}, 'model_1'),
    event(3, 'model.delta', { content: '我先查一下支付记录' }, 'model_1'),
    event(4, 'tool.started', { name: 'query_payments' }, 'call_a'),
    event(5, 'tool.result', { rows: [] }, 'call_a'),
    event(6, 'model.request', {}, 'model_2'),
  ]

  it('keeps the earlier text as a step instead of dropping it', () => {
    expect(liveSegments(events)).toEqual({ steps: ['我先查一下支付记录'], current: '' })
  })

  it('separates finished steps from the answer being written now', () => {
    const writing = [...events, event(7, 'model.delta', { content: '两个查询都为空' }, 'model_2')]
    expect(liveSegments(writing)).toEqual({
      steps: ['我先查一下支付记录'],
      current: '两个查询都为空',
    })
    // liveText 仍然只给正在写的那一段，滚动与流式光标的行为不变。
    expect(liveText(writing)).toBe('两个查询都为空')
  })

  it('drops everything once the answer is stored', () => {
    const answered = [...events, event(7, 'message.assistant', { content: '结论' }, 'model_2')]
    expect(liveSegments(answered)).toEqual({ steps: [], current: '' })
  })

  it('leaves a rejected draft out of the chat area', () => {
    const rejected = [
      ...events,
      event(7, 'model.delta', { content: '截断的草稿' }, 'model_2'),
      event(8, 'answer.rejected', { message: 'truncated' }, 'model_2'),
    ]
    expect(liveSegments(rejected)).toEqual({ steps: ['我先查一下支付记录'], current: '' })
  })
})
