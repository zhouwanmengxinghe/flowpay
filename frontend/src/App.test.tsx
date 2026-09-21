/* 前端回归测试：验证 App 的状态或交互；模拟事件用于复现后端协议。 */
// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import type { TraceEvent } from './state'

class FakeEventSource extends EventTarget {
  static instances: FakeEventSource[] = []
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  closed = false
  constructor(public url: string) {
    super()
    FakeEventSource.instances.push(this)
    queueMicrotask(() => this.onopen?.())
  }
  close() {
    this.closed = true
  }
  push(event: TraceEvent) {
    this.dispatchEvent(new MessageEvent('trace', { data: JSON.stringify(event) }))
  }
}
const trace = (
  seq: number,
  type: string,
  payload: Record<string, any>,
  call: string | null = null,
): TraceEvent => ({
  seq,
  type,
  payload,
  call_id: call,
  parent_call_id: null,
  schema_version: 1,
  session_id: 'session_test',
  run_id: type === 'session.created' ? null : 'run_test',
  event_id: `evt_${seq}`,
  timestamp: '2026-09-09T01:00:00Z',
})
let recorded: TraceEvent[]
let requests: { path: string; body: any }[]

beforeEach(() => {
  recorded = [trace(1, 'session.created', { workflow: { id: 'fixture', version: '1' } })]
  requests = []
  const storage = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
    removeItem: (key: string) => storage.delete(key),
    clear: () => storage.clear(),
  })
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
  HTMLElement.prototype.scrollIntoView = vi.fn()
  HTMLElement.prototype.hasPointerCapture = vi.fn(() => false)
  HTMLElement.prototype.setPointerCapture = vi.fn()
  HTMLElement.prototype.releasePointerCapture = vi.fn()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: string, init?: RequestInit) => {
      if (init?.body) requests.push({ path, body: JSON.parse(init.body as string) })
      let body: unknown = {}
      if (path === '/api/meta')
        body = {
          workflow: { id: 'fixture', label: '测试工作流', description: '模拟数据' },
          model: 'fixture-model',
          examples: ['没有收到通知'],
          reply_hint: '测试信息：acct_demo production req_demo_503',
        }
      else if (path === '/api/sessions' && !init)
        body = [
          {
            id: 'session_test',
            title: '订单未通知',
            status: 'waiting_user',
            updated_at: '2026-09-09T01:00:00Z',
          },
        ]
      else if (path === '/api/sessions' && init)
        body = { id: 'session_new', title: '新会话', status: 'idle' }
      else if (path.includes('/events')) body = recorded
      else body = { id: 'run_new', status: 'running' }
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }),
  )
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function mounted() {
  render(<App />)
  await waitFor(() => expect(FakeEventSource.instances.length).toBe(1))
}

