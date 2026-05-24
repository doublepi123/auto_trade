import { api } from './client'
import type { AccountInfo, OrderCancelResult, OrderPage } from '../types'

export interface GetOrdersParams {
  scope?: 'today' | 'history'
  page?: number
  page_size?: number
  limit?: number
  refresh?: boolean
}

export async function getOrders(params: GetOrdersParams | number = {}): Promise<OrderPage> {
  const requestParams = typeof params === 'number' ? { limit: params } : params
  const resp = await api.get('/api/orders', { params: requestParams })
  if (Array.isArray(resp.data)) {
    return {
      items: resp.data,
      total: resp.data.length,
      page: 1,
      page_size: resp.data.length,
      scope: 'history',
    }
  }
  return resp.data
}

export async function cancelOrder(orderId: string): Promise<OrderCancelResult> {
  const resp = await api.post(`/api/orders/${encodeURIComponent(orderId)}/cancel`)
  return resp.data
}

export async function getAccount(): Promise<AccountInfo> {
  const resp = await api.get('/api/account')
  return resp.data
}

export async function pauseTrading(reason = 'manual'): Promise<{ message: string }> {
  const resp = await api.post('/api/control/pause', { reason })
  return resp.data
}

export async function resumeTrading(): Promise<{ message: string }> {
  const resp = await api.post('/api/control/resume')
  return resp.data
}

export async function activateKillSwitch(reason = 'manual'): Promise<{ message: string }> {
  const resp = await api.post('/api/control/kill-switch', { reason })
  return resp.data
}

export async function disableKillSwitch(): Promise<{ message: string }> {
  const resp = await api.post('/api/control/disable-kill-switch')
  return resp.data
}

export async function startTrading(): Promise<{ message: string }> {
  const resp = await api.post('/api/control/start')
  return resp.data
}

export async function stopTrading(reason = 'manual'): Promise<{ message: string }> {
  const resp = await api.post('/api/control/stop', { reason })
  return resp.data
}
