import { useEffect, useState } from 'react'

// Helper to format UTC timestamp to local time
function formatTimestamp(utcTimestamp: string): string {
  // Ensure the timestamp is treated as UTC
  const timestamp = utcTimestamp.endsWith('Z') ? utcTimestamp : utcTimestamp + 'Z'
  return new Date(timestamp).toLocaleString()
}

function formatTime(utcTimestamp: string): string {
  const timestamp = utcTimestamp.endsWith('Z') ? utcTimestamp : utcTimestamp + 'Z'
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

// API base URL - gateway server
const API_BASE = import.meta.env.VITE_API_URL || 'http://192.168.1.184:8001'

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

function App() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [requests, setRequests] = useState<Request[]>([])
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedRequest, setSelectedRequest] = useState<RequestDetail | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)

  const refresh = async () => {
    try {
      const [statsData, requestsData, catalogData, healthData] = await Promise.all([
        fetchStats(),
        fetchRequests(),
        fetchCatalog(),
        fetchHealth(),
      ])
      setStats(statsData)
      setRequests(requestsData.requests)
      setCatalog(catalogData)
      setHealth(healthData)
      setError(null)
    } catch (e) {
      setError(`Failed to fetch data: ${e}`)
    } finally {
      setLoading(false)
    }
  }

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
