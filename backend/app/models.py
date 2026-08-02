from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DDL,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_TZDateTime = DateTime(timezone=True)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class DurableJobLease(Base):
    """Durable cross-process ownership for one background job.

    Epoch values are written from SQLite's clock by the lease service.  The
    row is retained across releases so ``fencing_token`` can never suffer an
    ABA reset through an ordinary delete/reinsert cycle.
    """

    __tablename__ = "durable_job_leases"
    __table_args__ = (
        CheckConstraint(
            "length(lease_key) > 0 AND length(lease_key) <= 128 "
            "AND lease_key = trim(lease_key)",
            name="ck_durable_job_lease_key",
        ),
        CheckConstraint(
            "length(holder_id) > 0 AND length(holder_id) <= 128 "
            "AND holder_id = trim(holder_id)",
            name="ck_durable_job_lease_holder",
        ),
        CheckConstraint(
            "fencing_token >= 1",
            name="ck_durable_job_lease_fencing_token",
        ),
        CheckConstraint(
            "acquired_at_epoch_ms >= 0 "
            "AND renewed_at_epoch_ms >= 0 "
            "AND expires_at_epoch_ms >= 0",
            name="ck_durable_job_lease_epoch_ms",
        ),
    )

    lease_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    holder_id: Mapped[str] = mapped_column(String(128), nullable=False)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    acquired_at_epoch_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    renewed_at_epoch_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    expires_at_epoch_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )


event.listen(
    DurableJobLease.__table__,
    "after_create",
    DDL(
        "CREATE TRIGGER trg_durable_job_leases_no_delete "
        "BEFORE DELETE ON durable_job_leases "
        "BEGIN "
        "SELECT RAISE(ABORT, 'durable_job_leases rows cannot be deleted'); "
        "END"
    ).execute_if(dialect="sqlite"),
)


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
    max_drawdown_amount: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        default=None,
    )
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


class StrategyV2ShadowConfig(Base):
    """Forward-test configuration for the P2 strategy.

    This configuration deliberately has no live-execution mode.  The shadow
    service owns only virtual state and never writes to the real order ledger.
    """

    __tablename__ = "strategy_v2_shadow_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    universe_managed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    opening_momentum_execution_eligible: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    zscore_window_1m_bars: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    zscore_window_5m_bars: Mapped[int] = mapped_column(Integer, default=12, nullable=False)
    breach_zscore: Mapped[float] = mapped_column(Float, default=-2.0, nullable=False)
    reclaim_zscore: Mapped[float] = mapped_column(Float, default=-1.0, nullable=False)
    five_minute_zscore_max: Mapped[float] = mapped_column(Float, default=-0.5, nullable=False)
    adx_period: Mapped[int] = mapped_column(Integer, default=14, nullable=False)
    max_adx: Mapped[float] = mapped_column(Float, default=20.0, nullable=False)
    realized_vol_window_bars: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    min_realized_vol: Mapped[float] = mapped_column(Float, default=0.10, nullable=False)
    max_realized_vol: Mapped[float] = mapped_column(Float, default=0.80, nullable=False)
    stop_loss_pct: Mapped[float] = mapped_column(Float, default=0.75, nullable=False)
    profit_target_pct: Mapped[float] = mapped_column(Float, default=0.50, nullable=False)
    max_holding_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    entry_cutoff_minutes_before_close: Mapped[int] = mapped_column(Integer, default=45, nullable=False)
    flatten_minutes_before_close: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    arm_ttl_bars: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    max_entries_per_day: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    entry_cooldown_minutes: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    slippage_bps: Mapped[float] = mapped_column(Float, default=2.0, nullable=False)
    estimated_fee_rate_us: Mapped[float] = mapped_column(Float, default=0.0005, nullable=False)
    estimated_fee_rate_hk: Mapped[float] = mapped_column(Float, default=0.003, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, onupdate=_utcnow)


