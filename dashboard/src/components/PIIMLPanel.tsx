import { useEffect, useState } from 'react'
import type { PIIMLAction, PIIMLView } from '../types'
import { formatTimestamp } from '../lib/format'
import { CollapsibleSection, StatCard } from './shared'
import { fetchPIIML, updatePIIML } from '../lib/api'

const pct = (v: number | null | undefined) => (v === null || v === undefined ? '—' : `${v}%`)
const ms = (v: number | null | undefined) => (v === null || v === undefined ? '—' : `${Math.round(v)} ms`)

/** ML PII detection (D-052): gate health, measured miss rate, per-category policy. */
export function PIIMLSection() {
  const [view, setView] = useState<PIIMLView | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [collapsed, setCollapsed] = useState(true)
  const [draft, setDraft] = useState<Record<string, PIIMLAction>>({})
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const reload = () =>
    fetchPIIML().then(v => {
      setView(v)
      setLoaded(true)
    })

  useEffect(() => {
    reload()
    const interval = setInterval(reload, 15000)
    return () => clearInterval(interval)
  }, [])

  if (!loaded || !view) {
    return (
      <CollapsibleSection id="pii-ml" title="ML PII Detection" summary={loaded ? 'data unavailable' : 'loading'}
        open={!collapsed} onToggle={() => setCollapsed(!collapsed)}>
        <p className="text-gray-400 text-sm">ML PII status couldn't be loaded. Check the admin key.</p>
      </CollapsibleSection>
    )
  }

  const s = view.summary
  const current = Object.fromEntries(view.categories.map(c => [c.label, c.action]))
  const changed = Object.fromEntries(Object.entries(draft).filter(([label, action]) => current[label] !== action))
  const dirty = Object.keys(changed).length > 0
  const scrubCount = view.categories.filter(c => (draft[c.label] ?? c.action) === 'scrub_stored').length

  const summary = !view.enabled
    ? 'gate not running'
    : s && s.requests
      ? `${s.requests} analysed · miss rate ${pct(s.gate_miss_rate_pct)} · ${scrubCount} scrubbing`
      : `running · no traffic yet · ${scrubCount} scrubbing`

  const save = async () => {
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      setView(await updatePIIML(changed))
      setDraft({})
      setSaved(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <CollapsibleSection id="pii-ml" title="ML PII Detection" summary={summary}
      open={!collapsed} onToggle={() => setCollapsed(!collapsed)}>
      {!view.enabled ? (
        <div className="text-sm text-orange-300">
          The Laya gate isn't running. Start the sidecar on the Mac (<code>tools/laya_pii_sidecar</code>), set{' '}
          <code>GATEWAY_PII_GATE_ENABLED=true</code> and <code>GATEWAY_PII_GATE_URL</code>, and restart the gateway.
          Category settings below are saved now and apply once it runs.
        </div>
      ) : (
        <div className="text-xs text-gray-500">
          Gate {view.gate_url} · extractor {view.finder_model} · flags at ≥{view.threshold} · samples{' '}
          {Math.round(view.sample_rate * 100)}% of clean texts to measure misses
        </div>
      )}

      {s && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <StatCard label={`Analysed (${s.period_hours}h)`} value={s.requests} />
          <StatCard label="Gate miss rate" value={pct(s.gate_miss_rate_pct)}
            subtext={`${s.gate_missed} of ${s.sampled} sampled clean texts had PII`} />
          <StatCard label="Extractor skipped" value={pct(s.skipped_finder_pct)} subtext="share the gate let through" />
          <StatCard label="Latency" value={ms(s.avg_gate_ms)}
            subtext={`gate · extractor ${ms(s.avg_finder_ms)}${s.gate_errors ? ` · ${s.gate_errors} gate errors` : ''}`} />
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="border-b border-gray-700">
            <tr className="text-left text-gray-400">
              <th className="py-2 px-3">Category</th>
              <th className="py-2 px-3 text-right" title="Texts the gate flagged for this category">Gate flagged</th>
              <th className="py-2 px-3 text-right" title="Texts where the extractor found a value">Found</th>
              <th className="py-2 px-3 text-right" title="Found in texts the gate called clean (sampled)">Missed by gate</th>
              <th className="py-2 px-3 text-right">Scrubbed</th>
              <th className="py-2 px-3">Policy</th>
            </tr>
          </thead>
          <tbody>
            {view.categories.map(c => {
              const action = draft[c.label] ?? c.action
              return (
                <tr key={c.label} className="border-b border-gray-700">
                  <td className="py-2 px-3" title={c.includes}>
                    <div className="font-mono text-xs">{c.label}</div>
                    <div className="text-gray-500 text-xs">{c.name}</div>
                  </td>
                  <td className="py-2 px-3 text-right">{c.gate_flagged ?? 0}</td>
                  <td className="py-2 px-3 text-right">{c.found ?? 0}</td>
                  <td className={`py-2 px-3 text-right ${c.found_in_sampled ? 'text-orange-300' : ''}`}>{c.found_in_sampled ?? 0}</td>
                  <td className="py-2 px-3 text-right">{c.scrubbed ?? 0}</td>
                  <td className="py-2 px-3">
                    <select
                      aria-label={`Policy for ${c.label}`}
                      className="bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs"
                      value={action}
                      disabled={saving}
                      onChange={e => { setDraft({ ...draft, [c.label]: e.target.value as PIIMLAction }); setSaved(false) }}
                    >
                      <option value="detect">Detect only</option>
                      <option value="scrub_stored">Scrub stored copies</option>
                    </select>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <div className="text-xs text-gray-500">
        <b>Scrub stored copies</b> replaces values found in that category with <code>[CATEGORY]</code> in the
        request's stored audit bodies and scan messages, shortly after the request. It never changes what the
        model receives (that's the PII Scrubbing switch). Values the gate misses aren't scrubbed: watch the miss
        rate before relying on it.
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <button
          className="px-3 py-1.5 rounded text-sm bg-blue-600 hover:bg-blue-500 disabled:bg-gray-700 disabled:text-gray-500"
          disabled={!dirty || saving}
          onClick={save}
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
        {saved && !dirty && <span className="text-xs text-green-400">Saved · applies to newly analysed requests</span>}
        {error && <span className="text-xs text-red-400">{error}</span>}
        <span className="text-xs text-gray-500 ml-auto">
          {view.source === 'dashboard'
            ? `Set by ${view.updated_by ?? 'unknown'}${view.updated_at ? ` · ${formatTimestamp(view.updated_at)}` : ''}`
            : 'Default: every category detect-only'}
        </span>
      </div>
      {!view.persisted && (
        <div className="text-xs text-orange-300">No database: changes apply now but revert on restart.</div>
      )}
    </CollapsibleSection>
  )
}
