/* 将运行状态映射为统一徽章，保持会话列表与详情的视觉语义一致。 */
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import { statusLabels } from '@/state'

export function StatusBadge({ status }: { status: string }) {
  return (
    <Badge
      variant="outline"
      className={cn('shrink-0 gap-1.5 py-1 text-[10px] font-normal', {
        'border-blue-200 bg-blue-50 text-blue-700': status === 'running',
        'border-emerald-200 bg-emerald-50 text-emerald-700': status === 'completed',
        'border-amber-200 bg-amber-50 text-amber-700': status === 'waiting_user',
        'border-red-200 bg-red-50 text-red-700': status === 'failed',
        'bg-muted text-muted-foreground': ['idle', 'canceled', 'interrupted'].includes(status),
      })}
    >
      <span className="size-1.5 rounded-full bg-current" />
      {statusLabels[status] ?? status}
    </Badge>
  )
}