describe('chat workspace interactions', () => {
  it('submits a message and displays persisted SSE output', async () => {
    await mounted()
    const user = userEvent.setup()
    await user.type(screen.getByRole('textbox', { name: '输入问题或补充信息' }), '支付通知没收到')
    await user.click(screen.getByRole('button', { name: '发送消息' }))
    expect(requests.find((request) => request.path.endsWith('/messages'))?.body.content).toBe(
      '支付通知没收到',
    )
    expect(requests[0].body.request_id).toBeTruthy()
    await act(async () => {
      const stream = FakeEventSource.instances[0]
      stream.push(trace(2, 'message.user', { content: '支付通知没收到' }))
      stream.push(trace(3, 'run.started', { input: '支付通知没收到' }))
      stream.push(trace(4, 'message.assistant', { content: '请提供请求编号', evidence: [] }))
      stream.push(trace(5, 'run.finished', { status: 'waiting_user', state: {} }))
    })
    expect(
      within(screen.getByRole('region', { name: '客服对话' })).getByText('请提供请求编号'),
    ).toBeTruthy()
    await user.click(screen.getByRole('button', { name: /测试信息：acct_demo/ }))
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe(
      'acct_demo production req_demo_503',
    )
  })

  it('restores history, renders tool details and opens cited evidence', async () => {
    recorded.push(
      trace(2, 'message.user', { content: '支付通知没收到' }),
      trace(3, 'run.started', { input: '支付通知没收到' }),
      trace(
        4,
        'tool.started',
        { name: 'get_request', arguments: { request_id: 'req_demo_503' } },
        'tool_1',
      ),
      trace(
        5,
        'tool.result',
        { name: 'get_request', result: { status: 503 }, duration_ms: 12 },
        'tool_1',
      ),
      trace(6, 'message.assistant', {
        content: '支付创建失败',
        evidence: [{ id: 'evidence_1', title: '请求记录', content: '503 原始记录' }],
      }),
      trace(7, 'run.finished', { status: 'completed', state: {} }),
    )
    await mounted()
    expect(
      within(screen.getByRole('region', { name: '客服对话' })).getByText('支付创建失败'),
    ).toBeTruthy()
    const user = userEvent.setup()
    await user.click(screen.getByText('get_request · tool'))
    expect(
      [...document.querySelectorAll('pre')].some((node) =>
        node.textContent?.includes('"request_id": "req_demo_503"'),
      ),
    ).toBe(true)
    await user.click(screen.getByText(/本轮资料/))
    await user.click(screen.getByRole('button', { name: '请求记录' }))
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(screen.getByRole('dialog').textContent).toContain('503 原始记录')
    await user.click(screen.getByRole('button', { name: '关闭详情' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByRole('link', { name: '导出会话 JSONL' }).getAttribute('href')).toBe(
      '/api/sessions/session_test/export',
    )
  })

  it('stops a running session and explicitly continues an interrupted turn', async () => {
    recorded.push(trace(2, 'run.started', { input: 'question' }))
    await mounted()
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '停止' }))
    expect(requests.some((request) => request.path.endsWith('/cancel'))).toBe(true)
    await act(async () =>
      FakeEventSource.instances[0].push(trace(3, 'run.canceled', { reason: 'user_requested' })),
    )
    await user.click(screen.getByRole('button', { name: '继续本轮' }))
    expect(
      requests.find((request) => request.path.endsWith('/continue'))?.body.request_id,
    ).toBeTruthy()
  })
  it('closes the evidence dialog with Escape and restores trigger focus', async () => {
    recorded.push(
      trace(2, 'run.started', { input: 'question' }),
      trace(3, 'message.assistant', {
        content: '支付创建失败',
        evidence: [{ id: 'evidence_1', title: '请求记录' }],
      }),
      trace(4, 'run.finished', { status: 'completed', state: {} }),
    )
    await mounted()
    const user = userEvent.setup()
    await user.click(screen.getByText(/本轮资料/))
    const trigger = screen.getByRole('button', { name: '请求记录' })
    await user.click(trigger)
    expect(screen.getByRole('dialog', { name: '请求记录' })).toBeTruthy()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.activeElement).toBe(trigger)
  })

  it('filters the trace using the shadcn Select', async () => {
    recorded.push(
      trace(2, 'run.started', { input: 'first' }),
      trace(3, 'run.finished', { status: 'waiting_user', state: {} }),
      { ...trace(4, 'run.started', { input: 'second' }), run_id: 'run_second' },
      { ...trace(5, 'run.finished', { status: 'completed', state: {} }), run_id: 'run_second' },
    )
    await mounted()
    const user = userEvent.setup()
    expect(screen.getByText('RUN 01')).toBeTruthy()
    await user.click(screen.getByRole('combobox', { name: '筛选运行' }))
    await user.click(screen.getByRole('option', { name: /^Run 02/ }))
    expect(screen.queryByText('RUN 01')).toBeNull()
    expect(screen.getByText('RUN 02')).toBeTruthy()
  })

  it('opens the session Sheet and returns focus on Escape', async () => {
    await mounted()
    const user = userEvent.setup()
    const trigger = screen.getByRole('button', { name: '切换会话列表' })
    await user.click(trigger)
    const sheet = screen.getByRole('dialog', { name: '会话列表' })
    expect(within(sheet).getByRole('navigation', { name: '会话记录' })).toBeTruthy()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.activeElement).toBe(trigger)
  })
  it('shows the full request context inside the model call', async () => {
    recorded.push(
      trace(2, 'message.user', { content: '检查通知' }),
      trace(3, 'run.started', { input: '检查通知' }),
      trace(
        4,
        'model.request',
        {
          request: {
            body: {
              model: 'fixture',
              messages: [
                { role: 'system', content: '系统注入的规则' },
                { role: 'developer', content: '开发者约束' },
                { role: 'user', content: '检查通知' },
              ],
              tools: [{ type: 'function', function: { name: 'search_rules' } }],
            },
          },
        },
        'model_1',
      ),
      trace(
        5,
        'model.response',
        { completion: { choices: [{ message: { role: 'assistant', content: '请补充编号' } }] } },
        'model_1',
      ),
      trace(6, 'message.assistant', { content: '请补充编号' }),
      trace(7, 'run.finished', { status: 'waiting_user', state: {} }),
    )
    await mounted()
    const tracePanel = within(screen.getByRole('complementary', { name: '运行轨迹' }))
    expect(tracePanel.getByText('用户 · user')).toBeTruthy()
    expect(tracePanel.getAllByText('fixture')).toHaveLength(1)
    const user = userEvent.setup()
    // 系统提示词不再单列成会话卡片：它是第一次请求消息列表的一部分，展开调用就能看到。
    await user.click(tracePanel.getByRole('button', { name: /fixture/ }))
    expect(tracePanel.getByText(/系统注入的规则/)).toBeTruthy()
    expect(tracePanel.getByText(/开发者约束/)).toBeTruthy()
    expect(tracePanel.getByText('messages[2]')).toBeTruthy()
    expect(tracePanel.getByText(/本次提供的工具/)).toBeTruthy()
  })
  it('keeps the system prompt with the model call that sent it', async () => {
    const request = {
      request: {
        body: { model: 'fixture', messages: [{ role: 'system', content: 'session rules' }] },
      },
    }
    recorded.push(
      trace(2, 'run.started', { input: 'first' }),
      trace(3, 'model.request', request, 'model_1'),
      trace(
        4,
        'model.response',
        { completion: { choices: [{ message: { role: 'assistant', content: 'first answer' } }] } },
        'model_1',
      ),
      trace(5, 'run.finished', { status: 'waiting_user', state: {} }),
      { ...trace(6, 'run.started', { input: 'second' }), run_id: 'run_second' },
      { ...trace(7, 'model.request', request, 'model_2'), run_id: 'run_second' },
      {
        ...trace(
          8,
          'model.response',
          {
            completion: { choices: [{ message: { role: 'assistant', content: 'second answer' } }] },
          },
          'model_2',
        ),
        run_id: 'run_second',
      },
      { ...trace(9, 'run.finished', { status: 'completed', state: {} }), run_id: 'run_second' },
    )
    await mounted()
    const panel = within(screen.getByRole('complementary', { name: '运行轨迹' }))
    // 系统提示词跟着它所属的那次调用走：两个 Run 各有一次调用，各自带一份。
    expect(panel.getAllByRole('button', { name: /fixture/ })).toHaveLength(2)
    const user = userEvent.setup()
    await user.click(panel.getByRole('combobox', { name: '筛选运行' }))
    await user.click(screen.getByRole('option', { name: /^Run 02/ }))
    expect(panel.getAllByRole('button', { name: /fixture/ })).toHaveLength(1)
    expect(panel.queryByText('RUN 01')).toBeNull()
    await user.click(panel.getByRole('button', { name: /fixture/ }))
    expect(panel.getByText(/session rules/)).toBeTruthy()
  })
})

