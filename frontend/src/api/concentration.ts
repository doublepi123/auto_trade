import { api } from './client'

export interface ConcentrationBreakdown {
  symbol: string
  trade_count: number
  total_pnl: number
  pnl_share: number
  count_share: number
}

export interface ConcentrationResult {
  lookback_days: number
  sample_size: number
  symbol_count: number
  hhi_pnl: number
  effective_n_pnl: number
  hhi_count: number
  effective_n_count: number
  concentration_level: string
  top_symbol: ConcentrationBreakdown | null
  breakdown: ConcentrationBreakdown[]
  assessment: string
  error?: string
}

export async function getConcentration(
  params: { lookback_days?: number } = {},
): Promise<ConcentrationResult> {
  const resp = await api.get('/api/concentration/analyze', { params })
  return resp.data
}
