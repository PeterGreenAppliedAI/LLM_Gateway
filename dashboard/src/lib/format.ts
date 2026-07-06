// Helper to format UTC timestamp to local time
export function formatTimestamp(utcTimestamp: string): string {
  // Handle various timestamp formats
  // - Already has Z suffix: use as-is
  // - Has timezone offset (+00:00): use as-is
  // - No timezone info: append Z to treat as UTC
  let timestamp = utcTimestamp
  if (!utcTimestamp.endsWith('Z') && !utcTimestamp.match(/[+-]\d{2}:\d{2}$/)) {
    timestamp = utcTimestamp + 'Z'
  }
  return new Date(timestamp).toLocaleString()
}

export function formatTime(utcTimestamp: string): string {
  let timestamp = utcTimestamp
  if (!utcTimestamp.endsWith('Z') && !utcTimestamp.match(/[+-]\d{2}:\d{2}$/)) {
    timestamp = utcTimestamp + 'Z'
  }
  return new Date(timestamp).toLocaleTimeString()
}
