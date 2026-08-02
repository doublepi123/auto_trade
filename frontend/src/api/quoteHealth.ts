import { api } from './client'
import type { QuoteStreamHealth } from '../types'

/**
 * Result of the read-only quote-stream health probe.
 *
 * The backend returns the same schema-validated `QuoteStreamHealth` body with
 * HTTP 200 (live tracker snapshot) and with HTTP 503 (no runner/tracker yet,
 * `status === "unavailable"`). The 503 is NOT an error — it distinguishes "no
 * runtime yet" from a known disconnected stream, so the HTTP status is
 * preserved here for truthful presentation. Viewing never subscribes,
 * reconnects, resets, polls the broker, or constructs a runner.
 */
export interface QuoteStreamHealthResult {
  http_status: 200 | 503
  health: QuoteStreamHealth
}

export async function getQuoteStreamHealth(): Promise<QuoteStreamHealthResult> {
  const resp = await api.get<QuoteStreamHealth>('/api/quote-health', {
    // Both 200 and 503 carry a schema-validated QuoteStreamHealth body.
    validateStatus: (status) => status === 200 || status === 503,
  })
  return { http_status: resp.status as 200 | 503, health: resp.data }
}
