from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.orm import Session

from app.core.market_calendar import trade_day_for
from app.models import (
    OrderRecord,
    StrategyV2ShadowConfig,
    StrategyV2ShadowDecision,
    StrategyV2ShadowState,
)
from app.services.trade_execution_service import EntryPolicyCheckResult

_ENTRY_ACTIONS = ("BUY", "SELL_SHORT")


class LiveEntryPolicyService:
    """Fail-closed bridge from shadow evidence to live entry permission."""

    def __init__(
        self,
        db: Session,
        *,
        regime_gate_enabled: bool,
        max_data_age_seconds: int,
        max_entries_per_symbol_per_day: int,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        if max_data_age_seconds < 60:
            raise ValueError("max_data_age_seconds must be at least 60")
        if max_entries_per_symbol_per_day < 0:
            raise ValueError(
                "max_entries_per_symbol_per_day must not be negative"
            )
        self.db = db
        self.regime_gate_enabled = regime_gate_enabled
        self.max_data_age_seconds = max_data_age_seconds
        self.max_entries_per_symbol_per_day = (
            max_entries_per_symbol_per_day
        )
        self._now_provider = now_provider or (
            lambda: datetime.now(timezone.utc)
        )

    def evaluate(
        self,
        symbol: str,
        action: str,
        market: str,
    ) -> EntryPolicyCheckResult | None:
        normalized_action = action.upper()
        if normalized_action not in _ENTRY_ACTIONS:
            return None
        now = self._aware_now()
        entry_count = self._entry_count(
            symbol=symbol,
            market=market,
            now=now,
        )
        if (
            self.max_entries_per_symbol_per_day > 0
            and entry_count >= self.max_entries_per_symbol_per_day
        ):
            return EntryPolicyCheckResult(
                issue=(
                    f"daily entry cap reached for {symbol}: "
                    f"{entry_count}/{self.max_entries_per_symbol_per_day}"
                ),
                skip_category="COOLDOWN",
                details={
                    "entry_policy": "DAILY_ENTRY_CAP",
                    "entries_today": entry_count,
                    "max_entries_per_day": (
                        self.max_entries_per_symbol_per_day
                    ),
                },
            )
        if not self.regime_gate_enabled:
            return None

        config = (
            self.db.query(StrategyV2ShadowConfig)
            .filter(StrategyV2ShadowConfig.symbol == symbol)
            .first()
        )
        if config is None or not config.enabled:
            return self._regime_block(
                "strategy v2 shadow gate is not enabled",
                "SHADOW_GATE_DISABLED",
            )
        state = (
            self.db.query(StrategyV2ShadowState)
            .filter(StrategyV2ShadowState.symbol == symbol)
            .first()
        )
        if state is None or not state.config_version:
            return self._regime_block(
                "strategy v2 shadow state is unavailable",
                "SHADOW_STATE_UNAVAILABLE",
            )
        if state.last_poll_error:
            return self._regime_block(
                "strategy v2 shadow data is not healthy",
                "SHADOW_DATA_ERROR",
                shadow_error=state.last_poll_error[:200],
            )

        session_date = trade_day_for(market, now)
        if state.session_date != session_date:
            return self._regime_block(
                "strategy v2 shadow state is stale for the current session",
                "SHADOW_SESSION_STALE",
                expected_session_date=session_date.isoformat(),
                shadow_session_date=(
                    state.session_date.isoformat()
                    if state.session_date is not None
                    else ""
                ),
            )
        decision = (
            self.db.query(StrategyV2ShadowDecision)
            .filter(
                StrategyV2ShadowDecision.symbol == symbol,
                StrategyV2ShadowDecision.config_version
                == state.config_version,
                StrategyV2ShadowDecision.session_date == session_date,
            )
            .order_by(
                StrategyV2ShadowDecision.bar_at.desc(),
                StrategyV2ShadowDecision.id.desc(),
            )
            .first()
        )
        if decision is None:
            return self._regime_block(
                "strategy v2 shadow decision is unavailable",
                "SHADOW_DECISION_UNAVAILABLE",
            )
        bar_at = self._as_utc(decision.bar_at)
        age_seconds = (now - bar_at).total_seconds()
        if age_seconds < -60 or age_seconds > self.max_data_age_seconds:
            return self._regime_block(
                "strategy v2 shadow decision is stale",
                "SHADOW_DECISION_STALE",
                decision_age_seconds=round(age_seconds, 3),
                max_data_age_seconds=self.max_data_age_seconds,
            )
        if not decision.gate_passed:
            return self._regime_block(
                "strategy v2 shadow entry gate rejected the current regime",
                "SHADOW_REGIME_REJECTED",
                decision_at=bar_at.isoformat(),
                gate_reasons=self._gate_reasons(decision.gate_reasons_json),
                adx_5m=decision.adx_5m,
                realized_vol_1m=decision.realized_vol_1m,
                zscore_1m=decision.zscore_1m,
                zscore_5m=decision.zscore_5m,
            )
        return None

    def _entry_count(
        self,
        *,
        symbol: str,
        market: str,
        now: datetime,
    ) -> int:
        expected_day = trade_day_for(market, now)
        rows = (
            self.db.query(OrderRecord)
            .filter(
                OrderRecord.symbol == symbol,
                OrderRecord.side.in_(_ENTRY_ACTIONS),
            )
            .all()
        )
        count = 0
        for row in rows:
            executed_quantity = float(row.executed_quantity or 0)
            has_execution = executed_quantity > 0 or (
                row.status == "FILLED"
                and float(row.quantity or 0) > 0
            )
            if not has_execution:
                continue
            occurred_at = (
                row.filled_at
                or row.broker_updated_at
                or row.created_at
            )
            if (
                occurred_at is not None
                and trade_day_for(
                    market,
                    self._as_utc(occurred_at),
                )
                == expected_day
            ):
                count += 1
        return count

    @staticmethod
    def _gate_reasons(raw: str) -> list[str]:
        try:
            decoded = json.loads(raw or "[]")
        except (TypeError, json.JSONDecodeError):
            return ["INVALID_GATE_REASON_PAYLOAD"]
        if not isinstance(decoded, list):
            return ["INVALID_GATE_REASON_PAYLOAD"]
        return [str(item) for item in decoded[:20]]

    @staticmethod
    def _regime_block(
        issue: str,
        policy_reason: str,
        **details: object,
    ) -> EntryPolicyCheckResult:
        return EntryPolicyCheckResult(
            issue=issue,
            skip_category="REGIME",
            details={
                "entry_policy": "STRATEGY_V2_SHADOW_GATE",
                "policy_reason": policy_reason,
                **details,
            },
        )

    def _aware_now(self) -> datetime:
        return self._as_utc(self._now_provider())

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
