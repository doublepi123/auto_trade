import { api } from './client'
import type { ClosedTradePage, TradeStats } from '../types'

export interface ClosedTradeQuery {
  symbol?: string
  from_date?: string
  to_date?: string
  limit?: number
}

export async function getClosedTrades(params: ClosedTradeQuery = {}): Promise<ClosedTradePage> {
  const resp = await api.get('/api/trades', { params })
  return resp.data
}

export interface TradeStatsQuery {
  symbol?: string
  days?: number
}

export async function getTradeStats(params: TradeStatsQuery = {}): Promise<TradeStats> {
  const resp = await api.get('/api/trades/stats', { params })
  return resp.data
}
