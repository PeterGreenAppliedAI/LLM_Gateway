import { useCallback, useEffect, useRef, useState } from 'react'

// Helper to format UTC timestamp to local time
function formatTimestamp(utcTimestamp: string): string {
  // Handle various timestamp formats
  // - Already has Z suffix: use as-is
  // - Has timezone offset (+00:00): use as-is
  // - No timezone info: append Z to treat as UTC
  let timestamp = utcTimestamp
  if (!utcTimestamp.endsWith('Z') && !utcTimestamp.match(/[+-]\d{2}:\d{2}$/)) {
    timestamp = utcTimestamp + 'Z'
  }
  return new Date(timestamp).toLocaleString()
}

function formatTime(utcTimestamp: string): string {
  let timestamp = utcTimestamp
  if (!utcTimestamp.endsWith('Z') && !utcTimestamp.match(/[+-]\d{2}:\d{2}$/)) {
    timestamp = utcTimestamp + 'Z'
  }
  return new Date(timestamp).toLocaleTimeString()
}

// Types
interface Stats {
  period_hours: number
  total_requests: number
  success_count: number
  error_count: number
  success_rate: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  avg_latency_ms: number
  min_latency_ms: number
  max_latency_ms: number
  requests_by_endpoint: Record<string, number>
  top_models: Record<string, number>
}

interface Request {
  id: number
  request_id: string
  timestamp: string
  client_id: string
  task: string
  model: string
  endpoint: string
  status: string
  latency_ms: number | null
  prompt_tokens: number
  completion_tokens: number
  error_code: string | null
}

interface RequestDetail extends Request {
  user_id: string | null
  environment: string | null
  provider_type: string | null
  stream: boolean
  max_tokens: number | null
  temperature: number | null
  error_message: string | null
  time_to_first_token_ms: number | null
  tokens_per_second: number | null
  total_tokens: number
  estimated_cost_usd: number | null
  request_body: string | null
  response_body: string | null
}

interface Endpoint {
  name: string
  type: string
  url: string
  enabled: boolean
  healthy: boolean
  labels: Record<string, string>
  models: string[]
}

interface Catalog {
  last_discovery: string
  endpoints: Endpoint[]
  total_models: number
  total_endpoints: number
}

interface HealthResponse {
  status: string
  providers_configured: number
  providers_healthy: number
  providers: { name: string; status: string; healthy: boolean }[]
}

interface SecurityAlert {
  timestamp: string
  request_id: string
  client_id: string
  severity: string
  alert_type: string
  description: string
  details: Record<string, unknown>
}

interface SecurityStats {
  requests_analyzed: number
  alerts_generated: number
  requests_dropped: number
  queue_size: number
  alerts_in_memory: number
  guard_scans: number
  guard_skipped: number
  guard_unsafe: number
}

interface SecurityResult {
  request_id: string
  analyzed_at: string
  regex_threat_level: string
  regex_match_count: number
  guard_safe: boolean | null
  guard_skipped: boolean | null
  guard_category_code: string | null
  guard_category_name: string | null
  guard_confidence: string | null
  guard_inference_ms: number | null
  guard_error: string | null
  alert_count: number
}

interface ApiKeyInfo {
  id: number
  prefix: string
  name: string
  client_id: string
  environment: string | null
  created_at: string | null
  last_used_at: string | null
  is_active: boolean
  allowed_endpoints: string[] | null
  allowed_models: string[] | null
  rate_limit_rpm: number | null
  description: string | null
}

interface BudgetTier {
  name: string
  cost_multiplier: number
  daily_limit: number | null
}

interface ModelClassification {
  model: string
  tier: string | null
  cost_multiplier: number
  classified: boolean
}

interface BudgetConfig {
  enabled: boolean
  default_daily_limit: number
  default_cost_multiplier: number
  enforce_pre_request: boolean
  tiers: BudgetTier[]
  model_assignments: Record<string, string>
  model_classifications: ModelClassification[]
}

interface BudgetKeyUsage {
  key: string
  daily_limit: number
  tokens_used: number
  tokens_remaining: number
  tier_usage: Record<string, number>
  request_count?: number
  resets_at: string
}

interface BudgetUsage {
  enabled: boolean
  keys: BudgetKeyUsage[]
}

interface SecurityScan {
  request_id: string
  timestamp: string
  client_id: string
  model: string | null
  task: string | null
  messages: { role: string; content: string }[]
  regex_threat_level: string
  regex_match_count: number
  guard_safe: boolean | null
  guard_skipped: boolean | null
  guard_category_code: string | null
  is_disagreement: boolean
  label: string | null
  label_category: string | null
  labeled_by: string | null
  label_notes: string | null
}

interface LabelStats {
  total: number
  labeled: number
  unlabeled: number
  safe: number
  unsafe: number
  disagreements: number
}

interface PIIStats {
  enabled: boolean
  total_detections: number
  by_type: Record<string, number>
  scrubbed_count: number
  flagged_only_count: number
  unique_requests: number
  unique_values: number
}

interface PIIEvent {
  id: number
  request_id: string
  timestamp: string
  client_id: string
  model: string | null
  task: string | null
  pii_type: string
  message_index: number | null
  message_role: string | null
  position_start: number | null
  position_end: number | null
  value_hash: string
  was_scrubbed: boolean
  scan_time_ms: number | null
}

// API base URL - gateway server
const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8001'

// Fetch helpers
async function fetchStats(hours = 24): Promise<Stats> {
  const res = await fetch(`${API_BASE}/api/stats?hours=${hours}`)
  return res.json()
}

async function fetchRequests(limit = 50): Promise<{ requests: Request[] }> {
  const res = await fetch(`${API_BASE}/api/requests?limit=${limit}`)
  return res.json()
}

async function fetchRequestDetail(requestId: string): Promise<RequestDetail> {
  const res = await fetch(`${API_BASE}/api/requests/${requestId}`)
  return res.json()
}

async function fetchCatalog(): Promise<Catalog> {
  const res = await fetch(`${API_BASE}/v1/devmesh/catalog`)
  return res.json()
}

async function fetchHealth(): Promise<HealthResponse> {
  const res = await fetch(`${API_BASE}/health`)
  return res.json()
}

async function fetchSecurityAlerts(limit = 50): Promise<{ alerts: SecurityAlert[]; total: number }> {
  const res = await fetch(`${API_BASE}/api/security/alerts?limit=${limit}`)
  return res.json()
}

async function fetchSecurityStats(): Promise<SecurityStats> {
  const res = await fetch(`${API_BASE}/api/security/stats`)
  return res.json()
}

async function fetchSecurityResults(limit = 50, disagreementsOnly = false): Promise<{ results: SecurityResult[]; total: number; filter: string }> {
  const params = new URLSearchParams({ limit: String(limit) })
  if (disagreementsOnly) params.set('disagreements_only', 'true')
  else params.set('guard_only', 'true')
  const res = await fetch(`${API_BASE}/api/security/results?${params}`)
  if (!res.ok) return { results: [], total: 0, filter: 'all' }
  return res.json()
}

async function fetchApiKeys(): Promise<{ keys: ApiKeyInfo[]; total: number }> {
  const res = await fetch(`${API_BASE}/api/keys`)
  if (!res.ok) return { keys: [], total: 0 }
  return res.json()
}

async function createApiKey(body: { name: string; client_id: string; description?: string }): Promise<{ key: string; key_id: number; prefix: string }> {
  const res = await fetch(`${API_BASE}/api/keys`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`Failed to create key: ${res.statusText}`)
  return res.json()
}

async function revokeApiKey(keyId: number): Promise<void> {
  const res = await fetch(`${API_BASE}/api/keys/${keyId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`Failed to revoke key: ${res.statusText}`)
}

async function fetchBudgetConfig(): Promise<BudgetConfig> {
  const res = await fetch(`${API_BASE}/api/budget/config`)
  if (!res.ok) return { enabled: false, default_daily_limit: 0, default_cost_multiplier: 1, enforce_pre_request: false, tiers: [], model_assignments: {}, model_classifications: [] }
  return res.json()
}

async function fetchBudgetUsage(): Promise<BudgetUsage> {
  const res = await fetch(`${API_BASE}/api/budget/usage`)
  if (!res.ok) return { enabled: false, keys: [] }
  return res.json()
}

async function createTier(name: string, costMultiplier: number, dailyLimit?: number): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE}/api/budget/tiers`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, cost_multiplier: costMultiplier, daily_limit: dailyLimit }),
  })
  return res.json()
}

async function deleteTier(name: string): Promise<{ status: string; message?: string }> {
  const res = await fetch(`${API_BASE}/api/budget/tiers/${encodeURIComponent(name)}`, { method: 'DELETE' })
  return res.json()
}

async function assignModelTier(model: string, tier: string): Promise<{ status: string; message?: string }> {
  const res = await fetch(`${API_BASE}/api/budget/assignments`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model, tier }),
  })
  return res.json()
}

