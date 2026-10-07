import { api } from './client'
import type {
  OpeningMomentumExecutionStatus,
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
