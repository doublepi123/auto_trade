from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from typing import Annotated, Any, Literal, Optional

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
    max_drawdown_amount: Optional[float] = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    max_consecutive_losses: int = Field(default=3, ge=1, le=100)
    llm_interval_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    fee_rate_us: Optional[float] = Field(default=None, ge=0, le=0.01, allow_inf_nan=False)
    fee_rate_hk: Optional[float] = Field(default=None, ge=0, le=0.02, allow_inf_nan=False)
    min_repricing_pct: Optional[float] = Field(default=None, ge=0, le=0.05, allow_inf_nan=False)
    llm_action_cooldown_seconds: Optional[int] = Field(default=None, ge=0, le=3600)
    trading_session_mode: Literal["RTH_ONLY", "ANY"] = "ANY"
    margin_safety_factor: Optional[float] = Field(default=None, ge=0, le=1, allow_inf_nan=False)
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
    max_drawdown_amount: Optional[float] = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    max_consecutive_losses: int = Field(default=3, ge=1, le=100)
    llm_interval_minutes: int = Field(default=2, ge=1, le=1440)
    fee_rate_us: float = Field(default=0.0005, ge=0, le=0.01, allow_inf_nan=False)
    fee_rate_hk: float = Field(default=0.003, ge=0, le=0.02, allow_inf_nan=False)
    min_repricing_pct: float = Field(default=0.003, ge=0, le=0.05, allow_inf_nan=False)
    llm_action_cooldown_seconds: int = Field(default=60, ge=0, le=3600)
    trading_session_mode: Literal["RTH_ONLY", "ANY"] = "ANY"
    margin_safety_factor: float = Field(default=0.9, ge=0, le=1, allow_inf_nan=False)
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
    type: Literal["serverchan", "webhook", "telegram"]
    severity_floor: Literal["INFO", "WARNING", "CRITICAL"] = "INFO"
    url: Optional[str] = None
    bot_token: Optional[str] = Field(default=None, max_length=4096)
    chat_id: Optional[str] = Field(default=None, max_length=4096)

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
    max_drawdown_amount: Optional[float] = None
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


