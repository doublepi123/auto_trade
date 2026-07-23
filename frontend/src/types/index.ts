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
  max_drawdown_amount: number | null
  max_consecutive_losses: number
  llm_interval_minutes: number
  fee_rate_us: number
  fee_rate_hk: number
  min_repricing_pct: number
  llm_action_cooldown_seconds: number
  trading_session_mode: 'ANY' | 'RTH_ONLY'
  margin_safety_factor: number
  allow_position_addons: boolean
  max_position_quantity: number
  max_position_notional: number
  max_risk_per_trade: number
  stop_loss_pct: number
  max_holding_minutes: number
  entry_cutoff_minutes_before_close: number
  flatten_minutes_before_close: number
  llm_order_execution_enabled: boolean
  report_schedule_enabled: boolean
  report_schedule_interval_hours: number
  report_schedule_symbol: string
  updated_at: string
}

export interface NotificationChannel {
  type: 'serverchan' | 'webhook' | 'telegram'
  severity_floor: 'INFO' | 'WARNING' | 'CRITICAL'
  url?: string
  /** Optional JSON payload template for webhook channels.
   *  Supports the tokens {title} {content} {severity} {timestamp} {source}.
   *  Must be a JSON object string starting with '{'. Unknown tokens
   *  are rejected at the backend and the channel falls back to the
   *  default payload. */
  template?: string
  bot_token?: string
  chat_id?: string
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
  protective_exit_permitted: boolean
  runner_running: boolean
  daily_pnl: number
  consecutive_losses: number
  cumulative_realized_pnl: number
  peak_realized_pnl: number
  drawdown_amount: number
  max_drawdown_amount: number | null
  last_price: number
  last_trigger_price: number
  last_trigger_at: string | null
  last_action_message: string
  trading_session_mode: 'ANY' | 'RTH_ONLY'
  is_trading_hours: boolean
  execution_state: 'IDLE' | 'REDUCING'
  reduction_reason: string
  reduction_started_at: string | null
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
  protective_exit_permitted: boolean
  daily_pnl: number
  consecutive_losses: number
}

export interface QuoteQuality {
  has_quote: boolean
  price_positive: boolean
  spread_reasonable: boolean
  last_bbo_consistent: boolean
  source_timestamp_fresh: boolean
  last_price?: number | null
  bid?: number | null
  ask?: number | null
}

export interface DiagnosticSymbolRuntime {
  symbol: string
  market: string
  is_primary: boolean
  trading_enabled: boolean
  engine_state: string
  last_price: number
  last_trigger_price: number
  recent_quote_count: number
  has_pending_order: boolean
  quote_quality?: QuoteQuality | null
  position_quantity: number
  position_avg_price: number
  position_notional: number
  position_risk_at_stop: number
  position_limit_breaches: string[]
}

export interface DiagnosticLiveSafety {
  full_buying_power_usage_enabled: boolean
  buying_power_usage_pct: number
  short_entries_enabled: boolean
  allow_position_addons: boolean
  max_position_quantity: number
  max_position_notional: number
  max_risk_per_trade: number
  stop_loss_pct: number
  max_holding_minutes: number
  entry_cutoff_minutes_before_close: number
  flatten_minutes_before_close: number
  llm_shadow_mode: boolean
  llm_order_execution_enabled: boolean
  live_regime_gate_enabled: boolean
  live_regime_max_data_age_seconds: number
  live_max_entries_per_symbol_per_day: number
}

export interface DiagnosticsResponse {
  runner_running: boolean
  thread_alive: boolean
  quotes_subscribed: boolean
  trigger_in_flight: boolean
  pending_order_symbols: string[]
  pending_order_ids: string[]
  unrepresentable_live_order_issues: string[]
  order_sync_succeeded: boolean
  execution_state: 'IDLE' | 'REDUCING'
  reduction_reason: string
  live_safety: DiagnosticLiveSafety
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
  decision_bid: number | null
  decision_ask: number | null
  quote_age_ms: number | null
  config_version: string
  ack_latency_ms: number | null
  fill_latency_ms: number | null
  estimated_fee: number | null
  actual_fee: number | null
  fee_currency: string
  fee_source: 'ACTUAL' | 'ESTIMATED' | 'UNKNOWN'
  slippage_amount: number | null
  slippage_bps: number | null
  exit_cause: string
  exit_reason: string
  gross_pnl: number | null
  net_pnl: number | null
  pnl_source: 'TRACKED_ENTRY' | 'BROKER_POSITION' | 'LEDGER_REPLAY' | 'UNKNOWN'
  cost_basis_price: number | null
  cost_basis_quantity: number | null
  cost_basis_opened_at: string | null
  position_quantity_before: number | null
  pnl_fee: number | null
  pnl_fee_source: 'ACTUAL' | 'MIXED' | 'ESTIMATED' | 'UNKNOWN'
  pnl_fee_rate: number | null
}

