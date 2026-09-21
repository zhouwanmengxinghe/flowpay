import { useEffect, useState } from 'react'
import { errorMessage, sessionApi } from '@/lib/session-api'
import { mergeEvents, statusOf, type TraceEvent } from '@/state'

export type ConnectionStatus = 'connecting' | 'connected' | 'reconnecting' | 'error'

/** 先读取历史，再按游标订阅；切换会话仅断开 SSE，不停止后端 Run。 */
export function useSessionEvents(
  sessionId: string | null,
  retry: number,
  refreshSessions: () => Promise<void>,
  reportError: (message: string) => void,
) {
  const [snapshot, setSnapshot] = useState<{ sessionId: string | null; events: TraceEvent[] }>({
    sessionId: null,
    events: [],
  })
  const [connection, setConnection] = useState<ConnectionStatus>('connecting')
  useEffect(() => {
    setSnapshot({ sessionId, events: [] })
    setConnection('connecting')
    if (!sessionId) return
    let disposed = false
    let stream: EventSource | undefined
    sessionApi
      .getEvents(sessionId)
      .then((history) => {
        if (disposed) return
        setSnapshot({ sessionId, events: history })
        stream = new EventSource(
          `/api/sessions/${sessionId}/stream?after=${history.at(-1)?.seq ?? 0}`,
        )
        stream.onopen = () => {
          if (!disposed) setConnection('connected')
        }
        stream.onerror = () => {
          if (!disposed) {
            setConnection('reconnecting')
            // 会话可能在历史读取之后、SSE 建立之前被其他页面删除。
            refreshSessions().catch(() => {
              /* 网络故障时保留重连状态。 */
            })
          }
        }
        stream.addEventListener('session-deleted', () => {
          if (disposed) return
          stream?.close()
          setSnapshot({ sessionId, events: [] })
          refreshSessions().catch((error) => reportError(errorMessage(error)))
        })
        stream.addEventListener('trace', (event) => {
          if (disposed) return
          const next = JSON.parse((event as MessageEvent).data) as TraceEvent
          setSnapshot((previous) => ({ sessionId, events: mergeEvents(previous.events, [next]) }))
          if (next.type.startsWith('run.') || next.type === 'message.user')
            refreshSessions().catch((error) => {
              if (!disposed) reportError(errorMessage(error))
            })
        })
      })
      .catch((error) => {
        if (!disposed) {
          setConnection('error')
          reportError(errorMessage(error))
        }
      })
    return () => {
      disposed = true
      stream?.close()
    }
  }, [sessionId, retry, refreshSessions, reportError])
  // effect 清理前也不让旧会话事件进入新会话的渲染。
  const events = snapshot.sessionId === sessionId ? snapshot.events : []
  return { events, connection, status: statusOf(events) }
}
