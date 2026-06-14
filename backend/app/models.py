from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_TZDateTime = DateTime(timezone=True)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class StrategyConfig(Base):
    __tablename__ = "strategy_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(50), default="")
    market: Mapped[str] = mapped_column(String(10), default="US")
    buy_low: Mapped[float] = mapped_column(Float, default=0.0)
    sell_high: Mapped[float] = mapped_column(Float, default=0.0)
    short_selling: Mapped[bool] = mapped_column(Boolean, default=False)
    min_profit_amount: Mapped[float] = mapped_column(Float, default=0.0)
    auto_resume_minutes: Mapped[int] = mapped_column(Integer, default=3)
    max_daily_loss: Mapped[float] = mapped_column(Float, default=5000.0)
    max_consecutive_losses: Mapped[int] = mapped_column(Integer, default=3)
    sct_key: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, onupdate=_utcnow)

    fee_rate_us: Mapped[float] = mapped_column(Float, default=0.0005)
    fee_rate_hk: Mapped[float] = mapped_column(Float, default=0.003)
    min_repricing_pct: Mapped[float] = mapped_column(Float, default=0.003)
    llm_action_cooldown_seconds: Mapped[int] = mapped_column(Integer, default=60)

    auto_interval_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_interval_minutes: Mapped[int] = mapped_column(Integer, default=2)
    llm_suggested_buy_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    llm_suggested_sell_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    llm_confidence_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    llm_analysis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    llm_last_analysis_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    llm_next_analysis_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    llm_applied_buy_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    llm_applied_sell_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    llm_applied_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    llm_reject_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    trading_session_mode: Mapped[str] = mapped_column(String(16), default="ANY", nullable=False)
    margin_safety_factor: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.9)


class CredentialConfig(Base):
    __tablename__ = "credential_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    longbridge_app_key: Mapped[str] = mapped_column(Text, default="")
    longbridge_app_secret: Mapped[str] = mapped_column(Text, default="")
    longbridge_access_token: Mapped[str] = mapped_column(Text, default="")
    sct_key: Mapped[str] = mapped_column(Text, default="")
    notification_channels: Mapped[str] = mapped_column(
        Text,
        default='[{"type":"serverchan","severity_floor":"INFO"}]',
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, onupdate=_utcnow)


class OrderRecord(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_symbol_filled_at", "symbol", "filled_at"),
        Index("ix_orders_symbol_created_at", "symbol", "created_at"),
        Index("ix_orders_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broker_order_id: Mapped[str] = mapped_column(String(100), default="")
    symbol: Mapped[str] = mapped_column(String(50))
    side: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    executed_quantity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    executed_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="SUBMITTED")
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)
    filled_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    raw_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class TradeEvent(Base):
    __tablename__ = "trade_events"
    __table_args__ = (
        Index("ix_trade_events_symbol_created_at", "symbol", "created_at"),
        Index("ix_trade_events_event_type", "event_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(50))
    symbol: Mapped[str] = mapped_column(String(50), default="")
    broker_order_id: Mapped[str] = mapped_column(String(100), default="")
    side: Mapped[str] = mapped_column(String(20), default="")
    status: Mapped[str] = mapped_column(String(30), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(50))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)


class LLMInteraction(Base):
    __tablename__ = "llm_interactions"
    __table_args__ = (
        Index("ix_llm_interactions_symbol_created_at", "symbol", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    interaction_type: Mapped[str] = mapped_column(String(20), default="analyze")
    symbol: Mapped[str] = mapped_column(String(50), default="")
    market: Mapped[str] = mapped_column(String(10), default="US")
    prompt: Mapped[str] = mapped_column(Text, default="")
    raw_response: Mapped[str] = mapped_column(Text, default="")
    parsed_response: Mapped[str] = mapped_column(Text, default="")
    context_snapshot: Mapped[str] = mapped_column(Text, default="")
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(Text, default="")
    order_action: Mapped[str] = mapped_column(String(30), default="NONE")
    order_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    order_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    prompt_variant: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)


class LLMSymbolScheduleState(Base):
    __tablename__ = "llm_symbol_schedule_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(50), default="", unique=True, index=True)
    market: Mapped[str] = mapped_column(String(10), default="US")
    last_analysis_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    next_analysis_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(20), default="")
    last_skip_reason: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)


class RuntimeState(Base):
    __tablename__ = "runtime_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(50), default="", index=True)
    engine_state: Mapped[str] = mapped_column(String(20), default="flat")
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_reason: Mapped[str] = mapped_column(Text, default="")
    paused_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    pause_auto_resumable: Mapped[bool] = mapped_column(Boolean, default=False)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    daily_pnl_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    last_price: Mapped[float] = mapped_column(Float, default=0.0)
    last_trigger_price: Mapped[float] = mapped_column(Float, default=0.0)
    last_trigger_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, onupdate=_utcnow)


class TrackedEntry(Base):
    """Persisted weighted-average entry cost used to compute exit PnL.

    Survives process restarts so that exit accounting does not fall back to
    the broker's stale ``avg_price``.
    """

    __tablename__ = "tracked_entries"

    symbol: Mapped[str] = mapped_column(String(50), primary_key=True)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, onupdate=_utcnow)


class RuntimeStateSnapshot(Base):
    __tablename__ = "runtime_state_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(50), default="", index=True)
    engine_state: Mapped[str] = mapped_column(String(20), default="flat")
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    last_price: Mapped[float] = mapped_column(Float, default=0.0)
    last_trigger_price: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="INFO")
    actor_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="anonymous")
    source_ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    request_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    result: Mapped[str] = mapped_column(String(16), nullable=False, default="SUCCESS")
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, index=True)


class WatchlistItem(Base):
    """Symbols under observation; only the StrategyConfig.symbol is the active trading target."""

    __tablename__ = "watchlist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    market: Mapped[str] = mapped_column(String(10), default="US", nullable=False)
    alias: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)

    __table_args__ = (UniqueConstraint("symbol", name="uq_watchlist_symbol"),)


class WatchlistScore(Base):
    """Cached LLM scoring for watchlist items. The score is a 0..100 trade
    attractiveness rating produced by the LLM advisor when explicitly asked
    via POST /api/watchlist/score. Caching avoids re-prompting on every
    snapshot render."""

    __tablename__ = "watchlist_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    market: Mapped[str] = mapped_column(String(10), default="US", nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    recommended_action: Mapped[str] = mapped_column(String(16), default="HOLD", nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="llm", nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_watchlist_scores_symbol_created_at", "symbol", "created_at"),
    )


class PromptVersion(Base):
    """Versioned prompt templates for A/B testing."""

    __tablename__ = "prompt_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    template: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)


class ExperimentResult(Base):
    """Tracks LLM experiment outcomes for A/B test analysis."""

    __tablename__ = "experiment_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_name: Mapped[str] = mapped_column(String(100), nullable=False)
    variant_name: Mapped[str] = mapped_column(String(100), nullable=False)
    interaction_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    order_action: Mapped[str] = mapped_column(String(32), nullable=False, default="NONE")
    predicted_direction: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    actual_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    was_profitable: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)

class StrategyExperiment(Base):
    __tablename__ = "strategy_experiments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    base_params_json: Mapped[str] = mapped_column(Text, nullable=False)
    parameter_grid_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="PENDING")
    estimated_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, index=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)


class StrategyExperimentRun(Base):
    __tablename__ = "strategy_experiment_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    parameters_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="COMPLETED")
    total_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_return_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    closed_trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sharpe_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profit_factor: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profit_loss_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    result_summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, index=True)