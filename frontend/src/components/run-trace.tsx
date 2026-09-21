import { useRef, useState } from 'react'
import {
  Activity,
  Braces,
  ArrowDownToLine,
  ArrowDown,
  ChevronDown,
  ChevronsDownUp,
  Search,
  Maximize2,
  Minimize2,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { TraceMessageCard } from '@/components/trace-message'
import { StatusBadge } from '@/components/status-badge'
import { contentText, type TraceEntry } from '@/trace'
import { time, type Inspect } from '@/lib/display'
import type { TraceProjection } from '@/hooks/use-trace'

const kinds = [
  ['all', '全部'],
  ['call', '模型调用'],
  ['tool', '工具'],
  ['event', '检索等事件'],
  ['error', '异常'],
] as const

function matches(entry: TraceEntry, kind: string, query: string): boolean {
  const category =
    kind === 'all' ||
    entry.kind === kind ||
    (kind === 'error' && (entry.status === 'error' || entry.status === 'interrupted'))
  return (
    category &&
    `${entry.label} ${contentText(entry.message?.content)} ${contentText(entry.arguments)}`
      .toLowerCase()
      .includes(query.trim().toLowerCase())
  )
}

/** 筛选和折叠只影响展示，不更改投影或原始日志。 */
export function RunTrace({
  sessionId,
  eventCount,
  projection,
  runFilter,
  setRunFilter,
  wide,
  setWide,
  inspect,
}: {
  sessionId: string | null
  eventCount: number
  projection: TraceProjection
  runFilter: string
  setRunFilter: (value: string) => void
  wide: boolean
  setWide: (value: boolean) => void
  inspect: Inspect
}) {
  const { runs, visibleRuns, visibleEvents, tokens } = projection
  const [kind, setKind] = useState('all')
  const [query, setQuery] = useState('')
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [expandDefault, setExpandDefault] = useState(true)
  const bottom = useRef<HTMLDivElement>(null)
  const shown = visibleRuns.map((run) => ({
    run,
    entries: run.entries.filter((entry) => matches(entry, kind, query)),
  }))
  const count = shown.reduce((sum, item) => sum + item.entries.length, 0)
  const filtered = kind !== 'all' || !!query.trim()
  return (
    <aside aria-label="运行轨迹" className="flex min-h-0 flex-1 flex-col bg-muted/40">
      <div className="shrink-0 space-y-3 border-b bg-background/80 px-4 pt-5 pb-3">
        <div className="flex items-center justify-between">
          <h2 className="flex items-center gap-2 text-xs font-semibold">
            <Activity className="size-4 text-primary" />
            运行轨迹
          </h2>
          <span className="text-[10px] text-muted-foreground">
            {runs.length} 轮 · {tokens ? `${tokens.toLocaleString()} tokens` : '暂无用量'}
          </span>
        </div>
        <div className="flex items-center gap-1">
          <Select value={runFilter} onValueChange={setRunFilter}>
            <SelectTrigger
              aria-label="筛选运行"
              className="h-8 min-w-0 flex-1 bg-background text-xs"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部运行 ({runs.length})</SelectItem>
              {runs.map((run) => (
                <SelectItem key={run.id} value={run.id}>
                  Run {String(run.number).padStart(2, '0')} · {time(run.startedAt)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button
            variant="ghost"
            size="icon"
            className="hidden size-8 md:inline-flex"
            title={wide ? '恢复侧栏宽度' : '加宽以展开完整上下文'}
            aria-label={wide ? '恢复侧栏宽度' : '加宽轨迹'}
            aria-pressed={wide}
            onClick={() => setWide(!wide)}
          >
            {wide ? <Minimize2 className="size-4" /> : <Maximize2 className="size-4" />}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="size-8"
            title="查看原始日志"
            aria-label="查看原始日志"
            disabled={!eventCount}
            onClick={() => inspect('会话原始事件', visibleEvents)}
          >
            <Braces className="size-4" />
          </Button>
          <Button variant="ghost" size="icon" className="size-8" asChild>
            <a
              href={sessionId ? `/api/sessions/${encodeURIComponent(sessionId)}/export` : undefined}
              aria-disabled={!sessionId}
              tabIndex={sessionId ? 0 : -1}
              className={!sessionId ? 'pointer-events-none opacity-40' : ''}
              title="导出会话 JSONL"
              aria-label="导出会话 JSONL"
            >
              <ArrowDownToLine className="size-4" />
            </a>
          </Button>
        </div>
        <label className="flex items-center gap-2 rounded-lg border bg-background px-2.5">
          <Search className="size-3.5 shrink-0 text-muted-foreground" />
          <input
            type="search"
            aria-label="搜索轨迹"
            placeholder="搜索消息、工具或事件…"
            className="h-8 min-w-0 flex-1 bg-transparent text-xs outline-none"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <div className="flex flex-wrap gap-1" aria-label="轨迹类型">
          {kinds.map(([value, label]) => (
            <Button
              key={value}
              variant={kind === value ? 'secondary' : 'ghost'}
              size="sm"
              aria-pressed={kind === value}
              className="h-7 px-2 text-[10px]"
              onClick={() => setKind(value)}
            >
              {label}
            </Button>
          ))}
        </div>
        <div className="flex items-center justify-between text-[10px] text-muted-foreground">
          <span>
            {count} 个步骤{filtered && ' · 已筛选'}
          </span>
          <Button
            variant="ghost"
            size="sm"
            className="h-6 gap-1 px-1 text-[10px]"
            onClick={() => {
              setExpanded({})
              setExpandDefault(!expandDefault)
            }}
          >
            <ChevronsDownUp className="size-3" />
            {expandDefault ? '折叠全部轮次' : '展开全部轮次'}
          </Button>
        </div>
      </div>
      <div className="thin-scrollbar min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
        {!runs.length && (
          <div className="my-14 text-center">
            <Activity className="mx-auto mb-4 size-7 text-muted-foreground/50" />
            <h3 className="text-xs font-medium">每一步，都有记录</h3>
            <p className="mt-3 text-[11px] leading-6 text-muted-foreground">
              发起对话后，在这里查看模型调用、
              <br />
              工具输入输出和检索记录。
            </p>
          </div>
        )}
        {!!runs.length && !count && filtered && (
          <div className="py-10 text-center">
            <p className="text-xs text-muted-foreground">没有匹配的步骤</p>
            <Button
              variant="link"
              size="sm"
              onClick={() => {
                setKind('all')
                setQuery('')
              }}
            >
              清除筛选
            </Button>
          </div>
        )}
        {shown
          .filter(({ entries }) => !filtered || entries.length)
          .map(({ run, entries }) => (
            <Collapsible
              key={run.id}
              open={expanded[run.id] ?? expandDefault}
              onOpenChange={(open) => setExpanded((values) => ({ ...values, [run.id]: open }))}
            >
              <div className="mb-2 flex items-center gap-1">
                <CollapsibleTrigger asChild>
                  <Button
                    variant="ghost"
                    className="group h-auto min-w-0 flex-1 justify-start gap-2 px-1 py-2 text-xs"
                  >
                    <ChevronDown className="size-3 shrink-0 transition-transform group-data-[state=closed]:-rotate-90" />
                    <span className="font-mono text-[10px]">
                      RUN {String(run.number).padStart(2, '0')}
                    </span>
                    <time className="text-[10px] font-normal text-muted-foreground">
                      {time(run.startedAt)}
                    </time>
                    <span className="ml-auto">
                      <StatusBadge status={run.status} />
                    </span>
                  </Button>
                </CollapsibleTrigger>
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-7 shrink-0"
                  aria-label={`查看 Run ${run.number} 配置`}
                  title="运行配置与输入"
                  onClick={() => inspect('运行配置与输入', run.startEvent)}
                >
                  <Braces className="size-3" />
                </Button>
              </div>
              <CollapsibleContent>
                {/* 时间轴：序号沿竖线排下来，工具调用缩进到产生它的那次模型调用下面。 */}
                <div className="relative ml-3 space-y-2 pl-6 before:absolute before:top-1 before:bottom-1 before:left-0 before:w-px before:bg-border">
                  {run.resumed && (
                    <p className="py-1 text-[10px] text-amber-700">从中断轮次重新开始</p>
                  )}
                  {entries.map((entry, index) => (
                    <TraceMessageCard
                      key={entry.id}
                      entry={entry}
                      step={index + 1}
                      nested={
                        !!entry.parentId && entries.some((item) => item.id === entry.parentId)
                      }
                      inspect={inspect}
                    />
                  ))}
                  {run.endEvent && (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 text-[10px] text-muted-foreground"
                      onClick={() => inspect('运行结束事件', run.endEvent)}
                    >
                      查看本轮结束信息
                    </Button>
                  )}
                </div>
              </CollapsibleContent>
            </Collapsible>
          ))}
        <div ref={bottom} />
      </div>
      <div className="flex shrink-0 items-center justify-between border-t bg-background/80 px-4 py-2">
        <span className="text-[10px] text-muted-foreground">
          {eventCount.toLocaleString()} 条原始事件
        </span>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 gap-1 text-[10px]"
          disabled={!runs.length}
          onClick={() => {
            setKind('all')
            setQuery('')
            setRunFilter('all')
            setExpanded({})
            setExpandDefault(true)
            requestAnimationFrame(() => bottom.current?.scrollIntoView({ block: 'end' }))
          }}
        >
          <ArrowDown className="size-3" />
          定位最新
        </Button>
      </div>
    </aside>
  )
}
