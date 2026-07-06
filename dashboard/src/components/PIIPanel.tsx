import { useEffect, useState } from 'react'
import type { PIIStats, PIIEvent } from '../types'
import { formatTime } from '../lib/format'
import { StatCard } from './shared'
import { fetchPIIStats, fetchPIIEvents } from '../lib/api'

export function PIISection() {
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
