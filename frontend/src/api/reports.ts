import { api } from './client'
import type { ReportResponse } from '../types'

export async function getDailyReport(params: {
  symbol: string
  date: string
}) {
  return api.get<ReportResponse>('/api/reports/daily', { params })
}

export async function getWeeklyReport(params: {
  symbol: string
  week_start: string
}) {
  return api.get<ReportResponse>('/api/reports/weekly', { params })
}

export async function getMonthlyReport(params: {
  symbol: string
  month: string
}) {
  return api.get<ReportResponse>('/api/reports/monthly', { params })
}

export async function getRangeReport(params: {
  symbol: string
  from_date: string
  to_date: string
}) {
  return api.get<ReportResponse>('/api/reports/range', { params })
}

export async function exportReport(params: {
  symbol: string
  from_date: string
  to_date: string
  format: 'json' | 'csv'
}) {
  return api.get('/api/reports/export', {
    params,
    responseType: 'blob',
  })
}