class StrategyV2ShadowConfigValues(BaseModel):
    """Validated P2 values shared by API updates and the shadow service."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    symbol: str
    zscore_window_1m_bars: int = Field(default=30, ge=10, le=240)
    zscore_window_5m_bars: int = Field(default=12, ge=5, le=120)
    breach_zscore: float = Field(default=-2.0, ge=-6.0, lt=-0.1, allow_inf_nan=False)
    reclaim_zscore: float = Field(default=-1.0, ge=-5.0, le=0.0, allow_inf_nan=False)
    five_minute_zscore_max: float = Field(default=-0.5, ge=-5.0, le=0.0, allow_inf_nan=False)
    adx_period: int = Field(default=14, ge=5, le=50)
    max_adx: float = Field(default=20.0, ge=1.0, le=40.0, allow_inf_nan=False)
    realized_vol_window_bars: int = Field(default=30, ge=10, le=240)
    min_realized_vol: float = Field(default=0.10, ge=0.0, le=3.0, allow_inf_nan=False)
    max_realized_vol: float = Field(default=0.80, gt=0.0, le=3.0, allow_inf_nan=False)
    stop_loss_pct: float = Field(default=0.75, gt=0.0, le=0.75, allow_inf_nan=False)
    profit_target_pct: float = Field(default=0.50, gt=0.0, le=5.0, allow_inf_nan=False)
    max_holding_minutes: int = Field(default=60, ge=1, le=60)
    entry_cutoff_minutes_before_close: int = Field(default=45, ge=45, le=180)
    flatten_minutes_before_close: int = Field(default=15, ge=15, le=180)
    arm_ttl_bars: int = Field(default=10, ge=1, le=60)
    max_entries_per_day: int = Field(default=2, ge=1, le=2)
    entry_cooldown_minutes: int = Field(default=15, ge=15, le=240)
    slippage_bps: float = Field(default=2.0, ge=0.0, le=50.0, allow_inf_nan=False)
    estimated_fee_rate_us: float = Field(
        default=0.0005,
        ge=0.0,
        le=0.1,
        allow_inf_nan=False,
    )
    estimated_fee_rate_hk: float = Field(
        default=0.003,
        ge=0.0,
        le=0.1,
        allow_inf_nan=False,
    )
    algorithm_version: Literal["strategy-v2-rth-mr-v5-causal-entry"] = (
        "strategy-v2-rth-mr-v5-causal-entry"
    )
    mode: Literal["SHADOW"] = "SHADOW"
    order_submission_allowed: Literal[False] = False
    allow_position_addons: Literal[False] = False
    short_entries_enabled: Literal[False] = False

    @field_validator("symbol")
    @classmethod
    def validate_shadow_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @model_validator(mode="after")
    def validate_shadow_thresholds(self) -> "StrategyV2ShadowConfigValues":
        if self.breach_zscore >= self.reclaim_zscore:
            raise ValueError("breach_zscore must be less than reclaim_zscore")
        if self.min_realized_vol >= self.max_realized_vol:
            raise ValueError("min_realized_vol must be less than max_realized_vol")
        if self.flatten_minutes_before_close > self.entry_cutoff_minutes_before_close:
            raise ValueError(
                "flatten_minutes_before_close must not exceed "
                "entry_cutoff_minutes_before_close"
            )
        max_5m_window = 68 if self.symbol.endswith(".US") else 56
        max_adx_period = 34 if self.symbol.endswith(".US") else 28
        if self.zscore_window_5m_bars > max_5m_window:
            raise ValueError(
                f"zscore_window_5m_bars must not exceed {max_5m_window} for {self.symbol}"
            )
        if self.adx_period > max_adx_period:
            raise ValueError(
                f"adx_period must not exceed {max_adx_period} for {self.symbol}"
            )
        return self


class StrategyV2ShadowConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: Optional[bool] = None
    opening_momentum_execution_eligible: Optional[bool] = None
    zscore_window_1m_bars: Optional[int] = Field(default=None, ge=10, le=240)
    zscore_window_5m_bars: Optional[int] = Field(default=None, ge=5, le=120)
    breach_zscore: Optional[float] = Field(default=None, ge=-6.0, lt=-0.1, allow_inf_nan=False)
    reclaim_zscore: Optional[float] = Field(default=None, ge=-5.0, le=0.0, allow_inf_nan=False)
    five_minute_zscore_max: Optional[float] = Field(default=None, ge=-5.0, le=0.0, allow_inf_nan=False)
    adx_period: Optional[int] = Field(default=None, ge=5, le=50)
    max_adx: Optional[float] = Field(default=None, ge=1.0, le=40.0, allow_inf_nan=False)
    realized_vol_window_bars: Optional[int] = Field(default=None, ge=10, le=240)
    min_realized_vol: Optional[float] = Field(default=None, ge=0.0, le=3.0, allow_inf_nan=False)
    max_realized_vol: Optional[float] = Field(default=None, gt=0.0, le=3.0, allow_inf_nan=False)
    stop_loss_pct: Optional[float] = Field(default=None, gt=0.0, le=0.75, allow_inf_nan=False)
    profit_target_pct: Optional[float] = Field(default=None, gt=0.0, le=5.0, allow_inf_nan=False)


class StrategyV2ShadowConfigResponse(StrategyV2ShadowConfigValues):
    opening_momentum_execution_eligible: bool = True
    config_version: str
    updated_at: datetime
    estimated_round_trip_cost_pct: float = Field(ge=0.0, allow_inf_nan=False)
    estimated_net_reward_risk_ratio: float = Field(allow_inf_nan=False)
    minimum_net_reward_risk_ratio: float = 1.0


class StrategyV2ShadowDecisionResponse(BaseModel):
    id: int
    idempotency_key: str
    symbol: str
    market: str
    config_version: str
    observed_at: datetime
    bar_timestamp_1m: datetime
    bar_timestamp_5m: Optional[datetime] = None
    price: float
    vwap_1m: Optional[float] = None
    zscore_1m: Optional[float] = None
    vwap_5m: Optional[float] = None
    zscore_5m: Optional[float] = None
    adx: Optional[float] = None
    realized_vol: Optional[float] = None
    regime_eligible: bool
    breach_armed: bool
    action: str
    reason: str
    virtual_position: str
    reference_price: Optional[float] = None
    quantity: float = 0.0
    gross_pnl: Optional[float] = None
    fee: Optional[float] = None
    net_pnl: Optional[float] = None
    exit_reason: str = ""
    holding_minutes: Optional[float] = None
    mae_pct: Optional[float] = None
    mfe_pct: Optional[float] = None
    gate_reasons: list[str] = Field(default_factory=list)


class StrategyV2ShadowLatestResponse(BaseModel):
    observed_at: datetime
    data_age_seconds: float
    bar_timestamp_1m: Optional[datetime] = None
    bar_timestamp_5m: Optional[datetime] = None
    price: float
    vwap_1m: Optional[float] = None
    zscore_1m: Optional[float] = None
    vwap_5m: Optional[float] = None
    zscore_5m: Optional[float] = None
    adx: Optional[float] = None
    realized_vol: Optional[float] = None
    regime_eligible: bool
    breach_armed: bool
    virtual_position: Literal["FLAT", "LONG"]
    virtual_entry_price: Optional[float] = None
    virtual_entry_at: Optional[datetime] = None
    last_action: str
    last_reason: str


class StrategyV2ShadowDecisionPage(BaseModel):
    items: list[StrategyV2ShadowDecisionResponse]
    total: int
    page: int
    page_size: int


class StrategyV2ShadowTradeResponse(BaseModel):
    id: int
    symbol: str
    config_version: str
    status: str
    entry_at: datetime
    exit_at: Optional[datetime] = None
    entry_price: float
    exit_price: Optional[float] = None
    quantity: float
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    signal_vwap: Optional[float] = None
    holding_deadline: Optional[datetime] = None
    entry_reason: str
    exit_reason: str
    gross_pnl: Optional[float] = None
    estimated_fees: Optional[float] = None
    net_pnl: Optional[float] = None
    mfe_amount: Optional[float] = None
    mae_amount: Optional[float] = None
    mfe_pct: Optional[float] = None
    mae_pct: Optional[float] = None
    holding_seconds: Optional[float] = None
    fee_source: Literal["ESTIMATED"] = "ESTIMATED"
    estimated_fee_rate: Optional[float] = None

    model_config = {"from_attributes": True}


class StrategyV2ShadowMetrics(BaseModel):
    bars: int = 0
    eligible_bars: int = 0
    breaches: int = 0
    reclaims: int = 0
    entries: int = 0
    exits: int = 0
    closed_trades: int = 0
    win_rate: float = 0.0
    gross_pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0
    max_drawdown: float = 0.0
    avg_holding_minutes: float = 0.0
    avg_mae_pct: float = 0.0
    avg_mfe_pct: float = 0.0
    comparison_available: bool = False
    live_action_count: Optional[int] = None
    action_agreement_rate: Optional[float] = None
    net_pnl_delta_vs_live: Optional[float] = None


class StrategyV2ShadowStatusResponse(BaseModel):
    config: StrategyV2ShadowConfigResponse
    evidence_config_version: str
    version_transition_pending: bool = False
    latest: Optional[StrategyV2ShadowLatestResponse] = None
    metrics: StrategyV2ShadowMetrics
    gate_counts: dict[str, int] = Field(default_factory=dict)
    phase: str = "COLD"
    last_polled_at: Optional[datetime] = None
    last_poll_error: str = ""


class OpeningMomentumShadowConfigResponse(BaseModel):
    enabled: bool
    algorithm_version: str
    config_version: str
    mode: Literal["SHADOW"] = "SHADOW"
    order_submission_allowed: Literal[False] = False
    signal_minutes: int
    execution_delay_minutes: int
    holding_minutes: int
    minimum_universe_size: int
    minimum_market_return_bps: float
    minimum_candidate_return_bps: float
    minimum_excess_return_bps: float
    one_side_fee_rate: float
    one_side_slippage_bps: float
    round_trip_cost_bps: float
    stop_loss_pct: Optional[float] = None


class OpeningMomentumRankResponse(BaseModel):
    symbol: str
    opening_return_bps: float
    opening_activity_rank: Optional[int] = Field(default=None, ge=1)
    opening_activity_ratio: Optional[float] = Field(default=None, gt=0)


class OpeningMomentumShadowRunResponse(BaseModel):
    id: int
    session_date: date
    algorithm_version: str
    config_version: str
    status: Literal["SKIPPED", "OPEN", "CLOSED"]
    reason: str
    signal_at: datetime
    observed_at: datetime
    selection_run_id: Optional[int] = None
    universe_source: str
    universe_size: int
    universe: list[str] = Field(default_factory=list)
    excluded_symbols: dict[str, str] = Field(default_factory=dict)
    ranking: list[OpeningMomentumRankResponse] = Field(default_factory=list)
    candidate_symbol: Optional[str] = None
    market_return_bps: Optional[float] = None
    candidate_return_bps: Optional[float] = None
    excess_return_bps: Optional[float] = None
    candidate_first_five_return_bps: Optional[float] = None
    candidate_last_five_return_bps: Optional[float] = None
    candidate_path_efficiency: Optional[float] = None
    candidate_max_pullback_bps: Optional[float] = None
    candidate_opening_range_bps: Optional[float] = None
    candidate_signal_turnover: Optional[float] = Field(default=None, ge=0)
    candidate_avg_dollar_volume: Optional[float] = Field(default=None, ge=0)
    candidate_signal_turnover_ratio: Optional[float] = Field(
        default=None,
        ge=0,
    )
    candidate_opening_activity_ratio: Optional[float] = Field(
        default=None,
        gt=0,
    )
    candidate_overnight_gap_bps: Optional[float] = None
    candidate_prev_close_to_signal_bps: Optional[float] = None
    benchmark_qqq_return_bps: Optional[float] = None
    benchmark_dia_return_bps: Optional[float] = None
    benchmark_average_return_bps: Optional[float] = None
    entry_at: Optional[datetime] = None
    entry_price: Optional[float] = None
    exit_due_at: Optional[datetime] = None
    exit_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    gross_return_bps: Optional[float] = None
    estimated_cost_bps: float
    net_return_bps: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    maximum_adverse_excursion_bps: Optional[float] = None
    maximum_favorable_excursion_bps: Optional[float] = None


class OpeningMomentumShadowMetrics(BaseModel):
    observed_sessions: int = 0
    skipped_sessions: int = 0
    signals: int = 0
    open_trades: int = 0
    closed_trades: int = 0
    wins: int = 0
    win_rate: float = 0.0
    mean_net_return_bps: float = 0.0
    cumulative_net_return_bps: float = 0.0
    max_drawdown_bps: float = 0.0
    profit_factor: Optional[float] = None


class OpeningMomentumPairedComparisonResponse(BaseModel):
    resolved_sessions: int = 0
    cumulative_delta_bps: float = 0.0
    mean_delta_bps: float = 0.0
    outperformance_rate: float = 0.0
    confidence_lower_bps: Optional[float] = None
    confidence_upper_bps: Optional[float] = None
    max_drawdown_delta_bps: float = 0.0
    risk_guard_passed: bool = False
    minimum_promotion_sessions: int = 20
    policy_displacement_sessions: Optional[int] = None
    minimum_policy_displacement_sessions: Optional[int] = None
    displacement_outperformance_rate: Optional[float] = None
    evidence_gate_passed: Optional[bool] = None
    multiple_testing_method: Optional[
        Literal["HOLM_BONFERRONI"]
    ] = None
    multiple_testing_family_size: Optional[int] = None
    multiple_testing_adjusted_pvalue: Optional[float] = None
    multiple_testing_evidence_passed: Optional[bool] = None
    promotion_ready: bool = False
    recommendation: Literal[
        "COLLECTING",
        "EARLY_LEADER",
        "LAGGING",
        "INCONCLUSIVE",
        "UNDERPERFORMING",
        "PROMOTION_CANDIDATE",
    ] = "COLLECTING"


class OpeningMomentumShadowVariantResponse(BaseModel):
    variant: Literal[
        "INCUMBENT",
        "REVERSAL_CHALLENGER",
        "CONTINUATION_CHALLENGER",
        "BREADTH_GATED_CHALLENGER",
        "LAST5_POSITIVE_CHALLENGER",
        "LAST5_ONLY_CHALLENGER",
        "EARLY_BROAD_CHALLENGER",
        "EARLY_RKLB_CHALLENGER",
        "EARLY_WDAY_CHALLENGER",
        "EARLY_SNDK_CHALLENGER",
        "EARLY_ALAB_CHALLENGER",
        "EARLY_LITE_CHALLENGER",
        "EARLY_QCOM_CHALLENGER",
        "EXECUTION_BROAD_CHALLENGER",
        "EXECUTION_PATH_EFFICIENCY_CHALLENGER",
        "WEAK_BREADTH_PATH_CHALLENGER",
        "WEAK_BREADTH_RELAXED_CHALLENGER",
        "MODERATE_BREADTH_PATH_CHALLENGER",
        "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER",
        "QUALITY_FIRST_PATH_RERANK_CHALLENGER",
        "EXCEPTIONAL_PATH_PANW_COHORT_CHALLENGER",
        "WEAK_BREADTH_INDEX_COHORT_CHALLENGER",
        "WEAK_BREADTH_SPARSE_INDEX_COHORT_CHALLENGER",
        "WEAK_BREADTH_MRVL_EXCLUSION_CHALLENGER",
        "WEAK_BREADTH_WIDE_STOP_CHALLENGER",
        "ETF_REGIME_PATH_CHALLENGER",
        "ETF_REGIME_CRWD_CHALLENGER",
        "ETF_REGIME_TRV_CHALLENGER",
        "OPENING_RANGE_STOP_CHALLENGER",
        "FIVE_MINUTE_ORB_CHALLENGER",
        "STOCKS_IN_PLAY_ORB_CHALLENGER",
        "STOCKS_IN_PLAY_ORB_TOP10_CHALLENGER",
        "STOCKS_IN_PLAY_ORB_TOP5_CHALLENGER",
        "INDEX_CATALOG_FIVE_MINUTE_ORB_CHALLENGER",
        "INDEX_CATALOG_STOCKS_IN_PLAY_ORB_CHALLENGER",
        "INDEX_CATALOG_STOCKS_IN_PLAY_ORB_TOP10_CHALLENGER",
        "INDEX_CATALOG_STOCKS_IN_PLAY_ORB_TOP5_CHALLENGER",
        "INDEX_CATALOG_RELATIVE_VOLUME_ORB_TOP5_CHALLENGER",
        "INDEX_CATALOG_RELATIVE_VOLUME_ORB_TOP5_OPENING_RETURN_CHALLENGER",
        "EXECUTION_SNDK_CHALLENGER",
        "EXECUTION_INTC_CHALLENGER",
        "EXECUTION_QCOM_CHALLENGER",
        "EXECUTION_RKLB_CHALLENGER",
        "EXECUTION_PANW_CHALLENGER",
        "EXECUTION_CRWD_CHALLENGER",
    ]
    universe_source: str
    algorithm_version: str
    config_version: str
    candidate_selection_mode: Literal[
        "TOP_THEN_GATE",
        "PATH_ELIGIBLE_RERANK",
        "OPENING_ACTIVITY_TOP_N_THEN_BREAKOUT",
        "OPENING_ACTIVITY_TOP_N_THEN_OPENING_RETURN_BREAKOUT",
    ] = "TOP_THEN_GATE"
    signal_minutes: int
    minimum_market_return_bps: float
    minimum_candidate_return_bps: float
    minimum_excess_return_bps: float
    minimum_data_coverage: float = 1.0
    minimum_path_efficiency: Optional[float] = Field(
        default=None,
        ge=0,
        le=1,
    )
    maximum_market_return_bps: Optional[float] = None
    exceptional_minimum_path_efficiency: Optional[float] = Field(
        default=None,
        ge=0,
        le=1,
    )
    exceptional_maximum_market_return_bps: Optional[float] = None
    maximum_benchmark_average_return_bps: Optional[float] = None
    opening_activity_top_n: Optional[int] = Field(default=None, ge=1)
    opening_activity_baseline: Optional[
        Literal["DAILY_ADV_PROXY", "PRIOR_SAME_WINDOW_VOLUME"]
    ] = None
    opening_activity_lookback_sessions: Optional[int] = Field(
        default=None,
        ge=1,
    )
    minimum_opening_activity_ratio: Optional[float] = Field(
        default=None,
        gt=0,
    )
    required_symbols: list[str] = Field(default_factory=list)
    excluded_symbols: list[str] = Field(default_factory=list)
    holding_minutes: int
    stop_loss_pct: Optional[float] = None
    forward_evidence_start_date: Optional[date] = None
    excluded_pre_forward_sessions: int = Field(default=0, ge=0)
    comparison_sessions: int = 0
    comparison_baseline: Optional[
        Literal[
            "INCUMBENT",
            "EARLY_BROAD_CHALLENGER",
            "EXECUTION_BROAD_CHALLENGER",
            "WEAK_BREADTH_PATH_CHALLENGER",
            "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER",
            "ETF_REGIME_PATH_CHALLENGER",
            "FIVE_MINUTE_ORB_CHALLENGER",
            "INDEX_CATALOG_FIVE_MINUTE_ORB_CHALLENGER",
            "INDEX_CATALOG_RELATIVE_VOLUME_ORB_TOP5_CHALLENGER",
        ]
    ] = None
    latest: Optional[OpeningMomentumShadowRunResponse] = None
    metrics: OpeningMomentumShadowMetrics
    comparison: Optional[
        OpeningMomentumPairedComparisonResponse
    ] = None


class OpeningMomentumShadowStatusResponse(BaseModel):
    config: OpeningMomentumShadowConfigResponse
    state: Literal["DISABLED", "WAITING", "OPEN", "COLLECTING"]
    latest: Optional[OpeningMomentumShadowRunResponse] = None
    metrics: OpeningMomentumShadowMetrics
    variants: list[OpeningMomentumShadowVariantResponse] = Field(
        default_factory=list
    )


class OpeningMomentumExecutionConfigResponse(BaseModel):
    enabled: bool
    paper_account_confirmed: bool
    mode: Literal["PAPER_LIVE"] = "PAPER_LIVE"
    order_submission_allowed: bool
    algorithm_version: str
    config_version: str
    universe_source: str
    selection_run_id: Optional[int] = Field(default=None, ge=1)
    universe_size: int = Field(default=0, ge=0)
    universe: list[str] = Field(default_factory=list)
    required_symbols: list[str] = Field(default_factory=list)
    excluded_symbols: list[str] = Field(default_factory=list)
    universe_ready: bool = False
    signal_minutes: int
    execution_delay_minutes: int
    holding_minutes: int
    stop_loss_pct: float
    minimum_path_efficiency: Optional[float] = None
    maximum_market_return_bps: Optional[float] = None
    exceptional_minimum_path_efficiency: Optional[float] = Field(
        default=None,
        ge=0,
        le=1,
    )
    exceptional_maximum_market_return_bps: Optional[float] = None
    forward_evidence_start_date: Optional[date] = None
    max_entry_delay_seconds: int
    max_price_deviation_bps: float
    capital_slots: Literal[1] = 1


class OpeningMomentumExecutionResponse(BaseModel):
    id: int
    session_date: date
    algorithm_version: str
    config_version: str
    universe_source: str
    selection_run_id: Optional[int] = None
    status: Literal[
        "SKIPPED",
        "ARMED",
        "SUBMITTING",
        "SUBMITTED",
        "OPEN",
        "EXITING",
        "CLOSED",
        "REJECTED",
        "EXPIRED",
        "FAILED",
        "UNCERTAIN",
    ]
    reason: str
    symbol: Optional[str] = None
    signal_at: datetime
    armed_at: datetime
    entry_due_at: datetime
    entry_deadline_at: datetime
    requested_at: Optional[datetime] = None
    universe_size: int
    market_return_bps: Optional[float] = None
    candidate_return_bps: Optional[float] = None
    excess_return_bps: Optional[float] = None
    candidate_path_efficiency: Optional[float] = None
    candidate_signal_turnover: Optional[float] = Field(default=None, ge=0)
    candidate_avg_dollar_volume: Optional[float] = Field(default=None, ge=0)
    candidate_signal_turnover_ratio: Optional[float] = Field(
        default=None,
        ge=0,
    )
    reference_entry_price: Optional[float] = None
    max_price_deviation_bps: float
    stop_loss_pct: float
    max_holding_minutes: int
    signal_context: dict[str, Any] = Field(default_factory=dict)
    submit_attempts: int
    entry_order_id: str
    exit_order_id: str
    entry_filled_at: Optional[datetime] = None
    entry_price: Optional[float] = None
    quantity: Optional[float] = None
    exit_filled_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    net_pnl: Optional[float] = None


class OpeningMomentumExecutionStatusResponse(BaseModel):
    config: OpeningMomentumExecutionConfigResponse
    state: Literal[
        "DISABLED",
        "WAITING",
        "ARMED",
        "SUBMITTING",
        "SUBMITTED",
        "OPEN",
        "EXITING",
        "CLOSED",
        "SKIPPED",
        "REJECTED",
        "EXPIRED",
        "FAILED",
        "UNCERTAIN",
    ]
    latest: Optional[OpeningMomentumExecutionResponse] = None


class StrategyV2ShadowVersionResponse(BaseModel):
    symbol: str
    config_version: str
    activated_at: datetime
    current: bool
    params: dict[str, Any]
    observed_trading_days: int = 0
    bars: int = 0
    closed_trades: int = 0
    net_pnl: float = 0.0


class StrategyV2ShadowHourlyEvidence(BaseModel):
    session_hour: int = Field(ge=0, le=23)
    bars: int = 0
    ready_bars: int = 0
    eligible_bars: int = 0
    gate_counts: dict[str, int] = Field(default_factory=dict)


class StrategyV2ShadowDailyEvidence(BaseModel):
    session_date: date
    first_bar_at: datetime
    last_bar_at: datetime
    bars: int
    eligible_bars: int
    expected_internal_bars: int
    missing_internal_bars: int
    incomplete_feature_bars: int = 0
    coverage_ratio: float
    trades: int
    net_pnl: float
    exit_reasons: dict[str, int] = Field(default_factory=dict)
    partial_start: bool
    partial_end: bool
    outside_session_bars: int = 0
    complete_session: bool = False
    first_ready_at: Optional[datetime] = None
    ready_bars: int = 0
    warmup_lost_bars: int = 0
    hourly_eligibility: list[StrategyV2ShadowHourlyEvidence] = Field(
        default_factory=list
    )


class StrategyV2ShadowEvaluationResponse(BaseModel):
    symbol: str
    config_version: str
    mode: Literal["SHADOW"] = "SHADOW"
    order_submission_allowed: Literal[False] = False
    status: Literal["COLLECTING", "READY_FOR_REVIEW"]
    observed_trading_days: int
    excluded_trading_days: int = 0
    minimum_trading_days: int = 20
    minimum_session_coverage_ratio: float = 0.995
    remaining_trading_days: int
    closed_trades: int
    eligible_closed_trades: int = 0
    excluded_closed_trades: int = 0
    minimum_closed_trades: int = 50
    remaining_closed_trades: int
    first_bar_at: Optional[datetime] = None
    last_bar_at: Optional[datetime] = None
    bars: int = 0
    readiness_blockers: list[str] = Field(default_factory=list)
    data_quality_warnings: list[str] = Field(default_factory=list)
    quality: Optional[dict[str, Any]] = None
    daily: list[StrategyV2ShadowDailyEvidence] = Field(default_factory=list)


class StrategyV2AdxChallengerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    config_version: Optional[str] = Field(default=None, max_length=64)

    @field_validator("symbol")
    @classmethod
    def validate_challenger_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)


class StrategyV2AdxChallengerDaily(BaseModel):
    session_date: date
    bars: int = 0
    eligible_bars: int = 0
    breaches: int = 0
    reclaims: int = 0
    closed_trades: int = 0
    net_pnl: float = 0.0
    max_drawdown: float = 0.0
    exit_reasons: dict[str, int] = Field(default_factory=dict)


class StrategyV2AdxChallengerResult(BaseModel):
    label: Literal["BASELINE", "CHALLENGER"]
    max_adx: float
    config_version: str
    metrics: StrategyV2ShadowMetrics = Field(default_factory=StrategyV2ShadowMetrics)
    daily: list[StrategyV2AdxChallengerDaily] = Field(default_factory=list)


class StrategyV2WarmupDaily(BaseModel):
    session_date: date
    seed_session_date: date
    trend_context_cutoff_at: datetime
    overnight_gap_pct: float
    first_ready_at: Optional[datetime] = None
    bars: int = 0
    ready_bars: int = 0
    warmup_lost_bars: int = 0
    eligible_bars: int = 0
    hourly_eligibility: list[StrategyV2ShadowHourlyEvidence] = Field(
        default_factory=list
    )


class StrategyV2WarmupVariant(BaseModel):
    label: Literal["SESSION_LOCAL", "CAUSAL_TREND_PREWARM"]
    warmup_scope: Literal["NONE", "ADX_VOL_ONLY"]
    source_config_version: str
    metrics: StrategyV2ShadowMetrics = Field(default_factory=StrategyV2ShadowMetrics)
    daily: list[StrategyV2WarmupDaily] = Field(default_factory=list)


class StrategyV2WarmupDiagnostic(BaseModel):
    algorithm_version: Literal["strategy-v2-causal-trend-prewarm-v1"] = (
        "strategy-v2-causal-trend-prewarm-v1"
    )
    status: Literal[
        "INSUFFICIENT_EVIDENCE",
        "READY_FOR_REVIEW",
        "BLOCKED",
    ]
    minimum_causal_pairs: int = 5
    observed_causal_pairs: int = 0
    evaluated_causal_pairs: int = 0
    blockers: list[str] = Field(default_factory=list)
    same_sample: Literal[True] = True
    causal_history_only: Literal[True] = True
    vwap_zscore_session_local: Literal[True] = True
    variants: list[StrategyV2WarmupVariant] = Field(default_factory=list)


class StrategyV2BoundaryNeutralDiagnosticRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    config_version: Optional[str] = Field(default=None, max_length=64)

    @field_validator("symbol")
    @classmethod
    def validate_boundary_neutral_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)


class StrategyV2BoundaryNeutralCandidateSpec(BaseModel):
    schema_version: Literal[1] = 1
    algorithm_version: Literal[
        "strategy-v2-causal-trend-prewarm-boundary-neutral-v1"
    ] = "strategy-v2-causal-trend-prewarm-boundary-neutral-v1"
    legacy_algorithm_version: Literal[
        "strategy-v2-causal-trend-prewarm-v1"
    ] = "strategy-v2-causal-trend-prewarm-v1"
    evaluation_scope: Literal["RETROSPECTIVE_DIAGNOSTIC_ONLY"] = (
        "RETROSPECTIVE_DIAGNOSTIC_ONLY"
    )
    boundary_rule: Literal[
        "SEED_TARGET_FIRST_5M_DM_ZERO_TR_TARGET_RANGE"
    ] = "SEED_TARGET_FIRST_5M_DM_ZERO_TR_TARGET_RANGE"
    warmup_scope: Literal["ADX_VOL_ONLY"] = "ADX_VOL_ONLY"
    target_sample: Literal["SAME_PERSISTED_TARGET_BARS"] = (
        "SAME_PERSISTED_TARGET_BARS"
    )
    observation_schedule: Literal["SAME_PERSISTED_OBSERVED_AT"] = (
        "SAME_PERSISTED_OBSERVED_AT"
    )
    fee_slippage: Literal["SAME_FROZEN_SOURCE_CONFIG"] = (
        "SAME_FROZEN_SOURCE_CONFIG"
    )
    retrospective_results_forward_eligible: Literal[False] = False
    forward_evidence_requires_registration_before_target_open: Literal[True] = (
        True
    )
    order_submission_allowed: Literal[False] = False
    automatic_promotion_allowed: Literal[False] = False


class StrategyV2BoundaryNeutralVariant(BaseModel):
    label: Literal[
        "SESSION_LOCAL_BASELINE",
        "LEGACY_CAUSAL_TREND_PREWARM_V1",
        "BOUNDARY_NEUTRAL_CAUSAL_TREND_PREWARM_V1",
    ]
    algorithm_version: str
    warmup_scope: Literal["NONE", "ADX_VOL_ONLY"]
    source_config_version: str
    metrics: StrategyV2ShadowMetrics = Field(
        default_factory=StrategyV2ShadowMetrics
    )
    daily: list[StrategyV2WarmupDaily] = Field(default_factory=list)


class StrategyV2BoundaryNeutralDiagnosticResponse(BaseModel):
    persisted: Literal[False] = False
    mode: Literal["SHADOW"] = "SHADOW"
    evaluation_scope: Literal["RETROSPECTIVE_DIAGNOSTIC_ONLY"] = (
        "RETROSPECTIVE_DIAGNOSTIC_ONLY"
    )
    order_submission_allowed: Literal[False] = False
    automatic_promotion_allowed: Literal[False] = False
    retrospective_results_forward_eligible: Literal[False] = False
    forward_evidence_requires_registration_before_target_open: Literal[True] = (
        True
    )
    symbol: str
    source_config_version: str
    candidate_spec: StrategyV2BoundaryNeutralCandidateSpec = Field(
        default_factory=StrategyV2BoundaryNeutralCandidateSpec
    )
    candidate_spec_sha256: str = Field(min_length=64, max_length=64)
    status: Literal[
        "INSUFFICIENT_EVIDENCE",
        "READY_FOR_REVIEW",
        "BLOCKED",
    ]
    minimum_causal_pairs: int = 5
    observed_causal_pairs: int = 0
    evaluated_causal_pairs: int = 0
    blockers: list[str] = Field(default_factory=list)
    baseline_replay_match: Optional[bool] = None
    same_target_bars: Literal[True] = True
    same_observation_schedule: Literal[True] = True
    same_fee_slippage: Literal[True] = True
    causal_history_only: Literal[True] = True
    vwap_zscore_session_local: Literal[True] = True
    retrospective_target_sessions: list[date] = Field(default_factory=list)
    variants: list[StrategyV2BoundaryNeutralVariant] = Field(
        default_factory=list
    )


class StrategyV2ForwardRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    source_config_version: str = Field(min_length=64, max_length=64)
    candidate_algorithm_version: Literal[
        "strategy-v2-causal-trend-prewarm-v1",
        "strategy-v2-causal-trend-prewarm-boundary-neutral-v1",
    ] = "strategy-v2-causal-trend-prewarm-v1"
    confirm_forward_only: Literal[True]
    confirm_no_automatic_promotion: Literal[True]

    @field_validator("symbol")
    @classmethod
    def validate_forward_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @field_validator("source_config_version")
    @classmethod
    def validate_forward_source_version(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("source_config_version must be a SHA-256 hex digest")
        return normalized


class StrategyV2ForwardRegistrationResponse(BaseModel):
    id: int
    symbol: str
    market: Literal["US", "HK"]
    market_timezone: str
    candidate_algorithm_version: Literal[
        "strategy-v2-causal-trend-prewarm-v1",
        "strategy-v2-causal-trend-prewarm-boundary-neutral-v1",
    ]
    source_config_version: str
    evaluator_digest: str
    registered_at: datetime
    eligible_after: datetime
    minimum_ready_pairs: Literal[5] = 5
    minimum_mature_pairs: Literal[20] = 20


class StrategyV2ForwardDailyEvidence(BaseModel):
    target_session_date: date
    seed_session_date: Optional[date] = None
    target_open_at: datetime
    evaluated_at: datetime
    disposition: Literal["INCLUDED", "EXCLUDED"]
    exclusion_reason: str = ""
    structural_failure: bool = False
    target_bars: int = 0
    target_bars_sha256: str = ""
    seed_bars_sha256: str = ""
    baseline_input_sha256: str = ""
    candidate_input_sha256: str = ""
    same_target_bars: bool = False
    baseline_replay_match: Optional[bool] = None
    session_local_invariant: Optional[bool] = None
    baseline: Optional[StrategyV2WarmupDaily] = None
    candidate: Optional[StrategyV2WarmupDaily] = None
    baseline_metrics: Optional[StrategyV2ShadowMetrics] = None
    candidate_metrics: Optional[StrategyV2ShadowMetrics] = None
    baseline_result_sha256: str = ""
    candidate_result_sha256: str = ""
    evidence_digest_sha256: str = ""


class StrategyV2ForwardValidationResponse(BaseModel):
    registration: Optional[StrategyV2ForwardRegistrationResponse] = None
    status: Literal[
        "NOT_REGISTERED",
        "FROZEN",
        "COLLECTING",
        "READY_FOR_REVIEW",
        "MATURE_EVIDENCE",
        "BLOCKED",
    ]
    mode: Literal["SHADOW"] = "SHADOW"
    order_submission_allowed: Literal[False] = False
    automatic_promotion_allowed: Literal[False] = False
    historical_target_backfill_allowed: Literal[False] = False
    evaluation_scope: Literal["FORWARD_OUT_OF_SAMPLE"] = "FORWARD_OUT_OF_SAMPLE"
    included_pairs: int = 0
    excluded_targets: int = 0
    minimum_ready_pairs: Literal[5] = 5
    minimum_mature_pairs: Literal[20] = 20
    remaining_ready_pairs: int = 5
    remaining_mature_pairs: int = 20
    blockers: list[str] = Field(default_factory=list)
    baseline_metrics: StrategyV2ShadowMetrics = Field(
        default_factory=StrategyV2ShadowMetrics
    )
    candidate_metrics: StrategyV2ShadowMetrics = Field(
        default_factory=StrategyV2ShadowMetrics
    )
    daily: list[StrategyV2ForwardDailyEvidence] = Field(default_factory=list)


TrustedFrozenSha256 = Annotated[
    str,
    Field(pattern=r"^[0-9a-f]{64}$"),
]


class TrustedFrozenTradeSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    closed_trades: int = Field(ge=0)
    gross_pnl_decimal: str
    gross_pnl_float_hex: str
    fees_decimal: str
    fees_float_hex: str
    net_pnl_decimal: str
    net_pnl_float_hex: str
    closed_trade_entry_notional_decimal: str
    net_return_bps: float | None = None
    return_preimage_complete: bool
    ordered_trade_preimage_sha256: TrustedFrozenSha256


class TrustedFrozenDailyLeaf(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    symbol: str
    role: str
    config_hash: TrustedFrozenSha256
    session_date: str
    disposition: Literal[
        "PENDING",
        "MISSING",
        "INCLUDED",
        "EXCLUDED_NON_STRUCTURAL",
        "EXCLUDED_STRUCTURAL",
        "INVALID",
    ]
    exclusion_reason: str
    structural_failure: bool
    row_present_after_cutoff: bool
    evidence_id: int | None = None
    evidence_digest_sha256: TrustedFrozenSha256 | None = None
    baseline_result_sha256: TrustedFrozenSha256 | None = None
    candidate_result_sha256: TrustedFrozenSha256 | None = None
    artifact_digest_sha256: TrustedFrozenSha256 | None = None
    artifact_binding_sha256: TrustedFrozenSha256 | None = None
    daily_binding_sha256: TrustedFrozenSha256 | None = None
    baseline: TrustedFrozenTradeSummary | None = None
    candidate: TrustedFrozenTradeSummary | None = None
    blockers: list[str]
    leaf_digest_sha256: TrustedFrozenSha256


class TrustedFrozenSymbolReport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    symbol: str
    role: str
    reason: str
    config_hash: TrustedFrozenSha256
    registration_id: int | None = None
    candidate_algorithm_version: Literal[
        "strategy-v2-causal-trend-prewarm-v1"
    ] | None = None
    evaluator_digest: Literal[
        "e5ae9ea3e68dcc47d5131c21d8ba223824aecabf59da1f4b592df72cb9aa0294"
    ] | None = None
    registered_at: str | None = None
    eligible_after: str | None = None
    registration_blockers: list[str]
    pre_window_rows_excluded: int = Field(ge=0)
    post_window_rows_excluded: int = Field(ge=0)
    expected_session_count: Literal[252]
    evidence_root_sha256: TrustedFrozenSha256
    leaves: list[TrustedFrozenDailyLeaf] = Field(
        min_length=252,
        max_length=252,
    )


class TrustedFrozenCandidateReport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    symbol: str
    role: str
    reason: str
    config_hash: TrustedFrozenSha256
    status: Literal[
        "INSUFFICIENT_EVIDENCE",
        "READY_FOR_MANUAL_DISPROOF_REVIEW",
    ]
    evidence_review_ready: bool
    promotion_eligible: Literal[False]
    promotion_blockers: list[str]
    expected_session_count: Literal[252]
    candidate_included_sessions: int = Field(ge=0, le=252)
    nvda_included_sessions: int = Field(ge=0, le=252)
    paired_included_sessions: int = Field(ge=0, le=252)
    candidate_expected_session_coverage_ratio: float = Field(ge=0, le=1)
    nvda_expected_session_coverage_ratio: float = Field(ge=0, le=1)
    paired_expected_session_coverage_ratio: float = Field(ge=0, le=1)
    candidate_closed_trades: int = Field(ge=0)
    remaining_candidate_closed_trades: int = Field(ge=0)
    within_symbol_baseline: TrustedFrozenTradeSummary
    within_symbol_candidate: TrustedFrozenTradeSummary
    candidate_same_window: TrustedFrozenTradeSummary
    nvda_same_window_control: TrustedFrozenTradeSummary
    evidence_blockers: list[str]

    @model_validator(mode="after")
    def validate_candidate_readiness(self) -> "TrustedFrozenCandidateReport":
        ready_status = self.status == "READY_FOR_MANUAL_DISPROOF_REVIEW"
        if ready_status is not self.evidence_review_ready:
            raise ValueError("candidate status and readiness are inconsistent")
        required = {
            "QUANT_CANDIDATE_VETO_NOT_VERIFIED",
            "MANUAL_PROMOTION_REQUIRED",
        }
        if not required.issubset(self.promotion_blockers):
            raise ValueError("candidate permanent promotion blockers are missing")
        if self.evidence_review_ready:
            if self.evidence_blockers or "EVIDENCE_REVIEW_NOT_READY" in self.promotion_blockers:
                raise ValueError("ready candidate carries evidence blockers")
        elif "EVIDENCE_REVIEW_NOT_READY" not in self.promotion_blockers:
            raise ValueError("unready candidate lacks its promotion blocker")
        return self


class TrustedFrozenProducerCutoff(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    authority: Literal["SERVER_CLOCK_AND_STATIC_NYSE_CALENDAR"]
    observed_at: str
    complete_through: str | None = None
    cutoff_at: str | None = None
    finalization_delay_minutes: Literal[15]
    cutoff_provenance_verified: Literal[True]
    caller_cutoff_accepted: Literal[False]


class TrustedFrozenFreezeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: Literal["nasdaq-djia-disproof-2026-07-31"]
    as_of_date: Literal["2026-07-31"]
    freeze_digest: Literal[
        "d2005f023cc9e1874609008a55c2b0d21d1d30647175ca607e60225e4f7ea69f"
    ]
    candidate_algorithm_version: Literal[
        "strategy-v2-causal-trend-prewarm-v1"
    ]
    evaluator_digest: Literal[
        "e5ae9ea3e68dcc47d5131c21d8ba223824aecabf59da1f4b592df72cb9aa0294"
    ]
    control_symbol: Literal["NVDA.US"]


class TrustedFrozenAssessmentWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    market: Literal["US"]
    first_expected_session_date: Literal["2026-08-03"]
    last_expected_session_date: Literal["2027-08-03"]
    expected_session_count: Literal[252]
    expected_session_dates: list[str] = Field(
        min_length=252,
        max_length=252,
    )
    expected_session_digest: Literal[
        "3378303933970bc8dfc2bfb310ef3d98623d7e6a906e004353cc243a159621d0"
    ]
    denominator_is_fixed: Literal[True]
    missing_and_excluded_count_in_denominator: Literal[True]


class TrustedFrozenEvidenceThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    minimum_future_trade_days: Literal[20]
    minimum_closed_trades_per_candidate: Literal[50]
    minimum_expected_session_coverage_ratio: float = Field(ge=0.95, le=0.95)
    full_replay_verified_required: Literal[True]
    source_trace_archive_promotion_grade: Literal[False]
    quant_candidate_required_for_promotion: Literal[True]
    manual_promotion_required: Literal[True]
    thresholds_tunable: Literal[False]


class TrustedFrozenAssessmentReport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[
        "strategy-v2-frozen-forward-disproof-trusted-report-v3"
    ]
    algorithm_version: Literal[
        "strategy-v2-frozen-forward-disproof-trusted-algorithm-v3"
    ]
    policy_version: Literal[
        "strategy-v2-frozen-forward-disproof-trusted-assessment-v3"
    ]
    status: Literal[
        "INSUFFICIENT_EVIDENCE",
        "READY_FOR_MANUAL_DISPROOF_REVIEW",
    ]
    generated_at: str
    authority_mode: Literal["ONLINE_SERVER_DB_DIRECT_READ"]
    caller_authority_accepted: Literal[False]
    portable_attestation_verified: Literal[False]
    research_only: Literal[True]
    live_equivalent: Literal[False]
    order_submission_allowed: Literal[False]
    automatic_promotion_allowed: Literal[False]
    automatic_disproof_decision_allowed: Literal[False]
    evidence_review_ready: bool
    promotion_eligible: Literal[False]
    promotion_blockers: list[str]
    producer_cutoff: TrustedFrozenProducerCutoff
    freeze: TrustedFrozenFreezeIdentity
    assessment_window: TrustedFrozenAssessmentWindow
    evidence_thresholds: TrustedFrozenEvidenceThresholds
    symbols: list[TrustedFrozenSymbolReport] = Field(
        min_length=6,
        max_length=6,
    )
    candidates: list[TrustedFrozenCandidateReport] = Field(
        min_length=5,
        max_length=5,
    )
    report_digest_sha256: TrustedFrozenSha256

    @model_validator(mode="after")
    def validate_frozen_report(self) -> "TrustedFrozenAssessmentReport":
        from decimal import Decimal, InvalidOperation

        from app.domain.strategy_v2.frozen_disproof_queue import (
            CONTROL_SYMBOL,
            FROZEN_QUEUE_ENTRIES,
        )
        from app.domain.strategy_v2.trusted_frozen_assessment import (
            TrustedDailyLeaf as DomainDailyLeaf,
            TrustedProducerCutoff as DomainProducerCutoff,
            TrustedSymbolEvidence as DomainSymbolEvidence,
            TrustedTradeSummary as DomainTradeSummary,
            build_trusted_assessment_report,
        )

        expected = {
            symbol: (role, reason, config_hash)
            for symbol, role, reason, config_hash in FROZEN_QUEUE_ENTRIES
        }
        by_symbol = {item.symbol: item for item in self.symbols}
        if len(by_symbol) != 6 or set(by_symbol) != set(expected):
            raise ValueError("trusted report symbol cohort is invalid")
        expected_dates = self.assessment_window.expected_session_dates
        dates_digest = hashlib.sha256(
            "\n".join(expected_dates).encode("ascii")
        ).hexdigest()
        if dates_digest != self.assessment_window.expected_session_digest:
            raise ValueError("trusted report session schedule digest is invalid")
        for symbol, (role, reason, config_hash) in expected.items():
            item = by_symbol[symbol]
            if (
                item.role != role
                or item.reason != reason
                or item.config_hash != config_hash
                or [leaf.session_date for leaf in item.leaves] != expected_dates
                or any(
                    leaf.symbol != symbol
                    or leaf.role != role
                    or leaf.config_hash != config_hash
                    for leaf in item.leaves
                )
            ):
                raise ValueError("trusted report symbol evidence identity is invalid")
        expected_candidates = set(expected) - {CONTROL_SYMBOL}
        candidate_symbols = [item.symbol for item in self.candidates]
        if (
            candidate_symbols != sorted(expected_candidates)
            or set(candidate_symbols) != expected_candidates
        ):
            raise ValueError("trusted report candidate cohort is invalid")
        for candidate in self.candidates:
            role, reason, config_hash = expected[candidate.symbol]
            symbol_report = by_symbol[candidate.symbol]
            if (
                candidate.role != role
                or candidate.reason != reason
                or candidate.config_hash != config_hash
                or candidate.role != symbol_report.role
                or candidate.reason != symbol_report.reason
                or candidate.config_hash != symbol_report.config_hash
            ):
                raise ValueError(
                    "trusted report candidate frozen identity is invalid"
                )
        all_ready = all(item.evidence_review_ready for item in self.candidates)
        if (
            self.evidence_review_ready is not all_ready
            or (self.status == "READY_FOR_MANUAL_DISPROOF_REVIEW") is not all_ready
        ):
            raise ValueError("trusted report status and readiness are inconsistent")
        required = {
            "QUANT_CANDIDATE_VETO_NOT_VERIFIED",
            "MANUAL_PROMOTION_REQUIRED",
        }
        if not required.issubset(self.promotion_blockers):
            raise ValueError("trusted report permanent blockers are missing")
        if all_ready:
            if "EVIDENCE_REVIEW_NOT_READY" in self.promotion_blockers:
                raise ValueError("ready report carries an evidence blocker")
        elif "EVIDENCE_REVIEW_NOT_READY" not in self.promotion_blockers:
            raise ValueError("unready report lacks its evidence blocker")
        model_payload = self.model_dump(mode="python")
        digest_payload = dict(model_payload)
        claimed_digest = str(digest_payload.pop("report_digest_sha256"))
        encoded = json.dumps(
            digest_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != claimed_digest:
            raise ValueError("trusted report digest is invalid")

        def domain_summary(
            item: TrustedFrozenTradeSummary | None,
        ) -> DomainTradeSummary | None:
            if item is None:
                return None
            return DomainTradeSummary(
                closed_trades=item.closed_trades,
                gross_pnl=float.fromhex(item.gross_pnl_float_hex),
                fees=float.fromhex(item.fees_float_hex),
                net_pnl=float.fromhex(item.net_pnl_float_hex),
                entry_notional=Decimal(
                    item.closed_trade_entry_notional_decimal
                ),
                ordered_trade_preimage_sha256=(
                    item.ordered_trade_preimage_sha256
                ),
            )

        try:
            domain_symbols = tuple(
                DomainSymbolEvidence(
                    symbol=item.symbol,
                    role=item.role,
                    reason=item.reason,
                    config_hash=item.config_hash,
                    registration_id=item.registration_id,
                    candidate_algorithm_version=(
                        item.candidate_algorithm_version
                    ),
                    evaluator_digest=item.evaluator_digest,
                    registered_at=(
                        datetime.fromisoformat(item.registered_at)
                        if item.registered_at is not None
                        else None
                    ),
                    eligible_after=(
                        datetime.fromisoformat(item.eligible_after)
                        if item.eligible_after is not None
                        else None
                    ),
                    registration_blockers=tuple(
                        item.registration_blockers
                    ),
                    pre_window_rows_excluded=(
                        item.pre_window_rows_excluded
                    ),
                    post_window_rows_excluded=(
                        item.post_window_rows_excluded
                    ),
                    leaves=tuple(
                        DomainDailyLeaf(
                            symbol=leaf.symbol,
                            role=leaf.role,
                            config_hash=leaf.config_hash,
                            session_date=date.fromisoformat(
                                leaf.session_date
                            ),
                            disposition=leaf.disposition,
                            exclusion_reason=leaf.exclusion_reason,
                            structural_failure=leaf.structural_failure,
                            row_present_after_cutoff=(
                                leaf.row_present_after_cutoff
                            ),
                            evidence_id=leaf.evidence_id,
                            evidence_digest_sha256=(
                                leaf.evidence_digest_sha256
                            ),
                            baseline_result_sha256=(
                                leaf.baseline_result_sha256
                            ),
                            candidate_result_sha256=(
                                leaf.candidate_result_sha256
                            ),
                            artifact_digest_sha256=(
                                leaf.artifact_digest_sha256
                            ),
                            artifact_binding_sha256=(
                                leaf.artifact_binding_sha256
                            ),
                            daily_binding_sha256=(
                                leaf.daily_binding_sha256
                            ),
                            baseline=domain_summary(leaf.baseline),
                            candidate=domain_summary(leaf.candidate),
                            blockers=tuple(leaf.blockers),
                            leaf_digest_sha256=(
                                leaf.leaf_digest_sha256
                            ),
                        )
                        for leaf in item.leaves
                    ),
                )
                for item in self.symbols
            )
            cutoff = DomainProducerCutoff(
                observed_at=datetime.fromisoformat(
                    self.producer_cutoff.observed_at
                ),
                complete_through=(
                    date.fromisoformat(
                        self.producer_cutoff.complete_through
                    )
                    if self.producer_cutoff.complete_through is not None
                    else None
                ),
                cutoff_at=(
                    datetime.fromisoformat(self.producer_cutoff.cutoff_at)
                    if self.producer_cutoff.cutoff_at is not None
                    else None
                ),
            )
            rebuilt = build_trusted_assessment_report(
                domain_symbols,
                producer_cutoff=cutoff,
            )
        except (
            InvalidOperation,
            OverflowError,
            TypeError,
            ValueError,
        ) as exc:
            raise ValueError(
                "trusted report domain attestation is invalid"
            ) from exc
        if rebuilt != model_payload:
            raise ValueError(
                "trusted report does not match its canonical domain rebuild"
            )
        return self


class StrategyV2ExitChallengerVariant(BaseModel):
    registration_id: int
    algorithm_version: str
    source_config_version: str
    evaluator_digest: str
    policy_type: Literal["PROFIT_LOCK", "TIME_STOP"] = "PROFIT_LOCK"
    activation_pct: float
    locked_profit_pct: float
    max_holding_minutes: int | None = None
    slippage_bps: float
    registered_at: datetime
    eligible_after: datetime
    status: Literal["COLLECTING", "READY_FOR_REVIEW", "MATURE_EVIDENCE"]
    paired_trades: int = 0
    open_trades: int = 0
    awaiting_baseline_trades: int = 0
    profit_lock_exits: int = 0
    time_stop_exits: int = 0
    improved_trades: int = 0
    worsened_trades: int = 0
    unchanged_trades: int = 0
    baseline_win_rate: float = 0.0
    challenger_win_rate: float = 0.0
    baseline_net_pnl: float = 0.0
    challenger_net_pnl: float = 0.0
    net_pnl_delta: float = 0.0
    mean_net_pnl_delta: float = 0.0
    baseline_mean_holding_minutes: float = 0.0
    challenger_mean_holding_minutes: float = 0.0
    mean_holding_minutes_saved: float = 0.0
    baseline_max_drawdown: float = 0.0
    challenger_max_drawdown: float = 0.0
    minimum_ready_pairs: Literal[20] = 20
    minimum_mature_pairs: Literal[50] = 50
    minimum_profit_lock_exits: Literal[5] = 5
    minimum_time_stop_exits: Literal[5] = 5
    promotion_ready: bool = False
    blockers: list[str] = Field(default_factory=list)


class StrategyV2ExitChallengerReport(BaseModel):
    symbol: str
    mode: Literal["SHADOW"] = "SHADOW"
    order_submission_allowed: Literal[False] = False
    automatic_promotion_allowed: Literal[False] = False
    historical_backfill_allowed: Literal[False] = False
    evaluation_scope: Literal["FORWARD_OUT_OF_SAMPLE"] = "FORWARD_OUT_OF_SAMPLE"
    variants: list[StrategyV2ExitChallengerVariant] = Field(default_factory=list)


class StrategyV2BracketChallengerVariant(BaseModel):
    registration_id: int
    algorithm_version: str
    source_config_version: str
    evaluator_digest: str
    stop_loss_pct: float
    profit_target_pct: float
    vwap_target_cap_bps: float | None = None
    slippage_bps: float
    estimated_fee_rate: float
    max_holding_minutes: int
    flatten_minutes_before_close: int
    estimated_round_trip_cost_pct: float
    estimated_net_reward_risk_ratio: float
    registered_at: datetime
    eligible_after: datetime
    status: Literal["COLLECTING", "READY_FOR_REVIEW", "MATURE_EVIDENCE"]
    paired_trades: int = 0
    open_trades: int = 0
    awaiting_baseline_trades: int = 0
    changed_exits: int = 0
    exit_reasons: dict[str, int] = Field(default_factory=dict)
    baseline_exit_reasons: dict[str, int] = Field(default_factory=dict)
    improved_trades: int = 0
    worsened_trades: int = 0
    unchanged_trades: int = 0
    baseline_win_rate: float = 0.0
    challenger_win_rate: float = 0.0
    baseline_net_pnl: float = 0.0
    challenger_net_pnl: float = 0.0
    net_pnl_delta: float = 0.0
    mean_net_pnl_delta: float = 0.0
    baseline_max_drawdown: float = 0.0
    challenger_max_drawdown: float = 0.0
    minimum_ready_pairs: Literal[20] = 20
    minimum_mature_pairs: Literal[50] = 50
    minimum_changed_exits: Literal[5] = 5
    promotion_ready: bool = False
    blockers: list[str] = Field(default_factory=list)


class StrategyV2BracketChallengerReport(BaseModel):
    symbol: str
    mode: Literal["SHADOW"] = "SHADOW"
    order_submission_allowed: Literal[False] = False
    automatic_promotion_allowed: Literal[False] = False
    historical_backfill_allowed: Literal[False] = False
    evaluation_scope: Literal[
        "FORWARD_OUT_OF_SAMPLE"
    ] = "FORWARD_OUT_OF_SAMPLE"
    variants: list[StrategyV2BracketChallengerVariant] = Field(
        default_factory=list
    )


class LiveExitChallengerVariant(BaseModel):
    registration_id: int
    algorithm_version: str
    evaluator_digest: str
    policy_type: Literal["PROFIT_LOCK", "TIME_STOP"] = "PROFIT_LOCK"
    activation_pct: float
    locked_profit_pct: float
    max_holding_minutes: Optional[int] = None
    slippage_bps: float
    registered_at: datetime
    eligible_after: datetime
    status: Literal["COLLECTING", "READY_FOR_REVIEW", "MATURE_EVIDENCE"]
    entry_config_versions: list[str] = Field(default_factory=list)
    paired_trades: int = 0
    open_trades: int = 0
    awaiting_baseline_trades: int = 0
    profit_lock_exits: int = 0
    time_stop_exits: int = 0
    improved_trades: int = 0
    worsened_trades: int = 0
    unchanged_trades: int = 0
    baseline_win_rate: float = 0.0
    challenger_win_rate: float = 0.0
    baseline_net_pnl: float = 0.0
    challenger_net_pnl: float = 0.0
    net_pnl_delta: float = 0.0
    mean_net_pnl_delta: float = 0.0
    baseline_mean_holding_minutes: float = 0.0
    challenger_mean_holding_minutes: float = 0.0
    mean_holding_minutes_saved: float = 0.0
    baseline_max_drawdown: float = 0.0
    challenger_max_drawdown: float = 0.0
    minimum_ready_pairs: Literal[20] = 20
    minimum_mature_pairs: Literal[50] = 50
    minimum_profit_lock_exits: Literal[5] = 5
    minimum_time_stop_exits: Literal[5] = 5
    promotion_ready: bool = False
    blockers: list[str] = Field(default_factory=list)


class LiveExitChallengerReport(BaseModel):
    symbol: str
    enabled: bool
    mode: Literal["LIVE_BASELINE_SHADOW"] = "LIVE_BASELINE_SHADOW"
    order_submission_allowed: Literal[False] = False
    automatic_promotion_allowed: Literal[False] = False
    historical_backfill_allowed: Literal[False] = False
    evaluation_scope: Literal[
        "FORWARD_LIVE_BASELINE"
    ] = "FORWARD_LIVE_BASELINE"
    variants: list[LiveExitChallengerVariant] = Field(default_factory=list)


class StrategyV2PortfolioRoutingMetrics(BaseModel):
    signal_groups: int = 0
    selected_signals: int = 0
    skipped_occupied: int = 0
    no_eligible: int = 0
    diagnosed_no_eligible: int = 0
    no_causal_signal_groups: int = 0
    rejection_counts: dict[str, int] = Field(default_factory=dict)
    pending_entries: int = 0
    open_trades: int = 0
    missed_entries: int = 0
    closed_trades: int = 0
    observed_sessions: int = 0
    distinct_symbols: int = 0
    win_rate: float = 0.0
    mean_net_return_pct: float = 0.0
    cumulative_net_return_pct: float = 0.0
    compounded_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    selections_by_symbol: dict[str, int] = Field(default_factory=dict)
    latest_signal_at: Optional[datetime] = None


class StrategyV2PortfolioRoutingVariant(BaseModel):
    registration_id: int
    policy: Literal[
        "FIXED_PRIMARY",
        "FIXED_CANDIDATE",
        "SELECTED_UNIVERSE",
        "QUANT_CANDIDATE",
        "QUANT_WATCH_PLUS",
        "SELECTED_VWAP_EDGE",
        "VWAP_EDGE_POOL",
        "VWAP_EDGE_75BPS_POOL",
        "VWAP_EDGE_OBSERVED_COST_POOL",
        "VWAP_EDGE_OBS_COST_75BPS_POOL",
        "RISK_GROUP_REL_OBS_75BPS_POOL",
        "RISK_GROUP_LOO_OBS_75BPS_POOL",
        "SECTOR_LOO_OBS_75BPS_POOL",
        "SELECTED_SECTOR_LOO_OBS_75BPS_POOL",
        "SELECTED_ZSCORE_OBS_75BPS_POOL",
        "ROTATION_ZSCORE_OBS_75BPS_POOL",
        "ROTATION_IV_WEIGHTED_ZSCORE_POOL",
        "ROTATION_IV_NET_EDGE_ZSCORE_POOL",
        "PIT_SHRINK_WEIGHTED_ZSCORE_POOL",
        "PIT_SHRINK_NET_EDGE_ZSCORE_POOL",
    ]
    algorithm_version: str
    evaluator_digest: str
    registered_at: datetime
    eligible_after: datetime
    target_symbol: Optional[str] = None
    edge_filter: Literal[
        "NONE",
        "COST_TO_STOP_VWAP_DISCOUNT",
        "COST_TO_75BPS_VWAP_DISCOUNT",
        "OBSERVED_COST_TO_STOP_VWAP_DISCOUNT",
        "OBSERVED_COST_TO_75BPS_VWAP_DISCOUNT",
        "RISK_GROUP_REL_OBS_COST_TO_75BPS",
        "RISK_GROUP_LOO_OBS_COST_TO_75BPS",
        "SECTOR_LOO_OBS_COST_TO_75BPS",
        "ZSCORE_OBS_COST_TO_75BPS",
    ] = "NONE"
    status: Literal["COLLECTING", "READY_FOR_REVIEW", "MATURE_EVIDENCE"]
    metrics: StrategyV2PortfolioRoutingMetrics = Field(
        default_factory=StrategyV2PortfolioRoutingMetrics
    )
    fixed_primary_compounded_return_pct: float = 0.0
    compounded_return_delta_pct: float = 0.0
    minimum_ready_trades: Literal[20] = 20
    minimum_mature_trades: Literal[50] = 50
    minimum_ready_sessions: Literal[10] = 10
    minimum_routed_symbols: int = Field(default=3, ge=1)
    promotion_ready: bool = False
    blockers: list[str] = Field(default_factory=list)


class StrategyV2PortfolioRoutingReport(BaseModel):
    primary_symbol: str = ""
    mode: Literal["SHADOW"] = "SHADOW"
    order_submission_allowed: Literal[False] = False
    automatic_promotion_allowed: Literal[False] = False
    historical_backfill_allowed: Literal[False] = False
    capital_slots: Literal[1] = 1
    evaluation_scope: Literal["FORWARD_OUT_OF_SAMPLE"] = (
        "FORWARD_OUT_OF_SAMPLE"
    )
    variants: list[StrategyV2PortfolioRoutingVariant] = Field(
        default_factory=list
    )


class StrategyV2AdxChallengerResponse(BaseModel):
    persisted: Literal[False] = False
    mode: Literal["SHADOW"] = "SHADOW"
    order_submission_allowed: Literal[False] = False
    evaluation_scope: Literal["EXPLORATORY_IN_SAMPLE"] = "EXPLORATORY_IN_SAMPLE"
    promotion_eligible: Literal[False] = False
    forward_validation_required: Literal[True] = True
    symbol: str
    source_config_version: str
    status: Literal[
        "INSUFFICIENT_EVIDENCE",
        "READY_FOR_REVIEW",
        "BLOCKED",
    ]
    minimum_complete_sessions: int = 5
    observed_complete_sessions: int = 0
    evaluated_complete_sessions: int = 0
    baseline_replay_match: Optional[bool] = None
    blockers: list[str] = Field(default_factory=list)
    candidates: list[StrategyV2AdxChallengerResult] = Field(default_factory=list)
    warmup_diagnostic: Optional[StrategyV2WarmupDiagnostic] = None


class StrategyV2ReplayBar(BaseModel):
    timestamp: datetime
    open: float = Field(gt=0, allow_inf_nan=False)
    high: float = Field(gt=0, allow_inf_nan=False)
    low: float = Field(gt=0, allow_inf_nan=False)
    close: float = Field(gt=0, allow_inf_nan=False)
    volume: float = Field(default=0.0, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_ohlc(self) -> "StrategyV2ReplayBar":
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be at least open, low, and close")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be at most open, high, and close")
        return self


class StrategyV2ShadowReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    market: Literal["US", "HK"]
    bars: list[StrategyV2ReplayBar] = Field(min_length=2, max_length=20_000)

    @field_validator("symbol")
    @classmethod
    def validate_replay_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @model_validator(mode="after")
    def validate_replay_market(self) -> "StrategyV2ShadowReplayRequest":
        _validate_symbol_market_pair(self.symbol, self.market)
        return self


class StrategyV2ShadowReplayResponse(BaseModel):
    persisted: Literal[False] = False
    config_version: str
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    trades: list[dict[str, Any]] = Field(default_factory=list)
    metrics: StrategyV2ShadowMetrics = Field(default_factory=StrategyV2ShadowMetrics)


class StatusResponse(BaseModel):
    engine_state: str
    paused: bool
    kill_switch: bool
    protective_exit_permitted: bool = False
    runner_running: bool = False
    daily_pnl: float
    consecutive_losses: int
    cumulative_realized_pnl: float = 0.0
    peak_realized_pnl: float = 0.0
    drawdown_amount: float = 0.0
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


class StatisticsQualityItem(BaseModel):
    trade_day: str
    symbol: str
    issue_code: str
    exit_order_id: int
    broker_order_id: str = ""
    side: str = ""
    filled_quantity: float
    matched_quantity: float
    unmatched_quantity: float
    exclusion_id: Optional[int] = None
    reason: str = ""

    model_config = {"from_attributes": True}


class StatisticsQuality(BaseModel):
    status: Literal[
        "COMPLETE",
        "KNOWN_EXCLUSIONS",
        "UNRESOLVED",
        "STALE_EXCLUSION",
    ] = "COMPLETE"
    known_exclusion_count: int = 0
    unresolved_issue_count: int = 0
    omitted_day_count: int = 0
    items: list[StatisticsQualityItem] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class MetricsValueSummary(BaseModel):
    trade_count: int = Field(ge=0)
    win_rate: float = Field(ge=0, le=100)
    profit_factor: float | None = Field(default=None, ge=0)
    sharpe_ratio: float | None = None
    avg_pnl: float
    total_pnl: float
    max_drawdown: float = Field(
        ge=0,
        description="Legacy cumulative-PnL drawdown percentage.",
    )
    max_drawdown_amount: float = Field(
        ge=0,
        description="Maximum cumulative realized-PnL drawdown in currency units.",
    )


class MetricsCurrencySummary(MetricsValueSummary):
    currency: Literal["USD", "HKD"]


class MetricsSummaryResponse(BaseModel):
    trade_count: int = Field(ge=0)
    win_rate: float = Field(ge=0, le=100)
    profit_factor: float | None = Field(default=None, ge=0)
    sharpe_ratio: float | None = None
    avg_pnl: float | None = None
    total_pnl: float | None = None
    max_drawdown: float | None = Field(
        default=None,
        ge=0,
        description="Legacy cumulative-PnL drawdown percentage.",
    )
    max_drawdown_amount: float | None = Field(
        default=None,
        ge=0,
        description="Maximum cumulative realized-PnL drawdown in currency units.",
    )
    window_days: int = Field(ge=1, le=365)
    currency: Literal["USD", "HKD", "MIXED"] | None = None
    totals_comparable: bool = True
    by_currency: list[MetricsCurrencySummary] = Field(default_factory=list)
    statistics_quality: StatisticsQuality


class ReportResponse(BaseModel):
    period_type: str
    symbol: str
    start_date: str
    end_date: str
    metrics: ReportMetrics
    daily_points: list[ReportDailyPoint]
    attribution: list[ReportAttributionPoint] = Field(default_factory=list)
    details: list[ReportDayDetail] = Field(default_factory=list)
    statistics_quality: StatisticsQuality

    model_config = {"from_attributes": True}


class DiagnosticQuoteStream(BaseModel):
    last_push_age_seconds: float | None = None
    last_quote_age_seconds: float | None = None
    recent_quote_count: int


class DiagnosticRiskState(BaseModel):
    paused: bool
    kill_switch: bool
    pause_reason: str = ""
    protective_exit_permitted: bool = False
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
    position_quantity: float = 0.0
    position_avg_price: float = 0.0
    position_notional: float = 0.0
    position_risk_at_stop: float = 0.0
    position_limit_breaches: list[str] = Field(default_factory=list)


class DiagnosticLiveSafety(BaseModel):
    full_buying_power_usage_enabled: bool = False
    buying_power_usage_pct: float = 90.0
    short_entries_enabled: bool
    allow_position_addons: bool
    max_position_quantity: int
    max_position_notional: float
    max_risk_per_trade: float
    stop_loss_pct: float
    max_holding_minutes: int
    opening_warmup_minutes: int = 5
    live_entry_crossing_required: bool = False
    live_entry_crossing_max_age_seconds: int = 30
    entry_cutoff_minutes_before_close: int
    flatten_minutes_before_close: int
    llm_shadow_mode: bool
    llm_order_execution_enabled: bool
    live_regime_gate_enabled: bool
    live_regime_max_data_age_seconds: int
    live_max_entries_per_symbol_per_day: int


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
    dedup_suppressed_total: int
    dedup_window_seconds: float
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
    decision_bid: Optional[float] = None
    decision_ask: Optional[float] = None
    quote_age_ms: Optional[float] = None
    config_version: str = ""
    ack_latency_ms: Optional[float] = None
    fill_latency_ms: Optional[float] = None
    estimated_fee: Optional[float] = None
    actual_fee: Optional[float] = None
    fee_currency: str = ""
    fee_source: str = "UNKNOWN"
    slippage_amount: Optional[float] = None
    slippage_bps: Optional[float] = None
    exit_cause: str = ""
    exit_reason: str = ""
    gross_pnl: Optional[float] = None
    net_pnl: Optional[float] = None
    pnl_source: str = "UNKNOWN"
    cost_basis_price: Optional[float] = None
    cost_basis_quantity: Optional[float] = None
    cost_basis_opened_at: Optional[datetime] = None
    position_quantity_before: Optional[float] = None
    pnl_fee: Optional[float] = None
    pnl_fee_source: str = "UNKNOWN"
    pnl_fee_rate: Optional[float] = None

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


class OrderCancelAllRequest(BaseModel):
    symbol: Optional[str] = Field(default=None, max_length=50)


class OrderCancelFailure(BaseModel):
    order_id: str
    error: str


class OrderCancelAllResponse(BaseModel):
    cancelled: int
    failed: list[OrderCancelFailure]
    skipped: int
    total_pending: int


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


class AuditLogOut(BaseModel):
    """Safe projection of an ``audit_logs`` row for the browse API.

    Mirrors the fields the existing ``/api/events`` audit export intentionally
    exposes (``actor_hash``, ``source_ip``, ``severity``, ``result``). The
    ``request_summary`` is a write-time-bounded, deliberately sanitized summary
    dict (never a raw request body or secret); if it is not valid JSON it is
    returned as ``{"raw": ...}``.
    """

    id: int
    action: str
    severity: str
    result: str
    actor_hash: str
    source_ip: str
    request_summary: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AuditLogPage(BaseModel):
    items: list[AuditLogOut]
    total: int
    limit: int
    offset: int


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


class BrokerBuyingPowerResponse(BaseModel):
    """Read-only broker capacity estimate for one security and limit price."""

    symbol: str
    side: Literal["BUY", "SELL"]
    market: Literal["US", "HK"]
    currency: Literal["USD", "HKD"]
    price: float = Field(gt=0, allow_inf_nan=False)
    available_cash: float = Field(allow_inf_nan=False)
    max_quantity: float = Field(ge=0, allow_inf_nan=False)
    buying_power: float = Field(ge=0, allow_inf_nan=False)
    is_trading_hours: bool
    estimated_at: datetime


class BacktestParams(BaseModel):
    symbol: str = Field(default="", max_length=50)
    market: Literal["US", "HK"] = "US"
    trading_session_mode: Literal["ANY", "RTH_ONLY"] = "ANY"
    opening_warmup_minutes: int = Field(default=0, ge=0, le=390)
    entry_crossing_required: bool = False
    max_entries_per_symbol_per_day: int = Field(default=0, ge=0, le=1_000)
    buy_low: float = Field(gt=0)
    sell_high: float = Field(gt=0)
    short_selling: bool = Field(default=False)
    min_profit_amount: float = Field(default=0.0, ge=0)
    max_daily_loss: float = Field(default=5000.0, ge=0, allow_inf_nan=False)
    max_drawdown_amount: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    max_consecutive_losses: int = Field(default=3, ge=1)
    quantity: float = Field(default=1.0, gt=0)
    initial_cash: float = Field(default=100000.0, gt=0)
    fee_rate: float = Field(default=0.0, ge=0, le=0.1)
    fixed_fee: float = Field(default=0.0, ge=0)
    slippage_pct: float = Field(default=0.0, ge=0, le=5)
    stop_loss_pct: float = Field(default=0.0, ge=0, le=100)
    trailing_stop_pct: float = Field(default=0.0, ge=0, le=100)
    max_holding_minutes: int = Field(default=0, ge=0, le=10_080)
    entry_cutoff_minutes_before_close: int = Field(default=0, ge=0, le=180)
    flatten_minutes_before_close: int = Field(default=0, ge=0, le=180)

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_backtest_market(cls, data: Any) -> Any:
        # ``market`` did not exist in older persisted backtest/experiment JSON.
        # Preserve those records by inferring the only unambiguous suffixes;
        # an explicitly supplied mismatch is still rejected below.
        if not isinstance(data, dict) or "market" in data:
            return data
        symbol = str(data.get("symbol") or "").strip().upper()
        suffix = symbol.rsplit(".", 1)[-1] if "." in symbol else ""
        if suffix not in {"US", "HK"}:
            return data
        normalized = dict(data)
        normalized["market"] = suffix
        return normalized

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

    @model_validator(mode="after")
    def validate_backtest_execution_windows(self) -> "BacktestParams":
        if self.symbol:
            _validate_symbol_market_pair(self.symbol, self.market)
        if (
            self.entry_cutoff_minutes_before_close > 0
            and self.flatten_minutes_before_close > 0
            and self.flatten_minutes_before_close
            > self.entry_cutoff_minutes_before_close
        ):
            raise ValueError(
                "flatten_minutes_before_close must not exceed "
                "entry_cutoff_minutes_before_close"
            )
        return self


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
    gross_pnl: Optional[float] = None
    net_pnl: Optional[float] = None
    total_fees: Optional[float] = None
    mfe_amount: Optional[float] = None
    mae_amount: Optional[float] = None
    mfe_pct: Optional[float] = None
    mae_pct: Optional[float] = None


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
    """Create or fully replace an alert rule.

    ``rule_type`` semantics:
      - ``price_above`` / ``price_below``: live quote vs ``threshold`` (price rules).
      - ``daily_loss``: active ``RuntimeState.daily_pnl`` <= ``threshold`` (signed P&L).
        Reads the ``RuntimeState`` row matching ``rule.symbol``; a blank symbol
        with no blank row falls back to the latest row by id (legacy contract).
      - ``consecutive_losses``: account-wide-only; fires when the authoritative
        account ``RuntimeState.consecutive_losses`` >= ``threshold``. ``threshold``
        must be a positive integer-like value (>= 1) so a zero threshold can
        never fire on a fresh state row. ``symbol`` must be blank.
      - ``kill_switch_engaged``: account-wide-only, notification-only; fires when
        the authoritative account ``RuntimeState.kill_switch`` is true.
        ``threshold`` must be exactly ``1.0`` (the numeric storage contract is
        preserved, but a fixed threshold guarantees a false state can never
        trigger because of threshold 0). ``symbol`` must be blank.

    The authoritative account state is resolved from the latest
    ``StrategyConfig`` symbol, then that symbol's ``RuntimeState`` row; if no
    primary-symbol row exists, the legacy ``RuntimeState`` row with
    ``symbol == ""`` is used; if neither exists, no data is returned. This
    never mutates ``RuntimeState`` (unlike ``RuntimeStateService.get_primary_runtime_state``).

    ``threshold`` rejects NaN and infinity for all rule types.
    """

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    symbol: str = Field(default="", max_length=50)
    rule_type: Literal[
        "price_above",
        "price_below",
        "daily_loss",
        "consecutive_losses",
        "kill_switch_engaged",
    ]
    threshold: float = Field(allow_inf_nan=False)
    severity: Literal["INFO", "WARNING", "CRITICAL"] = "WARNING"
    enabled: bool = True
    cooldown_seconds: int = Field(default=300, ge=0, le=86400)

    @model_validator(mode="after")
    def _validate_threshold_for_rule_type(self) -> "AlertRuleCreate":
        if self.rule_type == "consecutive_losses":
            # Require a positive integer-like threshold. A loss count is a
            # non-negative integer; a threshold of 0 would fire on a fresh
            # (zero-loss) state row, so reject it. Fractional thresholds are
            # meaningless for a count and are also rejected.
            if self.threshold < 1 or int(self.threshold) != self.threshold:
                raise ValueError(
                    "consecutive_losses threshold must be a positive integer >= 1"
                )
        if self.rule_type == "kill_switch_engaged":
            # The numeric threshold storage contract is preserved, but this
            # boolean-state rule requires exactly 1.0 so a false (0) state can
            # never satisfy ``value >= threshold`` and accidentally fire.
            if self.threshold != 1.0:
                raise ValueError("kill_switch_engaged threshold must be exactly 1.0")
        return self

    @model_validator(mode="after")
    def _validate_symbol_for_account_rule_types(self) -> "AlertRuleCreate":
        # consecutive_losses and kill_switch_engaged are account-wide-only:
        # they read the authoritative account state, never a secondary symbol's
        # row. Reject any non-blank symbol (including whitespace-only) so a
        # persisted rule can never silently bind to an unrelated symbol's
        # RuntimeState. The raw value is checked (not the stripped value) so
        # whitespace-only symbols are also rejected.
        if self.rule_type in ("consecutive_losses", "kill_switch_engaged"):
            if self.symbol != "":
                raise ValueError(
                    f"{self.rule_type} is account-wide-only; symbol must be blank"
                )
        return self


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


class AlertRuleEffectiveness(BaseModel):
    """One rule's firing effectiveness summary (read-only aggregate)."""

    id: int
    name: str
    symbol: str
    rule_type: str
    threshold: float
    severity: str
    enabled: bool
    cooldown_seconds: int
    created_at: datetime
    firing_count: int
    last_fired_at: Optional[datetime] = None
    never_fired: bool


