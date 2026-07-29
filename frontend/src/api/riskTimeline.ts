import { api } from './client'

export interface RiskCheckStep {
  timestamp: string
  symbol: string
  check_name: string
  passed: boolean
  reason: string
  category: string
  trade_id: number | null
}

export interface RiskSummary {
  total_checks: number
  passed: number
  blocked: number
  by_category: Record<string, { passed: number; blocked: number }>
  recent_blocks: RiskCheckStep[]
}

export async function getRiskChecks(params: { trade_id?: number; symbol?: string; limit?: number } = {}): Promise<RiskCheckStep[]> {
  const resp = await api.get('/api/risk-timeline/checks', { params })
  return resp.data
}

export async function getRiskSummary(hours: number = 24): Promise<RiskSummary> {
  const resp = await api.get('/api/risk-timeline/summary', { params: { hours } })
  return resp.data
}
