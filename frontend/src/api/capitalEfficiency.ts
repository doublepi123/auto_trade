import { api } from './client'
import type { StatisticsQuality } from '../types'

export interface CapitalEfficiencyResult {
  symbol: string
  lookback_days: number
  sample_size: number
  capital_base: number
  capital_base_currency: string | null
  evidence_scope: 'CLOSED_ROUND_TRIPS_ONLY'
  evidence_note: string
  total_pnl: number
  return_on_capital: number
  annualized_roc: number
  turnover_ratio: number
  pnl_per_unit_traded: number
  total_entry_notional: number
  winning_entry_notional_share: number
  average_closed_round_trip_capital: number
  capital_time_utilization_rate: number
  utilization_rate: number
  exit_active_days: number
  exit_active_day_rate: number
  assessment: string
  currency: string | null
  currencies: string[]
  totals_comparable: boolean
  statistics_quality: StatisticsQuality
  error?: string
}

export async function getCapitalEfficiency(
  params: { symbol?: string; lookback_days?: number; capital_base?: number } = {},
): Promise<CapitalEfficiencyResult> {
  const resp = await api.get('/api/capital-efficiency/analyze', { params })
  return resp.data
}
