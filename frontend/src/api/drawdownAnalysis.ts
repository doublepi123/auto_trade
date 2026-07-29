import { api } from './client'

export interface DrawdownSummary {
  symbol: string | null
  period_days: number
  peak_pnl: number
  current_pnl: number
  current_drawdown: number
  current_drawdown_pct: number
  max_drawdown: number
  max_drawdown_pct: number
  max_drawdown_date: string | null
  max_drawdown_duration_days: number
  recovery_count: number
  avg_recovery_days: number
  is_in_drawdown: boolean
}

export interface DrawdownTimelinePoint {
  date: string
  cumulative_pnl: number
  peak_pnl: number
  drawdown: number
  drawdown_pct: number
  is_in_drawdown: boolean
}

export async function getDrawdownSummary(params: { symbol?: string; days?: number } = {}): Promise<DrawdownSummary> {
  const resp = await api.get('/api/drawdown-analysis/summary', { params })
  return resp.data
}

export async function getDrawdownTimeline(params: { symbol?: string; days?: number } = {}): Promise<DrawdownTimelinePoint[]> {
  const resp = await api.get('/api/drawdown-analysis/timeline', { params })
  return resp.data
}