class StrategyV2ShadowVersion(Base):
    """Immutable parameter snapshot for one P2 shadow config hash."""

    __tablename__ = "strategy_v2_shadow_versions"
    __table_args__ = (
        UniqueConstraint("symbol", "config_version", name="uq_strategy_v2_shadow_version"),
        Index("ix_strategy_v2_shadow_versions_symbol_activated", "symbol", "activated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    config_version: Mapped[str] = mapped_column(String(64), nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, nullable=False)


class StrategyV2ForwardRegistration(Base):
    """Immutable registration for one prospective Strategy v2 challenger."""

    __tablename__ = "strategy_v2_forward_registrations"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "source_config_version",
            "candidate_algorithm_version",
            "evaluator_digest",
            name="uq_strategy_v2_forward_registration_candidate",
        ),
        Index(
            "ix_strategy_v2_forward_registration_symbol_eligible",
            "symbol",
            "eligible_after",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    candidate_algorithm_version: Mapped[str] = mapped_column(String(100), nullable=False)
    source_config_version: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluator_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_spec_json: Mapped[str] = mapped_column(Text, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    eligible_after: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)


class StrategyV2ForwardEvidence(Base):
    """Append-only, per-target prospective evidence produced by the cron."""

    __tablename__ = "strategy_v2_forward_evidence"
    __table_args__ = (
        UniqueConstraint(
            "registration_id",
            "target_session_date",
            name="uq_strategy_v2_forward_evidence_target",
        ),
        Index(
            "ix_strategy_v2_forward_evidence_registration_target",
            "registration_id",
            "target_session_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    registration_id: Mapped[int] = mapped_column(Integer, nullable=False)
    target_session_date: Mapped[date] = mapped_column(Date, nullable=False)
    seed_session_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    target_open_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    disposition: Mapped[str] = mapped_column(String(16), nullable=False)
    exclusion_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    structural_failure: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    target_bars: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    target_bars_sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    seed_bars_sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    baseline_input_sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    candidate_input_sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    same_target_bars: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    baseline_replay_match: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    session_local_invariant: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    baseline_result_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    candidate_result_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    baseline_result_sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    candidate_result_sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    evidence_digest_sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, nullable=False)


class StrategyV2ForwardReplayArtifact(Base):
    """Immutable, content-addressed replay bytes for forward evidence."""

    __tablename__ = "strategy_v2_forward_replay_artifacts"
    __table_args__ = (
        CheckConstraint(
            "length(digest_sha256) = 64 "
            "AND digest_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_strategy_v2_forward_replay_artifact_digest",
        ),
        CheckConstraint(
            "schema_version = 1",
            name="ck_strategy_v2_forward_replay_artifact_schema",
        ),
        CheckConstraint(
            "kind = 'STRATEGY_V2_FORWARD_REPLAY'",
            name="ck_strategy_v2_forward_replay_artifact_kind",
        ),
        CheckConstraint(
            "codec = 'zlib'",
            name="ck_strategy_v2_forward_replay_artifact_codec",
        ),
        CheckConstraint(
            "raw_size > 0 AND raw_size <= 8388608",
            name="ck_strategy_v2_forward_replay_artifact_raw_size",
        ),
        CheckConstraint(
            "compressed_size > 0 AND compressed_size <= 2097152",
            name="ck_strategy_v2_forward_replay_artifact_compressed_size",
        ),
        CheckConstraint(
            "length(payload) = compressed_size",
            name="ck_strategy_v2_forward_replay_artifact_payload_size",
        ),
    )

    digest_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    codec: Mapped[str] = mapped_column(String(16), nullable=False)
    raw_size: Mapped[int] = mapped_column(Integer, nullable=False)
    compressed_size: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        nullable=False,
    )


class StrategyV2ForwardEvidenceArtifact(Base):
    """Immutable role binding from one evidence row to replay bytes."""

    __tablename__ = "strategy_v2_forward_evidence_artifacts"
    __table_args__ = (
        CheckConstraint(
            "role = 'REPLAY_BUNDLE'",
            name="ck_strategy_v2_forward_evidence_artifact_role",
        ),
        CheckConstraint(
            "length(artifact_sha256) = 64 "
            "AND artifact_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_strategy_v2_forward_evidence_artifact_digest",
        ),
        CheckConstraint(
            "length(binding_sha256) = 64 "
            "AND binding_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_strategy_v2_forward_evidence_artifact_binding",
        ),
        Index(
            "ix_strategy_v2_forward_evidence_artifact_digest",
            "artifact_sha256",
        ),
    )

    evidence_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("strategy_v2_forward_evidence.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    artifact_sha256: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "strategy_v2_forward_replay_artifacts.digest_sha256",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    binding_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        nullable=False,
    )


class StrategyV2ExitChallengerRegistration(Base):
    """Immutable forward registration for one Strategy v2 exit policy."""

    __tablename__ = "strategy_v2_exit_challenger_registrations"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "source_config_version",
            "algorithm_version",
            name="uq_strategy_v2_exit_challenger_registration",
        ),
        Index(
            "ix_strategy_v2_exit_challenger_registration_symbol",
            "symbol",
            "registered_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    source_config_version: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(100), nullable=False)
    policy_type: Mapped[str] = mapped_column(
        String(24),
        default="PROFIT_LOCK",
        nullable=False,
    )
    activation_pct: Mapped[float] = mapped_column(Float, nullable=False)
    locked_profit_pct: Mapped[float] = mapped_column(Float, nullable=False)
    max_holding_minutes: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )
    slippage_bps: Mapped[float] = mapped_column(Float, nullable=False)
    evaluator_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    eligible_after: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        nullable=False,
    )


