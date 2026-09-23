const TOKEN_KEY = 'viljaops_token'
const USER_KEY = 'viljaops_user'

export type User = { email: string; name: string; role: string }

export const auth = {
  get token() {
    return localStorage.getItem(TOKEN_KEY)
  },
  get user(): User | null {
    try {
      return JSON.parse(localStorage.getItem(USER_KEY) || 'null')
    } catch {
      return null
    }
  },
  set(token: string, user: User) {
    localStorage.setItem(TOKEN_KEY, token)
    localStorage.setItem(USER_KEY, JSON.stringify(user))
  },
  clear() {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
  },
  can(...roles: string[]) {
    const r = auth.user?.role
    return !!r && roles.includes(r)
  },
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((init.headers as Record<string, string>) || {}),
  }
  if (auth.token) headers.Authorization = `Bearer ${auth.token}`

  const res = await fetch(path, { ...init, headers })

  if (res.status === 401) {
    auth.clear()
    if (!location.pathname.startsWith('/login')) location.href = '/login'
    throw new ApiError(401, 'Session expired')
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? body)
    } catch {
      /* keep the status text */
    }
    throw new ApiError(res.status, detail)
  }
  if (res.status === 204) return null as T
  return res.json()
}

export const api = {
  get: <T = any>(p: string) => request<T>(p),
  post: <T = any>(p: string, body?: any) =>
    request<T>(p, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: <T = any>(p: string, body: any) => request<T>(p, { method: 'PATCH', body: JSON.stringify(body) }),

  async login(email: string, password: string) {
    const form = new URLSearchParams({ username: email, password })
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: form,
    })
    if (!res.ok) throw new ApiError(res.status, res.status === 401 ? 'Incorrect email or password' : 'Login failed')
    const data = await res.json()
    auth.set(data.access_token, { email: data.email, name: data.name, role: data.role })
    return data
  },
}

// ---------------------------------------------------------------- helpers
export const fmt = {
  time(iso?: string | null) {
    if (!iso) return '—'
    return new Date(iso).toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  },
  ago(iso?: string | null) {
    if (!iso) return '—'
    const s = (Date.now() - new Date(iso).getTime()) / 1000
    if (s < 60) return 'just now'
    if (s < 3600) return `${Math.floor(s / 60)}m ago`
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`
    return `${Math.floor(s / 86400)}d ago`
  },
  pct(v?: number | null) {
    return v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`
  },
}

export const severityColor: Record<string, string> = {
  critical: 'bg-rose-500/15 text-rose-300 border border-rose-500/30',
  blocker: 'bg-rose-500/15 text-rose-300 border border-rose-500/30',
  high: 'bg-orange-500/15 text-orange-300 border border-orange-500/30',
  warning: 'bg-amber-500/15 text-amber-300 border border-amber-500/30',
  medium: 'bg-amber-500/15 text-amber-300 border border-amber-500/30',
  low: 'bg-sky-500/15 text-sky-300 border border-sky-500/30',
  info: 'bg-slate-500/15 text-slate-300 border border-slate-500/30',
}

export const riskColor: Record<string, string> = {
  red: 'bg-rose-500/15 text-rose-300 border border-rose-500/30',
  amber: 'bg-amber-500/15 text-amber-300 border border-amber-500/30',
  green: 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30',
  unknown: 'bg-slate-500/15 text-slate-400 border border-slate-500/30',
}
