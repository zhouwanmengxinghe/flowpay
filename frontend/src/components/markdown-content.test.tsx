// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it } from 'vitest'
import { MarkdownContent, JsonContent } from './markdown-content'

afterEach(cleanup)
it('updates an unfinished JSON fence in place and renders the completed Markdown', () => {
  const { container, rerender } = render(
    <MarkdownContent text={'## 结果\n\n```json\n{"ok":'} streaming />,
  )
  const pre = container.querySelector('pre')
  expect(pre?.textContent).toBe('{"ok":\n')
  rerender(
    <MarkdownContent
      text={'## 结果\n\n```json\n{"ok":true}\n```\n\n**完成**\n\n|状态|数量|\n|---|---|\n|成功|1|'}
    />,
  )
  expect(container.querySelector('pre')).toBe(pre)
  expect(container.querySelector('.hljs-literal')?.textContent).toBe('true')
  expect(container.querySelector('strong')?.textContent).toBe('完成')
  expect(screen.getByRole('table')).toBeTruthy()
  expect(screen.queryByLabelText('正在生成')).toBeNull()
})
it('does not execute HTML, unsafe links or automatically load images', () => {
  const { container } = render(
    <MarkdownContent
      text={
        '<script>alert(1)</script>\n\n[链接](javascript:alert%281%29)\n\n![图片](https://example.com/pixel.png)\n\n```unknown-language\n<script>literal</script>\n```'
      }
    />,
  )
  expect(container.querySelector('script')).toBeNull()
  expect(container.querySelector('img')).toBeNull()
  expect(container.querySelector('a')?.getAttribute('href')).not.toMatch(/^javascript:/)
  expect(container.querySelector('pre')?.textContent).toContain('<script>literal</script>')
})
it('copies highlighted JSON as plain text and preserves embedded backticks', async () => {
  const user = userEvent.setup()
  const data = { content: '```\n## literal', ok: true }
  render(<JsonContent data={data} />)
  await user.click(screen.getByRole('button', { name: '复制代码' }))
  expect(await navigator.clipboard.readText()).toBe(JSON.stringify(data, null, 2) + '\n')
  expect(screen.getByRole('status').textContent).toBe('已复制')
  expect(screen.queryByRole('heading')).toBeNull()
})
