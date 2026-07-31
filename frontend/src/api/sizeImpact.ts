import { api } from './client'
import type { StatisticsQuality } from '../types'

export interface SizeQuartile {
  quartile: string
  trade_count: number
  avg_entry_notional: number
  total_pnl: number
  avg_pnl: number
  win_rate: number
  avg_return_pct: number
}

export interface SizeImpactResult {
  symbol: string
  lookback_days: number
  sample_size: number
  quartiles: SizeQuartile[]
  size_efficiency_trend: string
  assessment: string
  statistics_quality: StatisticsQuality
  error?: string
}

export async function getSizeImpact(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<SizeImpactResult> {
  const resp = await api.get('/api/size-impact/analyze', { params })
  return resp.data
}
