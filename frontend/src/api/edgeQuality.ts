import { api } from './client'

export interface EdgeFactor {
  score: number
  max: number
  detail: string
}

export interface EdgeQualityResult {
  symbol: string
  lookback_days: number
  sample_size: number
  composite_score: number
  grade: string
  factors: {
    expectancy: EdgeFactor
    consistency: EdgeFactor
    drawdown_control: EdgeFactor
    sample_adequacy: EdgeFactor
  }
  underlying: {
    win_rate: number
    expectancy: number
    payoff_ratio: number | null
    max_drawdown: number
  }
  recommendation: string
  error?: string
}

export async function getEdgeQuality(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<EdgeQualityResult> {
  const resp = await api.get('/api/edge-quality/score', { params })
  return resp.data
}
