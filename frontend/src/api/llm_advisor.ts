import { api } from './client'
import type { LLMIntervalStatus, LLMPreviewAnalyzeRequest, LLMAnalyzeResponse } from '../types'

export async function getLLMIntervalStatus(): Promise<LLMIntervalStatus> {
  const resp = await api.get('/api/strategy/llm-interval/status')
  return resp.data
}

export async function analyzeLLMInterval(force = false): Promise<LLMAnalyzeResponse> {
  const resp = await api.post('/api/strategy/llm-interval/analyze', { force }, { timeout: 90000 })
  return resp.data
}

export async function previewLLMInterval(params: LLMPreviewAnalyzeRequest): Promise<LLMAnalyzeResponse> {
  const resp = await api.post('/api/strategy/llm-interval/preview', params, { timeout: 90000 })
  return resp.data
}

export async function enableLLMInterval(): Promise<void> {
  await api.put('/api/strategy/llm-interval/enable')
}

export async function disableLLMInterval(): Promise<void> {
  await api.put('/api/strategy/llm-interval/disable')
}
