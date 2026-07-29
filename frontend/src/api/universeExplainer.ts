import { api } from './client'

export interface ScoreBreakdown {
  [factor: string]: number
}

export interface PeerCandidate {
  symbol: string
  score: number
  selected: boolean
}

export interface SymbolExplanation {
  symbol: string
  selected: boolean
  rank: number | null
  score_breakdown: ScoreBreakdown
  hard_filters_passed: string[]
  hard_filters_failed: string[]
  peer_comparison: PeerCandidate[]
}

export interface RunCandidate {
  symbol: string
  score: number
  selected: boolean
  reason: string
}

export interface RunExplanation {
  run_id: number
  as_of_date: string
  status: string
  total_candidates: number
  selected_count: number
  coverage_ratio: number
  top_selected: RunCandidate[]
  top_rejected: RunCandidate[]
}

export async function explainSymbol(symbol: string): Promise<SymbolExplanation> {
  const resp = await api.get(`/api/universe-explainer/symbol/${encodeURIComponent(symbol)}`)
  return resp.data
}

export async function explainRun(runId?: number): Promise<RunExplanation> {
  const params: Record<string, number> = {}
  if (runId !== undefined) params.run_id = runId
  const resp = await api.get('/api/universe-explainer/run', { params })
  return resp.data
}
