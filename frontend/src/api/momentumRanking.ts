import { api } from './client'
import type { StatisticsQuality } from '../types'

export interface MomentumEntry {
  rank: number
  symbol: string
  trade_count: number
  total_pnl: number
  win_rate: number
  momentum_slope: number
  recent_return: number
  older_return: number
  return_acceleration: number
}

export interface MomentumRankingResult {
  lookback_days: number
  sample_size: number
  qualifying_symbols: number
  rankings: MomentumEntry[]
  top_momentum: MomentumEntry | null
  bottom_momentum: MomentumEntry | null
  currency: string | null
  currencies: string[]
  totals_comparable: boolean
  statistics_quality: StatisticsQuality
  error?: string
}

export async function getMomentumRanking(
  params: { lookback_days?: number; min_trades?: number } = {},
): Promise<MomentumRankingResult> {
  const resp = await api.get('/api/momentum-ranking/rank', { params })
  return resp.data
}
