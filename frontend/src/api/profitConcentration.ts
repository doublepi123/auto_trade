import { api } from './client'
import type { StatisticsQuality } from '../types'

export interface ParetoPoint {
  top_pct_trades: number
  trade_count: number
  profit_share: number
}

export interface ProfitConcentrationResult {
  days: number
  sample_size: number
  winning_trades: number
  losing_trades: number
  gross_profit: number
  gross_loss: number
  top_trade_pnl: number
  top_trade_share: number
  top5_share: number
  gini_winners: number
  pareto_curve: ParetoPoint[]
  currency: string | null
  currencies: string[]
  totals_comparable: boolean
  statistics_quality: StatisticsQuality
  error?: string
}

export async function getProfitConcentrationSummary(
  params: { days?: number } = {},
): Promise<ProfitConcentrationResult> {
  const resp = await api.get('/api/profit-concentration/summary', { params })
  return resp.data
}
