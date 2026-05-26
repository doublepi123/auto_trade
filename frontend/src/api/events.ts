import { api } from './client'
import type { TimelineSource, TradeEventPage } from '../types'

export type { TimelineSource }

export interface GetTradeEventsParams {
  page?: number
  page_size?: number
  limit?: number
  symbol?: string
  /** Single or multiple filters (repeatable query keys for FastAPI) */
  event_type?: string | string[]
  source?: TimelineSource
}

function buildEventsQuery(params: GetTradeEventsParams): string {
  const sp = new URLSearchParams()
  if (params.page != null) sp.set('page', String(params.page))
  if (params.page_size != null) sp.set('page_size', String(params.page_size))
  if (params.limit != null) sp.set('limit', String(params.limit))
  if (params.symbol) sp.set('symbol', params.symbol)
  if (params.source && params.source !== 'all') sp.set('source', params.source)

  const et = params.event_type
  if (Array.isArray(et)) {
    et.forEach((t) => {
      const s = String(t).trim()
      if (s) sp.append('event_type', s)
    })
  }
  else if (typeof et === 'string' && et.trim()) {
    sp.append('event_type', et.trim())
  }

  const qs = sp.toString()
  return qs ? `/api/events?${qs}` : '/api/events'
}

export async function getTradeEvents(params: GetTradeEventsParams = {}): Promise<TradeEventPage> {
  const url = buildEventsQuery(params)
  const resp = await api.get(url)
  return resp.data
}

export async function exportTradeEvents(format: 'csv' | 'json'): Promise<Blob> {
  const resp = await api.get('/api/events/export', {
    params: { format },
    responseType: 'blob',
  })
  return resp.data
}
