import { api } from './client'
import type { StatisticsQuality } from '../types'

export interface ConcentrationBreakdown {
  symbol: string
  trade_count: number
  total_pnl: number
  pnl_share: number | null
  count_share: number
}

export interface ConcentrationResult {
  lookback_days: number
  sample_size: number
  analysis_status?: 'READY' | 'UNAVAILABLE'
  symbol_count: number
  hhi_pnl: number | null
  effective_n_pnl: number | null
  hhi_count: number
  effective_n_count: number
  concentration_level: string
  top_symbol: ConcentrationBreakdown | null
  breakdown: ConcentrationBreakdown[]
  assessment: string
  statistics_quality: StatisticsQuality
  error?: string
}

export async function getConcentration(
  params: { lookback_days?: number } = {},
): Promise<ConcentrationResult> {
  const resp = await api.get('/api/concentration/analyze', { params })
  return resp.data
}
