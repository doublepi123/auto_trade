from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.core.fees import evaluate_long_round_trip_edge, one_side_fee_rate
from app.models import RuntimeState, StrategyConfig, WatchlistItem

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
    "max_drawdown_amount",
    "max_consecutive_losses",
    "llm_interval_minutes",
    "fee_rate_us",
    "fee_rate_hk",
    "min_repricing_pct",
    "llm_action_cooldown_seconds",
    "trading_session_mode",
    "margin_safety_factor",
    "allow_position_addons",
    "max_position_quantity",
    "max_position_notional",
    "max_risk_per_trade",
    "stop_loss_pct",
    "max_holding_minutes",
    "entry_cutoff_minutes_before_close",
    "flatten_minutes_before_close",
    "llm_order_execution_enabled",
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
        if data.get("short_selling"):
            logger.warning("forcing short_selling=false under the P0 live safety policy")
            data = {**data, "short_selling": False}
        config = self.db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
        is_new = config is None
        if config is None:
            config = StrategyConfig()

        before = {k: getattr(config, k, None) for k in STRATEGY_AUDIT_KEYS}

        updatable_fields = [
            "symbol", "market", "buy_low", "sell_high",
            "short_selling", "min_profit_amount", "auto_resume_minutes",
            "max_daily_loss", "max_drawdown_amount", "max_consecutive_losses",
            "llm_interval_minutes",
            "fee_rate_us", "fee_rate_hk", "min_repricing_pct", "llm_action_cooldown_seconds",
            "trading_session_mode",
            "margin_safety_factor",
            "allow_position_addons",
            "max_position_quantity",
            "max_position_notional",
            "max_risk_per_trade",
            "stop_loss_pct",
            "max_holding_minutes",
            "entry_cutoff_minutes_before_close",
            "flatten_minutes_before_close",
            "llm_order_execution_enabled",
            "report_schedule_enabled",
            "report_schedule_interval_hours",
            "report_schedule_symbol",
        ]
        for field in updatable_fields:
            if field in data:
                setattr(config, field, data[field])

        if "symbol" in data or "market" in data:
            normalized_symbol = str(config.symbol or "").strip().upper()
            self.db.query(WatchlistItem).update(
                {WatchlistItem.is_active: False},
                synchronize_session=False,
            )
            if normalized_symbol:
                self.db.query(WatchlistItem).filter(
                    WatchlistItem.symbol == normalized_symbol,
                ).update(
                    {WatchlistItem.is_active: True},
                    synchronize_session=False,
                )
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
            # Two concurrent callers can both see ``state is None`` and try
            # to INSERT the same primary key; catch the IntegrityError and
            # re-query so the loser gets the winner's row instead of
            # surfacing a 500 to the caller.
            try:
                self.db.commit()
            except IntegrityError:
                self.db.rollback()
                state = self.db.query(RuntimeState).filter(
                    RuntimeState.symbol == normalized
                ).first()
                if state is None:
                    raise
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
        "daily_pnl_date", "consecutive_losses", "cumulative_realized_pnl",
        "peak_realized_pnl", "last_price",
        "last_trigger_price", "last_trigger_at",
        "long_entry_rearm_required",
        "execution_state", "reduction_action", "reduction_cause",
        "reduction_reason", "reduction_started_at", "reduction_trigger_price",
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
      * The configured interval's estimated net edge after round-trip fees
        and slippage, using the configured reference quantity. This mirrors
        the live entry gate except for the live BBO spread, which is only
        available when an order is evaluated.
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
    fee_rate = one_side_fee_rate(
        market,
        Decimal(str(getattr(config, "fee_rate_us", 0) or 0)),
        Decimal(str(getattr(config, "fee_rate_hk", 0) or 0)),
    )
    min_profit = Decimal(str(config.min_profit_amount or 0))
    if (
        config.buy_low > 0
        and config.sell_high > config.buy_low
        and fee_rate >= 0
    ):
        quantity = Decimal(
            str(max(1, int(config.max_position_quantity or 1)))
        )
        entry_price = Decimal(str(config.buy_low))
        slippage = (
            entry_price
            * quantity
            * Decimal(str(settings.entry_round_trip_slippage_bps))
            / Decimal("10000")
        )
        edge = evaluate_long_round_trip_edge(
            entry_price=entry_price,
            exit_price=Decimal(str(config.sell_high)),
            quantity=quantity,
            one_side_rate=fee_rate,
            minimum_profit_amount=min_profit,
            minimum_profit_pct=Decimal(
                str(settings.min_exit_profit_pct or 0)
            ),
            extra_costs=slippage,
        )
        minimum_ratio = Decimal(
            str(settings.min_entry_edge_cost_ratio)
        )
        if not edge.meets(minimum_ratio):
            ratio = (
                f"{edge.edge_cost_ratio:.3f}"
                if edge.edge_cost_ratio is not None
                else "unbounded"
            )
            issues.append(
                {
                    "field": "sell_high",
                    "level": "warning",
                    "message": (
                        f"configured interval has fee-adjusted net profit "
                        f"{edge.net_profit:.2f} versus required "
                        f"{edge.required_profit:.2f}, with edge/cost ratio "
                        f"{ratio} versus minimum {minimum_ratio:.3f}, at "
                        f"quantity={quantity}. Widen the interval, lower "
                        f"{fee_rate_field}/cost assumptions, or reduce the "
                        "minimum profit requirement before live entry."
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
