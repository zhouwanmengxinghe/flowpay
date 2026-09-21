import type { UsageView } from '@/event-data'

/** 一轮对话的模型用量。聊天与嵌入分开显示，两者计费单位不同。 */
export function UsageLine({ usage }: { usage: UsageView }) {
  if (!usage.modelCalls && !usage.embeddingCalls) return null
  const n = (value: number) => value.toLocaleString()
  const parts = [
    `输入 ${n(usage.promptTokens)}`,
    ...(usage.cachedTokens ? [`命中缓存 ${n(usage.cachedTokens)}`] : []),
    `输出 ${n(usage.completionTokens)}`,
    `${usage.modelCalls} 次模型调用`,
    ...(usage.internalCalls ? [`其中内部判断 ${usage.internalCalls} 次`] : []),
    ...(usage.embeddingCalls
      ? [`嵌入 ${n(usage.embeddingTokens)} tokens / ${usage.embeddingCalls} 次`]
      : []),
  ]
  return (
    <p className="mt-3 text-[10px] text-muted-foreground" data-testid="usage-line">
      本轮用量 {n(usage.promptTokens + usage.completionTokens)} tokens · {parts.join(' · ')}
    </p>
  )
}
