/**
 * Format a duration (in milliseconds) as a compact relative-time label.
 *
 *   < 1s   → '刚刚'
 *   < 60s  → '3s前'
 *   < 60m  → '2m前'
 *   < 24h  → '1h前'
 *   else   → '3d前'
 *
 * Used to surface data freshness ("how long ago did we last hear from the
 * server?") without forcing the reader to do clock arithmetic. Negative or
 * non-finite input is clamped to 0.
 */
export function relativeTime(ageMs: number): string {
  if (!Number.isFinite(ageMs) || ageMs < 0) ageMs = 0
  const seconds = Math.floor(ageMs / 1000)
  if (seconds < 1) return '刚刚'
  if (seconds < 60) return `${seconds}s前`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h前`
  const days = Math.floor(hours / 24)
  return `${days}d前`
}

/** Convenience wrapper: format a whole-second age (e.g. `ageSeconds`). */
export function relativeAgeLabel(ageSeconds: number): string {
  return relativeTime((ageSeconds | 0) * 1000)
}

/**
 * Classify a data age (in seconds) into a freshness bucket for colour coding.
 *
 *   fresh (< 10s) → 'ok'
 *   stale (10–30s) → 'warn'
 *   very stale (> 30s) → 'danger'
 */
export function ageFreshnessClass(ageSeconds: number): 'ok' | 'warn' | 'danger' {
  if (ageSeconds < 10) return 'ok'
  if (ageSeconds <= 30) return 'warn'
  return 'danger'
}
