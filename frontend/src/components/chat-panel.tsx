import { useEffect, useRef, useState } from 'react'
import { UsageLine } from '@/components/usage-line'
import {
  Workflow,
  MessageSquare,
  ChevronRight,
  LoaderCircle,
  Layers3,
  FileText,
  RotateCcw,
  CircleHelp,
  Square,
  ArrowUp,
  User,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Alert, AlertTitle, AlertDescription } from '@/components/ui/alert'
import { MarkdownContent } from '@/components/markdown-content'
import { StatusBadge } from '@/components/status-badge'
import { textValue } from '@/event-data'
import { cn } from '@/lib/utils'
import { liveSegments, liveTools, statusLabels, chatMessages } from '@/state'
import type { RunActions } from '@/hooks/use-run-actions'
import type { ConnectionStatus } from '@/hooks/use-session-events'
import type { Meta, TraceEvent } from '@/state'
import { time, type Inspect } from '@/lib/display'

/** 正文空窗期的进度说明：模型每调一次工具就重开一次回答，这里交代它正在做什么。 */
function progressText({ active, finished }: { active: string[]; finished: string[] }) {
  if (active.length) return `正在查询 ${active.join('、')}`
  if (finished.length) return `已查询 ${finished.join('、')}，正在整理`
  return '正在查询与整理信息'
}

