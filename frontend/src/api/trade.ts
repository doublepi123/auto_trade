import { api } from './client'
import type { AccountInfo, OrderRecord } from '../types'

export async function getOrders(limit = 50): Promise<OrderRecord[]> {
  const resp = await api.get('/api/orders', { params: { limit } })
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

export async function startTrading(): Promise<{ message: string }> {
  const resp = await api.post('/api/control/start')
  return resp.data
}

export async function stopTrading(reason = 'manual'): Promise<{ message: string }> {
  const resp = await api.post('/api/control/stop', { reason })
  return resp.data
}

export async function getAccount(): Promise<AccountInfo> {
  const resp = await api.get('/api/account')
  return resp.data
}
