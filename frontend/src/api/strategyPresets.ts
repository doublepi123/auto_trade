import { api } from './client'
import type {
  StrategyPreset,
  StrategyPresetCreate,
  StrategyPresetPage,
  StrategyPresetApplyResult,
} from '../types'

export async function listStrategyPresets(): Promise<StrategyPresetPage> {
  const resp = await api.get('/api/strategy-presets')
  return resp.data
}

export async function createStrategyPreset(payload: StrategyPresetCreate): Promise<StrategyPreset> {
  const resp = await api.post('/api/strategy-presets', payload)
  return resp.data
}

export async function deleteStrategyPreset(id: number): Promise<void> {
  await api.delete(`/api/strategy-presets/${id}`)
}

export async function applyStrategyPreset(id: number): Promise<StrategyPresetApplyResult> {
  const resp = await api.post(`/api/strategy-presets/${id}/apply`)
  return resp.data
}
