import { useState } from 'react'
import type { SecurityAlert, SecurityStats, SecurityResult } from '../types'
import { formatTimestamp, formatTime } from '../lib/format'
import { getSeverityColor } from '../lib/ui'

export function SecurityAlertRow({ alert }: { alert: SecurityAlert }) {
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

export function GuardResultRow({ result }: { result: SecurityResult }) {
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

export function SecuritySection({ alerts, stats, guardResults, onFilterChange }: {
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
