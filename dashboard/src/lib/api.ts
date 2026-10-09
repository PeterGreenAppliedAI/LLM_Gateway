import { gatewayErrorMessage } from './forms'
import type { Stats, Request, RequestDetail, Catalog, HealthResponse, SecurityAlert, SecurityStats, SecurityResult, ApiKeyInfo, BudgetConfig, BudgetUsage, SecurityScan, LabelStats, PIIStats, PIIEvent, PIIConfig, PIIMLView, PIIMLAction, MediaEndpoint } from '../types'

// API base URL - gateway server
// Unset: the dev server talks to a local gateway. Empty string: same origin
// (the Docker image proxies the API behind the dashboard, D-044).
export const API_BASE = (import.meta.env.VITE_API_URL ?? 'http://localhost:8001').replace(/\/$/, '')

// Gateway API key: entered in the header, kept in localStorage, sent on every request
const API_KEY_STORAGE = 'gateway_api_key'

export function getStoredApiKey(): string {
  return localStorage.getItem(API_KEY_STORAGE) || ''
}

export function setStoredApiKey(key: string): void {
  if (key) localStorage.setItem(API_KEY_STORAGE, key)
  else localStorage.removeItem(API_KEY_STORAGE)
}

// Fired whenever the gateway rejects our key, so the UI can show an
// explicit "key required/invalid" state instead of silently-empty tables.
export const AUTH_ERROR_EVENT = 'gateway-auth-error'

export async function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const key = getStoredApiKey()
  const headers = new Headers(init.headers)
  if (key) headers.set('X-API-Key', key)
  const res = await fetch(input, { ...init, headers })
  if (res.status === 401) {
    window.dispatchEvent(new CustomEvent(AUTH_ERROR_EVENT))
  }
  return res
}

// Fetch helpers
export async function fetchStats(hours = 24): Promise<Stats> {
  const res = await apiFetch(`${API_BASE}/api/stats?hours=${hours}`)
  if (!res.ok) throw new Error(`stats: HTTP ${res.status}`)
  return res.json()
}

export interface RequestFilters {
  status: string
  client: string
  hours: string
}

// Paged, filtered audit listing (D-057). Rejects with the gateway's message.
export async function fetchRequests(
  params: Partial<RequestFilters> & { limit?: number; offset?: number } = {},
): Promise<{ requests: Request[]; has_more: boolean }> {
  const q = new URLSearchParams({
    limit: String(params.limit ?? 50),
    offset: String(params.offset ?? 0),
  })
  if (params.status) q.set('filter_status', params.status)
  if (params.client) q.set('filter_client', params.client)
  if (params.hours) q.set('hours', params.hours)
  const res = await apiFetch(`${API_BASE}/api/requests?${q}`)
  const body = await res.json().catch(() => null)
  if (!res.ok) throw new Error(gatewayErrorMessage(res.status, body))
  return { requests: body.requests ?? [], has_more: Boolean(body.has_more) }
}

export async function fetchRequestDetail(requestId: string): Promise<RequestDetail> {
  const res = await apiFetch(`${API_BASE}/api/requests/${requestId}`)
  if (!res.ok) throw new Error(`request detail: HTTP ${res.status}`)
  return res.json()
}

export async function fetchCatalog(): Promise<Catalog> {
  const res = await apiFetch(`${API_BASE}/v1/devmesh/catalog`)
  return res.json()
}

export async function fetchHealth(): Promise<HealthResponse> {
  const res = await apiFetch(`${API_BASE}/health`)
  return res.json()
}

export async function fetchSecurityAlerts(limit = 50): Promise<{ alerts: SecurityAlert[]; total: number }> {
  const res = await apiFetch(`${API_BASE}/api/security/alerts?limit=${limit}`)
  return res.json()
}

// null when unavailable (e.g. key rejected): the error envelope used to be
// returned as if it were stats, and rendering it crashed the page (D-057)
export async function fetchSecurityStats(): Promise<SecurityStats | null> {
  const res = await apiFetch(`${API_BASE}/api/security/stats`)
  if (!res.ok) return null
  return res.json()
}

export async function fetchSecurityResults(limit = 50, disagreementsOnly = false): Promise<{ results: SecurityResult[]; total: number; filter: string }> {
  const params = new URLSearchParams({ limit: String(limit) })
  if (disagreementsOnly) params.set('disagreements_only', 'true')
  else params.set('guard_only', 'true')
  const res = await apiFetch(`${API_BASE}/api/security/results?${params}`)
  if (!res.ok) return { results: [], total: 0, filter: 'all' }
  return res.json()
}

export async function fetchApiKeys(): Promise<{ keys: ApiKeyInfo[]; total: number }> {
  const res = await apiFetch(`${API_BASE}/api/keys`)
  if (!res.ok) return { keys: [], total: 0 }
  return res.json()
}

