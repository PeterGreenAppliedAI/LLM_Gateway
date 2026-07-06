import { useState } from 'react'
import type { BudgetConfig, BudgetUsage, Catalog } from '../types'
import { createTier, deleteTier, assignModelTier, unassignModelTier } from '../lib/api'

export function TokenBudgetSection({ budgetConfig, budgetUsage, catalog, onRefresh }: {
  budgetConfig: BudgetConfig | null
  budgetUsage: BudgetUsage | null
  catalog: Catalog | null
  onRefresh: () => void
}) {
  const [assignModel, setAssignModel] = useState('')
  const [assignTier, setAssignTier] = useState('')
  const [assigning, setAssigning] = useState(false)
  const [showCreateTier, setShowCreateTier] = useState(false)
  const [newTierName, setNewTierName] = useState('')
  const [newTierMultiplier, setNewTierMultiplier] = useState('1.0')
  const [newTierLimit, setNewTierLimit] = useState('')
  const [creatingTier, setCreatingTier] = useState(false)
  const [showClassifications, setShowClassifications] = useState(false)

  if (!budgetConfig) return null

  // Build model list from catalog (already fetched and working) merged with classification data
  const classificationMap = new Map(budgetConfig.model_classifications.map(m => [m.model, m]))
  const allModels: string[] = []
  if (catalog) {
    for (const ep of catalog.endpoints) {
      for (const model of ep.models) {
        if (!allModels.includes(model)) allModels.push(model)
      }
    }
  }
  // Also include any models from classifications not in catalog
  for (const mc of budgetConfig.model_classifications) {
    if (!allModels.includes(mc.model)) allModels.push(mc.model)
  }
  allModels.sort()

  // Classify using the map, fallback to unclassified
  const allClassified = allModels.map(name => {
    const mc = classificationMap.get(name)
    return mc || { model: name, tier: null, cost_multiplier: budgetConfig.default_cost_multiplier, classified: false }
  })

  const handleAssign = async () => {
    if (!assignModel || !assignTier) return
    setAssigning(true)
    try {
      const result = await assignModelTier(assignModel, assignTier)
      if (result.status === 'success') {
        setAssignModel('')
        setAssignTier('')
        onRefresh()
      }
    } catch (e) {
      console.error('Failed to assign:', e)
    } finally {
      setAssigning(false)
    }
  }

  const handleUnassign = async (model: string) => {
    try {
      await unassignModelTier(model)
      onRefresh()
    } catch (e) {
      console.error('Failed to unassign:', e)
    }
  }

  const unclassified = allClassified.filter(m => !m.classified)
  const classified = allClassified.filter(m => m.classified)

  return (
    <div className="mb-6">
      <h2 className="text-lg font-semibold mb-3 flex items-center gap-2">
        Token Budgets
        {budgetConfig.enabled ? (
          <span className="bg-green-900 text-green-300 px-2 py-0.5 rounded-full text-xs">Enabled</span>
        ) : (
          <span className="bg-gray-700 text-gray-400 px-2 py-0.5 rounded-full text-xs">Disabled</span>
        )}
      </h2>

      {/* Budget Config Overview */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
        <div className="bg-gray-800 rounded-lg p-3 border border-gray-700">
          <div className="text-gray-400 text-xs">Daily Limit</div>
          <div className="text-xl font-bold">{budgetConfig.default_daily_limit.toLocaleString()}</div>
          <div className="text-gray-500 text-xs">tokens/key</div>
        </div>
        <div className="bg-gray-800 rounded-lg p-3 border border-gray-700">
          <div className="text-gray-400 text-xs">Default Multiplier</div>
          <div className="text-xl font-bold">{budgetConfig.default_cost_multiplier}x</div>
          <div className="text-gray-500 text-xs">unclassified models</div>
        </div>
        <div className="bg-gray-800 rounded-lg p-3 border border-gray-700">
          <div className="text-gray-400 text-xs">Tiers</div>
          <div className="text-xl font-bold">{budgetConfig.tiers.length}</div>
        </div>
        <div className="bg-gray-800 rounded-lg p-3 border border-orange-800">
          <div className="text-orange-400 text-xs">Unclassified</div>
          <div className="text-xl font-bold text-orange-400">{unclassified.length}</div>
          <div className="text-gray-500 text-xs">using default rate</div>
        </div>
      </div>

      {/* Tiers */}
      <div className="bg-gray-800 rounded-lg border border-gray-700 p-4 mb-4">
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-sm font-semibold text-gray-300">Cost Tiers</h3>
          <button
            onClick={() => setShowCreateTier(!showCreateTier)}
            className="bg-blue-600 hover:bg-blue-700 px-3 py-1 rounded text-xs"
          >
            {showCreateTier ? 'Cancel' : 'Add Tier'}
          </button>
        </div>
        {showCreateTier && (
          <div className="bg-gray-900 rounded p-3 mb-3 flex gap-2 items-end">
            <div>
              <label className="text-gray-400 text-xs block mb-1">Name</label>
              <input type="text" value={newTierName} onChange={e => setNewTierName(e.target.value)} placeholder="e.g. standard" className="bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm w-32" />
            </div>
            <div>
              <label className="text-gray-400 text-xs block mb-1">Multiplier</label>
              <input type="number" value={newTierMultiplier} onChange={e => setNewTierMultiplier(e.target.value)} step="0.1" min="0" className="bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm w-24" />
            </div>
            <div>
              <label className="text-gray-400 text-xs block mb-1">Daily Limit (optional)</label>
              <input type="number" value={newTierLimit} onChange={e => setNewTierLimit(e.target.value)} placeholder="unlimited" min="0" className="bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm w-32" />
            </div>
            <button
              disabled={creatingTier || !newTierName}
              onClick={async () => {
                setCreatingTier(true)
                try {
                  await createTier(newTierName, parseFloat(newTierMultiplier) || 1.0, newTierLimit ? parseInt(newTierLimit) : undefined)
                  setNewTierName(''); setNewTierMultiplier('1.0'); setNewTierLimit(''); setShowCreateTier(false)
                  onRefresh()
                } catch (e) { console.error('Failed to create tier:', e) }
                finally { setCreatingTier(false) }
              }}
              className="bg-green-600 hover:bg-green-700 disabled:opacity-50 px-3 py-1.5 rounded text-xs whitespace-nowrap"
            >
              {creatingTier ? 'Creating...' : 'Create'}
            </button>
          </div>
        )}
        {budgetConfig.tiers.length > 0 ? (
          <div className="flex flex-wrap gap-3">
            {budgetConfig.tiers.map(tier => (
              <div key={tier.name} className="bg-gray-900 rounded px-3 py-2 border border-gray-700 flex items-start gap-2">
                <div>
                  <div className="font-semibold text-sm">{tier.name}</div>
                  <div className="text-gray-400 text-xs">{tier.cost_multiplier}x multiplier</div>
                  {tier.daily_limit && <div className="text-gray-500 text-xs">{tier.daily_limit.toLocaleString()} daily cap</div>}
                </div>
                <button
                  onClick={async () => {
                    const result = await deleteTier(tier.name)
                    if (result.status === 'error') alert(result.message)
                    else onRefresh()
                  }}
                  className="text-red-400 hover:text-red-300 text-xs mt-0.5"
                  title="Delete tier"
                >&times;</button>
              </div>
            ))}
          </div>
        ) : (
          <div>
            <div className="text-gray-500 text-sm mb-2">No tiers configured yet.</div>
            <button
              onClick={async () => {
                const defaults = [
                  { name: 'frontier', multiplier: 15.0 },
                  { name: 'midrange', multiplier: 3.0 },
                  { name: 'standard', multiplier: 1.0 },
                  { name: 'embedding', multiplier: 0.1 },
                ]
                for (const d of defaults) {
                  await createTier(d.name, d.multiplier)
                }
                onRefresh()
              }}
              className="bg-blue-600 hover:bg-blue-700 px-3 py-1.5 rounded text-xs"
            >
              Create Default Tiers (frontier 15x, midrange 3x, standard 1x, embedding 0.1x)
            </button>
          </div>
        )}
      </div>

      {/* Assign Model to Tier */}
      <div className="bg-gray-800 rounded-lg border border-gray-700 p-4 mb-4">
        <h3 className="text-sm font-semibold text-gray-300 mb-2">Assign Model to Tier</h3>
        <div className="flex gap-2 items-end">
          <div className="flex-1">
            <label className="text-gray-400 text-xs block mb-1">Model</label>
            {allModels.length > 0 ? (
              <select
                value={assignModel}
                onChange={e => setAssignModel(e.target.value)}
                className="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-sm"
              >
                <option value="">Select model...</option>
                {unclassified.length > 0 && <optgroup label="Unclassified">
                  {unclassified.map(m => (
                    <option key={m.model} value={m.model}>{m.model} ({m.cost_multiplier}x)</option>
                  ))}
                </optgroup>}
                {classified.length > 0 && <optgroup label="Classified">
                  {classified.map(m => (
                    <option key={m.model} value={m.model}>{m.model} ({m.tier} - {m.cost_multiplier}x)</option>
                  ))}
                </optgroup>}
              </select>
            ) : (
              <input
                type="text"
                value={assignModel}
                onChange={e => setAssignModel(e.target.value)}
                placeholder="e.g. llama3.2 or llama-*"
                className="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-sm"
              />
            )}
          </div>
          <div className="w-48">
            <label className="text-gray-400 text-xs block mb-1">Tier</label>
            <select
              value={assignTier}
              onChange={e => setAssignTier(e.target.value)}
              className="bg-gray-900 border border-gray-600 rounded px-3 py-2 w-full text-sm"
            >
              <option value="">Select tier...</option>
              {budgetConfig.tiers.map(t => (
                <option key={t.name} value={t.name}>{t.name} ({t.cost_multiplier}x)</option>
              ))}
            </select>
          </div>
          <button
            onClick={handleAssign}
            disabled={assigning || !assignModel || !assignTier}
            className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 px-4 py-2 rounded text-sm whitespace-nowrap"
          >
            {assigning ? 'Assigning...' : 'Assign'}
          </button>
        </div>
      </div>

      {/* Model Classifications Table — Collapsible */}
      <div className="bg-gray-800 rounded-lg border border-gray-700 overflow-hidden mb-4">
        <button
          onClick={() => setShowClassifications(!showClassifications)}
          className="w-full text-left p-3 border-b border-gray-700 flex items-center justify-between hover:bg-gray-750"
        >
          <h3 className="text-sm font-semibold text-gray-300">
            Model Classifications ({allClassified.length} models, {unclassified.length} unclassified)
          </h3>
          <span className="text-gray-400">{showClassifications ? '▼' : '▶'}</span>
        </button>
        {showClassifications && <table className="w-full">
          <thead className="bg-gray-750 border-b border-gray-700">
            <tr className="text-left text-gray-400 text-sm">
              <th className="py-2 px-3">Model</th>
              <th className="py-2 px-3">Tier</th>
              <th className="py-2 px-3 text-right">Multiplier</th>
              <th className="py-2 px-3"></th>
            </tr>
          </thead>
          <tbody>
            {allClassified.map(m => (
              <tr key={m.model} className={`border-b border-gray-700 ${!m.classified ? 'bg-orange-900/10' : ''}`}>
                <td className="py-2 px-3 font-mono text-sm">{m.model}</td>
                <td className="py-2 px-3">
                  {m.classified ? (
                    <span className="px-2 py-0.5 rounded text-xs bg-blue-900 text-blue-300">{m.tier}</span>
                  ) : (
                    <span className="px-2 py-0.5 rounded text-xs bg-orange-900 text-orange-300">unclassified</span>
                  )}
                </td>
                <td className="py-2 px-3 text-right text-sm">{m.cost_multiplier}x</td>
                <td className="py-2 px-3 text-right">
                  {m.classified && (
                    <button
                      onClick={() => handleUnassign(m.model)}
                      className="text-red-400 hover:text-red-300 text-xs"
                    >
                      Remove
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {allClassified.length === 0 && (
              <tr><td colSpan={4} className="py-8 text-center text-gray-500">No models discovered yet</td></tr>
            )}
          </tbody>
        </table>}
      </div>

      {/* Per-Key Usage */}
      {budgetUsage && budgetUsage.enabled && budgetUsage.keys.length > 0 && (
        <div className="bg-gray-800 rounded-lg border border-gray-700 overflow-hidden">
          <h3 className="text-sm font-semibold text-gray-300 p-3 border-b border-gray-700">Per-Key Budget Usage</h3>
          <table className="w-full">
            <thead className="bg-gray-750 border-b border-gray-700">
              <tr className="text-left text-gray-400 text-sm">
                <th className="py-2 px-3">Key</th>
                <th className="py-2 px-3 text-right">Used</th>
                <th className="py-2 px-3 text-right">Remaining</th>
                <th className="py-2 px-3 text-right">Limit</th>
                <th className="py-2 px-3">Usage</th>
              </tr>
            </thead>
            <tbody>
              {budgetUsage.keys.map(k => {
                const pct = k.daily_limit > 0 ? (k.tokens_used / k.daily_limit) * 100 : 0
                return (
                  <tr key={k.key} className="border-b border-gray-700">
                    <td className="py-2 px-3 font-mono text-sm">{k.key.slice(0, 12)}...</td>
                    <td className="py-2 px-3 text-right text-sm">{k.tokens_used.toLocaleString()}</td>
                    <td className="py-2 px-3 text-right text-sm">{k.tokens_remaining.toLocaleString()}</td>
                    <td className="py-2 px-3 text-right text-sm">{k.daily_limit.toLocaleString()}</td>
                    <td className="py-2 px-3 w-32">
                      <div className="bg-gray-700 rounded-full h-2">
                        <div
                          className={`h-2 rounded-full ${pct > 90 ? 'bg-red-500' : pct > 70 ? 'bg-yellow-500' : 'bg-green-500'}`}
                          style={{ width: `${Math.min(pct, 100)}%` }}
                        />
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
