import { api } from './client'
import type { NotificationLogOut, NotificationLogPage, NotificationStatsResponse } from '../types'

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

/** Read-only server-side delivery aggregate. Never triggers a send or retry. */
export async function getNotificationStats(
  params: {
    severity?: string
    from_date?: string
    to_date?: string
  } = {},
): Promise<NotificationStatsResponse> {
  const resp = await api.get('/api/notifications/stats', { params })
  return resp.data
}

export async function retryNotification(id: number): Promise<NotificationLogOut> {
  const resp = await api.post(`/api/notifications/${id}/retry`)
  return resp.data
}

export async function exportNotifications(
  format: 'csv' | 'json',
  params: {
    severity?: string
    q?: string
    success?: boolean
    from_date?: string
    to_date?: string
  } = {},
): Promise<Blob> {
  const resp = await api.get('/api/notifications/export', {
    params: { ...params, format },
    responseType: 'blob',
  })
  return resp.data
}