// Mutations reject with the gateway's own message (validation detail,
// policy reason, {status:"error"} bodies) so panels can show it inline.
// Before D-057 several returned res.json() unchecked and failed silently.
async function checked<T>(res: Response): Promise<T> {
  const body = await res.json().catch(() => null)
  const softError =
    body && typeof body === 'object' && (body as { status?: string }).status === 'error'
  if (!res.ok || softError) throw new Error(gatewayErrorMessage(res.status, body))
  return body as T
}

export async function createApiKey(body: {
  name: string
  client_id: string
  description?: string
  rate_limit_rpm?: number
  max_concurrent?: number
  priority?: 'interactive' | 'batch'
  allowed_models?: string[]
  allowed_endpoints?: string[]
}): Promise<{ key: string; key_id: number; prefix: string; client_id: string }> {
  const res = await apiFetch(`${API_BASE}/api/keys`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return checked(res)
}

export async function revokeApiKey(keyId: number): Promise<void> {
  const res = await apiFetch(`${API_BASE}/api/keys/${keyId}`, { method: 'DELETE' })
  await checked(res)
}

export async function fetchBudgetConfig(): Promise<BudgetConfig> {
  const res = await apiFetch(`${API_BASE}/api/budget/config`)
  if (!res.ok) return { enabled: false, default_daily_limit: 0, default_cost_multiplier: 1, enforce_pre_request: false, tiers: [], model_assignments: {}, model_classifications: [] }
  return res.json()
}

export async function fetchBudgetUsage(): Promise<BudgetUsage> {
  const res = await apiFetch(`${API_BASE}/api/budget/usage`)
  if (!res.ok) return { enabled: false, keys: [] }
  return res.json()
}

export async function createTier(name: string, costMultiplier: number, dailyLimit?: number): Promise<{ status: string }> {
  const res = await apiFetch(`${API_BASE}/api/budget/tiers`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, cost_multiplier: costMultiplier, daily_limit: dailyLimit }),
  })
  return checked(res)
}

export async function deleteTier(name: string): Promise<{ status: string; message?: string }> {
  const res = await apiFetch(`${API_BASE}/api/budget/tiers/${encodeURIComponent(name)}`, { method: 'DELETE' })
  return checked(res)
}

export async function assignModelTier(model: string, tier: string): Promise<{ status: string; message?: string }> {
  const res = await apiFetch(`${API_BASE}/api/budget/assignments`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model, tier }),
  })
  return checked(res)
}

export async function unassignModelTier(model: string): Promise<{ status: string }> {
  const res = await apiFetch(`${API_BASE}/api/budget/assignments/${encodeURIComponent(model)}`, { method: 'DELETE' })
  return checked(res)
}

export async function fetchSecurityScans(params: { limit?: number; offset?: number; unlabeled_only?: boolean; disagreements_only?: boolean; min_threat_level?: string } = {}): Promise<{ scans: SecurityScan[]; total: number }> {
  const searchParams = new URLSearchParams()
  if (params.limit) searchParams.set('limit', String(params.limit))
  if (params.offset) searchParams.set('offset', String(params.offset))
  if (params.unlabeled_only) searchParams.set('unlabeled_only', 'true')
  if (params.disagreements_only) searchParams.set('disagreements_only', 'true')
  if (params.min_threat_level) searchParams.set('min_threat_level', params.min_threat_level)
  const res = await apiFetch(`${API_BASE}/api/security/scans?${searchParams}`)
  if (!res.ok) return { scans: [], total: 0 }
  return res.json()
}

export async function labelScan(requestId: string, label: string, labelCategory?: string, notes?: string): Promise<{ status: string }> {
  const res = await apiFetch(`${API_BASE}/api/security/scans/${encodeURIComponent(requestId)}/label`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label, label_category: labelCategory, notes }),
  })
  return res.json()
}

export async function bulkLabelScans(requestIds: string[], label: string, labelCategory?: string): Promise<{ status: string; labeled: number }> {
  const res = await apiFetch(`${API_BASE}/api/security/scans/bulk-label`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ request_ids: requestIds, label, label_category: labelCategory }),
  })
  return res.json()
}

// The server names the count `total_scans`; the dashboard always read
// `total`, so the Total Scans tile was blank and `.toLocaleString()` on it
// crashed the page (D-057). Map the wire shape here, once.
export function toLabelStats(body: Record<string, unknown> | null): LabelStats {
  const n = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : 0)
  const b = body ?? {}
  return {
    total: n(b.total_scans ?? b.total),
    labeled: n(b.labeled),
    unlabeled: n(b.unlabeled),
    safe: n(b.safe),
    unsafe: n(b.unsafe),
    disagreements: n(b.disagreements),
  }
}

export async function fetchLabelStats(): Promise<LabelStats> {
  const res = await apiFetch(`${API_BASE}/api/security/scans/stats`)
  if (!res.ok) return toLabelStats(null)
  return toLabelStats(await res.json().catch(() => null))
}

export async function exportTrainingData(format: string = 'llama_guard'): Promise<{ count: number; examples: unknown[] }> {
  const res = await apiFetch(`${API_BASE}/api/security/training-data?format=${format}`)
  if (!res.ok) return { count: 0, examples: [] }
  return res.json()
}

