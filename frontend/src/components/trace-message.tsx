/* 展示一条轨迹。模型调用按"请求 → 响应"两段呈现，其余按类型给出对应细节。 */
import {
  Braces,
  ChevronRight,
  CornerDownRight,
  LoaderCircle,
  MessageSquare,
  Search,
  Terminal,
  Workflow,
} from 'lucide-react'
import { MarkdownContent, JsonContent } from '@/components/markdown-content'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { cn } from '@/lib/utils'
import { contentText, roleLabels } from '@/trace'
import type { TraceEntry, TraceMessage, TraceRequest } from '@/trace'

const ICONS = {
  user: MessageSquare,
  call: Workflow,
  tool: Terminal,
  answer: MessageSquare,
  event: Search,
}

const n = (value: number) => value.toLocaleString()

/** 一行摘要：这次调用发了多少上下文、带了哪些工具、花了多少 token。 */
function callSummary(entry: TraceEntry): string {
  const request = entry.request
  const response = entry.response
  if (!request) return entry.label
  const added = request.messages.length - request.carried
  const parts = [`${request.messages.length} 条消息`]
  if (added > 0) parts.push(`本次新增 ${added}`)
  if (request.tools.length) parts.push(`${request.tools.length} 个工具`)
  if (response?.promptTokens) parts.push(`${n(response.promptTokens)} tokens`)
  return parts.join(' · ')
}

/** 请求里的一条消息；沿用的消息默认折叠，只有本次新增的展开。 */
function MessageRow({
  message,
  index,
  fresh,
}: {
  message: TraceMessage
  index: number
  fresh: boolean
}) {
  const role = message.role ?? 'unknown'
  const calls = message.tool_calls as { function?: { name?: string } }[] | undefined
  return (
    <div
      className={cn(
        'space-y-1 rounded-md border px-2 py-1.5',
        fresh && 'border-primary/40 bg-primary/5',
      )}
    >
      <div className="flex items-center gap-2">
        <Badge variant="outline" className="font-mono text-[9px]">
          {role}
        </Badge>
        <span className="text-[9px] text-muted-foreground">messages[{index}]</span>
        {fresh && <span className="text-[9px] text-primary">本次新增</span>}
      </div>
      {message.content != null && contentText(message.content) !== '' && (
        <MarkdownContent text={contentText(message.content)} />
      )}
      {!!calls?.length && (
        <p className="text-[10px] text-muted-foreground">
          请求工具：{calls.map((call) => call.function?.name ?? '?').join('、')}
        </p>
      )}
    </div>
  )
}

/** 请求侧：沿用的消息折叠成一行，本次新增逐条展开，工具面单列。 */
function RequestBlock({ request }: { request: TraceRequest }) {
  const fresh = request.messages.slice(request.carried)
  return (
    <div className="space-y-2">
      <p className="text-[10px] font-medium text-muted-foreground">
        请求 · 发给 {request.model} 的完整上下文
      </p>
      {request.carried > 0 && (
        <Collapsible>
          <CollapsibleTrigger asChild>
            <Button variant="ghost" size="sm" className="h-6 w-full justify-start px-1 text-[10px]">
              <ChevronRight className="size-3 transition-transform data-[state=open]:rotate-90" />
              沿用前 {request.carried} 条消息
            </Button>
          </CollapsibleTrigger>
          <CollapsibleContent className="mt-1 space-y-1.5">
            {request.messages.slice(0, request.carried).map((message, index) => (
              <MessageRow key={index} message={message} index={index} fresh={false} />
            ))}
          </CollapsibleContent>
        </Collapsible>
      )}
      {fresh.map((message, index) => (
        <MessageRow
          key={request.carried + index}
          message={message}
          index={request.carried + index}
          fresh={request.carried > 0}
        />
      ))}
      {!!request.tools.length && (
        <p className="text-[10px] text-muted-foreground">
          本次提供的工具（{request.tools.length}）：{request.tools.join('、')}
        </p>
      )}
    </div>
  )
}

/**
 * 只负责展示已经投影好的 TraceEntry，不在组件中重新解释原始事件协议。
 * inspect 由上层统一管理弹窗，避免每张卡片各自维护一份会话日志。
 */
