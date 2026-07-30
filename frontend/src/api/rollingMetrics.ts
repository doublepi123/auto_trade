import { api } from './client'

export interface RollingPoint {
  index: number
  total_pnl: number
  avg_pnl: number
  win_rate: number
  sharpe: number
}

export interface RollingSummary {
  sharpe_mean: number
  sharpe_min: number
  sharpe_max: number
  win_rate_mean: number
  latest_sharpe: number
  latest_win_rate: number
  trend: string
}

export interface RollingMetricsResult {
  symbol: string
  lookback_days: number
  window: number
  sample_size: number
  points: RollingPoint[]
  summary: RollingSummary
  error?: string
}

export async function getRollingMetrics(
  params: { symbol?: string; lookback_days?: number; window?: number } = {},
): Promise<RollingMetricsResult> {
  const resp = await api.get('/api/rolling-metrics/compute', { params })
  return resp.data
}