export function ChatPanel({
  meta,
  title,
  sessionId: active,
  events,
  status,
  connection,
  loading,
  actions,
  inspect,
}: {
  meta: Meta | null
  title?: string
  sessionId: string | null
  events: TraceEvent[]
  status: string
  connection: ConnectionStatus
  loading: boolean
  actions: Pick<RunActions, 'pending' | 'submitMessage' | 'resumeRun' | 'cancelRun'>
  inspect: Inspect
}) {
  // 草稿和滚动属于聊天界面；网络重试 ID 由操作 hook 持有。
  const [draft, setDraft] = useState('')
  const draftSession = useRef(active)
  const { pending } = actions
  const running = status === 'running'
  const canContinue = ['failed', 'interrupted', 'canceled'].includes(status)
  useEffect(() => {
    draftSession.current = active
    setDraft('')
  }, [active])
  async function submit(content = draft) {
    pinned.current = true
    const accepted = await actions.submitMessage(content)
    if (accepted && draftSession.current === active) setDraft('')
  }
  const bottom = useRef<HTMLDivElement>(null)
  const scroll = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)
  const content = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!content.current || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => {
      if (pinned.current) bottom.current?.scrollIntoView({ block: 'end' })
    })
    observer.observe(content.current)
    return () => observer.disconnect()
  }, [])
  const messages = chatMessages(events)
  const { steps, current: live } = liveSegments(events)
  const tools = liveTools(events)
  const terminalError = events.findLast((event) =>
    ['run.failed', 'run.interrupted', 'run.canceled'].includes(event.type),
  )
  useEffect(() => {
    pinned.current = true
  }, [active])
  // 用户阅读历史时保留滚动位置；跟随最新消息时才滚动到底部。
  useEffect(() => {
    if (pinned.current) bottom.current?.scrollIntoView({ block: 'end' })
  }, [messages.length, live, status])
  return (
    <section aria-label="客服对话" className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center justify-between gap-4 border-b border-border/50 px-5 py-5 sm:px-7">
        <div className="min-w-0">
          <h1 className="truncate text-sm font-semibold">{title ?? '新的调查'}</h1>
          <p className="mt-1.5 text-[10px] text-muted-foreground">
            {meta?.workflow.description ?? '正在读取运行环境'}
          </p>
        </div>
        <StatusBadge status={status} />
      </div>
      <div
        className="thin-scrollbar min-h-0 flex-1 overflow-y-auto px-5 py-4 sm:px-7 xl:px-10"
        ref={scroll}
        onScroll={() => {
          const node = scroll.current
          if (node) pinned.current = node.scrollHeight - node.scrollTop - node.clientHeight < 90
        }}
      >
        <div ref={content}>
          {!messages.length && (
            <div className="mx-auto my-8 max-w-md sm:my-14">
              <span className="mb-6 flex size-14 items-center justify-center rounded-2xl border bg-gradient-to-br from-blue-50 to-white text-blue-500 shadow-xs">
                <Workflow className="size-7" strokeWidth={1.5} />
              </span>
              <p className="text-[10px] tracking-[0.2em] text-muted-foreground">FLOWPAY SUPPORT</p>
              <h2 className="mt-3 mb-4 text-3xl leading-relaxed font-medium tracking-tight">
                从问题开始，
                <br />
                逐步找到答案。
              </h2>
              <p className="text-xs leading-6 text-muted-foreground">
                描述你遇到的问题。助手会查询相关信息，
                <br className="hidden xl:block" />
                并在需要时向你追问。
              </p>
              {(meta?.examples ?? []).map((example) => (
                <Button
                  variant="outline"
                  key={example}
                  disabled={pending}
                  className="mt-7 h-auto w-full justify-start gap-3 bg-muted/40 p-4 text-left whitespace-normal"
                  onClick={() => submit(example)}
                >
                  <MessageSquare className="size-4 shrink-0 text-muted-foreground" />
                  <span className="flex-1 text-xs leading-6 font-normal">{example}</span>
                  <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
                </Button>
              ))}
              {loading && (
                <LoaderCircle className="mt-6 size-5 animate-spin text-muted-foreground" />
              )}
            </div>
          )}
          {messages.map((message) => (
            <article key={message.event_id} className="mx-auto my-5 mb-8 flex max-w-3xl gap-3">
              {/* 两种角色共用同一套头像与左边距，正文因此在同一条竖线上起笔。 */}
              <span
                className={cn(
                  'flex size-8 shrink-0 items-center justify-center rounded-lg border',
                  message.type === 'message.user'
                    ? 'bg-muted text-muted-foreground'
                    : 'border-blue-100 bg-blue-50 text-blue-600',
                )}
              >
                {message.type === 'message.user' ? (
                  <User className="size-4" />
                ) : (
                  <Layers3 className="size-4" />
                )}
              </span>
              <div className="min-w-0 flex-1">
                <div className="mb-2 flex h-8 items-center gap-2.5">
                  <strong className="text-[13px] font-semibold">
                    {message.type === 'message.user' ? '你' : 'FlowPay 助手'}
                  </strong>
                  <time className="text-[11px] text-muted-foreground">
                    {time(message.timestamp)}
                  </time>
                </div>
                {message.type === 'message.user' ? (
                  <p className="text-[15px] leading-8 font-medium break-words whitespace-pre-wrap">
                    {message.payload.content}
                  </p>
                ) : (
                  <MarkdownContent text={message.payload.content} />
                )}
                {message.payload.usage && <UsageLine usage={message.payload.usage} />}
                {!!message.payload.available.length && (
                  <details className="mt-4 text-xs text-muted-foreground">
                    <summary className="cursor-pointer">
                      本轮资料（{message.payload.available.length}）
                    </summary>
                    <p className="my-2 text-[10px]">
                      本轮检索命中、整篇读回，以及可继续引用的往轮片段，含回答没有采用的。
                    </p>
                    <div className="flex flex-wrap gap-2">
                      {message.payload.available.map((item) => (
                        <Button
                          key={item.id}
                          variant="outline"
                          size="sm"
                          className="h-auto whitespace-normal text-left text-[10px]"
                          onClick={() => inspect(item.title ?? item.id, item)}
                        >
                          <FileText className="size-3 shrink-0" />
                          {item.title ?? item.id}
                        </Button>
                      ))}
                    </div>
                  </details>
                )}
              </div>
            </article>
          ))}
          {(running || !!live || !!steps.length) && (
            <article className="mx-auto my-5 flex max-w-3xl gap-3">
              <span className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-blue-100 bg-blue-50 text-blue-600">
                <Layers3 className="size-4" />
              </span>
              <div className="min-w-0 flex-1">
                <div className="mb-2.5 flex h-8 items-center gap-3">
                  <strong className="text-xs">FlowPay 助手</strong>
                  <span className="text-[10px] text-muted-foreground">
                    {running ? (live ? '正在生成回答' : '调查中') : '未完成的输出'}
                  </span>
                </div>
                {steps.map((step, index) => (
                  <p
                    key={index}
                    className="mb-3 border-l-2 border-border/70 pl-3 text-xs leading-6 whitespace-pre-wrap text-muted-foreground"
                  >
                    {step}
                  </p>
                ))}
                {live ? (
                  <MarkdownContent text={live} streaming={running} />
                ) : (
                  <p className="flex items-center gap-2 py-2 text-xs text-muted-foreground">
                    <LoaderCircle className="size-3.5 animate-spin" />
                    {progressText(tools)}
                  </p>
                )}
              </div>
            </article>
          )}
          {canContinue && (
            <Alert className="my-4 border-amber-200 bg-amber-50 text-amber-900">
              <RotateCcw className="size-4" />
              <AlertTitle className="text-xs">{statusLabels[status]}，记录已保留</AlertTitle>
              <AlertDescription className="space-y-3 text-xs text-amber-800">
                <p>
                  {textValue(terminalError?.payload.message) ||
                    '继续会从本轮开始重新执行，只读工具可能再次被调用。'}
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={pending}
                  onClick={() => actions.resumeRun()}
                >
                  继续本轮
                </Button>
              </AlertDescription>
            </Alert>
          )}
          <div ref={bottom} />
        </div>
      </div>
      {/* 输入区与消息流之间给一条边界，滚动时正文不会贴着输入框穿过去。 */}
      <div className="shrink-0 border-t bg-background px-4 pt-3 pb-3 sm:px-7 xl:px-10">
        {status === 'waiting_user' && meta?.reply_hint && (
          <Button
            variant="ghost"
            className="mb-2 h-auto w-full justify-start gap-2 px-1 py-1 text-left text-[10px] font-normal whitespace-normal text-amber-700"
            onClick={() => setDraft(meta.reply_hint!.replace(/^测试信息：/, ''))}
          >
            <CircleHelp className="size-3.5 shrink-0" />
            <span>{meta.reply_hint}</span>
          </Button>
        )}
        <form
          className="rounded-xl border border-input bg-background p-3 shadow-xs transition-shadow focus-within:border-ring focus-within:ring-2 focus-within:ring-ring/15"
          onSubmit={(event) => {
            event.preventDefault()
            submit()
          }}
        >
          <Textarea
            className="max-h-40 min-h-10 resize-none overflow-y-auto rounded-none border-0 px-1 py-1 text-sm shadow-none focus-visible:ring-0 md:text-[13px]"
            aria-label="输入问题或补充信息"
            placeholder={
              canContinue
                ? '请先继续本轮，或新建会话'
                : status === 'waiting_user'
                  ? '补充助手需要的信息…'
                  : '描述问题，或提供你看到的报错…'
            }
            value={draft}
            disabled={pending || canContinue || !meta}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault()
                submit()
              }
            }}
            rows={2}
          />
          <div className="mt-1 flex items-center justify-between">
            <span className="flex items-center gap-2 pl-1 font-mono text-[10px] text-muted-foreground">
              <span className="size-1.5 rounded-xs bg-slate-400" />
              {meta?.model ?? '尚未连接'}
            </span>
            {running ? (
              <Button
                type="button"
                variant="secondary"
                size="sm"
                disabled={pending}
                onClick={() => actions.cancelRun()}
              >
                <Square className="size-3" fill="currentColor" />
                停止
              </Button>
            ) : (
              <Button
                type="submit"
                size="icon"
                className="size-8"
                aria-label="发送消息"
                disabled={!draft.trim() || pending || canContinue || !meta}
              >
                {pending ? (
                  <LoaderCircle className="size-4 animate-spin" />
                ) : (
                  <ArrowUp className="size-4" />
                )}
              </Button>
            )}
          </div>
        </form>
        <div className="mt-2.5 flex justify-between gap-2 text-[9px] text-muted-foreground">
          <span className="flex items-center gap-1.5">
            <span
              className={cn(
                'size-1 rounded-full',
                connection === 'connected' ? 'bg-emerald-500' : 'bg-slate-400',
              )}
            />
            {!active
              ? '发送后自动创建会话'
              : connection === 'connected'
                ? '会话已连接 · 事件持久化保存'
                : '连接恢复中 · 后台运行不受影响'}
          </span>
          <span className="hidden xl:inline">Enter 发送 · Shift Enter 换行</span>
        </div>
      </div>
    </section>
  )
}
