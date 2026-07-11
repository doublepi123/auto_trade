from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Float, Index, Integer, String, Text, UniqueConstraint, text
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
    allow_position_addons: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_position_quantity: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    max_position_notional: Mapped[float] = mapped_column(Float, default=5000.0, nullable=False)
    max_risk_per_trade: Mapped[float] = mapped_column(Float, default=250.0, nullable=False)
    stop_loss_pct: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    max_holding_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    entry_cutoff_minutes_before_close: Mapped[int] = mapped_column(Integer, default=45, nullable=False)
    flatten_minutes_before_close: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    llm_order_execution_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    report_schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    report_schedule_interval_hours: Mapped[int] = mapped_column(Integer, default=24, nullable=False)
    report_schedule_symbol: Mapped[str] = mapped_column(String(50), default="", nullable=False)


class StrategyParamVersion(Base):
    """Immutable snapshot of the tunable strategy params at a point in time.

    Each successful ``PUT /api/strategy`` (and explicit rollback) records one
    row; the user can later list versions and roll back to a prior snapshot.
    """

    __tablename__ = "strategy_param_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    actor_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)


class PortfolioConfig(Base):
    __tablename__ = "portfolio_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    symbols_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    allocations_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    per_symbol_risk_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    rebalance_threshold_pct: Mapped[float] = mapped_column(Float, default=5.0)
    max_gross_exposure: Mapped[float] = mapped_column(Float, default=1.0)
    max_net_exposure: Mapped[float] = mapped_column(Float, default=1.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, onupdate=_utcnow)


class PaperOrder(Base):
    __tablename__ = "paper_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broker_order_id: Mapped[str] = mapped_column(String(50), index=True)
    symbol: Mapped[str] = mapped_column(String(50))
    side: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[int] = mapped_column(Integer)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    limit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="SUBMITTED")
    intent_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, onupdate=_utcnow)


class Transaction(Base):
    """Transaction ledger (one row per fill, pyfolio-style).

    Populated by ``TransactionService.record`` (called from the
    ``TransactionLogger`` bus subscriber on each FillEvent). Each row captures
    the broker order id, symbol, side, signed-quantity, price, commission,
    provenance (``source``), and the fill timestamp — the columns pyfolio's
    ``transactions`` expects for tearsheet analysis.
    """

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broker_order_id: Mapped[str] = mapped_column(String(50), index=True)
    symbol: Mapped[str] = mapped_column(String(50), index=True)
    side: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(20), default="paper")
    timestamp: Mapped[datetime] = mapped_column(_TZDateTime, index=True)


class PlatformBacktestRun(Base):
    """Saved platform backtest run (Lean-style persisted runs).

    One row per ``POST /api/platform/backtest/runs`` execution. The full
    ``PlatformBacktestService.run`` result (equity curve, fills, positions,
    stats, analytics) is JSON-serialized into ``result_json``; ``final_nav``
    and ``sharpe`` are denormalized for cheap list/compare queries.
    """

    __tablename__ = "platform_backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    strategy: Mapped[str] = mapped_column(String(50))
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    symbols_json: Mapped[str] = mapped_column(Text, default="[]")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    final_nav: Mapped[float] = mapped_column(Float, default=0.0)
    sharpe: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)


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
        Index(
            "ux_orders_broker_order_id_nonempty",
            "broker_order_id",
            unique=True,
            sqlite_where=text("broker_order_id <> ''"),
        ),
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
    symbol: Mapped[str] = mapped_column(String(50), default="", unique=True, index=True)
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
    execution_state: Mapped[str] = mapped_column(String(20), default="IDLE", nullable=False)
    reduction_action: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    reduction_cause: Mapped[str] = mapped_column(String(30), default="", nullable=False)
    reduction_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    reduction_started_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    reduction_trigger_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, onupdate=_utcnow)


