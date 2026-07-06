import { useCallback, useEffect, useState } from 'react'
import type { SecurityScan, LabelStats } from '../types'
import { formatTime } from '../lib/format'
import { fetchSecurityScans, labelScan, bulkLabelScans, fetchLabelStats, exportTrainingData } from '../lib/api'

export function SecurityScansSection({ onRefresh }: { onRefresh: () => void }) {
  const [expanded, setExpanded] = useState(false)
  const [scans, setScans] = useState<SecurityScan[]>([])
  const [labelStats, setLabelStats] = useState<LabelStats | null>(null)
  const [filter, setFilter] = useState<'all' | 'unlabeled' | 'disagreements'>('unlabeled')
  const [selectedScans, setSelectedScans] = useState<Set<string>>(new Set())
  const [expandedScan, setExpandedScan] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [exportFormat, setExportFormat] = useState<'llama_guard' | 'raw'>('llama_guard')
  const [exportResult, setExportResult] = useState<{ count: number } | null>(null)

  const loadScans = useCallback(async () => {
    setLoading(true)
    try {
      const [scansData, statsData] = await Promise.all([
        fetchSecurityScans({
          limit: 50,
          unlabeled_only: filter === 'unlabeled',
          disagreements_only: filter === 'disagreements',
        }),
        fetchLabelStats(),
      ])
      setScans(scansData.scans)
      setLabelStats(statsData)
    } catch (e) {
      console.error('Failed to load scans:', e)
    } finally {
      setLoading(false)
    }
  }, [filter])

  useEffect(() => {
    loadScans()
  }, [loadScans])

  const handleLabel = async (requestId: string, label: string, category?: string) => {
    await labelScan(requestId, label, category)
    setSelectedScans(prev => { const next = new Set(prev); next.delete(requestId); return next })
    loadScans()
    onRefresh()
  }

  const handleBulkLabel = async (label: string) => {
    if (selectedScans.size === 0) return
    await bulkLabelScans(Array.from(selectedScans), label)
    setSelectedScans(new Set())
    loadScans()
    onRefresh()
  }

  const toggleSelect = (id: string) => {
    setSelectedScans(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleSelectAll = () => {
    if (selectedScans.size === scans.length) {
      setSelectedScans(new Set())
    } else {
      setSelectedScans(new Set(scans.map(s => s.request_id)))
    }
  }

  const handleExport = async () => {
    try {
      const result = await exportTrainingData(exportFormat)
      setExportResult({ count: result.count })
      // Trigger download
      const blob = new Blob([JSON.stringify(result.examples, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `training-data-${exportFormat}-${new Date().toISOString().slice(0, 10)}.json`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      console.error('Export failed:', e)
    }
  }

  const progressPct = labelStats && labelStats.total > 0 ? (labelStats.labeled / labelStats.total) * 100 : 0

  return (
    <div className="mb-6">
      <button
        onClick={() => { setExpanded(!expanded); if (!expanded && scans.length === 0) loadScans() }}
        className="w-full text-left flex items-center justify-between mb-3"
      >
        <h2 className="text-lg font-semibold flex items-center gap-2">
          Security Scan Labeling
          {labelStats && <span className="text-gray-400 text-sm font-normal">({labelStats.total} scans, {labelStats.unlabeled} unlabeled)</span>}
        </h2>
        <span className="text-gray-400">{expanded ? '▼' : '▶'}</span>
      </button>

      {!expanded ? null : <>
      {/* Label Stats */}
      {labelStats && (
        <div className="grid grid-cols-2 md:grid-cols-6 gap-3 mb-4">
          <div className="bg-gray-800 rounded-lg p-3 border border-gray-700">
            <div className="text-gray-400 text-xs">Total Scans</div>
            <div className="text-xl font-bold">{labelStats.total}</div>
          </div>
          <div className="bg-gray-800 rounded-lg p-3 border border-green-800">
            <div className="text-green-400 text-xs">Labeled</div>
            <div className="text-xl font-bold text-green-400">{labelStats.labeled}</div>
          </div>
          <div className="bg-gray-800 rounded-lg p-3 border border-gray-700">
            <div className="text-gray-400 text-xs">Unlabeled</div>
            <div className="text-xl font-bold">{labelStats.unlabeled}</div>
          </div>
          <div className="bg-gray-800 rounded-lg p-3 border border-gray-700">
            <div className="text-gray-400 text-xs">Safe</div>
            <div className="text-xl font-bold text-green-400">{labelStats.safe}</div>
          </div>
          <div className="bg-gray-800 rounded-lg p-3 border border-gray-700">
            <div className="text-gray-400 text-xs">Unsafe</div>
            <div className="text-xl font-bold text-red-400">{labelStats.unsafe}</div>
          </div>
          <div className="bg-gray-800 rounded-lg p-3 border border-yellow-800">
            <div className="text-yellow-400 text-xs">Disagreements</div>
            <div className="text-xl font-bold text-yellow-400">{labelStats.disagreements}</div>
          </div>
        </div>
      )}

      {/* Progress Bar */}
      {labelStats && labelStats.total > 0 && (
        <div className="bg-gray-800 rounded-lg p-3 border border-gray-700 mb-4">
          <div className="flex justify-between text-sm mb-1">
            <span className="text-gray-400">Labeling Progress</span>
            <span className="text-gray-300">{progressPct.toFixed(1)}% ({labelStats.labeled}/{labelStats.total})</span>
          </div>
          <div className="bg-gray-700 rounded-full h-3">
            <div className="bg-green-500 h-3 rounded-full transition-all" style={{ width: `${progressPct}%` }} />
          </div>
        </div>
      )}

      {/* Filter + Bulk Actions + Export */}
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <div className="flex gap-2">
          {(['all', 'unlabeled', 'disagreements'] as const).map(f => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-3 py-1.5 rounded text-sm capitalize ${filter === f ? 'bg-blue-600 text-white' : 'bg-gray-800 text-gray-400 hover:text-white'}`}
            >
              {f === 'all' ? 'All Scans' : f === 'unlabeled' ? 'Unlabeled' : 'Disagreements'}
            </button>
          ))}
        </div>
        <div className="flex gap-2 items-center">
          {selectedScans.size > 0 && (
            <>
              <span className="text-gray-400 text-sm">{selectedScans.size} selected</span>
              <button onClick={() => handleBulkLabel('safe')} className="bg-green-700 hover:bg-green-600 px-3 py-1.5 rounded text-xs">Mark Safe</button>
              <button onClick={() => handleBulkLabel('unsafe')} className="bg-red-700 hover:bg-red-600 px-3 py-1.5 rounded text-xs">Mark Unsafe</button>
            </>
          )}
          <div className="border-l border-gray-600 pl-2 flex gap-2 items-center">
            <select value={exportFormat} onChange={e => setExportFormat(e.target.value as 'llama_guard' | 'raw')} className="bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-xs">
              <option value="llama_guard">Llama Guard Format</option>
              <option value="raw">Raw Format</option>
            </select>
            <button onClick={handleExport} className="bg-purple-700 hover:bg-purple-600 px-3 py-1.5 rounded text-xs">Export Training Data</button>
            {exportResult && <span className="text-green-400 text-xs">{exportResult.count} examples exported</span>}
          </div>
        </div>
      </div>

      {/* Scans Table */}
      <div className="bg-gray-800 rounded-lg border border-gray-700 overflow-hidden">
        {loading ? (
          <div className="p-8 text-center text-gray-500">Loading scans...</div>
        ) : scans.length === 0 ? (
          <div className="p-8 text-center text-gray-500">
            {filter === 'unlabeled' ? 'No unlabeled scans' : filter === 'disagreements' ? 'No disagreements found' : 'No security scans yet'}
          </div>
        ) : (
          <>
            <table className="w-full">
              <thead className="bg-gray-750 border-b border-gray-700">
                <tr className="text-left text-gray-400 text-sm">
                  <th className="py-2 px-3 w-8">
                    <input type="checkbox" checked={selectedScans.size === scans.length && scans.length > 0} onChange={toggleSelectAll} className="rounded" />
                  </th>
                  <th className="py-2 px-3">Time</th>
                  <th className="py-2 px-3 max-w-md">Message</th>
                  <th className="py-2 px-3">Regex</th>
                  <th className="py-2 px-3">Guard</th>
                  <th className="py-2 px-3">Label</th>
                  <th className="py-2 px-3">Actions</th>
                </tr>
              </thead>
              <tbody>
                {scans.map(scan => (
                  <>
                    <tr key={scan.request_id} className={`border-b border-gray-700 cursor-pointer hover:bg-gray-750 ${scan.is_disagreement ? 'bg-yellow-900/10' : ''}`}>
                      <td className="py-2 px-3" onClick={e => e.stopPropagation()}>
                        <input type="checkbox" checked={selectedScans.has(scan.request_id)} onChange={() => toggleSelect(scan.request_id)} className="rounded" />
                      </td>
                      <td className="py-2 px-3 text-sm text-gray-400" onClick={() => setExpandedScan(expandedScan === scan.request_id ? null : scan.request_id)}>
                        {scan.timestamp ? formatTime(scan.timestamp) : '-'}
                      </td>
                      <td className="py-2 px-3 text-sm max-w-md" onClick={() => setExpandedScan(expandedScan === scan.request_id ? null : scan.request_id)}>
                        <div className="truncate text-gray-300" title={scan.messages.filter(m => m.role === 'user').map(m => m.content).join(' | ') || scan.messages.map(m => m.content).join(' | ')}>
                          {(() => {
                            const userMsgs = scan.messages.filter(m => m.role === 'user');
                            const preview = userMsgs.length > 0 ? userMsgs.map(m => m.content).join(' | ') : scan.messages.map(m => `[${m.role}] ${m.content}`).join(' | ');
                            return preview || <span className="text-gray-500 italic">no content</span>;
                          })()}
                        </div>
                      </td>
                      <td className="py-2 px-3" onClick={() => setExpandedScan(expandedScan === scan.request_id ? null : scan.request_id)}>
                        <span className={`px-2 py-0.5 rounded text-xs ${scan.regex_threat_level !== 'none' ? 'bg-red-900 text-red-300' : 'bg-green-900 text-green-300'}`}>
                          {scan.regex_threat_level}
                        </span>
                        {scan.is_disagreement && <span className="ml-1 px-1 py-0.5 rounded text-xs bg-yellow-900 text-yellow-300">!</span>}
                      </td>
                      <td className="py-2 px-3" onClick={() => setExpandedScan(expandedScan === scan.request_id ? null : scan.request_id)}>
                        {scan.guard_safe === null ? (
                          <span className="text-gray-500 text-xs">-</span>
                        ) : (
                          <span className={`px-2 py-0.5 rounded text-xs ${scan.guard_safe ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
                            {scan.guard_safe ? 'safe' : 'unsafe'}
                          </span>
                        )}
                      </td>
                      <td className="py-2 px-3">
                        {scan.label ? (
                          <span className={`px-2 py-0.5 rounded text-xs ${scan.label === 'safe' ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
                            {scan.label}{scan.label_category ? ` (${scan.label_category})` : ''}
                          </span>
                        ) : (
                          <span className="text-gray-500 text-xs">unlabeled</span>
                        )}
                      </td>
                      <td className="py-2 px-3">
                        {!scan.label && (
                          <div className="flex gap-1">
                            <button onClick={(e) => { e.stopPropagation(); handleLabel(scan.request_id, 'safe') }} className="bg-green-800 hover:bg-green-700 px-2 py-0.5 rounded text-xs">Safe</button>
                            <button onClick={(e) => { e.stopPropagation(); handleLabel(scan.request_id, 'unsafe') }} className="bg-red-800 hover:bg-red-700 px-2 py-0.5 rounded text-xs">Unsafe</button>
                          </div>
                        )}
                      </td>
                    </tr>
                    {expandedScan === scan.request_id && (
                      <tr key={`${scan.request_id}-detail`} className="border-b border-gray-700">
                        <td colSpan={7} className="p-3 bg-gray-900">
                          <div className="text-xs font-mono space-y-2">
                            <div><strong className="text-gray-400">Request ID:</strong> {scan.request_id}</div>
                            {scan.model && <div><strong className="text-gray-400">Model:</strong> {scan.model}</div>}
                            <div>
                              <strong className="text-gray-400">Messages:</strong>
                              <div className="mt-1 space-y-1">
                                {scan.messages.map((msg, i) => (
                                  <div key={i} className="bg-gray-800 p-2 rounded">
                                    <span className={`font-bold ${msg.role === 'user' ? 'text-blue-400' : msg.role === 'system' ? 'text-yellow-400' : 'text-green-400'}`}>{msg.role}: </span>
                                    <span className="text-gray-300 whitespace-pre-wrap">{msg.content}</span>
                                  </div>
                                ))}
                              </div>
                            </div>
                            {scan.label_notes && <div><strong className="text-gray-400">Notes:</strong> {scan.label_notes}</div>}
                          </div>
                        </td>
                      </tr>
                    )}
                  </>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
      </>}
    </div>
  )
}
