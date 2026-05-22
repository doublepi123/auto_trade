from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_TZDateTime = lambda: DateTime(timezone=True)


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
    updated_at: Mapped[datetime] = mapped_column(_TZDateTime(), default=_utcnow)

    auto_interval_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_interval_minutes: Mapped[int] = mapped_column(Integer, default=2)
    llm_suggested_buy_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    llm_suggested_sell_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    llm_confidence_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    llm_analysis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    llm_last_analysis_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime(), nullable=True)
    llm_next_analysis_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime(), nullable=True)
    llm_applied_buy_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    llm_applied_sell_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    llm_applied_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime(), nullable=True)
    llm_reject_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class CredentialConfig(Base):
    __tablename__ = "credential_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    longbridge_app_key: Mapped[str] = mapped_column(Text, default="")
    longbridge_app_secret: Mapped[str] = mapped_column(Text, default="")
    longbridge_access_token: Mapped[str] = mapped_column(Text, default="")
    sct_key: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(_TZDateTime(), default=_utcnow)


class OrderRecord(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broker_order_id: Mapped[str] = mapped_column(String(100), default="")
    symbol: Mapped[str] = mapped_column(String(50))
    side: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    executed_quantity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    executed_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="SUBMITTED")
    created_at: Mapped[datetime] = mapped_column(_TZDateTime(), default=_utcnow)
    filled_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime(), nullable=True)
    raw_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(50))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime(), default=_utcnow)


class LLMInteraction(Base):
    __tablename__ = "llm_interactions"

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
    created_at: Mapped[datetime] = mapped_column(_TZDateTime(), default=_utcnow)


class RuntimeState(Base):
    __tablename__ = "runtime_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    engine_state: Mapped[str] = mapped_column(String(20), default="flat")
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_reason: Mapped[str] = mapped_column(Text, default="")
    paused_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime(), nullable=True)
    pause_auto_resumable: Mapped[bool] = mapped_column(Boolean, default=False)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    daily_pnl_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    last_price: Mapped[float] = mapped_column(Float, default=0.0)
    last_trigger_price: Mapped[float] = mapped_column(Float, default=0.0)
    last_trigger_at: Mapped[Optional[datetime]] = mapped_column(_TZDateTime(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(_TZDateTime(), default=_utcnow)
