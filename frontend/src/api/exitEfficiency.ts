import { api } from './client'

export interface ExitCauseRow {
  exit_cause: string
  trades: number
  net_pnl: number
  avg_capture: number | null
}

export interface ExitEfficiencyResult {
  days: number
  sample_size: number
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
  error?: string
}

export async function getExitEfficiencySummary(
  params: { days?: number } = {},
): Promise<ExitEfficiencyResult> {
  const resp = await api.get('/api/exit-efficiency/summary', { params })
  return resp.data
}
