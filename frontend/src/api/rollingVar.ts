import { api } from './client'

export interface VarPoint {
  index: number
  var: number
  cvar: number
}

export interface RollingVarResult {
  symbol: string
  lookback_days: number
  window: number
  confidence: number
  sample_size: number
  points: VarPoint[]
  summary: {
    latest_var: number
    latest_cvar: number
    var_mean: number
    var_max: number
    cvar_mean: number
    cvar_max: number
  }
  error?: string
}

export async function getRollingVar(
  params: { symbol?: string; lookback_days?: number; window?: number; confidence?: number } = {},
): Promise<RollingVarResult> {
  const resp = await api.get('/api/rolling-var/compute', { params })
  return resp.data
}