class AlertRuleEffectivenessPage(BaseModel):
    items: list[AlertRuleEffectiveness]
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
# Database storage health (read-only SQLite operational snapshot)
# ---------------------------------------------------------------------------


class DatabaseHealthSnapshot(BaseModel):
    """Safe operational snapshot of the repository's SQLite database.

    No filesystem/database paths, connection URLs, table contents or secrets
    are exposed. ``database_size_bytes`` and ``free_space_bytes`` are logical
    SQLite page metrics (``page_size_bytes * page_count`` and
    ``page_size_bytes * freelist_count``) for both in-memory and file-backed
    databases — they reflect SQLite's internal page accounting, not the
    on-disk file size. ``wal_size_bytes`` is ``None`` for in-memory SQLite
    (no WAL file is applicable, including ``:memory:`` and shared-memory URI
    variants), ``0`` for a file-backed DB whose ``-wal`` sidecar is absent,
    and ``None`` when the WAL file size cannot be determined (e.g. permission
    or I/O error) — never misreported as merely absent.
    """

    checked_at: datetime
    dialect: str
    journal_mode: Optional[str] = None
    page_size_bytes: Optional[int] = None
    page_count: Optional[int] = None
    freelist_count: Optional[int] = None
    used_page_count: Optional[int] = None
    database_size_bytes: Optional[int] = None
    free_space_bytes: Optional[int] = None
    wal_size_bytes: Optional[int] = None


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


