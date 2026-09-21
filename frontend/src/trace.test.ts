/* 前端回归测试：轨迹要与 AI 交互流程一致——每次调用发了什么、回了什么，都看得到。 */
import { describe, expect, it } from 'vitest'
import { projectRunTrace, traceCounts } from './trace'
import type { TraceEvent } from './state'

const event = (
  seq: number,
  type: string,
  payload: Record<string, any>,
  call_id: string | null = null,
): TraceEvent => ({
  seq,
  type,
  payload,
  call_id,
  schema_version: 1,
  event_id: `evt_${seq}`,
  run_id: 'run_1',
  session_id: 'session_1',
  parent_call_id: null,
  timestamp: '2026-09-09T00:00:00Z',
})

const request = (messages: unknown[], tools: string[] = []) => ({
  request: {
    body: {
      model: 'fixture-model',
      messages,
      tools: tools.map((name) => ({ type: 'function', function: { name } })),
    },
  },
})

const response = (message: Record<string, unknown>, usage?: Record<string, unknown>) => ({
  completion: {
    choices: [{ message, finish_reason: message.tool_calls ? 'tool_calls' : 'stop' }],
    usage,
  },
  duration_ms: 120,
})

describe('模型调用按请求与响应两段投影', () => {
  it('请求侧保留真正发出去的消息与工具，不只显示回复', () => {
    const [call] = projectRunTrace([
      event(1, 'run.started', {}),
      event(
        2,
        'model.request',
        request(
          [
            { role: 'system', content: '你是助手' },
            { role: 'user', content: '端点为什么被停用' },
          ],
          ['search_documents', 'read_document'],
        ),
        'model_1',
      ),
      event(3, 'model.response', response({ role: 'assistant', content: '按规则说明' }), 'model_1'),
    ])
    expect(call.kind).toBe('call')
    expect(call.request?.messages).toHaveLength(2)
    expect(call.request?.tools).toEqual(['search_documents', 'read_document'])
    expect(call.response?.message?.content).toBe('按规则说明')
    expect(call.response?.finishReason).toBe('stop')
  })

  it('第二次调用只把新增消息算作本次增量，沿用部分折叠', () => {
    const first = [
      { role: 'system', content: '你是助手' },
      { role: 'user', content: '端点为什么被停用' },
    ]
    const entries = projectRunTrace([
      event(1, 'run.started', {}),
      event(2, 'model.request', request(first), 'model_1'),
      event(3, 'model.response', response({ role: 'assistant', content: '先查文档' }), 'model_1'),
      event(
        4,
        'model.request',
        request([
          ...first,
          { role: 'assistant', content: '先查文档' },
          { role: 'tool', content: '{}' },
        ]),
        'model_2',
      ),
      event(5, 'model.response', response({ role: 'assistant', content: '结论' }), 'model_2'),
    ])
    const calls = entries.filter((entry) => entry.kind === 'call')
    // 第一次没有上一轮可沿用；第二次沿用两条，新增 assistant 与 tool 各一条。
    expect(calls[0].request?.carried).toBe(0)
    expect(calls[1].request?.carried).toBe(2)
    expect(calls[1].request!.messages.length - calls[1].request!.carried).toBe(2)
  })

  it('用量取自供应商 usage，缓存命中单独保留', () => {
    const [call] = projectRunTrace([
      event(1, 'model.request', request([{ role: 'user', content: '问题' }]), 'model_1'),
      event(
        2,
        'model.response',
        response(
          { role: 'assistant', content: '答' },
          {
            prompt_tokens: 7436,
            completion_tokens: 721,
            prompt_tokens_details: { cached_tokens: 7296 },
          },
        ),
        'model_1',
      ),
    ])
    expect(call.response?.promptTokens).toBe(7436)
    expect(call.response?.completionTokens).toBe(721)
    expect(call.response?.cachedTokens).toBe(7296)
  })

  it('内部判断单独标记，便于和回答用的调用区分', () => {
    const [call] = projectRunTrace([
      event(
        1,
        'model.request',
        { ...request([{ role: 'user', content: '?' }]), output: 'internal' },
        'model_1',
      ),
      event(2, 'model.response', response({ role: 'assistant', content: 'lookup' }), 'model_1'),
    ])
    expect(call.internal).toBe(true)
  })
})

it('工具调用保留输入与输出，未完成的调用不当成成功', () => {
  const entries = projectRunTrace([
    event(1, 'run.started', {}),
    event(2, 'tool.started', { name: 'search_documents', arguments: '{"query":"410"}' }, 'call_1'),
    event(3, 'tool.result', { name: 'search_documents', result: { passages: [] } }, 'call_1'),
    event(4, 'tool.started', { name: 'read_document', arguments: '{}' }, 'call_2'),
  ])
  const tools = entries.filter((entry) => entry.kind === 'tool')
  expect(tools[0].arguments).toBe('{"query":"410"}')
  expect(tools[0].message?.content).toEqual({ passages: [] })
  expect(tools[0].status).toBeUndefined()
  expect(tools[1].status).toBe('pending')
})

it('失败与取消分别记为 error 与 interrupted', () => {
  const failed = projectRunTrace([
    event(1, 'model.request', request([{ role: 'user', content: '问题' }]), 'model_1'),
    event(2, 'model.error', { message: '连接失败' }, 'model_1'),
  ])
  expect(failed[0].status).toBe('error')
  const canceled = projectRunTrace([
    event(1, 'model.request', request([{ role: 'user', content: '问题' }]), 'model_2'),
    event(2, 'model.canceled', {}, 'model_2'),
  ])
  expect(canceled[0].status).toBe('interrupted')
})

it('检索与向量化单列一行，保留来源事件', () => {
  const entries = projectRunTrace([
    event(1, 'knowledge.request', { query: 'HTTP 410' }, 'retrieval_1'),
    event(2, 'knowledge.response', { results: [{ id: 'doc:A#c001' }] }, 'retrieval_1'),
    event(3, 'embedding.request', {}, 'embedding_1'),
    event(4, 'embedding.response', { usage: { prompt_tokens: 38 } }, 'embedding_1'),
  ])
  expect(entries.map((entry) => entry.label)).toEqual(['知识检索', '查询向量化'])
  expect(entries[0].sources).toHaveLength(2)
})

it('恢复运行时复用原输入，不向日志重复追加用户消息', () => {
  const [entry] = projectRunTrace([event(1, 'run.started', { resumed: true, input: '继续排查' })])
  expect(entry.kind).toBe('user')
  expect(entry.reused).toBe(true)
})

it('计数区分模型调用与其中的内部判断，并给出当前上下文规模', () => {
  const counts = traceCounts(
    projectRunTrace([
      event(1, 'message.user', { content: '问题' }),
      event(
        2,
        'model.request',
        { ...request([{ role: 'user', content: '问题' }]), output: 'internal' },
        'model_1',
      ),
      event(3, 'model.response', response({ role: 'assistant', content: 'lookup' }), 'model_1'),
      event(
        4,
        'model.request',
        request([
          { role: 'user', content: '问题' },
          { role: 'assistant', content: 'lookup' },
        ]),
        'model_2',
      ),
      event(5, 'model.response', response({ role: 'assistant', content: '答' }), 'model_2'),
      event(6, 'message.assistant', { content: '答' }),
    ]),
  )
  expect(counts).toEqual({ user: 1, call: 2, internal: 1, tool: 0, answer: 1, inputMessages: 2 })
})
