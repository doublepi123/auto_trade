import { api } from './client'

export interface RegimeIndicators {
  volatility_level: string
  trend_direction: string
  volume_regime: string
  price_vs_mean_pct: number
}

export interface CurrentRegime {
  symbol: string
  regime_label: string
  confidence: number
  indicators: RegimeIndicators
  as_of: string
  data_points: number
}

export interface RegimeHistoryPoint {
  date: string
  regime_label: string
  avg_price: number
  volatility_proxy: number
}

export async function getCurrentRegime(symbol: string): Promise<CurrentRegime> {
  const resp = await api.get('/api/regime/current', { params: { symbol } })
  return resp.data
}

export async function getRegimeHistory(symbol: string, days: number = 30): Promise<RegimeHistoryPoint[]> {
  const resp = await api.get('/api/regime/history', { params: { symbol, days } })
  return resp.data
}
