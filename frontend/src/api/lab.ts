import { api } from './client'
import type {
  PromptVersion,
  PromptVersionCreate,
  ExperimentSummary,
  PerformanceStats,
  PerformanceVariant,
  IndicatorsResponse,
} from '../types'

export async function listPromptVersions(): Promise<PromptVersion[]> {
  const resp = await api.get('/api/experiments/versions')
  return resp.data
}

export async function createPromptVersion(payload: PromptVersionCreate): Promise<PromptVersion> {
  const resp = await api.post('/api/experiments/versions', payload)
  return resp.data
}

export async function activatePromptVersion(id: number): Promise<void> {
  await api.post(`/api/experiments/versions/${id}/activate`)
}

export async function listExperimentNames(): Promise<string[]> {
  const resp = await api.get('/api/experiments')
  return resp.data
}

export async function getExperimentSummary(name: string): Promise<ExperimentSummary[]> {
  const resp = await api.get(`/api/experiments/${encodeURIComponent(name)}/summary`)
  return resp.data
}

export async function getPerformanceStats(experiment: string): Promise<PerformanceStats> {
  const resp = await api.get('/api/performance/stats', { params: { experiment } })
  return resp.data
}

export async function comparePerformanceVariants(experiment: string): Promise<PerformanceVariant[]> {
  const resp = await api.get('/api/performance/compare', { params: { experiment } })
  return resp.data
}

export async function getPerformanceRecommendations(experiment: string): Promise<string[]> {
  const resp = await api.get('/api/performance/recommendations', { params: { experiment } })
  return resp.data
}

export async function getIndicators(symbol?: string): Promise<IndicatorsResponse> {
  const resp = await api.get('/api/indicators', { params: symbol ? { symbol } : {} })
  return resp.data
}