export async function fetchPIIStats(hours = 24): Promise<PIIStats> {
  const res = await apiFetch(`${API_BASE}/api/pii/stats?hours=${hours}`)
  if (!res.ok) return { enabled: false, total_detections: 0, by_type: {}, scrubbed_count: 0, flagged_only_count: 0, unique_requests: 0, unique_values: 0 }
  return res.json()
}

export async function fetchPIIEvents(limit = 50, piiType?: string): Promise<{ events: PIIEvent[]; total: number }> {
  let url = `${API_BASE}/api/pii/events?limit=${limit}`
  if (piiType) url += `&pii_type=${piiType}`
  const res = await apiFetch(url)
  if (!res.ok) return { events: [], total: 0 }
  return res.json()
}

export async function fetchPIIConfig(): Promise<PIIConfig | null> {
  const res = await apiFetch(`${API_BASE}/api/pii/config`)
  if (!res.ok) return null
  return res.json()
}

// Resolves with the new config, or rejects with the gateway's error message
export async function updatePIIConfig(body: { scrub_enabled: boolean; scrub_routes: string[] }): Promise<PIIConfig> {
  const res = await apiFetch(`${API_BASE}/api/pii/config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(data?.error?.message || data?.detail?.[0]?.msg || `Request failed (${res.status})`)
  }
  return data
}

export async function fetchPIIML(hours = 24): Promise<PIIMLView | null> {
  const res = await apiFetch(`${API_BASE}/api/pii/ml?hours=${hours}`)
  if (!res.ok) return null
  return res.json()
}

// Only the categories sent change. Rejects with the gateway's error message.
export async function updatePIIML(categories: Record<string, PIIMLAction>): Promise<PIIMLView> {
  const res = await apiFetch(`${API_BASE}/api/pii/ml`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ categories }),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(data?.error?.message || data?.detail?.[0]?.msg || `Request failed (${res.status})`)
  }
  return data
}

// ---- Media (voice) ----

export async function fetchMediaCatalog(refresh = false): Promise<MediaEndpoint[]> {
  const res = refresh
    ? await apiFetch(`${API_BASE}/api/media/catalog/refresh`, { method: 'POST' })
    : await apiFetch(`${API_BASE}/api/media/catalog`)
  if (!res.ok) return []
  return (await res.json()).endpoints
}

async function errorMessage(res: Response): Promise<string> {
  const data = await res.json().catch(() => null)
  return data?.error?.message || data?.detail?.[0]?.msg || `Request failed (${res.status})`
}

/** Text-to-speech through the gateway's real route (audited and metered). */
export async function synthesizeSpeech(body: Record<string, unknown>): Promise<{ audio: Blob; ms: number }> {
  const started = performance.now()
  const res = await apiFetch(`${API_BASE}/v1/audio/speech`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(await errorMessage(res))
  const audio = await res.blob()
  return { audio, ms: performance.now() - started }
}

/** Speech-to-text (task 'transcriptions' or 'translations') through the gateway. */
export async function transcribeAudio(
  task: 'transcriptions' | 'translations',
  file: Blob,
  filename: string,
  fields: Record<string, string>,
): Promise<{ text: string; contentType: string; ms: number }> {
  const form = new FormData()
  form.append('file', file, filename)
  for (const [key, value] of Object.entries(fields)) {
    if (value) form.append(key, value)
  }
  const started = performance.now()
  const res = await apiFetch(`${API_BASE}/v1/audio/${task}`, { method: 'POST', body: form })
  if (!res.ok) throw new Error(await errorMessage(res))
  return { text: await res.text(), contentType: res.headers.get('content-type') || '', ms: performance.now() - started }
}

export interface RoutingTaskPin {
  task: string
  allowed_endpoints: string[]
  denied_endpoints: string[]
}

export interface RoutingModelHome {
  model: string
  endpoint: string
}

export interface RoutingConfig {
  strategy: 'priority' | 'least_loaded'
  task_endpoints: RoutingTaskPin[]
  model_defaults: RoutingModelHome[]
  available_endpoints: string[]
  available_tasks: string[]
  endpoint_priority: string[]
  source: string
  updated_at: string | null
  updated_by: string | null
  persisted: boolean
}

export async function fetchRoutingConfig(): Promise<RoutingConfig | null> {
  const res = await apiFetch(`${API_BASE}/api/routing/config`)
  if (!res.ok) return null
  return res.json()
}

// Resolves with the new config, or rejects with the gateway's error message
export async function updateRoutingConfig(body: {
  strategy: string
  task_endpoints: { task: string; allowed_endpoints: string[]; denied_endpoints: string[] }[]
  model_defaults: { model: string; endpoint: string }[]
}): Promise<RoutingConfig> {
  const res = await apiFetch(`${API_BASE}/api/routing/config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data?.error?.message || `HTTP ${res.status}`)
  return data
}
