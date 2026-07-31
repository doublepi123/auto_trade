import { api } from './client'

export interface DailyDistribution {
  trades_per_day: number
  day_count: number
}

export interface TradeFrequencyResult {
  symbol: string
  lookback_days: number
  total_trades: number
  active_days: number
  avg_trades_per_day: number
  max_trades_in_day: number
  max_day_date: string
  avg_interval_seconds: number
  min_interval_seconds: number
  rapid_fire_count: number
  rapid_fire_pct: number
  daily_distribution: DailyDistribution[]
  overtrading_flag: boolean
  assessment: string
  error?: string
}

export async function getTradeFrequency(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<TradeFrequencyResult> {
  const resp = await api.get('/api/trade-frequency/analyze', { params })
  return resp.data
}
