/* 用详情弹窗展示原始事件或引用内容，便于核对可见消息的来源。 */
import { JsonContent } from '@/components/markdown-content'
import { useRef } from 'react'
import { X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

export function EventDialog({
  title,
  data,
  close,
}: {
  title: string
  data: unknown
  close: () => void
}) {
  const previousFocus = useRef(document.activeElement as HTMLElement | null)
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) close()
      }}
    >
      <DialogContent
        className="flex max-h-[85dvh] flex-col gap-0 overflow-hidden p-0 sm:max-w-3xl"
        showCloseButton={false}
        onCloseAutoFocus={(event) => {
          event.preventDefault()
          previousFocus.current?.focus()
        }}
      >
        <DialogHeader className="border-b px-6 py-4 pr-14 text-left">
          <DialogTitle className="text-base">{title}</DialogTitle>
          <DialogDescription className="text-xs">查看此会话已记录的原始内容。</DialogDescription>
        </DialogHeader>
        <DialogClose asChild>
          <Button
            variant="ghost"
            size="icon"
            className="absolute top-3 right-3"
            aria-label="关闭详情"
          >
            <X />
          </Button>
        </DialogClose>
        <div className="thin-scrollbar min-h-0 flex-1 overflow-auto px-6 py-3">
          <JsonContent data={data} />
        </div>
      </DialogContent>
    </Dialog>
  )
}
