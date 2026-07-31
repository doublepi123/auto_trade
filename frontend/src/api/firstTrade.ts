import { api } from './client'

export interface FirstTradeBucket {
  trades: number
  win_rate: number | null
  avg_pnl: number | null
  total_pnl: number
}

export interface FirstTradeResult {
  days: number
  sample_size: number
  trading_days: number
  green_day_pct: number | null
  multi_trade_days: number
  first_trade: FirstTradeBucket
  rest_of_day: FirstTradeBucket
  tone_match_pct: number | null
  tone_days: number
  error?: string
}

export async function getFirstTradeSummary(
  params: { days?: number } = {},
): Promise<FirstTradeResult> {
  const resp = await api.get('/api/first-trade/summary', { params })
  return resp.data
}
