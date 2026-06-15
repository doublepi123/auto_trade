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


class StrategyConfigSchema(BaseModel):
    # Reject unknown keys so the API surface stays closed: a typo
    # such as ``buyLown`` (camelCase) is a 422 instead of a silent
    # no-op. Strategy update audit diffs also stay clean because
    # Pydantic only forwards known fields to model_dump.
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(default="", max_length=50)
    market: str = Field(default="US")
    buy_low: Optional[float] = Field(default=None, gt=0)
    sell_high: Optional[float] = Field(default=None, gt=0)
    short_selling: bool = Field(default=False)
    min_profit_amount: Optional[float] = Field(default=None, ge=0)
    auto_resume_minutes: Optional[int] = Field(default=None, ge=0, le=1440)
    max_daily_loss: float = Field(default=5000.0, gt=0)
    max_consecutive_losses: int = Field(default=3, ge=1)
    llm_interval_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    fee_rate_us: Optional[float] = Field(default=None, ge=0, le=0.01)
    fee_rate_hk: Optional[float] = Field(default=None, ge=0, le=0.02)
    min_repricing_pct: Optional[float] = Field(default=None, ge=0, le=0.05)
    llm_action_cooldown_seconds: Optional[int] = Field(default=None, ge=0, le=3600)
    trading_session_mode: Literal["RTH_ONLY", "ANY"] = "ANY"
    margin_safety_factor: Optional[float] = Field(default=None, ge=0, le=1)

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


class StrategyMergedSchema(BaseModel):
    symbol: str = Field(default="", max_length=50)
    market: str = Field(default="US")
    buy_low: Optional[float] = Field(default=None)
    sell_high: Optional[float] = Field(default=None)
    short_selling: bool = Field(default=False)
    min_profit_amount: float = Field(default=0.0, ge=0)
    auto_resume_minutes: int = Field(default=3, ge=0, le=1440)
    max_daily_loss: float = Field(default=5000.0, gt=0)
    max_consecutive_losses: int = Field(default=3, ge=1)
    llm_interval_minutes: int = Field(default=2, ge=1, le=1440)
    fee_rate_us: float = Field(default=0.0005, ge=0, le=0.01)
    fee_rate_hk: float = Field(default=0.003, ge=0, le=0.02)
    min_repricing_pct: float = Field(default=0.003, ge=0, le=0.05)
    llm_action_cooldown_seconds: int = Field(default=60, ge=0, le=3600)
    trading_session_mode: Literal["RTH_ONLY", "ANY"] = "ANY"
    margin_safety_factor: float = Field(default=0.9, ge=0, le=1)

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
    last_price: float | None = None
    bid: float | None = None
    ask: float | None = None


class DiagnosticSymbolRuntime(BaseModel):
    symbol: str
    market: str
    is_primary: bool
    engine_state: str
    last_price: float
    last_trigger_price: float
    recent_quote_count: int
    has_pending_order: bool
    quote_quality: QuoteQuality | None = None


class DiagnosticsResponse(BaseModel):
    runner_running: bool
    thread_alive: bool
    quotes_subscribed: bool
    trigger_in_flight: bool
    pending_order_symbols: list[str]
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
    """Unified row for ``GET /api/events`` (trade_events ∪ audit_logs)."""

    source: Literal["trade", "audit"]
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
    interval_minutes: int
    last_analysis_at: Optional[str] = None
    next_analysis_at: Optional[str] = None
    current_suggestion: Optional[LLMSuggestion] = None
    applied_values: Optional[dict[str, Any]] = None
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
