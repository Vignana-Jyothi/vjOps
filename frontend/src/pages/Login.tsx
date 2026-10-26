import { useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { api, auth } from '../lib/api'

export default function Login() {
  const [mode, setMode] = useState<'signin' | 'signup'>('signin')
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const nav = useNavigate()

  if (auth.token) return <Navigate to="/" replace />

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      if (mode === 'signup') await api.signup(name.trim(), email.trim(), password)
      else await api.login(email.trim(), password)
      nav('/')
    } catch (err: any) {
      setError(err.message || (mode === 'signup' ? 'Sign up failed' : 'Login failed'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid min-h-screen place-items-center px-6">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <div className="mx-auto mb-3 grid h-11 w-11 place-items-center rounded-lg bg-sky-600 font-bold text-white">V</div>
          <h1 className="text-xl font-semibold">ViljaOps</h1>
          <p className="mt-1 text-sm text-slate-500">AI DevOps intelligence for the startup incubator</p>
        </div>

        <form onSubmit={submit} className="card space-y-4">
          {mode === 'signup' && (
            <div>
              <label className="label">Name</label>
              <input className="input" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
            </div>
          )}
          <div>
            <label className="label">Email</label>
            <input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
          </div>
          <div>
            <label className="label">Password</label>
            <input
              className="input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              minLength={mode === 'signup' ? 8 : undefined}
              required
            />
          </div>
          {error && <div className="rounded-md border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{error}</div>}
          <button className="btn-primary w-full justify-center" disabled={busy}>
            {mode === 'signup' ? (busy ? 'Creating account…' : 'Create account') : busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        <p className="mt-4 text-center text-sm text-slate-500">
          {mode === 'signin' ? "Don't have an account? " : 'Already have an account? '}
          <button
            type="button"
            className="text-sky-500 hover:underline"
            onClick={() => {
              setMode(mode === 'signin' ? 'signup' : 'signin')
              setError('')
            }}
          >
            {mode === 'signin' ? 'Sign up' : 'Sign in'}
          </button>
        </p>

        <p className="mt-2 text-center text-xs text-slate-600">
          First run? Sign in with the bootstrap admin from your .env, then change the password.
        </p>
      </div>
    </div>
  )
}
