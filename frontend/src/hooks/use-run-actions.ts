import { useRef, useState } from 'react'
import { errorMessage, sessionApi } from '@/lib/session-api'

export type RunActions = {
  pending: boolean
  createSession: () => Promise<boolean>
  submitMessage: (content: string) => Promise<boolean>
  resumeRun: () => Promise<boolean>
  cancelRun: () => Promise<boolean>
}

type Submission = {
  id: string
  sessionId: string
  operation: 'message' | 'resume'
  content?: string
}

/** 请求幂等与操作互斥；运行最终状态始终以 SSE 事件为准。 */
export function useRunActions(options: {
  sessionId: string | null
  status: string
  blocked?: boolean
  selectSession: (id: string) => void
  refreshSessions: () => Promise<void>
  reportError: (message: string) => void
}): RunActions {
  const { sessionId, status, selectSession, refreshSessions, reportError } = options
  const [pending, setPending] = useState(false)
  const busy = useRef(false)
  const submission = useRef<Submission | null>(null)

  async function perform(operation: () => Promise<void>): Promise<boolean> {
    if (busy.current || options.blocked) return false
    busy.current = true
    setPending(true)
    reportError('')
    try {
      await operation()
      return true
    } catch (error) {
      reportError(errorMessage(error))
      return false
    } finally {
      busy.current = false
      setPending(false)
    }
  }

  function requestId(next: Omit<Submission, 'id'>): string {
    const previous = submission.current
    if (
      previous &&
      previous.sessionId === next.sessionId &&
      previous.operation === next.operation &&
      previous.content === next.content
    )
      return previous.id
    const id = crypto.randomUUID()
    submission.current = { ...next, id }
    return id
  }

  async function refreshAfterAcceptance() {
    // 命令已经受理时，列表刷新失败不能把它变成可重发的新提交。
    try {
      await refreshSessions()
    } catch (error) {
      reportError(errorMessage(error))
    }
  }

  async function createSession() {
    return perform(async () => {
      const session = await sessionApi.createSession()
      selectSession(session.id)
      await refreshAfterAcceptance()
    })
  }

  async function submitMessage(content: string) {
    const text = content.trim()
    if (!text || status === 'running') return false
    return perform(async () => {
      let targetId = sessionId
      if (!targetId) {
        const session = await sessionApi.createSession()
        targetId = session.id
        selectSession(targetId)
      }
      const id = requestId({
        sessionId: targetId,
        operation: 'message',
        content: text,
      })
      await sessionApi.submitMessage(targetId, id, text)
      submission.current = null
      await refreshAfterAcceptance()
    })
  }

  async function resumeRun() {
    if (!sessionId) return false
    return perform(async () => {
      const id = requestId({ sessionId, operation: 'resume' })
      await sessionApi.resumeRun(sessionId, id)
      submission.current = null
      await refreshAfterAcceptance()
    })
  }

  async function cancelRun() {
    if (!sessionId) return false
    return perform(async () => {
      await sessionApi.cancelRun(sessionId)
      await refreshAfterAcceptance()
    })
  }
  return {
    pending: pending || !!options.blocked,
    createSession,
    submitMessage,
    resumeRun,
    cancelRun,
  }
}
