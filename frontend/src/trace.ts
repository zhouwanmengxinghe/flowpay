import { modelTextDelta } from './model-events'
/* 把事件投影成 AI 交互流程：每次模型调用展示请求（消息与工具）与响应，工具调用单列。 */
import { objectValue, textValue, numberValue, messageValue } from './event-data'
import type { TraceEvent } from './state'

export type TraceMessage = Record<string, unknown> & { role?: string; content?: unknown }

/** 一次模型调用的请求侧：发出去的消息、这轮新增了几条、带了哪些工具。 */
export type TraceRequest = {
  model: string
  messages: TraceMessage[]
  /** 与上一次调用相同的前缀长度；其余是本次新增，UI 据此只展开增量。 */
  carried: number
  tools: string[]
}

/** 一次模型调用的响应侧；usage 缺失时供应商没有回报用量。 */
export type TraceResponse = {
  message?: TraceMessage
  finishReason?: string
  promptTokens?: number
  completionTokens?: number
  cachedTokens?: number
  duration?: number
}

export type TraceEntry = {
  id: string
  kind: 'user' | 'call' | 'tool' | 'answer' | 'event'
  seq: number
  label: string
  status?: 'pending' | 'error' | 'interrupted'
  message?: TraceMessage
  request?: TraceRequest
  response?: TraceResponse
  arguments?: unknown
  details?: unknown
  duration?: number
  reused?: boolean
  internal?: boolean
  /** 产生这一步的模型调用条目 id；工具调用据此在轨迹里缩进到那次调用下面。 */
  parentId?: string
  sources: TraceEvent[]
}

export const roleLabels: Record<string, string> = {
  user: '用户',
  assistant: '模型',
  tool: '工具',
  system: '系统',
  developer: '开发者',
}

export function contentText(content: unknown): string {
  if (typeof content === 'string') return content
  if (content == null) return ''
  return JSON.stringify(content, null, 2)
}

/** 与上一次调用相同的消息前缀长度；用于只展开本次新增的几条。 */
function carriedPrefix(previous: TraceMessage[], current: TraceMessage[]): number {
  let index = 0
  while (
    index < previous.length &&
    index < current.length &&
    JSON.stringify(previous[index]) === JSON.stringify(current[index])
  )
    index += 1
  return index
}

function toolNames(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .map((tool) => textValue(objectValue(objectValue(tool).function).name))
    .filter(Boolean)
}

function requestOf(event: TraceEvent, previous: TraceMessage[]): TraceRequest {
  const body = objectValue(objectValue(event.payload.request).body)
  const messages = Array.isArray(body.messages)
    ? body.messages.map((raw: unknown) => messageValue(raw)).filter((m): m is TraceMessage => !!m)
    : []
  return {
    model: textValue(body.model) || '模型',
    messages,
    carried: carriedPrefix(previous, messages),
    tools: toolNames(body.tools),
  }
}

function responseOf(result: TraceEvent | undefined, streamed: string): TraceResponse | undefined {
  if (!result) return streamed ? { message: { role: 'assistant', content: streamed } } : undefined
  const completion = objectValue(result.payload.completion)
  const choice = Array.isArray(completion.choices) ? objectValue(completion.choices[0]) : {}
  const usage = objectValue(completion.usage)
  const details = objectValue(usage.prompt_tokens_details)
  return {
    message: messageValue(choice.message),
    finishReason: textValue(choice.finish_reason) || undefined,
    promptTokens: numberValue(usage.prompt_tokens),
    completionTokens: numberValue(usage.completion_tokens),
    cachedTokens: numberValue(details.cached_tokens),
    duration: numberValue(result.payload.duration_ms),
  }
}

// 这里只生成显示投影，不修改已保存事件和供应商请求体。
/**
 * 把单个 Run 的事件投影成 AI 交互流程；调用方需先按 run_id 过滤。
 *
 * 每次模型调用产出一条 `call`：请求侧是真正发出去的消息与工具，响应侧是供应商返回的
 * 消息、结束原因和用量。请求消息按与上一次调用的公共前缀切成"沿用"和"新增"，
 * 让上下文怎么长起来成为可读的主线，而不是每轮重复一整份。
 */
