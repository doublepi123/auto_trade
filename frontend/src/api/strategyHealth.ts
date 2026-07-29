import { api } from './client'

export interface TradeSideMetrics {
  win_rate: number
  avg_pnl: number
  trade_count: number
  avg_holding_minutes: number
  profit_factor: number
}

export interface HealthDrift {
  win_rate_drift: number
  pnl_drift: number
  trade_frequency_drift: number
}

export interface HealthReport {
  symbol: string | null
  period_days: number
  live_metrics: TradeSideMetrics
  shadow_metrics: TradeSideMetrics
  drift: HealthDrift
  health_status: 'HEALTHY' | 'WARNING' | 'DEGRADED' | 'INSUFFICIENT_DATA'
  alerts: string[]
}

export interface TrendRow {
  week_start: string
  live_win_rate: number
  shadow_win_rate: number
  live_avg_pnl: number
  shadow_avg_pnl: number
  live_trades: number
  shadow_trades: number
}

export async function getHealthReport(symbol?: string): Promise<HealthReport> {
  const params: Record<string, string> = {}
  if (symbol) params.symbol = symbol
  const resp = await api.get('/api/strategy-health/report', { params })
  return resp.data
}

export async function getPerformanceTrend(params: { symbol?: string; weeks?: number } = {}): Promise<TrendRow[]> {
  const resp = await api.get('/api/strategy-health/trend', { params })
  return resp.data
}
