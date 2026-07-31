import { api } from './client'

export interface AcfLag {
  lag: number
  acf: number
  significant: boolean
}

export interface AutocorrelationResult {
  symbol: string
  lookback_days: number
  sample_size: number
  lags: AcfLag[]
  ljung_box_q: number
  significant_lags: number
  confidence_band: number
  pattern: string
  interpretation: string
  error?: string
}

export async function getAutocorrelation(
  params: { symbol?: string; lookback_days?: number; max_lag?: number } = {},
): Promise<AutocorrelationResult> {
  const resp = await api.get('/api/autocorrelation/analyze', { params })
  return resp.data
}
