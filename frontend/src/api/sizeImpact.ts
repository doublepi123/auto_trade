import { api } from './client'

export interface SizeQuartile {
  quartile: string
  trade_count: number
  avg_quantity: number
  total_pnl: number
  avg_pnl: number
  win_rate: number
  pnl_per_unit: number
}

export interface SizeImpactResult {
  symbol: string
  lookback_days: number
  sample_size: number
  quartiles: SizeQuartile[]
  size_efficiency_trend: string
  assessment: string
  error?: string
}

export async function getSizeImpact(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<SizeImpactResult> {
  const resp = await api.get('/api/size-impact/analyze', { params })
  return resp.data
}
