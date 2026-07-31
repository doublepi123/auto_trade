import { api } from './client'

export interface RegimeStats {
  regime: string
  trade_count: number
  win_rate: number
  avg_pnl: number
  total_pnl: number
  sharpe: number
}

export interface RegimeSensitivityResult {
  symbol: string
  lookback_days: number
  sample_size: number
  window: number
  median_volatility: number
  regimes: RegimeStats[]
  sensitivity: number
  interpretation: string
  error?: string
}

export async function getRegimeSensitivity(
  params: { symbol?: string; lookback_days?: number; window?: number } = {},
): Promise<RegimeSensitivityResult> {
  const resp = await api.get('/api/regime-sensitivity/analyze', { params })
  return resp.data
}
