import { api } from './client'

export interface DecayWindow {
  window: number
  trade_count: number
  win_rate: number
  total_pnl: number
  avg_pnl: number
  sharpe: number
}

export interface DecayDetectionResult {
  symbol: string
  lookback_days: number
  sample_size: number
  n_windows: number
  windows: DecayWindow[]
  slopes: {
    win_rate_per_window: number
    sharpe_per_window: number
    avg_pnl_per_window: number
  }
  decay_signals: number
  verdict: string
  assessment: string
  error?: string
}

export async function getDecayDetection(
  params: { symbol?: string; lookback_days?: number; n_windows?: number } = {},
): Promise<DecayDetectionResult> {
  const resp = await api.get('/api/decay-detection/detect', { params })
  return resp.data
}
