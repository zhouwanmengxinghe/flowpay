// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { sessionApi } from '@/lib/session-api'
import { useRunActions } from './use-run-actions'

const options = () => ({
  sessionId: 'session_1',
  status: 'idle',
  selectSession: vi.fn(),
  refreshSessions: vi.fn(async () => {}),
  reportError: vi.fn(),
})
afterEach(() => vi.restoreAllMocks())

it.each(['failed', 'interrupted', 'canceled'])('allows a new message after %s', async (status) => {
  const submit = vi.spyOn(sessionApi, 'submitMessage').mockResolvedValue({})
  const { result } = renderHook(() => useRunActions({ ...options(), status }))
  await act(async () => {
    expect(await result.current.submitMessage('新的问题')).toBe(true)
  })
  expect(submit).toHaveBeenCalledWith('session_1', expect.any(String), '新的问题')
})

it('still rejects new messages while a run is active', async () => {
  const submit = vi.spyOn(sessionApi, 'submitMessage').mockResolvedValue({})
  const { result } = renderHook(() => useRunActions({ ...options(), status: 'running' }))
  await act(async () => {
    expect(await result.current.submitMessage('新的问题')).toBe(false)
  })
  expect(submit).not.toHaveBeenCalled()
})

it('reuses the original request ID after a lost submit response', async () => {
  const submit = vi
    .spyOn(sessionApi, 'submitMessage')
    .mockRejectedValueOnce(new Error('connection lost'))
    .mockResolvedValueOnce({})
  const { result } = renderHook(() => useRunActions(options()))
  await act(async () => {
    expect(await result.current.submitMessage('问题')).toBe(false)
  })
  await act(async () => {
    expect(await result.current.submitMessage('问题')).toBe(true)
  })
  expect(submit.mock.calls).toHaveLength(2)
  expect(submit.mock.calls[0]).toEqual(submit.mock.calls[1])
})

it('blocks repeated clicks before React has rendered the pending state', async () => {
  let finish: (value: unknown) => void = () => {}
  const response = new Promise((resolve) => {
    finish = resolve
  })
  const submit = vi.spyOn(sessionApi, 'submitMessage').mockReturnValue(response)
  const { result } = renderHook(() => useRunActions(options()))
  await act(async () => {
    const first = result.current.submitMessage('问题')
    expect(await result.current.submitMessage('问题')).toBe(false)
    finish({})
    expect(await first).toBe(true)
  })
  expect(submit).toHaveBeenCalledTimes(1)
})

it('keeps a successful submission accepted when only the session list refresh fails', async () => {
  const submit = vi.spyOn(sessionApi, 'submitMessage').mockResolvedValue({})
  const config = options()
  config.refreshSessions.mockRejectedValue(new Error('list unavailable'))
  const { result } = renderHook(() => useRunActions(config))
  await act(async () => {
    expect(await result.current.submitMessage('问题')).toBe(true)
  })
  expect(submit).toHaveBeenCalledTimes(1)
  expect(config.reportError).toHaveBeenCalledWith('list unavailable')
})
