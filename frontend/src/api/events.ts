import { api } from './client'
import type { TradeEventPage } from '../types'

export interface GetTradeEventsParams {
  page?: number
  page_size?: number
  limit?: number
  symbol?: string
  event_type?: string
}

export async function getTradeEvents(params: GetTradeEventsParams = {}): Promise<TradeEventPage> {
  const resp = await api.get('/api/events', { params })
  return resp.data
}

export async function exportTradeEvents(format: 'csv' | 'json'): Promise<Blob> {
  const resp = await api.get('/api/events/export', {
    params: { format },
    responseType: 'blob',
  })
  return resp.data
}
