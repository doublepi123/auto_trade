from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import RuntimeState, StrategyConfig

logger = logging.getLogger("auto_trade.strategy_service")

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
        is_new = config is None
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
        if is_new:
            # When creating a new config, only report changes for fields
            # explicitly provided in the update data (avoids spurious diffs
            # from default values).
            diff = {
                k: {"old": before.get(k, None), "new": after[k]}
                for k in STRATEGY_AUDIT_KEYS
                if k in data and before.get(k) != after[k]
            }
        else:
            diff = {
                k: {"old": before[k], "new": after[k]}
                for k in STRATEGY_AUDIT_KEYS
                if before[k] != after[k]
            }
        return config, diff

    def resolve_primary_symbol(self) -> str:
        config = self.db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
        return (config.symbol or "").strip().upper() if config else ""

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


def validate_strategy_consistency(config: StrategyConfig) -> list[dict[str, str]]:
    """Cross-field validation: surface config combinations that will silently
    prevent profitable exits.

    Currently checks:
      * ``min_profit_amount`` vs the round-trip fee on a notional 1-share
        exit. If fees alone exceed the configured floor, every exit will
        be skipped with ``skip_category=FEE`` and the strategy cannot
        realise any profit.
      * ``max_daily_loss`` vs ``min_profit_amount`` — a daily-loss limit
        that is smaller than a single-round profit floor makes the
        strategy impossible to honour under any trade.

    The returned list contains one entry per detected issue, with a
    ``field`` (the offending config key), a ``level`` (``warning`` or
    ``error``), and a human-readable ``message``. Callers should log the
    warnings; errors block startup in stricter deployments.
    """
    issues: list[dict[str, str]] = []
    market = (config.market or "US").upper()
    fee_rate_field = "fee_rate_hk" if market == "HK" else "fee_rate_us"
    fee_rate = Decimal(str(getattr(config, fee_rate_field, 0) or 0))
    min_profit = Decimal(str(config.min_profit_amount or 0))
    if min_profit > 0 and fee_rate > 0:
        # Per-share round-trip fee (entry + exit) at the configured rate.
        per_share_fee = fee_rate * 2
        if per_share_fee > min_profit:
            issues.append(
                {
                    "field": "min_profit_amount",
                    "level": "warning",
                    "message": (
                        f"min_profit_amount={min_profit} is below the per-share "
                        f"round-trip fee ({per_share_fee}). Exits will be "
                        f"skipped with skip_category=FEE. Lower {fee_rate_field} "
                        f"or raise min_profit_amount."
                    ),
                }
            )
    max_daily_loss = Decimal(str(config.max_daily_loss or 0))
    if min_profit > 0 and 0 < max_daily_loss < min_profit:
        issues.append(
            {
                "field": "max_daily_loss",
                "level": "warning",
                "message": (
                    f"max_daily_loss={max_daily_loss} is smaller than "
                    f"min_profit_amount={min_profit}. A single profitable "
                    f"trade would trip the daily-loss limit."
                ),
            }
        )
    if config.buy_low > 0 and config.sell_high > 0 and config.sell_high <= config.buy_low:
        issues.append(
            {
                "field": "sell_high",
                "level": "error",
                "message": (
                    f"sell_high ({config.sell_high}) must be greater than "
                    f"buy_low ({config.buy_low}). The strategy cannot trigger."
                ),
            }
        )
    for issue in issues:
        logger.warning(
            "strategy config consistency: %s [%s] %s",
            issue["field"],
            issue["level"],
            issue["message"],
        )
    return issues
