import { api } from './client'
import type { StatisticsQuality } from '../types'

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
  classified_trades: number
  window: number
  regime_basis: 'PRIOR_CLOSED_TRADE_PNL_VOLATILITY'
  median_volatility: number
  regimes: RegimeStats[]
  sensitivity: number
  interpretation: string
  currency: string | null
  currencies: string[]
  totals_comparable: boolean
  statistics_quality: StatisticsQuality
  error?: string
}

export async function getRegimeSensitivity(
  params: { symbol?: string; lookback_days?: number; window?: number } = {},
): Promise<RegimeSensitivityResult> {
  const resp = await api.get('/api/regime-sensitivity/analyze', { params })
  return resp.data
}
