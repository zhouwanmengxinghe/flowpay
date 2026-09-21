import { useCallback, useEffect, useRef, useState } from 'react'
import { errorMessage, sessionApi } from '@/lib/session-api'
import type { Meta, Session } from '@/state'

/** 工作空间元信息和会话选择；聊天事件由独立订阅持有。 */
export function useSessionCatalog(retry: number, reportError: (message: string) => void) {
  const [meta, setMeta] = useState<Meta | null>(null)
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeId, setActiveId] = useState<string | null>(() =>
    localStorage.getItem('flowpay.session'),
  )
  const [loading, setLoading] = useState(true)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState('')
  const deleteBusy = useRef(false)
  const removed = useRef(new Set<string>())
  const refreshVersion = useRef(0)
  const refresh = useCallback(async () => {
    const version = ++refreshVersion.current
    const items = await sessionApi.listSessions()
    if (version !== refreshVersion.current) return
    const visible = items.filter((item) => !removed.current.has(item.id))
    setSessions(visible)
    setActiveId((current) =>
      visible.some((item) => item.id === current) ? current : (visible[0]?.id ?? null),
    )
  }, [])

  async function deleteSessions(ids: string[]): Promise<boolean> {
    if (deleteBusy.current || !ids.length) return false
    deleteBusy.current = true
    setDeleting(true)
    setDeleteError('')
    try {
      if (ids.length === 1) await sessionApi.deleteSession(ids[0])
      else await sessionApi.deleteSessions(ids)
      // 成功响应也覆盖已经不存在的 ID；旧列表请求不得把删除项重新放回来。
      ids.forEach((id) => removed.current.add(id))
      ++refreshVersion.current
      setSessions((items) => items.filter((item) => !removed.current.has(item.id)))
      setActiveId((current) =>
        current && removed.current.has(current)
          ? (sessions.find((item) => !removed.current.has(item.id))?.id ?? null)
          : current,
      )
      try {
        await refresh()
      } catch (error) {
        reportError(errorMessage(error))
      }
      return true
    } catch (error) {
      setDeleteError(errorMessage(error))
      return false
    } finally {
      deleteBusy.current = false
      setDeleting(false)
    }
  }

  useEffect(() => {
    let disposed = false
    setLoading(true)
    Promise.all([sessionApi.getMeta(), sessionApi.listSessions()])
      .then(([metadata, items]) => {
        if (disposed) return
        setMeta(metadata)
        items = items.filter((item) => !removed.current.has(item.id))
        setSessions(items)
        setActiveId((current) =>
          items.some((item) => item.id === current) ? current : (items[0]?.id ?? null),
        )
        reportError('')
      })
      .catch((error) => {
        if (!disposed) reportError(errorMessage(error))
      })
      .finally(() => {
        if (!disposed) setLoading(false)
      })
    return () => {
      disposed = true
    }
  }, [retry, reportError])

  useEffect(() => {
    if (activeId) localStorage.setItem('flowpay.session', activeId)
    else localStorage.removeItem('flowpay.session')
  }, [activeId])
  return {
    meta,
    sessions,
    activeId,
    selectSession: setActiveId,
    loading,
    refresh,
    deleting,
    deleteError,
    deleteSessions,
  }
}
