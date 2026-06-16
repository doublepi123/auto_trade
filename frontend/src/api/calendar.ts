import { api } from './client'
import type { MarketSessionStatus } from '../types'

export async function getMarketSession(symbol: string): Promise<MarketSessionStatus> {
  const resp = await api.get('/api/calendar/session', { params: { symbol } })
  return resp.data
}
