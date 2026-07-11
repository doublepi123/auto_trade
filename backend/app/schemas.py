from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_SYMBOL_RE = re.compile(r"^[A-Z0-9\-]{1,12}\.[A-Z]{2,4}$")


def _normalize_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    if "." not in symbol:
        raise ValueError("symbol must include market suffix, e.g. AAPL.US")
    if not _SYMBOL_RE.fullmatch(symbol):
        raise ValueError("symbol must use CODE.MARKET format with letters and numbers only, e.g. AAPL.US")
    return symbol


def _validate_symbol_market_pair(symbol: str, market: str) -> None:
    suffix = symbol.rsplit(".", 1)[-1]
    if suffix != market:
        raise ValueError(
            f"symbol suffix .{suffix} does not match market {market}"
        )


class StrategyConfigSchema(BaseModel):
    # Reject unknown keys so the API surface stays closed: a typo
    # such as ``buyLown`` (camelCase) is a 422 instead of a silent
    # no-op. Strategy update audit diffs also stay clean because
    # Pydantic only forwards known fields to model_dump.
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(default="", max_length=50)
    market: str = Field(default="US")
    buy_low: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    sell_high: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    short_selling: bool = Field(default=False)
    min_profit_amount: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    auto_resume_minutes: Optional[int] = Field(default=None, ge=0, le=1440)
    max_daily_loss: float = Field(default=5000.0, gt=0, allow_inf_nan=False)
    max_consecutive_losses: int = Field(default=3, ge=1, le=100)
    llm_interval_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    fee_rate_us: Optional[float] = Field(default=None, ge=0, le=0.01)
    fee_rate_hk: Optional[float] = Field(default=None, ge=0, le=0.02)
    min_repricing_pct: Optional[float] = Field(default=None, ge=0, le=0.05)
    llm_action_cooldown_seconds: Optional[int] = Field(default=None, ge=0, le=3600)
    trading_session_mode: Literal["RTH_ONLY", "ANY"] = "ANY"
    margin_safety_factor: Optional[float] = Field(default=None, ge=0, le=1)
    allow_position_addons: Optional[bool] = None
    max_position_quantity: Optional[int] = Field(default=None, ge=1, le=1_000_000)
    max_position_notional: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    max_risk_per_trade: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    stop_loss_pct: Optional[float] = Field(default=None, gt=0, le=20, allow_inf_nan=False)
    max_holding_minutes: Optional[int] = Field(default=None, ge=1, le=10_080)
    entry_cutoff_minutes_before_close: Optional[int] = Field(default=None, ge=1, le=180)
    flatten_minutes_before_close: Optional[int] = Field(default=None, ge=1, le=180)
    llm_order_execution_enabled: Optional[bool] = None
    report_schedule_enabled: Optional[bool] = None
    report_schedule_interval_hours: Optional[int] = Field(default=None, ge=1, le=720)
    report_schedule_symbol: Optional[str] = Field(default=None, max_length=50)

    @field_validator("market")
    @classmethod
    def validate_market(cls, v: str) -> str:
        if v not in ("US", "HK"):
            raise ValueError("market must be US or HK")
        return v

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        return _normalize_symbol(v)

    @field_validator("sell_high")
    @classmethod
    def validate_sell_high(cls, v: Optional[float], info: Any) -> Optional[float]:
        if v is None:
            return v
        buy_low = info.data.get("buy_low")
        if buy_low is not None and v <= buy_low:
            raise ValueError("sell_high must be greater than buy_low")
        return v

    @model_validator(mode="after")
    def validate_partial_p0_safety(self) -> "StrategyConfigSchema":
        if self.short_selling:
            raise ValueError("short selling is disabled by the P0 live safety policy")
        if self.allow_position_addons:
            raise ValueError("position add-ons are disabled by the P0 live safety policy")
        if self.llm_order_execution_enabled:
            raise ValueError("LLM live orders are disabled by the P0 live safety policy")
        if (
            self.entry_cutoff_minutes_before_close is not None
            and self.flatten_minutes_before_close is not None
            and self.flatten_minutes_before_close
            > self.entry_cutoff_minutes_before_close
        ):
            raise ValueError(
                "flatten_minutes_before_close must not exceed "
                "entry_cutoff_minutes_before_close"
            )
        if {"symbol", "market"}.issubset(self.model_fields_set):
            _validate_symbol_market_pair(self.symbol, self.market)
        return self


class StrategyMergedSchema(BaseModel):
    symbol: str = Field(default="", max_length=50)
    market: str = Field(default="US")
    buy_low: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    sell_high: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    short_selling: bool = Field(default=False)
    min_profit_amount: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    auto_resume_minutes: int = Field(default=3, ge=0, le=1440)
    max_daily_loss: float = Field(default=5000.0, gt=0, allow_inf_nan=False)
    max_consecutive_losses: int = Field(default=3, ge=1, le=100)
    llm_interval_minutes: int = Field(default=2, ge=1, le=1440)
    fee_rate_us: float = Field(default=0.0005, ge=0, le=0.01)
    fee_rate_hk: float = Field(default=0.003, ge=0, le=0.02)
    min_repricing_pct: float = Field(default=0.003, ge=0, le=0.05)
    llm_action_cooldown_seconds: int = Field(default=60, ge=0, le=3600)
    trading_session_mode: Literal["RTH_ONLY", "ANY"] = "ANY"
    margin_safety_factor: float = Field(default=0.9, ge=0, le=1)
    allow_position_addons: bool = False
    max_position_quantity: int = Field(default=100, ge=1, le=1_000_000)
    max_position_notional: float = Field(default=5000.0, gt=0, allow_inf_nan=False)
    max_risk_per_trade: float = Field(default=250.0, gt=0, allow_inf_nan=False)
    stop_loss_pct: float = Field(default=1.0, gt=0, le=20, allow_inf_nan=False)
    max_holding_minutes: int = Field(default=60, ge=1, le=10_080)
    entry_cutoff_minutes_before_close: int = Field(default=45, ge=1, le=180)
    flatten_minutes_before_close: int = Field(default=15, ge=1, le=180)
    llm_order_execution_enabled: bool = False
    report_schedule_enabled: bool = False
    report_schedule_interval_hours: int = Field(default=24, ge=1, le=720)
    report_schedule_symbol: str = Field(default="", max_length=50)

    @field_validator("market")
    @classmethod
    def validate_market(cls, v: str) -> str:
        if v not in ("US", "HK"):
            raise ValueError("market must be US or HK")
        return v

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        return _normalize_symbol(v)

    @field_validator("sell_high")
    @classmethod
    def validate_sell_high(cls, v: Optional[float], info: Any) -> Optional[float]:
        if v is None:
            return v
        buy_low = info.data.get("buy_low")
        if buy_low is not None and buy_low > 0 and v <= buy_low:
            raise ValueError("sell_high must be greater than buy_low")
        return v

    @model_validator(mode="after")
    def validate_safety_windows(self) -> "StrategyMergedSchema":
        _validate_symbol_market_pair(self.symbol, self.market)
        if self.flatten_minutes_before_close > self.entry_cutoff_minutes_before_close:
            raise ValueError(
                "flatten_minutes_before_close must not exceed entry_cutoff_minutes_before_close"
            )
        if self.short_selling:
            raise ValueError("short selling is disabled by the P0 live safety policy")
        if self.allow_position_addons:
            raise ValueError("position add-ons are disabled by the P0 live safety policy")
        if self.llm_order_execution_enabled:
            raise ValueError("LLM live orders are disabled by the P0 live safety policy")
        return self


