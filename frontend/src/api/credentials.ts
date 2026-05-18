import { api } from './client'
import type { CredentialsConfig } from '../types'

export async function getCredentials(): Promise<CredentialsConfig> {
  const resp = await api.get('/api/credentials')
  return resp.data
}

export async function updateCredentials(data: Partial<CredentialsConfig>): Promise<CredentialsConfig> {
  const resp = await api.put('/api/credentials', data)
  return resp.data
}
