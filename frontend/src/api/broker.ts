import { api } from './client'
import type { BrokerCandlesResponse } from '../types'

export async function getBrokerCandles(
  symbol: string,
  period: string,
  count: number,
): Promise<BrokerCandlesResponse> {
  const resp = await api.get('/api/broker/candles', { params: { symbol, period, count } })
  return resp.data
}