describe('会话删除', () => {
  function deletionServer(fail = false) {
    let items = [
      { id: 'session_test', title: '订单未通知', status: 'completed', updated_at: '2026-09-09' },
      { id: 'session_other', title: '退款问题', status: 'idle', updated_at: '2026-09-09' },
    ]
    const deletions: string[][] = []
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const path = String(input)
      if (init?.method === 'DELETE' || path === '/api/sessions/batch-delete') {
        if (fail)
          return new Response(JSON.stringify({ detail: '会话正在运行，请先停止' }), { status: 409 })
        const ids: string[] =
          init?.method === 'DELETE'
            ? [path.split('/').at(-1)!]
            : JSON.parse(init?.body as string).session_ids
        deletions.push(ids)
        items = items.filter((item) => !ids.includes(item.id))
        return new Response(JSON.stringify({ deleted_ids: ids }))
      }
      if (path === '/api/meta')
        return new Response(
          JSON.stringify({
            workflow: { id: 'test', label: '测试', description: '测试' },
            model: 'test',
            examples: [],
          }),
        )
      if (path === '/api/sessions') return new Response(JSON.stringify(items))
      return new Response(JSON.stringify(recorded))
    })
    return deletions
  }

  it('取消确认不请求；删除当前会话后切换并关闭旧订阅', async () => {
    const deletions = deletionServer()
    render(<App />)
    const user = userEvent.setup()
    const remove = await screen.findByRole('button', { name: '删除会话：订单未通知' })
    await user.click(remove)
    await user.click(screen.getByRole('button', { name: '取消' }))
    expect(deletions).toEqual([])
    await user.click(remove)
    await user.click(screen.getByRole('button', { name: '确认删除' }))
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: '删除会话：订单未通知' })).toBeNull(),
    )
    expect(deletions).toEqual([['session_test']])
    await waitFor(() => expect(localStorage.getItem('flowpay.session')).toBe('session_other'))
    expect(
      FakeEventSource.instances.find((stream) => stream.url.includes('session_test'))?.closed,
    ).toBe(true)
  })

  it('多选批量删除后清空列表和当前会话', async () => {
    const deletions = deletionServer()
    render(<App />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: '批量管理' }))
    await user.click(screen.getByRole('checkbox', { name: '选择会话：订单未通知' }))
    await user.click(screen.getByRole('checkbox', { name: '选择会话：退款问题' }))
    await user.click(screen.getByRole('button', { name: '删除所选 (2)' }))
    await user.click(screen.getByRole('button', { name: '确认删除' }))
    await waitFor(() => expect(localStorage.getItem('flowpay.session')).toBeNull())
    expect(deletions).toEqual([['session_test', 'session_other']])
    expect(screen.queryByRole('checkbox', { name: '选择会话：退款问题' })).toBeNull()
  })

  it('删除失败保留会话、选择和确认弹窗', async () => {
    const deletions = deletionServer(true)
    render(<App />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: '删除会话：订单未通知' }))
    await user.click(screen.getByRole('button', { name: '确认删除' }))
    expect(await screen.findByText('会话正在运行，请先停止')).toBeTruthy()
    expect(screen.getByRole('button', { name: '确认删除' })).toBeTruthy()
    expect(localStorage.getItem('flowpay.session')).toBe('session_test')
    expect(deletions).toEqual([])
  })
})

