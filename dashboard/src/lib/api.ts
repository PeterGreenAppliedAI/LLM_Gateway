import type { Stats, Request, RequestDetail, Catalog, HealthResponse, SecurityAlert, SecurityStats, SecurityResult, ApiKeyInfo, BudgetConfig, BudgetUsage, SecurityScan, LabelStats, PIIStats, PIIEvent } from '../types'

// API base URL - gateway server
export const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8001'

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

export async function fetchRequests(limit = 50): Promise<{ requests: Request[] }> {
  const res = await apiFetch(`${API_BASE}/api/requests?limit=${limit}`)
  if (!res.ok) return { requests: [] }
  return res.json()
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

export async function fetchSecurityStats(): Promise<SecurityStats> {
  const res = await apiFetch(`${API_BASE}/api/security/stats`)
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

export async function createApiKey(body: { name: string; client_id: string; description?: string }): Promise<{ key: string; key_id: number; prefix: string }> {
  const res = await apiFetch(`${API_BASE}/api/keys`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`Failed to create key: ${res.statusText}`)
  return res.json()
}

export async function revokeApiKey(keyId: number): Promise<void> {
  const res = await apiFetch(`${API_BASE}/api/keys/${keyId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`Failed to revoke key: ${res.statusText}`)
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
  return res.json()
}

export async function deleteTier(name: string): Promise<{ status: string; message?: string }> {
  const res = await apiFetch(`${API_BASE}/api/budget/tiers/${encodeURIComponent(name)}`, { method: 'DELETE' })
  return res.json()
}

export async function assignModelTier(model: string, tier: string): Promise<{ status: string; message?: string }> {
  const res = await apiFetch(`${API_BASE}/api/budget/assignments`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model, tier }),
  })
  return res.json()
}

export async function unassignModelTier(model: string): Promise<{ status: string }> {
  const res = await apiFetch(`${API_BASE}/api/budget/assignments/${encodeURIComponent(model)}`, { method: 'DELETE' })
  return res.json()
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

export async function fetchLabelStats(): Promise<LabelStats> {
  const res = await apiFetch(`${API_BASE}/api/security/scans/stats`)
  if (!res.ok) return { total: 0, labeled: 0, unlabeled: 0, safe: 0, unsafe: 0, disagreements: 0 }
  return res.json()
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