class NotificationChannelSchema(BaseModel):
    type: Literal["serverchan", "webhook"]
    severity_floor: Literal["INFO", "WARNING", "CRITICAL"] = "INFO"
    url: Optional[str] = None

    @field_validator("url")
    @classmethod
    def validate_webhook_url_field(cls, v: Optional[str], info: Any) -> Optional[str]:
        if v is None or not str(v).strip():
            return v
        channel_type = info.data.get("type")
        if channel_type != "webhook":
            return v
        from app.core.url_safety import validate_webhook_url

        return validate_webhook_url(v)


class CredentialConfigSchema(BaseModel):
    longbridge_app_key: str = Field(default="", max_length=4096)
    longbridge_app_secret: str = Field(default="", max_length=4096)
    longbridge_access_token: str = Field(default="", max_length=4096)
    sct_key: str = Field(default="", max_length=4096)
    notification_channels: Optional[list[NotificationChannelSchema]] = None


class CredentialResponse(BaseModel):
    id: int
    longbridge_app_key: str
    longbridge_app_secret: str
    longbridge_access_token: str
    sct_key: str
    notification_channels: list[NotificationChannelSchema] = Field(default_factory=list)
    has_longbridge_app_key: bool = False
    has_longbridge_app_secret: bool = False
    has_longbridge_access_token: bool = False
    has_sct_key: bool = False
    updated_at: datetime
    reload_warning: Optional[str] = None

    model_config = {"from_attributes": True}


class StrategyResponse(BaseModel):
    id: int
    symbol: str
    market: str
    buy_low: float
    sell_high: float
    short_selling: bool
    min_profit_amount: float
    auto_resume_minutes: int
    max_daily_loss: float
    max_consecutive_losses: int
    llm_interval_minutes: int
    fee_rate_us: float
    fee_rate_hk: float
    min_repricing_pct: float
    llm_action_cooldown_seconds: int
    trading_session_mode: str = "ANY"
    margin_safety_factor: float = 0.9
    allow_position_addons: bool = False
    max_position_quantity: int = 100
    max_position_notional: float = 5000.0
    max_risk_per_trade: float = 250.0
    stop_loss_pct: float = 1.0
    max_holding_minutes: int = 60
    entry_cutoff_minutes_before_close: int = 45
    flatten_minutes_before_close: int = 15
    llm_order_execution_enabled: bool = False
    report_schedule_enabled: bool = False
    report_schedule_interval_hours: int = 24
    report_schedule_symbol: str = ""
    updated_at: datetime
    consistency_warnings: list[dict[str, str]] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class StatusResponse(BaseModel):
    engine_state: str
    paused: bool
    kill_switch: bool
    runner_running: bool = False
    daily_pnl: float
    consecutive_losses: int
    last_price: float
    last_trigger_price: float
    last_trigger_at: Optional[datetime]
    last_action_message: str = ""
    trading_session_mode: str = "ANY"
    is_trading_hours: bool = True
    execution_state: str = "IDLE"
    reduction_reason: str = ""
    reduction_started_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class StatusHistoryPoint(BaseModel):
    symbol: str = ""
    timestamp: datetime
    engine_state: str
    paused: bool
    kill_switch: bool
    daily_pnl: float
    consecutive_losses: int
    last_price: float
    last_trigger_price: float


class TradeSignalMarker(BaseModel):
    timestamp: datetime
    broker_order_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    status: str


class StatusHistoryResponse(BaseModel):
    points: list[StatusHistoryPoint]
    markers: list[TradeSignalMarker]


class ReportMetrics(BaseModel):
    total_pnl: float
    total_trades: int
    win_count: int
    loss_count: int
    win_rate: float
    profit_loss_ratio: float
    avg_pnl_per_trade: float
    max_profit: float | None = None
    max_loss: float | None = None
    max_drawdown: float
    llm_suggestions_count: int
    llm_applied_count: int
    llm_apply_rate: float
    llm_profitable_count: int
    llm_accuracy_rate: float

    model_config = {"from_attributes": True}


class ReportDailyPoint(BaseModel):
    date: str
    pnl: float
    cumulative_pnl: float
    drawdown: float
    trade_count: int
    win_count: int

    model_config = {"from_attributes": True}


class ReportAttributionPoint(BaseModel):
    key: str
    label: str
    trade_count: int
    pnl: float
    win_rate: float
    share: float

    model_config = {"from_attributes": True}


