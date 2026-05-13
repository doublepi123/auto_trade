export interface StrategyConfig {
  id: number
  symbol: string
  market: 'US' | 'HK'
  buy_low: number
  sell_high: number
  short_selling: boolean
  max_daily_loss: number
  max_consecutive_losses: number
  updated_at: string
}

export interface CredentialsConfig {
  id: number
  longbridge_app_key: string
  longbridge_app_secret: string
  longbridge_access_token: string
  sct_key: string
  has_longbridge_app_key: boolean
  has_longbridge_app_secret: boolean
  has_longbridge_access_token: boolean
  has_sct_key: boolean
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
