import { api } from './client'

export interface CapitalEfficiencyResult {
  symbol: string
  lookback_days: number
  sample_size: number
  capital_base: number
  total_pnl: number
  return_on_capital: number
  annualized_roc: number
  turnover_ratio: number
  pnl_per_unit_traded: number
  capital_efficiency: number
  active_days: number
  utilization_rate: number
  assessment: string
  error?: string
}

export async function getCapitalEfficiency(
  params: { symbol?: string; lookback_days?: number; capital_base?: number } = {},
): Promise<CapitalEfficiencyResult> {
  const resp = await api.get('/api/capital-efficiency/analyze', { params })
  return resp.data
}
