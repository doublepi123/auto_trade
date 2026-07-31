import { api } from './client'

export interface SkipCategoryRow {
  category: string
  count: number
  share: number
}

export interface SkipSymbolRow {
  symbol: string
  total: number
  by_category: Record<string, number>
}

export interface SkipReasonGroup {
  category: string
  reasons: { message: string; count: number }[]
}

export interface EventQualityIssue {
  code: string
  count: number
}

export interface EventQuality {
  status: 'COMPLETE' | 'DEGRADED'
  total_event_count: number
  valid_event_count: number
  invalid_event_count: number
  issues: EventQualityIssue[]
}

export interface SkipAnalyticsResult {
  days: number
  sample_size: number
  by_category: SkipCategoryRow[]
  by_symbol: SkipSymbolRow[]
  by_side: Record<string, number>
  top_reasons: SkipReasonGroup[]
  daily: { date: string; count: number }[]
  event_quality: EventQuality
  error?: string
}

export async function getSkipAnalyticsSummary(
  params: { days?: number } = {},
): Promise<SkipAnalyticsResult> {
  const resp = await api.get('/api/skip-analytics/summary', { params })
  return resp.data
}
