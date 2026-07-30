import { api } from './client'

export interface KellyVariant {
  fraction: number
  label: string
  allocation_pct: number
  expected_growth: number
}

export interface KellyResult {
  symbol: string
  lookback_days: number
  sample_size: number
  win_rate: number
  avg_win: number
  avg_loss: number
  payoff_ratio: number
  kelly_full_pct: number
  variants: KellyVariant[]
  recommendation: string
  error?: string
}

export async function getKellySizing(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<KellyResult> {
  const resp = await api.get('/api/kelly/sizing', { params })
  return resp.data
}
