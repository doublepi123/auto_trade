import { api } from './client'
import type {
  UniverseCatalogItem,
  UniverseSelectionRefreshResponse,
  UniverseSelectionRunResponse,
} from '../types'

function assertObject(value: unknown, endpoint: string): asserts value is Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`Unexpected ${endpoint} response`)
  }
}

export async function getUniverseCatalog(): Promise<UniverseCatalogItem[]> {
  const resp = await api.get('/api/universe/catalog')
  if (!Array.isArray(resp.data)) {
    throw new Error('Unexpected /api/universe/catalog response')
  }
  return resp.data as UniverseCatalogItem[]
}

export async function getLatestUniverseSelection(): Promise<UniverseSelectionRunResponse> {
  const resp = await api.get('/api/universe/latest')
  assertObject(resp.data, '/api/universe/latest')
  if (!Array.isArray(resp.data.items)) {
    throw new Error('Unexpected /api/universe/latest response: items is not an array')
  }
  return resp.data as unknown as UniverseSelectionRunResponse
}

export async function refreshUniverseSelection(): Promise<UniverseSelectionRefreshResponse> {
  const resp = await api.post('/api/universe/refresh', undefined, { timeout: 120_000 })
  assertObject(resp.data, '/api/universe/refresh')
  assertObject(resp.data.run, '/api/universe/refresh.run')
  if (!Array.isArray(resp.data.run.items)) {
    throw new Error('Unexpected /api/universe/refresh response: run.items is not an array')
  }
  for (const field of [
    'added_symbols',
    'removed_symbols',
    'retained_symbols',
    'shadow_enabled_symbols',
    'shadow_disabled_symbols',
    'shadow_failed_symbols',
  ]) {
    if (!Array.isArray(resp.data[field])) {
      throw new Error(`Unexpected /api/universe/refresh response: ${field} is not an array`)
    }
  }
  return resp.data as unknown as UniverseSelectionRefreshResponse
}
