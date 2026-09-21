import { useState } from 'react'
import {
  AlertCircle,
  Layers3,
  Plus,
  MessageSquare,
  Trash2,
  ListChecks,
  LoaderCircle,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { cn } from '@/lib/utils'
import { statusLabels } from '@/state'
import type { Session } from '@/state'

/** 距今几天：0 是今天，1 是昨天，NaN 表示时间无法解析。 */
function daysAgo(value: string): number {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return NaN
  const midnight = (at: Date) => new Date(at.getFullYear(), at.getMonth(), at.getDate()).getTime()
  return Math.round((midnight(new Date()) - midnight(date)) / 86_400_000)
}

// 侧栏最常用来找"刚才那一条"，所以今天的会话给到分钟，更早的才退回日期。
function updatedLabel(value: string): string {
  const days = daysAgo(value)
  if (Number.isNaN(days)) return ''
  const date = new Date(value)
  if (days <= 0) return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  if (days === 1) return '昨天'
  return date.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
}

function groupLabel(value: string): string {
  const days = daysAgo(value)
  if (Number.isNaN(days)) return '更早'
  if (days <= 0) return '今天'
  if (days === 1) return '昨天'
  if (days <= 7) return '最近七天'
  return '更早'
}

/** 按更新时间分段；顺序沿用传入的排序，不重新排。 */
function groupSessions(sessions: Session[]): { label: string; items: Session[] }[] {
  const groups: { label: string; items: Session[] }[] = []
  for (const session of sessions) {
    const label = groupLabel(session.updated_at)
    const last = groups.at(-1)
    if (last?.label === label) last.items.push(session)
    else groups.push({ label, items: [session] })
  }
  return groups
}

// 多选与确认属于导航；删除请求、当前会话及列表一致性由目录控制器负责。
export function SessionNavigation({
  sessions,
  activeId,
  onSelect,
  onCreate,
  onDelete,
  disabled,
  deleting,
  deleteError,
}: {
  sessions: Session[]
  activeId: string | null
  onSelect: (id: string) => void
  onCreate: () => void
  onDelete: (ids: string[]) => Promise<boolean>
  disabled: boolean
  deleting: boolean
  deleteError: string
}) {
  const [managing, setManaging] = useState(false)
  const [selected, setSelected] = useState<string[]>([])
  const [confirmation, setConfirmation] = useState<Session[] | null>(null)
  const [failed, setFailed] = useState(false)
  const eligible = sessions.filter((item) => item.status !== 'running')
  const chosen = selected.filter((id) => eligible.some((item) => item.id === id))
  function confirm(items: Session[]) {
    setFailed(false)
    setConfirmation(items)
  }
  function toggle(id: string) {
    setSelected((ids) => (ids.includes(id) ? ids.filter((item) => item !== id) : [...ids, id]))
  }
  return (
    <>
      <div className="flex items-center gap-3 px-2 pb-7 pt-2">
        <span className="flex size-10 items-center justify-center rounded-xl bg-slate-900 text-white shadow-sm">
          <Layers3 className="size-5" />
        </span>
        <div>
          <span className="text-lg font-semibold tracking-tight">
            FlowPay <span className="font-normal text-muted-foreground">Lab</span>
          </span>
          <p className="mt-0.5 text-[10px] tracking-[0.12em] text-muted-foreground">
            技术支持 · 调查工作台
          </p>
        </div>
      </div>
      <Button
        className="h-11 w-full justify-start gap-2.5 rounded-xl shadow-sm"
        onClick={onCreate}
        disabled={disabled}
      >
        <Plus className="size-4" />
        新建会话
      </Button>
      <div className="mt-7 mb-3 flex h-8 items-center justify-between px-2">
        <span className="flex items-center gap-2 text-xs font-medium">
          {managing ? '选择会话' : '会话记录'}
          <span className="rounded-md bg-black/5 px-1.5 py-0.5 text-[10px] tabular-nums text-muted-foreground">
            {sessions.length}
          </span>
        </span>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 gap-1.5 px-2 text-xs text-muted-foreground"
          disabled={disabled || !sessions.length}
          onClick={() => {
            setManaging(!managing)
            setSelected([])
          }}
        >
          {!managing && <ListChecks className="size-3.5" />}
          {managing ? '完成' : '批量管理'}
        </Button>
      </div>
      {managing && (
        <p className="mb-3 px-2 text-[11px] leading-5 text-muted-foreground">
          选择要删除的会话，正在运行的会话不可选。
        </p>
      )}
      <nav aria-label="会话记录" className="thin-scrollbar min-h-0 flex-1 overflow-y-auto pb-2">
        {groupSessions(sessions).map((group) => (
          <section key={group.label} className="mb-2 space-y-0.5">
            <h3 className="px-3 pt-3 pb-1 text-[10px] font-medium text-muted-foreground">
              {group.label}
            </h3>
            {group.items.map((session) => {
              const checked = chosen.includes(session.id)
              const locked =
                disabled || session.status === 'running' || (!checked && chosen.length >= 100)
              const current = managing ? checked : session.id === activeId
              return (
                <div
                  key={session.id}
                  className={cn(
                    'group relative flex items-center rounded-xl transition-colors',
                    // 选中态用左侧主色竖条，和 hover 的浅底区分开。
                    current
                      ? 'bg-accent before:absolute before:top-2 before:bottom-2 before:left-0 before:w-0.5 before:rounded-full before:bg-primary'
                      : 'hover:bg-background/80',
                  )}
                >
                  {managing && (
                    <input
                      type="checkbox"
                      className="ml-3 size-4 shrink-0 cursor-pointer accent-primary disabled:cursor-not-allowed"
                      aria-label={`选择会话：${session.title}`}
                      checked={checked}
                      disabled={locked}
                      onChange={() => toggle(session.id)}
                    />
                  )}
                  <Button
                    variant="ghost"
                    aria-current={!managing && session.id === activeId ? 'page' : undefined}
                    disabled={managing ? locked : deleting}
                    className="h-auto min-w-0 flex-1 items-start justify-start rounded-xl px-3 py-2.5 text-left whitespace-normal hover:bg-transparent"
                    onClick={() => (managing ? toggle(session.id) : onSelect(session.id))}
                  >
                    <span className="min-w-0 flex-1">
                      {/* 标题是用户问的第一句话，给两行才认得出是哪一条。 */}
                      <span
                        className={cn(
                          'block text-xs leading-5 font-medium break-words',
                          'line-clamp-2',
                          !managing && 'pr-6',
                        )}
                      >
                        {session.title}
                      </span>
                      <span className="mt-1.5 flex items-center gap-1.5 text-[10px] font-normal text-muted-foreground">
                        {/* 已完成是常态，不占位置；异常和进行中才出文字。 */}
                        {session.status !== 'completed' && (
                          <>
                            <span
                              className={cn(
                                'size-1.5 rounded-full bg-slate-300',
                                session.status === 'running' && 'animate-pulse bg-blue-500',
                                session.status === 'waiting_user' && 'bg-amber-500',
                                ['failed', 'interrupted'].includes(session.status) &&
                                  'bg-destructive',
                              )}
                            />
                            {statusLabels[session.status] ?? session.status}
                          </>
                        )}
                        <span className="ml-auto tabular-nums">
                          {updatedLabel(session.updated_at)}
                        </span>
                      </span>
                    </span>
                  </Button>
                  {!managing && (
                    <Button
                      variant="ghost"
                      size="icon"
                      // 绝对定位，标题因此能用满整行宽度；只在悬停或聚焦时浮出来。
                      className="absolute top-1.5 right-1 size-7 rounded-lg text-muted-foreground hover:bg-red-50 hover:text-destructive focus-visible:opacity-100 [@media(hover:hover)]:opacity-0 [@media(hover:hover)]:group-hover:opacity-100 [@media(hover:hover)]:group-focus-within:opacity-100"
                      aria-label={`删除会话：${session.title}`}
                      title={session.status === 'running' ? '请先停止运行再删除' : '删除会话'}
                      disabled={disabled || session.status === 'running'}
                      onClick={() => confirm([session])}
                    >
                      <Trash2 className="size-3.5" />
                    </Button>
                  )}
                </div>
              )
            })}
          </section>
        ))}
        {!sessions.length && (
          <div className="flex flex-col items-center px-4 py-12 text-center">
            <span className="mb-4 flex size-12 items-center justify-center rounded-2xl border border-dashed bg-background text-muted-foreground">
              <MessageSquare className="size-5" />
            </span>
            <p className="text-sm font-medium">还没有会话</p>
            <p className="mt-2 text-xs leading-6 text-muted-foreground">
              新建一个会话，开始调查。
              <br />
              历史记录会保存在这里。
            </p>
          </div>
        )}
      </nav>
      {managing && (
        <div className="mt-3 shrink-0 rounded-xl border bg-background p-3 shadow-sm">
          <div className="mb-3 flex items-center justify-between">
            <span className="text-xs text-muted-foreground">
              已选择{' '}
              <strong className="font-semibold tabular-nums text-foreground">
                {chosen.length}
              </strong>{' '}
              个会话
            </span>
            <Button
              variant="ghost"
              size="sm"
              className="h-6 px-1 text-xs text-primary"
              disabled={disabled || !eligible.length}
              onClick={() =>
                setSelected(
                  chosen.length === Math.min(eligible.length, 100)
                    ? []
                    : eligible.slice(0, 100).map((item) => item.id),
                )
              }
            >
              {chosen.length === Math.min(eligible.length, 100) && chosen.length
                ? '取消全选'
                : '全选'}
            </Button>
          </div>
          <Button
            variant="destructive"
            className="h-9 w-full rounded-lg text-xs"
            aria-label={`删除所选 (${chosen.length})`}
            disabled={disabled || !chosen.length}
            onClick={() => confirm(sessions.filter((item) => chosen.includes(item.id)))}
          >
            <Trash2 className="size-3.5" />
            删除所选
            {chosen.length > 0 && (
              <span className="rounded bg-white/20 px-1.5 text-[10px]">{chosen.length}</span>
            )}
          </Button>
          {eligible.length > 100 && (
            <p className="mt-2 text-[10px] text-muted-foreground">每次最多选择 100 个会话</p>
          )}
        </div>
      )}
      <div className="mt-4 flex shrink-0 items-center gap-2.5 border-t px-2 py-4 text-xs">
        <span className="flex size-8 items-center justify-center rounded-full border bg-background font-semibold text-muted-foreground">
          F
        </span>
        <span>
          本地工作空间
          <span className="mt-1 block text-[10px] text-muted-foreground">会话持续保存在本机</span>
        </span>
        <span className="ml-auto size-1.5 rounded-full bg-emerald-500" />
      </div>
      <Dialog
        open={confirmation !== null}
        onOpenChange={(open) => {
          if (!open && !deleting) setConfirmation(null)
        }}
      >
        <DialogContent
          showCloseButton={false}
          className="max-h-[calc(100dvh-2rem)] gap-0 overflow-y-auto rounded-2xl p-0 sm:max-w-md"
        >
          <div className="p-6 pb-5">
            <span className="mb-5 flex size-11 items-center justify-center rounded-xl bg-red-50 text-destructive">
              <Trash2 className="size-5" />
            </span>
            <DialogHeader className="text-left">
              <DialogTitle>
                {confirmation?.length === 1
                  ? '删除这个会话？'
                  : `删除 ${confirmation?.length ?? 0} 个会话？`}
              </DialogTitle>
              <DialogDescription className="pt-1 text-xs leading-6">
                删除后，消息和运行轨迹将一并清除。
                <br />
                此操作无法撤销。
              </DialogDescription>
            </DialogHeader>
            <div className="mt-4 max-h-44 space-y-2 overflow-y-auto rounded-xl border bg-muted/50 p-3">
              {confirmation?.map((item) => (
                <div key={item.id} className="flex items-center gap-2.5 text-xs">
                  <MessageSquare className="size-3.5 shrink-0 text-muted-foreground" />
                  <span className="min-w-0 truncate" title={item.title}>
                    {item.title}
                  </span>
                </div>
              ))}
            </div>
            {failed && deleteError && (
              <div
                role="alert"
                className="mt-4 flex items-start gap-2 rounded-lg bg-red-50 p-3 text-xs leading-5 text-destructive"
              >
                <AlertCircle className="mt-0.5 size-4 shrink-0" />
                {deleteError}
              </div>
            )}
          </div>
          <DialogFooter className="border-t bg-muted/40 px-6 py-4">
            <Button
              autoFocus
              variant="outline"
              disabled={deleting}
              onClick={() => setConfirmation(null)}
            >
              取消
            </Button>
            <Button
              variant="destructive"
              disabled={disabled || !confirmation}
              onClick={async () => {
                if (!confirmation) return
                setFailed(false)
                if (await onDelete(confirmation.map((item) => item.id))) {
                  setConfirmation(null)
                  setSelected([])
                  setManaging(false)
                } else setFailed(true)
              }}
            >
              {deleting && <LoaderCircle className="size-4 animate-spin" />}
              {deleting ? '删除中…' : '确认删除'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
