import { api } from './client'

export interface TimelineEntry {
  timestamp: string
  event_type: string
  status: string
  message: string
  payload_summary: Record<string, unknown>
}

export interface TradeReplay {
  trade_id: number
  symbol: string
  market: string
  side: string
  entry_price: number
  exit_price: number
  pnl: number
  entry_time: string
  exit_time: string
  timeline: TimelineEntry[]
}

export interface ReplayableTrade {
  trade_id: number
  symbol: string
  market: string
  side: string
  pnl: number
  entry_time: string
  exit_time: string
  event_count: number
}

export async function replayTrade(tradeId: number): Promise<TradeReplay> {
  const resp = await api.get(`/api/decision-replay/trade/${tradeId}`)
  return resp.data
}

export async function listReplayableTrades(params: { limit?: number; symbol?: string } = {}): Promise<ReplayableTrade[]> {
  const resp = await api.get('/api/decision-replay/trades', { params })
  return resp.data
}