async function unassignModelTier(model: string): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE}/api/budget/assignments/${encodeURIComponent(model)}`, { method: 'DELETE' })
  return res.json()
}

async function fetchSecurityScans(params: { limit?: number; offset?: number; unlabeled_only?: boolean; disagreements_only?: boolean; min_threat_level?: string } = {}): Promise<{ scans: SecurityScan[]; total: number }> {
  const searchParams = new URLSearchParams()
  if (params.limit) searchParams.set('limit', String(params.limit))
  if (params.offset) searchParams.set('offset', String(params.offset))
  if (params.unlabeled_only) searchParams.set('unlabeled_only', 'true')
  if (params.disagreements_only) searchParams.set('disagreements_only', 'true')
  if (params.min_threat_level) searchParams.set('min_threat_level', params.min_threat_level)
  const res = await fetch(`${API_BASE}/api/security/scans?${searchParams}`)
  if (!res.ok) return { scans: [], total: 0 }
  return res.json()
}

async function labelScan(requestId: string, label: string, labelCategory?: string, notes?: string): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE}/api/security/scans/${encodeURIComponent(requestId)}/label`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label, label_category: labelCategory, notes }),
  })
  return res.json()
}

async function bulkLabelScans(requestIds: string[], label: string, labelCategory?: string): Promise<{ status: string; labeled: number }> {
  const res = await fetch(`${API_BASE}/api/security/scans/bulk-label`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ request_ids: requestIds, label, label_category: labelCategory }),
  })
  return res.json()
}

async function fetchLabelStats(): Promise<LabelStats> {
  const res = await fetch(`${API_BASE}/api/security/scans/stats`)
  if (!res.ok) return { total: 0, labeled: 0, unlabeled: 0, safe: 0, unsafe: 0, disagreements: 0 }
  return res.json()
}

async function exportTrainingData(format: string = 'llama_guard'): Promise<{ count: number; examples: unknown[] }> {
  const res = await fetch(`${API_BASE}/api/security/training-data?format=${format}`)
  if (!res.ok) return { count: 0, examples: [] }
  return res.json()
}

async function fetchPIIStats(hours = 24): Promise<PIIStats> {
  const res = await fetch(`${API_BASE}/api/pii/stats?hours=${hours}`)
  if (!res.ok) return { enabled: false, total_detections: 0, by_type: {}, scrubbed_count: 0, flagged_only_count: 0, unique_requests: 0, unique_values: 0 }
  return res.json()
}

async function fetchPIIEvents(limit = 50, piiType?: string): Promise<{ events: PIIEvent[]; total: number }> {
  let url = `${API_BASE}/api/pii/events?limit=${limit}`
  if (piiType) url += `&pii_type=${piiType}`
  const res = await fetch(url)
  if (!res.ok) return { events: [], total: 0 }
  return res.json()
}

// Components
function StatCard({ label, value, subtext }: { label: string; value: string | number; subtext?: string }) {
  return (
    <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
      <div className="text-gray-400 text-sm">{label}</div>
      <div className="text-2xl font-bold mt-1">{value}</div>
      {subtext && <div className="text-gray-500 text-xs mt-1">{subtext}</div>}
    </div>
  )
}

