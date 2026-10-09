import { useEffect, useState } from 'react'
import type { PIIStats, PIIEvent, PIIConfig } from '../types'
import { formatTime, formatTimestamp } from '../lib/format'
import { CollapsibleSection, StatCard } from './shared'
import { fetchPIIStats, fetchPIIEvents, fetchPIIConfig, updatePIIConfig } from '../lib/api'

/** Scrubbing policy editor. Detection itself is set by environment variable. */
export function PIIScrubSettings() {
  const [config, setConfig] = useState<PIIConfig | null>(null)
  const [enabled, setEnabled] = useState(false)
  const [allRoutes, setAllRoutes] = useState(true)
  const [routes, setRoutes] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const load = (c: PIIConfig | null) => {
    setConfig(c)
    if (!c) return
    setEnabled(c.scrub_enabled)
    setAllRoutes(c.scrub_routes.length === 0)
    setRoutes(c.scrub_routes)
  }

  useEffect(() => {
    fetchPIIConfig().then(load)
  }, [])

  if (!config) return null

  const draftRoutes = allRoutes ? [] : routes
  const dirty =
    enabled !== config.scrub_enabled ||
    draftRoutes.length !== config.scrub_routes.length ||
    draftRoutes.some(r => !config.scrub_routes.includes(r))
  const noRouteSelected = enabled && !allRoutes && routes.length === 0
  const locked = !config.detection_enabled || saving

  const toggleRoute = (route: string) =>
    setRoutes(routes.includes(route) ? routes.filter(r => r !== route) : [...routes, route])

  const save = async () => {
    if (config.scrub_enabled && !enabled &&
        !window.confirm('Turn PII scrubbing off? Models will receive detected PII unmodified.')) {
      return
    }
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      load(await updatePIIConfig({ scrub_enabled: enabled, scrub_routes: draftRoutes }))
      setSaved(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="bg-gray-900 rounded border border-gray-700 p-4 space-y-3 text-left">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <h3 className="font-semibold">PII Scrubbing</h3>
          <span className={`px-2 py-0.5 rounded text-xs font-bold ${config.scrub_enabled ? 'bg-green-900 text-green-300' : 'bg-orange-900 text-orange-300'}`}>
            {config.scrub_enabled ? (config.scrub_routes.length ? `ON · ${config.scrub_routes.length} routes` : 'ON · all routes') : 'OFF · flag only'}
          </span>
        </div>
        <span className="text-xs text-gray-500">
          {config.source === 'dashboard'
            ? `Set from dashboard by ${config.updated_by ?? 'unknown'}${config.updated_at ? ` · ${formatTimestamp(config.updated_at)}` : ''}`
            : 'From environment (GATEWAY_PII_SCRUB_*)'}
        </span>
      </div>

      {!config.detection_enabled ? (
        <div className="text-sm text-orange-300">
          PII detection is off. Set <code>GATEWAY_PII_ENABLED=true</code> and restart the gateway to configure scrubbing.
        </div>
      ) : (
        <>
          <label className="flex items-start gap-2 text-sm cursor-pointer">
            <input type="checkbox" className="mt-1" checked={enabled} disabled={locked}
              onChange={e => { setEnabled(e.target.checked); setSaved(false) }} />
            <span>
              Replace detected PII with placeholders (<code>[EMAIL]</code>, <code>[SSN]</code>, …) before it reaches the model
              <span className="block text-xs text-gray-500">
                Off = flag only: PII is detected and logged as hashes, and the model receives the original text.
                Stored request bodies are redacted either way.
              </span>
            </span>
          </label>

          {enabled && (
            <div className="pl-6 space-y-2 text-sm">
              <div className="flex gap-4">
                <label className="flex items-center gap-1 cursor-pointer">
                  <input type="radio" checked={allRoutes} disabled={locked}
                    onChange={() => { setAllRoutes(true); setSaved(false) }} /> All routes
                </label>
                <label className="flex items-center gap-1 cursor-pointer">
                  <input type="radio" checked={!allRoutes} disabled={locked}
                    onChange={() => { setAllRoutes(false); setSaved(false) }} /> Selected routes
                </label>
              </div>
              {!allRoutes && (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-1">
                  {config.available_routes.map(route => (
                    <label key={route} className="flex items-center gap-2 cursor-pointer font-mono text-xs">
                      <input type="checkbox" checked={routes.includes(route)} disabled={locked}
                        onChange={() => { toggleRoute(route); setSaved(false) }} />
                      {route}
                    </label>
                  ))}
                </div>
              )}
              {noRouteSelected && <div className="text-xs text-orange-300">Select at least one route.</div>}
            </div>
          )}

          <div className="flex items-center gap-3">
            <button
              className="px-3 py-1.5 rounded text-sm bg-blue-600 hover:bg-blue-500 disabled:bg-gray-700 disabled:text-gray-500"
              disabled={!dirty || noRouteSelected || locked}
              onClick={save}
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
            {saved && !dirty && <span className="text-xs text-green-400">Saved · applies to new requests now</span>}
            {error && <span className="text-xs text-red-400">{error}</span>}
          </div>
          {!config.persisted && (
            <div className="text-xs text-orange-300">
              No database: changes apply now but revert to the environment setting on restart.
            </div>
          )}
        </>
      )}
    </div>
  )
}

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

  // Unavailable data keeps the section in place (it used to vanish) so the
  // tab's layout doesn't change shape with the data
  if (!stats) {
    return (
      <CollapsibleSection id="pii" title="PII Detection & Scrubbing" summary="data unavailable"
        open={!collapsed} onToggle={() => setCollapsed(!collapsed)}>
        <p className="text-gray-400 text-sm">PII statistics couldn't be loaded. Check the admin key, or whether PII detection is enabled on the gateway.</p>
      </CollapsibleSection>
    )
  }

  const piiTypes = Object.keys(stats.by_type)

  return (
    <CollapsibleSection
      id="pii"
      title="PII Detection & Scrubbing"
      summary={`${stats.total_detections} detections, ${stats.unique_values} unique values`}
      open={!collapsed}
      onToggle={() => setCollapsed(!collapsed)}
    >
          <PIIScrubSettings />

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
    </CollapsibleSection>
  )
}
