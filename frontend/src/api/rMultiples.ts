import { api } from './client'
import type { StatisticsQuality } from '../types'

export interface RBucket {
  bucket: string
  count: number
  share: number
}

export interface RMultiplesResult {
  days: number
  sample_size: number
  risk_unit_method: 'MEAN_REALIZED_LOSS_PROXY'
  true_initial_risk_available: false
  risk_unit: number
  expectancy_r: number
  pct_ge_1r: number
  pct_le_minus_1r: number
  min_r: number
  max_r: number
  histogram: RBucket[]
  currency: string | null
  currencies: string[]
  totals_comparable: boolean
  statistics_quality: StatisticsQuality
  error?: string
}

export async function getRMultiplesDistribution(
  params: { days?: number } = {},
): Promise<RMultiplesResult> {
  const resp = await api.get('/api/r-multiples/distribution', { params })
  return resp.data
}
