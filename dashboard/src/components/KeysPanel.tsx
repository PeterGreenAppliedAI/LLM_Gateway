import { useState } from 'react'
import type { ApiKeyInfo } from '../types'
import { formatTimestamp } from '../lib/format'
import { createApiKey, revokeApiKey } from '../lib/api'

export function ApiKeysSection({ keys, onRefresh }: { keys: ApiKeyInfo[]; onRefresh: () => void }) {
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
