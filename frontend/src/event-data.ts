/** 动态事件在投影入口收窄；未知字段留在原始日志里。 */
export type JsonRecord = Record<string, unknown>
export type EvidenceView = JsonRecord & { id: string; title?: string }
export type MessageView = JsonRecord & { role?: string; content?: unknown }

export function objectValue(value: unknown): JsonRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as JsonRecord)
    : {}
}
export function textValue(value: unknown): string {
  return typeof value === 'string' ? value : ''
}
export function numberValue(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}
export function messageValue(value: unknown): MessageView | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const raw = objectValue(value)
  return { ...raw, role: typeof raw.role === 'string' ? raw.role : undefined }
}
export function evidenceValues(value: unknown): EvidenceView[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item: unknown) => {
    const raw = objectValue(item)
    return typeof raw.id === 'string'
      ? [{ ...raw, id: raw.id, title: typeof raw.title === 'string' ? raw.title : undefined }]
      : []
  })
}
/** 一轮对话的模型用量。聊天与嵌入分开计，两者的计费单位不同。 */
export type UsageView = {
  promptTokens: number
  completionTokens: number
  cachedTokens: number
  modelCalls: number
  internalCalls: number
  embeddingTokens: number
  embeddingCalls: number
}

export function emptyUsage(): UsageView {
  return {
    promptTokens: 0,
    completionTokens: 0,
    cachedTokens: 0,
    modelCalls: 0,
    internalCalls: 0,
    embeddingTokens: 0,
    embeddingCalls: 0,
  }
}

/** 累加一次模型响应的用量；internal 指路由、缺口反馈这类不直接产出回答的调用。 */
export function addModelUsage(usage: UsageView, payload: JsonRecord, internal: boolean): void {
  const raw = objectValue(objectValue(payload.completion).usage)
  usage.promptTokens += numberValue(raw.prompt_tokens) ?? 0
  usage.completionTokens += numberValue(raw.completion_tokens) ?? 0
  usage.cachedTokens += numberValue(objectValue(raw.prompt_tokens_details).cached_tokens) ?? 0
  usage.modelCalls += 1
  if (internal) usage.internalCalls += 1
}

/** 累加一次嵌入响应的用量；嵌入只有输入 token。 */
export function addEmbeddingUsage(usage: UsageView, payload: JsonRecord): void {
  usage.embeddingTokens += numberValue(objectValue(payload.usage).prompt_tokens) ?? 0
  usage.embeddingCalls += 1
}

export type ChatMessageView = {
  event_id: string
  timestamp: string
  type: string
  payload: {
    content: string
    /** 本轮可查看的全部片段：本轮取回的，加上后端随回答交来的。 */
    available: EvidenceView[]
    usage?: UsageView
  }
}
