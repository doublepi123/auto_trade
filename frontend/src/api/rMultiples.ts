import { api } from './client'

export interface RBucket {
  bucket: string
  count: number
  share: number
}

export interface RMultiplesResult {
  days: number
  sample_size: number
  risk_unit: number
  expectancy_r: number
  pct_ge_1r: number
  pct_le_minus_1r: number
  min_r: number
  max_r: number
  histogram: RBucket[]
  error?: string
}

export async function getRMultiplesDistribution(
  params: { days?: number } = {},
): Promise<RMultiplesResult> {
  const resp = await api.get('/api/r-multiples/distribution', { params })
  return resp.data
}
