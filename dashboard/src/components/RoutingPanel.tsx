import { useEffect, useState } from 'react'
import {
  fetchRoutingConfig,
  updateRoutingConfig,
  type RoutingConfig,
  type RoutingModelHome,
  type RoutingTaskPin,
} from '../lib/api'

/** Routing knobs: pin a task type to endpoints ("all embeddings go to
 *  the-mini"), give model patterns a home endpoint, and pick the
 *  load-balancing strategy. Saved on the gateway; survives restarts. */
export function RoutingSection() {
  const [config, setConfig] = useState<RoutingConfig | null>(null)
  const [strategy, setStrategy] = useState<'priority' | 'least_loaded'>('priority')
  const [pins, setPins] = useState<RoutingTaskPin[]>([])
  const [homes, setHomes] = useState<RoutingModelHome[]>([])
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  const load = async () => {
    const data = await fetchRoutingConfig()
    if (data) {
      setConfig(data)
      setStrategy(data.strategy)
      setPins(data.task_endpoints)
      setHomes(data.model_defaults)
      setDirty(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  if (!config) {
    return <div className="text-gray-400 text-sm">Routing config unavailable (admin key required).</div>
  }

  const endpoints = config.available_endpoints
  const unusedTasks = config.available_tasks.filter(t => !pins.some(p => p.task === t))

  const touch = () => {
    setDirty(true)
    setMessage(null)
  }

  const togglePinEndpoint = (pinIdx: number, endpoint: string) => {
    setPins(prev =>
      prev.map((p, i) => {
        if (i !== pinIdx) return p
        const allowed = p.allowed_endpoints.includes(endpoint)
          ? p.allowed_endpoints.filter(e => e !== endpoint)
          : [...p.allowed_endpoints, endpoint]
        return { ...p, allowed_endpoints: allowed }
      })
    )
    touch()
  }

  const save = async () => {
    setSaving(true)
    setMessage(null)
    try {
      const data = await updateRoutingConfig({
        strategy,
        task_endpoints: pins,
        model_defaults: homes.filter(h => h.model.trim() && h.endpoint),
      })
      setConfig(data)
      setPins(data.task_endpoints)
      setHomes(data.model_defaults)
      setDirty(false)
      setMessage('Saved — in effect for the next request.')
    } catch (e) {
      setMessage(`Save failed: ${e instanceof Error ? e.message : e}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">Routing</h2>
          <p className="text-gray-400 text-sm">
            Source: {config.source}
            {config.updated_by ? ` (last change by ${config.updated_by})` : ''} · yaml priority
            order: {config.endpoint_priority.join(' → ') || '—'}
          </p>
        </div>
        <button
          onClick={save}
          disabled={!dirty || saving}
          className={`px-4 py-2 rounded text-sm ${
            dirty ? 'bg-blue-600 hover:bg-blue-700' : 'bg-gray-700 text-gray-400'
          }`}
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>

      {message && (
        <div
          className={`rounded p-3 text-sm ${
            message.startsWith('Save failed')
              ? 'bg-red-900 border border-red-700'
              : 'bg-green-900 border border-green-700'
          }`}
        >
          {message}
        </div>
      )}

      {/* Strategy */}
      <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
        <h3 className="font-medium mb-2">Load balancing</h3>
        <div className="space-y-2 text-sm">
          <label className="flex items-start gap-2">
            <input
              type="radio"
              checked={strategy === 'priority'}
              onChange={() => {
                setStrategy('priority')
                touch()
              }}
            />
            <span>
              <span className="font-medium">Priority order</span>
              <span className="text-gray-400">
                {' '}
                — first endpoint in the list that has the model; overflow only when it's full
              </span>
            </span>
          </label>
          <label className="flex items-start gap-2">
            <input
              type="radio"
              checked={strategy === 'least_loaded'}
              onChange={() => {
                setStrategy('least_loaded')
                touch()
              }}
            />
            <span>
              <span className="font-medium">Least loaded</span>
              <span className="text-gray-400">
                {' '}
                — endpoint with the most free capacity among those that have the model
              </span>
            </span>
          </label>
        </div>
      </div>

      {/* Task pins */}
      <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
        <h3 className="font-medium mb-1">Task pins</h3>
        <p className="text-gray-400 text-sm mb-3">
          Restrict a task type to specific endpoints. Applies on every routing path, fallback
          included — if the pinned endpoint is down, the task fails loudly rather than spilling
          elsewhere.
        </p>
        {pins.map((pin, i) => (
          <div key={pin.task} className="flex flex-wrap items-center gap-3 py-2 border-t border-gray-700">
            <span className="font-mono text-sm w-28">{pin.task}</span>
            {endpoints.map(ep => (
              <label key={ep} className="flex items-center gap-1 text-sm">
                <input
                  type="checkbox"
                  checked={pin.allowed_endpoints.includes(ep)}
                  onChange={() => togglePinEndpoint(i, ep)}
                />
                {ep}
              </label>
            ))}
            <button
              onClick={() => {
                setPins(prev => prev.filter((_, j) => j !== i))
                touch()
              }}
              className="ml-auto text-red-400 hover:text-red-300 text-sm"
            >
              remove
            </button>
          </div>
        ))}
        {unusedTasks.length > 0 && (
          <div className="pt-3">
            <select
              className="bg-gray-900 border border-gray-700 rounded px-2 py-1 text-sm"
              value=""
              onChange={e => {
                if (!e.target.value) return
                setPins(prev => [
                  ...prev,
                  { task: e.target.value, allowed_endpoints: [], denied_endpoints: [] },
                ])
                touch()
              }}
            >
              <option value="">+ pin a task…</option>
              {unusedTasks.map(t => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      {/* Model homes */}
      <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
        <h3 className="font-medium mb-1">Model homes</h3>
        <p className="text-gray-400 text-sm mb-3">
          Preferred endpoint for a model pattern (glob, e.g. <code>qwen3-embedding*</code>). A
          preference, not a pin: other endpoints still serve it under load or failure.
        </p>
        {homes.map((home, i) => (
          <div key={i} className="flex items-center gap-3 py-2 border-t border-gray-700">
            <input
              className="bg-gray-900 border border-gray-700 rounded px-2 py-1 text-sm font-mono w-56"
              value={home.model}
              placeholder="model pattern"
              onChange={e => {
                setHomes(prev => prev.map((h, j) => (j === i ? { ...h, model: e.target.value } : h)))
                touch()
              }}
            />
            <span className="text-gray-500">→</span>
            <select
              className="bg-gray-900 border border-gray-700 rounded px-2 py-1 text-sm"
              value={home.endpoint}
              onChange={e => {
                setHomes(prev =>
                  prev.map((h, j) => (j === i ? { ...h, endpoint: e.target.value } : h))
                )
                touch()
              }}
            >
              {endpoints.map(ep => (
                <option key={ep} value={ep}>
                  {ep}
                </option>
              ))}
            </select>
            <button
              onClick={() => {
                setHomes(prev => prev.filter((_, j) => j !== i))
                touch()
              }}
              className="ml-auto text-red-400 hover:text-red-300 text-sm"
            >
              remove
            </button>
          </div>
        ))}
        <div className="pt-3">
          <button
            onClick={() => {
              setHomes(prev => [...prev, { model: '', endpoint: endpoints[0] ?? '' }])
              touch()
            }}
            className="text-blue-400 hover:text-blue-300 text-sm"
          >
            + add model home
          </button>
        </div>
      </div>
    </div>
  )
}
