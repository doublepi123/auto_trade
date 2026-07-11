import { api } from './client'
import type {
  StrategyShadowConfig,
  StrategyShadowConfigUpdate,
  StrategyShadowDecisionPage,
  StrategyShadowStatus,
} from '../types'

export async function getStrategyShadowConfig(symbol?: string): Promise<StrategyShadowConfig> {
  const response = await api.get('/api/strategy-shadow/config', {
    params: symbol ? { symbol } : {},
  })
  return response.data
}

export async function getStrategyShadowConfigs(): Promise<StrategyShadowConfig[]> {
  const response = await api.get('/api/strategy-shadow/configs')
  return response.data
}

export async function updateStrategyShadowConfig(
  payload: StrategyShadowConfigUpdate,
  symbol?: string,
): Promise<StrategyShadowConfig> {
  const response = await api.put('/api/strategy-shadow/config', payload, {
    params: symbol ? { symbol } : {},
  })
  return response.data
}

export async function getStrategyShadowStatus(symbol?: string): Promise<StrategyShadowStatus> {
  const response = await api.get('/api/strategy-shadow/status', {
    params: symbol ? { symbol } : {},
  })
  return response.data
}

export async function getStrategyShadowDecisions(params: {
  symbol?: string
  action?: string
  from?: string
  to?: string
  page?: number
  page_size?: number
} = {}): Promise<StrategyShadowDecisionPage> {
  const response = await api.get('/api/strategy-shadow/decisions', { params })
  return response.data
}