class NotificationStatsBucket(BaseModel):
    """One aggregate bucket (per severity) in the delivery stats.

    Carries total/success/failed because severity is a stored column, so every
    row — successful or not — can be attributed.
    """

    key: str
    total: int
    success: int
    failed: int


class NotificationFailureCount(BaseModel):
    """Count of failed deliveries attributed to one channel.

    Failure-only: successful rows are excluded entirely because the
    ``NotificationLog`` table does not persist channel attribution for
    successful sends (``MultiChannelNotifier`` only records a channel in the
    ``error`` column when that channel raises). ``key`` is therefore a
    *failure attribution* — ``serverchan`` / ``webhook`` / ``telegram`` for
    failures whose ``error`` begins with the matching ``ClassName: `` prefix,
    or ``unknown`` for failed rows with no recognized prefix. The sum of these
    counts equals the response's ``failed`` total.
    """

    key: str
    count: int


class NotificationDailyPoint(BaseModel):
    """Per-day notification delivery counts (UTC calendar day)."""

    date: str
    total: int
    success: int
    failed: int


class NotificationStatsResponse(BaseModel):
    """Read-only notification delivery statistics.

    Aggregations only — never carries title/content/error payloads. ``success_rate``
    is a percentage in [0, 100].

    ``failures_by_channel`` is a *failure-only* attribution. The
    ``NotificationLog`` table does not persist which channel carried a
    successful send, so a per-channel total/success/failed bucket would be
    misleading (every successful row would have to be filed under
    ``unknown``). Instead this field counts only failed rows: known notifier
    class prefixes in ``error`` are attributed to ``serverchan`` / ``webhook``
    / ``telegram``; failed rows with no recognized prefix are ``unknown``.
    Successful rows are excluded entirely. The sum of all
    ``failures_by_channel`` counts equals the response's ``failed`` total.
    """

    from_date: Optional[str] = None
    to_date: Optional[str] = None
    total: int
    success: int
    failed: int
    success_rate: float
    by_severity: list[NotificationStatsBucket] = Field(default_factory=list)
    failures_by_channel: list[NotificationFailureCount] = Field(default_factory=list)
    daily: list[NotificationDailyPoint] = Field(default_factory=list)


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
        "fixed_fee", "slippage_pct", "stop_loss_pct", "trailing_stop_pct",
        "opening_warmup_minutes", "entry_crossing_required",
        "max_entries_per_symbol_per_day",
        "max_holding_minutes", "entry_cutoff_minutes_before_close",
        "flatten_minutes_before_close",
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
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
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
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    created_at: datetime


