import { useEffect, useState } from 'react'
import { Card, Empty, ErrorBox, Spinner, Stat } from '../components/ui'
import { api } from '../lib/api'

export default function Dataset() {
  const [stats, setStats] = useState<any>(null)
  const [card, setCard] = useState<any>(null)
  const [sample, setSample] = useState<any>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')

  useEffect(() => {
    api.get('/api/dataset/stats').then(setStats).catch((e) => setError(e.message))
  }, [])

  async function exportNow() {
    setBusy('export')
    try {
      setCard(await api.post('/api/dataset/export?eval_fraction=0.2'))
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  async function loadPreview() {
    setBusy('preview')
    try {
      setSample(await api.get('/api/dataset/preview?limit=1'))
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  if (error && !stats) return <ErrorBox error={error} />
  if (!stats) return <Spinner />

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold">Incident dataset</h1>
        <p className="text-sm text-slate-500">
          Confirmed incidents become training and evaluation data for the self-hosted model.
        </p>
      </div>

      {error && <ErrorBox error={error} />}

      <div className="grid gap-4 sm:grid-cols-3">
        <Stat label="Total incidents" value={stats.incidents_total} />
        <Stat label="Human-confirmed" value={stats.confirmed} tone="text-emerald-300" sub="exportable" />
        <Stat label="Awaiting confirmation" value={stats.unconfirmed} tone={stats.unconfirmed > 0 ? 'text-amber-300' : ''} />
      </div>

      <Card title="Why this matters">
        <p className="text-sm text-slate-400">{stats.guidance}</p>
      </Card>

      <Card title="Coverage by failure signature">
        {Object.keys(stats.by_signature).length === 0 ? (
          <Empty>No confirmed incidents yet. Confirm root causes as you resolve them.</Empty>
        ) : (
          <div className="space-y-1.5">
            {Object.entries(stats.by_signature).map(([sig, n]: any) => {
              const max = Math.max(...(Object.values(stats.by_signature) as number[]))
              return (
                <div key={sig} className="flex items-center gap-3">
                  <span className="w-64 shrink-0 truncate font-mono text-xs text-slate-400">{sig}</span>
                  <div className="h-2 flex-1 overflow-hidden rounded-full bg-ink-600">
                    <div className="h-full bg-sky-400" style={{ width: `${(n / max) * 100}%` }} />
                  </div>
                  <span className="w-8 text-right text-xs text-slate-500">{n}</span>
                </div>
              )
            })}
          </div>
        )}
      </Card>

      <Card
        title="Export"
        action={
          <div className="flex gap-2">
            <button className="btn-ghost" onClick={loadPreview} disabled={busy === 'preview'}>
              Preview a record
            </button>
            <button className="btn-primary" onClick={exportNow} disabled={busy === 'export' || stats.confirmed === 0}>
              {busy === 'export' ? 'Exporting…' : 'Export JSONL'}
            </button>
          </div>
        }
      >
        <p className="text-xs text-slate-500">
          Emails, hostnames, IPs, home paths and repository names are pseudonymized consistently within each record;
          credentials are destroyed outright. The split is stratified by failure signature so the eval set measures
          generalization, not memorization.
        </p>

        {card && (
          <div className="mt-4 space-y-3">
            <div className="flex flex-wrap gap-4 text-sm">
              <span><span className="text-slate-500">train</span> {card.train}</span>
              <span><span className="text-slate-500">eval</span> {card.eval}</span>
              <span><span className="text-slate-500">total</span> {card.total_records}</span>
            </div>
            <div className="flex gap-2">
              {['train', 'eval', 'card'].map((s) => (
                <a key={s} className="btn-ghost" href={`/api/dataset/download/${s}`}>
                  Download {s}
                </a>
              ))}
            </div>
            {card.caveats && (
              <ul className="space-y-1 text-[11px] text-amber-300/80">
                {card.caveats.map((c: string, i: number) => (
                  <li key={i}>⚠ {c}</li>
                ))}
              </ul>
            )}
          </div>
        )}

        {sample?.sample?.length > 0 && (
          <pre className="logline mt-4 max-h-96 overflow-auto rounded bg-ink-900 p-3 text-slate-400">
            {JSON.stringify(sample.sample[0], null, 2)}
          </pre>
        )}
      </Card>
    </div>
  )
}
