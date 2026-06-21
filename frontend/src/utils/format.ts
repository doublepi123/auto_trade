/**
 * Format a number as currency, prefixing with the correct symbol for the market.
 *
 * - HK markets use HK$ (HKD).
 * - US (and unknown) markets use $ (USD).
 *
 * Negative values are wrapped in `-` to keep the caller in control of sign formatting
 * (e.g. `signedCurrency` can prepend a `+` / `-`). When `value` is null/undefined we
 * return `-` to avoid throwing inside a template binding.
 */
export function formatCurrency(value: number | null | undefined, market?: string | null): string {
  if (value == null) return '-'
  const prefix = market && market.toUpperCase() === 'HK' ? 'HK$' : '$'
  const amount = Math.abs(value)
  const sign = value < 0 ? '-' : ''
  return `${sign}${prefix}${amount.toFixed(2)}`
}

/**
 * Derive the market suffix from a `symbol.MARKET` string. Returns the suffix
 * (e.g. `HK` for `0700.HK`) or `null` if the symbol has no recognisable suffix.
 */
export function marketFromSymbol(symbol?: string | null): string | null {
  if (!symbol) return null
  const idx = symbol.lastIndexOf('.')
  if (idx === -1 || idx === symbol.length - 1) return null
  return symbol.slice(idx + 1).toUpperCase()
}

/**
 * Fixed(2) numeric string. null/undefined → '0.00'. Consolidates the per-view
 * `formatNumber` helpers that used to be duplicated in Dashboard.
 */
export function formatNumber(value: number | null | undefined): string {
  return (value ?? 0).toFixed(2)
}

/**
 * Signed currency with an explicit `+` / `-` prefix (zero shows no sign), so a
 * positive PnL reads unambiguously as `+$12.34`. Market-aware via
 * {@link formatCurrency} (HK → HK$). Defaults to USD when no market is given.
 */
export function signedCurrency(value: number | null | undefined, market?: string | null): string {
  const normalized = value ?? 0
  const body = formatCurrency(normalized, market)
  if (normalized > 0) return `+${body}`
  return body
}

/**
 * Signed percentage with an explicit `+` / `-` prefix, e.g. `+1.20%` / `-0.50%`.
 */
export function signedPercent(value: number | null | undefined): string {
  const normalized = value ?? 0
  const amount = Math.abs(normalized).toFixed(2)
  if (normalized > 0) return `+${amount}%`
  if (normalized < 0) return `-${amount}%`
  return `${amount}%`
}

/** Unsigned percentage at the given digit precision (default 2). */
export function formatPercent(value: number | null | undefined, digits = 2): string {
  return `${(value ?? 0).toFixed(digits)}%`
}

/** Sign-prefixed plain number (no currency / percent), e.g. `+3.50`. */
export function formatSigned(value: number | null | undefined, digits = 2): string {
  const normalized = value ?? 0
  const amount = Math.abs(normalized).toFixed(digits)
  if (normalized > 0) return `+${amount}`
  if (normalized < 0) return `-${amount}`
  return amount
}

/**
 * Compact large numbers for tight UI: 1234 → '1.2k', 1.5e6 → '1.5M', 3e9 →
 * '3.0B'. Smaller numbers render with no suffix. Sign is preserved.
 */
export function formatCompact(value: number | null | undefined): string {
  const n = value ?? 0
  const abs = Math.abs(n)
  const sign = n < 0 ? '-' : ''
  if (abs >= 1e9) return `${sign}${(abs / 1e9).toFixed(1)}B`
  if (abs >= 1e6) return `${sign}${(abs / 1e6).toFixed(1)}M`
  if (abs >= 1e3) return `${sign}${(abs / 1e3).toFixed(1)}k`
  return `${sign}${abs.toFixed(0)}`
}