export function projectRunTrace(events: TraceEvent[]): TraceEntry[] {
  const rows: TraceEntry[] = []
  const started = events.find((e) => e.type === 'run.started')
  const finished = events.some((e) =>
    ['run.finished', 'run.failed', 'run.canceled', 'run.interrupted'].includes(e.type),
  )
  const byCall = new Map<string, TraceEvent[]>()
  for (const event of events) {
    if (!event.call_id) continue
    const group = byCall.get(event.call_id) ?? []
    group.push(event)
    byCall.set(event.call_id, group)
  }
  const related = (event: TraceEvent) => (event.call_id ? (byCall.get(event.call_id) ?? []) : [])

  for (const event of events.filter((e) => e.type === 'message.user'))
    rows.push({
      id: event.event_id,
      kind: 'user',
      seq: event.seq,
      label: '用户',
      message: { role: 'user', content: event.payload.content },
      sources: [event],
    })
  // 恢复不再追加用户事件，但轨迹仍需展示被复用的输入及其来源。
  if (
    !events.some((e) => e.type === 'message.user') &&
    started?.payload.resumed &&
    typeof started.payload.input === 'string'
  )
    rows.push({
      id: `${started.event_id}:input`,
      kind: 'user',
      seq: started.seq - 0.5,
      label: '用户',
      reused: true,
      message: { role: 'user', content: started.payload.input },
      sources: [started],
    })

  let previousMessages: TraceMessage[] = []
  // model.request 的 call_id 对应条目 id；tool.started 用 parent_call_id 找回它。
  const callEntries = new Map<string, string>()
  for (const event of events) {
    if (event.type === 'model.request') {
      if (event.call_id) callEntries.set(event.call_id, event.event_id)
      const sources = related(event)
      const result = sources.find((e) => e.type === 'model.response')
      const failure = sources.find((e) =>
        ['model.error', 'model.validation_error', 'answer.rejected', 'model.canceled'].includes(
          e.type,
        ),
      )
      const request = requestOf(event, previousMessages)
      previousMessages = request.messages
      rows.push({
        id: event.event_id,
        kind: 'call',
        seq: event.seq,
        label: request.model,
        internal: event.payload.output === 'internal',
        request,
        response: responseOf(result, sources.map((chunk) => modelTextDelta(chunk)).join('')),
        status: failure
          ? failure.type === 'model.canceled'
            ? 'interrupted'
            : 'error'
          : result
            ? undefined
            : finished
              ? 'interrupted'
              : 'pending',
        sources,
      })
    }
    if (event.type === 'tool.started') {
      const sources = related(event)
      const result = sources.find((e) => e.type === 'tool.result')
      const failure = sources.find((e) => ['tool.error', 'tool.canceled'].includes(e.type))
      rows.push({
        id: event.event_id,
        kind: 'tool',
        seq: result?.seq ?? failure?.seq ?? event.seq,
        label: textValue(event.payload.name),
        parentId: event.parent_call_id ? callEntries.get(event.parent_call_id) : undefined,
        arguments: event.payload.arguments,
        message: { role: 'tool', content: result?.payload.result ?? null },
        duration: numberValue(result?.payload.duration_ms),
        status: result
          ? undefined
          : failure?.type === 'tool.error'
            ? 'error'
            : failure || finished
              ? 'interrupted'
              : 'pending',
        sources,
      })
    }
    if (event.type === 'message.assistant')
      rows.push({
        id: event.event_id,
        kind: 'answer',
        seq: event.seq,
        label: '最终回答',
        message: { role: 'assistant', content: event.payload.content },
        sources: [event],
      })
  }
  // 检索与向量化不属于模型交互本身，单列一行说明这一步做了什么。
  const covered = new Set(rows.flatMap((row) => row.sources.map((e) => e.event_id)))
  for (const event of events) {
    if (covered.has(event.event_id) || event.type.startsWith('run.')) continue
    const family = event.type.split('.')[0]
    if (event.call_id && ['knowledge', 'embedding'].includes(family)) {
      const sources = related(event).filter((item) => item.type.startsWith(`${family}.`))
      sources.forEach((item) => covered.add(item.event_id))
      const request = sources.find((item) => item.type === `${family}.request`)
      const result = sources.find((item) => item.type === `${family}.response`)
      const failure = sources.find((item) => /\.(error|canceled)$/.test(item.type))
      rows.push({
        id: event.event_id,
        kind: 'event',
        seq: event.seq,
        label: family === 'knowledge' ? '知识检索' : '查询向量化',
        message: {
          content:
            family === 'knowledge'
              ? (request?.payload.query ?? '检索文档片段')
              : '将查询文本转换为向量',
        },
        details:
          family === 'knowledge'
            ? { request: request?.payload, results: result?.payload.results }
            : { usage: result?.payload.usage },
        status: result
          ? undefined
          : failure
            ? failure.type.endsWith('.error')
              ? 'error'
              : 'interrupted'
            : finished
              ? 'interrupted'
              : 'pending',
        sources,
      })
      continue
    }
    covered.add(event.event_id)
    rows.push({
      id: event.event_id,
      kind: 'event',
      seq: event.seq,
      label: event.type,
      sources: [event],
    })
  }
  return rows.sort((a, b) => a.seq - b.seq)
}

// 未完成的调用不计入；模型调用另外汇总本轮实际发出的上下文规模。
export function traceCounts(entries: TraceEntry[]) {
  const done = entries.filter((entry) => !entry.status)
  const calls = done.filter((entry) => entry.kind === 'call')
  return {
    user: done.filter((entry) => entry.kind === 'user').length,
    call: calls.length,
    internal: calls.filter((entry) => entry.internal).length,
    tool: done.filter((entry) => entry.kind === 'tool').length,
    answer: done.filter((entry) => entry.kind === 'answer').length,
    inputMessages: calls.at(-1)?.request?.messages.length ?? 0,
  }
}