class TrackedEntry(Base):
    """Persisted weighted-average entry cost used to compute exit PnL.

    Survives process restarts so that exit accounting does not fall back to
    the broker's stale ``avg_price``.
    """

    __tablename__ = "tracked_entries"

    symbol: Mapped[str] = mapped_column(String(50), primary_key=True)
    side: Mapped[str] = mapped_column(String(10), default="LONG", nullable=False)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[Optional[datetime]] = mapped_column(
        _TZDateTime,
        nullable=True,
        default=_utcnow,
    )
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
    execution_state: Mapped[str] = mapped_column(String(20), default="IDLE", nullable=False)
    reduction_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
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


class TradeNote(Base):
    """A user-authored journal entry attached to a filled/submitted order.

    Closes the post-trade review loop: freeform note + tags + a 1-5 rating,
    keyed one-per-order so the TradeHistory view can show "has note" indicators
    and open an editor. ``tags`` is stored as a JSON text column (the project
    stores all JSON-like data as Text rather than the SQLAlchemy JSON type).
    """

    __tablename__ = "trade_notes"
    __table_args__ = (Index("ix_trade_notes_symbol_updated", "symbol", "updated_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True, unique=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    rating: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, onupdate=_utcnow)


class BacktestRun(Base):
    """A saved backtest run for side-by-side comparison.

    Stores the params + metrics (JSON text) the user chose to keep; the full
    equity curve / trades are NOT persisted (re-run /run to see them) to keep
    rows small.
    """

    __tablename__ = "backtest_runs"
    __table_args__ = (Index("ix_backtest_runs_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)


class NotificationLog(Base):
    """A persisted record of a dispatched notification (any channel/source).

    Populated by an optional sink attached to MultiChannelNotifier.send, so
    every risk/alert/report notification is auditable after the fact.
    """

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)


class AlertRule(Base):
    """User-defined alert rule evaluated by a background cron.

    ``rule_type`` ∈ {price_above, price_below, daily_loss}. Price rules
    use live quotes; ``daily_loss`` fires when the active runtime_state's
    ``daily_pnl`` <= ``threshold`` (threshold is signed P&L, typically
    negative, e.g. -500 = "down 500"). A per-rule ``cooldown_seconds``
    (vs ``last_fired_at``) prevents spam.
    """

    __tablename__ = "alert_rules"
    __table_args__ = (Index("ix_alert_rules_enabled", "enabled"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    rule_type: Mapped[str] = mapped_column(String(24), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    severity: Mapped[str] = mapped_column(String(16), default="WARNING", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    last_fired_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)


class AlertFiring(Base):
    """Append-only record of an alert rule firing (one row per dispatched
    notification). Lets a trader answer 'how many times did this rule fire and
    when' — ``AlertRule.last_fired_at`` only keeps the latest and is overwritten
    on each fire. Has no FK so a deleted rule's history remains intact."""

    __tablename__ = "alert_firings"
    __table_args__ = (Index("ix_alert_firings_rule_fired_at", "rule_id", "fired_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    rule_type: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    threshold: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    trigger_value: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="WARNING", nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    fired_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, nullable=False)


class StrategyPreset(Base):
    """A named snapshot of strategy params for one-click re-application.

    Stores the updatable strategy fields as a JSON text column; ``apply`` feeds
    them straight into ``StrategyService.update_config``.
    """

    __tablename__ = "strategy_presets"
    __table_args__ = (Index("ix_strategy_presets_name", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)


class EventLog(Base):
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    timestamp: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)


class FactorSnapshot(Base):
    """Factor research warehouse row (P196).

    One row per (as_of date, symbol, factor name). Stores the computed factor
    value, the forward return observed over the holding horizon, and a
    JSON-encoded snapshot of cross-sectional context (decile rank, etc.). This
    is the alphalens/Qlib-style factor panel: query by factor + date range to
    build an IC time series or a factor-decile backtest.
    """

    __tablename__ = "factor_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    factor_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    as_of: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False, index=True)
    factor_value: Mapped[float] = mapped_column(Float, nullable=False)
    forward_return: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    horizon_bars: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    context_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)


class FactorICSeries(Base):
    """Aggregated IC data point per (factor_name, as_of) for an IC time series."""

    __tablename__ = "factor_ic_series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    factor_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    as_of: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False, index=True)
    mean_ic: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    std_ic: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ic_ir: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    num_symbols: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)
