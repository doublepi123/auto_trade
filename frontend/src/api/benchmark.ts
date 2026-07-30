import { api } from './client'

export interface BenchmarkResult {
  symbol: string
  lookback_days: number
  sample_size: number
  trading_days: number
  alpha: number
  beta: number
  r_squared: number
  information_ratio: number
  market_mean_daily: number
  strategy_mean_daily: number
  interpretation: string
  error?: string
}

export async function getBenchmarkAlphaBeta(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<BenchmarkResult> {
  const resp = await api.get('/api/benchmark/alpha-beta', { params })
  return resp.data
}
