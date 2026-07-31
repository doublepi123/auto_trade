import { api } from './client'

export interface ReentryBucket {
  trades: number
  win_rate: number | null
  avg_pnl: number | null
  total_pnl: number
}

export interface ReentrySymbolRow {
  symbol: string
  after_win: ReentryBucket
  after_loss: ReentryBucket
}

export interface ReentryAnalysisResult {
  days: number
  sample_size: number
  after_win: ReentryBucket
  after_loss: ReentryBucket
  after_scratch: ReentryBucket
  first_of_symbol: ReentryBucket
  tilt_avg_pnl_diff: number | null
  by_symbol: ReentrySymbolRow[]
  error?: string
}

export async function getReentryAnalysisSummary(
  params: { days?: number } = {},
): Promise<ReentryAnalysisResult> {
  const resp = await api.get('/api/reentry-analysis/summary', { params })
  return resp.data
}
