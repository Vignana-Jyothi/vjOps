import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Card, Empty, ErrorBox, Pill, ScoreRing, Spinner } from '../components/ui'
import { api, fmt } from '../lib/api'

const CATEGORY_LABEL: Record<string, string> = {
  containerization: 'Containerization',
  configuration: 'Configuration',
  security: 'Security',
  operability: 'Operability',
  build_quality: 'Build quality',
}

export default function ProjectDetail() {
  const { id } = useParams()
  const [overview, setOverview] = useState<any>(null)
  const [analysis, setAnalysis] = useState<any>(null)
  const [error, setError] = useState('')
  const [analyzing, setAnalyzing] = useState(false)
  const [tab, setTab] = useState<'findings' | 'plan' | 'artifacts' | 'detected'>('findings')
  const [openArtifact, setOpenArtifact] = useState<string>('')

  async function load() {
    try {
      const [o, list] = await Promise.all([
        api.get(`/api/projects/${id}/overview`),
        api.get(`/api/projects/${id}/analyses?limit=1`),
      ])
      setOverview(o)
      setAnalysis(list[0] || null)
    } catch (e: any) {
      setError(e.message)
    }
  }

  useEffect(() => {
    load()
  }, [id])

  async function runAnalysis() {
    setAnalyzing(true)
    setError('')
    try {
      const a = await api.post(`/api/projects/${id}/analyze`, { use_llm: true, expected_users: 50 })
      setAnalysis(a)
      load()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setAnalyzing(false)
    }
  }

  if (error && !overview) return <ErrorBox error={error} onRetry={load} />
  if (!overview) return <Spinner />

  const p = overview.project
  const findings = analysis?.findings || []
  const blockers = findings.filter((f: any) => f.severity === 'blocker')
  const res = analysis?.plan?.resources

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold">{p.name}</h1>
          <p className="font-mono text-xs text-slate-500">
            {p.slug}
            {p.github_repo && ` · ${p.github_repo}`}
            {p.domain && ` · ${p.domain}`}
          </p>
        </div>
        <button className="btn-primary" onClick={runAnalysis} disabled={analyzing || !p.github_repo}>
          {analyzing ? 'Analyzing repository…' : 'Run readiness analysis'}
        </button>
      </div>

      {error && <ErrorBox error={error} />}

      {!analysis ? (
        <Card title="Deployment readiness">
          <Empty>
            No analysis yet. Run one to clone the repository, detect the stack, scan for exposed secrets and
            generate a deployment plan.
          </Empty>
        </Card>
      ) : (
        <>
          <div className="grid gap-6 lg:grid-cols-3">
            <Card className="lg:col-span-1">
              <div className="flex items-center gap-5">
                <ScoreRing score={analysis.score} grade={analysis.grade} />
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-semibold text-slate-200">Deployment readiness</div>
                  <p className="mt-1 text-xs text-slate-500">
                    Analyzed {fmt.ago(analysis.created_at)} at commit{' '}
                    <span className="font-mono">{analysis.commit_sha?.slice(0, 8)}</span> in {analysis.duration_ms} ms.
                  </p>
                  {blockers.length > 0 ? (
                    <p className="mt-2 text-xs font-medium text-rose-300">
                      {blockers.length} blocker{blockers.length > 1 ? 's' : ''} — cannot deploy
                    </p>
                  ) : (
                    <p className="mt-2 text-xs font-medium text-emerald-300">No blockers — deployable</p>
                  )}
                </div>
              </div>

              <div className="mt-4 space-y-2">
                {Object.entries(analysis.breakdown || {}).map(([cat, b]: any) => (
                  <div key={cat}>
                    <div className="flex justify-between text-xs">
                      <span className="text-slate-400">{CATEGORY_LABEL[cat] || cat}</span>
                      <span className="text-slate-500">
                        {b.earned}/{b.weight}
                      </span>
                    </div>
                    <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-ink-600">
                      <div
                        className={`h-full ${b.earned / b.weight > 0.75 ? 'bg-emerald-400' : b.earned / b.weight > 0.4 ? 'bg-amber-400' : 'bg-rose-400'}`}
                        style={{ width: `${(b.earned / b.weight) * 100}%` }}
                      />
                    </div>
                  </div>
                ))}
              </div>
            </Card>

            <Card title="Reviewer notes" className="lg:col-span-2">
              {analysis.llm_review ? (
                <div className="space-y-2 text-sm text-slate-300">
                  {analysis.llm_review.split('\n\n').map((para: string, i: number) => (
                    <p key={i} className={para.startsWith('**') ? 'font-medium text-slate-100' : ''}>
                      {para.replace(/\*\*/g, '')}
                    </p>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-slate-500">
                  Static analysis only — the language model was unavailable for this run. The score and findings
                  below are unaffected.
                </p>
              )}

              {res && (
                <div className="mt-4 border-t border-ink-600 pt-4">
                  <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
                    Recommended resources
                  </div>
                  <div className="flex flex-wrap gap-4 text-sm">
                    <span><span className="text-slate-500">CPU</span> {res.cpu_cores} cores</span>
                    <span><span className="text-slate-500">RAM</span> {res.ram_mb} MB</span>
                    <span><span className="text-slate-500">Disk</span> {res.disk_gb} GB</span>
                    <span><span className="text-slate-500">Containers</span> {res.container_count}</span>
                    {res.needs_gpu && <span className="text-amber-300">GPU workload</span>}
                  </div>
                  {res.services?.length > 0 && (
                    <div className="mt-2 text-xs text-slate-500">Backing services: {res.services.join(', ')}</div>
                  )}
                  {res.warnings?.length > 0 && (
                    <ul className="mt-3 space-y-1 text-xs text-amber-300/90">
                      {res.warnings.map((w: string, i: number) => (
                        <li key={i}>⚠ {w}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </Card>
          </div>

          <Card>
            <div className="mb-4 flex gap-1 border-b border-ink-600">
              {(['findings', 'plan', 'artifacts', 'detected'] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className={`px-3 py-2 text-sm capitalize transition-colors ${
                    tab === t ? 'border-b-2 border-sky-500 text-slate-100' : 'text-slate-500 hover:text-slate-300'
                  }`}
                >
                  {t}
                  {t === 'findings' && ` (${findings.length})`}
                  {t === 'artifacts' && ` (${Object.keys(analysis.artifacts || {}).length})`}
                </button>
              ))}
            </div>

            {tab === 'findings' && (
              <div className="space-y-3">
                {findings.length === 0 && <Empty>Nothing flagged. This repository is in good shape.</Empty>}
                {findings.map((f: any) => (
                  <div key={f.id} className="rounded-md border border-ink-600 bg-ink-900/60 p-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <Pill tone={f.severity}>{f.severity}</Pill>
                      <span className="text-sm font-medium text-slate-100">{f.title}</span>
                      <span className="text-[11px] uppercase tracking-wide text-slate-600">{f.category}</span>
                    </div>
                    <p className="mt-2 text-sm text-slate-400">{f.detail}</p>
                    <p className="mt-2 text-sm text-emerald-300/90">
                      <span className="font-medium">Fix:</span> {f.fix}
                    </p>
                    {f.evidence?.length > 0 && (
                      <ul className="mt-2 space-y-0.5 font-mono text-[11px] text-slate-500">
                        {f.evidence.slice(0, 5).map((e: any, i: number) => (
                          <li key={i}>
                            {e.file}:{e.line} — {e.label} <span className="text-slate-600">{e.preview}</span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                ))}
              </div>
            )}

            {tab === 'plan' && (
              <ol className="space-y-2">
                {(analysis.plan?.steps || []).map((s: any) => (
                  <li key={s.n} className="flex gap-3 rounded-md border border-ink-600 bg-ink-900/60 p-3">
                    <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-ink-700 text-xs">{s.n}</span>
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium text-slate-200">{s.title}</span>
                        <span className={`pill ${s.automated ? 'bg-sky-500/15 text-sky-300' : 'bg-amber-500/15 text-amber-300'}`}>
                          {s.automated ? 'automated' : s.owner}
                        </span>
                      </div>
                      <p className="mt-1 text-xs text-slate-500">{s.detail}</p>
                    </div>
                  </li>
                ))}
                <li className="pt-2 text-xs text-slate-500">
                  Estimated time to deploy: {analysis.plan?.estimated_minutes} minutes
                </li>
              </ol>
            )}

            {tab === 'artifacts' && (
              <div className="space-y-2">
                {Object.keys(analysis.artifacts || {}).length === 0 && (
                  <Empty>No files needed to be generated — the repository already has what it needs.</Empty>
                )}
                {Object.entries(analysis.artifacts || {}).map(([name, content]: any) => (
                  <div key={name} className="rounded-md border border-ink-600">
                    <button
                      className="flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-ink-700/40"
                      onClick={() => setOpenArtifact(openArtifact === name ? '' : name)}
                    >
                      <span className="font-mono text-xs text-slate-200">{name}</span>
                      <span className="flex items-center gap-3 text-xs text-slate-500">
                        <span
                          role="button"
                          className="hover:text-sky-300"
                          onClick={(e) => {
                            e.stopPropagation()
                            navigator.clipboard?.writeText(content as string)
                          }}
                        >
                          copy
                        </span>
                        {openArtifact === name ? '▾' : '▸'}
                      </span>
                    </button>
                    {openArtifact === name && (
                      <pre className="logline max-h-96 overflow-auto border-t border-ink-600 bg-ink-900 p-3 text-slate-300">
                        {content as string}
                      </pre>
                    )}
                  </div>
                ))}
              </div>
            )}

            {tab === 'detected' && (
              <pre className="logline max-h-[32rem] overflow-auto rounded-md bg-ink-900 p-3 text-slate-400">
                {JSON.stringify(analysis.detected, null, 2)}
              </pre>
            )}
          </Card>
        </>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Recent deployments">
          {overview.deployments.length === 0 ? (
            <Empty>No deployments yet.</Empty>
          ) : (
            <table className="w-full text-sm">
              <tbody className="divide-y divide-ink-600">
                {overview.deployments.map((d: any) => (
                  <tr key={d.id}>
                    <td className="py-2">
                      <span
                        className={`pill ${
                          d.status === 'running'
                            ? 'bg-emerald-500/15 text-emerald-300'
                            : d.status === 'failed'
                              ? 'bg-rose-500/15 text-rose-300'
                              : 'bg-slate-500/15 text-slate-300'
                        }`}
                      >
                        {d.status}
                      </span>
                    </td>
                    <td className="py-2 font-mono text-xs text-slate-400">{d.commit_sha}</td>
                    <td className="py-2 text-xs text-slate-500">{d.port ? `:${d.port}` : '—'}</td>
                    <td className="py-2 text-right text-xs text-slate-500">{fmt.ago(d.started_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card title="Incidents">
          {overview.incidents.length === 0 ? (
            <Empty>No incidents for this project.</Empty>
          ) : (
            <div className="divide-y divide-ink-600">
              {overview.incidents.map((i: any) => (
                <Link key={i.id} to={`/incidents/${i.id}`} className="flex items-center gap-3 py-2 hover:bg-ink-700/40">
                  <Pill tone={i.severity}>{i.severity}</Pill>
                  <span className="min-w-0 flex-1 truncate text-sm">{i.title}</span>
                  <span className="text-xs text-slate-500">{fmt.ago(i.created_at)}</span>
                </Link>
              ))}
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}
