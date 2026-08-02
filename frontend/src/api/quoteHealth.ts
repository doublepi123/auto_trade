import { api } from './client'
import type { QuoteStreamHealth } from '../types'

/**
 * Result of the read-only quote-stream health probe.
 *
 * The backend returns the same schema-validated `QuoteStreamHealth` body with
 * HTTP 200 (live tracker snapshot) and with HTTP 503 (no runner/tracker yet,
 * `status === "unavailable"`). A validated 503 is NOT an error — it
 * distinguishes "no runtime yet" from a known disconnected stream, so the
 * HTTP status is preserved here for truthful presentation. Viewing never
 * subscribes, reconnects, resets, polls the broker, or constructs a runner.
 */
export interface QuoteStreamHealthResult {
  http_status: 200 | 503
  health: QuoteStreamHealth
}

/**
 * Runtime guard for the HTTP 503 unavailable contract. A generic or proxy
 * 503 body (HTML error page, `{detail: ...}`, partial payload) must NOT be
 * cast into the typed unavailable state: the special "runner not ready"
 * explanation is reserved for a body that carries the full schema shape AND
 * the contract-mandated `status === "unavailable"`.
 */
function isTypedUnavailableHealth(data: unknown): data is QuoteStreamHealth {
  if (!data || typeof data !== 'object') return false
  const h = data as Record<string, unknown>
  return (
    h.status === 'unavailable' &&
    typeof h.symbol === 'string' &&
    typeof h.quotes_received === 'number' &&
    (h.last_quote_timestamp === null || typeof h.last_quote_timestamp === 'string') &&
    (h.last_quote_age_seconds === null || typeof h.last_quote_age_seconds === 'number') &&
    typeof h.max_gap_seconds === 'number' &&
    typeof h.disconnect_count === 'number' &&
    typeof h.resubscribe_count === 'number' &&
    typeof h.disconnect_retry_count === 'number' &&
    typeof h.quotes_subscribed === 'boolean' &&
    typeof h.as_of === 'string'
  )
}

export async function getQuoteStreamHealth(): Promise<QuoteStreamHealthResult> {
  const resp = await api.get<QuoteStreamHealth>('/api/quote-health', {
    // Both 200 and 503 can carry a schema-validated QuoteStreamHealth body.
    validateStatus: (status) => status === 200 || status === 503,
  })
  if (resp.status === 503 && !isTypedUnavailableHealth(resp.data)) {
    // A 503 we cannot validate is an ordinary failure, not "runner not ready".
    throw new Error('行情流健康响应无法识别')
  }
  return { http_status: resp.status as 200 | 503, health: resp.data }
}