class LLMUsageDailySummary(BaseModel):
    date: str
    interactions: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class LLMUsageTypeSummary(BaseModel):
    interaction_type: str
    interactions: int
    total_tokens: int


class LLMUsageSummaryResponse(BaseModel):
    days: int
    total_interactions: int
    successful_interactions: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    by_day: list[LLMUsageDailySummary]
    by_type: list[LLMUsageTypeSummary]


class LLMUsageBySymbol(BaseModel):
    """One symbol's LLM usage aggregate (read-only, safe projection).

    Blank symbols are represented explicitly as ``UNSPECIFIED`` rather than
    silently dropped. No prompt, raw/parsed response, errors, order ids or
    context are exposed.

    ``success_rate`` is a fraction in ``[0, 1]`` — ``successful_interactions
    / interactions``, or ``0.0`` when there are no interactions.
    """

    symbol: str
    market: str
    interactions: int
    successful_interactions: int
    success_rate: float = Field(ge=0.0, le=1.0)
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latest_interaction_at: Optional[datetime] = None


class LLMUsageBySymbolResponse(BaseModel):
    """LLM usage aggregated by symbol (and market), bounded by ``limit``.

    ``total_groups`` is the count of distinct (symbol, market) groups before
    ``limit`` is applied, so callers can tell a truncated result from a
    complete one. Deterministic ordering: total tokens desc, interactions
    desc, then symbol/market asc.
    """

    days: int
    limit: int
    total_groups: int
    items: list[LLMUsageBySymbol]


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
    snapshots: list[dict[str, Any]]
    daily_pnl: float
    trade_count: int
    error_tags: list[str]
    included_in_statistics: bool = True
    statistics_quality: StatisticsQuality


