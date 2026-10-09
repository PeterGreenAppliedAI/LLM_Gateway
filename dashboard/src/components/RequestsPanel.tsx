import { useCallback, useEffect, useRef, useState } from 'react'
import type { Request, RequestDetail } from '../types'
import { formatTimestamp, formatTime } from '../lib/format'
import { fetchRequestDetail, fetchRequests, type RequestFilters } from '../lib/api'
import { MetricRow } from './shared'

const PAGE_SIZE = 50

function statusClass(status: string): string {
  if (status === 'success') return 'bg-green-900 text-green-300'
  if (status === 'denied') return 'bg-amber-900 text-amber-300'
  return 'bg-red-900 text-red-300'
}

/** Request detail as a modal dialog: titled, focus-managed, Escape closes,
 *  focus returns to whatever opened it (D-057). */
export function RequestDetailPanel({ detail, onClose }: { detail: RequestDetail; onClose: () => void }) {
  const timestamp = formatTimestamp(detail.timestamp)
  const closeRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    closeRef.current?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      opener?.focus?.()
    }
  }, [onClose])

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="request-detail-title"
        className="bg-gray-800 rounded-lg border border-gray-600 w-full max-w-2xl max-h-[90vh] overflow-auto m-2 sm:m-4 text-left"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-2 p-4 border-b border-gray-700">
          <div className="min-w-0">
            <h3 id="request-detail-title" className="text-lg font-semibold">Request details</h3>
            <p className="text-gray-400 text-sm font-mono break-all">{detail.request_id}</p>
          </div>
          <button
            ref={closeRef}
            onClick={onClose}
            className="text-gray-300 hover:text-white border border-gray-600 rounded px-3 py-1 text-sm"
          >
            Close
          </button>
        </div>

        <div className="p-4 space-y-4">
          <div className={`p-3 rounded border ${detail.status === 'success' ? 'bg-green-900/50 border-green-700' : detail.status === 'denied' ? 'bg-amber-900/50 border-amber-700' : 'bg-red-900/50 border-red-700'}`}>
            <div className="flex items-center gap-2">
              <span className="font-semibold capitalize">{detail.status}</span>
              {detail.error_code && <span className="text-red-300">({detail.error_code})</span>}
            </div>
            {detail.error_message && <p className="text-red-200 mt-1 text-sm break-words">{detail.error_message}</p>}
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {([
              ['Model', detail.model],
              ['Endpoint', detail.endpoint],
              ['Task', detail.task],
              ['Timestamp', timestamp],
            ] as const).map(([label, value]) => (
              <div key={label} className="bg-gray-900 p-3 rounded">
                <div className="text-gray-400 text-xs uppercase">{label}</div>
                <div className="font-mono mt-1 text-sm break-all">{value}</div>
              </div>
            ))}
          </div>

          <div>
            <h4 className="text-sm font-semibold text-gray-300 mb-2">Performance</h4>
            <div className="bg-gray-900 p-3 rounded space-y-1">
              <MetricRow label="Total Latency" value={detail.latency_ms} unit=" ms" />
              <MetricRow label="Time to First Token" value={detail.time_to_first_token_ms} unit=" ms" />
              <MetricRow label="Tokens/Second" value={detail.tokens_per_second} unit=" tok/s" />
            </div>
          </div>

          <div>
            <h4 className="text-sm font-semibold text-gray-300 mb-2">Token Usage</h4>
            <div className="bg-gray-900 p-3 rounded">
              <div className="grid grid-cols-3 gap-4 text-center">
                <div>
                  <div className="text-2xl font-bold text-blue-400">{detail.prompt_tokens}</div>
                  <div className="text-gray-400 text-xs">Prompt</div>
                </div>
                <div>
                  <div className="text-2xl font-bold text-green-400">{detail.completion_tokens}</div>
                  <div className="text-gray-400 text-xs">Completion</div>
                </div>
                <div>
                  <div className="text-2xl font-bold text-purple-400">{detail.total_tokens}</div>
                  <div className="text-gray-400 text-xs">Total</div>
                </div>
              </div>
              {detail.estimated_cost_usd !== null && detail.estimated_cost_usd > 0 && (
                <div className="mt-3 pt-3 border-t border-gray-700 text-center">
                  <span className="text-gray-400">Estimated Cost: </span>
                  <span className="text-yellow-400 font-mono">${detail.estimated_cost_usd.toFixed(4)}</span>
                </div>
              )}
            </div>
          </div>

          <div>
            <h4 className="text-sm font-semibold text-gray-300 mb-2">Parameters</h4>
            <div className="bg-gray-900 p-3 rounded space-y-1">
              <MetricRow label="Stream" value={detail.stream ? 'Yes' : 'No'} />
              <MetricRow label="Max Tokens" value={detail.max_tokens} />
              <MetricRow label="Temperature" value={detail.temperature} />
              <MetricRow label="Client ID" value={detail.client_id} />
              {detail.user_id && <MetricRow label="User ID" value={detail.user_id} />}
              {detail.environment && <MetricRow label="Environment" value={detail.environment} />}
            </div>
          </div>

          {detail.request_body && (
            <div>
              <h4 className="text-sm font-semibold text-gray-300 mb-2">Request Body</h4>
              <pre className="bg-gray-900 p-3 rounded text-xs overflow-auto max-h-40">
                {typeof detail.request_body === 'string' ? detail.request_body : JSON.stringify(detail.request_body, null, 2)}
              </pre>
            </div>
          )}
          {detail.response_body && (
            <div>
              <h4 className="text-sm font-semibold text-gray-300 mb-2">Response Body</h4>
              <pre className="bg-gray-900 p-3 rounded text-xs overflow-auto max-h-40">
                {typeof detail.response_body === 'string' ? detail.response_body : JSON.stringify(detail.response_body, null, 2)}
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/** A request row. The whole row is clickable for mouse users; the named
 *  Details button is the keyboard and screen-reader path (D-057). */
export function RequestRow({ request, onOpen }: { request: Request; onOpen: () => void }) {
  return (
    <tr className="border-b border-gray-700 hover:bg-gray-750 cursor-pointer text-left" onClick={onOpen}>
      <td className="py-2 px-3 text-gray-400 text-sm whitespace-nowrap">{formatTime(request.timestamp)}</td>
      <td className="py-2 px-3">
        <span className={`px-2 py-0.5 rounded text-xs ${statusClass(request.status)}`}>{request.status}</span>
      </td>
      <td className="py-2 px-3 text-gray-400 text-sm">{request.client_id}</td>
      <td className="py-2 px-3 font-mono text-sm">{request.model}</td>
      <td className="py-2 px-3 text-gray-400 text-sm">{request.endpoint}</td>
      <td className="py-2 px-3 text-right text-sm">{request.latency_ms ? `${request.latency_ms.toFixed(0)}ms` : '-'}</td>
      <td className="py-2 px-3 text-right text-gray-400 text-sm">{request.prompt_tokens + request.completion_tokens}</td>
      <td className="py-2 px-3 text-right">
        <button
          onClick={e => {
            e.stopPropagation()
            onOpen()
          }}
          className="text-blue-400 hover:text-blue-300 text-sm"
          aria-label={`Details for request ${request.request_id}`}
        >
          Details
        </button>
      </td>
    </tr>
  )
}

/** Requests tab: filters, paging, request-ID lookup, and a shareable
 *  #request=<id> link. Before D-057 it showed the latest 50 rows only, so
 *  an incident older than that was unreachable from the UI. */
export function RequestsSection({ refreshTick }: { refreshTick: number }) {
  const [filters, setFilters] = useState<RequestFilters>({ status: '', client: '', hours: '' })
  const [offset, setOffset] = useState(0)
  const [rows, setRows] = useState<Request[]>([])
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lookup, setLookup] = useState('')
  const [detail, setDetail] = useState<RequestDetail | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const page = await fetchRequests({ ...filters, limit: PAGE_SIZE, offset })
      setRows(page.requests)
      setHasMore(page.has_more)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [filters, offset])

  // Reload on filter/page change; follow the dashboard poll only on page 1,
  // so browsing older pages doesn't shift rows underneath the operator
  const lastTick = useRef(refreshTick)
  useEffect(() => {
    const tickChanged = refreshTick !== lastTick.current
    lastTick.current = refreshTick
    if (tickChanged && offset !== 0) return
    load()
  }, [load, refreshTick, offset])

  const open = useCallback(async (requestId: string) => {
    setError(null)
    try {
      setDetail(await fetchRequestDetail(requestId))
      window.history.replaceState(null, '', `#request=${encodeURIComponent(requestId)}`)
    } catch (e) {
      setError(`Request ${requestId}: ${e instanceof Error ? e.message : String(e)}`)
    }
  }, [])

  const close = useCallback(() => {
    setDetail(null)
    window.history.replaceState(null, '', window.location.pathname + window.location.search)
  }, [])

  // Deep link: #request=<id> opens that request on load
  useEffect(() => {
    const match = window.location.hash.match(/^#request=(.+)$/)
    if (match) open(decodeURIComponent(match[1]))
  }, [open])

  const setFilter = (key: keyof RequestFilters, value: string) => {
    setFilters(prev => ({ ...prev, [key]: value }))
    setOffset(0)
  }

  const page = Math.floor(offset / PAGE_SIZE) + 1
  const inputClass = 'bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm'

  return (
    <div className="text-left">
      {detail && <RequestDetailPanel detail={detail} onClose={close} />}

      <h2 className="text-lg font-semibold mb-3">Requests</h2>

      <form
        className="flex flex-wrap items-end gap-3 mb-3"
        onSubmit={e => {
          e.preventDefault()
          if (lookup.trim()) open(lookup.trim())
        }}
      >
        <div>
          <label htmlFor="req-status" className="text-gray-400 text-xs block mb-1">Status</label>
          <select id="req-status" className={inputClass} value={filters.status} onChange={e => setFilter('status', e.target.value)}>
            <option value="">All</option>
            <option value="success">Success</option>
            <option value="error">Error</option>
            <option value="denied">Denied</option>
          </select>
        </div>
        <div>
          <label htmlFor="req-client" className="text-gray-400 text-xs block mb-1">Client ID</label>
          <input id="req-client" className={`${inputClass} w-36`} value={filters.client}
            onChange={e => setFilter('client', e.target.value)} placeholder="any" />
        </div>
        <div>
          <label htmlFor="req-hours" className="text-gray-400 text-xs block mb-1">Time range</label>
          <select id="req-hours" className={inputClass} value={filters.hours} onChange={e => setFilter('hours', e.target.value)}>
            <option value="">All time</option>
            <option value="1">Last hour</option>
            <option value="24">Last 24 hours</option>
            <option value="168">Last 7 days</option>
          </select>
        </div>
        <div className="flex items-end gap-2">
          <div>
            <label htmlFor="req-lookup" className="text-gray-400 text-xs block mb-1">Request ID</label>
            <input id="req-lookup" className={`${inputClass} w-64 font-mono`} value={lookup}
              onChange={e => setLookup(e.target.value)} placeholder="paste an ID" />
          </div>
          <button type="submit" disabled={!lookup.trim()}
            className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 px-3 py-1.5 rounded text-sm">
            Open
          </button>
        </div>
      </form>

      {error && (
        <div className="bg-red-900 border border-red-700 rounded p-2 mb-3 text-sm" role="alert">{error}</div>
      )}

      <div className="bg-gray-800 rounded-lg border border-gray-700 overflow-x-auto">
        <table className="w-full">
          <thead className="bg-gray-750 border-b border-gray-700">
            <tr className="text-left text-gray-400 text-sm">
              <th className="py-2 px-3">Time</th>
              <th className="py-2 px-3">Status</th>
              <th className="py-2 px-3">Client</th>
              <th className="py-2 px-3">Model</th>
              <th className="py-2 px-3">Endpoint</th>
              <th className="py-2 px-3 text-right">Latency</th>
              <th className="py-2 px-3 text-right">Tokens</th>
              <th className="py-2 px-3"><span className="sr-only">Details</span></th>
            </tr>
          </thead>
          <tbody>
            {rows.map(req => (
              <RequestRow key={req.id} request={req} onOpen={() => open(req.request_id)} />
            ))}
            {rows.length === 0 && !loading && (
              <tr>
                <td colSpan={8} className="py-8 text-center text-gray-500">No requests match these filters</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between mt-3 text-sm" aria-live="polite">
        <span className="text-gray-400">
          {loading ? 'Loading…' : rows.length ? `Page ${page} · rows ${offset + 1}–${offset + rows.length}${hasMore ? ' · more available' : ' · end of results'}` : ''}
        </span>
        <div className="flex gap-2">
          <button onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} disabled={offset === 0 || loading}
            className="border border-gray-600 rounded px-3 py-1 disabled:opacity-40">
            Newer
          </button>
          <button onClick={() => setOffset(offset + PAGE_SIZE)} disabled={!hasMore || loading}
            className="border border-gray-600 rounded px-3 py-1 disabled:opacity-40">
            Older
          </button>
        </div>
      </div>
    </div>
  )
}
