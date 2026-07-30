import { api } from './client'

export interface TagStat {
  tag: string
  trade_count: number
  total_pnl: number
  avg_pnl: number
  win_rate: number
  avg_rating: number | null
}

export interface TagAnalyticsResult {
  total_notes: number
  unique_tags: number
  qualifying_tags: number
  tags: TagStat[]
  best_tag: TagStat | null
  worst_tag: TagStat | null
  error?: string
}

export async function getTagAnalytics(
  params: { min_trades?: number } = {},
): Promise<TagAnalyticsResult> {
  const resp = await api.get('/api/tag-analytics/performance', { params })
  return resp.data
}