class ReviewResponse(BaseModel):
    symbol: str
    from_date: str
    to_date: str
    days: list[ReviewDaySchema]
    total_pnl: float
    total_trades: int
    all_error_tags: list[str]
    statistics_quality: StatisticsQuality


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
    source: str = Field(default="manual", max_length=32)
    is_active: bool
    is_trading_target: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


class UniverseCatalogItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=50)
    market: Literal["US", "HK"] = "US"
    alias: str = Field(default="", max_length=100)
    sector: str = Field(default="", max_length=100)
    memberships: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @model_validator(mode="after")
    def validate_symbol_market(self) -> "UniverseCatalogItem":
        _validate_symbol_market_pair(self.symbol, self.market)
        return self


class UniverseRotationMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    algorithm_version: str = Field(min_length=1, max_length=100)
    lookback_bars: int = Field(ge=2)
    skip_bars: int = Field(ge=1)
    sma_bars: int = Field(ge=2)
    ranking_method: Literal[
        "raw_momentum",
        "return_to_variance",
    ] = "raw_momentum"
    momentum_pct: Optional[float] = Field(
        default=None,
        allow_inf_nan=False,
    )
    formation_realized_volatility: Optional[float] = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
    )
    ranking_metric: Optional[float] = Field(
        default=None,
        allow_inf_nan=False,
    )
    sma_price: Optional[float] = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
    )
    above_sma: Optional[bool] = None
    eligible: bool = False
    selected: bool = False
    rank: Optional[int] = Field(default=None, ge=1)
    score: float = Field(default=0.0, ge=0, le=100, allow_inf_nan=False)
    exclusion_reasons: list[str] = Field(default_factory=list, max_length=50)


class UniverseObservationHealthComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Literal[
        "UNIVERSE_SELECTION",
        "ROTATION_FORWARD_PRECOMMITMENT",
        "WATCHLIST_QUANT",
        "DIVERSIFIED_PRIORITY_OBSERVATION",
        "GROWTH_SATELLITE_OBSERVATION",
        "LIVE_INTERVAL_ALIGNMENT",
        "LIVE_EXIT_CHALLENGER",
        "STRATEGY_V2_EXIT_CHALLENGER",
        "STRATEGY_V2_FORWARD",
        "PORTFOLIO_ROUTING",
        "OPENING_MOMENTUM_SHADOW",
        "OPENING_MOMENTUM_EXECUTION",
    ]
    status: Literal["HEALTHY", "WARNING", "DEGRADED", "DISABLED"]
    latest_at: Optional[datetime] = None
    age_seconds: Optional[float] = Field(default=None, ge=0)
    latest_session_date: Optional[date] = None
    expected_session_date: Optional[date] = None
    observed_count: int = Field(default=0, ge=0)
    expected_count: int = Field(default=0, ge=0)
    coverage_ratio: Optional[float] = Field(default=None, ge=0, le=1)
    blockers: list[str] = Field(default_factory=list)


class UniverseObservationHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    status: Literal["HEALTHY", "WARNING", "DEGRADED"]
    order_submission_allowed: Literal[False] = False
    automatic_promotion_allowed: Literal[False] = False
    components: list[UniverseObservationHealthComponent]
    blockers: list[str] = Field(default_factory=list)


class UniverseSelectionMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: Optional[float] = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
    )
    avg_dollar_volume: Optional[float] = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    relative_spread_bps: Optional[float] = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    realized_vol_20d: Optional[float] = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    atr_pct_14d: Optional[float] = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    momentum_5d_pct: Optional[float] = Field(
        default=None,
        allow_inf_nan=False,
    )
    trend_efficiency_10d: Optional[float] = Field(
        default=None,
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    opportunity_to_cost_ratio: Optional[float] = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    rotation: Optional[UniverseRotationMetrics] = None


class UniverseSelectionCandidateResponse(BaseModel):
    id: int = Field(ge=1)
    run_id: int = Field(ge=1)
    symbol: str = Field(min_length=1, max_length=50)
    market: Literal["US", "HK"]
    alias: str = Field(default="", max_length=100)
    sector: str = Field(default="", max_length=100)
    memberships: list[str] = Field(default_factory=list, max_length=20)
    selected: bool
    exploration_selected: bool = False
    shadow_enabled: bool = False
    is_trading_target: bool = False
    rank: Optional[int] = Field(default=None, ge=1)
    score: float = Field(default=0.0, ge=0, le=100, allow_inf_nan=False)
    metrics: UniverseSelectionMetrics = Field(
        default_factory=UniverseSelectionMetrics,
    )
    exclusion_reasons: list[str] = Field(default_factory=list, max_length=50)
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="before")
    @classmethod
    def decode_json_fields(cls, data: Any) -> Any:
        import json as _json

        if hasattr(data, "__table__"):
            data = {
                column.name: getattr(data, column.name)
                for column in data.__table__.columns
            }
        if not isinstance(data, dict):
            return data
        decoded = dict(data)
        for source, target, default in (
            ("memberships_json", "memberships", "[]"),
            ("metrics_json", "metrics", "{}"),
            ("exclusion_reasons_json", "exclusion_reasons", "[]"),
        ):
            raw = decoded.pop(source, default)
            if target not in decoded:
                decoded[target] = _json.loads(raw) if isinstance(raw, str) else raw
        return decoded


class UniverseSelectionRunResponse(BaseModel):
    id: int = Field(ge=1)
    as_of_date: date
    algorithm_version: str = Field(min_length=1, max_length=100)
    source_version: str = Field(min_length=1, max_length=100)
    status: str = Field(min_length=1, max_length=20)
    candidate_count: int = Field(ge=0)
    evaluable_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    coverage_ratio: float = Field(ge=0, le=1, allow_inf_nan=False)
    parameters: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    started_at: datetime
    completed_at: Optional[datetime] = None
    created_at: datetime
    items: list[UniverseSelectionCandidateResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="before")
    @classmethod
    def decode_parameters(cls, data: Any) -> Any:
        import json as _json

        if hasattr(data, "__table__"):
            data = {
                column.name: getattr(data, column.name)
                for column in data.__table__.columns
            }
        if not isinstance(data, dict):
            return data
        decoded = dict(data)
        raw = decoded.pop("parameters_json", "{}")
        if "parameters" not in decoded:
            decoded["parameters"] = (
                _json.loads(raw)
                if isinstance(raw, str)
                else raw
            )
        return decoded


class UniversePromotionReadinessItem(BaseModel):
    symbol: str = Field(min_length=1, max_length=50)
    memberships: list[Literal["NASDAQ_100", "DJIA"]] = Field(
        default_factory=list,
        max_length=2,
    )
    sector: str = Field(default="", max_length=100)
    risk_group: str = Field(default="", max_length=100)
    universe_role: Literal[
        "SELECTED",
        "EXPLORATION",
        "TRADING_TARGET",
    ]
    rank: Optional[int] = Field(default=None, ge=1)
    selection_score: float = Field(
        ge=0,
        le=100,
        allow_inf_nan=False,
    )
    priority_rank: int = Field(ge=1)
    priority_score: float = Field(
        ge=0,
        le=100,
        allow_inf_nan=False,
    )
    diversified_observation_selected: bool = False
    diversified_observation_rank: Optional[int] = Field(
        default=None,
        ge=1,
        le=8,
    )
    growth_satellite_selected: bool = False
    growth_satellite_rank: Optional[int] = Field(
        default=None,
        ge=1,
        le=4,
    )
    quant_weight: float = Field(
        ge=0,
        le=0.35,
        allow_inf_nan=False,
    )
    quant_adjustment: float = Field(
        ge=-25,
        le=17.5,
        allow_inf_nan=False,
    )
    is_trading_target: bool
    shadow_enabled: bool
    quant_score: Optional[float] = Field(
        default=None,
        ge=0,
        le=100,
        allow_inf_nan=False,
    )
    quant_confidence: Optional[float] = Field(
        default=None,
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    quant_recommended_action: str = ""
    quant_source: str = ""
    quant_fresh: bool = False
    quant_expires_at: Optional[datetime] = None
    estimated_round_trip_cost_bps: Optional[float] = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    forward_status: Literal[
        "NOT_REGISTERED",
        "FROZEN",
        "COLLECTING",
        "READY_FOR_REVIEW",
        "MATURE_EVIDENCE",
        "BLOCKED",
    ]
    included_pairs: int = Field(ge=0)
    minimum_ready_pairs: int = Field(ge=1)
    minimum_mature_pairs: int = Field(ge=1)
    remaining_ready_pairs: int = Field(ge=0)
    remaining_mature_pairs: int = Field(ge=0)
    blockers: list[str] = Field(default_factory=list)
    baseline_metrics: StrategyV2ShadowMetrics = Field(
        default_factory=StrategyV2ShadowMetrics,
    )
    candidate_metrics: StrategyV2ShadowMetrics = Field(
        default_factory=StrategyV2ShadowMetrics,
    )
    review_ready: bool
    mature_evidence: bool
    automatic_promotion_allowed: Literal[False] = False

    model_config = ConfigDict(extra="forbid")


class UniversePromotionReadinessResponse(BaseModel):
    universe_run_id: int = Field(ge=1)
    as_of_date: date
    generated_at: datetime
    priority_algorithm_version: str = Field(min_length=1, max_length=100)
    diversified_observation_limit: Literal[8] = 8
    growth_satellite_limit: Literal[4] = 4
    items: list[UniversePromotionReadinessItem] = Field(
        default_factory=list,
    )

    model_config = ConfigDict(extra="forbid")


class UniverseRotationForwardCohortResponse(BaseModel):
    source_run_id: int = Field(ge=1)
    source_as_of_date: date
    cohort_month: date
    status: str = Field(min_length=1, max_length=50)
    evidence_mode: Literal[
        "FORWARD_PRECOMMITTED",
        "BACKFILLED_AFTER_ENTRY",
    ]
    signal_date: date
    entry_date: date
    mark_date: date
    registered_as_of_date: date
    forward_eligible: bool
    target_symbols: list[str] = Field(default_factory=list, max_length=20)
    forward_observation_sessions: int = Field(ge=0)
    net_return_pct: Optional[float] = Field(
        default=None,
        allow_inf_nan=False,
    )
    qqq_return_pct: Optional[float] = Field(
        default=None,
        allow_inf_nan=False,
    )
    dia_return_pct: Optional[float] = Field(
        default=None,
        allow_inf_nan=False,
    )
    excess_return_vs_qqq_pct: Optional[float] = Field(
        default=None,
        allow_inf_nan=False,
    )
    excess_return_vs_dia_pct: Optional[float] = Field(
        default=None,
        allow_inf_nan=False,
    )
    selection_drift_detected: bool
    survivorship_bias: bool
    blockers: list[str] = Field(default_factory=list, max_length=100)

    model_config = ConfigDict(extra="forbid")


class UniverseRotationForwardTrackResponse(BaseModel):
    variant_name: str = Field(min_length=1, max_length=100)
    status: Literal[
        "NOT_REGISTERED",
        "AWAITING_PRECOMMITMENT",
        "COLLECTING",
        "DATA_BLOCKED",
        "PERFORMANCE_BLOCKED",
        "READY_FOR_MANUAL_REVIEW",
    ]
    observed_cohorts: int = Field(ge=0)
    forward_eligible_cohorts: int = Field(ge=0)
    completed_cohorts: int = Field(ge=0)
    minimum_completed_cohorts: int = Field(ge=1)
    remaining_completed_cohorts: int = Field(ge=0)
    backfilled_cohorts: int = Field(ge=0)
    incomplete_closed_cohorts: int = Field(ge=0)
    selection_drift_cohorts: int = Field(ge=0)
    invalid_evidence_records: int = Field(ge=0)
    first_completed_cohort_month: Optional[date] = None
    latest_completed_cohort_month: Optional[date] = None
    open_cohort: Optional[UniverseRotationForwardCohortResponse] = None
    diagnostic_cohort: Optional[
        UniverseRotationForwardCohortResponse
    ] = None
    compounded_return_pct: Optional[float] = Field(
        default=None,
        allow_inf_nan=False,
    )
    qqq_compounded_return_pct: Optional[float] = Field(
        default=None,
        allow_inf_nan=False,
    )
    dia_compounded_return_pct: Optional[float] = Field(
        default=None,
        allow_inf_nan=False,
    )
    compounded_excess_vs_qqq_pct: Optional[float] = Field(
        default=None,
        allow_inf_nan=False,
    )
    compounded_excess_vs_dia_pct: Optional[float] = Field(
        default=None,
        allow_inf_nan=False,
    )
    positive_cohort_rate_pct: Optional[float] = Field(
        default=None,
        ge=0,
        le=100,
        allow_inf_nan=False,
    )
    excess_win_rate_vs_qqq_pct: Optional[float] = Field(
        default=None,
        ge=0,
        le=100,
        allow_inf_nan=False,
    )
    excess_win_rate_vs_dia_pct: Optional[float] = Field(
        default=None,
        ge=0,
        le=100,
        allow_inf_nan=False,
    )
    average_cohort_return_pct: Optional[float] = Field(
        default=None,
        allow_inf_nan=False,
    )
    worst_cohort_return_pct: Optional[float] = Field(
        default=None,
        allow_inf_nan=False,
    )
    manual_review_ready: bool
    automatic_promotion_allowed: Literal[False] = False
    blockers: list[str] = Field(default_factory=list, max_length=100)
    warnings: list[str] = Field(default_factory=list, max_length=100)

    model_config = ConfigDict(extra="forbid")


class UniverseRotationForwardScorecardResponse(BaseModel):
    algorithm_version: str = Field(min_length=1, max_length=100)
    universe_run_id: int = Field(ge=1)
    as_of_date: date
    generated_at: datetime
    source_run_count: int = Field(ge=1)
    tracks: list[UniverseRotationForwardTrackResponse] = Field(
        default_factory=list,
        max_length=20,
    )
    automatic_promotion_allowed: Literal[False] = False

    model_config = ConfigDict(extra="forbid")


class UniverseSelectionRefreshResponse(BaseModel):
    run: UniverseSelectionRunResponse
    exploration_symbols: list[str] = Field(
        default_factory=list,
        max_length=100,
    )
    added_symbols: list[str] = Field(default_factory=list, max_length=100)
    removed_symbols: list[str] = Field(default_factory=list, max_length=100)
    retained_symbols: list[str] = Field(default_factory=list, max_length=100)
    shadow_enabled_symbols: list[str] = Field(
        default_factory=list,
        max_length=100,
    )
    shadow_disabled_symbols: list[str] = Field(
        default_factory=list,
        max_length=100,
    )
    shadow_failed_symbols: list[str] = Field(
        default_factory=list,
        max_length=100,
    )
    applied: bool
    reason: str = Field(default="", max_length=500)


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
    estimated_round_trip_cost_bps: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    created_at: datetime
    expires_at: datetime
    is_stale: bool = False

    model_config = {"from_attributes": True}


class WatchlistScoreListResponse(BaseModel):
    scores: list[WatchlistScoreResponse]
    reviews: list[WatchlistScoreResponse] = Field(default_factory=list)


class WatchlistQuantV6PolicyResponse(BaseModel):
    promotion_eligible: Literal[False] = False
    automatic_promotion_allowed: Literal[False] = False
    order_submission_allowed: Literal[False] = False
    short_entry_allowed: Literal[False] = False
    position_add_on_allowed: Literal[False] = False

    model_config = ConfigDict(extra="forbid")


class WatchlistQuantV6PublicationSummary(BaseModel):
    publication_id: int = Field(ge=1)
    registration_id: int = Field(ge=1)
    market: Literal["US", "HK"]
    status: Literal["PUBLISHED"] = "PUBLISHED"
    contract_version: str = Field(min_length=1, max_length=100)
    algorithm_version: str = Field(min_length=1, max_length=160)
    registration_identity_sha256: str = Field(min_length=64, max_length=64)
    identity_sha256: str = Field(min_length=64, max_length=64)
    manifest_sha256: str = Field(min_length=64, max_length=64)
    registered_member_count: int = Field(ge=1)
    assessment_artifact_count: int = Field(ge=1)
    session_input_artifact_count: int = Field(ge=0)
    event_artifact_count: int = Field(ge=0)
    binding_count: int = Field(ge=1)
    first_training_session_date: date
    first_target_session_date: date
    last_target_session_date: date
    data_cutoff_at: datetime
    registered_at: datetime
    published_at: datetime
    policy: WatchlistQuantV6PolicyResponse

    model_config = ConfigDict(extra="forbid")


class WatchlistQuantV6PublicationPage(BaseModel):
    integrity_scope: Literal["PERSISTED_HEADERS"] = "PERSISTED_HEADERS"
    items: list[WatchlistQuantV6PublicationSummary]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=50)
    next_cursor: str | None = Field(default=None, max_length=512)

    model_config = ConfigDict(extra="forbid")


