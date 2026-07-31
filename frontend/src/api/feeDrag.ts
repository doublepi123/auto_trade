import { api } from './client'
import type { StatisticsQuality } from '../types'

export interface FeeDragSymbol {
  symbol: string
  trades: number
  fees: number
  gross_pnl: number
  net_pnl: number
  fee_share_of_gross: number | null
}

export interface FeeDragDaily {
  date: string
  fees: number
}

export interface FeeDragResult {
  days: number
  sample_size: number
  total_fees: number
  total_gross_pnl: number
  total_net_pnl: number
  avg_fee_per_trade: number
  fee_to_gross_win_ratio: number | null
  fee_sources: Record<string, number>
  by_symbol: FeeDragSymbol[]
  daily_fees: FeeDragDaily[]
  statistics_quality: StatisticsQuality
  error?: string
}

export async function getFeeDragSummary(
  params: { days?: number } = {},
): Promise<FeeDragResult> {
  const resp = await api.get('/api/fee-drag/summary', { params })
  return resp.data
}
