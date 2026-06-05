import { api } from './client'
import type { DiagnosticsResponse, StatusData, StatusHistory, StrategyConfig } from '../types'

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

export async function getStatusHistory(options: {
  limit?: number
  symbol?: string
  from?: string
  to?: string
} = {}): Promise<StatusHistory> {
  const resp = await api.get('/api/status/history', { params: options })
  return resp.data
}

export async function getDiagnostics(): Promise<DiagnosticsResponse> {
  const resp = await api.get('/api/diagnostics')
  return resp.data
}