class ReportOrderDetail(BaseModel):
    broker_order_id: str
    side: str
    quantity: float
    executed_price: float
    status: str
    filled_at: datetime | None
    pnl: float

    model_config = {"from_attributes": True}


class ReportDayDetail(BaseModel):
    date: str
    orders: list[ReportOrderDetail]

    model_config = {"from_attributes": True}


class ReportResponse(BaseModel):
    period_type: str
    symbol: str
    start_date: str
    end_date: str
    metrics: ReportMetrics
    daily_points: list[ReportDailyPoint]
    attribution: list[ReportAttributionPoint] = Field(default_factory=list)
    details: list[ReportDayDetail] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class DiagnosticQuoteStream(BaseModel):
    last_push_age_seconds: float | None = None
    last_quote_age_seconds: float | None = None
    recent_quote_count: int


class DiagnosticRiskState(BaseModel):
    paused: bool
    kill_switch: bool
    pause_reason: str = ""
    daily_pnl: float
    consecutive_losses: int


class QuoteQuality(BaseModel):
    has_quote: bool
    price_positive: bool
    spread_reasonable: bool
    last_bbo_consistent: bool = False
    source_timestamp_fresh: bool = False
    last_price: float | None = None
    bid: float | None = None
    ask: float | None = None


class DiagnosticSymbolRuntime(BaseModel):
    symbol: str
    market: str
    is_primary: bool
    trading_enabled: bool = False
    engine_state: str
    last_price: float
    last_trigger_price: float
    recent_quote_count: int
    has_pending_order: bool
    quote_quality: QuoteQuality | None = None


class DiagnosticLiveSafety(BaseModel):
    short_entries_enabled: bool
    allow_position_addons: bool
    max_position_quantity: int
    max_position_notional: float
    max_risk_per_trade: float
    stop_loss_pct: float
    max_holding_minutes: int
    entry_cutoff_minutes_before_close: int
    flatten_minutes_before_close: int
    llm_shadow_mode: bool
    llm_order_execution_enabled: bool


class DiagnosticsResponse(BaseModel):
    runner_running: bool
    thread_alive: bool
    quotes_subscribed: bool
    trigger_in_flight: bool
    pending_order_symbols: list[str]
    pending_order_ids: list[str] = Field(default_factory=list)
    unrepresentable_live_order_issues: list[str] = Field(default_factory=list)
    order_sync_succeeded: bool = False
    execution_state: str = "IDLE"
    reduction_reason: str = ""
    live_safety: DiagnosticLiveSafety
    quote_stream: DiagnosticQuoteStream
    risk: DiagnosticRiskState
    symbol_runtimes: list[DiagnosticSymbolRuntime]


class OrderResponse(BaseModel):
    id: int = 0
    broker_order_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    executed_quantity: Optional[float] = None
    executed_price: Optional[float] = None
    status: str
    created_at: datetime
    filled_at: Optional[datetime]
    source: str = "local"
    cancellable: bool = False

    model_config = {"from_attributes": True}


class OrderPageResponse(BaseModel):
    items: list[OrderResponse]
    total: int
    page: int
    page_size: int
    scope: str = "today"


class OrderCancelResponse(BaseModel):
    broker_order_id: str
    status: str
    message: str


class TradeEventResponse(BaseModel):
    id: int
    event_type: str
    symbol: str
    broker_order_id: str
    side: str
    status: str
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class TimelineEventResponse(BaseModel):
    """Unified row for ``GET /api/events`` (trade_events ∪ audit_logs ∪
    llm_interactions ∪ risk_events)."""

    source: Literal["trade", "audit", "llm", "risk"]
    id: int
    event_type: str
    symbol: str = ""
    broker_order_id: str = ""
    side: str = ""
    status: str = ""
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    actor_hash: Optional[str] = None
    source_ip: Optional[str] = None
    severity: Optional[str] = None
    result: Optional[str] = None


class TradeEventPageResponse(BaseModel):
    items: list[TimelineEventResponse]
    total: int
    page: int
    page_size: int


class ControlRequest(BaseModel):
    reason: str = Field(default="manual")


class MessageResponse(BaseModel):
    message: str


class CashBalanceSchema(BaseModel):
    currency: str
    available_cash: float
    frozen_cash: float


class PositionSchema(BaseModel):
    symbol: str
    side: str
    quantity: float
    avg_price: float
    market_value: float


class AccountResponse(BaseModel):
    total_assets: float
    cash_balances: list[CashBalanceSchema]
    positions: list[PositionSchema]
    available: bool = True
    error: Optional[str] = None


class BacktestPricePoint(BaseModel):
    timestamp: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def validate_ohlc(self) -> "BacktestPricePoint":
        if self.high < self.low:
            raise ValueError("high must be greater than or equal to low")
        if self.high < max(self.open, self.close):
            raise ValueError("high must be greater than or equal to open and close")
        if self.low > min(self.open, self.close):
            raise ValueError("low must be less than or equal to open and close")
        return self


class BrokerCandlesResponse(BaseModel):
    """Recent candlesticks from the broker, usable directly as backtest input."""

    symbol: str
    period: str
    count: int
    bars: list[BacktestPricePoint]
    csv_text: str



class BacktestParams(BaseModel):
    symbol: str = Field(default="", max_length=50)
    buy_low: float = Field(gt=0)
    sell_high: float = Field(gt=0)
    short_selling: bool = Field(default=False)
    min_profit_amount: float = Field(default=0.0, ge=0)
    max_daily_loss: float = Field(default=5000.0, gt=0)
    max_consecutive_losses: int = Field(default=3, ge=1)
    quantity: float = Field(default=1.0, gt=0)
    initial_cash: float = Field(default=100000.0, gt=0)
    fee_rate: float = Field(default=0.0, ge=0, le=0.1)
    fixed_fee: float = Field(default=0.0, ge=0)
    slippage_pct: float = Field(default=0.0, ge=0, le=5)
    stop_loss_pct: float = Field(default=0.0, ge=0, le=100)

    @field_validator("symbol")
    @classmethod
    def validate_optional_symbol(cls, v: str) -> str:
        symbol = v.strip().upper()
        if not symbol:
            return symbol
        return _normalize_symbol(symbol)

    @field_validator("sell_high")
    @classmethod
    def validate_backtest_sell_high(cls, v: float, info: Any) -> float:
        buy_low = info.data.get("buy_low")
        if buy_low is not None and v <= buy_low:
            raise ValueError("sell_high must be greater than buy_low")
        return v


class BacktestRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    params: BacktestParams
    csv_text: Optional[str] = Field(default=None, max_length=2_000_000)
    price_points: list[BacktestPricePoint] = Field(default_factory=list, max_length=50_000)

    @model_validator(mode="after")
    def validate_price_source(self) -> "BacktestRunRequest":
        if not (self.csv_text and self.csv_text.strip()) and not self.price_points:
            raise ValueError("either csv_text or price_points is required")
        return self


class BacktestTradeLog(BaseModel):
    timestamp: datetime
    action: str
    price: float
    quantity: float
    fee: float
    pnl: float
    state_after: str
    reason: str
    holding_minutes: Optional[float] = None


class BacktestSkippedSignal(BaseModel):
    timestamp: datetime
    action: str
    price: float
    reason: str
    state: str
    category: Optional[str] = None


class BacktestEquityPoint(BaseModel):
    timestamp: datetime
    close: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    drawdown_pct: float
    position: str


class BacktestMetrics(BaseModel):
    initial_cash: float
    final_equity: float
    total_pnl: float
    total_return_pct: float
    max_drawdown_pct: float
    trade_count: int
    closed_trade_count: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_holding_minutes: float
    fees_paid: float
    skipped_signals: int
    final_state: str
    sharpe_ratio: Optional[float] = None
    sortino_ratio: Optional[float] = None
    calmar_ratio: Optional[float] = None
    profit_factor: Optional[float] = None
    profit_loss_ratio: Optional[float] = None


class BacktestFeeSensitivityPoint(BaseModel):
    fee_rate: float
    total_pnl: float
    total_return_pct: float
    max_drawdown_pct: float


class BacktestResult(BaseModel):
    params: BacktestParams
    metrics: BacktestMetrics
    equity_curve: list[BacktestEquityPoint]
    trades: list[BacktestTradeLog]
    skipped_signals: list[BacktestSkippedSignal]
    fee_sensitivity: list[BacktestFeeSensitivityPoint]


class BacktestExportRequest(BaseModel):
    """Export a backtest result as a multi-section CSV file."""

    result: BacktestResult
    sections: list[str] = Field(
        default_factory=lambda: ["params", "trades", "equity_curve", "skipped_signals", "fee_sensitivity"],
    )


class StrategyExperimentGridValue(BaseModel):
    value: float


class StrategyExperimentGridRange(BaseModel):
    start: float
    end: float
    step: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_range_count(self) -> "StrategyExperimentGridRange":
        if self.step <= 0:
            raise ValueError("step must be positive")
        # Count values that do not exceed end (with epsilon for float tolerance).
        # Uses the same logic as ExperimentGridService._expand_item.
        count = int((self.end - self.start) / self.step + 1e-12) + 1
        if count < 1:
            raise ValueError("range must produce at least one value")
        if count > 500:
            raise ValueError(f"range produces {count} values, exceeds maximum 500")
        return self


class StrategyExperimentGridItem(BaseModel):
    value: Optional[float] = None
    values: Optional[list[float]] = None
    range: Optional[StrategyExperimentGridRange] = None

    @model_validator(mode="after")
    def validate_one_of(self) -> "StrategyExperimentGridItem":
        count = sum(1 for x in [self.value, self.values, self.range] if x is not None)
        if count != 1:
            raise ValueError("must set exactly one of value, values, or range")
        if self.values is not None and len(self.values) == 0:
            raise ValueError("values must not be empty")
        return self


class BacktestSweepHeatmapCell(BaseModel):
    buy_low: float
    sell_high: float
    value: Optional[float] = None


class BacktestSweepHeatmap(BaseModel):
    x_axis: str
    y_axis: str
    z_metric: str
    cells: list[BacktestSweepHeatmapCell]


class BacktestSweepRow(BaseModel):
    # The raw engine params actually run for this combination. A grid may push a
    # value past BacktestParams' display bounds (e.g. an exploratory fee_rate
    # above 0.1) that the engine accepts but BacktestParams would reject, so the
    # params are surfaced as a plain dict rather than re-validated.
    params: dict[str, Any]
    metrics: BacktestMetrics
    rank: int


class BacktestSweepResult(BaseModel):
    rows: list[BacktestSweepRow]
    best: Optional[BacktestSweepRow] = None
    heatmap: BacktestSweepHeatmap
    evaluated_count: int
    skipped_count: int
    sort_by: str


class BacktestSweepRequest(BaseModel):
    """Synchronous parameter sweep: run BacktestEngine over the Cartesian
    product of ``grid`` and rank by ``sort_by``. Instant + in-memory, distinct
    from the persisted, async StrategyExperiment system."""

    model_config = ConfigDict(extra="forbid")
    base: BacktestParams
    grid: dict[str, StrategyExperimentGridItem] = Field(min_length=1)
    sort_by: Literal[
        "sharpe_ratio", "sortino_ratio", "calmar_ratio", "profit_factor", "total_return_pct"
    ] = "sharpe_ratio"
    max_combinations: int = Field(default=2000, ge=1, le=10000)
    csv_text: Optional[str] = Field(default=None, max_length=2_000_000)
    price_points: list[BacktestPricePoint] = Field(default_factory=list, max_length=50_000)

    @model_validator(mode="after")
    def validate_price_source(self) -> "BacktestSweepRequest":
        if not (self.csv_text and self.csv_text.strip()) and not self.price_points:
            raise ValueError("either csv_text or price_points is required")
        return self


class WalkForwardWindowOut(BaseModel):
    index: int
    start: datetime
    end: datetime
    train_size: int
    test_size: int
    best_params: Optional[dict[str, Any]] = None
    test_metrics: Optional[BacktestMetrics] = None


