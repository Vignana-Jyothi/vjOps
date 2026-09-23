import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Card, Confidence, Empty, ErrorBox, Pill, SourceTag, Spinner } from '../components/ui'
import { api, auth, fmt } from '../lib/api'

const RISK_TONE: Record<string, string> = {
  safe: 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30',
  moderate: 'bg-amber-500/15 text-amber-300 border border-amber-500/30',
  dangerous: 'bg-rose-500/15 text-rose-300 border border-rose-500/30',
}

const FIX_STATUS_TONE: Record<string, string> = {
  proposed: 'bg-slate-500/15 text-slate-300',
  approved: 'bg-sky-500/15 text-sky-300',
  executing: 'bg-sky-500/15 text-sky-300',
  succeeded: 'bg-emerald-500/15 text-emerald-300',
  failed: 'bg-rose-500/15 text-rose-300',
  rejected: 'bg-slate-500/15 text-slate-500',
}

const VERIFY_TONE: Record<string, string> = {
  pending: 'bg-amber-500/15 text-amber-300 border border-amber-500/30',
  passed: 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30',
  failed: 'bg-rose-500/15 text-rose-300 border border-rose-500/30',
  unknown: 'bg-slate-500/15 text-slate-400 border border-slate-500/30',
}

const VERIFY_LABEL: Record<string, string> = {
  pending: 'Verifying…',
  passed: 'Verified',
  failed: 'Verification failed',
  unknown: 'Could not verify',
}

const TRACE_STAGE_LABEL: Record<string, string> = {
  repository: 'Repository Agent',
  investigator: 'Incident Investigator',
  remediation: 'Remediation Agent',
  verification: 'Verification Agent',
}

const TRACE_OUTCOME_TONE: Record<string, string> = {
  info: 'text-slate-500',
  pass: 'text-emerald-400',
  fail: 'text-rose-400',
  escalate: 'text-amber-400',
}

