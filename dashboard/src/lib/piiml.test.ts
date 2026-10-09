// ML PII policy (D-052): the dashboard sends only changed categories and
// shows the gateway's own rejection message, never a silent no-op
import { afterEach, describe, expect, it, vi } from 'vitest'
import { updatePIIML } from './api'

afterEach(() => vi.unstubAllGlobals())

describe('updatePIIML', () => {
  it('PUTs only the categories given', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ categories: [] }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('localStorage', { getItem: () => null })
    await updatePIIML({ CREDENTIAL: 'scrub_stored' })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toMatch(/\/api\/pii\/ml$/)
    expect(init.method).toBe('PUT')
    expect(JSON.parse(init.body)).toEqual({ categories: { CREDENTIAL: 'scrub_stored' } })
  })

  it('surfaces the gateway error message', async () => {
    const body = { error: { message: "Unknown categories ['EMAIL']" } }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status: 422 })))
    vi.stubGlobal('localStorage', { getItem: () => null })
    await expect(updatePIIML({ EMAIL: 'detect' } as never)).rejects.toThrow("Unknown categories ['EMAIL']")
  })
})
