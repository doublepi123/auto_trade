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
