import { api } from './client'

export interface PeriodPnl {
  period: string
  pnl: number
  trade_count: number
}

export interface ReturnCalendarResult {
  symbol: string
  lookback_days: number
  sample_size: number
  weekly: PeriodPnl[]
  monthly: PeriodPnl[]
  summary: {
    total_weeks: number
    positive_weeks: number
    weekly_win_rate: number
    total_months: number
    positive_months: number
    monthly_win_rate: number
    best_week: PeriodPnl | null
    worst_week: PeriodPnl | null
    best_month: PeriodPnl | null
    worst_month: PeriodPnl | null
  }
  error?: string
}

export async function getReturnCalendar(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<ReturnCalendarResult> {
  const resp = await api.get('/api/return-calendar/compute', { params })
  return resp.data
}
