import React from 'react'
import { severityColor } from '../lib/api'

export function Pill({ children, tone = 'info' }: { children: React.ReactNode; tone?: string }) {
  return <span className={`pill ${severityColor[tone] || severityColor.info}`}>{children}</span>
}

export function Card({
  title,
  action,
  children,
  className = '',
}: {
  title?: React.ReactNode
  action?: React.ReactNode
  children: React.ReactNode
  className?: string
}) {
  return (
    <div className={`card ${className}`}>
      {(title || action) && (
        <div className="mb-3 flex items-center justify-between gap-3">
          <h3 className="text-sm font-semibold text-slate-200">{title}</h3>
          {action}
        </div>
      )}
      {children}
    </div>
  )
}

export function Stat({ label, value, sub, tone }: { label: string; value: React.ReactNode; sub?: string; tone?: string }) {
  return (
    <div className="card">
      <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${tone || 'text-slate-100'}`}>{value}</div>
      {sub && <div className="mt-1 text-xs text-slate-500">{sub}</div>}
    </div>
  )
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="rounded-md border border-dashed border-ink-600 p-6 text-center text-sm text-slate-500">{children}</div>
}

export function Spinner({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-slate-500">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-600 border-t-sky-400" />
      {label}…
    </div>
  )
}

export function ErrorBox({ error, onRetry }: { error: string; onRetry?: () => void }) {
  return (
    <div className="rounded-md border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-200">
      <div className="font-medium">Something went wrong</div>
      <pre className="mt-1 whitespace-pre-wrap text-xs text-rose-300/90">{error}</pre>
      {onRetry && (
        <button className="btn-ghost mt-3" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  )
}

/** Confidence is central to trusting this system, so it is always shown as a
 *  number with an explicit interpretation rather than a vague "high/low". */
export function Confidence({ value, source }: { value: number; source?: string }) {
  const pct = Math.round(value * 100)
  const tone = pct >= 80 ? 'text-emerald-300' : pct >= 50 ? 'text-amber-300' : 'text-rose-300'
  const bar = pct >= 80 ? 'bg-emerald-400' : pct >= 50 ? 'bg-amber-400' : 'bg-rose-400'
  const note =
    pct >= 80
      ? 'Strong evidence — safe to act on'
      : pct >= 50
        ? 'Plausible — verify before acting'
        : 'Weak — treat as a hint only'
  return (
    <div className="min-w-[190px]">
      <div className="flex items-baseline justify-between">
        <span className="text-xs uppercase tracking-wide text-slate-400">Confidence</span>
        <span className={`text-sm font-semibold ${tone}`}>{pct}%</span>
      </div>
      <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-ink-600">
        <div className={`h-full ${bar}`} style={{ width: `${pct}%` }} />
      </div>
      <div className="mt-1 text-[11px] text-slate-500">
        {note}
        {source ? ` · ${source}` : ''}
      </div>
    </div>
  )
}

export function ScoreRing({ score, grade }: { score: number; grade: string }) {
  const color = score >= 80 ? '#34d399' : score >= 60 ? '#fbbf24' : '#fb7185'
  const r = 42
  const c = 2 * Math.PI * r
  return (
    <div className="relative h-28 w-28">
      <svg viewBox="0 0 100 100" className="h-full w-full -rotate-90">
        <circle cx="50" cy="50" r={r} fill="none" stroke="#30363d" strokeWidth="9" />
        <circle
          cx="50"
          cy="50"
          r={r}
          fill="none"
          stroke={color}
          strokeWidth="9"
          strokeLinecap="round"
          strokeDasharray={`${(score / 100) * c} ${c}`}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-2xl font-bold" style={{ color }}>
          {score}
        </span>
        <span className="text-[11px] uppercase tracking-wide text-slate-500">grade {grade}</span>
      </div>
    </div>
  )
}

const SOURCE_TONE: Record<string, string> = {
  actions: 'text-violet-300',
  docker: 'text-sky-300',
  nginx: 'text-emerald-300',
  app: 'text-amber-300',
  system: 'text-slate-400',
}

export function SourceTag({ source }: { source: string }) {
  return <span className={`font-mono text-[10px] uppercase ${SOURCE_TONE[source] || 'text-slate-400'}`}>{source}</span>
}
