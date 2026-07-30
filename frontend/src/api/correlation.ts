import { api } from './client'

export interface CorrelationPair {
  symbol_a: string
  symbol_b: string
  correlation: number
}

export interface CorrelationResult {
  lookback_days: number
  symbols: string[]
  matrix: number[][]
  pairs: CorrelationPair[]
  avg_abs_correlation: number
  diversification_score: number
  note?: string
}

export async function getCorrelationMatrix(
  params: { lookback_days?: number; min_trades?: number } = {},
): Promise<CorrelationResult> {
  const resp = await api.get('/api/correlation/matrix', { params })
  return resp.data
}
