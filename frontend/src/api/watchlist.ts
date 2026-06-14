import { api } from './client'
import type { WatchlistItem, WatchlistQuote, WatchlistSnapshot } from '../types'

export interface WatchlistScore {
  id: number
  symbol: string
  market: string
  score: number
  rationale: string
  confidence: number
  recommended_action: 'BUY' | 'SELL' | 'HOLD' | 'AVOID' | string
  source: string
  created_at: string
  expires_at: string
  is_stale: boolean
}

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

export async function getWatchlistSnapshots(): Promise<WatchlistSnapshot[]> {
  const resp = await api.get('/api/watchlist/snapshots')
  return resp.data
}

export interface ScoredWatchlistSnapshot extends WatchlistSnapshot {
  score: number
  is_stale: boolean
}

export async function getWatchlistScoredSnapshots(): Promise<ScoredWatchlistSnapshot[]> {
  const resp = await api.get('/api/watchlist/scored-snapshots')
  return resp.data
}

export async function scoreWatchlistSymbol(data: { symbol: string; market: 'US' | 'HK'; ttl_minutes?: number }): Promise<WatchlistScore> {
  const resp = await api.post('/api/watchlist/score', data)
  return resp.data
}

export async function getWatchlistScores(): Promise<WatchlistScore[]> {
  const resp = await api.get('/api/watchlist/scores')
  // The previous `resp.data.scores ?? []` silently swallowed shape changes
  // (typos in the backend, accidental rename, missing field after a deploy).
  // Surface the discrepancy explicitly so the caller can react instead of
  // rendering an empty list with no clue why.
  if (!Array.isArray(resp.data.scores)) {
    throw new Error(
      `Unexpected /api/watchlist/scores response: scores field is ${typeof resp.data.scores}`
    )
  }
  return resp.data.scores as WatchlistScore[]
}
