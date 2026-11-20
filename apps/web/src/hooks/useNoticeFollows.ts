import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type { StudentFollows } from '../api/types'

export function useNoticeFollows(enabled = true) {
  const [noticeIds, setNoticeIds] = useState<Set<number>>(new Set())
  const [busyIds, setBusyIds] = useState<Set<number>>(new Set())

  const refresh = useCallback(async () => {
    const response = await api.get<StudentFollows>('/alerts/follows')
    setNoticeIds(new Set(response.data.notices.map((entry) => entry.notice.id)))
  }, [])

  useEffect(() => {
    if (!enabled) {
      setNoticeIds(new Set())
      return
    }
    void refresh().catch(() => setNoticeIds(new Set()))
  }, [enabled, refresh])

  const toggle = useCallback(async (noticeId: number) => {
    setBusyIds((current) => new Set(current).add(noticeId))
    const following = noticeIds.has(noticeId)
    try {
      if (following) await api.delete(`/alerts/notices/${noticeId}/follow`)
      else await api.post(`/alerts/notices/${noticeId}/follow`)
      setNoticeIds((current) => {
        const next = new Set(current)
        if (following) next.delete(noticeId)
        else next.add(noticeId)
        return next
      })
      window.dispatchEvent(new CustomEvent('finalsay-alerts-updated'))
    } finally {
      setBusyIds((current) => {
        const next = new Set(current)
        next.delete(noticeId)
        return next
      })
    }
  }, [noticeIds])

  return { noticeIds, busyIds, toggle, refresh }
}
