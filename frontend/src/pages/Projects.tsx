import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Card, Empty, ErrorBox, Spinner } from '../components/ui'
import { api, auth, fmt } from '../lib/api'

export function UserSelect({ users, value, onChange }: { users: any[]; value: string; onChange: (id: string) => void }) {
  return (
    <select className="input" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">Nobody</option>
      {users.map((u) => (
        <option key={u.id} value={u.id}>
          {u.name ? `${u.name} (${u.email})` : u.email} · {u.role}
        </option>
      ))}
    </select>
  )
}

export default function Projects() {
  const [projects, setProjects] = useState<any[]>([])
  const [servers, setServers] = useState<any[]>([])
  const [users, setUsers] = useState<any[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [showArchived, setShowArchived] = useState(false)
  const blank = { name: '', slug: '', github_repo: '', team_name: '', domain: '', server_id: '', owner_id: '', mentor_id: '' }
  const [form, setForm] = useState(blank)
  const canManage = auth.can('admin', 'devops')

  async function load() {
    setLoading(true)
    try {
      setProjects(await api.get(`/api/projects?include_archived=${showArchived}`))
      if (canManage) {
        const [s, u] = await Promise.all([api.get('/api/agents/servers').catch(() => []), api.get('/api/auth/users').catch(() => [])])
        setServers(s)
        setUsers(u.filter((x: any) => x.is_active))
      }
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [showArchived])

  async function create(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    try {
      await api.post('/api/projects', {
        ...form,
        server_id: form.server_id || null,
        owner_id: form.owner_id || null,
        mentor_id: form.mentor_id || null,
      })
      setCreating(false)
      setForm(blank)
      load()
    } catch (e: any) {
      setError(e.message)
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">Projects</h1>
          <p className="text-sm text-slate-500">Every student project registered with the platform.</p>
        </div>
        {canManage && (
          <div className="flex items-center gap-4">
            <label className="flex items-center gap-2 text-xs text-slate-400">
              <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} />
              Show archived
            </label>
            <button className="btn-primary" onClick={() => setCreating((v) => !v)}>
              {creating ? 'Cancel' : 'Register project'}
            </button>
          </div>
        )}
      </div>

      {error && <ErrorBox error={error} />}

      {creating && (
        <Card title="Register a project">
          <form onSubmit={create} className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label">Project name</label>
              <input
                className="input"
                required
                value={form.name}
                onChange={(e) => {
                  const name = e.target.value
                  setForm((f) => ({
                    ...f,
                    name,
                    slug: f.slug || name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''),
                  }))
                }}
              />
            </div>
            <div>
              <label className="label">Slug (container + nginx site name)</label>
              <input className="input" required pattern="[a-z0-9][a-z0-9\-]{1,60}" value={form.slug} onChange={(e) => setForm({ ...form, slug: e.target.value })} />
            </div>
            <div>
              <label className="label">GitHub repository (org/repo)</label>
              <input className="input" placeholder="vnrvjiet-incubator/team-alpha" value={form.github_repo} onChange={(e) => setForm({ ...form, github_repo: e.target.value })} />
            </div>
            <div>
              <label className="label">Team</label>
              <input className="input" value={form.team_name} onChange={(e) => setForm({ ...form, team_name: e.target.value })} />
            </div>
            <div>
              <label className="label">Domain</label>
              <input className="input" placeholder="team-alpha.apps.vnrvjiet.in" value={form.domain} onChange={(e) => setForm({ ...form, domain: e.target.value })} />
            </div>
            <div>
              <label className="label">Deployment server</label>
              <select className="input" value={form.server_id} onChange={(e) => setForm({ ...form, server_id: e.target.value })}>
                <option value="">Assign on first deploy</option>
                {servers.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="label">Owner (student)</label>
              <UserSelect users={users} value={form.owner_id} onChange={(owner_id) => setForm({ ...form, owner_id })} />
              <p className="mt-1 text-[11px] text-slate-500">Students only see projects they own. They can sign up from the login page first.</p>
            </div>
            <div>
              <label className="label">Mentor</label>
              <UserSelect
                users={users.filter((u) => u.role !== 'student')}
                value={form.mentor_id}
                onChange={(mentor_id) => setForm({ ...form, mentor_id })}
              />
            </div>
            <div className="sm:col-span-2">
              <button className="btn-primary">Create project</button>
            </div>
          </form>
        </Card>
      )}

      {loading ? (
        <Spinner />
      ) : projects.length === 0 ? (
        <Empty>
          {canManage
            ? 'No projects registered yet. Click "Register project" to add the first one.'
            : 'No projects are linked to your account yet. Ask your DevOps admin to assign your team’s project to you.'}
        </Empty>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {projects.map((p) => (
            <Link key={p.id} to={`/projects/${p.id}`} className="card transition-colors hover:border-sky-600/50">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate font-medium text-slate-100">
                    {p.name}
                    {p.archived && <span className="pill ml-2 bg-slate-500/15 text-slate-400">archived</span>}
                  </div>
                  <div className="truncate font-mono text-xs text-slate-500">{p.slug}</div>
                </div>
              </div>
              <dl className="mt-3 space-y-1 text-xs text-slate-400">
                {p.github_repo && (
                  <div className="truncate">
                    <span className="text-slate-600">repo</span> {p.github_repo}
                  </div>
                )}
                {p.domain && (
                  <div className="truncate">
                    <span className="text-slate-600">domain</span> {p.domain}
                  </div>
                )}
                {p.team_name && (
                  <div className="truncate">
                    <span className="text-slate-600">team</span> {p.team_name}
                  </div>
                )}
                <div>
                  <span className="text-slate-600">created</span> {fmt.time(p.created_at)}
                </div>
              </dl>
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}
