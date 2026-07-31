import { api } from './client'

export interface SideStats {
  label: string
  count: number
  total: number
  avg: number
  median: number
  max: number
  min: number
  top3_share: number
}

export interface AsymmetryResult {
  symbol: string
  lookback_days: number
  sample_size: number
  win_stats: SideStats
  loss_stats: SideStats
  breakeven_count: number
  asymmetry_ratio: number | null
  total_win: number
  total_loss: number
  net_edge: number
  conditional: {
    after_big_win_avg: number | null
    after_big_win_count: number
    after_big_loss_avg: number | null
    after_big_loss_count: number
  }
  assessment: string
  error?: string
}

export async function getAsymmetry(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<AsymmetryResult> {
  const resp = await api.get('/api/asymmetry/analyze', { params })
  return resp.data
}
