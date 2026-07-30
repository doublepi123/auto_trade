import { api } from './client'

export interface BucketStats {
  bucket: number
  trade_count: number
  total_pnl: number
  avg_pnl: number
  win_rate: number
  day_name?: string
}

export interface TimePerformanceResult {
  symbol: string
  lookback_days: number
  sample_size: number
  by_hour: BucketStats[]
  by_day_of_week: BucketStats[]
  highlights: {
    best_hour: BucketStats | null
    worst_hour: BucketStats | null
    best_day: BucketStats | null
    worst_day: BucketStats | null
  }
  error?: string
}

export async function getTimePerformance(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<TimePerformanceResult> {
  const resp = await api.get('/api/time-performance/analyze', { params })
  return resp.data
}
