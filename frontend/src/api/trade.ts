import { api } from './client'
import type {
  AccountInfo,
  OrderCancelAllResult,
  OrderCancelResult,
  OrderPage,
  StatisticsQuality,
} from '../types'

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

export async function cancelAllOrders(symbol?: string): Promise<OrderCancelAllResult> {
  const resp = await api.post('/api/orders/cancel-all', symbol ? { symbol } : undefined)
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

export async function enableProtectiveExits(): Promise<{ message: string }> {
  const resp = await api.post('/api/control/protective-exit/enable')
  return resp.data
}

export async function disableProtectiveExits(): Promise<{ message: string }> {
  const resp = await api.post('/api/control/protective-exit/disable')
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

export interface MetricsValueSummary {
  trade_count: number
  win_rate: number
  profit_factor: number | null
  sharpe_ratio: number | null
  avg_pnl: number
  total_pnl: number
  max_drawdown: number
  max_drawdown_amount: number
}

export interface MetricsCurrencySummary extends MetricsValueSummary {
  currency: 'USD' | 'HKD'
}

export interface MetricsSummary {
  trade_count: number
  win_rate: number
  profit_factor: number | null
  sharpe_ratio: number | null
  avg_pnl?: number | null
  total_pnl?: number | null
  max_drawdown?: number | null
  max_drawdown_amount?: number | null
  window_days: number
  currency?: 'USD' | 'HKD' | 'MIXED' | null
  totals_comparable?: boolean
  by_currency?: MetricsCurrencySummary[]
  statistics_quality?: StatisticsQuality
}

export interface GetMetricsParams {
  days?: number
}

export async function getMetricsSummary(params: GetMetricsParams = {}): Promise<MetricsSummary> {
  const resp = await api.get('/api/metrics/summary', { params })
  return resp.data
}
