import axios from 'axios'
import type { AccountInfo, CredentialsConfig, StrategyConfig, StatusData, OrderRecord } from '../types'

const api = axios.create({ baseURL: '', timeout: 10000 })

api.interceptors.request.use((config) => {
  const key = localStorage.getItem('api_key')
  if (key) {
    config.headers['X-API-Key'] = key
  }
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response) {
      const { status } = error.response
      if (status === 401) {
        localStorage.removeItem('api_key')
        if (!_notified401) {
          _notified401 = true
          window.dispatchEvent(new CustomEvent('api-key-required'))
          setTimeout(() => { _notified401 = false }, 1000)
        }
      }
    }
    return Promise.reject(error)
  },
)

let _notified401 = false

export async function getStrategy(): Promise<StrategyConfig> {
  const resp = await api.get('/api/strategy')
  return resp.data
}

export async function updateStrategy(data: Partial<StrategyConfig>): Promise<StrategyConfig> {
  const resp = await api.put('/api/strategy', data)
  return resp.data
}

export async function getCredentials(): Promise<CredentialsConfig> {
  const resp = await api.get('/api/credentials')
  return resp.data
}

export async function updateCredentials(data: Partial<CredentialsConfig>): Promise<CredentialsConfig> {
  const resp = await api.put('/api/credentials', data)
  return resp.data
}

export async function getStatus(): Promise<StatusData> {
  const resp = await api.get('/api/status')
  return resp.data
}

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

export async function getAccount(): Promise<AccountInfo> {
  const resp = await api.get('/api/account')
  return resp.data
}
