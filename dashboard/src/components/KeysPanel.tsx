import { useRef, useState } from 'react'
import type { ApiKeyInfo } from '../types'
import { formatTimestamp } from '../lib/format'
import { API_BASE, createApiKey, revokeApiKey } from '../lib/api'
import { parseModelPatterns, parseNumberField, scopeSummary } from '../lib/forms'

export function ApiKeysSection({
  keys,
  endpoints,
  onRefresh,
}: {
  keys: ApiKeyInfo[]
  endpoints: string[]
  onRefresh: () => void
}) {
  const [showCreate, setShowCreate] = useState(false)
  const [newKeyName, setNewKeyName] = useState('')
  const [newKeyClientId, setNewKeyClientId] = useState('')
  const [newKeyDescription, setNewKeyDescription] = useState('')
  const [newKeyRpm, setNewKeyRpm] = useState('')
  const [newKeyConcurrency, setNewKeyConcurrency] = useState('')
  const [newKeyPriority, setNewKeyPriority] = useState<'interactive' | 'batch'>('interactive')
  const [newKeyModels, setNewKeyModels] = useState('')
  const [newKeyEndpoints, setNewKeyEndpoints] = useState<string[]>([])
  const [created, setCreated] = useState<{ key: string; clientId: string } | null>(null)
  const [creating, setCreating] = useState(false)
  const [copied, setCopied] = useState<'key' | 'example' | null>(null)
  // Failures are shown inline and the draft is kept (D-057); they used to
  // go only to console.error, leaving the form looking unchanged
  const [formError, setFormError] = useState<string | null>(null)
  const [revokeError, setRevokeError] = useState<string | null>(null)
  const rpmRef = useRef<HTMLInputElement>(null)
  const concurrencyRef = useRef<HTMLInputElement>(null)

  const handleCreate = async () => {
    if (!newKeyName || !newKeyClientId) return
    const rpm = parseNumberField(newKeyRpm, { min: 1, integer: true, label: 'Requests / minute' })
    if (rpm.error) {
      setFormError(rpm.error)
      rpmRef.current?.focus()
      return
    }
    const concurrency = parseNumberField(newKeyConcurrency, {
      min: 1,
      max: 10000,
      integer: true,
      label: 'Max concurrent',
    })
    if (concurrency.error) {
      setFormError(concurrency.error)
      concurrencyRef.current?.focus()
      return
    }
    setCreating(true)
    setFormError(null)
    try {
      const models = parseModelPatterns(newKeyModels)
      const result = await createApiKey({
        name: newKeyName,
        client_id: newKeyClientId,
        description: newKeyDescription || undefined,
        rate_limit_rpm: rpm.value,
        max_concurrent: concurrency.value,
        priority: newKeyPriority,
        allowed_models: models.length ? models : undefined,
        allowed_endpoints: newKeyEndpoints.length ? newKeyEndpoints : undefined,
      })
      setCreated({ key: result.key, clientId: newKeyClientId })
      setNewKeyName('')
      setNewKeyClientId('')
      setNewKeyDescription('')
      setNewKeyRpm('')
      setNewKeyConcurrency('')
      setNewKeyPriority('interactive')
      setNewKeyModels('')
      setNewKeyEndpoints([])
      onRefresh()
    } catch (e) {
      setFormError(e instanceof Error ? e.message : String(e))
    } finally {
      setCreating(false)
    }
  }

  const handleRevoke = async (keyId: number) => {
    setRevokeError(null)
    try {
      await revokeApiKey(keyId)
      onRefresh()
    } catch (e) {
      setRevokeError(e instanceof Error ? e.message : String(e))
    }
  }

  const copy = (what: 'key' | 'example', text: string) => {
    navigator.clipboard.writeText(text)
    setCopied(what)
    setTimeout(() => setCopied(null), 2000)
  }

  const example = created
    ? `curl ${API_BASE}/v1/chat/completions \\\n  -H "Authorization: Bearer ${created.key}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model": "<model>", "max_tokens": 256, "messages": [{"role": "user", "content": "hello"}]}'`
    : ''

  const toggleEndpoint = (ep: string) =>
    setNewKeyEndpoints(prev => (prev.includes(ep) ? prev.filter(e => e !== ep) : [...prev, ep]))

  const inputClass = 'bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-sm'

  return (
    <div className="mb-6">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold">API Keys</h2>
        <button
          onClick={() => {
            setShowCreate(!showCreate)
            setCreated(null)
            setFormError(null)
          }}
          className="bg-blue-600 hover:bg-blue-700 px-3 py-1.5 rounded text-sm"
        >
          {showCreate ? 'Cancel' : 'Create Key'}
        </button>
      </div>

      {revokeError && (
        <div className="bg-red-900 border border-red-700 rounded p-2 mb-3 text-sm" role="alert">
          Revoke failed: {revokeError}
        </div>
      )}

      {showCreate && (
        <div className="bg-gray-800 rounded-lg p-4 border border-gray-700 mb-4 text-left">
          {created ? (
            <div>
              <div className="text-green-400 font-semibold mb-2">Key created for {created.clientId}</div>
              <p className="text-yellow-400 text-sm mb-3">Copy this key now — it will not be shown again.</p>
              <div className="flex items-center gap-2 mb-3">
                <code className="bg-gray-900 px-3 py-2 rounded font-mono text-sm flex-1 break-all">
                  {created.key}
                </code>
                <button
                  onClick={() => copy('key', created.key)}
                  className="bg-gray-700 hover:bg-gray-600 px-3 py-2 rounded text-sm whitespace-nowrap"
                >
                  {copied === 'key' ? 'Copied!' : 'Copy key'}
                </button>
              </div>
              <p className="text-gray-400 text-sm mb-1">Integration example (OpenAI-compatible):</p>
              <div className="flex items-start gap-2 mb-3">
                <pre className="bg-gray-900 px-3 py-2 rounded font-mono text-xs flex-1 overflow-x-auto whitespace-pre">
                  {example}
                </pre>
                <button
                  onClick={() => copy('example', example)}
                  className="bg-gray-700 hover:bg-gray-600 px-3 py-2 rounded text-sm whitespace-nowrap"
                >
                  {copied === 'example' ? 'Copied!' : 'Copy'}
                </button>
              </div>
              <button
                onClick={() => {
                  setCreated(null)
                  setShowCreate(false)
                }}
                className="text-gray-400 hover:text-white text-sm"
              >
                Done
              </button>
            </div>
          ) : (
            <div className="space-y-3">
              {formError && (
                <div className="bg-red-900 border border-red-700 rounded p-2 text-sm" role="alert">
                  {formError}
                </div>
              )}
              <div>
                <label htmlFor="key-name" className="text-gray-400 text-sm block mb-1">Name</label>
                <input id="key-name" type="text" value={newKeyName} onChange={e => setNewKeyName(e.target.value)}
                  placeholder="e.g. my-app-key" className={inputClass} />
              </div>
              <div>
                <label htmlFor="key-client" className="text-gray-400 text-sm block mb-1">Client ID</label>
                <input id="key-client" type="text" value={newKeyClientId} onChange={e => setNewKeyClientId(e.target.value)}
                  placeholder="e.g. my-app" className={inputClass} />
              </div>
              <div>
                <label htmlFor="key-desc" className="text-gray-400 text-sm block mb-1">Description (optional)</label>
                <input id="key-desc" type="text" value={newKeyDescription} onChange={e => setNewKeyDescription(e.target.value)}
                  placeholder="What is this key for?" className={inputClass} />
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label htmlFor="key-rpm" className="text-gray-400 text-sm block mb-1">Requests / minute (optional)</label>
                  <input id="key-rpm" ref={rpmRef} type="number" min={1} step={1} value={newKeyRpm}
                    onChange={e => setNewKeyRpm(e.target.value)} placeholder="gateway default" className={inputClass}
                    aria-describedby="key-rpm-help" />
                  <p id="key-rpm-help" className="text-gray-500 text-xs mt-1">Burst and hourly limits scale with it.</p>
                </div>
                <div>
                  <label htmlFor="key-conc" className="text-gray-400 text-sm block mb-1">Max concurrent (optional)</label>
                  <input id="key-conc" ref={concurrencyRef} type="number" min={1} max={10000} step={1} value={newKeyConcurrency}
                    onChange={e => setNewKeyConcurrency(e.target.value)} placeholder="unlimited" className={inputClass}
                    aria-describedby="key-conc-help" />
                  <p id="key-conc-help" className="text-gray-500 text-xs mt-1">Requests in flight at once; more get 429.</p>
                </div>
              </div>
              <div>
                <label htmlFor="key-models" className="text-gray-400 text-sm block mb-1">Allowed models (optional)</label>
                <input id="key-models" type="text" value={newKeyModels} onChange={e => setNewKeyModels(e.target.value)}
                  placeholder="all models — or globs, e.g. qwen3*, phi4:14b" className={inputClass}
                  aria-describedby="key-models-help" />
                <p id="key-models-help" className="text-gray-500 text-xs mt-1">
                  Comma-separated patterns. Empty allows every model.
                </p>
              </div>
              <fieldset>
                <legend className="text-gray-400 text-sm mb-1">Allowed endpoints (optional)</legend>
                <div className="flex flex-wrap gap-3">
                  {endpoints.map(ep => (
                    <label key={ep} className="flex items-center gap-1 text-sm">
                      <input type="checkbox" checked={newKeyEndpoints.includes(ep)} onChange={() => toggleEndpoint(ep)} />
                      {ep}
                    </label>
                  ))}
                  {endpoints.length === 0 && <span className="text-gray-500 text-sm">No endpoints loaded</span>}
                </div>
                <p className="text-gray-500 text-xs mt-1">
                  {newKeyEndpoints.length ? `Restricted to ${newKeyEndpoints.join(', ')}` : 'None selected: all endpoints.'}
                </p>
              </fieldset>
              <div>
                <span id="key-priority-label" className="text-gray-400 text-sm block mb-1">Priority</span>
                <div className="flex gap-2" role="radiogroup" aria-labelledby="key-priority-label">
                  {(['interactive', 'batch'] as const).map(p => (
                    <button key={p} type="button" role="radio" aria-checked={newKeyPriority === p}
                      onClick={() => setNewKeyPriority(p)}
                      className={`px-3 py-1.5 rounded text-sm border ${newKeyPriority === p ? 'bg-blue-600 border-blue-500' : 'bg-gray-900 border-gray-600 hover:border-gray-400'}`}>
                      {p === 'interactive' ? 'Interactive' : 'Batch'}
                    </button>
                  ))}
                </div>
                <p className="text-gray-500 text-xs mt-1">
                  {newKeyPriority === 'batch'
                    ? 'Waits behind interactive traffic, queues longer, and uses at most part of each endpoint.'
                    : 'Served first when endpoints are busy.'}
                </p>
              </div>
              <button onClick={handleCreate} disabled={creating || !newKeyName || !newKeyClientId}
                className="bg-green-600 hover:bg-green-700 disabled:opacity-50 px-4 py-2 rounded text-sm">
                {creating ? 'Creating...' : 'Generate Key'}
              </button>
            </div>
          )}
        </div>
      )}

      <div className="bg-gray-800 rounded-lg border border-gray-700 overflow-x-auto">
        <table className="w-full">
          <thead className="bg-gray-750 border-b border-gray-700">
            <tr className="text-left text-gray-400 text-sm">
              <th className="py-2 px-3">Prefix</th>
              <th className="py-2 px-3">Name</th>
              <th className="py-2 px-3">Client ID</th>
              <th className="py-2 px-3">Scope</th>
              <th className="py-2 px-3">Limits</th>
              <th className="py-2 px-3">Created</th>
              <th className="py-2 px-3">Last Used</th>
              <th className="py-2 px-3">Status</th>
              <th className="py-2 px-3"><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {keys.map(k => (
              <tr key={k.id} className="border-b border-gray-700 text-left">
                <td className="py-2 px-3 font-mono text-sm">{k.prefix}...</td>
                <td className="py-2 px-3 text-sm">{k.name}</td>
                <td className="py-2 px-3 text-gray-400 text-sm">{k.client_id}</td>
                <td className="py-2 px-3 text-gray-400 text-sm">{scopeSummary(k)}</td>
                <td className="py-2 px-3 text-gray-400 text-sm">{keyLimits(k)}</td>
                <td className="py-2 px-3 text-gray-400 text-sm">{k.created_at ? formatTimestamp(k.created_at) : '-'}</td>
                <td className="py-2 px-3 text-gray-400 text-sm">{k.last_used_at ? formatTimestamp(k.last_used_at) : 'Never'}</td>
                <td className="py-2 px-3">
                  <span className={`px-2 py-0.5 rounded text-xs ${k.is_active ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
                    {k.is_active ? 'Active' : 'Revoked'}
                  </span>
                </td>
                <td className="py-2 px-3">
                  {k.is_active && (
                    <button onClick={() => handleRevoke(k.id)} className="text-red-400 hover:text-red-300 text-sm"
                      aria-label={`Revoke key ${k.name}`}>
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {keys.length === 0 && (
              <tr>
                <td colSpan={9} className="py-8 text-center text-gray-500">No API keys created yet</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function keyLimits(k: ApiKeyInfo): string {
  const parts: string[] = []
  if (k.rate_limit_rpm) parts.push(`${k.rate_limit_rpm} rpm`)
  if (k.max_concurrent) parts.push(`${k.max_concurrent} concurrent`)
  if (k.priority === 'batch') parts.push('batch')
  return parts.length ? parts.join(' · ') : 'default'
}
