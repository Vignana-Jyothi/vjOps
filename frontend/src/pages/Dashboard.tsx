import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Card, ErrorBox, Empty, Pill, Spinner, Stat } from '../components/ui'
import { api, auth, fmt } from '../lib/api'

function GettingStarted({ data }: { data: any }) {
  const online = data.servers.online > 0
  const steps = [
    {
      done: data.servers.total > 0,
      title: 'Enroll a deployment server',
      detail: 'Infrastructure → Enroll server. You get a one-time agent token.',
      to: '/infrastructure',
    },
    {
      done: online,
      title: 'Install the agent on that server',
      detail: 'Run the install command shown after enrolling. The server turns green once the agent checks in.',
      to: '/infrastructure',
    },
    {
      done: data.projects > 0,
      title: 'Register a student project',
      detail: 'Projects → Register project. Link the GitHub repo and assign the student who owns it.',
      to: '/projects',
    },
    {
      done: data.setup.analyses > 0,
      title: 'Run a readiness analysis',
      detail: 'Open the project and click "Run readiness analysis" to score the repo and generate a Dockerfile and deploy plan.',
      to: '/projects',
    },
    {
      done: data.setup.deployments > 0,
      title: 'Deploy through GitHub Actions',
      detail: 'Add the generated workflow to the repo. Failed deployments show up under Incidents with a diagnosis.',
      to: '/projects',
    },
    {
      done: data.setup.users > 1,
      title: 'Invite your team',
      detail: 'Students can sign up from the login page. Promote mentors and DevOps staff from Users.',
      to: auth.can('admin') ? '/users' : '/projects',
    },
  ]
  const remaining = steps.filter((s) => !s.done).length
  if (remaining === 0) return null
  const next = steps.findIndex((s) => !s.done)

  return (
    <Card title={`Getting started · ${steps.length - remaining} of ${steps.length} done`}>
      <ol className="space-y-2">
        {steps.map((s, i) => (
          <li key={s.title}>
            <Link
              to={s.to}
              className={`flex items-start gap-3 rounded-md border p-3 transition-colors ${
                i === next ? 'border-sky-600/50 bg-sky-500/5 hover:bg-sky-500/10' : 'border-ink-600 hover:bg-ink-700/40'
              }`}
            >
              <span
                className={`grid h-6 w-6 shrink-0 place-items-center rounded-full text-xs ${
                  s.done ? 'bg-emerald-500/20 text-emerald-300' : i === next ? 'bg-sky-600 text-white' : 'bg-ink-700 text-slate-400'
                }`}
              >
                {s.done ? '✓' : i + 1}
              </span>
              <div className="min-w-0">
                <div className={`text-sm font-medium ${s.done ? 'text-slate-500 line-through' : 'text-slate-200'}`}>{s.title}</div>
                {!s.done && <p className="mt-0.5 text-xs text-slate-500">{s.detail}</p>}
              </div>
              {i === next && <span className="ml-auto shrink-0 text-xs text-sky-300">Start here →</span>}
            </Link>
          </li>
        ))}
      </ol>
    </Card>
  )
}

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
  const isStudent = auth.user?.role === 'student'

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold">Platform overview</h1>
        <p className="text-sm text-slate-500">
          {isStudent ? 'Live state of your team’s deployments.' : 'Live state of every student deployment across the incubator.'}
        </p>
      </div>

      {auth.can('admin', 'devops') && <GettingStarted data={data} />}

      {isStudent && data.projects === 0 && (
        <Card title="Welcome to ViljaOps">
          <p className="text-sm text-slate-400">
            No project is linked to your account yet. Ask your incubator's DevOps admin to assign your team's project to{' '}
            <span className="font-mono text-slate-200">{auth.user?.email}</span>. Once they do, you'll see its readiness
            score, deployments and any failure diagnoses here.
          </p>
        </Card>
      )}

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
          {!isStudent && <Card title="Servers">
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
          </Card>}

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
