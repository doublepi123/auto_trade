export interface StrategyConfig {
  id: number
  symbol: string
  market: 'US' | 'HK'
  buy_low: number
  sell_high: number
  short_selling: boolean
  min_profit_amount: number
  max_daily_loss: number
  max_consecutive_losses: number
  llm_interval_minutes: number
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
  reload_warning?: string | null
}

export interface StatusData {
  engine_state: string
  paused: boolean
  kill_switch: boolean
  runner_running: boolean
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
  executed_quantity: number | null
  executed_price: number | null
  status: string
  created_at: string
  filled_at: string | null
}

export interface CashBalance {
  currency: string
  available_cash: number
  frozen_cash: number
}

export interface Position {
  symbol: string
  side: string
  quantity: number
  avg_price: number
  market_value: number
}

export interface AccountInfo {
  total_assets: number
  cash_balances: CashBalance[]
  positions: Position[]
  available: boolean
  error?: string | null
}

export interface LLMSuggestion {
  buy_low: number
  sell_high: number
  confidence_score: number
  analysis: string
}

export interface LLMIntervalStatus {
  enabled: boolean
  interval_minutes: number
  last_analysis_at: string | null
  next_analysis_at: string | null
  current_suggestion: LLMSuggestion | null
  applied_values: {
    buy_low: number
    sell_high: number
  } | null
  reject_reason: string | null
}

export interface LLMPreviewAnalyzeRequest {
  symbol: string
  market: 'US' | 'HK'
  current_price?: number | null
  current_buy_low?: number | null
  current_sell_high?: number | null
  min_profit_amount?: number | null
  short_selling: boolean
}

export interface LLMAnalyzeResponse {
  success: boolean
  applied: boolean
  reason: string
  suggested_buy_low?: number | null
  suggested_sell_high?: number | null
  confidence_score?: number | null
  analysis?: string | null
  next_analysis_at?: string | null
  applied_at?: string | null
}