class StrategyV2ExitChallengerTrade(Base):
    """A forward-only exit-policy observation paired to one baseline trade."""

    __tablename__ = "strategy_v2_exit_challenger_trades"
    __table_args__ = (
        UniqueConstraint(
            "registration_id",
            "baseline_trade_id",
            name="uq_strategy_v2_exit_challenger_trade_pair",
        ),
        Index(
            "ix_strategy_v2_exit_challenger_trade_registration",
            "registration_id",
            "baseline_exit_at",
        ),
        Index(
            "ix_strategy_v2_exit_challenger_trade_open",
            "symbol",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    registration_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "strategy_v2_exit_challenger_registrations.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    baseline_trade_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("strategy_v2_shadow_trades.id", ondelete="CASCADE"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    source_config_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="OPEN", nullable=False)
    entry_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    estimated_fee_rate: Mapped[float] = mapped_column(Float, nullable=False)
    last_bar_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    activation_at: Mapped[Optional[datetime]] = mapped_column(
        _TZDateTime,
        nullable=True,
    )
    activation_effective_at: Mapped[Optional[datetime]] = mapped_column(
        _TZDateTime,
        nullable=True,
    )
    activation_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    floor_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    challenger_exit_at: Mapped[Optional[datetime]] = mapped_column(
        _TZDateTime,
        nullable=True,
    )
    challenger_exit_price: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    challenger_exit_reason: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False,
    )
    challenger_gross_pnl: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    challenger_estimated_fees: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    challenger_net_pnl: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    baseline_exit_at: Mapped[Optional[datetime]] = mapped_column(
        _TZDateTime,
        nullable=True,
    )
    baseline_exit_price: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    baseline_exit_reason: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False,
    )
    baseline_net_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_pnl_delta: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    paired_at: Mapped[Optional[datetime]] = mapped_column(
        _TZDateTime,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class StrategyV2BracketChallengerRegistration(Base):
    """Immutable forward registration for one full bracket alternative."""

    __tablename__ = "strategy_v2_bracket_challenger_registrations"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "source_config_version",
            "algorithm_version",
            name="uq_strategy_v2_bracket_challenger_registration",
        ),
        Index(
            "ix_strategy_v2_bracket_challenger_registration_symbol",
            "symbol",
            "registered_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    source_config_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    algorithm_version: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    stop_loss_pct: Mapped[float] = mapped_column(Float, nullable=False)
    profit_target_pct: Mapped[float] = mapped_column(Float, nullable=False)
    vwap_target_cap_bps: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    slippage_bps: Mapped[float] = mapped_column(Float, nullable=False)
    estimated_fee_rate: Mapped[float] = mapped_column(Float, nullable=False)
    max_holding_minutes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    flatten_minutes_before_close: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    estimated_round_trip_cost_pct: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )
    estimated_net_reward_risk_ratio: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )
    evaluator_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    registered_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        nullable=False,
    )
    eligible_after: Mapped[datetime] = mapped_column(
        _TZDateTime,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        nullable=False,
    )


class StrategyV2BracketChallengerTrade(Base):
    """Forward-only full-bracket outcome paired to one baseline trade."""

    __tablename__ = "strategy_v2_bracket_challenger_trades"
    __table_args__ = (
        UniqueConstraint(
            "registration_id",
            "baseline_trade_id",
            name="uq_strategy_v2_bracket_challenger_trade_pair",
        ),
        Index(
            "ix_strategy_v2_bracket_challenger_trade_registration",
            "registration_id",
            "baseline_exit_at",
        ),
        Index(
            "ix_strategy_v2_bracket_challenger_trade_open",
            "symbol",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    registration_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "strategy_v2_bracket_challenger_registrations.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    baseline_trade_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("strategy_v2_shadow_trades.id", ondelete="CASCADE"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    source_config_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default="OPEN",
        nullable=False,
    )
    entry_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        nullable=False,
    )
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    signal_vwap: Mapped[float] = mapped_column(Float, nullable=False)
    holding_deadline: Mapped[datetime] = mapped_column(
        _TZDateTime,
        nullable=False,
    )
    estimated_fee_rate: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )
    last_bar_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        nullable=False,
    )
    stop_price: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    target_price: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    challenger_exit_at: Mapped[Optional[datetime]] = mapped_column(
        _TZDateTime,
        nullable=True,
    )
    challenger_exit_price: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    challenger_exit_reason: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False,
    )
    challenger_gross_pnl: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    challenger_estimated_fees: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    challenger_net_pnl: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    baseline_exit_at: Mapped[Optional[datetime]] = mapped_column(
        _TZDateTime,
        nullable=True,
    )
    baseline_exit_price: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    baseline_exit_reason: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False,
    )
    baseline_net_pnl: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    net_pnl_delta: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    paired_at: Mapped[Optional[datetime]] = mapped_column(
        _TZDateTime,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class LiveExitChallengerRegistration(Base):
    """Immutable forward registration for one live-baseline exit observer."""

    __tablename__ = "live_exit_challenger_registrations"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "algorithm_version",
            name="uq_live_exit_challenger_registration",
        ),
        Index(
            "ix_live_exit_challenger_registration_symbol",
            "symbol",
            "registered_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(100), nullable=False)
    policy_type: Mapped[str] = mapped_column(
        String(24),
        default="PROFIT_LOCK",
        nullable=False,
    )
    activation_pct: Mapped[float] = mapped_column(Float, nullable=False)
    locked_profit_pct: Mapped[float] = mapped_column(Float, nullable=False)
    max_holding_minutes: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )
    slippage_bps: Mapped[float] = mapped_column(Float, nullable=False)
    evaluator_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    eligible_after: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        nullable=False,
    )


