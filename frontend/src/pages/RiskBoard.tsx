import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Card, Empty, ErrorBox, Spinner } from '../components/ui'
import { api, fmt, riskColor } from '../lib/api'

export default function RiskBoard() {
  const [data, setData] = useState<any>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')

  async function load() {
    try {
      setData(await api.get('/api/observability/risk'))
    } catch (e: any) {
      setError(e.message)
    }
  }

  useEffect(() => {
    load()
  }, [])

  async function evaluate(projectId: string) {
    setBusy(projectId)
    try {
      await api.post(`/api/observability/risk/${projectId}/evaluate`)
      await load()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  if (error && !data) return <ErrorBox error={error} onRetry={load} />
  if (!data) return <Spinner />

  const needing = data.projects.filter((p: any) => p.needs_mentor)

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold">Mentor board</h1>
        <p className="text-sm text-slate-500">Which teams are stuck, and what would unblock them.</p>
      </div>

      <div className="rounded-md border border-sky-500/20 bg-sky-500/5 p-3 text-xs text-slate-400">
        <span className="font-medium text-sky-300">How this works: </span>
        {data.note}
      </div>

      {error && <ErrorBox error={error} />}

      {needing.length > 0 && (
        <Card title={`Needs attention (${needing.length})`}>
          <div className="space-y-3">
            {needing.map((p: any) => (
              <div key={p.project_id} className="rounded-md border border-ink-600 bg-ink-900/60 p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`pill ${riskColor[p.band]}`}>{p.band}</span>
                  <Link to={`/projects/${p.project_id}`} className="text-sm font-medium text-slate-100 hover:text-sky-300">
                    {p.name}
                  </Link>
                  {p.team && <span className="text-xs text-slate-500">{p.team}</span>}
                  <span className="ml-auto text-xs text-slate-500">risk {p.risk_score}/100</span>
                </div>
                <p className="mt-2 text-sm text-amber-200/90">{p.recommendation}</p>
                <ul className="mt-2 space-y-1 text-xs text-slate-500">
                  {(p.signals || []).map((s: any, i: number) => (
                    <li key={i}>
                      <span className="font-mono text-slate-600">{s.kind}</span> — {s.detail}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Card title="All projects">
        {data.projects.length === 0 ? (
          <Empty>No projects yet.</Empty>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-xs text-slate-500">
              <tr className="border-b border-ink-600 text-left">
                <th className="py-2">Project</th>
                <th>Team</th>
                <th>Risk</th>
                <th>Signals</th>
                <th>Evaluated</th>
                <th />
              </tr>
            </thead>
            <tbody className="divide-y divide-ink-600">
              {data.projects.map((p: any) => (
                <tr key={p.project_id}>
                  <td className="py-2">
                    <Link to={`/projects/${p.project_id}`} className="hover:text-sky-300">
                      {p.name}
                    </Link>
                  </td>
                  <td className="text-xs text-slate-500">{p.team || '—'}</td>
                  <td>
                    <span className={`pill ${riskColor[p.band]}`}>{p.risk_score}</span>
                  </td>
                  <td className="text-xs text-slate-500">{(p.signals || []).length}</td>
                  <td className="text-xs text-slate-600">{p.evaluated_at ? fmt.ago(p.evaluated_at) : 'never'}</td>
                  <td className="text-right">
                    <button className="btn-ghost text-xs" disabled={busy === p.project_id} onClick={() => evaluate(p.project_id)}>
                      {busy === p.project_id ? '…' : 'Re-evaluate'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  )
}
