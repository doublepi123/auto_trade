import { api } from './client'
import type { ReportResponse } from '../types'

export async function getDailyReport(params: {
  symbol: string
  date: string
}): Promise<ReportResponse> {
  const resp = await api.get<ReportResponse>('/api/reports/daily', { params })
  return resp.data
}

export async function getWeeklyReport(params: {
  symbol: string
  week_start: string
}): Promise<ReportResponse> {
  const resp = await api.get<ReportResponse>('/api/reports/weekly', { params })
  return resp.data
}

export async function getMonthlyReport(params: {
  symbol: string
  month: string
}): Promise<ReportResponse> {
  const resp = await api.get<ReportResponse>('/api/reports/monthly', { params })
  return resp.data
}

export async function getRangeReport(params: {
  symbol: string
  from_date: string
  to_date: string
}): Promise<ReportResponse> {
  const resp = await api.get<ReportResponse>('/api/reports/range', { params })
  return resp.data
}

export async function exportReport(params: {
  symbol: string
  from_date: string
  to_date: string
  format: 'json' | 'csv'
}): Promise<Blob | unknown> {
  const resp = await api.get('/api/reports/export', {
    params,
    responseType: params.format === 'csv' ? 'blob' : 'json',
  })
  return resp.data
}

export interface ScheduledReportRunResult {
  sent: boolean
  symbol: string
  title: string
  error: string | null
}

export async function runScheduledReportNow(): Promise<ScheduledReportRunResult> {
  const resp = await api.post<ScheduledReportRunResult>('/api/reports/schedule/run')
  return resp.data
}

