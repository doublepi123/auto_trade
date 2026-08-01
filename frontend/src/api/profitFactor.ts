import { api } from './client'
import type { StatisticsQuality } from '../types'

export type ProfitFactorState = 'FINITE' | 'INFINITE' | 'UNDEFINED'

export interface PfSegment {
  segment: string
  trade_count: number
  profit_factor: number | null
  profit_factor_state: ProfitFactorState
  net_pnl: number
  win_rate: number
}

export interface ProfitFactorResult {
  symbol: string
  lookback_days: number
  sample_size: number
  overall: {
    profit_factor: number | null
    profit_factor_state: ProfitFactorState
    gross_profit: number
    gross_loss: number
    net_pnl: number
  }
  by_symbol: PfSegment[]
  by_size: PfSegment[]
  concentration: {
    top3_wins_share: number
    top3_losses_share: number
  }
  statistics_quality: StatisticsQuality
  error?: string
}

export async function getProfitFactor(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<ProfitFactorResult> {
  const resp = await api.get('/api/profit-factor/decompose', { params })
  return resp.data
}
