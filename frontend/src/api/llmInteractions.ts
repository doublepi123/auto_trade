import { api } from './client'
import type { LLMInteractionDetail } from '../types'

export async function getLLMInteraction(id: number): Promise<LLMInteractionDetail> {
  const resp = await api.get(`/api/llm-interactions/${id}`)
  return resp.data
}
