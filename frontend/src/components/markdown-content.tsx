import {
  Children,
  isValidElement,
  memo,
  useDeferredValue,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import Markdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import { Check, Copy } from 'lucide-react'
import { Button } from '@/components/ui/button'

function textContent(children: ReactNode): string {
  return Children.toArray(children)
    .map((child): string => {
      if (typeof child === 'string' || typeof child === 'number') return String(child)
      return isValidElement<{ children?: ReactNode }>(child)
        ? textContent(child.props.children)
        : ''
    })
    .join('')
}

function CodeBlock({ children }: { children?: ReactNode }) {
  const [copied, setCopied] = useState('')
  const code = textContent(children)
  useEffect(() => {
    setCopied('')
  }, [code])
  const child = Children.toArray(children)[0]
  const language = isValidElement<{ className?: string }>(child)
    ? (/language-([^\s]+)/.exec(child.props.className ?? '')?.[1] ?? 'text')
    : 'text'
  const attempt = useRef(0)
  return (
    <div className="markdown-code my-3 min-w-0 overflow-hidden rounded-xl border bg-slate-50">
      <div className="flex items-center justify-between border-b px-3 py-1.5 text-[10px] text-muted-foreground">
        <span className="font-mono">{language}</span>
        <Button
          variant="ghost"
          size="sm"
          className="h-6 gap-1.5 px-2 text-[10px]"
          aria-label="复制代码"
          onClick={async () => {
            const id = ++attempt.current
            try {
              await navigator.clipboard.writeText(code)
              if (id === attempt.current) setCopied('已复制')
            } catch {
              if (id === attempt.current) setCopied('复制失败，请手动选择')
            }
          }}
        >
          {copied === '已复制' ? <Check className="size-3" /> : <Copy className="size-3" />}
          <span role="status">{copied || '复制'}</span>
        </Button>
      </div>
      <pre className="thin-scrollbar max-h-[28rem] overflow-auto p-3 text-[11px] leading-6">
        {children}
      </pre>
    </div>
  )
}

const components: Components = {
  pre: CodeBlock,
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
  // 模型提供的图片不自动加载，保留描述，避免流式渲染触发额外网络请求。
  img: ({ alt }) => <span className="text-muted-foreground">[图片：{alt || '未提供描述'}]</span>,
  table: ({ children }) => (
    <div className="my-3 overflow-x-auto">
      <table>{children}</table>
    </div>
  ),
}
const plugins = [remarkGfm]
const highlights = [rehypeHighlight]
const RenderedMarkdown = memo(function RenderedMarkdown({ text }: { text: string }) {
  return (
    <Markdown remarkPlugins={plugins} rehypePlugins={highlights} components={components} skipHtml>
      {text}
    </Markdown>
  )
})

/** 完整消息与流式片段共用解析器；未闭合代码围栏可先显示，后续片段继续更新。 */
export function MarkdownContent({
  text,
  streaming = false,
}: {
  text: string
  streaming?: boolean
}) {
  const deferred = useDeferredValue(text)
  return (
    <div className="markdown-content min-w-0 text-[13px] leading-7" aria-busy={streaming}>
      <RenderedMarkdown text={streaming ? deferred : text} />
      {streaming && (
        <span
          aria-label="正在生成"
          className="mt-1 inline-block h-3 w-1 animate-pulse bg-primary"
        />
      )}
    </div>
  )
}

/** 只格式化展示数据，不解析或改写原始事件。围栏长度避免内容意外结束代码块。 */
export function JsonContent({ data }: { data: unknown }) {
  const code = typeof data === 'string' ? data : (JSON.stringify(data, null, 2) ?? 'null')
  const fence = '`'.repeat(Math.max(3, ...[...code.matchAll(/`+/g)].map((m) => m[0].length + 1)))
  let language = 'json'
  if (typeof data === 'string') {
    try {
      JSON.parse(data)
    } catch {
      language = 'text'
    }
  }
  return <MarkdownContent text={`${fence}${language}\n${code}\n${fence}`} />
}
