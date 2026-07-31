import { api } from './client'
import type { StatisticsQuality } from '../types'

export interface EdgeFeature {
  feature: string
  win_rate: number
  count: number
}

export interface PredictionScoreResult {
  symbol: string
  lookback_days: number
  sample_size: number
  evidence_mode: 'RETROSPECTIVE_CONDITIONAL_FREQUENCY'
  live_decision_allowed: false
  baseline_win_rate: number
  dow_win_rates: Record<string, number>
  hour_win_rates: Record<string, number>
  streak_win_rates: Record<string, number>
  top_edges: EdgeFeature[]
  bottom_edges: EdgeFeature[]
  edge_spread: number
  currency: string | null
  currencies: string[]
  totals_comparable: boolean
  statistics_quality: StatisticsQuality
  error?: string
}

export async function getPredictionScore(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<PredictionScoreResult> {
  const resp = await api.get('/api/prediction-score/analyze', { params })
  return resp.data
}
