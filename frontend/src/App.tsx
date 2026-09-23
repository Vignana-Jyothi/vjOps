import { useEffect, useState } from 'react'
import { Link, Navigate, NavLink, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { api, auth } from './lib/api'
import Dashboard from './pages/Dashboard'
import Dataset from './pages/Dataset'
import IncidentDetail from './pages/IncidentDetail'
import Incidents from './pages/Incidents'
import Infrastructure from './pages/Infrastructure'
import Login from './pages/Login'
import ProjectDetail from './pages/ProjectDetail'
import Projects from './pages/Projects'
import RiskBoard from './pages/RiskBoard'

const NAV = [
  { to: '/', label: 'Overview', end: true },
  { to: '/projects', label: 'Projects' },
  { to: '/incidents', label: 'Incidents' },
  { to: '/infrastructure', label: 'Infrastructure', roles: ['admin', 'devops'] },
  { to: '/risk', label: 'Mentor board', roles: ['admin', 'devops', 'mentor'] },
  { to: '/dataset', label: 'Dataset', roles: ['admin', 'devops'] },
]

function SystemBanner() {
  const [status, setStatus] = useState<any>(null)
  useEffect(() => {
    api.get('/api/system/status').then(setStatus).catch(() => {})
  }, [])
  if (!status?.degraded_mode) return null
  return (
    <div className="border-b border-amber-500/30 bg-amber-500/10 px-6 py-2 text-xs text-amber-200">
      <strong>Degraded mode:</strong> the language model backend is unreachable. {status.degraded_note}
    </div>
  )
}

function Shell({ children }: { children: React.ReactNode }) {
  const user = auth.user
  const nav = useNavigate()
  const loc = useLocation()

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b border-ink-600 bg-ink-800/95 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center gap-6 px-6 py-3">
          <Link to="/" className="flex items-center gap-2">
            <span className="grid h-7 w-7 place-items-center rounded bg-sky-600 text-xs font-bold text-white">V</span>
            <span className="text-sm font-semibold tracking-tight">ViljaOps</span>
          </Link>
          <nav className="flex flex-1 items-center gap-1 overflow-x-auto">
            {NAV.filter((n) => !n.roles || auth.can(...n.roles)).map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.end}
                className={({ isActive }) =>
                  `whitespace-nowrap rounded-md px-3 py-1.5 text-sm transition-colors ${
                    isActive ? 'bg-ink-700 text-slate-100' : 'text-slate-400 hover:text-slate-200'
                  }`
                }
              >
                {n.label}
              </NavLink>
            ))}
          </nav>
          <div className="flex items-center gap-3 text-xs">
            <div className="text-right">
              <div className="text-slate-300">{user?.name || user?.email}</div>
              <div className="text-slate-500">{user?.role}</div>
            </div>
            <button
              className="btn-ghost"
              onClick={() => {
                auth.clear()
                nav('/login')
              }}
            >
              Sign out
            </button>
          </div>
        </div>
      </header>
      <SystemBanner />
      <main key={loc.pathname} className="mx-auto max-w-7xl px-6 py-6">
        {children}
      </main>
    </div>
  )
}

function Protected({ children }: { children: React.ReactNode }) {
  if (!auth.token) return <Navigate to="/login" replace />
  return <Shell>{children}</Shell>
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/" element={<Protected><Dashboard /></Protected>} />
      <Route path="/projects" element={<Protected><Projects /></Protected>} />
      <Route path="/projects/:id" element={<Protected><ProjectDetail /></Protected>} />
      <Route path="/incidents" element={<Protected><Incidents /></Protected>} />
      <Route path="/incidents/:id" element={<Protected><IncidentDetail /></Protected>} />
      <Route path="/infrastructure" element={<Protected><Infrastructure /></Protected>} />
      <Route path="/risk" element={<Protected><RiskBoard /></Protected>} />
      <Route path="/dataset" element={<Protected><Dataset /></Protected>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
