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

export async function getTradeNote(orderId: number): Promise<TradeNote | null> {
  // Treat 404 (no note yet) as null rather than throwing.
  const resp = await api.get(`/api/trade-notes/${orderId}`, {
    validateStatus: (s) => s === 200 || s === 404,
  })
  return resp.status === 404 ? null : resp.data
}

export async function upsertTradeNote(orderId: number, payload: TradeNoteUpsert): Promise<TradeNote> {
  const resp = await api.put(`/api/trade-notes/${orderId}`, payload)
  return resp.data
}

export async function deleteTradeNote(orderId: number): Promise<void> {
  await api.delete(`/api/trade-notes/${orderId}`)
}
