import { api } from './client'
import type { StatusData, StatusHistory, StrategyConfig } from '../types'

export async function getStrategy(): Promise<StrategyConfig> {
  const resp = await api.get('/api/strategy')
  return resp.data
}

export async function updateStrategy(data: Partial<StrategyConfig>): Promise<StrategyConfig> {
  const resp = await api.put('/api/strategy', data)
  return resp.data
}

export async function getStatus(): Promise<StatusData> {
  const resp = await api.get('/api/status')
  return resp.data
}

export async function getStatusHistory(limit = 200): Promise<StatusHistory> {
  const resp = await api.get('/api/status/history', { params: { limit } })
  return resp.data
}
