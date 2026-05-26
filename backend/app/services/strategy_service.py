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

    def update_config(self, data: dict) -> tuple[StrategyConfig, dict[str, Any]]:
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
        if "sct_key" in diff:
            diff["sct_key"] = {"changed": True}
        return config, diff

    def get_runtime_state(self) -> RuntimeState:
        state = self.db.query(RuntimeState).order_by(RuntimeState.id.desc()).first()
        if state is None:
            state = RuntimeState()
            self.db.add(state)
            self.db.commit()
        return state

    def update_runtime_state(self, **kwargs: object) -> RuntimeState:
        state = self.get_runtime_state()
        for key, value in kwargs.items():
            if hasattr(state, key):
                setattr(state, key, value)
        state.updated_at = datetime.now(timezone.utc)
        self.db.add(state)
        self.db.commit()
        self.db.refresh(state)
        return state