export function TraceMessageCard({
  entry,
  step,
  nested,
  inspect,
}: {
  entry: TraceEntry
  /** 本轮内的步骤序号，显示在时间轴节点上。 */
  step?: number
  /** 这一步由上一条模型调用产生，缩进一级并换用细边框。 */
  nested?: boolean
  inspect: (title: string, data: unknown) => void
}) {
  const Icon = ICONS[entry.kind]
  const title =
    entry.kind === 'call'
      ? entry.internal
        ? `${entry.label} · 内部判断`
        : entry.label
      : entry.kind === 'tool'
        ? `${entry.label} · tool`
        : entry.kind === 'user'
          ? `${roleLabels.user} · user`
          : entry.label
  const preview =
    entry.kind === 'call'
      ? callSummary(entry)
      : contentText(entry.message?.content).replace(/\s+/g, ' ').slice(0, 90) || entry.label
  const response = entry.response
  return (
    <Collapsible
      className={cn(
        'group/entry relative rounded-lg border bg-background',
        // 节点压在竖线上；模型调用实心，其余空心，一眼看出哪几步是模型在决策。
        'before:absolute before:top-4 before:-left-6 before:z-10 before:flex before:size-4',
        'before:-translate-x-1/2 before:items-center before:justify-center before:rounded-full',
        'before:border before:bg-background before:text-[8px] before:leading-none',
        'before:content-[attr(data-step)]',
        entry.kind === 'call'
          ? 'before:border-primary before:bg-primary before:text-primary-foreground'
          : 'before:border-border before:text-muted-foreground',
        nested && 'ml-4 border-dashed',
        entry.status === 'error' && 'border-destructive/40 before:border-destructive',
      )}
      data-step={step ?? ''}
    >
      <CollapsibleTrigger asChild>
        <Button variant="ghost" className="h-auto w-full justify-start gap-2 p-3 text-left">
          <Icon
            className={cn(
              'mt-0.5 size-3.5 shrink-0',
              entry.kind === 'call' ? 'text-primary' : 'text-muted-foreground',
            )}
          />
          <span className="min-w-0 flex-1 space-y-0.5">
            <span className="flex items-center gap-2">
              <span className="truncate text-xs font-medium">{title}</span>
              {entry.status === 'pending' ? (
                <LoaderCircle className="size-3 animate-spin text-muted-foreground" />
              ) : entry.status === 'error' ? (
                <span className="text-[10px] text-destructive">失败</span>
              ) : entry.status === 'interrupted' ? (
                <span className="text-[10px] text-muted-foreground">未完成</span>
              ) : (
                (response?.duration ?? entry.duration) != null && (
                  <span className="text-[10px] text-muted-foreground">
                    {response?.duration ?? entry.duration} ms
                  </span>
                )
              )}
            </span>
            <span className="block truncate text-[10px] text-muted-foreground">{preview}</span>
          </span>
          <ChevronRight className="mt-1 size-3 shrink-0 text-muted-foreground transition-transform group-data-[state=open]/entry:rotate-90" />
        </Button>
      </CollapsibleTrigger>
      <CollapsibleContent className="space-y-3 border-t p-3">
        {entry.reused && (
          <p className="text-[10px] text-muted-foreground">
            继续本轮时复用的原用户输入；没有向日志重复追加用户消息。
          </p>
        )}
        {entry.request && <RequestBlock request={entry.request} />}
        {entry.kind === 'call' && (
          <div className="space-y-1.5">
            <p className="flex items-center gap-1 text-[10px] font-medium text-muted-foreground">
              <CornerDownRight className="size-3" />
              响应
              {response?.finishReason && (
                <span className="font-mono">finish_reason={response.finishReason}</span>
              )}
              {response?.completionTokens != null && response.completionTokens > 0 && (
                <span>· 输出 {n(response.completionTokens)} tokens</span>
              )}
              {!!response?.cachedTokens && <span>· 命中缓存 {n(response.cachedTokens)}</span>}
            </p>
            {response?.message?.content != null && contentText(response.message.content) !== '' && (
              <MarkdownContent
                text={contentText(response.message.content)}
                streaming={entry.status === 'pending'}
              />
            )}
            {!!(response?.message?.tool_calls as unknown[] | undefined)?.length && (
              <JsonContent data={response?.message?.tool_calls} />
            )}
          </div>
        )}
        {entry.arguments !== undefined && (
          <div className="space-y-1.5">
            <p className="text-[10px] text-muted-foreground">工具输入 · arguments</p>
            <JsonContent data={entry.arguments} />
          </div>
        )}
        {entry.kind === 'tool' && entry.message?.content != null && (
          <div className="space-y-1.5">
            <p className="text-[10px] text-muted-foreground">工具输出</p>
            <JsonContent data={entry.message.content} />
          </div>
        )}
        {(entry.kind === 'user' || entry.kind === 'answer') && entry.message?.content != null && (
          <MarkdownContent text={contentText(entry.message.content)} />
        )}
        {entry.kind === 'event' && (
          <JsonContent data={entry.details ?? entry.sources.map((source) => source.payload)} />
        )}
        {entry.status && (
          <p className="text-[10px] text-muted-foreground">
            {entry.status === 'pending'
              ? '尚未完整返回。'
              : '没有完整返回，错误或中断信息保存在原始事件中。'}
          </p>
        )}
        <Button
          variant="link"
          size="sm"
          className="h-7 px-0 text-xs"
          onClick={() => inspect(`${title} · 原始事件`, entry.sources)}
        >
          <Braces className="size-3" />
          查看原始事件 ({entry.sources.length})
        </Button>
      </CollapsibleContent>
    </Collapsible>
  )
}