function EndpointCard({ endpoint }: { endpoint: Endpoint }) {
  return (
    <div className={`bg-gray-800 rounded-lg p-4 border ${endpoint.healthy ? 'border-green-600' : 'border-red-600'}`}>
      <div className="flex items-center justify-between">
        <div className="font-semibold">{endpoint.name}</div>
        <div className={`px-2 py-1 rounded text-xs ${endpoint.healthy ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
          {endpoint.healthy ? 'Healthy' : 'Unhealthy'}
        </div>
      </div>
      <div className="text-gray-400 text-sm mt-1">{endpoint.type} - {endpoint.url}</div>
      <div className="text-gray-500 text-xs mt-2">{endpoint.models.length} models</div>
      <div className="flex flex-wrap gap-1 mt-2">
        {endpoint.models.slice(0, 5).map(model => (
          <span key={model} className="bg-gray-700 px-2 py-0.5 rounded text-xs">{model}</span>
        ))}
        {endpoint.models.length > 5 && (
          <span className="text-gray-500 text-xs">+{endpoint.models.length - 5} more</span>
        )}
      </div>
    </div>
  )
}

function MetricRow({ label, value, unit = '' }: { label: string; value: string | number | null | undefined; unit?: string }) {
  if (value === null || value === undefined) return null
  return (
    <div className="flex justify-between py-1 border-b border-gray-700">
      <span className="text-gray-400">{label}</span>
      <span className="font-mono">{typeof value === 'number' ? value.toFixed(2) : value}{unit}</span>
    </div>
  )
}

function RequestDetailPanel({ detail, onClose }: { detail: RequestDetail; onClose: () => void }) {
  const timestamp = formatTimestamp(detail.timestamp)

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-gray-800 rounded-lg border border-gray-600 w-full max-w-2xl max-h-[90vh] overflow-auto m-4" onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-gray-700">
          <div>
            <h3 className="text-lg font-semibold">Request Details</h3>
            <p className="text-gray-400 text-sm font-mono">{detail.request_id}</p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-white text-2xl">&times;</button>
        </div>

        {/* Content */}
        <div className="p-4 space-y-4">
          {/* Status Banner */}
          <div className={`p-3 rounded ${detail.status === 'success' ? 'bg-green-900/50 border border-green-700' : 'bg-red-900/50 border border-red-700'}`}>
            <div className="flex items-center gap-2">
              <span className={`w-2 h-2 rounded-full ${detail.status === 'success' ? 'bg-green-400' : 'bg-red-400'}`}></span>
              <span className="font-semibold capitalize">{detail.status}</span>
              {detail.error_code && <span className="text-red-400">({detail.error_code})</span>}
            </div>
            {detail.error_message && <p className="text-red-300 mt-1 text-sm">{detail.error_message}</p>}
          </div>

          {/* Basic Info */}
          <div className="grid grid-cols-2 gap-4">
            <div className="bg-gray-900 p-3 rounded">
              <div className="text-gray-400 text-xs uppercase">Model</div>
              <div className="font-mono mt-1">{detail.model}</div>
            </div>
            <div className="bg-gray-900 p-3 rounded">
              <div className="text-gray-400 text-xs uppercase">Endpoint</div>
              <div className="font-mono mt-1">{detail.endpoint}</div>
            </div>
            <div className="bg-gray-900 p-3 rounded">
              <div className="text-gray-400 text-xs uppercase">Task</div>
              <div className="font-mono mt-1">{detail.task}</div>
            </div>
            <div className="bg-gray-900 p-3 rounded">
              <div className="text-gray-400 text-xs uppercase">Timestamp</div>
              <div className="font-mono mt-1 text-sm">{timestamp}</div>
            </div>
          </div>

          {/* Performance Metrics */}
          <div>
            <h4 className="text-sm font-semibold text-gray-300 mb-2">Performance</h4>
            <div className="bg-gray-900 p-3 rounded space-y-1">
              <MetricRow label="Total Latency" value={detail.latency_ms} unit=" ms" />
              <MetricRow label="Time to First Token" value={detail.time_to_first_token_ms} unit=" ms" />
              <MetricRow label="Tokens/Second" value={detail.tokens_per_second} unit=" tok/s" />
            </div>
          </div>

          {/* Token Usage */}
          <div>
            <h4 className="text-sm font-semibold text-gray-300 mb-2">Token Usage</h4>
            <div className="bg-gray-900 p-3 rounded">
              <div className="grid grid-cols-3 gap-4 text-center">
                <div>
                  <div className="text-2xl font-bold text-blue-400">{detail.prompt_tokens}</div>
                  <div className="text-gray-400 text-xs">Prompt</div>
                </div>
                <div>
                  <div className="text-2xl font-bold text-green-400">{detail.completion_tokens}</div>
                  <div className="text-gray-400 text-xs">Completion</div>
                </div>
                <div>
                  <div className="text-2xl font-bold text-purple-400">{detail.total_tokens}</div>
                  <div className="text-gray-400 text-xs">Total</div>
                </div>
              </div>
              {detail.estimated_cost_usd !== null && detail.estimated_cost_usd > 0 && (
                <div className="mt-3 pt-3 border-t border-gray-700 text-center">
                  <span className="text-gray-400">Estimated Cost: </span>
                  <span className="text-yellow-400 font-mono">${detail.estimated_cost_usd.toFixed(4)}</span>
                </div>
              )}
            </div>
          </div>

          {/* Request Parameters */}
          <div>
            <h4 className="text-sm font-semibold text-gray-300 mb-2">Parameters</h4>
            <div className="bg-gray-900 p-3 rounded space-y-1">
              <MetricRow label="Stream" value={detail.stream ? 'Yes' : 'No'} />
              <MetricRow label="Max Tokens" value={detail.max_tokens} />
              <MetricRow label="Temperature" value={detail.temperature} />
              <MetricRow label="Client ID" value={detail.client_id} />
              {detail.user_id && <MetricRow label="User ID" value={detail.user_id} />}
              {detail.environment && <MetricRow label="Environment" value={detail.environment} />}
            </div>
          </div>

          {/* Request/Response Bodies (if available) */}
          {detail.request_body && (
            <div>
              <h4 className="text-sm font-semibold text-gray-300 mb-2">Request Body</h4>
              <pre className="bg-gray-900 p-3 rounded text-xs overflow-auto max-h-40">
                {typeof detail.request_body === 'string' ? detail.request_body : JSON.stringify(detail.request_body, null, 2)}
              </pre>
            </div>
          )}
          {detail.response_body && (
            <div>
              <h4 className="text-sm font-semibold text-gray-300 mb-2">Response Body</h4>
              <pre className="bg-gray-900 p-3 rounded text-xs overflow-auto max-h-40">
                {typeof detail.response_body === 'string' ? detail.response_body : JSON.stringify(detail.response_body, null, 2)}
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function RequestRow({ request, onClick }: { request: Request; onClick: () => void }) {
  const time = formatTime(request.timestamp)
  return (
    <tr className="border-b border-gray-700 hover:bg-gray-750 cursor-pointer" onClick={onClick}>
      <td className="py-2 px-3 text-gray-400 text-sm">{time}</td>
      <td className="py-2 px-3">
        <span className={`px-2 py-0.5 rounded text-xs ${request.status === 'success' ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
          {request.status}
        </span>
      </td>
      <td className="py-2 px-3 font-mono text-sm">{request.model}</td>
      <td className="py-2 px-3 text-gray-400 text-sm">{request.endpoint}</td>
      <td className="py-2 px-3 text-right text-sm">
        {request.latency_ms ? `${request.latency_ms.toFixed(0)}ms` : '-'}
      </td>
      <td className="py-2 px-3 text-right text-gray-400 text-sm">
        {request.prompt_tokens + request.completion_tokens}
      </td>
    </tr>
  )
}

function getSeverityColor(severity: string): string {
  switch (severity) {
    case 'critical': return 'bg-red-900 text-red-300 border-red-700'
    case 'warning': return 'bg-yellow-900 text-yellow-300 border-yellow-700'
    case 'info': return 'bg-blue-900 text-blue-300 border-blue-700'
    default: return 'bg-gray-900 text-gray-300 border-gray-700'
  }
}

function SecurityAlertRow({ alert }: { alert: SecurityAlert }) {
  const datetime = formatTimestamp(alert.timestamp)
  const [expanded, setExpanded] = useState(false)

  return (
    <div className={`border rounded mb-2 ${getSeverityColor(alert.severity)}`}>
      <div
        className="p-3 cursor-pointer flex items-center justify-between"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-3">
          <span className={`px-2 py-0.5 rounded text-xs uppercase font-bold ${getSeverityColor(alert.severity)}`}>
            {alert.severity}
          </span>
          <span className="font-medium">{alert.alert_type}</span>
          <span className="text-sm opacity-75">{alert.description}</span>
        </div>
        <div className="flex items-center gap-3 text-sm opacity-75">
          <span>{alert.client_id}</span>
          <span>{datetime}</span>
          <span>{expanded ? '▼' : '▶'}</span>
        </div>
      </div>
      {expanded && (
        <div className="p-3 border-t border-current opacity-50">
          <div className="text-xs font-mono">
            <div><strong>Request ID:</strong> {alert.request_id}</div>
            {alert.details && Object.keys(alert.details).length > 0 && (
              <div className="mt-2">
                <strong>Details:</strong>
                <pre className="mt-1 overflow-auto max-h-40 bg-black/20 p-2 rounded">
                  {JSON.stringify(alert.details, null, 2)}
                </pre>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function GuardResultRow({ result }: { result: SecurityResult }) {
  const time = formatTime(result.analyzed_at)
  const regexFlagged = result.regex_threat_level !== 'none'
  const guardSafe = result.guard_safe
  const isDisagreement = regexFlagged !== (guardSafe === false)

  return (
    <div className={`flex items-center border-b border-gray-700 text-sm ${isDisagreement ? 'bg-yellow-900/20' : ''}`}>
      <div className="w-[15%] py-2 px-3 text-gray-400">{time}</div>
      <div className="w-[22%] py-2 px-3 font-mono text-xs truncate">{result.request_id.slice(0, 16)}...</div>
      <div className="w-[15%] py-2 px-3">
        <span className={`px-2 py-0.5 rounded text-xs ${regexFlagged ? 'bg-red-900 text-red-300' : 'bg-green-900 text-green-300'}`}>
          {result.regex_threat_level}
        </span>
        {result.regex_match_count > 0 && (
          <span className="text-gray-500 text-xs ml-1">({result.regex_match_count})</span>
        )}
      </div>
      <div className="w-[18%] py-2 px-3">
        {result.guard_skipped ? (
          <span className="px-2 py-0.5 rounded text-xs bg-gray-700 text-gray-400">skipped</span>
        ) : (
          <span className={`px-2 py-0.5 rounded text-xs ${guardSafe ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
            {guardSafe ? 'safe' : 'unsafe'}
          </span>
        )}
        {result.guard_category_code && (
          <span className="text-gray-400 text-xs ml-1" title={result.guard_category_name || ''}>
            {result.guard_category_code}
          </span>
        )}
        {result.guard_confidence && (
          <span className={`text-xs ml-1 ${result.guard_confidence === 'High' ? 'text-red-400' : 'text-gray-500'}`}>
            {result.guard_confidence}
          </span>
        )}
      </div>
      <div className="w-[15%] py-2 px-3 text-right text-gray-400">
        {result.guard_inference_ms ? `${result.guard_inference_ms.toFixed(0)}ms` : '-'}
      </div>
      <div className="w-[15%] py-2 px-3 text-center">
        {isDisagreement && (
          <span className="px-2 py-0.5 rounded text-xs bg-yellow-900 text-yellow-300">
            {regexFlagged && guardSafe ? 'FP' : 'missed'}
          </span>
        )}
      </div>
    </div>
  )
}

function SecuritySection({ alerts, stats, guardResults, onFilterChange }: {
  alerts: SecurityAlert[]
  stats: SecurityStats | null
  guardResults: SecurityResult[]
  onFilterChange: (disagreementsOnly: boolean) => void
}) {
  const criticalCount = alerts.filter(a => a.severity === 'critical').length
  const warningCount = alerts.filter(a => a.severity === 'warning').length
  const guardEnabled = (stats?.guard_scans || 0) > 0
  const [showDisagreements, setShowDisagreements] = useState(false)
  const [activeTab, setActiveTab] = useState<'alerts' | 'guard'>('alerts')

  const handleFilterToggle = () => {
    const next = !showDisagreements
    setShowDisagreements(next)
    onFilterChange(next)
  }

  return (
    <div className="mb-6">
      <h2 className="text-lg font-semibold mb-3 flex items-center gap-2">
        Security Monitor
        {criticalCount > 0 && (
          <span className="bg-red-600 text-white px-2 py-0.5 rounded-full text-xs animate-pulse">
            {criticalCount} Critical
          </span>
        )}
        {guardEnabled && (
          <span className="bg-purple-900 text-purple-300 px-2 py-0.5 rounded-full text-xs">
            Guard Active
          </span>
        )}
      </h2>

      {/* Security Stats */}
      <div className={`grid grid-cols-2 ${guardEnabled ? 'md:grid-cols-4 lg:grid-cols-8' : 'md:grid-cols-5'} gap-3 mb-4`}>
        <div className="bg-gray-800 rounded-lg p-3 border border-gray-700">
          <div className="text-gray-400 text-xs">Analyzed</div>
          <div className="text-xl font-bold">{stats?.requests_analyzed || 0}</div>
        </div>
        <div className="bg-gray-800 rounded-lg p-3 border border-gray-700">
          <div className="text-gray-400 text-xs">Alerts</div>
          <div className="text-xl font-bold text-yellow-400">{stats?.alerts_generated || 0}</div>
        </div>
        <div className="bg-gray-800 rounded-lg p-3 border border-gray-700">
          <div className="text-gray-400 text-xs">Critical</div>
          <div className="text-xl font-bold text-red-400">{criticalCount}</div>
        </div>
        <div className="bg-gray-800 rounded-lg p-3 border border-gray-700">
          <div className="text-gray-400 text-xs">Warnings</div>
          <div className="text-xl font-bold text-yellow-400">{warningCount}</div>
        </div>
        <div className="bg-gray-800 rounded-lg p-3 border border-gray-700">
          <div className="text-gray-400 text-xs">Queue</div>
          <div className="text-xl font-bold">{stats?.queue_size || 0}</div>
        </div>
        {guardEnabled && (
          <>
            <div className="bg-gray-800 rounded-lg p-3 border border-purple-800">
              <div className="text-purple-400 text-xs">Guard Scans</div>
              <div className="text-xl font-bold">{stats?.guard_scans || 0}</div>
            </div>
            <div className="bg-gray-800 rounded-lg p-3 border border-purple-800">
              <div className="text-purple-400 text-xs">Guard Unsafe</div>
              <div className="text-xl font-bold text-red-400">{stats?.guard_unsafe || 0}</div>
            </div>
            <div className="bg-gray-800 rounded-lg p-3 border border-purple-800">
              <div className="text-purple-400 text-xs">Guard Skipped</div>
              <div className="text-xl font-bold text-gray-400">{stats?.guard_skipped || 0}</div>
            </div>
          </>
        )}
      </div>

      {/* Tab Switcher */}
      {guardEnabled && (
        <div className="flex gap-2 mb-3">
          <button
            onClick={() => setActiveTab('alerts')}
            className={`px-3 py-1.5 rounded text-sm ${activeTab === 'alerts' ? 'bg-gray-600 text-white' : 'bg-gray-800 text-gray-400 hover:text-white'}`}
          >
            Alerts ({alerts.length})
          </button>
          <button
            onClick={() => setActiveTab('guard')}
            className={`px-3 py-1.5 rounded text-sm ${activeTab === 'guard' ? 'bg-purple-700 text-white' : 'bg-gray-800 text-purple-400 hover:text-white'}`}
          >
            Guard Results ({guardResults.length})
          </button>
        </div>
      )}

      {/* Alerts Tab */}
      {activeTab === 'alerts' && (
        <>
          {alerts.length > 0 ? (
            <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
              <h3 className="text-sm font-semibold text-gray-300 mb-3">Recent Alerts</h3>
              <div className="max-h-64 overflow-auto">
                {alerts.map((alert, idx) => (
                  <SecurityAlertRow key={`${alert.request_id}-${idx}`} alert={alert} />
                ))}
              </div>
            </div>
          ) : (
            <div className="bg-gray-800 rounded-lg p-8 border border-gray-700 text-center text-gray-500">
              <div className="text-4xl mb-2">&#10003;</div>
              <div>No security alerts</div>
              <div className="text-sm">All requests clean</div>
            </div>
          )}
        </>
      )}

      {/* Guard Results Tab */}
      {activeTab === 'guard' && (
        <div className="bg-gray-800 rounded-lg border border-purple-800 overflow-hidden">
          <div className="flex items-center justify-between p-3 border-b border-gray-700">
            <h3 className="text-sm font-semibold text-gray-300">
              Regex vs Guard — Side-by-Side Verdicts
            </h3>
            <button
              onClick={handleFilterToggle}
              className={`px-3 py-1 rounded text-xs ${showDisagreements ? 'bg-yellow-700 text-yellow-200' : 'bg-gray-700 text-gray-300 hover:text-white'}`}
            >
              {showDisagreements ? 'Showing Disagreements' : 'Show Disagreements Only'}
            </button>
          </div>
          {guardResults.length > 0 ? (
            <div>
              <div className="flex text-left text-gray-400 text-xs font-semibold border-b border-gray-700 bg-gray-750">
                <div className="w-[15%] py-2 px-3">Time</div>
                <div className="w-[22%] py-2 px-3">Request</div>
                <div className="w-[15%] py-2 px-3">Regex</div>
                <div className="w-[18%] py-2 px-3">Guard</div>
                <div className="w-[15%] py-2 px-3 text-right">Inference</div>
                <div className="w-[15%] py-2 px-3 text-center">Verdict</div>
              </div>
              <div className="max-h-72 overflow-auto">
                {guardResults.map((r, idx) => (
                  <GuardResultRow key={`${r.request_id}-${idx}`} result={r} />
                ))}
              </div>
            </div>
          ) : (
            <div className="p-8 text-center text-gray-500">
              <div className="text-sm">
                {showDisagreements ? 'No disagreements found — regex and guard agree' : 'No guard results yet'}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function ApiKeysSection({ keys, onRefresh }: { keys: ApiKeyInfo[]; onRefresh: () => void }) {
  const [showCreate, setShowCreate] = useState(false)
  const [newKeyName, setNewKeyName] = useState('')
  const [newKeyClientId, setNewKeyClientId] = useState('')
  const [newKeyDescription, setNewKeyDescription] = useState('')
  const [createdKey, setCreatedKey] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [copied, setCopied] = useState(false)

  const handleCreate = async () => {
    if (!newKeyName || !newKeyClientId) return
    setCreating(true)
    try {
      const result = await createApiKey({
        name: newKeyName,
        client_id: newKeyClientId,
        description: newKeyDescription || undefined,
      })
      setCreatedKey(result.key)
      setNewKeyName('')
      setNewKeyClientId('')
      setNewKeyDescription('')
      onRefresh()
    } catch (e) {
      console.error('Failed to create key:', e)
    } finally {
      setCreating(false)
    }
  }

  const handleRevoke = async (keyId: number) => {
    try {
      await revokeApiKey(keyId)
      onRefresh()
    } catch (e) {
      console.error('Failed to revoke key:', e)
    }
  }

  const handleCopy = () => {
    if (createdKey) {
      navigator.clipboard.writeText(createdKey)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }

  return (
    <div className="mb-6">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold">API Keys</h2>
        <button
          onClick={() => { setShowCreate(!showCreate); setCreatedKey(null) }}
          className="bg-blue-600 hover:bg-blue-700 px-3 py-1.5 rounded text-sm"
        >
          {showCreate ? 'Cancel' : 'Create Key'}
        </button>
      </div>

      {/* Create Key Form */}
      {showCreate && (
        <div className="bg-gray-800 rounded-lg p-4 border border-gray-700 mb-4">
          {createdKey ? (
            <div>
              <div className="text-green-400 font-semibold mb-2">Key Created Successfully</div>
              <p className="text-yellow-400 text-sm mb-3">
                Copy this key now - it will not be shown again.
              </p>
              <div className="flex items-center gap-2 mb-3">
                <code className="bg-gray-900 px-3 py-2 rounded font-mono text-sm flex-1 break-all">
                  {createdKey}
                </code>
                <button
                  onClick={handleCopy}
                  className="bg-gray-700 hover:bg-gray-600 px-3 py-2 rounded text-sm whitespace-nowrap"
                >
                  {copied ? 'Copied!' : 'Copy'}
                </button>
              </div>
              <button
                onClick={() => { setCreatedKey(null); setShowCreate(false) }}
                className="text-gray-400 hover:text-white text-sm"
              >
                Done
              </button>
            </div>
          ) : (
            <div className="space-y-3">
              <div>
                <label className="text-gray-400 text-sm block mb-1">Name</label>
                <input
                  type="text"
                  value={newKeyName}
                  onChange={e => setNewKeyName(e.target.value)}
                  placeholder="e.g. my-app-key"
                  className="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-sm"
                />
              </div>
              <div>
                <label className="text-gray-400 text-sm block mb-1">Client ID</label>
                <input
                  type="text"
                  value={newKeyClientId}
                  onChange={e => setNewKeyClientId(e.target.value)}
                  placeholder="e.g. my-app"
                  className="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-sm"
                />
              </div>
              <div>
                <label className="text-gray-400 text-sm block mb-1">Description (optional)</label>
                <input
                  type="text"
                  value={newKeyDescription}
                  onChange={e => setNewKeyDescription(e.target.value)}
                  placeholder="What is this key for?"
                  className="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-sm"
                />
              </div>
              <button
                onClick={handleCreate}
                disabled={creating || !newKeyName || !newKeyClientId}
                className="bg-green-600 hover:bg-green-700 disabled:opacity-50 px-4 py-2 rounded text-sm"
              >
                {creating ? 'Creating...' : 'Generate Key'}
              </button>
            </div>
          )}
        </div>
      )}

      {/* Keys Table */}
      <div className="bg-gray-800 rounded-lg border border-gray-700 overflow-hidden">
        <table className="w-full">
          <thead className="bg-gray-750 border-b border-gray-700">
            <tr className="text-left text-gray-400 text-sm">
              <th className="py-2 px-3">Prefix</th>
              <th className="py-2 px-3">Name</th>
              <th className="py-2 px-3">Client ID</th>
              <th className="py-2 px-3">Created</th>
              <th className="py-2 px-3">Last Used</th>
              <th className="py-2 px-3">Status</th>
              <th className="py-2 px-3"></th>
            </tr>
          </thead>
          <tbody>
            {keys.map(k => (
              <tr key={k.id} className="border-b border-gray-700">
                <td className="py-2 px-3 font-mono text-sm">{k.prefix}...</td>
                <td className="py-2 px-3 text-sm">{k.name}</td>
                <td className="py-2 px-3 text-gray-400 text-sm">{k.client_id}</td>
                <td className="py-2 px-3 text-gray-400 text-sm">
                  {k.created_at ? formatTimestamp(k.created_at) : '-'}
                </td>
                <td className="py-2 px-3 text-gray-400 text-sm">
                  {k.last_used_at ? formatTimestamp(k.last_used_at) : 'Never'}
                </td>
                <td className="py-2 px-3">
                  <span className={`px-2 py-0.5 rounded text-xs ${k.is_active ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
                    {k.is_active ? 'Active' : 'Revoked'}
                  </span>
                </td>
                <td className="py-2 px-3">
                  {k.is_active && (
                    <button
                      onClick={() => handleRevoke(k.id)}
                      className="text-red-400 hover:text-red-300 text-sm"
                    >
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {keys.length === 0 && (
              <tr>
                <td colSpan={7} className="py-8 text-center text-gray-500">
                  No API keys created yet
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function TokenBudgetSection({ budgetConfig, budgetUsage, catalog, onRefresh }: {
  budgetConfig: BudgetConfig | null
  budgetUsage: BudgetUsage | null
  catalog: Catalog | null
  onRefresh: () => void
}) {
  const [assignModel, setAssignModel] = useState('')
  const [assignTier, setAssignTier] = useState('')
  const [assigning, setAssigning] = useState(false)
  const [showCreateTier, setShowCreateTier] = useState(false)
  const [newTierName, setNewTierName] = useState('')
  const [newTierMultiplier, setNewTierMultiplier] = useState('1.0')
  const [newTierLimit, setNewTierLimit] = useState('')
  const [creatingTier, setCreatingTier] = useState(false)
  const [showClassifications, setShowClassifications] = useState(false)

  if (!budgetConfig) return null

  // Build model list from catalog (already fetched and working) merged with classification data
  const classificationMap = new Map(budgetConfig.model_classifications.map(m => [m.model, m]))
  const allModels: string[] = []
  if (catalog) {
    for (const ep of catalog.endpoints) {
      for (const model of ep.models) {
        if (!allModels.includes(model)) allModels.push(model)
      }
    }
  }
  // Also include any models from classifications not in catalog
  for (const mc of budgetConfig.model_classifications) {
    if (!allModels.includes(mc.model)) allModels.push(mc.model)
  }
  allModels.sort()

  // Classify using the map, fallback to unclassified
  const allClassified = allModels.map(name => {
    const mc = classificationMap.get(name)
    return mc || { model: name, tier: null, cost_multiplier: budgetConfig.default_cost_multiplier, classified: false }
  })

  const handleAssign = async () => {
    if (!assignModel || !assignTier) return
    setAssigning(true)
    try {
      const result = await assignModelTier(assignModel, assignTier)
      if (result.status === 'success') {
        setAssignModel('')
        setAssignTier('')
        onRefresh()
      }
    } catch (e) {
      console.error('Failed to assign:', e)
    } finally {
      setAssigning(false)
    }
  }

  const handleUnassign = async (model: string) => {
    try {
      await unassignModelTier(model)
      onRefresh()
    } catch (e) {
      console.error('Failed to unassign:', e)
    }
  }

  const unclassified = allClassified.filter(m => !m.classified)
  const classified = allClassified.filter(m => m.classified)

  return (
    <div className="mb-6">
      <h2 className="text-lg font-semibold mb-3 flex items-center gap-2">
        Token Budgets
        {budgetConfig.enabled ? (
          <span className="bg-green-900 text-green-300 px-2 py-0.5 rounded-full text-xs">Enabled</span>
        ) : (
          <span className="bg-gray-700 text-gray-400 px-2 py-0.5 rounded-full text-xs">Disabled</span>
        )}
      </h2>

      {/* Budget Config Overview */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
        <div className="bg-gray-800 rounded-lg p-3 border border-gray-700">
          <div className="text-gray-400 text-xs">Daily Limit</div>
          <div className="text-xl font-bold">{budgetConfig.default_daily_limit.toLocaleString()}</div>
          <div className="text-gray-500 text-xs">tokens/key</div>
        </div>
        <div className="bg-gray-800 rounded-lg p-3 border border-gray-700">
          <div className="text-gray-400 text-xs">Default Multiplier</div>
          <div className="text-xl font-bold">{budgetConfig.default_cost_multiplier}x</div>
          <div className="text-gray-500 text-xs">unclassified models</div>
        </div>
        <div className="bg-gray-800 rounded-lg p-3 border border-gray-700">
          <div className="text-gray-400 text-xs">Tiers</div>
          <div className="text-xl font-bold">{budgetConfig.tiers.length}</div>
        </div>
        <div className="bg-gray-800 rounded-lg p-3 border border-orange-800">
          <div className="text-orange-400 text-xs">Unclassified</div>
          <div className="text-xl font-bold text-orange-400">{unclassified.length}</div>
          <div className="text-gray-500 text-xs">using default rate</div>
        </div>
      </div>

      {/* Tiers */}
      <div className="bg-gray-800 rounded-lg border border-gray-700 p-4 mb-4">
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-sm font-semibold text-gray-300">Cost Tiers</h3>
          <button
            onClick={() => setShowCreateTier(!showCreateTier)}
            className="bg-blue-600 hover:bg-blue-700 px-3 py-1 rounded text-xs"
          >
            {showCreateTier ? 'Cancel' : 'Add Tier'}
          </button>
        </div>
        {showCreateTier && (
          <div className="bg-gray-900 rounded p-3 mb-3 flex gap-2 items-end">
            <div>
              <label className="text-gray-400 text-xs block mb-1">Name</label>
              <input type="text" value={newTierName} onChange={e => setNewTierName(e.target.value)} placeholder="e.g. standard" className="bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm w-32" />
            </div>
            <div>
              <label className="text-gray-400 text-xs block mb-1">Multiplier</label>
              <input type="number" value={newTierMultiplier} onChange={e => setNewTierMultiplier(e.target.value)} step="0.1" min="0" className="bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm w-24" />
            </div>
            <div>
              <label className="text-gray-400 text-xs block mb-1">Daily Limit (optional)</label>
              <input type="number" value={newTierLimit} onChange={e => setNewTierLimit(e.target.value)} placeholder="unlimited" min="0" className="bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm w-32" />
            </div>
            <button
              disabled={creatingTier || !newTierName}
              onClick={async () => {
                setCreatingTier(true)
                try {
                  await createTier(newTierName, parseFloat(newTierMultiplier) || 1.0, newTierLimit ? parseInt(newTierLimit) : undefined)
                  setNewTierName(''); setNewTierMultiplier('1.0'); setNewTierLimit(''); setShowCreateTier(false)
                  onRefresh()
                } catch (e) { console.error('Failed to create tier:', e) }
                finally { setCreatingTier(false) }
              }}
              className="bg-green-600 hover:bg-green-700 disabled:opacity-50 px-3 py-1.5 rounded text-xs whitespace-nowrap"
            >
              {creatingTier ? 'Creating...' : 'Create'}
            </button>
          </div>
        )}
        {budgetConfig.tiers.length > 0 ? (
          <div className="flex flex-wrap gap-3">
            {budgetConfig.tiers.map(tier => (
              <div key={tier.name} className="bg-gray-900 rounded px-3 py-2 border border-gray-700 flex items-start gap-2">
                <div>
                  <div className="font-semibold text-sm">{tier.name}</div>
                  <div className="text-gray-400 text-xs">{tier.cost_multiplier}x multiplier</div>
                  {tier.daily_limit && <div className="text-gray-500 text-xs">{tier.daily_limit.toLocaleString()} daily cap</div>}
                </div>
                <button
                  onClick={async () => {
                    const result = await deleteTier(tier.name)
                    if (result.status === 'error') alert(result.message)
                    else onRefresh()
                  }}
                  className="text-red-400 hover:text-red-300 text-xs mt-0.5"
                  title="Delete tier"
                >&times;</button>
              </div>
            ))}
          </div>
        ) : (
          <div>
            <div className="text-gray-500 text-sm mb-2">No tiers configured yet.</div>
            <button
              onClick={async () => {
                const defaults = [
                  { name: 'frontier', multiplier: 15.0 },
                  { name: 'midrange', multiplier: 3.0 },
                  { name: 'standard', multiplier: 1.0 },
                  { name: 'embedding', multiplier: 0.1 },
                ]
                for (const d of defaults) {
                  await createTier(d.name, d.multiplier)
                }
                onRefresh()
              }}
              className="bg-blue-600 hover:bg-blue-700 px-3 py-1.5 rounded text-xs"
            >
              Create Default Tiers (frontier 15x, midrange 3x, standard 1x, embedding 0.1x)
            </button>
          </div>
        )}
      </div>

      {/* Assign Model to Tier */}
      <div className="bg-gray-800 rounded-lg border border-gray-700 p-4 mb-4">
        <h3 className="text-sm font-semibold text-gray-300 mb-2">Assign Model to Tier</h3>
        <div className="flex gap-2 items-end">
          <div className="flex-1">
            <label className="text-gray-400 text-xs block mb-1">Model</label>
            {allModels.length > 0 ? (
              <select
                value={assignModel}
                onChange={e => setAssignModel(e.target.value)}
                className="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-sm"
              >
                <option value="">Select model...</option>
                {unclassified.length > 0 && <optgroup label="Unclassified">
                  {unclassified.map(m => (
                    <option key={m.model} value={m.model}>{m.model} ({m.cost_multiplier}x)</option>
                  ))}
                </optgroup>}
                {classified.length > 0 && <optgroup label="Classified">
                  {classified.map(m => (
                    <option key={m.model} value={m.model}>{m.model} ({m.tier} - {m.cost_multiplier}x)</option>
                  ))}
                </optgroup>}
              </select>
            ) : (
              <input
                type="text"
                value={assignModel}
                onChange={e => setAssignModel(e.target.value)}
                placeholder="e.g. llama3.2 or llama-*"
                className="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-sm"
              />
            )}
          </div>
          <div className="w-48">
            <label className="text-gray-400 text-xs block mb-1">Tier</label>
            <select
              value={assignTier}
              onChange={e => setAssignTier(e.target.value)}
              className="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-sm"
            >
              <option value="">Select tier...</option>
              {budgetConfig.tiers.map(t => (
                <option key={t.name} value={t.name}>{t.name} ({t.cost_multiplier}x)</option>
              ))}
            </select>
          </div>
          <button
            onClick={handleAssign}
            disabled={assigning || !assignModel || !assignTier}
            className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 px-4 py-2 rounded text-sm whitespace-nowrap"
          >
            {assigning ? 'Assigning...' : 'Assign'}
          </button>
        </div>
      </div>

      {/* Model Classifications Table — Collapsible */}
      <div className="bg-gray-800 rounded-lg border border-gray-700 overflow-hidden mb-4">
        <button
          onClick={() => setShowClassifications(!showClassifications)}
          className="w-full text-left p-3 border-b border-gray-700 flex items-center justify-between hover:bg-gray-750"
        >
          <h3 className="text-sm font-semibold text-gray-300">
            Model Classifications ({allClassified.length} models, {unclassified.length} unclassified)
          </h3>
          <span className="text-gray-400">{showClassifications ? '▼' : '▶'}</span>
        </button>
        {showClassifications && <table className="w-full">
          <thead className="bg-gray-750 border-b border-gray-700">
            <tr className="text-left text-gray-400 text-sm">
              <th className="py-2 px-3">Model</th>
              <th className="py-2 px-3">Tier</th>
              <th className="py-2 px-3 text-right">Multiplier</th>
              <th className="py-2 px-3"></th>
            </tr>
          </thead>
          <tbody>
            {allClassified.map(m => (
              <tr key={m.model} className={`border-b border-gray-700 ${!m.classified ? 'bg-orange-900/10' : ''}`}>
                <td className="py-2 px-3 font-mono text-sm">{m.model}</td>
                <td className="py-2 px-3">
                  {m.classified ? (
                    <span className="px-2 py-0.5 rounded text-xs bg-blue-900 text-blue-300">{m.tier}</span>
                  ) : (
                    <span className="px-2 py-0.5 rounded text-xs bg-orange-900 text-orange-300">unclassified</span>
                  )}
                </td>
                <td className="py-2 px-3 text-right text-sm">{m.cost_multiplier}x</td>
                <td className="py-2 px-3 text-right">
                  {m.classified && (
                    <button
                      onClick={() => handleUnassign(m.model)}
                      className="text-red-400 hover:text-red-300 text-xs"
                    >
                      Remove
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {allClassified.length === 0 && (
              <tr><td colSpan={4} className="py-8 text-center text-gray-500">No models discovered yet</td></tr>
            )}
          </tbody>
        </table>}
      </div>

      {/* Per-Key Usage */}
      {budgetUsage && budgetUsage.enabled && budgetUsage.keys.length > 0 && (
        <div className="bg-gray-800 rounded-lg border border-gray-700 overflow-hidden">
          <h3 className="text-sm font-semibold text-gray-300 p-3 border-b border-gray-700">Per-Key Budget Usage</h3>
          <table className="w-full">
            <thead className="bg-gray-750 border-b border-gray-700">
              <tr className="text-left text-gray-400 text-sm">
                <th className="py-2 px-3">Key</th>
                <th className="py-2 px-3 text-right">Used</th>
                <th className="py-2 px-3 text-right">Remaining</th>
                <th className="py-2 px-3 text-right">Limit</th>
                <th className="py-2 px-3">Usage</th>
              </tr>
            </thead>
            <tbody>
              {budgetUsage.keys.map(k => {
                const pct = k.daily_limit > 0 ? (k.tokens_used / k.daily_limit) * 100 : 0
                return (
                  <tr key={k.key} className="border-b border-gray-700">
                    <td className="py-2 px-3 font-mono text-sm">{k.key.slice(0, 12)}...</td>
                    <td className="py-2 px-3 text-right text-sm">{k.tokens_used.toLocaleString()}</td>
                    <td className="py-2 px-3 text-right text-sm">{k.tokens_remaining.toLocaleString()}</td>
                    <td className="py-2 px-3 text-right text-sm">{k.daily_limit.toLocaleString()}</td>
                    <td className="py-2 px-3 w-32">
                      <div className="bg-gray-700 rounded-full h-2">
                        <div
                          className={`h-2 rounded-full ${pct > 90 ? 'bg-red-500' : pct > 70 ? 'bg-yellow-500' : 'bg-green-500'}`}
                          style={{ width: `${Math.min(pct, 100)}%` }}
                        />
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function PIISection() {
  const [stats, setStats] = useState<PIIStats | null>(null)
  const [events, setEvents] = useState<PIIEvent[]>([])
  const [collapsed, setCollapsed] = useState(true)
  const [typeFilter, setTypeFilter] = useState<string>('')

  useEffect(() => {
    fetchPIIStats().then(setStats)
    fetchPIIEvents(50, typeFilter || undefined).then(r => setEvents(r.events))
  }, [typeFilter])

  // Auto-refresh
  useEffect(() => {
    const interval = setInterval(() => {
      fetchPIIStats().then(setStats)
      fetchPIIEvents(50, typeFilter || undefined).then(r => setEvents(r.events))
    }, 5000)
    return () => clearInterval(interval)
  }, [typeFilter])

  if (!stats) return null

  const piiTypes = Object.keys(stats.by_type)

  return (
    <div className="bg-gray-800 rounded-lg border border-gray-700">
      <div
        className="p-4 cursor-pointer flex items-center justify-between"
        onClick={() => setCollapsed(!collapsed)}
      >
        <h2 className="text-lg font-semibold">
          PII Detection Audit
          <span className="text-sm font-normal text-gray-400 ml-2">
            ({stats.total_detections} detections, {stats.unique_values} unique values)
          </span>
        </h2>
        <span className="text-gray-400">{collapsed ? '▶' : '▼'}</span>
      </div>

      {!collapsed && (
        <div className="p-4 pt-0 space-y-4">
          {/* Stats grid */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <StatCard label="Total Detections" value={stats.total_detections} />
            <StatCard label="Unique Requests" value={stats.unique_requests} />
            <StatCard label="Scrubbed" value={stats.scrubbed_count} />
            <StatCard label="Flagged Only" value={stats.flagged_only_count} subtext="detected but not scrubbed" />
          </div>

          {/* By type breakdown */}
          {piiTypes.length > 0 && (
            <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
              {piiTypes.map(type => (
                <div
                  key={type}
                  className={`p-2 rounded text-center text-sm cursor-pointer border ${typeFilter === type ? 'border-blue-500 bg-blue-900/20' : 'border-gray-700 bg-gray-900 hover:border-gray-600'}`}
                  onClick={() => setTypeFilter(typeFilter === type ? '' : type)}
                >
                  <div className="font-bold text-yellow-400">{stats.by_type[type]}</div>
                  <div className="text-gray-400 text-xs">{type}</div>
                </div>
              ))}
            </div>
          )}

          {/* Events table */}
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-750 border-b border-gray-700">
                <tr className="text-left text-gray-400 text-sm">
                  <th className="py-2 px-3">Time</th>
                  <th className="py-2 px-3">Type</th>
                  <th className="py-2 px-3">Role</th>
                  <th className="py-2 px-3">Task</th>
                  <th className="py-2 px-3">Value Hash</th>
                  <th className="py-2 px-3">Scrubbed</th>
                </tr>
              </thead>
              <tbody>
                {events.map(event => (
                  <tr key={event.id} className="border-b border-gray-700 hover:bg-gray-750">
                    <td className="py-2 px-3 text-gray-400">
                      {event.timestamp ? formatTime(event.timestamp) : '-'}
                    </td>
                    <td className="py-2 px-3">
                      <span className="px-2 py-0.5 rounded text-xs bg-yellow-900 text-yellow-300">
                        {event.pii_type}
                      </span>
                    </td>
                    <td className="py-2 px-3 text-gray-300">{event.message_role || '-'}</td>
                    <td className="py-2 px-3 text-gray-300">{event.task || '-'}</td>
                    <td className="py-2 px-3 font-mono text-xs text-gray-500" title={event.value_hash}>
                      {event.value_hash.substring(0, 12)}...
                    </td>
                    <td className="py-2 px-3">
                      {event.was_scrubbed ? (
                        <span className="px-2 py-0.5 rounded text-xs bg-green-900 text-green-300">scrubbed</span>
                      ) : (
                        <span className="px-2 py-0.5 rounded text-xs bg-orange-900 text-orange-300">flagged</span>
                      )}
                    </td>
                  </tr>
                ))}
                {events.length === 0 && (
                  <tr>
                    <td colSpan={6} className="py-8 text-center text-gray-500">
                      No PII detections{typeFilter ? ` for type ${typeFilter}` : ''} in the last 24 hours
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="text-xs text-gray-500 italic">
            Raw PII values are never stored. Only SHA-256 hashes are retained for deduplication and audit.
          </div>
        </div>
      )}
    </div>
  )
}

function SecurityScansSection({ onRefresh }: { onRefresh: () => void }) {
  const [expanded, setExpanded] = useState(false)
  const [scans, setScans] = useState<SecurityScan[]>([])
  const [labelStats, setLabelStats] = useState<LabelStats | null>(null)
  const [filter, setFilter] = useState<'all' | 'unlabeled' | 'disagreements'>('unlabeled')
  const [selectedScans, setSelectedScans] = useState<Set<string>>(new Set())
  const [expandedScan, setExpandedScan] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [exportFormat, setExportFormat] = useState<'llama_guard' | 'raw'>('llama_guard')
  const [exportResult, setExportResult] = useState<{ count: number } | null>(null)

  const loadScans = useCallback(async () => {
    setLoading(true)
    try {
      const [scansData, statsData] = await Promise.all([
        fetchSecurityScans({
          limit: 50,
          unlabeled_only: filter === 'unlabeled',
          disagreements_only: filter === 'disagreements',
        }),
        fetchLabelStats(),
      ])
      setScans(scansData.scans)
      setLabelStats(statsData)
    } catch (e) {
      console.error('Failed to load scans:', e)
    } finally {
      setLoading(false)
    }
  }, [filter])

  useEffect(() => {
    loadScans()
  }, [loadScans])

  const handleLabel = async (requestId: string, label: string, category?: string) => {
    await labelScan(requestId, label, category)
    setSelectedScans(prev => { const next = new Set(prev); next.delete(requestId); return next })
    loadScans()
    onRefresh()
  }

  const handleBulkLabel = async (label: string) => {
    if (selectedScans.size === 0) return
    await bulkLabelScans(Array.from(selectedScans), label)
    setSelectedScans(new Set())
    loadScans()
    onRefresh()
  }

  const toggleSelect = (id: string) => {
    setSelectedScans(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleSelectAll = () => {
    if (selectedScans.size === scans.length) {
      setSelectedScans(new Set())
    } else {
      setSelectedScans(new Set(scans.map(s => s.request_id)))
    }
  }

  const handleExport = async () => {
    try {
      const result = await exportTrainingData(exportFormat)
      setExportResult({ count: result.count })
      // Trigger download
      const blob = new Blob([JSON.stringify(result.examples, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `training-data-${exportFormat}-${new Date().toISOString().slice(0, 10)}.json`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      console.error('Export failed:', e)
    }
  }

  const progressPct = labelStats && labelStats.total > 0 ? (labelStats.labeled / labelStats.total) * 100 : 0

  return (
    <div className="mb-6">
      <button
        onClick={() => { setExpanded(!expanded); if (!expanded && scans.length === 0) loadScans() }}
        className="w-full text-left flex items-center justify-between mb-3"
      >
        <h2 className="text-lg font-semibold flex items-center gap-2">
          Security Scan Labeling
          {labelStats && <span className="text-gray-400 text-sm font-normal">({labelStats.total} scans, {labelStats.unlabeled} unlabeled)</span>}
        </h2>
        <span className="text-gray-400">{expanded ? '▼' : '▶'}</span>
      </button>

      {!expanded ? null : <>
      {/* Label Stats */}
      {labelStats && (
        <div className="grid grid-cols-2 md:grid-cols-6 gap-3 mb-4">
          <div className="bg-gray-800 rounded-lg p-3 border border-gray-700">
            <div className="text-gray-400 text-xs">Total Scans</div>
            <div className="text-xl font-bold">{labelStats.total}</div>
          </div>
          <div className="bg-gray-800 rounded-lg p-3 border border-green-800">
            <div className="text-green-400 text-xs">Labeled</div>
            <div className="text-xl font-bold text-green-400">{labelStats.labeled}</div>
          </div>
          <div className="bg-gray-800 rounded-lg p-3 border border-gray-700">
            <div className="text-gray-400 text-xs">Unlabeled</div>
            <div className="text-xl font-bold">{labelStats.unlabeled}</div>
          </div>
          <div className="bg-gray-800 rounded-lg p-3 border border-gray-700">
            <div className="text-gray-400 text-xs">Safe</div>
            <div className="text-xl font-bold text-green-400">{labelStats.safe}</div>
          </div>
          <div className="bg-gray-800 rounded-lg p-3 border border-gray-700">
            <div className="text-gray-400 text-xs">Unsafe</div>
            <div className="text-xl font-bold text-red-400">{labelStats.unsafe}</div>
          </div>
          <div className="bg-gray-800 rounded-lg p-3 border border-yellow-800">
            <div className="text-yellow-400 text-xs">Disagreements</div>
            <div className="text-xl font-bold text-yellow-400">{labelStats.disagreements}</div>
          </div>
        </div>
      )}

      {/* Progress Bar */}
      {labelStats && labelStats.total > 0 && (
        <div className="bg-gray-800 rounded-lg p-3 border border-gray-700 mb-4">
          <div className="flex justify-between text-sm mb-1">
            <span className="text-gray-400">Labeling Progress</span>
            <span className="text-gray-300">{progressPct.toFixed(1)}% ({labelStats.labeled}/{labelStats.total})</span>
          </div>
          <div className="bg-gray-700 rounded-full h-3">
            <div className="bg-green-500 h-3 rounded-full transition-all" style={{ width: `${progressPct}%` }} />
          </div>
        </div>
      )}

      {/* Filter + Bulk Actions + Export */}
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <div className="flex gap-2">
          {(['all', 'unlabeled', 'disagreements'] as const).map(f => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-3 py-1.5 rounded text-sm capitalize ${filter === f ? 'bg-blue-600 text-white' : 'bg-gray-800 text-gray-400 hover:text-white'}`}
            >
              {f === 'all' ? 'All Scans' : f === 'unlabeled' ? 'Unlabeled' : 'Disagreements'}
            </button>
          ))}
        </div>
        <div className="flex gap-2 items-center">
          {selectedScans.size > 0 && (
            <>
              <span className="text-gray-400 text-sm">{selectedScans.size} selected</span>
              <button onClick={() => handleBulkLabel('safe')} className="bg-green-700 hover:bg-green-600 px-3 py-1.5 rounded text-xs">Mark Safe</button>
              <button onClick={() => handleBulkLabel('unsafe')} className="bg-red-700 hover:bg-red-600 px-3 py-1.5 rounded text-xs">Mark Unsafe</button>
            </>
          )}
          <div className="border-l border-gray-600 pl-2 flex gap-2 items-center">
            <select value={exportFormat} onChange={e => setExportFormat(e.target.value as 'llama_guard' | 'raw')} className="bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-xs">
              <option value="llama_guard">Llama Guard Format</option>
              <option value="raw">Raw Format</option>
            </select>
            <button onClick={handleExport} className="bg-purple-700 hover:bg-purple-600 px-3 py-1.5 rounded text-xs">Export Training Data</button>
            {exportResult && <span className="text-green-400 text-xs">{exportResult.count} examples exported</span>}
          </div>
        </div>
      </div>

      {/* Scans Table */}
      <div className="bg-gray-800 rounded-lg border border-gray-700 overflow-hidden">
        {loading ? (
          <div className="p-8 text-center text-gray-500">Loading scans...</div>
        ) : scans.length === 0 ? (
          <div className="p-8 text-center text-gray-500">
            {filter === 'unlabeled' ? 'No unlabeled scans' : filter === 'disagreements' ? 'No disagreements found' : 'No security scans yet'}
          </div>
        ) : (
          <>
            <table className="w-full">
              <thead className="bg-gray-750 border-b border-gray-700">
                <tr className="text-left text-gray-400 text-sm">
                  <th className="py-2 px-3 w-8">
                    <input type="checkbox" checked={selectedScans.size === scans.length && scans.length > 0} onChange={toggleSelectAll} className="rounded" />
                  </th>
                  <th className="py-2 px-3">Time</th>
                  <th className="py-2 px-3 max-w-md">Message</th>
                  <th className="py-2 px-3">Regex</th>
                  <th className="py-2 px-3">Guard</th>
                  <th className="py-2 px-3">Label</th>
                  <th className="py-2 px-3">Actions</th>
                </tr>
              </thead>
              <tbody>
                {scans.map(scan => (
                  <>
                    <tr key={scan.request_id} className={`border-b border-gray-700 cursor-pointer hover:bg-gray-750 ${scan.is_disagreement ? 'bg-yellow-900/10' : ''}`}>
                      <td className="py-2 px-3" onClick={e => e.stopPropagation()}>
                        <input type="checkbox" checked={selectedScans.has(scan.request_id)} onChange={() => toggleSelect(scan.request_id)} className="rounded" />
                      </td>
                      <td className="py-2 px-3 text-sm text-gray-400" onClick={() => setExpandedScan(expandedScan === scan.request_id ? null : scan.request_id)}>
                        {scan.timestamp ? formatTime(scan.timestamp) : '-'}
                      </td>
                      <td className="py-2 px-3 text-sm max-w-md" onClick={() => setExpandedScan(expandedScan === scan.request_id ? null : scan.request_id)}>
                        <div className="truncate text-gray-300" title={scan.messages.filter(m => m.role === 'user').map(m => m.content).join(' | ') || scan.messages.map(m => m.content).join(' | ')}>
                          {(() => {
                            const userMsgs = scan.messages.filter(m => m.role === 'user');
                            const preview = userMsgs.length > 0 ? userMsgs.map(m => m.content).join(' | ') : scan.messages.map(m => `[${m.role}] ${m.content}`).join(' | ');
                            return preview || <span className="text-gray-500 italic">no content</span>;
                          })()}
                        </div>
                      </td>
                      <td className="py-2 px-3" onClick={() => setExpandedScan(expandedScan === scan.request_id ? null : scan.request_id)}>
                        <span className={`px-2 py-0.5 rounded text-xs ${scan.regex_threat_level !== 'none' ? 'bg-red-900 text-red-300' : 'bg-green-900 text-green-300'}`}>
                          {scan.regex_threat_level}
                        </span>
                        {scan.is_disagreement && <span className="ml-1 px-1 py-0.5 rounded text-xs bg-yellow-900 text-yellow-300">!</span>}
                      </td>
                      <td className="py-2 px-3" onClick={() => setExpandedScan(expandedScan === scan.request_id ? null : scan.request_id)}>
                        {scan.guard_safe === null ? (
                          <span className="text-gray-500 text-xs">-</span>
                        ) : (
                          <span className={`px-2 py-0.5 rounded text-xs ${scan.guard_safe ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
                            {scan.guard_safe ? 'safe' : 'unsafe'}
                          </span>
                        )}
                      </td>
                      <td className="py-2 px-3">
                        {scan.label ? (
                          <span className={`px-2 py-0.5 rounded text-xs ${scan.label === 'safe' ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
                            {scan.label}{scan.label_category ? ` (${scan.label_category})` : ''}
                          </span>
                        ) : (
                          <span className="text-gray-500 text-xs">unlabeled</span>
                        )}
                      </td>
                      <td className="py-2 px-3">
                        {!scan.label && (
                          <div className="flex gap-1">
                            <button onClick={(e) => { e.stopPropagation(); handleLabel(scan.request_id, 'safe') }} className="bg-green-800 hover:bg-green-700 px-2 py-0.5 rounded text-xs">Safe</button>
                            <button onClick={(e) => { e.stopPropagation(); handleLabel(scan.request_id, 'unsafe') }} className="bg-red-800 hover:bg-red-700 px-2 py-0.5 rounded text-xs">Unsafe</button>
                          </div>
                        )}
                      </td>
                    </tr>
                    {expandedScan === scan.request_id && (
                      <tr key={`${scan.request_id}-detail`} className="border-b border-gray-700">
                        <td colSpan={7} className="p-3 bg-gray-900">
                          <div className="text-xs font-mono space-y-2">
                            <div><strong className="text-gray-400">Request ID:</strong> {scan.request_id}</div>
                            {scan.model && <div><strong className="text-gray-400">Model:</strong> {scan.model}</div>}
                            <div>
                              <strong className="text-gray-400">Messages:</strong>
                              <div className="mt-1 space-y-1">
                                {scan.messages.map((msg, i) => (
                                  <div key={i} className="bg-gray-800 p-2 rounded">
                                    <span className={`font-bold ${msg.role === 'user' ? 'text-blue-400' : msg.role === 'system' ? 'text-yellow-400' : 'text-green-400'}`}>{msg.role}: </span>
                                    <span className="text-gray-300 whitespace-pre-wrap">{msg.content}</span>
                                  </div>
                                ))}
                              </div>
                            </div>
                            {scan.label_notes && <div><strong className="text-gray-400">Notes:</strong> {scan.label_notes}</div>}
                          </div>
                        </td>
                      </tr>
                    )}
                  </>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
      </>}
    </div>
  )
}

function App() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [requests, setRequests] = useState<Request[]>([])
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [securityAlerts, setSecurityAlerts] = useState<SecurityAlert[]>([])
  const [securityStats, setSecurityStats] = useState<SecurityStats | null>(null)
  const [guardResults, setGuardResults] = useState<SecurityResult[]>([])
  const guardDisagreementsRef = useRef(false)
  const [apiKeys, setApiKeys] = useState<ApiKeyInfo[]>([])
  const [budgetConfig, setBudgetConfig] = useState<BudgetConfig | null>(null)
  const [budgetUsage, setBudgetUsage] = useState<BudgetUsage | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedRequest, setSelectedRequest] = useState<RequestDetail | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [statsData, requestsData, catalogData, healthData, secAlertsData, secStatsData, guardData, apiKeysData, budgetConfigData, budgetUsageData] = await Promise.all([
        fetchStats(),
        fetchRequests(),
        fetchCatalog(),
        fetchHealth(),
        fetchSecurityAlerts(),
        fetchSecurityStats(),
        fetchSecurityResults(50, guardDisagreementsRef.current),
        fetchApiKeys(),
        fetchBudgetConfig(),
        fetchBudgetUsage(),
      ])
      setStats(statsData)
      setRequests(requestsData.requests)
      setCatalog(catalogData)
      setHealth(healthData)
      setSecurityAlerts(secAlertsData.alerts)
      setSecurityStats(secStatsData)
      setGuardResults(guardData.results)
      setApiKeys(apiKeysData.keys)
      setBudgetConfig(budgetConfigData)
      setBudgetUsage(budgetUsageData)
      setError(null)
    } catch (e) {
      setError(`Failed to fetch data: ${e}`)
    } finally {
      setLoading(false)
    }
  }, [])

  const handleRequestClick = async (request: Request) => {
    setLoadingDetail(true)
    try {
      const detail = await fetchRequestDetail(request.request_id)
      setSelectedRequest(detail)
    } catch (e) {
      console.error('Failed to fetch request detail:', e)
    } finally {
      setLoadingDetail(false)
    }
  }

  useEffect(() => {
    refresh()
    const interval = setInterval(refresh, 5000) // Refresh every 5s
    return () => clearInterval(interval)
  }, [])

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-xl">Loading...</div>
      </div>
    )
  }

  return (
    <div className="min-h-screen p-6">
      {/* Request Detail Modal */}
      {selectedRequest && (
        <RequestDetailPanel detail={selectedRequest} onClose={() => setSelectedRequest(null)} />
      )}

      {/* Loading overlay for detail */}
      {loadingDetail && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="text-xl">Loading...</div>
        </div>
      )}

      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">LLM Gateway Dashboard</h1>
          <p className="text-gray-400 text-sm">
            {health?.providers_healthy}/{health?.providers_configured} endpoints healthy
          </p>
        </div>
        <button
          onClick={refresh}
          className="bg-blue-600 hover:bg-blue-700 px-4 py-2 rounded text-sm"
        >
          Refresh
        </button>
      </div>

      {error && (
        <div className="bg-red-900 border border-red-700 rounded p-4 mb-6">
          {error}
        </div>
      )}

      {/* Stats Grid */}
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-4 mb-6">
        <StatCard label="Total Requests" value={stats?.total_requests || 0} subtext="Last 24h" />
        <StatCard label="Success Rate" value={`${(stats?.success_rate || 0).toFixed(1)}%`} />
        <StatCard label="Avg Latency" value={`${(stats?.avg_latency_ms || 0).toFixed(0)}ms`} />
        <StatCard label="Total Tokens" value={(stats?.total_tokens || 0).toLocaleString()} />
        <StatCard label="Models" value={catalog?.total_models || 0} />
        <StatCard label="Endpoints" value={catalog?.total_endpoints || 0} />
      </div>

      {/* Security Monitor */}
      <SecuritySection
        alerts={securityAlerts}
        stats={securityStats}
        guardResults={guardResults}
        onFilterChange={(d) => { guardDisagreementsRef.current = d; refresh() }}
      />

      {/* PII Detection Audit */}
      <PIISection />

      {/* Security Scan Labeling & Training Data */}
      <SecurityScansSection onRefresh={refresh} />

      {/* API Keys */}
      <ApiKeysSection keys={apiKeys} onRefresh={refresh} />

      {/* Token Budgets */}
      <TokenBudgetSection budgetConfig={budgetConfig} budgetUsage={budgetUsage} catalog={catalog} onRefresh={refresh} />

      {/* Endpoints */}
      <div className="mb-6">
        <h2 className="text-lg font-semibold mb-3">Endpoints</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {catalog?.endpoints.map(endpoint => (
            <EndpointCard key={endpoint.name} endpoint={endpoint} />
          ))}
        </div>
      </div>

      {/* Usage by Endpoint */}
      {stats?.requests_by_endpoint && Object.keys(stats.requests_by_endpoint).length > 0 && (
        <div className="mb-6">
          <h2 className="text-lg font-semibold mb-3">Requests by Endpoint</h2>
          <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
            <div className="space-y-2">
              {Object.entries(stats.requests_by_endpoint).map(([endpoint, count]) => {
                const pct = (count / stats.total_requests) * 100
                return (
                  <div key={endpoint} className="flex items-center gap-3">
                    <div className="w-32 text-sm">{endpoint}</div>
                    <div className="flex-1 bg-gray-700 rounded-full h-4">
                      <div
                        className="bg-blue-600 h-4 rounded-full"
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                    <div className="w-16 text-right text-sm text-gray-400">{count}</div>
                  </div>
                )
              })}
            </div>
          </div>
        </div>
      )}

      {/* Top Models */}
      {stats?.top_models && Object.keys(stats.top_models).length > 0 && (
        <div className="mb-6">
          <h2 className="text-lg font-semibold mb-3">Top Models</h2>
          <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
            <div className="flex flex-wrap gap-2">
              {Object.entries(stats.top_models).map(([model, count]) => (
                <div key={model} className="bg-gray-700 px-3 py-1 rounded-full text-sm">
                  {model} <span className="text-gray-400">({count})</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Recent Requests */}
      <div>
        <h2 className="text-lg font-semibold mb-3">Recent Requests</h2>
        <p className="text-gray-400 text-sm mb-2">Click a row to see details</p>
        <div className="bg-gray-800 rounded-lg border border-gray-700 overflow-hidden">
          <table className="w-full">
            <thead className="bg-gray-750 border-b border-gray-700">
              <tr className="text-left text-gray-400 text-sm">
                <th className="py-2 px-3">Time</th>
                <th className="py-2 px-3">Status</th>
                <th className="py-2 px-3">Model</th>
                <th className="py-2 px-3">Endpoint</th>
                <th className="py-2 px-3 text-right">Latency</th>
                <th className="py-2 px-3 text-right">Tokens</th>
              </tr>
            </thead>
            <tbody>
              {requests.map(req => (
                <RequestRow key={req.id} request={req} onClick={() => handleRequestClick(req)} />
              ))}
              {requests.length === 0 && (
                <tr>
                  <td colSpan={6} className="py-8 text-center text-gray-500">
                    No requests yet
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

export default App
