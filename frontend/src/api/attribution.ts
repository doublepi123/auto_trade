import { api } from './client'

export interface PnlBucket {
  total_pnl: number
  trade_count: number
  win_count?: number
  avg_pnl?: number
}

export interface DayPnl {
  date: string
  pnl: number
  trade_count: number
}

export interface AttributionResult {
  period_days: number
  total_pnl: number
  total_trades: number
  win_rate: number
  by_symbol: Record<string, PnlBucket>
  by_direction: Record<string, PnlBucket>
  by_exit_reason: Record<string, PnlBucket>
  by_session: Record<string, PnlBucket>
  by_day: DayPnl[]
}

export interface TopContributor {
  symbol: string
  total_pnl: number
  trade_count: number
  win_rate: number
  avg_holding_minutes: number
}

export async function getPnlAttribution(params: { days?: number; symbol?: string } = {}): Promise<AttributionResult> {
  const resp = await api.get('/api/attribution/pnl', { params })
  return resp.data
}

export async function getTopContributors(params: { days?: number; limit?: number } = {}): Promise<TopContributor[]> {
  const resp = await api.get('/api/attribution/top-contributors', { params })
  return resp.data
}
