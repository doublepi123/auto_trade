import { api } from './client'
import type { StatisticsQuality } from '../types'

export interface ExitCauseRow {
  exit_cause: string
  trades: number
  net_pnl: number
  avg_capture: number | null
}

export interface ExcursionQuality {
  status: 'COMPLETE' | 'PARTIAL' | 'INSUFFICIENT'
  closed_trade_count: number
  eligible_excursion_count: number
  excluded_excursion_count: number
  excluded_by_reason: Record<string, number>
  interior_observation_count: number
  max_gap_seconds: number | null
}

export interface ExitEfficiencyResult {
  days: number
  sample_size: number
  closed_trade_count: number
  eligible_excursion_count: number
  capture_sample_size: number
  mae_sample_size: number
  winners_with_mfe: number
  avg_capture_rate: number | null
  median_capture_rate: number | null
  avg_giveback: number | null
  median_giveback: number | null
  left_on_table_count: number
  left_on_table_pct: number | null
  avg_mae_depth: number | null
  avg_winner_mae_depth: number | null
  by_exit_cause: ExitCauseRow[]
  excursion_quality: ExcursionQuality
  statistics_quality: StatisticsQuality
  error?: string
}

export async function getExitEfficiencySummary(
  params: { days?: number } = {},
): Promise<ExitEfficiencyResult> {
  const resp = await api.get('/api/exit-efficiency/summary', { params })
  return resp.data
}
