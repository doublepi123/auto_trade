import { api } from './client'

export interface EdgeFeature {
  feature: string
  win_rate: number
  count: number
}

export interface PredictionScoreResult {
  symbol: string
  lookback_days: number
  sample_size: number
  baseline_win_rate: number
  dow_win_rates: Record<string, number>
  hour_win_rates: Record<string, number>
  streak_win_rates: Record<string, number>
  top_edges: EdgeFeature[]
  bottom_edges: EdgeFeature[]
  edge_spread: number
  error?: string
}

export async function getPredictionScore(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<PredictionScoreResult> {
  const resp = await api.get('/api/prediction-score/analyze', { params })
  return resp.data
}
