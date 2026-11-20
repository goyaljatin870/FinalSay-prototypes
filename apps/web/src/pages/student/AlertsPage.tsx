import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { api, errorMessage } from '../../api/client'
import type { StudentFollows, StudentNotification, StudentNotificationPage } from '../../api/types'

function timestamp(value: string) {
  return new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}

export default function AlertsPage() {
  const [notifications, setNotifications] = useState<StudentNotification[]>([])
  const [follows, setFollows] = useState<StudentFollows>({ notices: [], categories: [] })
  const [unread, setUnread] = useState(0)
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [category, setCategory] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<number | string | null>(null)

  const load = useCallback(async () => {
    setError('')
    try {
      const [alertResponse, followsResponse] = await Promise.all([
        api.get<StudentNotificationPage>('/alerts', { params: { page, page_size: 20 } }),
        api.get<StudentFollows>('/alerts/follows'),
      ])
      setNotifications(alertResponse.data.items)
      setUnread(alertResponse.data.unread_count)
      setTotal(alertResponse.data.total)
      setFollows(followsResponse.data)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [page])

  useEffect(() => { void load() }, [load])

  const addCategory = async (event: FormEvent) => {
    event.preventDefault()
    const label = category.trim()
    if (!label) return
    setBusy('category')
    setError('')
    try {
      await api.post('/alerts/categories', { category_label: label })
      setCategory('')
      await load()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const removeCategory = async (id: number) => {
    setBusy(`category-${id}`)
    try {
      await api.delete(`/alerts/categories/${id}`)
      await load()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const unfollowNotice = async (id: number) => {
    setBusy(id)
    try {
      await api.delete(`/alerts/notices/${id}/follow`)
      await load()
      window.dispatchEvent(new CustomEvent('finalsay-alerts-updated'))
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const markRead = async (id: number) => {
    setBusy(id)
    try {
      await api.patch(`/alerts/${id}/read`)
      setNotifications((current) => current.map((item) => item.id === id ? { ...item, read_at: new Date().toISOString() } : item))
      setUnread((count) => Math.max(0, count - 1))
      window.dispatchEvent(new CustomEvent('finalsay-alerts-updated'))
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const markAllRead = async () => {
    setBusy('all')
    try {
      await api.patch('/alerts/read-all')
      const now = new Date().toISOString()
      setNotifications((current) => current.map((item) => item.read_at ? item : { ...item, read_at: now }))
      setUnread(0)
      window.dispatchEvent(new CustomEvent('finalsay-alerts-updated'))
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const dismiss = async (id: number) => {
    const item = notifications.find((notice) => notice.id === id)
    setBusy(id)
    try {
      await api.delete(`/alerts/${id}`)
      setNotifications((current) => current.filter((notice) => notice.id !== id))
      if (item && !item.read_at) setUnread((count) => Math.max(0, count - 1))
      window.dispatchEvent(new CustomEvent('finalsay-alerts-updated'))
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="stack alerts-page">
      <section className="card">
        <div className="notice-browser-head">
          <div>
            <p className="eyebrow">Your updates</p>
            <h1>Alerts</h1>
            <p className="muted">Changes to official notices and new notices that match topics you follow.</p>
          </div>
          {unread > 0 && <button className="btn-secondary" type="button" disabled={busy === 'all'} onClick={() => void markAllRead()}>{busy === 'all' ? 'Saving…' : 'Mark all as read'}</button>}
        </div>
        <p className="muted" aria-live="polite">{unread} unread · {total} alerts</p>
        {error && <p className="error" role="alert">{error}</p>}
        {loading ? <p className="muted">Loading your alerts…</p> : notifications.length === 0 ? (
          <div className="feed-empty"><strong>You’re all caught up.</strong><p>Follow a notice or a topic to see relevant changes here.</p></div>
        ) : (
          <div className="stack-sm">
            {notifications.map((item) => (
              <article className={`card alert-card${item.read_at ? '' : ' alert-unread'}`} key={item.id}>
                <div className="notice-browser-head">
                  <div>
                    <p className="eyebrow">{item.read_at ? 'Read' : 'New'} · {timestamp(item.created_at)}</p>
                    <h2>{item.title}</h2>
                  </div>
                  {!item.read_at && <button type="button" className="btn-secondary" disabled={busy === item.id} onClick={() => void markRead(item.id)}>Mark read</button>}
                </div>
                <p>{item.message}</p>
                {item.old_value && item.new_value && <p className="alert-change"><strong>Changed:</strong> {item.old_value} → {item.new_value}</p>}
                {item.notice_title && <p className="muted">{item.notice_title}</p>}
                <div className="row">
                  {item.notice_id && <Link className="button-link" to={`/notices?notice=${item.notice_id}`} onClick={() => { if (!item.read_at) void markRead(item.id) }}>Open latest notice</Link>}
                  {item.source_url && <a href={item.source_url} target="_blank" rel="noreferrer">Official source ↗</a>}
                  <button type="button" className="btn-secondary" disabled={busy === item.id} onClick={() => void dismiss(item.id)}>Dismiss</button>
                </div>
              </article>
            ))}
            {total > 20 && <div className="row alert-pagination">
              <button type="button" className="btn-secondary" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>Newer</button>
              <span>Page {page} of {Math.ceil(total / 20)}</span>
              <button type="button" className="btn-secondary" disabled={page >= Math.ceil(total / 20)} onClick={() => setPage((value) => value + 1)}>Older</button>
            </div>}
          </div>
        )}
      </section>

      <section className="card">
        <p className="eyebrow">Personalize</p>
        <h2>Followed notices</h2>
        {follows.notices.length === 0 ? <p className="muted">You are not following any individual notices yet.</p> : (
          <div className="stack-sm">
            {follows.notices.map(({ notice, followed_at }) => (
              <div className="row alert-follow-row" key={notice.id}>
                <div><Link to={`/notices?notice=${notice.id}`}>{notice.title || `Official notice #${notice.id}`}</Link><small className="muted"> · followed {timestamp(followed_at)}</small></div>
                <button type="button" className="btn-secondary" disabled={busy === notice.id} onClick={() => void unfollowNotice(notice.id)}>Unfollow</button>
              </div>
            ))}
          </div>
        )}
        <h3>Topics and departments</h3>
        <p className="muted">Add a subject phrase, department, or topic such as “Examination”, “Fee notices”, or “Computer Engineering”. FinalSay matches it against the official notice title and text.</p>
        <form className="row" onSubmit={(event) => void addCategory(event)}>
          <label className="grow">Topic or department<input value={category} onChange={(event) => setCategory(event.target.value)} maxLength={120} placeholder="e.g. Computer Engineering" /></label>
          <button type="submit" disabled={busy === 'category' || category.trim().length < 2}>{busy === 'category' ? 'Adding…' : 'Follow topic'}</button>
        </form>
        {follows.categories.length > 0 && <div className="stack-sm">{follows.categories.map((item) => (
          <div className="row alert-follow-row" key={item.id}>
            <strong>{item.category_label}</strong>
            <button type="button" className="btn-secondary" disabled={busy === `category-${item.id}`} onClick={() => void removeCategory(item.id)}>Unfollow</button>
          </div>
        ))}</div>}
      </section>
    </div>
  )
}
