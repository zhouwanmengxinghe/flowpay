/* 定义事件类型并从事件推导页面状态；重连事件按会话序号去重。 */
import { modelTextDelta } from './model-events'
import {
  textValue,
  evidenceValues,
  emptyUsage,
  addModelUsage,
  addEmbeddingUsage,
  type EvidenceView,
  type ChatMessageView,
  type UsageView,
} from './event-data'

export type TraceEvent = {
  schema_version: number
  event_id: string
  session_id: string
  seq: number
  run_id: string | null
  call_id: string | null
  parent_call_id: string | null
  timestamp: string
  type: string
  payload: Record<string, unknown>
}
export type Session = { id: string; title: string; status: string; updated_at: string }
export type Meta = {
  workflow: { id: string; label: string; description: string }
  model: string
  examples: string[]
  reply_hint?: string
}

// SSE 重连会重放事件；以 seq 合并后排序，防止重复消息和乱序状态。
export function mergeEvents(existing: TraceEvent[], incoming: TraceEvent[]) {
  // 正常流式追加已按序到达，避免每个 token 都重新排序全部历史。
  if (
    incoming.length &&
    incoming[0].seq > (existing.at(-1)?.seq ?? 0) &&
    incoming.every((event, index) => index === 0 || event.seq > incoming[index - 1].seq)
  )
    return [...existing, ...incoming]
  const bySeq = new Map(existing.map((event) => [event.seq, event]))
  incoming.forEach((event) => bySeq.set(event.seq, event))
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq)
}

/** 顺序折叠事件得到当前状态；SSE 是否连接不代表 Run 是否仍在后台运行。 */
export function statusOf(events: TraceEvent[]) {
  let status = 'idle'
  for (const event of events) {
    if (event.type === 'run.started') status = 'running'
    else if (event.type === 'run.finished') status = textValue(event.payload.status) || status
    else if (['run.failed', 'run.canceled', 'run.interrupted'].includes(event.type))
      status = event.type.split('.')[1]
  }
  return status
}

/** 本轮尚未落库的模型正文，按模型调用拆开。
 *
 * 同一 Run 会调多次模型：每次调用前模型常先说一句"我先查支付记录"，调完工具再说下一句。
 * current 是最近一次调用正在写的正文，steps 是更早几次说过的话。两者分开返回，展示层
 * 才能把过程说明留在页面上又不与最终回答混为一谈——串成一段会读成一个虚假的完整答案。
 *
 * 落库的回答、内部反馈和被拒草稿都不在这里：它们各有归属，只留在轨迹中。
 */
export function liveSegments(events: TraceEvent[]): { steps: string[]; current: string } {
  const empty = { steps: [], current: '' }
  const lastRun = events.findLast((event) => event.type === 'run.started')?.run_id
  if (
    !lastRun ||
    events.some((event) => event.run_id === lastRun && event.type === 'message.assistant')
  )
    return empty
  const run = events.filter((event) => event.run_id === lastRun)
  const latest = run.findLast((event) => event.type === 'model.request')
  // 内部反馈留在轨迹；既不流入聊天正文，也不回退展示上一轮模型草稿。
  if (latest?.payload.output === 'internal') return empty
  const texts = new Map<string, string>()
  const order: string[] = []
  for (const event of run) {
    if (event.type === 'model.request' && event.call_id && event.payload.output !== 'internal') {
      texts.set(event.call_id, '')
      order.push(event.call_id)
    }
    const call = event.call_id
    if (call && texts.has(call)) texts.set(call, (texts.get(call) ?? '') + modelTextDelta(event))
    if (event.type === 'message.delta' && latest?.call_id)
      texts.set(
        latest.call_id,
        (texts.get(latest.call_id) ?? '') + textValue(event.payload.content),
      )
  }
  // 被拒绝的草稿不进聊天区：那一次的正文没有通过协议校验。
  const rejected = new Set(
    run.filter((event) => event.type === 'answer.rejected').map((event) => event.call_id),
  )
  const kept = order.filter((call) => !rejected.has(call) && (texts.get(call) ?? '') !== '')
  const current = latest?.call_id && !rejected.has(latest.call_id) ? latest.call_id : null
  return {
    steps: kept.filter((call) => call !== current).map((call) => texts.get(call) ?? ''),
    current: current ? (texts.get(current) ?? '') : '',
  }
}

// 最终回答落库后停止展示临时片段；仅返回最近一次模型请求正在写的文本。
export function liveText(events: TraceEvent[]) {
  return liveSegments(events).current
}

/** 本轮调用过的工具，按首次出现排序，分成还在跑的和已返回的。
 *
 * liveText 只认最近一次模型调用，模型每调一次工具正文就清空一次。空窗期用这些工具名
 * 说明进度，读者才知道刚才那段字去哪了——它们留在右侧轨迹里，没有丢。
 */