export default function IncidentDetail() {
  const { id } = useParams()
  const [data, setData] = useState<any>(null)
  const [timeline, setTimeline] = useState<any>(null)
  const [trace, setTrace] = useState<any[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [confirmForm, setConfirmForm] = useState({ confirmed_root_cause: '', confirmed_fix: '', was_ai_correct: true })
  const [envValue, setEnvValue] = useState<Record<string, string>>({})
  const canApprove = auth.can('admin', 'devops')

  async function load() {
    try {
      const d = await api.get(`/api/incidents/${id}`)
      setData(d)
      setConfirmForm((f) => ({ ...f, confirmed_root_cause: f.confirmed_root_cause || d.incident.root_cause }))
      if (d.deployment?.id) {
        api.get(`/api/deployments/${d.deployment.id}/timeline`).then(setTimeline).catch(() => {})
      }
      api.get(`/api/incidents/${id}/trace`).then(setTrace).catch(() => {})
    } catch (e: any) {
      setError(e.message)
    }
  }

  useEffect(() => {
    load()
  }, [id])

  // A pending fix's verdict can change on its own (the beat schedule resolves
  // it in the background) — poll gently while anything is still pending so
  // the badge updates without a manual refresh.
  useEffect(() => {
    const anyPending = (data?.fixes || []).some((f: any) => f.verification_status === 'pending')
    if (!anyPending) return
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [data])

  async function forceVerify(fix: any) {
    setBusy(`verify-${fix.id}`)
    try {
      await api.post(`/api/incidents/fixes/${fix.id}/verify`, {})
      await load()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  async function approve(fix: any) {
    setBusy(fix.id)
    setError('')
    try {
      const params = fix.action_type === 'set_env_var' ? { value: envValue[fix.id] || '' } : {}
      await api.post(`/api/incidents/fixes/${fix.id}/approve`, { params, note: '' })
      await load()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  async function reject(fix: any) {
    setBusy(fix.id)
    try {
      await api.post(`/api/incidents/fixes/${fix.id}/reject`, { note: 'Not the right fix' })
      await load()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  async function confirm(e: React.FormEvent) {
    e.preventDefault()
    setBusy('confirm')
    try {
      await api.post(`/api/incidents/${id}/confirm`, { ...confirmForm, add_to_knowledge_base: true })
      setConfirmOpen(false)
      await load()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  if (error && !data) return <ErrorBox error={error} onRetry={load} />
  if (!data) return <Spinner />

  const i = data.incident
  const evidenceIds = new Set((i.evidence || []).map((e: any) => e.id))

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Pill tone={i.severity}>{i.severity}</Pill>
            <span className="text-[11px] uppercase tracking-wide text-slate-600">{i.stage}</span>
            <span className="text-[11px] text-slate-600">{i.status}</span>
            {i.auto_verified && (
              <span className="pill bg-emerald-500/15 text-emerald-300 border border-emerald-500/30">
                operationally verified
              </span>
            )}
          </div>
          <h1 className="mt-1 text-lg font-semibold">{i.title}</h1>
          <p className="text-xs text-slate-500">
            {data.project && (
              <Link to={`/projects/${data.project.id}`} className="hover:text-sky-300">
                {data.project.name}
              </Link>
            )}
            {data.deployment && ` · commit ${data.deployment.commit_sha} · port ${data.deployment.port ?? '—'}`}
            {' · '}
            {fmt.time(i.created_at)}
          </p>
        </div>
        <Confidence value={i.confidence} source={i.analysis_source} />
      </div>

      {error && <ErrorBox error={error} />}

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <Card title="Root cause">
            <p className="text-base text-slate-100">{i.root_cause}</p>
            {i.explanation && <p className="mt-3 text-sm text-slate-400">{i.explanation}</p>}
            {i.student_explanation && (
              <div className="mt-4 rounded-md border border-sky-500/20 bg-sky-500/5 p-3">
                <div className="text-[11px] font-semibold uppercase tracking-wide text-sky-300">In plain language</div>
                <p className="mt-1 text-sm text-slate-300">{i.student_explanation}</p>
              </div>
            )}
          </Card>

          <Card title={`Evidence (${(i.evidence || []).length} cited lines)`}>
            {(i.evidence || []).length === 0 ? (
              <Empty>No evidence was cited, which is why confidence is low.</Empty>
            ) : (
              <div className="space-y-3">
                {i.evidence.map((e: any, idx: number) => (
                  <div key={idx} className="rounded-md border border-ink-600 bg-ink-900/60 p-3">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-[10px] text-slate-600">{e.id}</span>
                      <SourceTag source={e.source} />
                    </div>
                    <pre className="logline mt-1.5 text-slate-300">{e.line}</pre>
                    {e.why && <p className="mt-1.5 text-xs italic text-slate-500">{e.why}</p>}
                  </div>
                ))}
              </div>
            )}
          </Card>

          {i.correlation?.signals?.length > 0 && (
            <Card title="Why these sources were correlated">
              <ul className="space-y-2">
                {i.correlation.signals.map((s: any, idx: number) => (
                  <li key={idx} className="rounded-md border border-ink-600 bg-ink-900/60 p-3 text-sm">
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-[11px] text-sky-300">{s.kind}</span>
                      <span className="text-[11px] text-slate-600">{Math.round((s.confidence || 0) * 100)}%</span>
                    </div>
                    <p className="mt-1 text-slate-400">{s.detail}</p>
                  </li>
                ))}
              </ul>
              <p className="mt-3 text-[11px] text-slate-600">
                {i.correlation.events_considered} raw events reduced to {i.correlation.events_in_timeline} across{' '}
                {(i.correlation.sources || []).join(', ') || 'no sources'}.
                {i.correlation.hallucinated_evidence_dropped > 0 &&
                  ` ${i.correlation.hallucinated_evidence_dropped} unverifiable citation(s) were dropped.`}
              </p>
            </Card>
          )}

          {timeline && (
            <Card title={`Correlated timeline (${timeline.events.length} events)`}>
              <div className="max-h-96 overflow-auto rounded-md bg-ink-900 p-3">
                {timeline.events.map((e: any) => (
                  <div
                    key={e.id}
                    className={`logline border-l-2 py-0.5 pl-2 ${
                      evidenceIds.has(e.id)
                        ? 'border-sky-500 bg-sky-500/5'
                        : e.level === 'error' || e.level === 'critical'
                          ? 'border-rose-500/50'
                          : 'border-transparent'
                    }`}
                  >
                    <span className="text-slate-600">{e.id} </span>
                    <span className="text-slate-500">{new Date(e.ts).toLocaleTimeString()} </span>
                    <SourceTag source={e.source} />
                    <span className={e.level === 'error' || e.level === 'critical' ? ' text-rose-300' : ' text-slate-400'}>
                      {e.count > 1 ? ` (x${e.count})` : ''} {e.message}
                    </span>
                  </div>
                ))}
              </div>
            </Card>
          )}
          {trace.length > 0 && (
            <Card title="Agent trace">
              <p className="mb-3 text-[11px] text-slate-600">
                One line per stage of the pipeline — this is not a separate log, it is written by the same
                code that did the work, as it ran.
              </p>
              <ol className="space-y-3 border-l border-ink-600 pl-4">
                {trace.map((t: any) => (
                  <li key={t.id} className="relative">
                    <span className="absolute -left-[21px] top-1 h-2 w-2 rounded-full bg-ink-500" />
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-[11px] font-semibold text-slate-300">
                        {TRACE_STAGE_LABEL[t.stage] || t.stage}
                      </span>
                      <span className={`text-[11px] ${TRACE_OUTCOME_TONE[t.outcome] || 'text-slate-500'}`}>
                        {t.outcome === 'pass' ? '✓' : t.outcome === 'fail' ? '✕' : t.outcome === 'escalate' ? '⚠' : ''}
                      </span>
                      <span className="text-[10px] text-slate-600">{fmt.ago(t.created_at)}</span>
                    </div>
                    <p className="mt-0.5 text-xs text-slate-400">{t.summary}</p>
                  </li>
                ))}
              </ol>
            </Card>
          )}
        </div>

        <div className="space-y-6">
          <Card title="Recommended fixes">
            {!data.auto_remediation_enabled && (
              <p className="mb-3 rounded-md border border-ink-600 bg-ink-900/60 p-2 text-[11px] text-slate-500">
                Nothing runs without approval. Every executed action records who approved it.
              </p>
            )}
            <div className="space-y-3">
              {data.fixes.map((f: any) => (
                <div key={f.id} className="rounded-md border border-ink-600 bg-ink-900/60 p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={`pill ${RISK_TONE[f.risk]}`}>{f.risk}</span>
                    <span className={`pill ${FIX_STATUS_TONE[f.status]}`}>{f.status}</span>
                    {f.requires_code_change && <span className="pill bg-violet-500/15 text-violet-300">code change</span>}
                  </div>
                  <div className="mt-2 text-sm font-medium text-slate-100">{f.title}</div>
                  <p className="mt-1 text-xs text-slate-400">{f.rationale}</p>
                  <p className="mt-1 font-mono text-[10px] text-slate-600">{f.action_type}</p>

                  {f.patch && (
                    <pre className="logline mt-2 max-h-40 overflow-auto rounded bg-ink-900 p-2 text-emerald-300/90">{f.patch}</pre>
                  )}

                  {f.action_spec?.verify && f.action_spec.executable && (
                    <p className="mt-2 text-[11px] text-slate-600">Verified by: {f.action_spec.verify}</p>
                  )}

                  {f.verification_status && f.verification_status !== 'not_applicable' && (
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <span className={`pill ${VERIFY_TONE[f.verification_status] || VERIFY_TONE.unknown}`}>
                        {VERIFY_LABEL[f.verification_status] || f.verification_status}
                      </span>
                      {f.verification_status === 'pending' && (
                        <span className="text-[11px] text-slate-600">
                          watching for {f.verification_detail?.grace_minutes ?? '?'} min
                        </span>
                      )}
                      {f.verification_status === 'pending' && canApprove && (
                        <button
                          className="btn-ghost px-2 py-0.5 text-[11px]"
                          disabled={busy === `verify-${f.id}`}
                          onClick={() => forceVerify(f)}
                        >
                          {busy === `verify-${f.id}` ? 'Checking…' : 'Check now'}
                        </button>
                      )}
                      {f.verification_status === 'failed' && f.verification_detail?.evidence?.length > 0 && (
                        <p className="w-full text-[11px] text-rose-300/80">
                          {f.verification_detail.evidence.map((e: any) => e.message || e.detail).filter(Boolean).join('; ')}
                        </p>
                      )}
                    </div>
                  )}

                  {f.status === 'proposed' && canApprove && f.action_spec?.executable && (
                    <div className="mt-3 space-y-2">
                      {f.action_type === 'set_env_var' && (
                        <div>
                          <label className="label">Value for {f.params?.key}</label>
                          <input
                            className="input"
                            type="password"
                            placeholder="stored write-only, never logged"
                            value={envValue[f.id] || ''}
                            onChange={(e) => setEnvValue({ ...envValue, [f.id]: e.target.value })}
                          />
                        </div>
                      )}
                      {f.risk === 'dangerous' && (
                        <p className="rounded border border-rose-500/30 bg-rose-500/10 p-2 text-[11px] text-rose-300">
                          This action is not reversible. Confirm you have a backup before approving.
                        </p>
                      )}
                      <div className="flex gap-2">
                        <button
                          className={f.risk === 'dangerous' ? 'btn-danger' : 'btn-primary'}
                          disabled={busy === f.id}
                          onClick={() => approve(f)}
                        >
                          {busy === f.id ? 'Dispatching…' : 'Approve & run'}
                        </button>
                        <button className="btn-ghost" disabled={busy === f.id} onClick={() => reject(f)}>
                          Reject
                        </button>
                      </div>
                    </div>
                  )}

                  {f.status === 'proposed' && !f.action_spec?.executable && (
                    <p className="mt-2 text-[11px] text-slate-500">
                      Advisory — the team applies this in their repository.
                    </p>
                  )}

                  {f.approved_at && (
                    <p className="mt-2 text-[11px] text-slate-600">
                      {f.status === 'rejected' ? 'Rejected' : 'Approved'} {fmt.ago(f.approved_at)}
                    </p>
                  )}
                  {f.result && Object.keys(f.result).length > 0 && (
                    <pre className="logline mt-2 max-h-40 overflow-auto rounded bg-ink-900 p-2 text-slate-500">
                      {JSON.stringify(f.result, null, 2)}
                    </pre>
                  )}
                </div>
              ))}
            </div>
          </Card>

          {i.similar_incidents?.length > 0 && (
            <Card title="Similar past incidents">
              <ul className="space-y-2 text-xs">
                {i.similar_incidents.map((s: any, idx: number) => (
                  <li key={idx} className="rounded border border-ink-600 p-2">
                    <div className="flex justify-between">
                      <span className="text-slate-300">{s.title}</span>
                      <span className="text-slate-600">{Math.round(s.similarity * 100)}%</span>
                    </div>
                    <p className="mt-1 line-clamp-3 text-slate-500">{s.content}</p>
                  </li>
                ))}
              </ul>
            </Card>
          )}

          {canApprove && (
            <Card title="Close this incident">
              {i.confirmed_root_cause ? (
                <div className="space-y-2 text-sm">
                  <div>
                    <div className="label">Confirmed cause</div>
                    <p className="text-slate-300">{i.confirmed_root_cause}</p>
                  </div>
                  {i.confirmed_fix && (
                    <div>
                      <div className="label">Fix applied</div>
                      <p className="text-slate-300">{i.confirmed_fix}</p>
                    </div>
                  )}
                  <p className={i.was_ai_correct ? 'text-emerald-300' : 'text-amber-300'}>
                    {i.was_ai_correct ? 'AI diagnosis was correct' : 'AI diagnosis was wrong — recorded for retraining'}
                  </p>
                </div>
              ) : !confirmOpen ? (
                <>
                  <p className="mb-3 text-xs text-slate-500">
                    Recording what actually caused this turns the incident into a labelled example. This is what
                    makes the local model better over the semester.
                  </p>
                  <button className="btn-primary w-full justify-center" onClick={() => setConfirmOpen(true)}>
                    Confirm root cause
                  </button>
                </>
              ) : (
                <form onSubmit={confirm} className="space-y-3">
                  <div>
                    <label className="label">What actually caused it</label>
                    <textarea
                      className="input h-20"
                      required
                      value={confirmForm.confirmed_root_cause}
                      onChange={(e) => setConfirmForm({ ...confirmForm, confirmed_root_cause: e.target.value })}
                    />
                  </div>
                  <div>
                    <label className="label">What fixed it</label>
                    <textarea
                      className="input h-16"
                      value={confirmForm.confirmed_fix}
                      onChange={(e) => setConfirmForm({ ...confirmForm, confirmed_fix: e.target.value })}
                    />
                  </div>
                  <label className="flex items-center gap-2 text-sm text-slate-300">
                    <input
                      type="checkbox"
                      checked={confirmForm.was_ai_correct}
                      onChange={(e) => setConfirmForm({ ...confirmForm, was_ai_correct: e.target.checked })}
                    />
                    The AI diagnosis was correct
                  </label>
                  <div className="flex gap-2">
                    <button className="btn-primary" disabled={busy === 'confirm'}>
                      Save & close
                    </button>
                    <button type="button" className="btn-ghost" onClick={() => setConfirmOpen(false)}>
                      Cancel
                    </button>
                  </div>
                </form>
              )}
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}
