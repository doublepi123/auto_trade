import { api } from './client'

export interface SymbolBucket {
  orders: number
  fills: number
  rejects: number
}

export interface QualitySummary {
  period_days: number
  total_orders: number
  filled_orders: number
  rejected_orders: number
  cancelled_orders: number
  fill_rate_pct: number
  avg_fill_time_seconds: number
  rejection_rate_pct: number
  rejection_reasons: Record<string, number>
  by_symbol: Record<string, SymbolBucket>
}

export interface SlippageRow {
  symbol: string
  avg_slippage_pct: number
  max_slippage_pct: number
  trade_count: number
  direction_bias: number
}

export async function getQualitySummary(days: number = 30): Promise<QualitySummary> {
  const resp = await api.get('/api/execution-quality/summary', { params: { days } })
  return resp.data
}

export async function getSlippageAnalysis(days: number = 30): Promise<SlippageRow[]> {
  const resp = await api.get('/api/execution-quality/slippage', { params: { days } })
  return resp.data
}
