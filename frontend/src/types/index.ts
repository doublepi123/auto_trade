export interface StrategyConfig {
  id: number
  symbol: string
  market: 'US' | 'HK'
  buy_low: number
  sell_high: number
  short_selling: boolean
  min_profit_amount: number
  auto_resume_minutes: number
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
  last_action_message: string
}

export interface StatusHistoryPoint {
  timestamp: string
  engine_state: string
  paused: boolean
  kill_switch: boolean
  daily_pnl: number
  consecutive_losses: number
  last_price: number
  last_trigger_price: number
}

export interface TradeSignalMarker {
  timestamp: string
  broker_order_id: string
  symbol: string
  side: string
  quantity: number
  price: number
  status: string
}

export interface StatusHistory {
  points: StatusHistoryPoint[]
  markers: TradeSignalMarker[]
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
  source: string
  cancellable: boolean
}

export interface OrderPage {
  items: OrderRecord[]
  total: number
  page: number
  page_size: number
  scope: 'today' | 'history'
}

export interface OrderCancelResult {
  broker_order_id: string
  status: string
  message: string
}

export interface TradeEventRecord {
  id: number
  event_type: string
  symbol: string
  broker_order_id: string
  side: string
  status: string
  message: string
  payload: Record<string, unknown>
  created_at: string
}

export interface TradeEventPage {
  items: TradeEventRecord[]
  total: number
  page: number
  page_size: number
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
  interaction_id?: number | null
  suggested_buy_low?: number | null
  suggested_sell_high?: number | null
  confidence_score?: number | null
  analysis?: string | null
  next_analysis_at?: string | null
  applied_at?: string | null
  order_action?: string | null
  order_price?: number | null
  replacement_action?: string | null
  replacement_price?: number | null
  order_reason?: string | null
  order_status?: string | null
  order_id?: string | null
}

export interface LLMInteractionRecord {
  id: number
  interaction_type: string
  symbol: string
  market: string
  success: boolean
  error: string
  order_action: string
  order_status: string | null
  order_id: string | null
  applied: boolean
  created_at: string
}

export interface BacktestParams {
  symbol: string
  buy_low: number
  sell_high: number
  short_selling: boolean
  min_profit_amount: number
  max_daily_loss: number
  max_consecutive_losses: number
  quantity: number
  initial_cash: number
  fee_rate: number
  fixed_fee: number
  slippage_pct: number
  stop_loss_pct: number
}

export interface BacktestRunRequest {
  params: BacktestParams
  csv_text?: string | null
}

export interface BacktestTradeLog {
  timestamp: string
  action: string
  price: number
  quantity: number
  fee: number
  pnl: number
  state_after: string
  reason: string
  holding_minutes: number | null
}

export interface BacktestSkippedSignal {
  timestamp: string
  action: string
  price: number
  reason: string
  state: string
}

export interface BacktestEquityPoint {
  timestamp: string
  close: number
  equity: number
  realized_pnl: number
  unrealized_pnl: number
  drawdown_pct: number
  position: string
}

export interface BacktestMetrics {
  initial_cash: number
  final_equity: number
  total_pnl: number
  total_return_pct: number
  max_drawdown_pct: number
  trade_count: number
  closed_trade_count: number
  winning_trades: number
  losing_trades: number
  win_rate: number
  avg_holding_minutes: number
  fees_paid: number
  skipped_signals: number
  final_state: string
}

export interface BacktestFeeSensitivityPoint {
  fee_rate: number
  total_pnl: number
  total_return_pct: number
  max_drawdown_pct: number
}

export interface BacktestResult {
  params: BacktestParams
  metrics: BacktestMetrics
  equity_curve: BacktestEquityPoint[]
  trades: BacktestTradeLog[]
  skipped_signals: BacktestSkippedSignal[]
  fee_sensitivity: BacktestFeeSensitivityPoint[]
}
