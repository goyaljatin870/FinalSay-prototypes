import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../../api/client'
import type { AdminNotice, AdminNoticePage, InstitutionOut } from '../../api/types'

const STATUSES = ['active', 'flagged', 'archived', 'rejected'] as const

export default function AdminNoticesPage() {
  const [sources, setSources] = useState<InstitutionOut[]>([])
  const [items, setItems] = useState<AdminNotice[]>([])
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [institutionId, setInstitutionId] = useState('')
  const [status, setStatus] = useState('active')
  const [query, setQuery] = useState('')
  const [error, setError] = useState('')
  const [busyId, setBusyId] = useState<number | null>(null)
  const pageSize = 25

  const load = useCallback(async () => {
    setError('')
    try {
      const [sourcesResponse, pageResponse] = await Promise.all([
        api.get<InstitutionOut[]>('/ingest/sources'),
        api.get<AdminNoticePage>('/admin/notices', {
          params: {
            institution_id: institutionId || undefined,
            status: status || undefined,
            q: query.trim() || undefined,
            page,
            page_size: pageSize,
          },
        }),
      ])
      setSources(sourcesResponse.data)
      setItems(pageResponse.data.items)
      setTotal(pageResponse.data.total)
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [institutionId, page, query, status])

  useEffect(() => { void load() }, [load])

  const updateStatus = async (notice: AdminNotice, nextStatus: typeof STATUSES[number]) => {
    setBusyId(notice.id)
    setError('')
    try {
      await api.patch<AdminNotice>(`/admin/notices/${notice.id}/review`, {
        status: nextStatus,
        note: notice.admin_review_note,
      })
      await load()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusyId(null)
    }
  }

  const pageCount = Math.max(1, Math.ceil(total / pageSize))

  return (
    <div className="stack">
      <section className="card">
        <h1>Official notice review</h1>
        <p className="muted">Check what the source published and flag, archive, or reject records that need attention. Removed notices stay in the audit history.</p>
        <form className="row" onSubmit={(event) => { event.preventDefault(); setPage(1); void load() }}>
          <label>
            Institution
            <select value={institutionId} onChange={(event) => { setInstitutionId(event.target.value); setPage(1) }}>
              <option value="">All institutions</option>
              {sources.map((source) => <option key={source.id} value={source.id}>{source.name}</option>)}
            </select>
          </label>
          <label>
            Review status
            <select value={status} onChange={(event) => { setStatus(event.target.value); setPage(1) }}>
              <option value="">All statuses</option>
              {STATUSES.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
          </label>
          <label>
            Search
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Title or notice text" />
          </label>
          <button type="submit">Search</button>
        </form>
        {error && <p className="error" role="alert">{error}</p>}
        <p className="muted">{total} notices · page {page} of {pageCount}</p>
        {items.length === 0 ? <p className="muted">No notices match these filters.</p> : (
          <div className="table-scroll">
            <table>
              <thead><tr><th>Notice</th><th>Source status</th><th>Review status</th><th>Published</th><th>Retrieved</th><th>Actions</th></tr></thead>
              <tbody>{items.map((notice) => (
                <tr key={notice.id}>
                  <td>
                    <strong>{notice.title || `Official notice #${notice.id}`}</strong>
                    <div className="muted">{sources.find((source) => source.id === notice.institution_id)?.name || `Institution #${notice.institution_id ?? '—'}`} · #{notice.id}</div>
                    {notice.source_url && <a href={notice.source_url} target="_blank" rel="noreferrer">Open source ↗</a>}
                    <details><summary>Redacted notice text</summary><pre className="pre">{notice.redacted_text || 'No extracted text is stored.'}</pre></details>
                    {notice.admin_review_note && <div className="muted">Review note: {notice.admin_review_note}</div>}
                  </td>
                  <td>{notice.source_state}{notice.source_removed_at && <div className="muted">Removed {new Date(notice.source_removed_at).toLocaleString()}</div>}</td>
                  <td>{notice.admin_status}</td>
                  <td>{notice.published_date || '—'}</td>
                  <td>{notice.retrieved_at ? new Date(notice.retrieved_at).toLocaleString() : '—'}</td>
                  <td><div className="stack-sm">
                    {STATUSES.filter((value) => value !== notice.admin_status).map((value) => (
                      <button key={value} type="button" disabled={busyId === notice.id} onClick={() => void updateStatus(notice, value)}>
                        {busyId === notice.id ? 'Saving…' : value === 'active' ? 'Keep active' : value[0].toUpperCase() + value.slice(1)}
                      </button>
                    ))}
                  </div></td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
        <div className="row">
          <button type="button" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>Previous</button>
          <button type="button" disabled={page >= pageCount} onClick={() => setPage((value) => value + 1)}>Next</button>
        </div>
      </section>
    </div>
  )
}