class LiveExitChallengerTrade(Base):
    """Forward-only alternative exit paired to a real broker round trip."""

    __tablename__ = "live_exit_challenger_trades"
    __table_args__ = (
        UniqueConstraint(
            "registration_id",
            "entry_order_id",
            name="uq_live_exit_challenger_entry",
        ),
        Index(
            "ix_live_exit_challenger_trade_registration",
            "registration_id",
            "baseline_exit_at",
        ),
        Index(
            "ix_live_exit_challenger_trade_open",
            "symbol",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    registration_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "live_exit_challenger_registrations.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    entry_order_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=False,
    )
    baseline_exit_order_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=True,
    )
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    entry_config_version: Mapped[str] = mapped_column(
        String(64),
        default="",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), default="OPEN", nullable=False)
    entry_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    estimated_fee_rate: Mapped[float] = mapped_column(Float, nullable=False)
    last_bar_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    activation_at: Mapped[Optional[datetime]] = mapped_column(
        _TZDateTime,
        nullable=True,
    )
    activation_effective_at: Mapped[Optional[datetime]] = mapped_column(
        _TZDateTime,
        nullable=True,
    )
    activation_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    floor_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    challenger_exit_at: Mapped[Optional[datetime]] = mapped_column(
        _TZDateTime,
        nullable=True,
    )
    challenger_exit_price: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    challenger_exit_reason: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False,
    )
    challenger_gross_pnl: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    challenger_estimated_fees: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    challenger_net_pnl: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    baseline_exit_at: Mapped[Optional[datetime]] = mapped_column(
        _TZDateTime,
        nullable=True,
    )
    baseline_exit_price: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    baseline_exit_reason: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False,
    )
    baseline_net_pnl: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    net_pnl_delta: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    paired_at: Mapped[Optional[datetime]] = mapped_column(
        _TZDateTime,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class StrategyV2PortfolioRegistration(Base):
    """Immutable registration for one causal, single-slot routing policy."""

    __tablename__ = "strategy_v2_portfolio_registrations"
    __table_args__ = (
        UniqueConstraint(
            "baseline_symbol",
            "algorithm_version",
            name="uq_strategy_v2_portfolio_registration",
        ),
        Index(
            "ix_strategy_v2_portfolio_registration_eligible",
            "eligible_after",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    baseline_symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    policy: Mapped[str] = mapped_column(String(32), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(100), nullable=False)
    evaluator_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    eligible_after: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        nullable=False,
    )


class StrategyV2PortfolioObservation(Base):
    """One forward-only signal-group result for a single capital slot."""

    __tablename__ = "strategy_v2_portfolio_observations"
    __table_args__ = (
        UniqueConstraint(
            "registration_id",
            "signal_at",
            name="uq_strategy_v2_portfolio_observation_signal",
        ),
        UniqueConstraint(
            "registration_id",
            "source_trade_id",
            name="uq_strategy_v2_portfolio_observation_trade",
        ),
        Index(
            "ix_strategy_v2_portfolio_observation_status",
            "registration_id",
            "status",
        ),
        Index(
            "ix_strategy_v2_portfolio_observation_symbol",
            "selected_symbol",
            "signal_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    registration_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "strategy_v2_portfolio_registrations.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    signal_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    candidates_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    selected_symbol: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    source_config_version: Mapped[str] = mapped_column(
        String(64),
        default="",
        nullable=False,
    )
    source_signal_decision_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("strategy_v2_shadow_decisions.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_trade_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("strategy_v2_shadow_trades.id", ondelete="SET NULL"),
        nullable=True,
    )
    selection_rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    selection_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    quant_source: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    quant_action: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    quant_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    quant_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    gross_return_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_return_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


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
    decision_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    decision_bid: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    decision_ask: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    decision_spread: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    decision_spread_bps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    quote_age_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    config_version: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    config_snapshot: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    submit_started_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    broker_submitted_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    broker_updated_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    submit_latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ack_latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fill_latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    estimated_fee: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    actual_fee: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fee_currency: Mapped[str] = mapped_column(String(10), default="", nullable=False)
    fee_source: Mapped[str] = mapped_column(String(20), default="UNKNOWN", nullable=False)
    slippage_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    slippage_bps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_cause: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    exit_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    gross_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_source: Mapped[str] = mapped_column(String(30), default="UNKNOWN", nullable=False)
    cost_basis_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cost_basis_quantity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cost_basis_opened_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    position_quantity_before: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_fee: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_fee_source: Mapped[str] = mapped_column(String(20), default="UNKNOWN", nullable=False)
    pnl_fee_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mfe_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mae_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mfe_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mae_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


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
        Index("ix_llm_interactions_created_at_id", "created_at", "id"),
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
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
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
    cumulative_realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    peak_realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    last_price: Mapped[float] = mapped_column(Float, default=0.0)
    last_trigger_price: Mapped[float] = mapped_column(Float, default=0.0)
    last_trigger_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    long_entry_rearm_required: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
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


class StrategyV2ShadowState(Base):
    """Crash-safe state for the forward-only P2 shadow state machine."""

    __tablename__ = "strategy_v2_shadow_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    config_version: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    session_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    phase: Mapped[str] = mapped_column(String(24), default="COLD", nullable=False)
    last_bar_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    last_polled_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    last_poll_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    armed_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    armed_zscore: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entries_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_entry_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    open_trade_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    state_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, onupdate=_utcnow)


class StrategyV2ShadowDecision(Base):
    """Append-only decision and feature snapshot for every completed 1m bar."""

    __tablename__ = "strategy_v2_shadow_decisions"
    __table_args__ = (
        Index("ix_strategy_v2_shadow_decisions_symbol_bar", "symbol", "bar_at"),
        Index("ix_strategy_v2_shadow_decisions_action", "action"),
        UniqueConstraint("idempotency_key", name="uq_strategy_v2_shadow_decision_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    market: Mapped[str] = mapped_column(String(10), default="US", nullable=False)
    config_version: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    bar_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    bar_at_5m: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, nullable=False)
    action: Mapped[str] = mapped_column(String(24), default="WAIT", nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    state_before: Mapped[str] = mapped_column(String(24), default="FLAT", nullable=False)
    state_after: Mapped[str] = mapped_column(String(24), default="FLAT", nullable=False)
    close_price: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    vwap_1m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    zscore_1m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vwap_5m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    zscore_5m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    adx_5m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realized_vol_1m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gate_passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    breach_armed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    virtual_position: Mapped[str] = mapped_column(String(16), default="FLAT", nullable=False)
    reference_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    quantity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    gross_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fee: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    holding_minutes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mae_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mfe_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gate_reasons_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    features_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)


class StrategyV2ShadowTrade(Base):
    """Virtual P2 round trip; fees are always estimates, never broker charges."""

    __tablename__ = "strategy_v2_shadow_trades"
    __table_args__ = (
        Index("ix_strategy_v2_shadow_trades_symbol_exit", "symbol", "exit_at"),
        Index(
            "ux_strategy_v2_shadow_trade_open_symbol",
            "symbol",
            unique=True,
            sqlite_where=text("status = 'OPEN'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    config_version: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    entry_decision_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    exit_decision_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="OPEN", nullable=False)
    entry_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    exit_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    quantity: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    stop_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    signal_vwap: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    holding_deadline: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    entry_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    exit_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    gross_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    estimated_fees: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    highest_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lowest_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mfe_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mae_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mfe_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mae_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    holding_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fee_source: Mapped[str] = mapped_column(String(20), default="ESTIMATED", nullable=False)
    estimated_fee_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, onupdate=_utcnow)


class OpeningMomentumShadowRun(Base):
    """One prospective cross-sectional opening-momentum observation."""

    __tablename__ = "opening_momentum_shadow_runs"
    __table_args__ = (
        UniqueConstraint(
            "session_date",
            "config_version",
            name="uq_opening_momentum_shadow_session_version",
        ),
        Index(
            "ix_opening_momentum_shadow_status_session",
            "status",
            "session_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(100), nullable=False)
    config_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    signal_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    selection_run_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    universe_source: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    universe_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    universe_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    excluded_symbols_json: Mapped[str] = mapped_column(
        Text,
        default="{}",
        nullable=False,
    )
    ranking_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    candidate_symbol: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    market_return_bps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    candidate_return_bps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    excess_return_bps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    candidate_first_five_return_bps: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    candidate_last_five_return_bps: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    candidate_path_efficiency: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    candidate_max_pullback_bps: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    candidate_opening_range_bps: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    candidate_signal_turnover: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    candidate_avg_dollar_volume: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    candidate_signal_turnover_ratio: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    candidate_opening_activity_ratio: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    candidate_overnight_gap_bps: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    candidate_prev_close_to_signal_bps: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    benchmark_qqq_return_bps: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    benchmark_dia_return_bps: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    entry_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_due_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    exit_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime, nullable=True)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gross_return_bps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    estimated_cost_bps: Mapped[float] = mapped_column(Float, nullable=False)
    net_return_bps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_loss_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    maximum_adverse_excursion_bps: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    maximum_favorable_excursion_bps: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        onupdate=_utcnow,
    )


class OpeningActivityObservation(Base):
    """Completed opening-window activity used by causal volume baselines."""

    __tablename__ = "opening_activity_observations"
    __table_args__ = (
        UniqueConstraint(
            "session_date",
            "symbol",
            "window_minutes",
            name="uq_opening_activity_session_symbol_window",
        ),
        Index(
            "ix_opening_activity_symbol_session",
            "symbol",
            "session_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    window_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    turnover: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(48), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        nullable=False,
    )


class OpeningMomentumExecution(Base):
    """Crash-safe single-slot execution of one causal opening signal."""

    __tablename__ = "opening_momentum_executions"
    __table_args__ = (
        UniqueConstraint(
            "session_date",
            name="uq_opening_momentum_execution_session",
        ),
        Index(
            "ix_opening_momentum_execution_status_session",
            "status",
            "session_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(160), nullable=False)
    config_version: Mapped[str] = mapped_column(String(64), nullable=False)
    universe_source: Mapped[str] = mapped_column(String(48), nullable=False)
    selection_run_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        default="ARMED",
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    symbol: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    signal_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    armed_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    entry_due_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    entry_deadline_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        nullable=False,
    )
    requested_at: Mapped[Optional[datetime]] = mapped_column(
        _TZDateTime,
        nullable=True,
    )
    universe_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    market_return_bps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    candidate_return_bps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    excess_return_bps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reference_entry_price: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    max_price_deviation_bps: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss_pct: Mapped[float] = mapped_column(Float, nullable=False)
    max_holding_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    signal_context_json: Mapped[str] = mapped_column(
        Text,
        default="{}",
        nullable=False,
    )
    submit_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    entry_order_id: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    exit_order_id: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    entry_filled_at: Mapped[Optional[datetime]] = mapped_column(
        _TZDateTime,
        nullable=True,
    )
    entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    quantity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_filled_at: Mapped[Optional[datetime]] = mapped_column(
        _TZDateTime,
        nullable=True,
    )
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        onupdate=_utcnow,
    )


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


class WatchlistQuantV6Registration(Base):
    """Immutable server-side registration for one historical quant-v6 cohort."""

    __tablename__ = "watchlist_quant_v6_registrations"
    __table_args__ = (
        UniqueConstraint(
            "identity_sha256",
            name="uq_watchlist_quant_v6_registration_identity",
        ),
        UniqueConstraint(
            "id",
            "identity_sha256",
            name="uq_watchlist_quant_v6_registration_id_identity",
        ),
        CheckConstraint(
            "length(identity_sha256) = 64 "
            "AND identity_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_registration_identity_sha",
        ),
        CheckConstraint(
            "schema_version = 1",
            name="ck_watchlist_quant_v6_registration_schema",
        ),
        CheckConstraint(
            "length(contract_version) > 0 "
            "AND contract_version = trim(contract_version) "
            "AND length(selection_rule_version) > 0 "
            "AND selection_rule_version = trim(selection_rule_version) "
            "AND length(algorithm_version) > 0 "
            "AND algorithm_version = trim(algorithm_version)",
            name="ck_watchlist_quant_v6_registration_versions",
        ),
        CheckConstraint(
            "length(semantic_digest_sha256) = 64 "
            "AND semantic_digest_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_registration_semantic_sha",
        ),
        CheckConstraint(
            "length(evaluator_digest_sha256) = 64 "
            "AND evaluator_digest_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_registration_evaluator_sha",
        ),
        CheckConstraint(
            "length(acquisition_spec_sha256) = 64 "
            "AND acquisition_spec_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_registration_acquisition_sha",
        ),
        CheckConstraint(
            "cohort_source = 'ROTATION_RESEARCH_CATALOG_PIT'",
            name="ck_watchlist_quant_v6_registration_cohort_source",
        ),
        CheckConstraint(
            "market IN ('US', 'HK')",
            name="ck_watchlist_quant_v6_registration_market",
        ),
        CheckConstraint(
            "length(source_snapshot_sha256) = 64 "
            "AND source_snapshot_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_registration_source_sha",
        ),
        CheckConstraint(
            "length(cohort_manifest_sha256) = 64 "
            "AND cohort_manifest_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_registration_cohort_sha",
        ),
        CheckConstraint(
            "cohort_member_count > 0",
            name="ck_watchlist_quant_v6_registration_member_count",
        ),
        CheckConstraint(
            "length(schedule_sha256) = 64 "
            "AND schedule_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_registration_schedule_sha",
        ),
        CheckConstraint(
            "training_session_count = 10 AND target_session_count = 30",
            name="ck_watchlist_quant_v6_registration_session_counts",
        ),
        CheckConstraint(
            "first_training_session_date < first_target_session_date "
            "AND first_target_session_date <= last_target_session_date",
            name="ck_watchlist_quant_v6_registration_session_dates",
        ),
        CheckConstraint(
            "bar_period = 'MIN_5' AND adjustment_mode = 'NO_ADJUST'",
            name="ck_watchlist_quant_v6_registration_bar_source",
        ),
        CheckConstraint(
            "json_valid(registration_json) = 1 "
            "AND json_type(registration_json) = 'object'",
            name="ck_watchlist_quant_v6_registration_json",
        ),
        CheckConstraint(
            "server_generated = 1",
            name="ck_watchlist_quant_v6_registration_server_generated",
        ),
        CheckConstraint(
            "short_entry_allowed = 0 "
            "AND position_add_on_allowed = 0 "
            "AND order_submission_allowed = 0 "
            "AND automatic_promotion_allowed = 0",
            name="ck_watchlist_quant_v6_registration_p0",
        ),
        CheckConstraint(
            "data_cutoff_at <= cohort_observed_at "
            "AND cohort_observed_at <= registered_at",
            name="ck_watchlist_quant_v6_registration_times",
        ),
        Index(
            "ix_watchlist_quant_v6_registration_market_target_registered",
            "market",
            "last_target_session_date",
            "registered_at",
        ),
        Index(
            "ix_watchlist_quant_v6_registration_registered_id",
            "registered_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    identity_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    selection_rule_version: Mapped[str] = mapped_column(String(160), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(160), nullable=False)
    semantic_digest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluator_digest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    acquisition_spec_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    cohort_source: Mapped[str] = mapped_column(
        String(48),
        default="ROTATION_RESEARCH_CATALOG_PIT",
        nullable=False,
    )
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    source_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    cohort_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    cohort_member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    schedule_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    training_session_count: Mapped[int] = mapped_column(
        Integer,
        default=10,
        nullable=False,
    )
    target_session_count: Mapped[int] = mapped_column(
        Integer,
        default=30,
        nullable=False,
    )
    first_training_session_date: Mapped[date] = mapped_column(Date, nullable=False)
    first_target_session_date: Mapped[date] = mapped_column(Date, nullable=False)
    last_target_session_date: Mapped[date] = mapped_column(Date, nullable=False)
    data_cutoff_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    bar_period: Mapped[str] = mapped_column(
        String(16),
        default="MIN_5",
        nullable=False,
    )
    adjustment_mode: Mapped[str] = mapped_column(
        String(16),
        default="NO_ADJUST",
        nullable=False,
    )
    registration_json: Mapped[str] = mapped_column(Text, nullable=False)
    server_generated: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    short_entry_allowed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    position_add_on_allowed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    order_submission_allowed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    automatic_promotion_allowed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    cohort_observed_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)


class WatchlistQuantV6Artifact(Base):
    """Immutable content-addressed quant-v6 evidence bytes."""

    __tablename__ = "watchlist_quant_v6_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "digest_sha256",
            "kind",
            name="uq_watchlist_quant_v6_artifact_digest_kind",
        ),
        CheckConstraint(
            "length(digest_sha256) = 64 "
            "AND digest_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_artifact_digest_sha",
        ),
        CheckConstraint(
            "schema_version = 1",
            name="ck_watchlist_quant_v6_artifact_schema",
        ),
        CheckConstraint(
            "kind IN ('WATCHLIST_QUANT_V6_ASSESSMENT', "
            "'WATCHLIST_QUANT_V6_SESSION_INPUT', 'WATCHLIST_QUANT_V6_EVENT')",
            name="ck_watchlist_quant_v6_artifact_kind",
        ),
        CheckConstraint(
            "codec = 'zlib' AND compression_level = 9",
            name="ck_watchlist_quant_v6_artifact_codec",
        ),
        CheckConstraint(
            "raw_size >= 1 AND raw_size <= 2097152",
            name="ck_watchlist_quant_v6_artifact_raw_size",
        ),
        CheckConstraint(
            "compressed_size >= 1 AND compressed_size <= 524288",
            name="ck_watchlist_quant_v6_artifact_compressed_size",
        ),
        CheckConstraint(
            "length(payload) = compressed_size",
            name="ck_watchlist_quant_v6_artifact_payload_size",
        ),
        Index(
            "ix_watchlist_quant_v6_artifact_kind_created",
            "kind",
            "created_at",
        ),
    )

    digest_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    codec: Mapped[str] = mapped_column(String(16), default="zlib", nullable=False)
    compression_level: Mapped[int] = mapped_column(Integer, default=9, nullable=False)
    raw_size: Mapped[int] = mapped_column(Integer, nullable=False)
    compressed_size: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)


class WatchlistQuantV6Publication(Base):
    """One atomic, immutable publication for a completed registered cohort."""

    __tablename__ = "watchlist_quant_v6_publications"
    __table_args__ = (
        UniqueConstraint(
            "registration_id",
            name="uq_watchlist_quant_v6_publication_registration",
        ),
        UniqueConstraint(
            "identity_sha256",
            name="uq_watchlist_quant_v6_publication_identity",
        ),
        ForeignKeyConstraint(
            ["registration_id", "registration_identity_sha256"],
            [
                "watchlist_quant_v6_registrations.id",
                "watchlist_quant_v6_registrations.identity_sha256",
            ],
            name="fk_watchlist_quant_v6_publication_registration_identity",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(registration_identity_sha256) = 64 "
            "AND registration_identity_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_publication_registration_sha",
        ),
        CheckConstraint(
            "length(identity_sha256) = 64 "
            "AND identity_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_publication_identity_sha",
        ),
        CheckConstraint(
            "schema_version = 1",
            name="ck_watchlist_quant_v6_publication_schema",
        ),
        CheckConstraint(
            "length(contract_version) > 0 "
            "AND contract_version = trim(contract_version)",
            name="ck_watchlist_quant_v6_publication_contract",
        ),
        CheckConstraint(
            "status = 'PUBLISHED'",
            name="ck_watchlist_quant_v6_publication_status",
        ),
        CheckConstraint(
            "length(manifest_sha256) = 64 "
            "AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_publication_manifest_sha",
        ),
        CheckConstraint(
            "json_valid(publication_json) = 1 "
            "AND json_type(publication_json) = 'object'",
            name="ck_watchlist_quant_v6_publication_json",
        ),
        CheckConstraint(
            "registered_member_count > 0 "
            "AND assessment_artifact_count = registered_member_count "
            "AND session_input_artifact_count >= 0 "
            "AND event_artifact_count >= 0 "
            "AND binding_count = assessment_artifact_count "
            "+ session_input_artifact_count + event_artifact_count",
            name="ck_watchlist_quant_v6_publication_counts",
        ),
        CheckConstraint(
            "promotion_eligible = 0 "
            "AND automatic_promotion_allowed = 0 "
            "AND order_submission_allowed = 0 "
            "AND short_entry_allowed = 0 "
            "AND position_add_on_allowed = 0",
            name="ck_watchlist_quant_v6_publication_p0",
        ),
        Index(
            "ix_watchlist_quant_v6_publication_published_id",
            "published_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    registration_id: Mapped[int] = mapped_column(Integer, nullable=False)
    registration_identity_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    identity_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        default="PUBLISHED",
        nullable=False,
    )
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    publication_json: Mapped[str] = mapped_column(Text, nullable=False)
    registered_member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    assessment_artifact_count: Mapped[int] = mapped_column(Integer, nullable=False)
    session_input_artifact_count: Mapped[int] = mapped_column(Integer, nullable=False)
    event_artifact_count: Mapped[int] = mapped_column(Integer, nullable=False)
    binding_count: Mapped[int] = mapped_column(Integer, nullable=False)
    promotion_eligible: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    automatic_promotion_allowed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    order_submission_allowed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    short_entry_allowed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    position_add_on_allowed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    published_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)


class WatchlistQuantV6PublicationArtifact(Base):
    """Typed immutable binding between a cohort member and evidence bytes."""

    __tablename__ = "watchlist_quant_v6_publication_artifacts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["artifact_sha256", "artifact_kind"],
            [
                "watchlist_quant_v6_artifacts.digest_sha256",
                "watchlist_quant_v6_artifacts.kind",
            ],
            name="fk_watchlist_quant_v6_binding_artifact_identity",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "binding_sha256",
            name="uq_watchlist_quant_v6_binding_sha",
        ),
        CheckConstraint(
            "member_ordinal >= 0 AND artifact_ordinal >= 0",
            name="ck_watchlist_quant_v6_binding_ordinals",
        ),
        CheckConstraint(
            "length(symbol) > 3 AND length(symbol) <= 50 "
            "AND symbol = trim(symbol) AND symbol = upper(symbol)",
            name="ck_watchlist_quant_v6_binding_symbol",
        ),
        CheckConstraint(
            "market IN ('US', 'HK') "
            "AND ((market = 'US' AND symbol LIKE '%.US') "
            "OR (market = 'HK' AND symbol LIKE '%.HK'))",
            name="ck_watchlist_quant_v6_binding_market",
        ),
        CheckConstraint(
            "role IN ('ASSESSMENT', 'SESSION_INPUT', 'EVENT')",
            name="ck_watchlist_quant_v6_binding_role",
        ),
        CheckConstraint(
            "artifact_kind IN ('WATCHLIST_QUANT_V6_ASSESSMENT', "
            "'WATCHLIST_QUANT_V6_SESSION_INPUT', 'WATCHLIST_QUANT_V6_EVENT')",
            name="ck_watchlist_quant_v6_binding_kind",
        ),
        CheckConstraint(
            "(role = 'ASSESSMENT' "
            "AND artifact_kind = 'WATCHLIST_QUANT_V6_ASSESSMENT' "
            "AND session_date IS NULL AND artifact_ordinal = 0) "
            "OR (role = 'SESSION_INPUT' "
            "AND artifact_kind = 'WATCHLIST_QUANT_V6_SESSION_INPUT' "
            "AND session_date IS NOT NULL AND artifact_ordinal < 30) "
            "OR (role = 'EVENT' "
            "AND artifact_kind = 'WATCHLIST_QUANT_V6_EVENT' "
            "AND session_date IS NOT NULL)",
            name="ck_watchlist_quant_v6_binding_role_kind_session",
        ),
        CheckConstraint(
            "length(artifact_sha256) = 64 "
            "AND artifact_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_binding_artifact_sha",
        ),
        CheckConstraint(
            "length(binding_sha256) = 64 "
            "AND binding_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_watchlist_quant_v6_binding_sha256",
        ),
        Index(
            "ix_watchlist_quant_v6_binding_member_session_role",
            "publication_id",
            "member_ordinal",
            "session_date",
            "role",
        ),
        Index(
            "ix_watchlist_quant_v6_binding_artifact_sha",
            "artifact_sha256",
        ),
    )

    publication_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "watchlist_quant_v6_publications.id",
            name="fk_watchlist_quant_v6_binding_publication",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    member_ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    role: Mapped[str] = mapped_column(String(20), primary_key=True)
    artifact_ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)


class WatchlistItem(Base):
    """Symbols under observation; only the StrategyConfig.symbol is the active trading target."""

    __tablename__ = "watchlist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    market: Mapped[str] = mapped_column(String(10), default="US", nullable=False)
    alias: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow)

    __table_args__ = (UniqueConstraint("symbol", name="uq_watchlist_symbol"),)


class UniverseSelectionRun(Base):
    """One reproducible daily universe-selection attempt."""

    __tablename__ = "universe_selection_runs"
    __table_args__ = (
        UniqueConstraint(
            "as_of_date",
            "algorithm_version",
            "source_version",
            name="uq_universe_selection_run_identity",
        ),
        Index("ix_universe_selection_runs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(100), nullable=False)
    source_version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        default="RUNNING",
        nullable=False,
    )
    candidate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    evaluable_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    selected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    coverage_ratio: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    parameters_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        nullable=False,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        _TZDateTime,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        nullable=False,
    )


class UniverseSelectionCandidate(Base):
    """Persisted evaluation evidence for one symbol in a selection run."""

    __tablename__ = "universe_selection_candidates"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "symbol",
            name="uq_universe_selection_candidate_symbol",
        ),
        Index("ix_universe_selection_candidates_run_id", "run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("universe_selection_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    market: Mapped[str] = mapped_column(String(10), default="US", nullable=False)
    alias: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    sector: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    memberships_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    exclusion_reasons_json: Mapped[str] = mapped_column(
        Text,
        default="[]",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        _TZDateTime,
        default=_utcnow,
        nullable=False,
    )


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
    estimated_round_trip_cost_bps: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(_TZDateTime, default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_watchlist_scores_symbol_created_at", "symbol", "created_at"),
        Index(
            "ix_watchlist_scores_symbol_source_created_at",
            "symbol",
            "source",
            "created_at",
        ),
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

    ``rule_type`` ∈ {price_above, price_below, daily_loss, consecutive_losses,
    kill_switch_engaged}. Price rules use live quotes; ``daily_loss`` fires
    when the active runtime_state's ``daily_pnl`` <= ``threshold`` (threshold
    is signed P&L, typically negative, e.g. -500 = "down 500");
    ``consecutive_losses`` and ``kill_switch_engaged`` are account-wide-only
    (``symbol`` must be blank) and read the authoritative account state — the
    latest StrategyConfig symbol's RuntimeState, falling back to the legacy
    ``symbol == ""`` row. ``consecutive_losses`` fires when the account
    runtime_state's ``consecutive_losses`` >= ``threshold`` (threshold is a
    positive integer); ``kill_switch_engaged`` is notification-only and fires
    when the account runtime_state's ``kill_switch`` is true (threshold fixed
    at 1.0). A per-rule ``cooldown_seconds`` (vs ``last_fired_at``) prevents
    spam.
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
