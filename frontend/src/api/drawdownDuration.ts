import { api } from './client'
import type { StatisticsQuality } from '../types'

export interface DurationHistogram {
  duration: number
  count: number
}

export interface DrawdownDurationResult {
  symbol: string
  lookback_days: number
  sample_size: number
  evidence_scope: 'WINDOW_LOCAL_UNDERWATER_RUNS'
  pre_window_high_water_known: false
  duration_unit: 'closed_trades'
  scope_note: string
  /** Compatibility alias: fully observed completed episodes only. */
  episodes: number
  completed_episodes: number
  durations: number[]
  histogram: DurationHistogram[]
  summary: {
    avg: number | null
    max: number | null
    median: number | null
    p25: number | null
    p75: number | null
  }
  median_method: 'statistics.median'
  quantile_method: "statistics.quantiles(n=4, method='inclusive')"
  current_open_duration: number
  is_underwater: boolean
  left_censored: boolean
  excluded_left_censored_duration: number | null
  observed_underwater_trade_count: number
  pct_time_underwater: number
  statistics_quality: StatisticsQuality
  note?: string
  error?: string
}

export async function getDrawdownDuration(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<DrawdownDurationResult> {
  const resp = await api.get('/api/drawdown-duration/analyze', { params })
  return resp.data
}
