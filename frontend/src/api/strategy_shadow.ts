import { api } from './client'
import type {
  StrategyShadowAdxChallengerRequest,
  StrategyShadowAdxChallengerResponse,
  StrategyShadowConfig,
  StrategyShadowConfigUpdate,
  StrategyShadowDecisionPage,
  StrategyShadowStatus,
  StrategyShadowEvaluation,
  StrategyShadowVersion,
} from '../types'

export async function evaluateStrategyShadowAdxChallengers(
  payload: StrategyShadowAdxChallengerRequest,
): Promise<StrategyShadowAdxChallengerResponse> {
  const response = await api.post('/api/strategy-shadow/adx-challengers', payload, {
    timeout: 120_000,
  })
  return response.data
}

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

export async function getStrategyShadowVersions(symbol?: string): Promise<StrategyShadowVersion[]> {
  const response = await api.get('/api/strategy-shadow/versions', {
    params: symbol ? { symbol } : {},
  })
  return response.data
}

export async function getStrategyShadowEvaluation(
  symbol?: string,
  configVersion?: string,
): Promise<StrategyShadowEvaluation> {
  const response = await api.get('/api/strategy-shadow/evaluation', {
    params: {
      ...(symbol ? { symbol } : {}),
      ...(configVersion ? { config_version: configVersion } : {}),
    },
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
  config_version?: string
} = {}): Promise<StrategyShadowDecisionPage> {
  const response = await api.get('/api/strategy-shadow/decisions', { params })
  return response.data
}
