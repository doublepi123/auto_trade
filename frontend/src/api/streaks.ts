import { api } from './client'

export interface StreakDistribution {
  length: number
  count: number
}

export interface StreakGroup {
  count: number
  max: number
  avg: number
  distribution: StreakDistribution[]
}

export interface StreakResult {
  symbol: string
  lookback_days: number
  sample_size: number
  win_rate: number
  current_streak: { type: string; length: number }
  win_streaks: StreakGroup
  loss_streaks: StreakGroup
  probability: {
    win_streak_3: number
    win_streak_5: number
    loss_streak_3: number
    loss_streak_5: number
  }
  error?: string
}

export async function getStreakAnalysis(
  params: { symbol?: string; lookback_days?: number } = {},
): Promise<StreakResult> {
  const resp = await api.get('/api/streaks/analyze', { params })
  return resp.data
}
