import type { Meta, Session, TraceEvent } from '@/state'

async function request<T>(
  path: string,
  method: 'GET' | 'POST' | 'DELETE',
  body?: unknown,
): Promise<T> {
  const response = await fetch(
    `/api${path}`,
    method === 'GET'
      ? undefined
      : {
          method,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        },
  )
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`
    try {
      const error = await response.json()
      if (typeof error.detail === 'string') detail = error.detail
    } catch {
      /* 非 JSON 错误正文使用 HTTP 状态提示。 */
    }
    throw new Error(detail)
  }
  return response.json()
}

// HTTP 路径和请求格式只在此处定义；界面调用含义明确的动作。
export const sessionApi = {
  deleteSession: (id: string) =>
    request<{ deleted_ids: string[] }>(`/sessions/${encodeURIComponent(id)}`, 'DELETE'),
  deleteSessions: (ids: string[]) =>
    request<{ deleted_ids: string[] }>('/sessions/batch-delete', 'POST', { session_ids: ids }),
  getMeta: () => request<Meta>('/meta', 'GET'),
  listSessions: () => request<Session[]>('/sessions', 'GET'),
  createSession: () => request<Session>('/sessions', 'POST', {}),
  getEvents: (id: string) => request<TraceEvent[]>(`/sessions/${id}/events`, 'GET'),
  submitMessage: (id: string, requestId: string, content: string) =>
    request(`/sessions/${id}/messages`, 'POST', { request_id: requestId, content }),
  resumeRun: (id: string, requestId: string) =>
    request(`/sessions/${id}/continue`, 'POST', { request_id: requestId }),
  cancelRun: (id: string) => request(`/sessions/${id}/cancel`, 'POST', {}),
}
export const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : String(error)
