import { api } from './client'
import type { EquityCurveResponse } from '../types'

export interface EquityCurveQuery {
  symbol?: string
  days?: number
}

export async function getEquityCurve(params: EquityCurveQuery = {}): Promise<EquityCurveResponse> {
  const resp = await api.get('/api/equity/curve', { params })
  return resp.data
}
