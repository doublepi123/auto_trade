import { api } from './client'
import type { TradeNote, TradeNotePage, TradeNoteUpsert, TradeNoteAnalytics } from '../types'

export async function getTradeNotes(
  params: { symbol?: string; page?: number; page_size?: number } = {},
): Promise<TradeNotePage> {
  const resp = await api.get('/api/trade-notes', { params })
  return resp.data
}

export async function getTradeNoteAnalytics(): Promise<TradeNoteAnalytics> {
  const resp = await api.get('/api/trade-notes/analytics')
  return resp.data
}

export async function upsertTradeNote(orderId: number, payload: TradeNoteUpsert): Promise<TradeNote> {
  const resp = await api.put(`/api/trade-notes/${orderId}`, payload)
  return resp.data
}

export async function deleteTradeNote(orderId: number): Promise<void> {
  await api.delete(`/api/trade-notes/${orderId}`)
}
