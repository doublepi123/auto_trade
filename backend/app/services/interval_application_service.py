from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from app.config import settings
from app.services.strategy_service import StrategyService

logger = logging.getLogger("auto_trade.interval_application")


class LLMIntervalDisposition(str, Enum):
    ALLOW = "ALLOW"
    REJECT = "REJECT"
    SHADOW = "SHADOW"


@dataclass(frozen=True)
class LLMIntervalPolicyDecision:
    disposition: LLMIntervalDisposition
    code: str
    reason: str
    buy_low: float | None = None
    sell_high: float | None = None
    confidence: float | None = None
    deviation_pct: float | None = None


class IntervalApplicationService:
    """Applies LLM suggestions with progressive smooth transition and risk guardrails."""

    def apply_suggestion(
        self,
        db: Any,
        engine_state: str,
        current_price: float,
        suggestion: dict[str, Any],
        reference_quantity: float = 1.0,
        runtime_reload: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Apply LLM suggestion based on current engine state."""
        svc = StrategyService(db)
        config = svc.get_config()

        policy = self._evaluate_policy(
            current_price,
            suggestion.get("suggested_buy_low"),
            suggestion.get("suggested_sell_high"),
            suggestion.get("confidence_score"),
            min_profit_amount=config.min_profit_amount,
            reference_quantity=reference_quantity,
        )
        if policy.disposition != LLMIntervalDisposition.ALLOW:
            return self._record_non_application(db, config, policy)

        buy_low = policy.buy_low
        sell_high = policy.sell_high
        normalized_current_price = float(current_price)
        previous = self._application_snapshot(config)

        if engine_state == "flat":
            applied = self._apply_flat(db, config, buy_low, sell_high)
            reason = "FLAT state: interval applied directly"
        elif engine_state == "long":
            applied = self._apply_long(db, config, normalized_current_price, buy_low, sell_high)
            reason = f"LONG state: buy_low {config.buy_low:.2f}, sell_high {config.sell_high:.2f}"
        elif engine_state == "short":
            applied = self._apply_short(db, config, normalized_current_price, buy_low)
            reason = f"SHORT state: buy_low adjusted to {config.buy_low:.2f}"
        else:
            return {
                "success": False,
                "applied": False,
                "reason": f"Unknown engine state: {engine_state}",
            }

        config.llm_applied_buy_low = config.buy_low
        config.llm_applied_sell_high = config.sell_high
        config.llm_applied_at = datetime.now(timezone.utc)
        config.llm_reject_reason = None
        db.commit()
        self._confirm_or_rollback(db, config, previous, runtime_reload)

        return {
            "success": True,
            "applied": applied,
            "reason": reason,
            "buy_low": config.buy_low,
            "sell_high": config.sell_high,
            "policy_status": policy.disposition.value,
            "policy_code": policy.code,
            "deviation_pct": policy.deviation_pct,
        }

    def apply_direct_suggestion(
        self,
        db: Any,
        current_price: float,
        suggestion: dict[str, Any],
        reference_quantity: float = 1.0,
        runtime_reload: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Apply both suggested interval bounds after guardrail validation."""
        svc = StrategyService(db)
        config = svc.get_config()

        policy = self._evaluate_policy(
            current_price,
            suggestion.get("suggested_buy_low"),
            suggestion.get("suggested_sell_high"),
            suggestion.get("confidence_score"),
            min_profit_amount=config.min_profit_amount,
            reference_quantity=reference_quantity,
        )
        now = datetime.now(timezone.utc)
        if policy.disposition != LLMIntervalDisposition.ALLOW:
            return self._record_non_application(db, config, policy)

        buy_low = policy.buy_low
        sell_high = policy.sell_high

        if buy_low is None or sell_high is None:
            raise RuntimeError("validated LLM suggestion is missing interval bounds")

        old_buy_low = config.buy_low
        old_sell_high = config.sell_high
        previous = self._application_snapshot(config)
        config.buy_low = buy_low
        config.sell_high = sell_high
        config.llm_applied_buy_low = config.buy_low
        config.llm_applied_sell_high = config.sell_high
        config.llm_applied_at = now
        config.llm_reject_reason = None
        db.commit()
        self._confirm_or_rollback(db, config, previous, runtime_reload)

        return {
            "success": True,
            "applied": config.buy_low != old_buy_low or config.sell_high != old_sell_high,
            "reason": "LLM suggestion applied to buy_low and sell_high",
            "buy_low": config.buy_low,
            "sell_high": config.sell_high,
            "applied_at": now.isoformat(),
            "policy_status": policy.disposition.value,
            "policy_code": policy.code,
            "deviation_pct": policy.deviation_pct,
        }

    @staticmethod
    def _application_snapshot(config: Any) -> dict[str, Any]:
        return {
            "buy_low": config.buy_low,
            "sell_high": config.sell_high,
            "llm_applied_buy_low": config.llm_applied_buy_low,
            "llm_applied_sell_high": config.llm_applied_sell_high,
            "llm_applied_at": config.llm_applied_at,
            "llm_reject_reason": config.llm_reject_reason,
        }

    @staticmethod
    def _confirm_or_rollback(
        db: Any,
        config: Any,
        previous: dict[str, Any],
        runtime_reload: Callable[[], None] | None,
    ) -> None:
        if runtime_reload is None:
            return
        try:
            runtime_reload()
        except Exception:
            logger.exception("interval runtime reload failed; rolling back applied interval")
            for field_name, value in previous.items():
                setattr(config, field_name, value)
            db.commit()
            try:
                runtime_reload()
            except Exception:
                logger.critical("interval rollback could not be confirmed in runtime", exc_info=True)
            raise

    @staticmethod
    def _record_non_application(
        db: Any,
        config: Any,
        policy: LLMIntervalPolicyDecision,
    ) -> dict[str, Any]:
        config.llm_reject_reason = (
            policy.reason
            if policy.disposition == LLMIntervalDisposition.REJECT
            else None
        )
        db.commit()
        return {
            "success": policy.disposition == LLMIntervalDisposition.SHADOW,
            "applied": False,
            "reason": policy.reason,
            "policy_status": policy.disposition.value,
            "policy_code": policy.code,
            "deviation_pct": policy.deviation_pct,
        }

    @staticmethod
    def _apply_flat(db: Any, config: Any, buy_low: float | None, sell_high: float | None) -> bool:
        """Apply new interval directly when flat."""
        if buy_low is not None:
            config.buy_low = buy_low
        if sell_high is not None:
            config.sell_high = sell_high
        return True

    @staticmethod
    def _apply_long(
        db: Any,
        config: Any,
        current_price: float,
        new_buy_low: float | None,
        new_sell_high: float | None,
    ) -> bool:
        """Apply non-chasing interval adjustments when long."""
        old_buy_low = config.buy_low
        old_sell_high = config.sell_high

        if new_buy_low is not None and new_buy_low <= old_buy_low:
            config.buy_low = new_buy_low

        if new_sell_high is not None:
            min_sell_high = current_price * (1 + settings.llm_interval_volatility_threshold_pct / 100)
            config.sell_high = max(new_sell_high, min_sell_high)

        return config.buy_low != old_buy_low or config.sell_high != old_sell_high

    @staticmethod
    def _apply_short(db: Any, config: Any, current_price: float, new_buy_low: float | None) -> bool:
        """Apply buy_low adjustment when short."""
        if new_buy_low is None:
            return False

        old_buy_low = config.buy_low
        max_buy_low = current_price * (1 - settings.llm_interval_volatility_threshold_pct / 100)

        if new_buy_low <= old_buy_low:
            config.buy_low = new_buy_low
        else:
            config.buy_low = min(new_buy_low, max_buy_low)

        return config.buy_low != old_buy_low

    @staticmethod
    def _evaluate_policy(
        current_price: Any,
        buy_low: Any,
        sell_high: Any,
        confidence: Any,
        *,
        min_profit_amount: float = 0.0,
        reference_quantity: float = 1.0,
    ) -> LLMIntervalPolicyDecision:
        """Validate first, then downgrade an otherwise valid suggestion to shadow."""
        normalized_price = IntervalApplicationService._finite_number(current_price)
        if normalized_price is None or normalized_price <= 0:
            return LLMIntervalPolicyDecision(
                LLMIntervalDisposition.REJECT,
                "INVALID_CURRENT_PRICE",
                "current_price must be a positive finite number",
            )

        normalized_confidence = IntervalApplicationService._finite_number(confidence)
        if normalized_confidence is None or not 0 <= normalized_confidence <= 1:
            return LLMIntervalPolicyDecision(
                LLMIntervalDisposition.REJECT,
                "INVALID_CONFIDENCE",
                "confidence_score must be a finite number between 0 and 1",
            )

        if normalized_confidence < settings.llm_min_confidence:
            return LLMIntervalPolicyDecision(
                LLMIntervalDisposition.REJECT,
                "LOW_CONFIDENCE",
                (
                    f"confidence_score {normalized_confidence:.2f} below threshold "
                    f"{settings.llm_min_confidence}"
                ),
                confidence=normalized_confidence,
            )

        if buy_low is None or sell_high is None:
            return LLMIntervalPolicyDecision(
                LLMIntervalDisposition.REJECT,
                "MISSING_INTERVAL_BOUND",
                "missing buy_low or sell_high in suggestion",
                confidence=normalized_confidence,
            )

        normalized_buy_low = IntervalApplicationService._finite_number(buy_low)
        normalized_sell_high = IntervalApplicationService._finite_number(sell_high)
        if normalized_buy_low is None or normalized_sell_high is None:
            return LLMIntervalPolicyDecision(
                LLMIntervalDisposition.REJECT,
                "INVALID_INTERVAL_BOUND",
                "buy_low and sell_high must be finite numbers",
                confidence=normalized_confidence,
            )
        if normalized_buy_low <= 0 or normalized_sell_high <= 0:
            return LLMIntervalPolicyDecision(
                LLMIntervalDisposition.REJECT,
                "INVALID_INTERVAL_BOUND",
                "buy_low and sell_high must be positive",
                buy_low=normalized_buy_low,
                sell_high=normalized_sell_high,
                confidence=normalized_confidence,
            )

        if normalized_sell_high <= normalized_buy_low:
            return LLMIntervalPolicyDecision(
                LLMIntervalDisposition.REJECT,
                "INVALID_INTERVAL_ORDER",
                (
                    f"sell_high ({normalized_sell_high:.2f}) must be greater than "
                    f"buy_low ({normalized_buy_low:.2f})"
                ),
                buy_low=normalized_buy_low,
                sell_high=normalized_sell_high,
                confidence=normalized_confidence,
            )

        interval_width = normalized_sell_high - normalized_buy_low
        minimum_width = IntervalApplicationService._minimum_interval_width(
            normalized_price,
            min_profit_amount,
            reference_quantity,
        )
        if interval_width < minimum_width:
            return LLMIntervalPolicyDecision(
                LLMIntervalDisposition.REJECT,
                "INTERVAL_TOO_NARROW",
                (
                    f"interval width ({interval_width:.2f}) below minimum profit width "
                    f"{minimum_width:.2f}"
                ),
                buy_low=normalized_buy_low,
                sell_high=normalized_sell_high,
                confidence=normalized_confidence,
            )

        stripe_width_pct = interval_width / normalized_price * 100
        if stripe_width_pct > settings.llm_max_stripe_width_pct:
            return LLMIntervalPolicyDecision(
                LLMIntervalDisposition.REJECT,
                "INTERVAL_TOO_WIDE",
                (
                    f"interval width ({stripe_width_pct:.1f}%) exceeds max "
                    f"{settings.llm_max_stripe_width_pct}%"
                ),
                buy_low=normalized_buy_low,
                sell_high=normalized_sell_high,
                confidence=normalized_confidence,
            )

        deviation_pct = max(
            abs(normalized_buy_low - normalized_price),
            abs(normalized_sell_high - normalized_price),
        ) / normalized_price * 100
        if deviation_pct > settings.llm_max_interval_bound_deviation_pct:
            return LLMIntervalPolicyDecision(
                LLMIntervalDisposition.REJECT,
                "INTERVAL_BOUND_DEVIATION",
                (
                    f"interval bound deviation {deviation_pct:.3f}% exceeds max "
                    f"{settings.llm_max_interval_bound_deviation_pct:.3f}%"
                ),
                buy_low=normalized_buy_low,
                sell_high=normalized_sell_high,
                confidence=normalized_confidence,
                deviation_pct=deviation_pct,
            )

        disposition = (
            LLMIntervalDisposition.SHADOW
            if settings.llm_shadow_mode
            else LLMIntervalDisposition.ALLOW
        )
        return LLMIntervalPolicyDecision(
            disposition,
            "SHADOW_MODE" if disposition == LLMIntervalDisposition.SHADOW else "ALLOW",
            (
                "LLM shadow mode records the validated interval without changing live bounds"
                if disposition == LLMIntervalDisposition.SHADOW
                else "LLM interval passed live policy"
            ),
            buy_low=normalized_buy_low,
            sell_high=normalized_sell_high,
            confidence=normalized_confidence,
            deviation_pct=deviation_pct,
        )

    @staticmethod
    def _finite_number(value: Any) -> float | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            normalized = float(value)
        except (TypeError, ValueError):
            return None
        return normalized if math.isfinite(normalized) else None

    @staticmethod
    def _minimum_interval_width(
        current_price: float,
        min_profit_amount: float,
        reference_quantity: float,
    ) -> float:
        min_exit_pct = settings.min_exit_profit_pct or 0.0
        pct_width = current_price * min_exit_pct / 100 if current_price > 0 else 0.0
        try:
            quantity = float(reference_quantity)
        except (TypeError, ValueError):
            quantity = 0.0
        try:
            configured_amount = float(min_profit_amount)
        except (TypeError, ValueError):
            configured_amount = 0.0
        amount_width = configured_amount / quantity if configured_amount > 0 and quantity > 0 else 0.0
        return max(pct_width, amount_width)
