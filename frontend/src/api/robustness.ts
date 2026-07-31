import { api } from './client'

export interface RobustnessFactor {
  score: number
  max: number
  detail: string
}

export interface RobustnessResult {
  symbol: string
  lookback_days: number
  sample_size: number
  composite_score: number
  grade: string
  factors: {
    sub_period_stability: RobustnessFactor
    outlier_independence: RobustnessFactor
    wr_consistency: RobustnessFactor
    sample_adequacy: RobustnessFactor
  }
  quarter_pnls: number[]
  recommendation: string
  error?: string
}

export async function getRobustness(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<RobustnessResult> {
  const resp = await api.get('/api/robustness/score', { params })
  return resp.data
}