export function liveTools(events: TraceEvent[]): { active: string[]; finished: string[] } {
  const lastRun = events.findLast((event) => event.type === 'run.started')?.run_id
  if (!lastRun) return { active: [], finished: [] }
  const run = events.filter((event) => event.run_id === lastRun)
  const answered = new Set(
    run
      .filter((event) => ['tool.result', 'tool.error', 'tool.canceled'].includes(event.type))
      .map((event) => event.call_id),
  )
  const active: string[] = []
  const finished: string[] = []
  for (const event of run) {
    if (event.type !== 'tool.started') continue
    const name = textValue(event.payload.name)
    const list = answered.has(event.call_id) ? finished : active
    if (name && !list.includes(name)) list.push(name)
  }
  return { active, finished }
}

export const statusLabels: Record<string, string> = {
  idle: '等待提问',
  running: '调查中',
  waiting_user: '等待补充',
  completed: '已完成',
  failed: '执行失败',
  canceled: '已停止',
  interrupted: '已中断',
}

export const eventLabels: Record<string, string> = {
  'model.request': '模型请求',
  'model.response': '模型返回',
  'model.chunk': '流式片段',
  'model.error': '模型错误',
  'model.validation_error': '模型结果校验失败',
  'model.retry': '模型连接重试',
  'answer.rejected': '回答正文不完整',
  'answer.disputed': '引用了未查到的记录',
  'answer.prepared': '回答已整理',
  'retrieval.feedback': '信息缺口反馈',
  'model.canceled': '模型响应已停止',
  'tool.started': '工具开始',
  'tool.result': '工具结果',
  'tool.error': '工具错误',
  'tool.canceled': '工具已停止',
  'run.started': '运行开始',
  'run.finished': '运行结束',
  'run.failed': '运行失败',
  'run.interrupted': '运行中断',
  'run.canceled': '运行停止',
  'message.user': '用户输入',
  'message.assistant': '最终回答',
  'session.created': '会话创建',
}

/** 聊天组件只接收已经解释的正文与证据，不自行读取动态载荷。 */
export function chatMessages(events: TraceEvent[]): ChatMessageView[] {
  const retrievedByRun = new Map<string | null, Map<string, EvidenceView>>()
  const usageByRun = new Map<string | null, UsageView>()
  // 路由与缺口反馈用 output:"internal" 标记；这些调用同样花 token，单独计数便于对照。
  const internalCalls = new Set<string>()
  const messages: ChatMessageView[] = []
  const usageOf = (runId: string | null) => {
    const usage = usageByRun.get(runId) ?? emptyUsage()
    usageByRun.set(runId, usage)
    return usage
  }
  for (const event of events) {
    if (event.type === 'model.request' && event.payload.output === 'internal' && event.call_id)
      internalCalls.add(event.call_id)
    if (event.type === 'model.response')
      addModelUsage(usageOf(event.run_id), event.payload, internalCalls.has(event.call_id ?? ''))
    if (event.type === 'embedding.response') addEmbeddingUsage(usageOf(event.run_id), event.payload)
    // 检索命中与整篇读回都是本轮新拿到的片段，两种事件都要收。
    const incoming =
      event.type === 'knowledge.response'
        ? event.payload.results
        : event.type === 'document.response'
          ? event.payload.passages
          : undefined
    if (Array.isArray(incoming)) {
      const retrieved = retrievedByRun.get(event.run_id) ?? new Map<string, EvidenceView>()
      for (const item of evidenceValues(incoming)) retrieved.set(item.id, item)
      retrievedByRun.set(event.run_id, retrieved)
    }
    if (event.type !== 'message.user' && event.type !== 'message.assistant') continue
    const content = textValue(event.payload.content)
    // 后端交来的片段（本会话累计）和本轮新取回的片段合成一份清单，按 id 去重。
    // 两者会重叠：同一轮里检索到的片段既在事件里，也在提交的 evidence 里。
    // 本轮取回的片段与后端随回答交来的片段合成一份清单，按 id 去重。
    const thisRun = [...(retrievedByRun.get(event.run_id)?.values() ?? [])]
    const available = [
      ...new Map(
        [...thisRun, ...evidenceValues(event.payload.evidence)].map((item) => [item.id, item]),
      ).values(),
    ]
    const assistant = event.type === 'message.assistant'
    messages.push({
      event_id: event.event_id,
      timestamp: event.timestamp,
      type: event.type,
      payload: {
        content,
        available: assistant ? available : [],
        usage: assistant ? usageByRun.get(event.run_id) : undefined,
      },
    })
  }
  return messages
}