class WalkForwardSummaryOut(BaseModel):
    window_count: int
    evaluated_window_count: int
    mean_test_return_pct: Optional[float] = None
    median_test_return_pct: Optional[float] = None
    mean_test_metric: Optional[float] = None
    profitable_window_pct: Optional[float] = None
    test_return_std_pct: Optional[float] = None


class WalkForwardResultOut(BaseModel):
    windows: list[WalkForwardWindowOut]
    summary: WalkForwardSummaryOut
    sort_by: str
    train_size: int
    test_size: int
    step: int


class WalkForwardRequest(BaseModel):
    """Walk-forward rolling-window backtest: optimize on each train window,
    evaluate out-of-sample on the next test window. Empty ``grid`` = plain
    rolling-window evaluation of ``base`` (consistency only)."""

    model_config = ConfigDict(extra="forbid")
    base: BacktestParams
    grid: dict[str, StrategyExperimentGridItem] = Field(default_factory=dict)
    train_size: int = Field(ge=2)
    test_size: int = Field(ge=1)
    step: Optional[int] = Field(default=None, ge=1)
    sort_by: Literal[
        "sharpe_ratio", "sortino_ratio", "calmar_ratio", "profit_factor", "total_return_pct"
    ] = "sharpe_ratio"
    max_combinations: int = Field(default=2000, ge=1, le=10000)
    csv_text: Optional[str] = Field(default=None, max_length=2_000_000)
    price_points: list[BacktestPricePoint] = Field(default_factory=list, max_length=50_000)

    @model_validator(mode="after")
    def validate_price_source(self) -> "WalkForwardRequest":
        if not (self.csv_text and self.csv_text.strip()) and not self.price_points:
            raise ValueError("either csv_text or price_points is required")
        return self


class StressTestResult(BaseModel):
    scenarios_run: int
    baseline_return_pct: Optional[float] = None
    median_return_pct: Optional[float] = None
    p5_return_pct: Optional[float] = None
    p95_return_pct: Optional[float] = None
    worst_return_pct: Optional[float] = None
    worst_drawdown_pct: Optional[float] = None
    profitable_scenario_pct: Optional[float] = None
    jitter_pct: float
    seed: int
    returns: list[float]


class StressTestRequest(BaseModel):
    """What-If stress ensemble: re-run the engine over N jittered price paths."""

    model_config = ConfigDict(extra="forbid")
    base: BacktestParams
    scenarios: int = Field(default=50, ge=1, le=1000)
    jitter_pct: float = Field(default=1.0, ge=0, le=20)
    seed: int = Field(default=42, ge=0)
    csv_text: Optional[str] = Field(default=None, max_length=2_000_000)
    price_points: list[BacktestPricePoint] = Field(default_factory=list, max_length=50_000)

    @model_validator(mode="after")
    def validate_price_source(self) -> "StressTestRequest":
        if not (self.csv_text and self.csv_text.strip()) and not self.price_points:
            raise ValueError("either csv_text or price_points is required")
        return self


class BacktestRunSaveRequest(BaseModel):
    """Save a backtest run (params + metrics) for side-by-side comparison."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    params: BacktestParams
    metrics: BacktestMetrics

    @field_validator("name")
    @classmethod
    def _non_blank_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return v


class BacktestRunOut(BaseModel):
    id: int
    name: str
    symbol: str
    params: BacktestParams
    metrics: BacktestMetrics
    created_at: datetime


class BacktestRunPage(BaseModel):
    items: list[BacktestRunOut]
    total: int
    page: int
    page_size: int


class BacktestRunCompare(BaseModel):
    runs: list[BacktestRunOut]


# ---------------------------------------------------------------------------
# Conditional Alert Rules (user-defined, cron-evaluated)
# ---------------------------------------------------------------------------


class AlertRuleCreate(BaseModel):
    """Create or fully replace an alert rule."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    symbol: str = Field(default="", max_length=50)
    rule_type: Literal["price_above", "price_below", "daily_loss"]
    threshold: float
    severity: Literal["INFO", "WARNING", "CRITICAL"] = "WARNING"
    enabled: bool = True
    cooldown_seconds: int = Field(default=300, ge=0, le=86400)


class AlertRuleOut(BaseModel):
    id: int
    name: str
    symbol: str
    rule_type: str
    threshold: float
    severity: str
    enabled: bool
    cooldown_seconds: int
    last_fired_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AlertRulePage(BaseModel):
    items: list[AlertRuleOut]
    total: int


class AlertEvaluateResult(BaseModel):
    evaluated: int
    fired: int
    skipped_cooldown: int


class AlertFiringOut(BaseModel):
    id: int
    rule_id: int
    symbol: str
    rule_type: str
    threshold: float
    trigger_value: float
    severity: str
    message: str
    fired_at: datetime

    model_config = {"from_attributes": True}


class AlertFiringPage(BaseModel):
    items: list[AlertFiringOut]
    total: int


# ---------------------------------------------------------------------------
# Strategy presets (named param snapshots)
# ---------------------------------------------------------------------------


