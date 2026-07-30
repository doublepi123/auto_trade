import { api } from './client'

export interface RecoveryEpisode {
  peak_trade_index: number
  trough_trade_index: number
  drawdown: number
  drawdown_pct: number
  recovered: boolean
  recovery_trades: number | null
  duration_trades: number
  still_underwater: boolean
}

export interface RecoveryResult {
  symbol: string
  lookback_days: number
  sample_size: number
  total_episodes: number
  recovered_count: number
  underwater_count: number
  avg_recovery_trades: number | null
  max_recovery_trades: number | null
  max_drawdown: number
  episodes: RecoveryEpisode[]
  error?: string
}

export async function getRecoveryTimeline(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<RecoveryResult> {
  const resp = await api.get('/api/recovery/timeline', { params })
  return resp.data
}
