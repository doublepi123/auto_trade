from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import RuntimeState, StrategyConfig

STRATEGY_AUDIT_KEYS = (
    "symbol",
    "market",
    "buy_low",
    "sell_high",
    "short_selling",
    "min_profit_amount",
    "auto_resume_minutes",
    "max_daily_loss",
    "max_consecutive_losses",
    "llm_interval_minutes",
    "fee_rate_us",
    "fee_rate_hk",
    "min_repricing_pct",
    "llm_action_cooldown_seconds",
    "trading_session_mode",
    "margin_safety_factor",
)


class StrategyService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_config(self) -> StrategyConfig:
        config = self.db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
        if config is None:
            config = StrategyConfig()
            config.updated_at = datetime.now(timezone.utc)
            self.db.add(config)
            self.db.commit()
        return config

    def update_config(self, data: dict[str, Any]) -> tuple[StrategyConfig, dict[str, Any]]:
        config = self.db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
        if config is None:
            config = StrategyConfig()

        before = {k: getattr(config, k, None) for k in STRATEGY_AUDIT_KEYS}

        updatable_fields = [
            "symbol", "market", "buy_low", "sell_high",
            "short_selling", "min_profit_amount", "auto_resume_minutes",
            "max_daily_loss", "max_consecutive_losses",
            "llm_interval_minutes",
            "fee_rate_us", "fee_rate_hk", "min_repricing_pct", "llm_action_cooldown_seconds",
            "trading_session_mode",
            "margin_safety_factor",
        ]
        for field in updatable_fields:
            if field in data:
                setattr(config, field, data[field])

        config.updated_at = datetime.now(timezone.utc)
        self.db.add(config)
        self.db.commit()
        self.db.refresh(config)

        after = {k: getattr(config, k, None) for k in STRATEGY_AUDIT_KEYS}
        diff = {
            k: {"old": before[k], "new": after[k]}
            for k in STRATEGY_AUDIT_KEYS
            if before[k] != after[k]
        }
        return config, diff

    def resolve_primary_symbol(self) -> str:
        return (self.get_config().symbol or "").strip().upper()

    def get_runtime_state(self, symbol: str = "") -> RuntimeState:
        normalized = (symbol or "").strip().upper()
        state = self.db.query(RuntimeState).filter(RuntimeState.symbol == normalized).first()
        if state is None:
            state = RuntimeState(symbol=normalized)
            self.db.add(state)
            self.db.commit()
        return state

    def get_primary_runtime_state(self) -> RuntimeState:
        symbol = self.resolve_primary_symbol()
        if not symbol:
            return self.get_runtime_state(symbol="")

        named = self.db.query(RuntimeState).filter(RuntimeState.symbol == symbol).first()
        if named is not None:
            return named

        legacy = self.db.query(RuntimeState).filter(RuntimeState.symbol == "").first()
        if legacy is not None:
            legacy.symbol = symbol
            legacy.updated_at = datetime.now(timezone.utc)
            self.db.add(legacy)
            self.db.commit()
            self.db.refresh(legacy)
            return legacy

        return self.get_runtime_state(symbol=symbol)

    UPDATABLE_STATE_FIELDS = frozenset({
        "engine_state", "paused", "pause_reason", "paused_at",
        "pause_auto_resumable", "kill_switch", "daily_pnl",
        "daily_pnl_date", "consecutive_losses", "last_price",
        "last_trigger_price", "last_trigger_at",
    })

    def update_runtime_state(self, symbol: str = "", **kwargs: object) -> RuntimeState:
        normalized = (symbol or "").strip().upper()
        state = self.get_runtime_state(symbol=normalized)
        for key, value in kwargs.items():
            if key not in self.UPDATABLE_STATE_FIELDS:
                raise AttributeError(f"Cannot update runtime state field: {key}")
            setattr(state, key, value)
        state.updated_at = datetime.now(timezone.utc)
        self.db.add(state)
        self.db.commit()
        self.db.refresh(state)
        return state

    def update_primary_runtime_state(self, **kwargs: object) -> RuntimeState:
        state = self.get_primary_runtime_state()
        return self.update_runtime_state(symbol=state.symbol, **kwargs)
