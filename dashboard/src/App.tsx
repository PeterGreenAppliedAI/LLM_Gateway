import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'
import type {
  Stats, Request, RequestDetail, Catalog, HealthResponse,
  SecurityAlert, SecurityStats, SecurityResult, ApiKeyInfo,
  BudgetConfig, BudgetUsage,
} from './types'
import {
  AUTH_ERROR_EVENT,
  fetchStats, fetchRequests, fetchRequestDetail, fetchCatalog, fetchHealth,
  fetchSecurityAlerts, fetchSecurityStats, fetchSecurityResults,
  fetchApiKeys, fetchBudgetConfig, fetchBudgetUsage,
  getStoredApiKey, setStoredApiKey,
} from './lib/api'
import { StatCard, EndpointCard } from './components/shared'
import { RequestDetailPanel, RequestRow } from './components/RequestsPanel'
import { SecuritySection } from './components/SecurityPanel'
import { ApiKeysSection } from './components/KeysPanel'
import { TokenBudgetSection } from './components/BudgetPanel'
import { PIISection } from './components/PIIPanel'
import { SecurityScansSection } from './components/ScansPanel'

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
  const [authError, setAuthError] = useState(false)
  const [selectedRequest, setSelectedRequest] = useState<RequestDetail | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [activeTab, setActiveTab] = useState<'dashboard' | 'security' | 'keys' | 'requests'>('dashboard')

  useEffect(() => {
    const onAuthError = () => setAuthError(true)
    window.addEventListener(AUTH_ERROR_EVENT, onAuthError)
    return () => window.removeEventListener(AUTH_ERROR_EVENT, onAuthError)
  }, [])

  const refresh = useCallback(async () => {
    setAuthError(false)
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
  }, [refresh])

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
        <div className="flex items-center gap-2">
          <input
            type="password"
            defaultValue={getStoredApiKey()}
            placeholder="Gateway API key"
            onChange={e => setStoredApiKey(e.target.value.trim())}
            onBlur={refresh}
            className="bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm w-56 focus:outline-none focus:border-blue-600"
            title="Sent as X-API-Key on every dashboard request; stored in this browser only"
          />
          <button
            onClick={refresh}
            className="bg-blue-600 hover:bg-blue-700 px-4 py-2 rounded text-sm"
          >
            Refresh
          </button>
        </div>
      </div>

      {authError && (
        <div className="bg-amber-900 border border-amber-700 rounded p-4 mb-6">
          Gateway API key required or invalid — enter a valid key in the field above.
          Data shown may be incomplete until then.
        </div>
      )}

      {error && !authError && (
        <div className="bg-red-900 border border-red-700 rounded p-4 mb-6">
          {error}
        </div>
      )}

      {/* Tab Navigation */}
      <div className="flex gap-1 mb-6 border-b border-gray-700">
        {([
          ['dashboard', 'Dashboard'],
          ['security', 'Security'],
          ['keys', 'Keys & Budgets'],
          ['requests', 'Requests'],
        ] as const).map(([tab, label]) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-4 py-2 text-sm font-medium rounded-t transition-colors ${
              activeTab === tab
                ? 'bg-gray-800 text-white border border-gray-700 border-b-transparent -mb-px'
                : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800/50'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* === Dashboard Tab === */}
      {activeTab === 'dashboard' && (
        <>
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
        </>
      )}

      {/* === Security Tab === */}
      {activeTab === 'security' && (
        <>
          <SecuritySection
            alerts={securityAlerts}
            stats={securityStats}
            guardResults={guardResults}
            onFilterChange={(d) => { guardDisagreementsRef.current = d; refresh() }}
          />
          <PIISection />
          <SecurityScansSection onRefresh={refresh} />
        </>
      )}

      {/* === Keys & Budgets Tab === */}
      {activeTab === 'keys' && (
        <>
          <ApiKeysSection keys={apiKeys} onRefresh={refresh} />
          <TokenBudgetSection budgetConfig={budgetConfig} budgetUsage={budgetUsage} catalog={catalog} onRefresh={refresh} />
        </>
      )}

      {/* === Requests Tab === */}
      {activeTab === 'requests' && (
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
      )}
    </div>
  )
}

export default App
