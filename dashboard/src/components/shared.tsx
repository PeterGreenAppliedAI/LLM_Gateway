import type { Endpoint } from '../types'

export function StatCard({ label, value, subtext }: { label: string; value: string | number; subtext?: string }) {
  return (
    <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
      <div className="text-gray-400 text-sm">{label}</div>
      <div className="text-2xl font-bold mt-1">{value}</div>
      {subtext && <div className="text-gray-500 text-xs mt-1">{subtext}</div>}
    </div>
  )
}

export function EndpointCard({ endpoint }: { endpoint: Endpoint }) {
  return (
    <div className={`bg-gray-800 rounded-lg p-4 border ${endpoint.healthy ? 'border-green-600' : 'border-red-600'}`}>
      <div className="flex items-center justify-between">
        <div className="font-semibold">{endpoint.name}</div>
        <div className={`px-2 py-1 rounded text-xs ${endpoint.healthy ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
          {endpoint.healthy ? 'Healthy' : 'Unhealthy'}
        </div>
      </div>
      <div className="text-gray-400 text-sm mt-1">{endpoint.type} - {endpoint.url}</div>
      <div className="text-gray-500 text-xs mt-2">{endpoint.models.length} models</div>
      <div className="flex flex-wrap gap-1 mt-2">
        {endpoint.models.slice(0, 5).map(model => (
          <span key={model} className="bg-gray-700 px-2 py-0.5 rounded text-xs">{model}</span>
        ))}
        {endpoint.models.length > 5 && (
          <span className="text-gray-500 text-xs">+{endpoint.models.length - 5} more</span>
        )}
      </div>
    </div>
  )
}

export function MetricRow({ label, value, unit = '' }: { label: string; value: string | number | null | undefined; unit?: string }) {
  if (value === null || value === undefined) return null
  return (
    <div className="flex justify-between py-1 border-b border-gray-700">
      <span className="text-gray-400">{label}</span>
      <span className="font-mono">{typeof value === 'number' ? value.toFixed(2) : value}{unit}</span>
    </div>
  )
}
