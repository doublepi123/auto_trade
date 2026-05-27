import { api } from './client'
import type { WatchlistItem, WatchlistQuote } from '../types'

export async function getWatchlist(): Promise<WatchlistItem[]> {
  const resp = await api.get('/api/watchlist')
  return resp.data
}

export async function addWatchlistItem(data: { symbol: string; market: 'US' | 'HK'; alias?: string }): Promise<WatchlistItem> {
  const resp = await api.post('/api/watchlist', data)
  return resp.data
}

export async function removeWatchlistItem(itemId: number): Promise<{ message: string }> {
  const resp = await api.delete(`/api/watchlist/${itemId}`)
  return resp.data
}

export async function activateWatchlistItem(itemId: number): Promise<WatchlistItem> {
  const resp = await api.post(`/api/watchlist/${itemId}/set-trading`)
  return resp.data
}

export async function getWatchlistQuotes(): Promise<WatchlistQuote[]> {
  const resp = await api.get('/api/watchlist/quotes')
  return resp.data
}
