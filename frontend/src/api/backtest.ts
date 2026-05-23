import { api } from './client'
import type { BacktestResult, BacktestRunRequest } from '../types'

export async function runBacktest(payload: BacktestRunRequest): Promise<BacktestResult> {
  const resp = await api.post('/api/backtest/run', payload)
  return resp.data
}
