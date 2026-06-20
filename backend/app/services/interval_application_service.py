from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.services.strategy_service import StrategyService

logger = logging.getLogger("auto_trade.interval_application")


class IntervalApplicationService:
    """Applies LLM suggestions with progressive smooth transition and risk guardrails."""

    def apply_suggestion(
        self,
        db: Any,
        engine_state: str,
        current_price: float,
        suggestion: dict[str, Any],
        reference_quantity: float = 1.0,
    ) -> dict[str, Any]:
        """Apply LLM suggestion based on current engine state."""
        svc = StrategyService(db)
        config = svc.get_config()

        buy_low = suggestion.get("suggested_buy_low")
        sell_high = suggestion.get("suggested_sell_high")
        confidence = suggestion.get("confidence_score") or 0.0

        reject_reason = self._validate_guardrails(
            current_price,
            buy_low,
            sell_high,
            confidence,
            min_profit_amount=config.min_profit_amount,
            reference_quantity=reference_quantity,
        )
        if reject_reason:
            config.llm_reject_reason = reject_reason
            config.llm_applied_at = datetime.now(timezone.utc)
            config.llm_applied_buy_low = None
            config.llm_applied_sell_high = None
            db.commit()
            return {
                "success": False,
                "applied": False,
                "reason": reject_reason,
            }

        if engine_state == "flat":
            applied = self._apply_flat(db, config, buy_low, sell_high)
            reason = "FLAT state: interval applied directly"
        elif engine_state == "long":
            applied = self._apply_long(db, config, current_price, buy_low, sell_high)
            reason = f"LONG state: buy_low {config.buy_low:.2f}, sell_high {config.sell_high:.2f}"
        elif engine_state == "short":
            applied = self._apply_short(db, config, current_price, buy_low)
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

        return {
            "success": True,
            "applied": applied,
            "reason": reason,
            "buy_low": config.buy_low,
            "sell_high": config.sell_high,
        }

    def apply_direct_suggestion(
        self,
        db: Any,
        current_price: float,
        suggestion: dict[str, Any],
        reference_quantity: float = 1.0,
    ) -> dict[str, Any]:
        """Apply both suggested interval bounds after guardrail validation."""
        svc = StrategyService(db)
        config = svc.get_config()

        buy_low = suggestion.get("suggested_buy_low")
        sell_high = suggestion.get("suggested_sell_high")
        confidence = suggestion.get("confidence_score") or 0.0

        reject_reason = self._validate_guardrails(
            current_price,
            buy_low,
            sell_high,
            confidence,
            min_profit_amount=config.min_profit_amount,
            reference_quantity=reference_quantity,
        )
        now = datetime.now(timezone.utc)
        if reject_reason:
            config.llm_reject_reason = reject_reason
            config.llm_applied_at = now
            config.llm_applied_buy_low = None
            config.llm_applied_sell_high = None
            db.commit()
            return {
                "success": False,
                "applied": False,
                "reason": reject_reason,
            }

        if buy_low is None or sell_high is None:
            raise RuntimeError("validated LLM suggestion is missing interval bounds")

        old_buy_low = config.buy_low
        old_sell_high = config.sell_high
        config.buy_low = buy_low
        config.sell_high = sell_high
        config.llm_applied_buy_low = config.buy_low
        config.llm_applied_sell_high = config.sell_high
        config.llm_applied_at = now
        config.llm_reject_reason = None
        db.commit()

        return {
            "success": True,
            "applied": config.buy_low != old_buy_low or config.sell_high != old_sell_high,
            "reason": "LLM suggestion applied to buy_low and sell_high",
            "buy_low": config.buy_low,
            "sell_high": config.sell_high,
            "applied_at": now.isoformat(),
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
    def _validate_guardrails(
        current_price: float,
        buy_low: float | None,
        sell_high: float | None,
        confidence: float,
        *,
        min_profit_amount: float = 0.0,
        reference_quantity: float = 1.0,
    ) -> str | None:
        """Validate suggestion against risk guardrails. Returns reject reason or None."""
        if current_price <= 0:
            return "current_price must be positive"

        if confidence < settings.llm_min_confidence:
            return f"confidence_score {confidence:.2f} below threshold {settings.llm_min_confidence}"

        if buy_low is None or sell_high is None or not all(math.isfinite(float(value)) for value in (buy_low, sell_high, confidence)):
            return "buy_low, sell_high, and confidence_score must be finite"

        if buy_low is None or sell_high is None:
            return "missing buy_low or sell_high in suggestion"

        if sell_high <= buy_low:
            return f"sell_high ({sell_high:.2f}) must be greater than buy_low ({buy_low:.2f})"

        interval_width = sell_high - buy_low
        minimum_width = IntervalApplicationService._minimum_interval_width(
            current_price,
            min_profit_amount,
            reference_quantity,
        )
        if interval_width < minimum_width:
            return (
                f"interval width ({interval_width:.2f}) below minimum profit width "
                f"{minimum_width:.2f}"
            )

        stripe_width_pct = (sell_high - buy_low) / current_price * 100
        if stripe_width_pct > settings.llm_max_stripe_width_pct:
            return f"interval width ({stripe_width_pct:.1f}%) exceeds max {settings.llm_max_stripe_width_pct}%"

        return None

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
