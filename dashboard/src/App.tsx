import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'
import type {
  Stats, Catalog, HealthResponse,
  SecurityAlert, SecurityStats, SecurityResult, ApiKeyInfo,
  BudgetConfig, BudgetUsage,
} from './types'
import {
  AUTH_ERROR_EVENT,
  fetchStats, fetchCatalog, fetchHealth,
  fetchSecurityAlerts, fetchSecurityStats, fetchSecurityResults,
  fetchApiKeys, fetchBudgetConfig, fetchBudgetUsage,
  getStoredApiKey, setStoredApiKey,
} from './lib/api'
import { StatCard, EndpointCard } from './components/shared'
import { RequestsSection } from './components/RequestsPanel'
import { SecuritySection } from './components/SecurityPanel'
import { ApiKeysSection } from './components/KeysPanel'
import { TokenBudgetSection } from './components/BudgetPanel'
import { PIISection } from './components/PIIPanel'
import { PIIMLSection } from './components/PIIMLPanel'
import { VoiceSection } from './components/VoicePanel'
import { RoutingSection } from './components/RoutingPanel'
import { SecurityScansSection } from './components/ScansPanel'
import { ErrorBoundary } from './components/ErrorBoundary'

function App() {
  const [stats, setStats] = useState<Stats | null>(null)
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
  const [activeTab, setActiveTab] = useState<'dashboard' | 'security' | 'keys' | 'requests' | 'voice' | 'routing'>('dashboard')

  // Polling stops once the gateway rejects the key: every rejected poll is a
  // durable audit row (D-055), so a forgotten tab with a stale key would
  // write ~120 denial rows a minute indefinitely (D-057). Manual refresh or
  // entering a key resumes it.
  const authRejectedRef = useRef(false)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [refreshTick, setRefreshTick] = useState(0)

  useEffect(() => {
    const onAuthError = () => {
      authRejectedRef.current = true
      setAuthError(true)
    }
    window.addEventListener(AUTH_ERROR_EVENT, onAuthError)
    return () => window.removeEventListener(AUTH_ERROR_EVENT, onAuthError)
  }, [])

  const refresh = useCallback(async () => {
    authRejectedRef.current = false
    setAuthError(false)
    try {
      const [statsData, catalogData, healthData, secAlertsData, secStatsData, guardData, apiKeysData, budgetConfigData, budgetUsageData] = await Promise.all([
        fetchStats(),
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
      setCatalog(catalogData)
      setHealth(healthData)
      setSecurityAlerts(secAlertsData.alerts)
      setSecurityStats(secStatsData)
      setGuardResults(guardData.results)
      setApiKeys(apiKeysData.keys)
      setBudgetConfig(budgetConfigData)
      setBudgetUsage(budgetUsageData)
      setError(null)
      if (!authRejectedRef.current) {
        setLastUpdated(new Date())
        setRefreshTick(t => t + 1)
      }
    } catch (e) {
      setError(`Failed to fetch data: ${e}`)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
    const interval = setInterval(() => {
      if (!authRejectedRef.current) refresh() // paused while the key is rejected
    }, 5000)
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
    <div className="min-h-screen p-3 sm:p-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 mb-6">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold">LLM Gateway Dashboard</h1>
          <p className="text-gray-400 text-sm">
            {health?.providers_healthy}/{health?.providers_configured} endpoints healthy
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 w-full sm:w-auto">
          <label htmlFor="admin-key" className="sr-only">
            Admin API key
          </label>
          <input
            id="admin-key"
            type="password"
            defaultValue={getStoredApiKey()}
            placeholder="Admin API key"
            onChange={e => setStoredApiKey(e.target.value.trim())}
            onBlur={refresh}
            className="bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm flex-1 min-w-0 sm:w-56 sm:flex-none focus:outline-none focus:border-blue-600"
            title="The gateway's GATEWAY_ADMIN_API_KEY (operator key), not a client inference key. Stored in this browser only."
          />
          <button
            onClick={refresh}
            className="bg-blue-600 hover:bg-blue-700 px-4 py-2 rounded text-sm"
          >
            Refresh
          </button>
          <span className="text-xs text-gray-400 w-full sm:w-auto" role="status" aria-live="polite">
            {authError
              ? lastUpdated
                ? `Paused · showing data from ${lastUpdated.toLocaleTimeString()}`
                : 'Paused · no data yet'
              : lastUpdated
                ? `Updated ${lastUpdated.toLocaleTimeString()}`
                : ''}
          </span>
        </div>
      </div>

      {health?.access?.mode === 'dev' && (
        <div className="bg-yellow-900 border border-yellow-600 rounded p-3 mb-6 text-sm text-left" role="status">
          <strong>Test mode.</strong> Keyless requests and this dashboard work without a key from{' '}
          {health.access.keyless_from.join(', ')}. Requests are logged as client <code>dev</code>.
          Turn off GATEWAY_DEV_MODE before real use.
        </div>
      )}
      {health?.access?.mode === 'solo' && (
        <div className="bg-gray-800 border border-gray-600 rounded p-3 mb-6 text-sm text-left" role="status">
          <strong>Solo mode:</strong> authentication is off, so the gateway only accepts requests from{' '}
          {health.access.keyless_from.join(', ')}.
        </div>
      )}
      {health?.access?.mode === 'keys' && health.access.admin_key_configured === false && (
        <div className="bg-amber-900 border border-amber-700 rounded p-3 mb-6 text-sm text-left" role="status">
          The dashboard needs an operator key: set <code>GATEWAY_ADMIN_API_KEY</code> on the gateway and enter it above.
        </div>
      )}

      {authError && (
        <div className="bg-amber-900 border border-amber-700 rounded p-4 mb-6 text-sm" role="alert">
          <strong>The gateway rejected this key.</strong> The dashboard needs the operator key
          (<code>GATEWAY_ADMIN_API_KEY</code>), not a client inference key. Auto-refresh is paused
          so rejected requests don't fill the audit log; enter the admin key above and press
          Refresh to resume. Figures below are{' '}
          {lastUpdated ? `from ${lastUpdated.toLocaleTimeString()} and may be stale` : 'not loaded'}.
        </div>
      )}

      {error && !authError && (
        <div className="bg-red-900 border border-red-700 rounded p-4 mb-6">
          {error}
        </div>
      )}

      {/* Tab Navigation */}
      <div className="flex gap-1 mb-6 border-b border-gray-700 overflow-x-auto" role="tablist">
        {([
          ['dashboard', 'Dashboard'],
          ['security', 'Security'],
          ['keys', 'Keys & Budgets'],
          ['requests', 'Requests'],
          ['voice', 'Voice'],
          ['routing', 'Routing'],
        ] as const).map(([tab, label]) => (
          <button
            key={tab}
            role="tab"
            aria-selected={activeTab === tab}
            onClick={() => setActiveTab(tab)}
            className={`px-4 py-2 text-sm font-medium rounded-t transition-colors whitespace-nowrap shrink-0 ${
              activeTab === tab
                ? 'bg-gray-800 text-white border border-gray-700 border-b-transparent -mb-px'
                : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800/50'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      <ErrorBoundary key={activeTab} label={activeTab}>
      {/* === Dashboard Tab === */}
      {activeTab === 'dashboard' && (
        <>
          {/* Stats Grid */}
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-4 mb-6">
            <StatCard
              label="Inference Requests"
              value={stats ? stats.total_requests : '—'}
              subtext={`Last 24h${stats?.denied_count ? ` · ${stats.denied_count} denied (auth/policy), not counted` : ''}`}
            />
            <StatCard
              label="Success Rate"
              value={stats && stats.total_requests > 0 ? `${stats.success_rate.toFixed(1)}%` : '—'}
              subtext={stats && stats.total_requests === 0 ? 'No inference traffic yet' : 'Of inference requests'}
            />
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
          <ErrorBoundary label="Security Monitor">
            <SecuritySection
              alerts={securityAlerts}
              stats={securityStats}
              guardResults={guardResults}
              onFilterChange={(d) => { guardDisagreementsRef.current = d; refresh() }}
            />
          </ErrorBoundary>
          <ErrorBoundary label="PII">
            <PIISection />
          </ErrorBoundary>
          <ErrorBoundary label="ML PII Detection">
            <PIIMLSection />
          </ErrorBoundary>
          <ErrorBoundary label="Security Scan Labeling">
            <SecurityScansSection onRefresh={refresh} />
          </ErrorBoundary>
        </>
      )}

      {/* === Keys & Budgets Tab === */}
      {activeTab === 'keys' && (
        <>
          <ApiKeysSection
            keys={apiKeys}
            endpoints={catalog?.endpoints.map(e => e.name) ?? []}
            onRefresh={refresh}
          />
          <TokenBudgetSection budgetConfig={budgetConfig} budgetUsage={budgetUsage} catalog={catalog} onRefresh={refresh} />
        </>
      )}

      {/* === Voice Tab === */}
      {activeTab === 'voice' && <VoiceSection />}

      {activeTab === 'routing' && <RoutingSection />}

      {/* === Requests Tab === */}
      {activeTab === 'requests' && <RequestsSection refreshTick={refreshTick} />}
      </ErrorBoundary>
    </div>
  )
}

export default App
