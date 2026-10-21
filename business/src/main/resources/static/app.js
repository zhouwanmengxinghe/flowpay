const $ = (id) => document.getElementById(id)
const labels = {
  idle: '待提问',
  queued: '排队中',
  running: '调查中',
  completed: '本轮完成',
  waiting_user: '等待补充',
  failed: '执行失败',
  canceled: '已停止',
  interrupted: '服务重启中断',
  OPEN: '待处理',
  ASSIGNED: '已接单',
  IN_PROGRESS: '处理中',
  RESOLVED: '待商户确认',
  CLOSED: '已关闭',
}
let me,
  page = 'sessions',
  active = null,
  ticket = null,
  timer,
  errorTimer
let pendingMessage = null,
  pendingTicket = null
let capabilities = []
let tenantLimits = []
function showError(error) {
  $('error').textContent = error.message || String(error)
  $('error').hidden = false
  clearTimeout(errorTimer)
  errorTimer = setTimeout(() => ($('error').hidden = true), 7000)
}
async function api(path, body) {
  const response = await fetch('/api' + path, {
    credentials: 'same-origin',
    ...(body === undefined
      ? {}
      : {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }),
  })
  let value
  try {
    value = await response.json()
  } catch {
    throw Error('服务响应异常')
  }
  if (!response.ok) {
    if (response.status === 401) loggedOut()
    const error = Error(value.detail || '请求失败，请稍后重试')
    error.status = response.status
    throw error
  }
  return value
}
function loggedOut() {
  clearInterval(timer)
  me = null
  active = null
  ticket = null
  pendingMessage = null
  pendingTicket = null
  $('workspace').hidden = true
  $('login').hidden = false
}
function run(action) {
  return async (event) => {
    event?.preventDefault()
    const button = event?.submitter || event?.currentTarget
    if (button?.tagName === 'BUTTON') button.disabled = true
    try {
      await action(event)
    } catch (e) {
      showError(e)
    } finally {
      if (button?.tagName === 'BUTTON')
        button.disabled =
          button.id === 'send' && ['排队中', '调查中'].includes($('session-status').textContent)
    }
  }
}
function textNode(tag, text, className) {
  const node = document.createElement(tag)
  node.textContent = text
  if (className) node.className = className
  return node
}
async function enter(user) {
  me = user
  capabilities = await api('/admin/capabilities')
  $('tab-admin').hidden = capabilities.length === 0
  const roleLabel = me.role === 'MERCHANT'
    ? (capabilities.includes('MERCHANT_ADMIN') ? '商户管理员' : '商户成员')
    : (capabilities.includes('SUPPORT_ADMIN') ? '平台支持管理员' : '技术支持')
  $('identity').textContent = `${me.username} · ${roleLabel}`
  $('login').hidden = true
  $('workspace').hidden = false
  try {
    const status = await api('/agent/status')
    $('agent-notice').textContent = status.mode === 'real'
      ? `当前：真实 Agent · ${status.engine === 'langchain' ? 'LangChain' : '原生 s4'}。业务记录为合成数据，请核对回答中的证据与限制。`
      : '当前：模拟验收模式。用于测试业务流程，不查询真实业务或调用模型。'
  } catch {
    $('agent-notice').textContent = 'Agent 暂不可用，仍可查看已保存的调查记录和工单。'
  }
  $('session-tools').hidden = me.role !== 'MERCHANT'
  const accounts = await api('/accounts')
  $('account').replaceChildren(
    ...accounts.map((a) => {
      const o = document.createElement('option')
      o.value = a.account_id
      o.textContent = a.account_id
      return o
    }),
  )
  await switchPage('sessions')
  clearInterval(timer)
  timer = setInterval(() => {
    if (page === 'sessions' && active) loadSession().catch(showError)
  }, 1500)
}
async function switchPage(next) {
  page = next
  active = null
  ticket = null
  $('administration').hidden = next !== 'admin'
  $('investigation-layout').hidden = next === 'admin'
  for (const name of ['sessions', 'tickets', 'admin']) {
    $('tab-' + name).classList.toggle('secondary', next !== name)
  }
  if (next === 'admin') return loadAdministration()
  $('empty').hidden = false
  $('session-detail').hidden = true
  $('ticket-detail').hidden = true
  $('session-tools').hidden = page !== 'sessions' || me.role !== 'MERCHANT'
  $('list-title').textContent = page === 'sessions' ? '调查记录' : '技术工单'
  await refreshList()
}
async function refreshList() {
  const requestedPage = page
  const requestedUser = me
  const items = await api('/' + requestedPage)
  if (page !== requestedPage || me !== requestedUser) return
  $('list').replaceChildren(
    ...items.map((item) => {
      const button = textNode(
        'button',
        `${item.title}\n${labels[item.status] || item.status}`,
        'item',
      )
      button.onclick = run(async () => {
        if (page === 'sessions') {
          active = item.id
          pendingMessage = null
          pendingTicket = null
          await loadSession()
        } else {
          ticket = await api('/tickets/' + item.id)
          renderTicket()
        }
      })
      return button
    }),
  )
}
async function loadSession() {
  const sid = active
  const detail = await api('/sessions/' + sid)
  if (sid !== active || page !== 'sessions') return
  $('empty').hidden = true
  $('ticket-detail').hidden = true
  $('session-detail').hidden = false
  $('session-title').textContent = detail.title
  $('scope').textContent = `${detail.account_id} · ${detail.environment} · ${detail.id}`
  $('session-status').textContent = labels[detail.status] || detail.status
  const items = []
  for (const item of detail.runs) {
    items.push(textNode('div', '你：' + item.input, 'bubble user'))
    if (item.answer) {
      const protocol = ['<｜DSML｜', '<|DSML|', '<tool_call>'].some(marker => item.answer.includes(marker))
      items.push(textNode('div', '助手：' + (protocol ? '这条历史回答包含模型工具协议标记，已隐藏。请查看调查证据；其中未执行的查询不能视为结论。' : item.answer), 'bubble'))
    }
    if (item.error) items.push(textNode('div', item.error, 'bubble error'))
  }
  $('messages').replaceChildren(...items)
  const busy = ['queued', 'running'].includes(detail.status)
  $('message-form').hidden = me.role !== 'MERCHANT'
  $('send').disabled = busy
  $('cancel').hidden = !busy
  $('create-ticket').hidden = me.role !== 'MERCHANT'
  $('create-ticket').disabled = !detail.runs.some((r) =>
    ['completed', 'waiting_user'].includes(r.status),
  )
}
function renderTicket() {
  $('empty').hidden = true
  $('session-detail').hidden = true
  $('ticket-detail').hidden = false
  $('ticket-title').textContent = ticket.title
  $('ticket-status').textContent = labels[ticket.status]
  $('ticket-meta').textContent =
    `${ticket.id} · 处理人：${ticket.assignee_id || '未分配'} · 版本 ${ticket.version}`
  $('evidence').textContent = JSON.stringify(ticket.evidence, null, 2)
  $('history').replaceChildren(
    ...ticket.history.map((h) =>
      textNode(
        'div',
        `${h.actor_id}：${labels[h.from_status] || '创建'} → ${labels[h.to_status]}\n${h.note}`,
        'history-item',
      ),
    ),
  )
  let targets = []
  if (me.role === 'SUPPORT') {
    if (ticket.status === 'OPEN') targets = [['ASSIGNED', '接单']]
    else if (ticket.assignee_id === me.id && ticket.status === 'ASSIGNED')
      targets = [['IN_PROGRESS', '开始处理']]
    else if (ticket.assignee_id === me.id && ticket.status === 'IN_PROGRESS')
      targets = [['RESOLVED', '提交解决结果']]
  } else if (ticket.status === 'RESOLVED')
    targets = [
      ['CLOSED', '确认解决并关闭'],
      ['OPEN', '问题未解决，重新打开'],
    ]
  $('transitions').replaceChildren(
    ...targets.map(([status, label]) => {
      const button = textNode('button', label)
      button.onclick = run(async () => {
        const note = $('note').value.trim()
        if (!note) throw Error('请填写处理说明')
        ticket = await api('/tickets/' + ticket.id + '/transition', {
          version: ticket.version,
          status,
          note,
        })
        $('note').value = ''
        renderTicket()
        await refreshList()
      })
      return button
    }),
  )
}
$('login-form').onsubmit = run(async (event) => {
  const form = event.currentTarget
  const data = new FormData(form)
  const user = await api('/auth/login', {
    username: data.get('username'),
    password: data.get('password'),
  })
  form.reset()
  await enter(user)
})
$('logout').onclick = run(async () => {
  await api('/auth/logout', {})
  loggedOut()
})
$('tab-sessions').onclick = run(() => switchPage('sessions'))
$('tab-tickets').onclick = run(() => switchPage('tickets'))
$('tab-admin').onclick = run(() => switchPage('admin'))
$('refresh-admin').onclick = run(loadAdministration)
$('member-form').onsubmit = run(async (event) => {
  const form = event.currentTarget
  const data = new FormData(form)
  await api('/members', {username: data.get('username'), password: data.get('password'), administrator: data.get('administrator') === 'true'})
  form.reset()
  await loadAdministration()
})
$('grant-form').onsubmit = run(async (event) => {
  const data = new FormData(event.currentTarget)
  await api('/admin/grants', {userId: data.get('userId'), tenantId: data.get('tenantId'), enabled: data.get('enabled') === 'true'})
  await loadAdministration()
})
function renderQuota() {
  const limit = tenantLimits.find(row => row.tenant_id === $('quota-tenant').value)
  $('save-quota').disabled = !limit
  $('quota-rpm').value = limit?.requests_per_minute ?? ''
  $('quota-daily').value = limit?.daily_runs ?? ''
  $('quota-active').value = limit?.active_runs ?? ''
  $('quota-feedback').textContent = ''
}
$('quota-tenant').onchange = renderQuota
$('quota-form').onsubmit = run(async () => {
  const tenant = $('quota-tenant').value
  if (!tenantLimits.some(row => row.tenant_id === tenant)) throw Error('请选择商户')
  const values = {
    requestsPerMinute: Number($('quota-rpm').value),
    dailyRuns: Number($('quota-daily').value),
    activeRuns: Number($('quota-active').value),
  }
  $('quota-feedback').textContent = ''
  await api('/admin/limits/' + encodeURIComponent(tenant), values)
  const limit = tenantLimits.find(row => row.tenant_id === tenant)
  Object.assign(limit, {requests_per_minute: values.requestsPerMinute, daily_runs: values.dailyRuns, active_runs: values.activeRuns})
  $('quota-feedback').textContent = `${limit.name}的配额已保存，对新请求生效。`
})
async function loadAdministration() {
  const memberAdmin = capabilities.includes('MERCHANT_ADMIN')
  const grantAdmin = capabilities.includes('SUPPORT_ADMIN')
  const observe = capabilities.includes('OBSERVE')
  $('member-controls').hidden = !memberAdmin
  $('grant-controls').hidden = !grantAdmin
  $('quota-controls').hidden = !grantAdmin
  $('operation-controls').hidden = !observe
  if (memberAdmin) {
    const members = await api('/members')
    $('member-list').replaceChildren(...members.map(member => {
      const row = textNode('div', `${member.username} · ${member.administrator ? '管理员' : '成员'} · ${member.active ? '启用' : '禁用'}`, 'item')
      const state = textNode('button', member.active ? '禁用成员' : '启用成员', 'secondary')
      state.onclick = run(async () => { await api('/members/' + member.id, {active: !member.active, administrator: member.administrator}); await loadAdministration() })
      const role = textNode('button', member.administrator ? '设为普通成员' : '设为管理员', 'secondary')
      role.onclick = run(async () => { await api('/members/' + member.id, {active: member.active, administrator: !member.administrator}); await loadAdministration() })
      row.append(state, role)
      return row
    }))
    $('usage').textContent = JSON.stringify(await api('/usage'), null, 2)
    $('admin-audit').textContent = JSON.stringify(await api('/admin/audit'), null, 2)
  }
  if (grantAdmin) {
    const selectedTenant = $('quota-tenant').value
    tenantLimits = await api('/admin/limits')
    $('quota-tenant').replaceChildren(...tenantLimits.map(row => {
      const option = textNode('option', `${row.name}（${row.tenant_id}）`)
      option.value = row.tenant_id
      return option
    }))
    if (tenantLimits.some(row => row.tenant_id === selectedTenant)) $('quota-tenant').value = selectedTenant
    renderQuota()
    const data = await api('/admin/grants')
    const options = rows => rows.map(row => { const option = textNode('option', row.username || row.name); option.value = row.id; return option })
    $('grant-user').replaceChildren(...options(data.supportUsers))
    $('grant-tenant').replaceChildren(...options(data.tenants))
    $('grant-list').textContent = JSON.stringify(data.grants, null, 2)
  }
  if (observe) $('operation-stats').textContent = JSON.stringify(await api('/operations'), null, 2)
}
$('create-session').onclick = run(async () => {
  const session = await api('/sessions', {
    accountId: $('account').value,
    environment: $('environment').value,
  })
  active = session.id
  pendingMessage = null
  pendingTicket = null
  await refreshList()
  await loadSession()
})
$('message-form').onsubmit = run(async () => {
  const content = $('message').value.trim()
  if (!content || !active) return
  if (!pendingMessage || pendingMessage.session !== active || pendingMessage.content !== content)
    pendingMessage = { session: active, content, requestId: crypto.randomUUID() }
  try {
    await api('/sessions/' + active + '/messages', {
      requestId: pendingMessage.requestId,
      content: pendingMessage.content,
    })
  } catch (error) {
    if (error.status === 503) pendingMessage = null
    throw error
  }
  pendingMessage = null
  $('message').value = ''
  await loadSession()
  await refreshList()
})
$('cancel').onclick = run(async () => {
  await api('/sessions/' + active + '/cancel', {})
  await loadSession()
})
$('create-ticket').onclick = run(async () => {
  const title = $('session-title').textContent.slice(0, 150)
  if (!pendingTicket || pendingTicket.sessionId !== active || pendingTicket.title !== title)
    pendingTicket = { sessionId: active, title, requestId: crypto.randomUUID() }
  ticket = await api('/tickets', pendingTicket)
  pendingTicket = null
  page = 'tickets'
  $('tab-sessions').classList.add('secondary')
  $('tab-tickets').classList.remove('secondary')
  $('session-tools').hidden = true
  $('list-title').textContent = '技术工单'
  renderTicket()
  await refreshList()
})
$('export').onclick = run(async () => {
  const events = await api('/sessions/' + active + '/events')
  $('events').textContent = JSON.stringify(events, null, 2)
  $('events').hidden = !$('events').hidden
})
api('/auth/me')
  .then(enter)
  .catch(() => loggedOut())
