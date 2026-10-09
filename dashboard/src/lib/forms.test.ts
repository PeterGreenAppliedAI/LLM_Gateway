// Regression tests for the 2026-10-09 operator-experience review (D-057)
import { describe, expect, it } from 'vitest'
import { gatewayErrorMessage, parseModelPatterns, parseNumberField, scopeSummary } from './forms'

describe('parseNumberField (U2: zero must survive)', () => {
  it('keeps a deliberate zero instead of falling back', () => {
    // The bug: parseFloat("0") || 1.0 saved a 0x tier as 1x
    expect(parseNumberField('0', { min: 0, required: true })).toEqual({ value: 0, error: null })
  })
  it('treats empty as unset, or as an error when required', () => {
    expect(parseNumberField('')).toEqual({ value: undefined, error: null })
    expect(parseNumberField('  ', { required: true, label: 'Multiplier' }).error).toBe('Multiplier is required')
  })
  it('rejects values below the minimum (U1: -1 rpm)', () => {
    expect(parseNumberField('-1', { min: 1, label: 'Requests / minute' }).error).toBe(
      'Requests / minute must be at least 1',
    )
  })
  it('rejects non-numbers and non-integers where integers are required', () => {
    expect(parseNumberField('abc').error).toMatch(/must be a number/)
    expect(parseNumberField('2.5', { integer: true }).error).toMatch(/whole number/)
  })
  it('accepts fractional multipliers', () => {
    expect(parseNumberField('0.1', { min: 0 })).toEqual({ value: 0.1, error: null })
  })
})

describe('gatewayErrorMessage (U1: show the server reason)', () => {
  it('reads FastAPI validation lists, naming the field', () => {
    const body = { detail: [{ loc: ['body', 'rate_limit_rpm'], msg: 'Input should be greater than or equal to 1' }] }
    expect(gatewayErrorMessage(422, body)).toBe('rate_limit_rpm: Input should be greater than or equal to 1')
  })
  it('reads the gateway error envelope', () => {
    expect(gatewayErrorMessage(403, { error: { code: 'x', message: 'Admin key required' } })).toBe('Admin key required')
  })
  it('reads {status: "error"} mutation bodies', () => {
    expect(gatewayErrorMessage(200, { status: 'error', message: 'Tier in use' })).toBe('Tier in use')
  })
  it('falls back to the HTTP status for unknown bodies', () => {
    expect(gatewayErrorMessage(500, null)).toBe('Request failed (HTTP 500)')
  })
})

describe('key scopes (U5)', () => {
  it('parses comma and newline separated model patterns', () => {
    expect(parseModelPatterns('qwen3*, phi4:14b\n gemma4:*,')).toEqual(['qwen3*', 'phi4:14b', 'gemma4:*'])
    expect(parseModelPatterns('  ')).toEqual([])
  })
  it('summarizes unrestricted keys explicitly', () => {
    expect(scopeSummary({ allowed_models: null, allowed_endpoints: null })).toBe('all models · all endpoints')
    expect(scopeSummary({ allowed_models: ['qwen3*'], allowed_endpoints: ['the-mini'] })).toBe('qwen3* · the-mini')
  })
})
