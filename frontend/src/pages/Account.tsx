import { useState } from 'react'
import { Card, ErrorBox } from '../components/ui'
import { api, auth } from '../lib/api'

export default function Account() {
  const user = auth.user
  const [form, setForm] = useState({ current_password: '', new_password: '', confirm: '' })
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)
  const [busy, setBusy] = useState(false)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    setDone(false)
    if (form.new_password !== form.confirm) return setError('The new passwords do not match')
    setBusy(true)
    try {
      await api.post('/api/auth/change-password', { current_password: form.current_password, new_password: form.new_password })
      setForm({ current_password: '', new_password: '', confirm: '' })
      setDone(true)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto max-w-lg space-y-6">
      <div>
        <h1 className="text-lg font-semibold">Your account</h1>
        <p className="text-sm text-slate-500">
          Signed in as {user?.email} · <span className="text-slate-400">{user?.role}</span>
        </p>
      </div>

      <Card title="Change password">
        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="label">Current password</label>
            <input className="input" type="password" required value={form.current_password} onChange={(e) => setForm({ ...form, current_password: e.target.value })} />
          </div>
          <div>
            <label className="label">New password</label>
            <input className="input" type="password" required minLength={8} value={form.new_password} onChange={(e) => setForm({ ...form, new_password: e.target.value })} />
            <p className="mt-1 text-[11px] text-slate-500">At least 8 characters.</p>
          </div>
          <div>
            <label className="label">Confirm new password</label>
            <input className="input" type="password" required minLength={8} value={form.confirm} onChange={(e) => setForm({ ...form, confirm: e.target.value })} />
          </div>
          {error && <ErrorBox error={error} />}
          {done && (
            <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-200">
              Password changed. Use the new one next time you sign in.
            </div>
          )}
          <button className="btn-primary" disabled={busy}>
            {busy ? 'Saving…' : 'Change password'}
          </button>
        </form>
      </Card>
    </div>
  )
}
