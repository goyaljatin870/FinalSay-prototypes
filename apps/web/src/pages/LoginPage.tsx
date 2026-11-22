import { useState } from 'react'
import { useLocation, useNavigate, Link } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { errorMessage } from '../api/client'

export default function LoginPage() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const from = (location.state as { from?: string } | null)?.from || '/'

  const doLogin = async (e: string, p: string) => {
    setError('')
    setBusy(true)
    try {
      await login(e, p)
      navigate(from, { replace: true })
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="narrow">
      <div className="card">
        <h1>Log in</h1>
        <form
          onSubmit={(ev) => {
            ev.preventDefault()
            void doLogin(email, password)
          }}
        >
          <label>
            Email
            <input
              type="email"
              value={email}
              onChange={(ev) => setEmail(ev.target.value)}
              required
            />
          </label>
          <label>
            Password
            <input
              type="password"
              value={password}
              onChange={(ev) => setPassword(ev.target.value)}
              required
            />
          </label>
          {error && <p className="error">{error}</p>}
          <button type="submit" disabled={busy}>
            {busy ? 'Signing in…' : 'Log in'}
          </button>
        </form>
        <p className="muted">
          No account? <Link to="/register">Register</Link>
        </p>
        <div style={{ marginTop: '1.25rem', paddingTop: '1rem', borderTop: '1px solid #e5e0d8', fontSize: '0.875rem' }}>
          <p style={{ margin: '0 0 0.5rem', fontWeight: 600 }}>Demo credentials (click to fill):</p>
          <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
            <button
              type="button"
              className="button-secondary"
              style={{ fontSize: '0.8rem', padding: '0.35rem 0.65rem' }}
              onClick={() => {
                setEmail('student@finalsay.local')
                setPassword('Student123!')
              }}
            >
              Student (student@finalsay.local)
            </button>
            <button
              type="button"
              className="button-secondary"
              style={{ fontSize: '0.8rem', padding: '0.35rem 0.65rem' }}
              onClick={() => {
                setEmail('admin@finalsay.local')
                setPassword('AdminPass12345678!')
              }}
            >
              Admin (admin@finalsay.local)
            </button>
          </div>
        </div>
      </div>

    </div>
  )
}
