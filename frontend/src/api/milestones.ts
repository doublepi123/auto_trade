import { api } from './client'
import type { StatisticsQuality } from '../types'

export interface Milestone {
  level: number
  trade_index: number
  direction: string
}

export interface MilestoneResult {
  symbol: string
  lookback_days: number
  sample_size: number
  step: number
  final_cumulative_pnl: number
  total_milestones: number
  up_milestones: number
  down_milestones: number
  milestones: Milestone[]
  pace: {
    avg_up_pace: number | null
    avg_down_pace: number | null
    up_acceleration: string
    down_acceleration: string
  }
  statistics_quality: StatisticsQuality
  error?: string
}

export async function getMilestones(
  params: { symbol?: string; lookback_days?: number; step?: number } = {},
): Promise<MilestoneResult> {
  const resp = await api.get('/api/milestones/track', { params })
  return resp.data
}
