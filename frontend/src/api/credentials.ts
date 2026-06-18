import { api } from './client'
import type { CredentialsConfig, NotificationChannel } from '../types'

export async function getCredentials(): Promise<CredentialsConfig> {
  const resp = await api.get('/api/credentials')
  return resp.data
}

export async function updateCredentials(data: Partial<CredentialsConfig>): Promise<CredentialsConfig> {
  const resp = await api.put('/api/credentials', data)
  return resp.data
}

export async function testNotificationChannel(channel: NotificationChannel): Promise<{ ok: boolean; error?: string }> {
  const resp = await api.post('/api/credentials/notification-channels/test', channel)
  return resp.data
}