export interface TradeNote {
  id: number
  order_id: number
  symbol: string
  note: string
  tags: string[]
  rating: number | null
  created_at: string
  updated_at: string
}

export interface TradeNoteUpsert {
  note: string
  tags: string[]
  rating: number | null
}

export interface TradeNotePage {
  items: TradeNote[]
  total: number
  page: number
  page_size: number
}

export interface TradeNoteTagCount {
  tag: string
  count: number
}

export interface TradeNoteAnalytics {
  total: number
  rated_count: number
  avg_rating: number | null
  rating_distribution: Record<number, number>
  top_tags: TradeNoteTagCount[]
  distinct_symbols: number
}

export interface LLMInteractionDetail {
  id: number
  interaction_type: string
  symbol: string
  market: string
  prompt: string
  raw_response: string
  parsed_response: Record<string, unknown>
  context_snapshot: Record<string, unknown>
  success: boolean
  error: string
  order_action: string
  order_status: string | null
  order_id: string | null
  applied: boolean
  prompt_variant: string | null
  created_at: string
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

export interface OrderCancelFailure {
  order_id: string
  error: string
}

export interface OrderCancelAllResult {
  cancelled: number
  failed: OrderCancelFailure[]
  skipped: number
  total_pending: number
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

export type TimelineSource = 'all' | 'trade' | 'audit' | 'llm' | 'risk'

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
  shadow_mode: boolean
  policy_status: 'SHADOW' | 'LIVE'
  interval_minutes: number
  last_analysis_at: string | null
  next_analysis_at: string | null
  current_suggestion: LLMSuggestion | null
  applied_values: {
    buy_low: number
    sell_high: number
  } | null
  last_applied_values: {
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
  prompt_tokens?: number | null
  completion_tokens?: number | null
  total_tokens?: number | null
  created_at: string
}

export interface LLMUsageDailySummary {
  date: string
  interactions: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}

export interface LLMUsageTypeSummary {
  interaction_type: string
  interactions: number
  total_tokens: number
}

export interface LLMUsageSummary {
  days: number
  total_interactions: number
  successful_interactions: number
  total_prompt_tokens: number
  total_completion_tokens: number
  total_tokens: number
  by_day: LLMUsageDailySummary[]
  by_type: LLMUsageTypeSummary[]
}

export interface BacktestParams {
  symbol: string
  buy_low: number
  sell_high: number
  short_selling: boolean
  min_profit_amount: number
  max_daily_loss: number
  max_drawdown_amount?: number
  max_consecutive_losses: number
  quantity: number
  initial_cash: number
  fee_rate: number
  fixed_fee: number
  slippage_pct: number
  stop_loss_pct: number
  trailing_stop_pct?: number
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
  gross_pnl: number | null
  net_pnl: number | null
  total_fees: number | null
  mfe_amount: number | null
  mae_amount: number | null
  mfe_pct: number | null
  mae_pct: number | null
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
  sharpe_ratio: number | null
  sortino_ratio: number | null
  calmar_ratio: number | null
  profit_factor: number | null
  profit_loss_ratio: number | null
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

export interface BacktestExportRequest {
  result: BacktestResult
  sections?: string[]
}

/** Parameter axes a sweep may explore (mirrors backend SWEEP_ALLOWED_GRID_KEYS). */
export type BacktestSweepAxis =
  | 'buy_low'
  | 'sell_high'
  | 'min_profit_amount'
  | 'quantity'
  | 'fee_rate'
  | 'slippage_pct'
  | 'stop_loss_pct'

export type BacktestSweepGrid = Partial<Record<BacktestSweepAxis, StrategyExperimentGridItem>>

export type BacktestSweepSortBy =
  | 'sharpe_ratio'
  | 'sortino_ratio'
  | 'calmar_ratio'
  | 'profit_factor'
  | 'total_return_pct'

export interface BacktestSweepHeatmapCell {
  buy_low: number
  sell_high: number
  value: number | null
}

export interface BacktestSweepHeatmap {
  x_axis: string
  y_axis: string
  z_metric: string
  cells: BacktestSweepHeatmapCell[]
}

export interface BacktestSweepRow {
  params: BacktestParams
  metrics: BacktestMetrics
  rank: number
}

export interface BacktestSweepResult {
  rows: BacktestSweepRow[]
  best: BacktestSweepRow | null
  heatmap: BacktestSweepHeatmap
  evaluated_count: number
  skipped_count: number
  sort_by: string
}

export interface BacktestSweepRequest {
  base: BacktestParams
  grid: BacktestSweepGrid
  sort_by: BacktestSweepSortBy
  max_combinations: number
  csv_text?: string | null
}

export interface WalkForwardWindow {
  index: number
  start: string
  end: string
  train_size: number
  test_size: number
  best_params: BacktestParams | null
  test_metrics: BacktestMetrics | null
}

export interface WalkForwardSummary {
  window_count: number
  evaluated_window_count: number
  mean_test_return_pct: number | null
  median_test_return_pct: number | null
  mean_test_metric: number | null
  profitable_window_pct: number | null
  test_return_std_pct: number | null
}

export interface WalkForwardResult {
  windows: WalkForwardWindow[]
  summary: WalkForwardSummary
  sort_by: string
  train_size: number
  test_size: number
  step: number
}

export interface WalkForwardRequest {
  base: BacktestParams
  grid: BacktestSweepGrid
  train_size: number
  test_size: number
  step?: number | null
  sort_by: BacktestSweepSortBy
  max_combinations: number
  csv_text?: string | null
}

export interface PositionPnlRow {
  symbol: string
  quantity: number
  avg_entry_cost: number
  last_price: number | null
  unrealized_pnl: number
  unrealized_pnl_pct: number | null
  market_value: number
  cost_value: number
  has_quote: boolean
}

export interface PositionPnlResult {
  positions: PositionPnlRow[]
  total_unrealized_pnl: number
  total_cost_basis: number
  total_unrealized_pnl_pct: number | null
  available: boolean
  error: string | null
}

export interface ClosedTrade {
  symbol: string
  side: string
  entry_order_id: number
  exit_order_id: number
  entry_at: string
  exit_at: string
  entry_price: number
  exit_price: number
  quantity: number
  gross_pnl: number
  est_fees: number
  net_pnl: number
  holding_seconds: number
  fee_source: 'ACTUAL' | 'MIXED' | 'ESTIMATED'
  actual_fees: number | null
  slippage_amount: number | null
  slippage_bps: number | null
  ack_latency_ms: number | null
  fill_latency_ms: number | null
  exit_cause: string
  exit_reason: string
  mfe_amount: number | null
  mae_amount: number | null
  mfe_pct: number | null
  mae_pct: number | null
}

export interface ClosedTradePage {
  items: ClosedTrade[]
  total: number
  statistics_quality: StatisticsQuality
}

export interface TradeStats {
  total_trades: number
  win_count: number
  loss_count: number
  breakeven_count: number
  win_rate: number
  total_gross_pnl: number
  total_net_pnl: number
  avg_win: number | null
  avg_loss: number | null
  expectancy: number
  profit_factor: number | null
  payoff_ratio: number | null
  largest_win: number | null
  largest_loss: number | null
  current_streak_type: string
  current_streak_count: number
  max_win_streak: number
  max_loss_streak: number
  avg_hold_seconds: number | null
  total_fees: number
  actual_fee_coverage_pct: number
  avg_slippage_bps: number | null
  avg_ack_latency_ms: number | null
  statistics_quality: StatisticsQuality
}

export interface TradeCalendarDay {
  date: string
  trade_count: number
  win_count: number
  loss_count: number
  net_pnl: number
  gross_pnl: number
  symbols: string[]
}

export interface TradeCalendarResponse {
  items: TradeCalendarDay[]
  total_trades: number
  total_net_pnl: number
  statistics_quality: StatisticsQuality
}

export interface TradeHoldDurationBucket {
  bucket: string
  min_seconds: number | null
  max_seconds: number | null
  trade_count: number
  win_count: number
  loss_count: number
  win_rate: number
  net_pnl: number
  avg_net_pnl: number | null
}

export interface TradeHoldDurationResponse {
  items: TradeHoldDurationBucket[]
  total_trades: number
  statistics_quality: StatisticsQuality
}

export interface TradePnlDistributionBucket {
  bucket: string
  min_pnl: number | null
  max_pnl: number | null
  trade_count: number
  net_pnl: number
}

export interface TradePnlDistributionResponse {
  items: TradePnlDistributionBucket[]
  total_trades: number
  total_net_pnl: number
  statistics_quality: StatisticsQuality
}

export interface TradeMonthlySummaryRow {
  month: string
  trade_count: number
  win_count: number
  loss_count: number
  win_rate: number
  net_pnl: number
  gross_pnl: number
  cumulative_pnl: number
  drawdown: number
}

export interface TradeMonthlySummaryResponse {
  items: TradeMonthlySummaryRow[]
  total_trades: number
  total_net_pnl: number
  statistics_quality: StatisticsQuality
}

export interface TradeWeekdayAttributionRow {
  weekday: number
  label: string
  trade_count: number
  win_count: number
  loss_count: number
  win_rate: number
  net_pnl: number
  avg_net_pnl: number | null
}

export interface TradeWeekdayAttributionResponse {
  items: TradeWeekdayAttributionRow[]
  total_trades: number
  total_net_pnl: number
  statistics_quality: StatisticsQuality
}

export interface EquityCurvePoint {
  date: string
  realized_pnl: number
  cumulative_pnl: number
  drawdown: number
  trade_count: number
}

export interface EquityCurveResponse {
  points: EquityCurvePoint[]
  total_realized_pnl: number
  max_drawdown: number
  statistics_quality: StatisticsQuality
}

export interface SymbolAttributionRow {
  symbol: string
  realized_pnl: number
  trade_count: number
  win_count: number
  win_rate: number
  contribution_share: number
  largest_win: number | null
  largest_loss: number | null
}

export interface SymbolAttributionResponse {
  rows: SymbolAttributionRow[]
  total_realized_pnl: number
  statistics_quality: StatisticsQuality
}

export interface StressTestResult {
  scenarios_run: number
  baseline_return_pct: number | null
  median_return_pct: number | null
  p5_return_pct: number | null
  p95_return_pct: number | null
  worst_return_pct: number | null
  worst_drawdown_pct: number | null
  profitable_scenario_pct: number | null
  jitter_pct: number
  seed: number
  returns: number[]
}

export interface StressTestRequest {
  base: BacktestParams
  scenarios: number
  jitter_pct: number
  seed: number
  csv_text?: string | null
}

export interface BacktestRunOut {
  id: number
  name: string
  symbol: string
  params: BacktestParams
  metrics: BacktestMetrics
  created_at: string
}

export interface BacktestRunPage {
  items: BacktestRunOut[]
  total: number
  page: number
  page_size: number
}

export interface BacktestRunSaveRequest {
  name: string
  params: BacktestParams
  metrics: BacktestMetrics
}

export interface BacktestRunCompare {
  runs: BacktestRunOut[]
}

export type AlertRuleType = 'price_above' | 'price_below' | 'daily_loss'
export type AlertSeverity = 'INFO' | 'WARNING' | 'CRITICAL'

export interface AlertRule {
  id: number
  name: string
  symbol: string
  rule_type: AlertRuleType
  threshold: number
  severity: AlertSeverity
  enabled: boolean
  cooldown_seconds: number
  last_fired_at: string | null
  created_at: string
}

export interface AlertRuleCreate {
  name: string
  symbol: string
  rule_type: AlertRuleType
  threshold: number
  severity: AlertSeverity
  enabled: boolean
  cooldown_seconds: number
}

export interface AlertRulePage {
  items: AlertRule[]
  total: number
}

export interface AlertEvaluateResult {
  evaluated: number
  fired: number
  skipped_cooldown: number
}

export interface AlertFiring {
  id: number
  rule_id: number
  symbol: string
  rule_type: string
  threshold: number
  trigger_value: number
  severity: string
  message: string
  fired_at: string
}

export interface AlertFiringPage {
  items: AlertFiring[]
  total: number
}

export interface StrategyPreset {
  id: number
  name: string
  params: Record<string, unknown>
  created_at: string
}

export interface StrategyPresetCreate {
  name: string
  params: Record<string, unknown>
}

export interface StrategyPresetPage {
  items: StrategyPreset[]
  total: number
}

export interface StrategyPresetApplyResult {
  applied: boolean
  changed: string[]
}

export interface NotificationLogOut {
  id: number
  title: string
  content: string
  severity: string
  success: boolean
  error: string
  created_at: string
}

export interface NotificationLogPage {
  items: NotificationLogOut[]
  total: number
  page: number
  page_size: number
}

export interface RiskHistoryPoint {
  created_at: string
  engine_state: string
  paused: boolean
  kill_switch: boolean
  daily_pnl: number
  consecutive_losses: number
}

export interface RiskHistoryResponse {
  points: RiskHistoryPoint[]
  latest: RiskHistoryPoint | null
}

export interface BrokerCandleBar {
  timestamp: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface BrokerCandlesResponse {
  symbol: string
  period: string
  count: number
  bars: BrokerCandleBar[]
  csv_text: string
}

export type MarketSessionPhase = 'rth' | 'pre' | 'post' | 'lunch' | 'closed'

export interface MarketSessionStatus {
  market: string
  symbol: string
  status: MarketSessionPhase
  is_trading: boolean
  local_time: string
  utc_time: string
  next_open: string
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
  included_in_statistics: boolean
  statistics_quality: StatisticsQuality
}

export interface ReviewResponse {
  symbol: string
  from_date: string
  to_date: string
  days: ReviewDay[]
  total_pnl: number
  total_trades: number
  all_error_tags: string[]
  statistics_quality: StatisticsQuality
}

export type StatisticsQualityStatus =
  | 'COMPLETE'
  | 'KNOWN_EXCLUSIONS'
  | 'UNRESOLVED'
  | 'STALE_EXCLUSION'

export interface StatisticsQualityItem {
  trade_day: string
  symbol: string
  issue_code: string
  exit_order_id: number
  broker_order_id: string
  side: string
  filled_quantity: number
  matched_quantity: number
  unmatched_quantity: number
  exclusion_id: number | null
  reason: string
}

export interface StatisticsQuality {
  status: StatisticsQualityStatus
  known_exclusion_count: number
  unresolved_issue_count: number
  omitted_day_count: number
  items: StatisticsQualityItem[]
}

export interface ReportMetrics {
  total_pnl: number
  total_trades: number
  win_count: number
  loss_count: number
  win_rate: number
  profit_loss_ratio: number
  avg_pnl_per_trade: number
  max_profit: number | null
  max_loss: number | null
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
  statistics_quality: StatisticsQuality
}

export interface WatchlistItem {
  id: number
  symbol: string
  market: 'US' | 'HK'
  alias: string
  source: string
  is_active: boolean
  is_trading_target: boolean
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

export interface UniverseCatalogItem {
  symbol: string
  market: 'US' | 'HK'
  alias: string
  sector: string
  memberships: string[]
}

export interface UniverseSelectionMetrics {
  price: number | null
  avg_dollar_volume: number | null
  relative_spread_bps: number | null
  realized_vol_20d: number | null
  atr_pct_14d: number | null
  momentum_5d_pct: number | null
  trend_efficiency_10d: number | null
  opportunity_to_cost_ratio: number | null
}

export interface UniverseSelectionItem {
  symbol: string
  market: 'US' | 'HK'
  alias: string
  sector: string
  memberships: string[]
  selected: boolean
  shadow_enabled: boolean
  is_trading_target: boolean
  rank: number | null
  score: number
  metrics: UniverseSelectionMetrics
  exclusion_reasons: string[]
  created_at: string
}

export interface UniverseSelectionRunResponse {
  id: number
  as_of_date: string
  algorithm_version: string
  source_version: string
  status: string
  candidate_count: number
  evaluable_count: number
  selected_count: number
  coverage_ratio: number
  parameters: Record<string, unknown>
  error: string
  started_at: string
  completed_at: string | null
  created_at: string
  items: UniverseSelectionItem[]
}

export interface UniverseSelectionRefreshResponse {
  run: UniverseSelectionRunResponse
  added_symbols: string[]
  removed_symbols: string[]
  retained_symbols: string[]
  shadow_enabled_symbols: string[]
  shadow_disabled_symbols: string[]
  shadow_failed_symbols: string[]
  applied: boolean
  reason: string
}

export type UniversePromotionForwardStatus =
  | 'NOT_REGISTERED'
  | 'FROZEN'
  | 'COLLECTING'
  | 'READY_FOR_REVIEW'
  | 'MATURE_EVIDENCE'
  | 'BLOCKED'

export interface UniversePromotionReadinessItem {
  symbol: string
  rank: number
  selection_score: number
  priority_rank: number
  priority_score: number
  quant_weight: number
  quant_score: number | null
  quant_confidence: number | null
  quant_recommended_action: string
  quant_source: string
  quant_fresh: boolean
  quant_expires_at: string | null
  is_trading_target: boolean
  shadow_enabled: boolean
  forward_status: UniversePromotionForwardStatus
  included_pairs: number
  minimum_ready_pairs: number
  minimum_mature_pairs: number
  remaining_ready_pairs: number
  remaining_mature_pairs: number
  blockers: string[]
  baseline_metrics: StrategyShadowMetrics
  candidate_metrics: StrategyShadowMetrics
  review_ready: boolean
  mature_evidence: boolean
  automatic_promotion_allowed: false
}

export interface UniversePromotionReadinessResponse {
  universe_run_id: number
  as_of_date: string
  generated_at: string
  priority_algorithm_version: string
  items: UniversePromotionReadinessItem[]
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

export type StrategyShadowMode = 'SHADOW'
export type StrategyShadowPosition = 'FLAT' | 'LONG'

export interface StrategyShadowConfig {
  enabled: boolean
  symbol: string
  zscore_window_1m_bars: number
  zscore_window_5m_bars: number
  breach_zscore: number
  reclaim_zscore: number
  five_minute_zscore_max: number
  adx_period: number
  max_adx: number
  realized_vol_window_bars: number
  min_realized_vol: number
  max_realized_vol: number
  stop_loss_pct: number
  profit_target_pct: number
  max_holding_minutes: number
  entry_cutoff_minutes_before_close: number
  flatten_minutes_before_close: number
  arm_ttl_bars: number
  max_entries_per_day: number
  entry_cooldown_minutes: number
  slippage_bps: number
  estimated_fee_rate_us: number
  estimated_fee_rate_hk: number
  algorithm_version: 'strategy-v2-rth-mr-v4-frozen-config'
  mode: StrategyShadowMode
  order_submission_allowed: boolean
  allow_position_addons: boolean
  short_entries_enabled: boolean
  config_version: string
  updated_at: string
}

export interface StrategyShadowConfigUpdate {
  enabled: boolean
  zscore_window_1m_bars: number
  zscore_window_5m_bars: number
  breach_zscore: number
  reclaim_zscore: number
  five_minute_zscore_max: number
  adx_period: number
  max_adx: number
  realized_vol_window_bars: number
  min_realized_vol: number
  max_realized_vol: number
  stop_loss_pct: number
  profit_target_pct: number
}

export interface StrategyShadowLatest {
  observed_at: string
  data_age_seconds: number
  bar_timestamp_1m: string | null
  bar_timestamp_5m: string | null
  price: number
  vwap_1m: number | null
  zscore_1m: number | null
  vwap_5m: number | null
  zscore_5m: number | null
  adx: number | null
  realized_vol: number | null
  regime_eligible: boolean
  breach_armed: boolean
  virtual_position: StrategyShadowPosition
  virtual_entry_price: number | null
  virtual_entry_at: string | null
  last_action: string
  last_reason: string
}

export interface StrategyShadowMetrics {
  bars: number
  eligible_bars: number
  breaches: number
  reclaims: number
  entries: number
  exits: number
  closed_trades: number
  win_rate: number
  gross_pnl: number
  fees: number
  net_pnl: number
  max_drawdown: number
  avg_holding_minutes: number
  avg_mae_pct: number
  avg_mfe_pct: number
  comparison_available: boolean
  live_action_count: number | null
  action_agreement_rate: number | null
  net_pnl_delta_vs_live: number | null
}

export interface StrategyShadowStatus {
  config: StrategyShadowConfig
  evidence_config_version: string
  version_transition_pending: boolean
  latest: StrategyShadowLatest | null
  metrics: StrategyShadowMetrics
  gate_counts: Record<string, number>
  phase: string
  last_polled_at: string | null
  last_poll_error: string
}

export interface StrategyShadowDecision {
  id: number
  symbol: string
  config_version: string
  observed_at: string
  bar_timestamp_1m: string | null
  bar_timestamp_5m: string | null
  price: number
  vwap_1m: number | null
  zscore_1m: number | null
  vwap_5m: number | null
  zscore_5m: number | null
  adx: number | null
  realized_vol: number | null
  regime_eligible: boolean
  breach_armed: boolean
  action: string
  reason: string
  virtual_position: StrategyShadowPosition
  reference_price: number | null
  quantity: number
  gross_pnl: number | null
  fee: number | null
  net_pnl: number | null
  exit_reason: string | null
  holding_minutes: number | null
  mae_pct: number | null
  mfe_pct: number | null
}

export interface StrategyShadowDecisionPage {
  items: StrategyShadowDecision[]
  total: number
  page: number
  page_size: number
}

export interface StrategyShadowVersion {
  symbol: string
  config_version: string
  activated_at: string
  current: boolean
  params: Record<string, unknown>
  observed_trading_days: number
  bars: number
  closed_trades: number
  net_pnl: number
}

export interface StrategyShadowHourlyEligibility {
  session_hour: number
  bars: number
  ready_bars: number
  eligible_bars: number
  gate_counts: Record<string, number>
}

export interface StrategyShadowDailyEvidence {
  session_date: string
  first_bar_at: string
  last_bar_at: string
  first_ready_at: string | null
  bars: number
  ready_bars: number
  warmup_lost_bars: number
  eligible_bars: number
  hourly_eligibility: StrategyShadowHourlyEligibility[]
  expected_internal_bars: number
  missing_internal_bars: number
  incomplete_feature_bars: number
  coverage_ratio: number
  trades: number
  net_pnl: number
  exit_reasons: Record<string, number>
  partial_start: boolean
  partial_end: boolean
  outside_session_bars: number
  complete_session: boolean
}

export interface StrategyShadowEvaluation {
  symbol: string
  config_version: string
  mode: StrategyShadowMode
  order_submission_allowed: false
  status: 'COLLECTING' | 'READY_FOR_REVIEW'
  observed_trading_days: number
  excluded_trading_days: number
  minimum_trading_days: number
  minimum_session_coverage_ratio: number
  remaining_trading_days: number
  closed_trades: number
  eligible_closed_trades: number
  excluded_closed_trades: number
  minimum_closed_trades: number
  remaining_closed_trades: number
  first_bar_at: string | null
  last_bar_at: string | null
  bars: number
  readiness_blockers: string[]
  data_quality_warnings: string[]
  quality: Record<string, unknown> | null
  daily: StrategyShadowDailyEvidence[]
}

export interface StrategyShadowAdxChallengerRequest {
  symbol: string
  config_version?: string
}

export interface StrategyShadowAdxChallengerDaily {
  session_date: string
  bars: number
  eligible_bars: number
  breaches: number
  reclaims: number
  closed_trades: number
  net_pnl: number
  max_drawdown: number
  exit_reasons: Record<string, number>
}

export interface StrategyShadowAdxChallengerCandidate {
  label: 'BASELINE' | 'CHALLENGER'
  max_adx: number
  config_version: string
  metrics: StrategyShadowMetrics
  daily: StrategyShadowAdxChallengerDaily[]
}

export interface StrategyShadowWarmupDaily {
  session_date: string
  seed_session_date: string
  trend_context_cutoff_at: string
  overnight_gap_pct: number
  first_ready_at: string | null
  bars: number
  ready_bars: number
  warmup_lost_bars: number
  eligible_bars: number
  hourly_eligibility: StrategyShadowHourlyEligibility[]
}

export interface StrategyShadowWarmupVariant {
  label: 'SESSION_LOCAL' | 'CAUSAL_TREND_PREWARM'
  warmup_scope: 'NONE' | 'ADX_VOL_ONLY'
  source_config_version: string
  metrics: StrategyShadowMetrics
  daily: StrategyShadowWarmupDaily[]
}

export interface StrategyShadowWarmupDiagnostic {
  algorithm_version: 'strategy-v2-causal-trend-prewarm-v1'
  status: 'INSUFFICIENT_EVIDENCE' | 'READY_FOR_REVIEW' | 'BLOCKED'
  minimum_causal_pairs: number
  observed_causal_pairs: number
  evaluated_causal_pairs: number
  blockers: string[]
  same_sample: true
  causal_history_only: true
  vwap_zscore_session_local: true
  variants: StrategyShadowWarmupVariant[]
}

export type StrategyShadowForwardValidationStatus =
  | 'NOT_REGISTERED'
  | 'FROZEN'
  | 'COLLECTING'
  | 'READY_FOR_REVIEW'
  | 'MATURE_EVIDENCE'
  | 'BLOCKED'

export interface StrategyShadowForwardValidationRegistration {
  id: number
  symbol: string
  market: 'US' | 'HK'
  market_timezone: string
  candidate_algorithm_version: 'strategy-v2-causal-trend-prewarm-v1'
  source_config_version: string
  evaluator_digest: string
  registered_at: string
  eligible_after: string
  minimum_ready_pairs: number
  minimum_mature_pairs: number
}

export interface StrategyShadowForwardValidationDaily {
  target_session_date: string
  seed_session_date: string | null
  target_open_at: string
  evaluated_at: string
  disposition: 'INCLUDED' | 'EXCLUDED'
  exclusion_reason: string
  structural_failure: boolean
  target_bars: number
  target_bars_sha256: string | null
  seed_bars_sha256: string
  baseline_input_sha256: string
  candidate_input_sha256: string
  same_target_bars: boolean | null
  baseline_replay_match: boolean | null
  session_local_invariant: boolean | null
  baseline: StrategyShadowWarmupDaily | null
  candidate: StrategyShadowWarmupDaily | null
  baseline_metrics: StrategyShadowMetrics | null
  candidate_metrics: StrategyShadowMetrics | null
  baseline_result_sha256: string
  candidate_result_sha256: string
  evidence_digest_sha256: string
}

export interface StrategyShadowForwardValidationResponse {
  registration: StrategyShadowForwardValidationRegistration | null
  status: StrategyShadowForwardValidationStatus
  mode: StrategyShadowMode
  order_submission_allowed: false
  automatic_promotion_allowed: false
  historical_target_backfill_allowed: false
  evaluation_scope: 'FORWARD_OUT_OF_SAMPLE'
  included_pairs: number
  excluded_targets: number
  minimum_ready_pairs: number
  minimum_mature_pairs: number
  remaining_ready_pairs: number
  remaining_mature_pairs: number
  blockers: string[]
  baseline_metrics: StrategyShadowMetrics
  candidate_metrics: StrategyShadowMetrics
  daily: StrategyShadowForwardValidationDaily[]
}

export interface StrategyShadowForwardValidationRegisterRequest {
  symbol: string
  source_config_version: string
  candidate_algorithm_version: 'strategy-v2-causal-trend-prewarm-v1'
  confirm_forward_only: true
  confirm_no_automatic_promotion: true
}

export interface StrategyShadowAdxChallengerResponse {
  persisted: false
  mode: StrategyShadowMode
  order_submission_allowed: false
  evaluation_scope: 'EXPLORATORY_IN_SAMPLE'
  promotion_eligible: false
  forward_validation_required: true
  symbol: string
  source_config_version: string
  status: 'INSUFFICIENT_EVIDENCE' | 'READY_FOR_REVIEW' | 'BLOCKED'
  minimum_complete_sessions: number
  observed_complete_sessions: number
  evaluated_complete_sessions: number
  baseline_replay_match: boolean | null
  blockers: string[]
  candidates: StrategyShadowAdxChallengerCandidate[]
  warmup_diagnostic: StrategyShadowWarmupDiagnostic | null
}
