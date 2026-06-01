import { api } from './client'
import type {
  LLMEvaluationResponse,
  StrategyExperiment,
  StrategyExperimentCreate,
  StrategyExperimentRun,
  StrategyExperimentRunPage,
  StrategyExperimentRunRequest,
} from '../types'

export async function createStrategyExperiment(
  payload: StrategyExperimentCreate,
): Promise<StrategyExperiment> {
  const resp = await api.post('/api/strategy-experiments', payload)
  return resp.data
}

export async function listStrategyExperiments(): Promise<StrategyExperiment[]> {
  const resp = await api.get('/api/strategy-experiments')
  return resp.data
}

export async function getStrategyExperiment(id: number): Promise<StrategyExperiment> {
  const resp = await api.get(`/api/strategy-experiments/${id}`)
  return resp.data
}

export async function runStrategyExperiment(
  id: number,
  payload: StrategyExperimentRunRequest,
): Promise<StrategyExperiment> {
  const resp = await api.post(`/api/strategy-experiments/${id}/run`, payload)
  return resp.data
}

export async function listStrategyExperimentRuns(
  id: number,
  params: { sort: string; order: 'asc' | 'desc'; page: number; page_size: number },
): Promise<StrategyExperimentRunPage> {
  const resp = await api.get(`/api/strategy-experiments/${id}/runs`, { params })
  return resp.data
}

export async function getStrategyExperimentRun(
  experimentId: number,
  runId: number,
): Promise<StrategyExperimentRun> {
  const resp = await api.get(`/api/strategy-experiments/${experimentId}/runs/${runId}`)
  return resp.data
}
export async function exportStrategyExperiment(
  experimentId: number,
  format: 'csv' | 'json' = 'json',
): Promise<Blob | Record<string, unknown>> {
  const resp = await api.get(`/api/strategy-experiments/${experimentId}/export`, {
    params: { format },
    responseType: format === 'csv' ? 'blob' : 'json',
  })
  return resp.data
}
export async function getLLMEvaluations(
  symbol: string,
  params?: { start?: string; end?: string; horizon_minutes?: number },
): Promise<LLMEvaluationResponse> {
  const resp = await api.get('/api/strategy-experiments/llm-evaluations', {
    params: { symbol, ...params },
  })
  return resp.data
}
