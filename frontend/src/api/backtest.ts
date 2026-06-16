import { api } from './client'
import type {
  BacktestResult,
  BacktestRunRequest,
  BacktestSweepRequest,
  BacktestSweepResult,
  WalkForwardRequest,
  WalkForwardResult,
  StressTestRequest,
  StressTestResult,
  BacktestRunOut,
  BacktestRunPage,
  BacktestRunSaveRequest,
  BacktestRunCompare,
} from '../types'

export async function runBacktest(payload: BacktestRunRequest): Promise<BacktestResult> {
  const resp = await api.post('/api/backtest/run', payload, { timeout: 120000 })
  return resp.data
}

export async function runBacktestSweep(payload: BacktestSweepRequest): Promise<BacktestSweepResult> {
  const resp = await api.post('/api/backtest/sweep', payload, { timeout: 120000 })
  return resp.data
}

export async function runWalkForward(payload: WalkForwardRequest): Promise<WalkForwardResult> {
  const resp = await api.post('/api/backtest/walk-forward', payload, { timeout: 120000 })
  return resp.data
}

export async function runStressTest(payload: StressTestRequest): Promise<StressTestResult> {
  const resp = await api.post('/api/backtest/stress', payload, { timeout: 120000 })
  return resp.data
}

export async function saveBacktestRun(payload: BacktestRunSaveRequest): Promise<BacktestRunOut> {
  const resp = await api.post('/api/backtest/runs', payload)
  return resp.data
}

export async function listBacktestRuns(): Promise<BacktestRunPage> {
  const resp = await api.get('/api/backtest/runs')
  return resp.data
}

export async function compareBacktestRuns(ids: number[]): Promise<BacktestRunCompare> {
  const resp = await api.get('/api/backtest/runs/compare', { params: { ids } })
  return resp.data
}

export async function deleteBacktestRun(id: number): Promise<void> {
  await api.delete(`/api/backtest/runs/${id}`)
}



