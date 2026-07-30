import { api } from './client'

export interface SampleStats {
  mean_pnl: number
  win_rate: number
  total_pnl: number
  best_trade: number
  worst_trade: number
}

export interface PnlDistribution {
  mean: number
  median: number
  p5: number
  p25: number
  p75: number
  p95: number
  min: number
  max: number
}

export interface DrawdownDistribution {
  mean: number
  median: number
  p95: number
  max: number
}

export interface MonteCarloResult {
  symbol: string
  lookback_days: number
  sample_size: number
  n_simulations: number
  sim_trades: number
  sample_stats: SampleStats
  final_pnl_distribution: PnlDistribution
  max_drawdown_distribution: DrawdownDistribution
  ruin_probability: number
  profit_probability: number
  error?: string
}

export async function getMonteCarloSimulation(
  params: {
    symbol?: string
    lookback_days?: number
    n_simulations?: number
    n_trades?: number
    seed?: number
  } = {},
): Promise<MonteCarloResult> {
  const resp = await api.get('/api/monte-carlo/simulate', { params })
  return resp.data
}
