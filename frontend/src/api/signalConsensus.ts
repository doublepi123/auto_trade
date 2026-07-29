import { api } from './client'

export interface SignalVote {
  signal: 'BULLISH' | 'BEARISH' | 'NEUTRAL' | 'NO_DATA'
  confidence: number
  detail: string
  updated_at: string | null
}

export interface ConsensusRow {
  symbol: string
  range_engine: SignalVote
  strategy_v2: SignalVote
  opening_momentum: SignalVote
  quant_score: SignalVote
  llm_advisor: SignalVote
  consensus: 'AGREE_BULLISH' | 'AGREE_BEARISH' | 'MIXED' | 'INSUFFICIENT_DATA'
  agreement_score: number
}

export interface ConsensusSummary {
  total_symbols: number
  agree_bullish: number
  agree_bearish: number
  mixed: number
  insufficient: number
}

export async function getSignalMatrix(symbols?: string): Promise<ConsensusRow[]> {
  const params: Record<string, string> = {}
  if (symbols) params.symbols = symbols
  const resp = await api.get('/api/signal-consensus/matrix', { params })
  return resp.data
}

export async function getSignalSummary(): Promise<ConsensusSummary> {
  const resp = await api.get('/api/signal-consensus/summary')
  return resp.data
}
