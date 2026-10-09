// Regression for the Security-tab crash (D-057): the server sends
// `total_scans`; reading `total` gave undefined and `.toLocaleString()`
// on it unmounted the whole dashboard
import { describe, expect, it } from 'vitest'
import { toLabelStats } from './api'

describe('toLabelStats', () => {
  it('maps the server wire shape (total_scans)', () => {
    const wire = { total_scans: 713993, labeled: 0, unlabeled: 713993, safe: 0, unsafe: 0, disagreements: 2068 }
    const stats = toLabelStats(wire)
    expect(stats.total).toBe(713993)
    expect(() => stats.total.toLocaleString()).not.toThrow()
  })
  it('yields zeros for a missing or malformed body, never undefined', () => {
    for (const body of [null, {}, { error: { message: 'Admin authentication required' } }]) {
      const s = toLabelStats(body as Record<string, unknown> | null)
      expect(Object.values(s).every(v => typeof v === 'number')).toBe(true)
    }
  })
})