class StrategyPresetCreate(BaseModel):
    """Save a named snapshot of strategy params."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    params: StrategyConfigSchema


class StrategyPresetOut(BaseModel):
    id: int
    name: str
    params: dict[str, Any]
    created_at: datetime


class StrategyPresetPage(BaseModel):
    items: list[StrategyPresetOut]
    total: int


class StrategyPresetApplyResult(BaseModel):
    applied: bool
    changed: list[str]


class NotificationLogOut(BaseModel):
    id: int
    title: str
    content: str
    severity: str
    success: bool
    error: str
    created_at: datetime

    model_config = {"from_attributes": True}


class NotificationLogPage(BaseModel):
    items: list[NotificationLogOut]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Daily risk history (runtime_state_snapshots)
# ---------------------------------------------------------------------------


class RiskHistoryPoint(BaseModel):
    created_at: datetime
    engine_state: str
    paused: bool
    kill_switch: bool
    daily_pnl: float
    consecutive_losses: int


class RiskHistoryResponse(BaseModel):
    points: list[RiskHistoryPoint]
    latest: Optional[RiskHistoryPoint] = None



class StrategyExperimentCreate(BaseModel):
    name: str = Field(max_length=128)
    symbol: str = Field(max_length=50)
    base_params: BacktestParams
    parameter_grid: dict[str, StrategyExperimentGridItem] = Field(min_length=1)

    _ALLOWED_GRID_KEYS: set[str] = {
        "buy_low", "sell_high", "min_profit_amount", "max_daily_loss",
        "max_consecutive_losses", "quantity", "initial_cash", "fee_rate",
        "fixed_fee", "slippage_pct", "stop_loss_pct",
    }

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        return _normalize_symbol(v)

    @model_validator(mode="after")
    def validate_experiment(self) -> "StrategyExperimentCreate":
        bp = self.base_params
        if bp.symbol and bp.symbol != self.symbol:
            raise ValueError("base_params.symbol must match symbol or be empty")
        if not bp.symbol:
            bp.symbol = self.symbol

        for key in self.parameter_grid:
            if key not in self._ALLOWED_GRID_KEYS:
                raise ValueError(
                    f"parameter_grid key '{key}' is not allowed. "
                    f"Allowed: {sorted(self._ALLOWED_GRID_KEYS)}"
                )

        return self


class StrategyExperimentRunRequest(BaseModel):
    csv_text: Optional[str] = Field(default=None, max_length=2_000_000)
    price_points: list[BacktestPricePoint] = Field(default_factory=list, max_length=50_000)

    @model_validator(mode="after")
    def validate_price_source(self) -> "StrategyExperimentRunRequest":
        if not (self.csv_text and self.csv_text.strip()) and not self.price_points:
            raise ValueError("either csv_text or price_points is required")
        return self


class StrategyExperimentResponse(BaseModel):
    id: int
    name: str
    symbol: str
    base_params_json: str
    parameter_grid_json: str
    status: str
    estimated_runs: int
    completed_runs: int
    failed_runs: int
    error: str
    created_at: datetime
    completed_at: Optional[datetime] = None
    model_config = {"from_attributes": True}

class StrategyExperimentRunResponse(BaseModel):
    id: int
    experiment_id: int
    parameters: dict[str, Any] = Field(default_factory=dict)
    status: str
    total_pnl: float
    total_return_pct: float
    max_drawdown_pct: float
    win_rate: float
    trade_count: int
    closed_trade_count: int
    sharpe_ratio: Optional[float] = None
    profit_factor: Optional[float] = None
    profit_loss_ratio: Optional[float] = None
    result_summary_json: str
    error: str
    created_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _remap_parameters(cls, data: Any) -> Any:
        """Accept ORM attribute ``parameters_json`` as ``parameters``."""
        import json as _json
        if hasattr(data, "parameters_json"):
            # SQLAlchemy model instance
            d = {}
            for c in data.__table__.columns:
                d[c.name] = getattr(data, c.name)
            raw = d.pop("parameters_json", "{}")
            d["parameters"] = _json.loads(raw) if isinstance(raw, str) else raw
            return d
        if isinstance(data, dict) and "parameters_json" in data:
            data = dict(data)
            raw = data.pop("parameters_json", "{}")
            data["parameters"] = _json.loads(raw) if isinstance(raw, str) else raw
        return data

class StrategyExperimentRunPage(BaseModel):
    items: list[StrategyExperimentRunResponse]
    page: int
    page_size: int
    total: int

class LLMAnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = Field(default=False)


class LLMPreviewAnalyzeRequest(BaseModel):
    symbol: str = Field(max_length=50)
    market: str = Field(default="US")
    current_price: Optional[float] = Field(default=None, gt=0)
    current_buy_low: Optional[float] = Field(default=None, ge=0)
    current_sell_high: Optional[float] = Field(default=None, ge=0)
    min_profit_amount: Optional[float] = Field(default=None, ge=0)
    short_selling: bool = Field(default=False)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        return _normalize_symbol(v)

    @field_validator("market")
    @classmethod
    def validate_market(cls, v: str) -> str:
        if v not in ("US", "HK"):
            raise ValueError("market must be US or HK")
        return v


class LLMAnalyzeResponse(BaseModel):
    success: bool
    applied: bool
    reason: str
    interaction_id: Optional[int] = None
    suggested_buy_low: Optional[float] = None
    suggested_sell_high: Optional[float] = None
    confidence_score: Optional[float] = None
    analysis: Optional[str] = None
    next_analysis_at: Optional[str] = None
    applied_at: Optional[str] = None
    order_action: Optional[str] = None
    order_price: Optional[float] = None
    replacement_action: Optional[str] = None
    replacement_price: Optional[float] = None
    order_reason: Optional[str] = None
    order_status: Optional[str] = None
    order_id: Optional[str] = None


class LLMInteractionResponse(BaseModel):
    id: int
    interaction_type: str
    symbol: str
    market: str
    success: bool
    error: str
    order_action: str
    order_status: Optional[str] = None
    order_id: Optional[str] = None
    applied: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class LLMInteractionDetail(BaseModel):
    """Full LLM interaction record incl. prompt / raw response / parsed /
    context snapshot (omitted from the lightweight list response)."""

    id: int
    interaction_type: str
    symbol: str
    market: str
    prompt: str
    raw_response: str
    parsed_response: dict[str, Any]
    context_snapshot: dict[str, Any]
    success: bool
    error: str
    order_action: str
    order_status: Optional[str] = None
    order_id: Optional[str] = None
    applied: bool
    prompt_variant: Optional[str] = None
    created_at: datetime


class MarketSessionStatus(BaseModel):
    """Granular market session phase for the session-clock widget."""

    market: str
    symbol: str
    status: str  # rth | pre | post | lunch | closed
    is_trading: bool
    local_time: str
    utc_time: datetime
    next_open: datetime


class LLMSuggestion(BaseModel):
    buy_low: float
    sell_high: float
    confidence_score: float
    analysis: str



class LLMBudgetStatus(BaseModel):
    max_symbols_per_cycle: int
    max_analyses_per_hour: int
    tracked_symbol_count: int
    effective_symbol_budget: int
    used_analyses_last_hour: int = 0
    remaining_analyses_this_hour: int = 0


class LLMSymbolStatus(BaseModel):
    symbol: str
    market: str
    is_primary: bool
    has_pending_order: bool
    buy_cooldown_remaining_seconds: float | None = None
    sell_cooldown_remaining_seconds: float | None = None
    last_analysis_at: str | None = None
    next_analysis_at: str | None = None
    last_status: str | None = None
    last_skip_reason: str | None = None

class LLMIntervalStatus(BaseModel):
    enabled: bool
    shadow_mode: bool
    policy_status: Literal["SHADOW", "LIVE"]
    interval_minutes: int
    last_analysis_at: Optional[str] = None
    next_analysis_at: Optional[str] = None
    current_suggestion: Optional[LLMSuggestion] = None
    applied_values: Optional[dict[str, Any]] = None
    last_applied_values: Optional[dict[str, Any]] = None
    reject_reason: Optional[str] = None
    budget: LLMBudgetStatus
    symbol_statuses: list[LLMSymbolStatus] = Field(default_factory=list)


class LLMEvaluationRequest(BaseModel):
    symbol: str
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    horizon_minutes: int = Field(default=60, ge=5, le=1440)
class LLMEvaluationSample(BaseModel):
    interaction_id: int
    created_at: str
    order_action: str
    order_price: Optional[float] = None
    tag: str
    reason: str
    metrics: dict[str, Any] = Field(default_factory=dict)
class LLMEvaluationResponse(BaseModel):
    symbol: str
    horizon_minutes: int
    sample_count: int
    tag_distribution: dict[str, int]
    hit_rate: float
    samples: list[LLMEvaluationSample]

class ReviewDaySchema(BaseModel):
    date: str
    symbol: str
    llm_interactions: list[dict[str, Any]]
    orders: list[dict[str, Any]]
    events: list[dict[str, Any]]
    daily_pnl: float
    trade_count: int
    error_tags: list[str]


class ReviewResponse(BaseModel):
    symbol: str
    from_date: str
    to_date: str
    days: list[ReviewDaySchema]
    total_pnl: float
    total_trades: int
    all_error_tags: list[str]


class ReviewExportQuery(BaseModel):
    symbol: str
    from_date: str
    to_date: str
    format: Literal["json", "csv"] = "json"


class WatchlistItemSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(max_length=50)
    market: str = Field(default="US")
    alias: str = Field(default="", max_length=100)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        return _normalize_symbol(v)

    @field_validator("market")
    @classmethod
    def validate_market(cls, v: str) -> str:
        if v not in ("US", "HK"):
            raise ValueError("market must be US or HK")
        return v

    @model_validator(mode="after")
    def validate_symbol_market(self) -> "WatchlistItemSchema":
        _validate_symbol_market_pair(self.symbol, self.market)
        return self


class WatchlistItemResponse(BaseModel):
    id: int
    symbol: str
    market: str
    alias: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class WatchlistQuote(BaseModel):
    symbol: str
    last_price: float
    bid: float
    ask: float
    timestamp: str


class WatchlistSnapshot(BaseModel):
    symbol: str
    market: str
    alias: str = ""
    is_trading_target: bool = False
    last_price: float
    bid: float
    ask: float
    timestamp: str


class WatchlistScoredSnapshot(WatchlistSnapshot):
    """Snapshot enriched with the latest LLM score for the symbol.

    Symbols without a cached score still appear, with ``score=0`` and
    ``is_stale=True`` so the UI can render the full list while scoring runs.
    """
    score: float
    is_stale: bool = True


class WatchlistSetTradingRequest(BaseModel):
    id: int


class WatchlistScoreRequest(BaseModel):
    symbol: str = Field(max_length=50)
    market: str = Field(default="US")
    ttl_minutes: int = Field(default=60, ge=1, le=1440)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        return _normalize_symbol(v)

    @field_validator("market")
    @classmethod
    def validate_market(cls, v: str) -> str:
        if v not in ("US", "HK"):
            raise ValueError("market must be US or HK")
        return v

    @model_validator(mode="after")
    def validate_symbol_market(self) -> "WatchlistScoreRequest":
        _validate_symbol_market_pair(self.symbol, self.market)
        return self


class WatchlistScoreResponse(BaseModel):
    id: int
    symbol: str
    market: str
    score: float
    rationale: str
    confidence: float
    recommended_action: str
    source: str
    created_at: datetime
    expires_at: datetime
    is_stale: bool = False

    model_config = {"from_attributes": True}


class WatchlistScoreListResponse(BaseModel):
    scores: list[WatchlistScoreResponse]


class PromptVersionCreate(BaseModel):
    name: str = Field(max_length=100)
    version: str = Field(max_length=20)
    description: str = Field(default="", max_length=500)
    template: str


class PromptVersionResponse(BaseModel):
    id: int
    name: str
    version: str
    description: str
    template: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ExperimentResultResponse(BaseModel):
    id: int
    experiment_name: str
    variant_name: str
    interaction_id: int | None
    order_action: str
    predicted_direction: str
    actual_pnl: float
    was_profitable: bool | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ExperimentSummary(BaseModel):
    variant_name: str
    total_count: int
    profitable_count: int
    avg_pnl: float
    win_rate: float


class PerformanceStats(BaseModel):
    total_trades: int
    win_rate: float
    total_pnl: float
    avg_pnl: float


class PerformanceVariant(BaseModel):
    variant: str
    total_trades: int
    win_rate: float
    total_pnl: float
    avg_pnl: float


class MacdValue(BaseModel):
    macd: float
    signal: float
    histogram: float


class VolumeAnalysisSchema(BaseModel):
    avg_volume: float
    volume_ratio: float
    trend: str


class SentimentValue(BaseModel):
    sentiment: str
    score: float
    description: str


class MultiTimeframeSchema(BaseModel):
    daily_trend: str
    minute_trend: str
    aligned: bool
    description: str


class IndicatorsResponse(BaseModel):
    available: bool
    symbol: str
    market: str
    atr: float | None = None
    rsi: float | None = None
    macd: MacdValue | None = None
    volume_analysis: VolumeAnalysisSchema | None = None
    sentiment: SentimentValue | None = None
    multi_timeframe: MultiTimeframeSchema | None = None
    bb_upper: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None


# ---------------------------------------------------------------------------
# Trade Journal (post-trade notes / tags / rating attached to an order)
# ---------------------------------------------------------------------------


class TradeNoteUpsert(BaseModel):
    """Body for PUT /api/trade-notes/{order_id} (upsert)."""

    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=8000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    rating: Optional[int] = Field(default=None, ge=1, le=5)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for tag in value:
            tag = tag.strip()
            if not tag or len(tag) > 32 or tag in seen:
                continue
            seen.add(tag)
            cleaned.append(tag)
        return cleaned


class TradeNoteOut(BaseModel):
    id: int
    order_id: int
    symbol: str
    note: str
    tags: list[str]
    rating: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class TradeNotePage(BaseModel):
    items: list[TradeNoteOut]
    total: int
    page: int
    page_size: int


class TradeNoteTagCount(BaseModel):
    tag: str
    count: int


class TradeNoteAnalytics(BaseModel):
    total: int
    rated_count: int
    avg_rating: Optional[float] = None
    rating_distribution: dict[int, int]
    top_tags: list[TradeNoteTagCount]
    distinct_symbols: int



# ---------------------------------------------------------------------------
# Live unrealized PnL (positions) — joins tracked_entries cost with live quotes
# ---------------------------------------------------------------------------


class PositionPnlRow(BaseModel):
    symbol: str
    quantity: float
    avg_entry_cost: float
    last_price: Optional[float] = None
    unrealized_pnl: float
    unrealized_pnl_pct: Optional[float] = None
    market_value: float
    cost_value: float
    has_quote: bool = True


class PositionPnlResult(BaseModel):
    positions: list[PositionPnlRow]
    total_unrealized_pnl: float
    total_cost_basis: float
    total_unrealized_pnl_pct: Optional[float] = None
    available: bool = True
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Closed round-trip trades (entry <-> exit pairing)
# ---------------------------------------------------------------------------


class ClosedTrade(BaseModel):
    """A paired entry<->exit round trip with realized PnL and hold duration."""

    symbol: str
    side: str
    entry_order_id: int
    exit_order_id: int
    entry_at: datetime
    exit_at: datetime
    entry_price: float
    exit_price: float
    quantity: float
    gross_pnl: float
    est_fees: float
    net_pnl: float
    holding_seconds: float


class ClosedTradePage(BaseModel):
    items: list[ClosedTrade]
    total: int


class TradeStats(BaseModel):
    """Per-trade performance stats over closed round trips (streaks, expectancy)."""

    model_config = ConfigDict(from_attributes=True)

    total_trades: int
    win_count: int
    loss_count: int
    breakeven_count: int
    win_rate: float
    total_gross_pnl: float
    total_net_pnl: float
    avg_win: Optional[float] = None
    avg_loss: Optional[float] = None
    expectancy: float
    profit_factor: Optional[float] = None
    payoff_ratio: Optional[float] = None
    largest_win: Optional[float] = None
    largest_loss: Optional[float] = None
    current_streak_type: str
    current_streak_count: int
    max_win_streak: int
    max_loss_streak: int
    avg_hold_seconds: Optional[float] = None


class TradeCalendarDay(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    date: str
    trade_count: int
    win_count: int
    loss_count: int
    net_pnl: float
    gross_pnl: float
    symbols: list[str]


class TradeCalendarResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    items: list[TradeCalendarDay]
    total_trades: int
    total_net_pnl: float


class TradeHoldDurationBucket(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    bucket: str
    min_seconds: Optional[float] = None
    max_seconds: Optional[float] = None
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float
    net_pnl: float
    avg_net_pnl: Optional[float] = None


class TradeHoldDurationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    items: list[TradeHoldDurationBucket]
    total_trades: int


class TradePnlDistributionBucket(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    bucket: str
    min_pnl: Optional[float] = None
    max_pnl: Optional[float] = None
    trade_count: int
    net_pnl: float


class TradePnlDistributionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    items: list[TradePnlDistributionBucket]
    total_trades: int
    total_net_pnl: float


class TradeMonthlySummaryRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    month: str
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float
    net_pnl: float
    gross_pnl: float
    cumulative_pnl: float
    drawdown: float


class TradeMonthlySummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    items: list[TradeMonthlySummaryRow]
    total_trades: int
    total_net_pnl: float


class TradeWeekdayAttributionRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    weekday: int
    label: str
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float
    net_pnl: float
    avg_net_pnl: Optional[float] = None


class TradeWeekdayAttributionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    items: list[TradeWeekdayAttributionRow]
    total_trades: int
    total_net_pnl: float


class EquityCurvePoint(BaseModel):
    date: str
    realized_pnl: float
    cumulative_pnl: float
    drawdown: float
    trade_count: int


class EquityCurveResponse(BaseModel):
    """Account-wide cumulative realized PnL curve (net), day-granularity."""

    model_config = ConfigDict(from_attributes=True)

    points: list[EquityCurvePoint]
    total_realized_pnl: float
    max_drawdown: float


class SymbolAttributionRow(BaseModel):
    symbol: str
    realized_pnl: float
    trade_count: int
    win_count: int
    win_rate: float
    contribution_share: float
    largest_win: Optional[float] = None
    largest_loss: Optional[float] = None


class SymbolAttributionResponse(BaseModel):
    """Portfolio-level realized PnL grouped by symbol (net)."""

    model_config = ConfigDict(from_attributes=True)

    rows: list[SymbolAttributionRow]
    total_realized_pnl: float
