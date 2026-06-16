import { api } from './client'
import type { RiskHistoryResponse } from '../types'

export async function getRiskHistory(
  params: { symbol?: string; limit?: number } = {},
): Promise<RiskHistoryResponse> {
  const resp = await api.get('/api/risk/history', { params })
  return resp.data
}
