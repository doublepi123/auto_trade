import { api } from './client'

export interface DurationHistogram {
  duration: number
  count: number
}

export interface DrawdownDurationResult {
  symbol: string
  lookback_days: number
  sample_size: number
  episodes: number
  durations: number[]
  histogram: DurationHistogram[]
  summary: {
    avg: number
    max: number
    median: number
    p25: number
    p75: number
  }
  pct_time_underwater: number
  note?: string
  error?: string
}

export async function getDrawdownDuration(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<DrawdownDurationResult> {
  const resp = await api.get('/api/drawdown-duration/analyze', { params })
  return resp.data
}
