import type { Request, RequestDetail } from '../types'
import { formatTimestamp, formatTime } from '../lib/format'
import { MetricRow } from './shared'

export function RequestDetailPanel({ detail, onClose }: { detail: RequestDetail; onClose: () => void }) {
  const timestamp = formatTimestamp(detail.timestamp)

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-gray-800 rounded-lg border border-gray-600 w-full max-w-2xl max-h-[90vh] overflow-auto m-4" onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-gray-700">
          <div>
            <h3 className="text-lg font-semibold">Request Details</h3>
            <p className="text-gray-400 text-sm font-mono">{detail.request_id}</p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-white text-2xl">&times;</button>
        </div>

        {/* Content */}
        <div className="p-4 space-y-4">
          {/* Status Banner */}
          <div className={`p-3 rounded ${detail.status === 'success' ? 'bg-green-900/50 border border-green-700' : 'bg-red-900/50 border border-red-700'}`}>
            <div className="flex items-center gap-2">
              <span className={`w-2 h-2 rounded-full ${detail.status === 'success' ? 'bg-green-400' : 'bg-red-400'}`}></span>
              <span className="font-semibold capitalize">{detail.status}</span>
              {detail.error_code && <span className="text-red-400">({detail.error_code})</span>}
            </div>
            {detail.error_message && <p className="text-red-300 mt-1 text-sm">{detail.error_message}</p>}
          </div>

          {/* Basic Info */}
          <div className="grid grid-cols-2 gap-4">
            <div className="bg-gray-900 p-3 rounded">
              <div className="text-gray-400 text-xs uppercase">Model</div>
              <div className="font-mono mt-1">{detail.model}</div>
            </div>
            <div className="bg-gray-900 p-3 rounded">
              <div className="text-gray-400 text-xs uppercase">Endpoint</div>
              <div className="font-mono mt-1">{detail.endpoint}</div>
            </div>
            <div className="bg-gray-900 p-3 rounded">
              <div className="text-gray-400 text-xs uppercase">Task</div>
              <div className="font-mono mt-1">{detail.task}</div>
            </div>
            <div className="bg-gray-900 p-3 rounded">
              <div className="text-gray-400 text-xs uppercase">Timestamp</div>
              <div className="font-mono mt-1 text-sm">{timestamp}</div>
            </div>
          </div>

          {/* Performance Metrics */}
          <div>
            <h4 className="text-sm font-semibold text-gray-300 mb-2">Performance</h4>
            <div className="bg-gray-900 p-3 rounded space-y-1">
              <MetricRow label="Total Latency" value={detail.latency_ms} unit=" ms" />
              <MetricRow label="Time to First Token" value={detail.time_to_first_token_ms} unit=" ms" />
              <MetricRow label="Tokens/Second" value={detail.tokens_per_second} unit=" tok/s" />
            </div>
          </div>

          {/* Token Usage */}
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

          {/* Request Parameters */}
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

          {/* Request/Response Bodies (if available) */}
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

export function RequestRow({ request, onClick }: { request: Request; onClick: () => void }) {
  const time = formatTime(request.timestamp)
  return (
    <tr className="border-b border-gray-700 hover:bg-gray-750 cursor-pointer" onClick={onClick}>
      <td className="py-2 px-3 text-gray-400 text-sm">{time}</td>
      <td className="py-2 px-3">
        <span className={`px-2 py-0.5 rounded text-xs ${request.status === 'success' ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
          {request.status}
        </span>
      </td>
      <td className="py-2 px-3 font-mono text-sm">{request.model}</td>
      <td className="py-2 px-3 text-gray-400 text-sm">{request.endpoint}</td>
      <td className="py-2 px-3 text-right text-sm">
        {request.latency_ms ? `${request.latency_ms.toFixed(0)}ms` : '-'}
      </td>
      <td className="py-2 px-3 text-right text-gray-400 text-sm">
        {request.prompt_tokens + request.completion_tokens}
      </td>
    </tr>
  )
}
