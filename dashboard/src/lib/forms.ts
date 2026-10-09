/** Form-value helpers shared by the dashboard panels (D-057). */

import type { ApiKeyInfo } from '../types'

export type NumberField = { value: number | undefined; error: string | null }

/**
 * Parse an optional numeric form field without truthiness fallbacks.
 *
 * `parseFloat(x) || 1` turned a deliberate 0 into 1 — a zero cost multiplier
 * silently saved as 1x. Empty means "not set" (undefined); anything else
 * must parse and satisfy the bounds, or it's an error shown to the operator.
 */
export function parseNumberField(
  raw: string,
  opts: { min?: number; max?: number; integer?: boolean; required?: boolean; label?: string } = {},
): NumberField {
  const label = opts.label ?? 'Value'
  const text = raw.trim()
  if (text === '') {
    return opts.required
      ? { value: undefined, error: `${label} is required` }
      : { value: undefined, error: null }
  }
  const value = Number(text)
  if (!Number.isFinite(value)) return { value: undefined, error: `${label} must be a number` }
  if (opts.integer && !Number.isInteger(value)) {
    return { value: undefined, error: `${label} must be a whole number` }
  }
  if (opts.min !== undefined && value < opts.min) {
    return { value: undefined, error: `${label} must be at least ${opts.min}` }
  }
  if (opts.max !== undefined && value > opts.max) {
    return { value: undefined, error: `${label} must be at most ${opts.max}` }
  }
  return { value, error: null }
}

/**
 * The message an operator should see for a failed gateway call.
 *
 * Handles the gateway's error envelope ({"error": {"message"}}), FastAPI
 * validation lists ({"detail": [{"loc", "msg"}]}), and `{status: "error",
 * message}` bodies from older mutation routes.
 */
export function gatewayErrorMessage(status: number, body: unknown): string {
  if (body && typeof body === 'object') {
    const b = body as Record<string, unknown>
    const err = b.error as Record<string, unknown> | undefined
    if (err && typeof err.message === 'string') return err.message
    if (Array.isArray(b.detail)) {
      const parts = b.detail.map(d => {
        const item = d as { loc?: unknown[]; msg?: string }
        const field = Array.isArray(item.loc) ? String(item.loc[item.loc.length - 1]) : ''
        return field && item.msg ? `${field}: ${item.msg}` : item.msg ?? ''
      })
      const text = parts.filter(Boolean).join('; ')
      if (text) return text
    }
    if (typeof b.detail === 'string') return b.detail
    if (b.status === 'error' && typeof b.message === 'string') return b.message
  }
  return `Request failed (HTTP ${status})`
}

/** Split a comma/newline-separated list of model globs; empty means "all". */
export function parseModelPatterns(raw: string): string[] {
  return raw
    .split(/[,\n]/)
    .map(s => s.trim())
    .filter(Boolean)
}

export function scopeSummary(k: Pick<ApiKeyInfo, 'allowed_models' | 'allowed_endpoints'>): string {
  const models = k.allowed_models?.length ? k.allowed_models.join(', ') : 'all models'
  const endpoints = k.allowed_endpoints?.length ? k.allowed_endpoints.join(', ') : 'all endpoints'
  return `${models} · ${endpoints}`
}
