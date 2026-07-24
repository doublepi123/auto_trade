import { api } from './client'
import type {
  OpeningMomentumShadowRun,
  OpeningMomentumShadowStatus,
} from '../types'

export async function getOpeningMomentumShadowStatus(): Promise<OpeningMomentumShadowStatus> {
  const response = await api.get('/api/opening-momentum-shadow/status')
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
