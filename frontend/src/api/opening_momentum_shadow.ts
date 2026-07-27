import { api } from './client'
import type {
  OpeningMomentumExecutionStatus,
  OpeningMomentumShadowRun,
  OpeningMomentumShadowStatus,
} from '../types'

export async function getOpeningMomentumShadowStatus(): Promise<OpeningMomentumShadowStatus> {
  const response = await api.get('/api/opening-momentum-shadow/status')
  return response.data
}

export async function getOpeningMomentumExecutionStatus(): Promise<OpeningMomentumExecutionStatus> {
  const response = await api.get('/api/opening-momentum-shadow/execution/status')
  return response.data
}

export async function getOpeningMomentumShadowRuns(
  limit = 100,
): Promise<OpeningMomentumShadowRun[]> {
  const response = await api.get('/api/opening-momentum-shadow/runs', {
    params: { limit },
  })
  return response.data
}
