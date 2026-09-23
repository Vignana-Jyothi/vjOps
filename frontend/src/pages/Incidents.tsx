import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Card, Empty, ErrorBox, Pill, Spinner } from '../components/ui'
import { api, auth, fmt } from '../lib/api'

const STATUSES = ['', 'open', 'diagnosed', 'fixing', 'resolved', 'dismissed']

export default function Incidents() {
  const [items, setItems] = useState<any[]>([])
  const [accuracy, setAccuracy] = useState<any>(null)
  const [status, setStatus] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  async function load() {
    setLoading(true)
    try {
      setItems(await api.get(`/api/incidents?limit=100${status ? `&status=${status}` : ''}`))
      if (auth.can('admin', 'devops')) setAccuracy(await api.get('/api/incidents/meta/accuracy').catch(() => null))
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [status])

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold">Incidents</h1>
          <p className="text-sm text-slate-500">Deployment failures diagnosed from correlated logs and metrics.</p>
        </div>
        <div className="flex gap-1">
          {STATUSES.map((s) => (
            <button
              key={s || 'all'}
              onClick={() => setStatus(s)}
              className={`rounded-md px-2.5 py-1 text-xs capitalize ${
                status === s ? 'bg-ink-700 text-slate-100' : 'text-slate-500 hover:text-slate-300'
              }`}
            >
              {s || 'all'}
            </button>
          ))}
        </div>
      </div>

      {accuracy?.confirmed_incidents > 0 && (
        <Card title="Diagnosis accuracy">
          <div className="flex flex-wrap items-center gap-6 text-sm">
            <div>
              <span className="text-2xl font-semibold text-slate-100">{fmt.pct(accuracy.accuracy)}</span>
              <span className="ml-2 text-xs text-slate-500">
                across {accuracy.confirmed_incidents} human-confirmed incidents
              </span>
            </div>
            {Object.entries(accuracy.by_analysis_source || {}).map(([src, v]: any) => (
              <div key={src} className="text-xs text-slate-400">
                <span className="text-slate-600">{src}</span> {fmt.pct(v.accuracy)} ({v.total})
              </div>
            ))}
          </div>
          <p className="mt-2 text-xs text-slate-600">{accuracy.note}</p>
        </Card>
      )}

      {error && <ErrorBox error={error} onRetry={load} />}
      {loading ? (
        <Spinner />
      ) : items.length === 0 ? (
        <Empty>No incidents match this filter.</Empty>
      ) : (
        <div className="space-y-2">
          {items.map((i) => (
            <Link key={i.id} to={`/incidents/${i.id}`} className="card flex flex-wrap items-start gap-4 transition-colors hover:border-sky-600/50">
              <div className="flex shrink-0 flex-col gap-1">
                <Pill tone={i.severity}>{i.severity}</Pill>
                <span className="text-[10px] uppercase tracking-wide text-slate-600">{i.stage}</span>
              </div>
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium text-slate-100">{i.title}</div>
                <div className="mt-0.5 text-sm text-slate-400">{i.root_cause}</div>
                <div className="mt-1 flex flex-wrap gap-3 text-[11px] text-slate-600">
                  <span>{i.status}</span>
                  {i.signature_key && <span className="font-mono">{i.signature_key}</span>}
                  <span>{i.analysis_source}</span>
                  <span>{fmt.ago(i.created_at)}</span>
                </div>
              </div>
              <div className="shrink-0 text-right">
                <div
                  className={`text-lg font-semibold ${
                    i.confidence >= 0.8 ? 'text-emerald-300' : i.confidence >= 0.5 ? 'text-amber-300' : 'text-rose-300'
                  }`}
                >
                  {fmt.pct(i.confidence)}
                </div>
                <div className="text-[10px] uppercase tracking-wide text-slate-600">confidence</div>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}
