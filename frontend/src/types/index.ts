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
  fee_rate_us: number
  fee_rate_hk: number
  min_repricing_pct: number
  llm_action_cooldown_seconds: number
  trading_session_mode: 'ANY' | 'RTH_ONLY'
  margin_safety_factor: number
  updated_at: string
}

export interface NotificationChannel {
  type: 'serverchan' | 'webhook'
  severity_floor: 'INFO' | 'WARNING' | 'CRITICAL'
  url?: string
  /** Optional JSON payload template for webhook channels.
   *  Supports the tokens {title} {content} {severity} {timestamp} {source}.
   *  Must be a JSON object string starting with '{'. Unknown tokens
   *  are rejected at the backend and the channel falls back to the
   *  default payload. */
  template?: string
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
  notification_channels?: NotificationChannel[]
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
  trading_session_mode: 'ANY' | 'RTH_ONLY'
  is_trading_hours: boolean
}

export interface StatusHistoryPoint {
  symbol: string
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

export interface DiagnosticQuoteStream {
  last_push_age_seconds: number | null
  last_quote_age_seconds: number | null
  recent_quote_count: number
}

export interface DiagnosticRiskState {
  paused: boolean
  kill_switch: boolean
  pause_reason: string
  daily_pnl: number
  consecutive_losses: number
}

export interface QuoteQuality {
  has_quote: boolean
  price_positive: boolean
  spread_reasonable: boolean
  last_price?: number | null
  bid?: number | null
  ask?: number | null
}

export interface DiagnosticSymbolRuntime {
  symbol: string
  market: string
  is_primary: boolean
  engine_state: string
  last_price: number
  last_trigger_price: number
  recent_quote_count: number
  has_pending_order: boolean
  quote_quality?: QuoteQuality | null
}

export interface DiagnosticsResponse {
  runner_running: boolean
  thread_alive: boolean
  quotes_subscribed: boolean
  trigger_in_flight: boolean
  pending_order_symbols: string[]
  quote_stream: DiagnosticQuoteStream
  risk: DiagnosticRiskState
  symbol_runtimes: DiagnosticSymbolRuntime[]
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
  source: 'trade' | 'audit'
  event_type: string
  symbol: string
  broker_order_id: string
  side: string
  status: string
  message: string
  payload: Record<string, unknown>
  created_at: string
  actor_hash?: string | null
  source_ip?: string | null
  severity?: string | null
  result?: string | null
}

export interface TradeEventPage {
  items: TradeEventRecord[]
  total: number
  page: number
  page_size: number
}

export type TimelineSource = 'all' | 'trade' | 'audit'

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

export interface LLMBudgetStatus {
  max_symbols_per_cycle: number
  max_analyses_per_hour: number
  tracked_symbol_count: number
  effective_symbol_budget: number
  used_analyses_last_hour: number
  remaining_analyses_this_hour: number
}

export interface LLMSymbolStatus {
  symbol: string
  market: string
  is_primary: boolean
  has_pending_order: boolean
  buy_cooldown_remaining_seconds: number | null
  sell_cooldown_remaining_seconds: number | null
  last_analysis_at: string | null
  next_analysis_at: string | null
  last_status: string | null
  last_skip_reason: string | null
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
  budget: LLMBudgetStatus
  symbol_statuses: LLMSymbolStatus[]
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
  category?: string | null
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

export interface ReviewLLMInteraction {
  id: number
  interaction_type: string
  symbol: string
  market: string
  success: boolean
  order_action: string
  order_status: string | null
  order_id: string | null
  applied: boolean
  created_at: string
}

export interface ReviewOrder {
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

export interface ReviewEvent {
  id: number
  event_type: string
  symbol: string
  broker_order_id: string
  side: string
  status: string
  message: string
  payload_json: string
  created_at: string
}

export interface ReviewSnapshot {
  id: number
  engine_state: string
  daily_pnl: number
  consecutive_losses: number
  last_price: number
  last_trigger_price: number
  created_at: string
}

export interface ReviewDay {
  date: string
  symbol: string
  llm_interactions: ReviewLLMInteraction[]
  orders: ReviewOrder[]
  events: ReviewEvent[]
  snapshots: ReviewSnapshot[]
  daily_pnl: number
  trade_count: number
  error_tags: string[]
}

export interface ReviewResponse {
  symbol: string
  from_date: string
  to_date: string
  days: ReviewDay[]
  total_pnl: number
  total_trades: number
  all_error_tags: string[]
}

export interface ReportMetrics {
  total_pnl: number
  total_trades: number
  win_count: number
  loss_count: number
  win_rate: number
  profit_loss_ratio: number
  avg_pnl_per_trade: number
  max_profit: number
  max_loss: number
  max_drawdown: number
  llm_suggestions_count: number
  llm_applied_count: number
  llm_apply_rate: number
  llm_profitable_count: number
  llm_accuracy_rate: number
}

export interface ReportDailyPoint {
  date: string
  pnl: number
  cumulative_pnl: number
  drawdown: number
  trade_count: number
  win_count: number
}

export interface ReportAttributionPoint {
  key: string
  label: string
  trade_count: number
  pnl: number
  win_rate: number
  share: number
}

export interface ReportOrderDetail {
  broker_order_id: string
  side: string
  quantity: number
  executed_price: number
  status: string
  filled_at: string | null
  pnl: number
}

export interface ReportDayDetail {
  date: string
  orders: ReportOrderDetail[]
}

export interface ReportResponse {
  period_type: string
  symbol: string
  start_date: string
  end_date: string
  metrics: ReportMetrics
  daily_points: ReportDailyPoint[]
  attribution: ReportAttributionPoint[]
  details: ReportDayDetail[]
}

export interface WatchlistItem {
  id: number
  symbol: string
  market: 'US' | 'HK'
  alias: string
  is_active: boolean
  created_at: string
}

export interface WatchlistQuote {
  symbol: string
  last_price: number
  bid: number
  ask: number
  timestamp: string
}

export interface WatchlistSnapshot {
  symbol: string
  market: 'US' | 'HK'
  alias: string
  is_trading_target: boolean
  last_price: number
  bid: number
  ask: number
  timestamp: string
}

export interface PromptVersion {
  id: number
  name: string
  version: string
  description: string
  template: string
  is_active: boolean
  created_at: string
}

export interface PromptVersionCreate {
  name: string
  version: string
  description?: string
  template: string
}

export interface ExperimentSummary {
  variant_name: string
  total_count: number
  profitable_count: number
  avg_pnl: number
  win_rate: number
}

export interface PerformanceStats {
  total_trades: number
  win_rate: number
  total_pnl: number
  avg_pnl: number
}

export interface PerformanceVariant {
  variant: string
  total_trades: number
  win_rate: number
  total_pnl: number
  avg_pnl: number
}

export interface MacdValue {
  macd: number
  signal: number
  histogram: number
}

export interface VolumeAnalysis {
  avg_volume: number
  volume_ratio: number
  trend: string
}

export interface SentimentValue {
  sentiment: string
  score: number
  description: string
}

export interface MultiTimeframe {
  daily_trend: string
  minute_trend: string
  aligned: boolean
  description: string
}

export interface IndicatorsResponse {
  available: boolean
  symbol: string
  market: string
  atr: number | null
  rsi: number | null
  macd: MacdValue | null
  volume_analysis: VolumeAnalysis | null
  sentiment: SentimentValue | null
  multi_timeframe: MultiTimeframe | null
  bb_upper: number | null
  bb_middle: number | null
  bb_lower: number | null
}


export interface StrategyExperimentGridRange {
  start: number
  end: number
  step: number
}

export interface StrategyExperimentGridItem {
  value?: number
  values?: number[]
  range?: StrategyExperimentGridRange
}

export type StrategyExperimentGrid = Partial<
  Record<keyof BacktestParams, StrategyExperimentGridItem>
>

export interface StrategyExperimentCreate {
  name: string
  symbol: string
  base_params: BacktestParams
  parameter_grid: StrategyExperimentGrid
}

export interface StrategyExperimentRunRequest {
  csv_text?: string | null
}

export type StrategyExperimentStatus =
  | 'PENDING'
  | 'RUNNING'
  | 'COMPLETED'
  | 'FAILED'

export interface StrategyExperiment {
  id: number
  name: string
  symbol: string
  status: StrategyExperimentStatus
  estimated_runs: number
  completed_runs: number
  failed_runs: number
  error: string
  base_params_json: string
  parameter_grid_json: string
  created_at: string
  completed_at: string | null
}

export interface StrategyExperimentRun {
  id: number
  experiment_id: number
  parameters: Partial<BacktestParams>
  status: 'COMPLETED' | 'FAILED'
  total_pnl: number
  total_return_pct: number
  max_drawdown_pct: number
  win_rate: number
  trade_count: number
  closed_trade_count: number
  sharpe_ratio: number | null
  profit_factor: number | null
  profit_loss_ratio: number | null
  result_summary_json: string
  error: string
  created_at: string
}

export interface StrategyExperimentRunPage {
  items: StrategyExperimentRun[]
  total: number
  page: number
  page_size: number
}

export interface LLMEvaluationSample {
  interaction_id: number
  created_at: string
  order_action: string
  order_price: number | null
  tag: string
  reason: string
  metrics: Record<string, unknown>
}
export interface LLMEvaluationResponse {
  symbol: string
  horizon_minutes: number
  sample_count: number
  tag_distribution: Record<string, number>
  hit_rate: number
  samples: LLMEvaluationSample[]
}
