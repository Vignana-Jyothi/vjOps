import { useEffect, useState } from 'react'
import { Card, Empty, ErrorBox, Spinner } from '../components/ui'
import { api, fmt } from '../lib/api'

export default function Infrastructure() {
  const [servers, setServers] = useState<any[]>([])
  const [selected, setSelected] = useState<string>('')
  const [usage, setUsage] = useState<any>(null)
  const [configs, setConfigs] = useState<any[]>([])
  const [commands, setCommands] = useState<any[]>([])
  const [projects, setProjects] = useState<any[]>([])
  const [error, setError] = useState('')
  const [enrolling, setEnrolling] = useState(false)
  const [newServer, setNewServer] = useState({ name: '', hostname: '', port_range_start: 3000, port_range_end: 3999 })
  const [token, setToken] = useState('')
  const [nginxForm, setNginxForm] = useState({ project_id: '', domain: '', upstream_port: '' })
  const [preview, setPreview] = useState('')

  async function load() {
    try {
      const s = await api.get('/api/agents/servers')
      setServers(s)
      const first = selected || s[0]?.id || ''
      setSelected(first)
      if (first) setUsage(await api.get(`/api/infra/ports/${first}`))
      setConfigs(await api.get('/api/infra/nginx'))
      setCommands(await api.get('/api/agents/commands/history?limit=15'))
      setProjects(await api.get('/api/projects'))
    } catch (e: any) {
      setError(e.message)
    }
  }

  useEffect(() => {
    load()
  }, [])

  useEffect(() => {
    if (selected) api.get(`/api/infra/ports/${selected}`).then(setUsage).catch(() => {})
  }, [selected])

  async function enroll(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    try {
      const res = await api.post('/api/agents/servers', newServer)
      setToken(res.agent_token)
      setEnrolling(false)
      load()
    } catch (e: any) {
      setError(e.message)
    }
  }

  async function previewNginx() {
    setError('')
    setPreview('')
    try {
      const res = await api.post('/api/infra/nginx/preview', {
        project_id: nginxForm.project_id,
        domain: nginxForm.domain,
        upstream_port: nginxForm.upstream_port ? Number(nginxForm.upstream_port) : null,
        options: {},
      })
      setPreview(res.rendered)
    } catch (e: any) {
      setError(e.message)
    }
  }

  async function createAndApply() {
    setError('')
    try {
      const cfg = await api.post('/api/infra/nginx', {
        project_id: nginxForm.project_id,
        domain: nginxForm.domain,
        upstream_port: nginxForm.upstream_port ? Number(nginxForm.upstream_port) : null,
        options: {},
      })
      await api.post(`/api/infra/nginx/${cfg.id}/apply`)
      setPreview('')
      load()
    } catch (e: any) {
      setError(e.message)
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">Infrastructure</h1>
          <p className="text-sm text-slate-500">Port registry and Nginx sites — the parts that used to be manual.</p>
        </div>
        <button className="btn-primary" onClick={() => setEnrolling((v) => !v)}>
          {enrolling ? 'Cancel' : 'Enroll server'}
        </button>
      </div>

      {error && <ErrorBox error={error} />}

      {token && (
        <Card title="Agent token — shown once">
          <p className="mb-2 text-xs text-slate-400">
            Copy this now. Install the agent on the server with:
          </p>
          <pre className="logline overflow-auto rounded bg-ink-900 p-3 text-emerald-300">
{`sudo VILJAOPS_URL=${location.origin} \\
     VILJAOPS_AGENT_TOKEN=${token} \\
     bash agent/install.sh`}
          </pre>
          <button className="btn-ghost mt-3" onClick={() => setToken('')}>
            I've saved it
          </button>
        </Card>
      )}

      {enrolling && (
        <Card title="Enroll a deployment server">
          <form onSubmit={enroll} className="grid gap-4 sm:grid-cols-4">
            <div className="sm:col-span-2">
              <label className="label">Name</label>
              <input className="input" required value={newServer.name} onChange={(e) => setNewServer({ ...newServer, name: e.target.value })} />
            </div>
            <div className="sm:col-span-2">
              <label className="label">Hostname</label>
              <input className="input" value={newServer.hostname} onChange={(e) => setNewServer({ ...newServer, hostname: e.target.value })} />
            </div>
            <div>
              <label className="label">Port range start</label>
              <input className="input" type="number" value={newServer.port_range_start} onChange={(e) => setNewServer({ ...newServer, port_range_start: +e.target.value })} />
            </div>
            <div>
              <label className="label">Port range end</label>
              <input className="input" type="number" value={newServer.port_range_end} onChange={(e) => setNewServer({ ...newServer, port_range_end: +e.target.value })} />
            </div>
            <div className="sm:col-span-4">
              <button className="btn-primary">Enroll</button>
            </div>
          </form>
        </Card>
      )}

      <div className="grid gap-6 lg:grid-cols-3">
        <Card title="Servers">
          {servers.length === 0 ? (
            <Empty>No servers enrolled.</Empty>
          ) : (
            <ul className="space-y-2">
              {servers.map((s) => (
                <li key={s.id}>
                  <button
                    onClick={() => setSelected(s.id)}
                    className={`w-full rounded-md border p-2 text-left text-sm ${
                      selected === s.id ? 'border-sky-600/50 bg-ink-700' : 'border-ink-600 hover:bg-ink-700/50'
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="flex items-center gap-2">
                        <span className={`h-2 w-2 rounded-full ${s.online ? 'bg-emerald-400' : 'bg-rose-400'}`} />
                        {s.name}
                      </span>
                      <span className="text-[11px] text-slate-500">{s.agent_version || '—'}</span>
                    </div>
                    <div className="mt-1 text-[11px] text-slate-500">
                      {s.cpu_cores} cores · {Math.round(s.ram_mb / 1024)} GB · ports {s.port_range_start}–{s.port_range_end}
                    </div>
                    <div className="text-[11px] text-slate-600">last seen {fmt.ago(s.last_seen)}</div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Port registry" className="lg:col-span-2">
          {!usage ? (
            <Spinner />
          ) : (
            <>
              <div className="mb-3 flex flex-wrap gap-4 text-sm">
                <span><span className="text-slate-500">allocated</span> {usage.allocated}</span>
                <span><span className="text-slate-500">reserved</span> {usage.reserved}</span>
                <span><span className="text-slate-500">free</span> {usage.free}</span>
                <span className={usage.utilization_pct > 80 ? 'text-amber-300' : 'text-slate-400'}>
                  {usage.utilization_pct}% of range in use
                </span>
              </div>
              <div className="mb-4 h-1.5 overflow-hidden rounded-full bg-ink-600">
                <div
                  className={`h-full ${usage.utilization_pct > 80 ? 'bg-amber-400' : 'bg-sky-400'}`}
                  style={{ width: `${usage.utilization_pct}%` }}
                />
              </div>
              {usage.allocations.length === 0 ? (
                <Empty>No ports allocated on this server yet.</Empty>
              ) : (
                <div className="max-h-72 overflow-auto">
                  <table className="w-full text-xs">
                    <thead className="text-slate-500">
                      <tr className="border-b border-ink-600 text-left">
                        <th className="py-1.5">Port</th>
                        <th>Status</th>
                        <th>Purpose</th>
                        <th>Note</th>
                        <th className="text-right">Since</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-ink-600">
                      {usage.allocations.map((a: any) => (
                        <tr key={a.port}>
                          <td className="py-1.5 font-mono text-slate-200">{a.port}</td>
                          <td>
                            <span className={`pill ${a.status === 'allocated' ? 'bg-sky-500/15 text-sky-300' : 'bg-slate-500/15 text-slate-400'}`}>
                              {a.status}
                            </span>
                          </td>
                          <td className="text-slate-400">{a.purpose}</td>
                          <td className="max-w-[16rem] truncate text-slate-500">{a.note || '—'}</td>
                          <td className="text-right text-slate-600">{fmt.ago(a.allocated_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </Card>
      </div>

      <Card title="Generate an Nginx site">
        <div className="grid gap-4 sm:grid-cols-4">
          <div className="sm:col-span-2">
            <label className="label">Project</label>
            <select className="input" value={nginxForm.project_id} onChange={(e) => setNginxForm({ ...nginxForm, project_id: e.target.value })}>
              <option value="">Select…</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label">Domain</label>
            <input className="input" value={nginxForm.domain} onChange={(e) => setNginxForm({ ...nginxForm, domain: e.target.value })} />
          </div>
          <div>
            <label className="label">Upstream port</label>
            <input className="input" placeholder="auto" value={nginxForm.upstream_port} onChange={(e) => setNginxForm({ ...nginxForm, upstream_port: e.target.value })} />
          </div>
        </div>
        <div className="mt-3 flex gap-2">
          <button className="btn-ghost" onClick={previewNginx} disabled={!nginxForm.project_id || !nginxForm.domain}>
            Preview
          </button>
          <button className="btn-primary" onClick={createAndApply} disabled={!preview}>
            Create & apply
          </button>
        </div>
        <p className="mt-2 text-[11px] text-slate-500">
          Applying stages the file, runs <span className="font-mono">nginx -t</span>, reloads only if it passes, and
          restores the previous config if anything fails.
        </p>
        {preview && <pre className="logline mt-3 max-h-96 overflow-auto rounded bg-ink-900 p-3 text-slate-300">{preview}</pre>}
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Nginx sites">
          {configs.length === 0 ? (
            <Empty>No sites generated yet.</Empty>
          ) : (
            <table className="w-full text-xs">
              <tbody className="divide-y divide-ink-600">
                {configs.map((c) => (
                  <tr key={c.id}>
                    <td className="py-2 text-slate-200">{c.domain}</td>
                    <td className="font-mono text-slate-500">:{c.upstream_port}</td>
                    <td>v{c.version}</td>
                    <td>
                      <span
                        className={`pill ${
                          c.status === 'applied'
                            ? 'bg-emerald-500/15 text-emerald-300'
                            : c.status === 'failed'
                              ? 'bg-rose-500/15 text-rose-300'
                              : 'bg-slate-500/15 text-slate-400'
                        }`}
                      >
                        {c.status}
                      </span>
                    </td>
                    <td className="text-right text-slate-600">{fmt.ago(c.applied_at || c.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card title="Agent command audit">
          {commands.length === 0 ? (
            <Empty>No commands have been dispatched.</Empty>
          ) : (
            <ul className="space-y-1.5 text-xs">
              {commands.map((c) => (
                <li key={c.id} className="flex items-center gap-2">
                  <span
                    className={`pill ${
                      c.status === 'succeeded'
                        ? 'bg-emerald-500/15 text-emerald-300'
                        : c.status === 'failed'
                          ? 'bg-rose-500/15 text-rose-300'
                          : 'bg-slate-500/15 text-slate-400'
                    }`}
                  >
                    {c.status}
                  </span>
                  <span className="font-mono text-slate-300">{c.kind}</span>
                  <span className="ml-auto text-slate-600">{fmt.ago(c.created_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  )
}
