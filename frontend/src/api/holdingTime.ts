import { api } from './client'
import type { StatisticsQuality } from '../types'

export interface HoldingBucket {
  bucket: string
  trade_count: number
  total_pnl: number
  avg_pnl: number
  win_rate: number
}

export interface HoldingTimeResult {
  symbol: string
  lookback_days: number
  sample_size: number
  avg_holding_seconds: number
  median_holding_seconds: number
  buckets: HoldingBucket[]
  best_bucket: HoldingBucket | null
  statistics_quality: StatisticsQuality
  error?: string
}

export async function getHoldingTimeAnalysis(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<HoldingTimeResult> {
  const resp = await api.get('/api/holding-time/analyze', { params })
  return resp.data
}
