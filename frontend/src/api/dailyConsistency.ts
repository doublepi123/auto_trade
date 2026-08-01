import { api } from './client'
import type { StatisticsQuality } from '../types'

export interface DailyPnlPoint {
  date: string
  pnl: number
}

export interface DailyConsistencyResult {
  days: number
  trading_days: number
  total_pnl: number
  green_days: number
  red_days: number
  green_day_pct: number
  avg_daily_pnl: number
  daily_std: number
  daily_sharpe: number | null
  longest_green_streak: number
  longest_red_streak: number
  current_streak: number
  top5_day_profit_share: number | null
  best_day: DailyPnlPoint
  worst_day: DailyPnlPoint
  daily: DailyPnlPoint[]
  statistics_quality: StatisticsQuality
  error?: string
}

export async function getDailyConsistencySummary(
  params: { days?: number } = {},
): Promise<DailyConsistencyResult> {
  const resp = await api.get('/api/daily-consistency/summary', { params })
  return resp.data
}
