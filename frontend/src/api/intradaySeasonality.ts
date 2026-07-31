import { api } from './client'

export interface IntradayBucket {
  bucket: string
  trade_count: number
  avg_pnl: number
  total_pnl: number
  win_rate: number
}

export interface IntradaySeasonalityResult {
  symbol: string
  lookback_days: number
  sample_size: number
  buckets: IntradayBucket[]
  unmatched_count: number
  best_bucket: IntradayBucket | null
  worst_bucket: IntradayBucket | null
  error?: string
}

export async function getIntradaySeasonality(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<IntradaySeasonalityResult> {
  const resp = await api.get('/api/intraday-seasonality/analyze', { params })
  return resp.data
}
