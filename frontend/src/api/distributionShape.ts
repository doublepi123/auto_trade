import { api } from './client'
import type { StatisticsQuality } from '../types'

export interface DistributionPercentiles {
  p5: number
  p25: number
  p50: number
  p75: number
  p95: number
}

export interface DistributionShapeResult {
  symbol: string
  lookback_days: number
  sample_size: number
  analysis_status?: 'READY' | 'DEGENERATE' | 'INSUFFICIENT_SAMPLE'
  mean: number
  std: number
  skewness: number
  kurtosis: number
  jarque_bera: number
  is_normal_like: boolean
  tail_label: string
  asymmetry: string
  percentiles: DistributionPercentiles
  iqr: number
  interpretation: string
  statistics_quality: StatisticsQuality
  error?: string
}

export async function getDistributionShape(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<DistributionShapeResult> {
  const resp = await api.get('/api/distribution-shape/analyze', { params })
  return resp.data
}
