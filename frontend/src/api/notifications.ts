import { api } from './client'
import type { NotificationLogPage } from '../types'

export async function getNotifications(
  params: {
    severity?: string
    q?: string
    success?: boolean
    from_date?: string
    to_date?: string
    page?: number
    page_size?: number
  } = {},
): Promise<NotificationLogPage> {
  const resp = await api.get('/api/notifications', { params })
  return resp.data
}
