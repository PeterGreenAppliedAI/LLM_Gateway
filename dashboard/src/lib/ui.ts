export function getSeverityColor(severity: string): string {
  switch (severity) {
    case 'critical': return 'bg-red-900 text-red-300 border-red-700'
    case 'warning': return 'bg-yellow-900 text-yellow-300 border-yellow-700'
    case 'info': return 'bg-blue-900 text-blue-300 border-blue-700'
    default: return 'bg-gray-900 text-gray-300 border-gray-700'
  }
}
