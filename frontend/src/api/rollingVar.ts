import { api } from './client'
import type { StatisticsQuality } from '../types'

export interface VarPoint {
  index: number
  at: string
  var: number
  cvar: number
}

export interface RollingVarResult {
  symbol: string
  lookback_days: number
  window: number
  confidence: number
  sample_size: number
  tail_count: number
  points: VarPoint[]
  summary: {
    latest_var: number
    latest_cvar: number
    var_mean: number
    var_max: number
    cvar_mean: number
    cvar_max: number
  }
  currency: string | null
  currencies: string[]
  totals_comparable: boolean
  statistics_quality: StatisticsQuality
  error?: string
}

export async function getRollingVar(
  params: { symbol?: string; lookback_days?: number; window?: number; confidence?: number } = {},
): Promise<RollingVarResult> {
  const resp = await api.get('/api/rolling-var/compute', { params })
  return resp.data
}
