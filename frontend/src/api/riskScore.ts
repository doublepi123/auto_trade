import { api } from './client'

export interface FactorScores {
  volatility: number
  drawdown: number
  streak: number
  loss_ratio: number
}

export interface SymbolRiskScore {
  symbol: string
  trade_count: number
  total_pnl: number
  pnl_std: number
  max_drawdown: number
  max_loss_streak: number
  loss_ratio: number
  factor_scores: FactorScores
  composite_score: number
  risk_level: string
}

export interface RiskScoreResult {
  lookback_days: number
  sample_size: number
  symbols: SymbolRiskScore[]
  avg_composite: number
  error?: string
}

export async function getRiskScore(
  params: { lookback_days?: number } = {},
): Promise<RiskScoreResult> {
  const resp = await api.get('/api/risk-score/compute', { params })
  return resp.data
}
