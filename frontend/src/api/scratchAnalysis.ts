import { api } from './client'

export interface ScratchSymbolRow {
  symbol: string
  total: number
  scratch: number
  scratch_rate: number
}

export interface ScratchWeekRow {
  week: string
  total: number
  scratch_rate: number
}

export interface ScratchAnalysisResult {
  days: number
  sample_size: number
  scratch_count: number
  scratch_rate: number
  scratch_fee_total: number
  avg_scratch_hold_min: number | null
  avg_decisive_hold_min: number | null
  median_scratch_hold_min: number | null
  by_symbol: ScratchSymbolRow[]
  weekly: ScratchWeekRow[]
  error?: string
}

export async function getScratchAnalysisSummary(
  params: { days?: number } = {},
): Promise<ScratchAnalysisResult> {
  const resp = await api.get('/api/scratch-analysis/summary', { params })
  return resp.data
}
