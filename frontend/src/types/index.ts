export interface StrategyConfig {
  id: number
  symbol: string
  market: 'US' | 'HK'
  buy_low: number
  sell_high: number
  short_selling: boolean
  max_daily_loss: number
  max_consecutive_losses: number
  sct_key: string
  updated_at: string
}

export interface StatusData {
  engine_state: string
  paused: boolean
  kill_switch: boolean
  daily_pnl: number
  consecutive_losses: number
  last_price: number
  last_trigger_price: number
  last_trigger_at: string | null
}

export interface OrderRecord {
  id: number
  broker_order_id: string
  symbol: string
  side: string
  quantity: number
  price: number
  status: string
  created_at: string
  filled_at: string | null
}
