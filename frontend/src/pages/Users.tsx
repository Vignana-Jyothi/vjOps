import { useEffect, useState } from 'react'
import { Card, Empty, ErrorBox, Spinner } from '../components/ui'
import { api, auth, fmt } from '../lib/api'

const ROLES = ['student', 'mentor', 'devops', 'admin']

const ROLE_HELP: Record<string, string> = {
  student: 'Sees only the projects they own.',
  mentor: 'Sees every project and the mentor board.',
  devops: 'Manages servers, projects, ports, Nginx and approves fixes.',
  admin: 'Everything devops can do, plus managing users.',
}

export default function Users() {
  const [users, setUsers] = useState<any[] | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState({ name: '', email: '', password: '', role: 'student' })
  const me = auth.user

  async function load() {
    try {
      setUsers(await api.get('/api/auth/users'))
    } catch (e: any) {
      setError(e.message)
    }
  }

  useEffect(() => {
    load()
  }, [])

  async function create(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    try {
      await api.post('/api/auth/users', form)
      setNotice(`Created ${form.email}. Share the password with them and ask them to change it from their account page.`)
      setForm({ name: '', email: '', password: '', role: 'student' })
      setCreating(false)
      load()
    } catch (e: any) {
      setError(e.message)
    }
  }

  async function update(u: any, changes: any, message: string) {
    setError('')
    setNotice('')
    try {
      await api.patch(`/api/auth/users/${u.id}`, changes)
      setNotice(message)
      load()
    } catch (e: any) {
      setError(e.message)
    }
  }

  function resetPassword(u: any) {
    const password = prompt(`New password for ${u.email} (at least 8 characters):`)
    if (!password) return
    if (password.length < 8) return setError('Password must be at least 8 characters')
    update(u, { password }, `Password reset for ${u.email}.`)
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">Users</h1>
          <p className="text-sm text-slate-500">Who can sign in, and what each person is allowed to do.</p>
        </div>
        <button className="btn-primary" onClick={() => setCreating((v) => !v)}>
          {creating ? 'Cancel' : 'Add user'}
        </button>
      </div>

      {error && <ErrorBox error={error} />}
      {notice && (
        <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-4 py-2 text-sm text-emerald-200">{notice}</div>
      )}

      {creating && (
        <Card title="Add a user">
          <form onSubmit={create} className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label">Name</label>
              <input className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </div>
            <div>
              <label className="label">Email</label>
              <input className="input" type="email" required value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
            </div>
            <div>
              <label className="label">Temporary password</label>
              <input className="input" type="text" required minLength={8} value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
            </div>
            <div>
              <label className="label">Role</label>
              <select className="input" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
                {ROLES.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
              <p className="mt-1 text-[11px] text-slate-500">{ROLE_HELP[form.role]}</p>
            </div>
            <div className="sm:col-span-2">
              <button className="btn-primary">Create user</button>
            </div>
          </form>
        </Card>
      )}

      <Card>
        {!users ? (
          <Spinner />
        ) : users.length === 0 ? (
          <Empty>No users yet.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-slate-500">
                <tr className="border-b border-ink-600 text-left">
                  <th className="py-2">User</th>
                  <th>Role</th>
                  <th>Status</th>
                  <th>Joined</th>
                  <th />
                </tr>
              </thead>
              <tbody className="divide-y divide-ink-600">
                {users.map((u) => {
                  const self = u.email === me?.email
                  return (
                    <tr key={u.id} className={u.is_active ? '' : 'opacity-50'}>
                      <td className="py-2">
                        <div className="text-slate-200">
                          {u.name || '—'} {self && <span className="text-xs text-slate-500">(you)</span>}
                        </div>
                        <div className="text-xs text-slate-500">{u.email}</div>
                      </td>
                      <td>
                        <select
                          className="input w-auto py-1"
                          value={u.role}
                          disabled={self}
                          title={self ? 'You cannot change your own role' : ROLE_HELP[u.role]}
                          onChange={(e) => update(u, { role: e.target.value }, `${u.email} is now ${e.target.value}.`)}
                        >
                          {ROLES.map((r) => (
                            <option key={r} value={r}>
                              {r}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td>
                        <span className={`pill ${u.is_active ? 'bg-emerald-500/15 text-emerald-300' : 'bg-slate-500/15 text-slate-400'}`}>
                          {u.is_active ? 'active' : 'disabled'}
                        </span>
                      </td>
                      <td className="text-xs text-slate-500">{fmt.ago(u.created_at)}</td>
                      <td className="space-x-2 whitespace-nowrap text-right">
                        <button className="btn-ghost py-1 text-xs" onClick={() => resetPassword(u)}>
                          Reset password
                        </button>
                        {!self && (
                          <button
                            className="btn-ghost py-1 text-xs"
                            onClick={() =>
                              update(u, { is_active: !u.is_active }, `${u.email} ${u.is_active ? 'can no longer sign in' : 'can sign in again'}.`)
                            }
                          >
                            {u.is_active ? 'Disable' : 'Enable'}
                          </button>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
