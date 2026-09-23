import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Card, Empty, ErrorBox, Spinner } from '../components/ui'
import { api, auth, fmt } from '../lib/api'

export default function Projects() {
  const [projects, setProjects] = useState<any[]>([])
  const [servers, setServers] = useState<any[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState({ name: '', slug: '', github_repo: '', team_name: '', domain: '', server_id: '' })
  const canManage = auth.can('admin', 'devops')

  async function load() {
    setLoading(true)
    try {
      setProjects(await api.get('/api/projects'))
      if (canManage) setServers(await api.get('/api/agents/servers').catch(() => []))
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  async function create(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    try {
      await api.post('/api/projects', { ...form, server_id: form.server_id || null })
      setCreating(false)
      setForm({ name: '', slug: '', github_repo: '', team_name: '', domain: '', server_id: '' })
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
          <button className="btn-primary" onClick={() => setCreating((v) => !v)}>
            {creating ? 'Cancel' : 'Register project'}
          </button>
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
            <div className="sm:col-span-2">
              <button className="btn-primary">Create project</button>
            </div>
          </form>
        </Card>
      )}

      {loading ? (
        <Spinner />
      ) : projects.length === 0 ? (
        <Empty>No projects registered yet.</Empty>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {projects.map((p) => (
            <Link key={p.id} to={`/projects/${p.id}`} className="card transition-colors hover:border-sky-600/50">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate font-medium text-slate-100">{p.name}</div>
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
