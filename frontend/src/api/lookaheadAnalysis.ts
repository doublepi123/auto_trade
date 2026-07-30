import { api } from './client'

export interface BaselineStats {
  trade_count: number
  win_rate: number
  total_pnl: number
  avg_pnl: number
}

export interface SliceResult {
  pct: number
  trade_count: number
  win_rate: number
  total_pnl: number
  avg_pnl: number
  signal_consistency: number
  win_rate_delta: number
  pnl_delta: number
}

export interface LookaheadResult {
  symbol: string
  lookback_days: number
  total_exits: number
  baseline: BaselineStats
  slices: SliceResult[]
  has_bias: boolean
  bias_score: number
  recommendation: string
}

export async function getLookaheadAnalysis(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<LookaheadResult> {
  const resp = await api.get('/api/lookahead-analysis/analyze', { params })
  return resp.data
}
