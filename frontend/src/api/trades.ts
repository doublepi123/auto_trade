import { api } from './client'
import type {
  ClosedTradePage,
  TradeCalendarResponse,
  TradeHoldDurationResponse,
  TradeMonthlySummaryResponse,
  TradePnlDistributionResponse,
  TradeStats,
  TradeWeekdayAttributionResponse,
} from '../types'

export interface ClosedTradeQuery {
  symbol?: string
  from_date?: string
  to_date?: string
  limit?: number
}

export async function getClosedTrades(params: ClosedTradeQuery = {}): Promise<ClosedTradePage> {
  const resp = await api.get('/api/trades', { params })
  return resp.data
}

export async function exportClosedTrades(params: ClosedTradeQuery = {}): Promise<Blob> {
  const resp = await api.get('/api/trades/export', {
    params: { ...params, format: 'csv' },
    responseType: 'blob',
  })
  return resp.data
}

export interface TradeStatsQuery {
  symbol?: string
  days?: number
}

export async function getTradeStats(params: TradeStatsQuery = {}): Promise<TradeStats> {
  const resp = await api.get('/api/trades/stats', { params })
  return resp.data
}

export interface TradeAnalyticsQuery {
  symbol?: string
  from_date?: string
  to_date?: string
}

export async function getTradeCalendar(params: TradeAnalyticsQuery = {}): Promise<TradeCalendarResponse> {
  const resp = await api.get('/api/trades/analytics/calendar', { params })
  return resp.data
}

export async function getTradeHoldDuration(params: TradeAnalyticsQuery = {}): Promise<TradeHoldDurationResponse> {
  const resp = await api.get('/api/trades/analytics/hold-duration', { params })
  return resp.data
}

export async function getTradePnlDistribution(params: TradeAnalyticsQuery = {}): Promise<TradePnlDistributionResponse> {
  const resp = await api.get('/api/trades/analytics/pnl-distribution', { params })
  return resp.data
}

export async function getTradeMonthlySummary(params: TradeAnalyticsQuery = {}): Promise<TradeMonthlySummaryResponse> {
  const resp = await api.get('/api/trades/analytics/monthly', { params })
  return resp.data
}

export async function getTradeWeekdayAttribution(params: TradeAnalyticsQuery = {}): Promise<TradeWeekdayAttributionResponse> {
  const resp = await api.get('/api/trades/analytics/weekday', { params })
  return resp.data
}
