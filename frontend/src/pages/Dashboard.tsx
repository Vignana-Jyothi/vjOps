import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Card, ErrorBox, Empty, Pill, Spinner, Stat } from '../components/ui'
import { api, fmt } from '../lib/api'

export default function Dashboard() {
  const [data, setData] = useState<any>(null)
  const [anomalies, setAnomalies] = useState<any[]>([])
  const [incidents, setIncidents] = useState<any[]>([])
  const [error, setError] = useState('')

  async function load() {
    setError('')
    try {
      const [d, a, i] = await Promise.all([
        api.get('/api/observability/dashboard'),
        api.get('/api/observability/anomalies?acknowledged=false&limit=8'),
        api.get('/api/incidents?limit=8'),
      ])
      setData(d)
      setAnomalies(a)
      setIncidents(i)
    } catch (e: any) {
      setError(e.message)
    }
  }

  useEffect(() => {
    load()
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [])

  if (error) return <ErrorBox error={error} onRetry={load} />
  if (!data) return <Spinner />

  const dep = data.deployments_7d
  const inc = data.incidents

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold">Platform overview</h1>
        <p className="text-sm text-slate-500">Live state of every student deployment across the incubator.</p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Active projects" value={data.projects} />
        <Stat
          label="Deploy success (7d)"
          value={dep.success_rate === null ? '—' : fmt.pct(dep.success_rate)}
          sub={`${dep.total} deployments, ${dep.failed} failed`}
          tone={dep.success_rate === null ? '' : dep.success_rate > 0.8 ? 'text-emerald-300' : 'text-amber-300'}
        />
        <Stat
          label="Open incidents"
          value={inc.open}
          sub={`${inc.critical} critical · ${fmt.pct(inc.auto_diagnosis_rate)} auto-diagnosed`}
          tone={inc.critical > 0 ? 'text-rose-300' : ''}
        />
        <Stat
          label="Mean time to resolve"
          value={inc.mean_time_to_resolve_min ? `${inc.mean_time_to_resolve_min}m` : '—'}
          sub="incidents closed in the last 7 days"
        />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card title="Recent incidents" className="lg:col-span-2">
          {incidents.length === 0 ? (
            <Empty>No incidents. Every deployment is healthy.</Empty>
          ) : (
            <div className="divide-y divide-ink-600">
              {incidents.map((i) => (
                <Link key={i.id} to={`/incidents/${i.id}`} className="flex items-start gap-3 py-2.5 hover:bg-ink-700/40">
                  <Pill tone={i.severity}>{i.severity}</Pill>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm text-slate-200">{i.title}</div>
                    <div className="truncate text-xs text-slate-500">{i.root_cause}</div>
                  </div>
                  <div className="shrink-0 text-right text-xs text-slate-500">
                    <div>{fmt.pct(i.confidence)}</div>
                    <div>{fmt.ago(i.created_at)}</div>
                  </div>
                </Link>
              ))}
            </div>
          )}
        </Card>

        <div className="space-y-6">
          <Card title="Servers">
            {data.servers.names.length === 0 ? (
              <Empty>No servers enrolled yet.</Empty>
            ) : (
              <ul className="space-y-2">
                {data.servers.names.map((s: any) => (
                  <li key={s.name} className="flex items-center justify-between text-sm">
                    <span className="flex items-center gap-2">
                      <span className={`h-2 w-2 rounded-full ${s.online ? 'bg-emerald-400' : 'bg-rose-400'}`} />
                      {s.name}
                    </span>
                    <span className="text-xs text-slate-500">{s.online ? 'online' : fmt.ago(s.last_seen)}</span>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card title={`Predicted failures (${anomalies.length})`}>
            {anomalies.length === 0 ? (
              <Empty>Nothing trending toward failure.</Empty>
            ) : (
              <ul className="space-y-3">
                {anomalies.map((a) => (
                  <li key={a.id} className="text-xs">
                    <div className="flex items-center gap-2">
                      <Pill tone={a.severity}>{a.kind.replace(/_/g, ' ')}</Pill>
                    </div>
                    <p className="mt-1 text-slate-400">{a.message}</p>
                    {a.prediction && <p className="mt-0.5 font-medium text-amber-300">{a.prediction}</p>}
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>
    </div>
  )
}