it('filters and collapses trace steps without discarding their source events', async () => {
  recorded.push(
    trace(2, 'run.started', { input: 'test' }),
    trace(3, 'message.user', { content: 'test' }),
    trace(4, 'tool.started', { name: 'lookup_payment', arguments: { id: 'p1' } }, 'tool1'),
    trace(5, 'tool.result', { result: { status: 'paid' } }, 'tool1'),
    trace(6, 'message.assistant', { content: '**查询完成**' }),
    trace(7, 'run.finished', { status: 'completed' }),
  )
  await mounted()
  const user = userEvent.setup()
  const panel = within(screen.getByRole('complementary', { name: '运行轨迹' }))
  await user.click(panel.getByRole('button', { name: '工具' }))
  expect(panel.queryByText('最终回答')).toBeNull()
  await user.click(panel.getByRole('button', { name: /lookup_payment · tool/ }))
  expect(panel.getByText('工具输出')).toBeTruthy()
  await user.click(panel.getByRole('button', { name: /查看原始事件/ }))
  expect(screen.getByRole('dialog').textContent).toContain('tool.result')
  await user.click(screen.getByRole('button', { name: '关闭详情' }))
  await user.type(panel.getByRole('searchbox', { name: '搜索轨迹' }), 'missing')
  expect(panel.getByText('没有匹配的步骤')).toBeTruthy()
  await user.click(panel.getByRole('button', { name: '清除筛选' }))
  await user.click(panel.getByRole('button', { name: '折叠全部轮次' }))
  expect(panel.queryByText('最终回答')).toBeNull()
  await user.click(panel.getByRole('button', { name: '展开全部轮次' }))
  expect(panel.getByText('最终回答')).toBeTruthy()
})
