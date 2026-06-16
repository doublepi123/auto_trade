import { api } from './client'
import type { PositionPnlResult } from '../types'

export async function getPositionPnl(): Promise<PositionPnlResult> {
  const resp = await api.get('/api/positions/pnl')
  return resp.data
}
