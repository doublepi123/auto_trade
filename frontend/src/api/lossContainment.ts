import { api } from './client'

export interface LossCauseRow {
  exit_cause: string
  count: number
  total_loss: number
  avg_loss: number
  share_of_loss: number | null
}

export interface LossBucket {
  bucket: string
  count: number
}

export interface LossContainmentResult {
  days: number
  sample_size: number
  total_loss: number
  median_loss: number
  mean_loss: number
  worst_loss: number
  worst_to_median: number | null
  top3_loss_share: number | null
  tail_breach_count: number
  tail_breach_pct: number
  by_exit_cause: LossCauseRow[]
  histogram: LossBucket[]
  error?: string
}

export async function getLossContainmentSummary(
  params: { days?: number } = {},
): Promise<LossContainmentResult> {
  const resp = await api.get('/api/loss-containment/summary', { params })
  return resp.data
}