class WatchlistQuantV6ValidationResponse(BaseModel):
    integrity_scope: Literal["SELF_CONSISTENT"] = "SELF_CONSISTENT"
    registration_identity_verified: Literal[True] = True
    publication_identity_verified: Literal[True] = True
    binding_manifest_verified: Literal[True] = True
    artifact_payloads_verified: Literal[False] = False

    model_config = ConfigDict(extra="forbid")


class WatchlistQuantV6RegistrationResponse(BaseModel):
    registration_id: int = Field(ge=1)
    identity_sha256: str = Field(min_length=64, max_length=64)
    schema_version: Literal[1] = 1
    contract_version: str = Field(min_length=1, max_length=100)
    selection_rule_version: str = Field(min_length=1, max_length=160)
    algorithm_version: str = Field(min_length=1, max_length=160)
    semantic_digest_sha256: str = Field(min_length=64, max_length=64)
    evaluator_digest_sha256: str = Field(min_length=64, max_length=64)
    acquisition_spec_sha256: str = Field(min_length=64, max_length=64)
    cohort_source: Literal["ROTATION_RESEARCH_CATALOG_PIT"]
    market: Literal["US", "HK"]
    source_snapshot_sha256: str = Field(min_length=64, max_length=64)
    cohort_manifest_sha256: str = Field(min_length=64, max_length=64)
    cohort_member_count: int = Field(ge=1, le=1_000)
    schedule_sha256: str = Field(min_length=64, max_length=64)
    training_session_count: Literal[10] = 10
    target_session_count: Literal[30] = 30
    first_training_session_date: date
    first_target_session_date: date
    last_target_session_date: date
    data_cutoff_at: datetime
    bar_period: Literal["MIN_5"] = "MIN_5"
    adjustment_mode: Literal["NO_ADJUST"] = "NO_ADJUST"
    server_generated: Literal[True] = True
    short_entry_allowed: Literal[False] = False
    position_add_on_allowed: Literal[False] = False
    order_submission_allowed: Literal[False] = False
    automatic_promotion_allowed: Literal[False] = False
    cohort_observed_at: datetime
    registered_at: datetime

    model_config = ConfigDict(extra="forbid")


class WatchlistQuantV6PublicationDetail(BaseModel):
    publication: WatchlistQuantV6PublicationSummary
    registration: WatchlistQuantV6RegistrationResponse
    acquisition_request_start_at: datetime
    acquisition_request_end_at: datetime
    validation: WatchlistQuantV6ValidationResponse

    model_config = ConfigDict(extra="forbid")


class WatchlistQuantV6MemberAcquisitionResponse(BaseModel):
    pages: int = Field(ge=0)
    raw_rows: int = Field(ge=0)
    accepted_bars: int = Field(ge=0)
    rejected_rows: int = Field(ge=0)
    complete_session_count: int = Field(ge=0)
    off_grid_accepted_bars: int = Field(ge=0)
    scheduled_grid_present_bars: int = Field(ge=0)
    accepted_bar_starts_sha256: str = Field(min_length=64, max_length=64)
    scheduled_grid_present_starts_sha256: str = Field(
        min_length=64,
        max_length=64,
    )
    scheduled_grid_coverage_bitset_hex: str

    model_config = ConfigDict(extra="forbid")


class WatchlistQuantV6MemberSummary(BaseModel):
    member_ordinal: int = Field(ge=0)
    symbol: str = Field(min_length=4, max_length=50)
    market: Literal["US", "HK"]
    alias: str = Field(default="", max_length=160)
    sector: str = Field(default="", max_length=160)
    memberships: list[str] = Field(default_factory=list, max_length=20)
    assessment_artifact_sha256: str = Field(min_length=64, max_length=64)
    assessment_binding_sha256: str = Field(min_length=64, max_length=64)
    acquisition: WatchlistQuantV6MemberAcquisitionResponse

    model_config = ConfigDict(extra="forbid")


class WatchlistQuantV6MemberPage(BaseModel):
    integrity_scope: Literal["REQUESTED_PAGE"] = "REQUESTED_PAGE"
    publication_id: int = Field(ge=1)
    items: list[WatchlistQuantV6MemberSummary]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    next_cursor: str | None = Field(default=None, max_length=512)

    model_config = ConfigDict(extra="forbid")


class WatchlistQuantV6BindingResponse(BaseModel):
    publication_id: int = Field(ge=1)
    member_ordinal: int = Field(ge=0)
    symbol: str = Field(min_length=4, max_length=50)
    market: Literal["US", "HK"]
    role: Literal["ASSESSMENT", "SESSION_INPUT", "EVENT"]
    artifact_ordinal: int = Field(ge=0, le=49_999)
    session_date: date | None = None
    artifact_sha256: str = Field(min_length=64, max_length=64)
    artifact_kind: Literal[
        "WATCHLIST_QUANT_V6_ASSESSMENT",
        "WATCHLIST_QUANT_V6_SESSION_INPUT",
        "WATCHLIST_QUANT_V6_EVENT",
    ]
    binding_sha256: str = Field(min_length=64, max_length=64)
    artifact_schema_version: Literal[1] = 1
    artifact_codec: Literal["zlib"] = "zlib"
    artifact_compression_level: Literal[9] = 9
    artifact_raw_size: int = Field(ge=1, le=2_097_152)
    artifact_compressed_size: int = Field(ge=1, le=524_288)
    binding_created_at: datetime
    artifact_created_at: datetime
    binding_identity_verified: Literal[True] = True

    model_config = ConfigDict(extra="forbid")


class WatchlistQuantV6BindingPage(BaseModel):
    integrity_scope: Literal["REQUESTED_PAGE"] = "REQUESTED_PAGE"
    publication_id: int = Field(ge=1)
    items: list[WatchlistQuantV6BindingResponse]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=500)
    next_cursor: str | None = Field(default=None, max_length=512)

    model_config = ConfigDict(extra="forbid")


class WatchlistQuantV6ArtifactResponse(BaseModel):
    integrity_scope: Literal["REQUESTED_ARTIFACT"] = "REQUESTED_ARTIFACT"
    publication_id: int = Field(ge=1)
    digest_sha256: str = Field(min_length=64, max_length=64)
    schema_version: Literal[1] = 1
    kind: Literal[
        "WATCHLIST_QUANT_V6_ASSESSMENT",
        "WATCHLIST_QUANT_V6_SESSION_INPUT",
        "WATCHLIST_QUANT_V6_EVENT",
    ]
    codec: Literal["zlib"] = "zlib"
    compression_level: Literal[9] = 9
    raw_size: int = Field(ge=1, le=2_097_152)
    compressed_size: int = Field(ge=1, le=524_288)
    created_at: datetime
    binding_count: Literal[1] = 1
    payload: dict[str, Any]
    payload_identity_verified: Literal[True] = True
    bound_to_publication: Literal[True] = True

    model_config = ConfigDict(extra="forbid")


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
    strategy_source: str = "LEGACY_UNATTRIBUTED"
    strategy_config_version: str = ""
    opening_execution_id: Optional[int] = None
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
    fee_source: str = "ESTIMATED"
    actual_fees: Optional[float] = None
    slippage_amount: Optional[float] = None
    slippage_bps: Optional[float] = None
    ack_latency_ms: Optional[float] = None
    fill_latency_ms: Optional[float] = None
    exit_cause: str = ""
    exit_reason: str = ""
    mfe_amount: Optional[float] = None
    mae_amount: Optional[float] = None
    mfe_pct: Optional[float] = None
    mae_pct: Optional[float] = None


class ClosedTradePage(BaseModel):
    items: list[ClosedTrade]
    total: int
    statistics_quality: StatisticsQuality


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
    total_fees: float = 0.0
    actual_fee_coverage_pct: float = 0.0
    avg_slippage_bps: Optional[float] = None
    avg_ack_latency_ms: Optional[float] = None
    statistics_quality: StatisticsQuality


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
    statistics_quality: StatisticsQuality


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
    statistics_quality: StatisticsQuality


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
    statistics_quality: StatisticsQuality


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
    statistics_quality: StatisticsQuality


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
    statistics_quality: StatisticsQuality


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
    statistics_quality: StatisticsQuality


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
    statistics_quality: StatisticsQuality


# ---------------------------------------------------------------------------
# Scheduled report preview / status (read-only; no notifier/audit/throttle side effects)
# ---------------------------------------------------------------------------


class ReportSchedulePreviewResponse(BaseModel):
    """Preview of the scheduled daily report without dispatching it.

    ``symbol`` is the effective symbol actually used (override or configured
    fallback). ``target_date`` is the YYYY-MM-DD the report was built for.
    ``title``/``content`` are exactly what ``build_summary`` produced.
    """

    symbol: str
    target_date: str
    title: str
    content: str


class ReportScheduleStatusResponse(BaseModel):
    """Safe operational snapshot of the scheduled-report throttle.

    All fields are derived/sanitized; raw monotonic timestamps are never
    exposed. ``state_scope``/``resets_on_restart`` make the process-local,
    non-persistent nature of the send-history throttle explicit.
    """

    enabled: bool
    configured_symbol: str
    effective_symbol: str
    interval_hours: int
    has_process_send_history: bool
    last_sent_age_seconds: float | None = None
    next_eligible_in_seconds: float | None = None
    eligible_now: bool
    state_scope: str = "process"
    resets_on_restart: bool = True
