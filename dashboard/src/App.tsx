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
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedRequest, setSelectedRequest] = useState<RequestDetail | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [statsData, requestsData, catalogData, healthData, secAlertsData, secStatsData, guardData, apiKeysData] = await Promise.all([
        fetchStats(),
        fetchRequests(),
        fetchCatalog(),
        fetchHealth(),
        fetchSecurityAlerts(),
        fetchSecurityStats(),
        fetchSecurityResults(50, guardDisagreementsRef.current),
        fetchApiKeys(),
      ])
      setStats(statsData)
      setRequests(requestsData.requests)
      setCatalog(catalogData)
      setHealth(healthData)
      setSecurityAlerts(secAlertsData.alerts)
      setSecurityStats(secStatsData)
      setGuardResults(guardData.results)
      setApiKeys(apiKeysData.keys)
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

      {/* API Keys */}
      <ApiKeysSection keys={apiKeys} onRefresh={refresh} />

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
