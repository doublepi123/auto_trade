from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models import LLMInteraction, OrderRecord, RuntimeStateSnapshot

logger = logging.getLogger(__name__)

_EVALUATION_TAGS = {
    "EFFECTIVE",
    "INEFFECTIVE",
    "TOO_EARLY",
    "TOO_LATE",
    "RISKY",
    "INSUFFICIENT_DATA",
}

_BUY_ACTIONS = {"BUY_NOW", "BUY_TO_COVER_NOW"}
_SELL_ACTIONS = {"SELL_NOW", "SELL_SHORT_NOW", "STOP_LOSS_SELL_NOW", "STOP_LOSS_COVER_NOW"}


class LLMRecommendationEvaluator:
    """Evaluates historical LLM recommendations against subsequent price action."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def evaluate(
        self,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
        horizon_minutes: int = 60,
        price_adverse_threshold_pct: float = 3.0,
        price_risky_threshold_pct: float = 5.0,
        profit_threshold_pct: float = 0.5,
    ) -> dict[str, Any]:
        """Evaluate all LLM interactions for a symbol within a date range."""
        interactions = self._query_interactions(symbol, start, end)
        samples: list[dict[str, Any]] = []
        for interaction in interactions:
            sample = self._evaluate_single(
                interaction,
                horizon_minutes=horizon_minutes,
                price_adverse_threshold_pct=price_adverse_threshold_pct,
                price_risky_threshold_pct=price_risky_threshold_pct,
                profit_threshold_pct=profit_threshold_pct,
            )
            samples.append(sample)

        tag_counts: dict[str, int] = {tag: 0 for tag in _EVALUATION_TAGS}
        for s in samples:
            tag_counts[s["tag"]] = tag_counts.get(s["tag"], 0) + 1

        effective_count = tag_counts.get("EFFECTIVE", 0)
        total_evaluable = len([s for s in samples if s["tag"] != "INSUFFICIENT_DATA"])

        return {
            "symbol": symbol,
            "horizon_minutes": horizon_minutes,
            "sample_count": len(samples),
            "tag_distribution": tag_counts,
            "hit_rate": effective_count / total_evaluable if total_evaluable > 0 else 0.0,
            "samples": samples,
        }

    def _query_interactions(
        self,
        symbol: str,
        start: datetime | None,
        end: datetime | None,
    ) -> list[LLMInteraction]:
        query = (
            self.db.query(LLMInteraction)
            .filter(LLMInteraction.symbol == symbol)
            .filter(LLMInteraction.success.is_(True))  # type: ignore[union-attr]
            .order_by(LLMInteraction.created_at.asc())
        )
        if start is not None:
            query = query.filter(LLMInteraction.created_at >= start)
        if end is not None:
            query = query.filter(LLMInteraction.created_at <= end)
        return query.all()

    @staticmethod
    def _extract_price(item: Any) -> float:
        if isinstance(item, dict):
            return float(item.get("last_price", 0) or 0)
        return float(item) if item is not None else 0.0

    def _evaluate_single(
        self,
        interaction: LLMInteraction,
        horizon_minutes: int,
        price_adverse_threshold_pct: float,
        price_risky_threshold_pct: float,
        profit_threshold_pct: float,
    ) -> dict[str, Any]:
        # Parse parsed_response
        try:
            parsed: dict[str, Any] = json.loads(interaction.parsed_response or "{}")
        except json.JSONDecodeError:
            parsed = {}

        order_action = parsed.get("order_action", "NONE")
        if order_action == "NONE":
            return self._make_sample(interaction, parsed, "INSUFFICIENT_DATA", "no order action")

        # Get context snapshot
        try:
            context: dict[str, Any] = json.loads(interaction.context_snapshot or "{}")
        except json.JSONDecodeError:
            context = {}

        start_price = context.get("current_price")
        if start_price is None or start_price <= 0:
            return self._make_sample(
                interaction, parsed, "INSUFFICIENT_DATA", "missing start price"
            )

        # Query snapshots in window
        window_end = interaction.created_at + timedelta(minutes=horizon_minutes)
        snapshots = (
            self.db.query(RuntimeStateSnapshot)
            .filter(RuntimeStateSnapshot.symbol == interaction.symbol)
            .filter(RuntimeStateSnapshot.created_at >= interaction.created_at)
            .filter(RuntimeStateSnapshot.created_at <= window_end)
            .order_by(RuntimeStateSnapshot.created_at.asc())
            .all()
        )

        # Filter out snapshots with missing or non-positive prices to avoid
        # distorting min/max/end calculations.
        snapshots = [s for s in snapshots if s.last_price is not None and s.last_price > 0]

        if len(snapshots) < 2:
            return self._make_sample(
                interaction, parsed, "INSUFFICIENT_DATA", "insufficient snapshots"
            )

        prices = [s.last_price for s in snapshots]
        max_price = max(prices)
        min_price = min(prices)
        end_price = prices[-1]

        # Query orders in window
        orders = (
            self.db.query(OrderRecord)
            .filter(OrderRecord.symbol == interaction.symbol)
            .filter(OrderRecord.created_at >= interaction.created_at)
            .filter(OrderRecord.created_at <= window_end)
            .all()
        )

        # Determine direction
        is_buy = order_action in _BUY_ACTIONS
        is_sell = order_action in _SELL_ACTIONS

        if not is_buy and not is_sell:
            return self._make_sample(
                interaction,
                parsed,
                "INSUFFICIENT_DATA",
                f"unrecognized action {order_action}",
            )

        # Calculate metrics
        if is_buy:
            profit_pct = (end_price - start_price) / start_price * 100
            adverse_pct = (start_price - min_price) / start_price * 100
        else:
            profit_pct = (start_price - end_price) / start_price * 100
            adverse_pct = (max_price - start_price) / start_price * 100

        # Check TOO_LATE via recent_prices
        recent_prices = context.get("recent_prices", [])
        too_late = False
        if recent_prices and len(recent_prices) >= 2:
            first_price = self._extract_price(recent_prices[0])
            last_price = self._extract_price(recent_prices[-1])
            if first_price > 0:
                if is_buy:
                    recent_change = (last_price - first_price) / first_price * 100
                    too_late = recent_change > profit_threshold_pct * 2
                else:
                    recent_change = (first_price - last_price) / first_price * 100
                    too_late = recent_change > profit_threshold_pct * 2

        # Check TOO_EARLY
        too_early = adverse_pct > price_adverse_threshold_pct and profit_pct > profit_threshold_pct

        # Determine tag
        if too_late:
            tag = "TOO_LATE"
            reason = "price already moved significantly before recommendation"
        elif adverse_pct > price_risky_threshold_pct:
            tag = "RISKY"
            reason = f"max adverse move {adverse_pct:.2f}% exceeds threshold"
        elif too_early:
            tag = "TOO_EARLY"
            reason = f"initial adverse move {adverse_pct:.2f}% before profit"
        elif profit_pct > profit_threshold_pct:
            has_favorable_order = any(
                (
                    is_buy
                    and o.side == "BUY"
                    and o.status in ("FILLED", "PARTIAL_FILLED")
                )
                or (
                    is_sell
                    and o.side == "SELL"
                    and o.status in ("FILLED", "PARTIAL_FILLED")
                )
                for o in orders
            )
            tag = "EFFECTIVE"
            reason = "profitable direction" + (
                " with order execution" if has_favorable_order else ""
            )
        else:
            tag = "INEFFECTIVE"
            reason = f"profit {profit_pct:.2f}% below threshold"

        return self._make_sample(
            interaction,
            parsed,
            tag,
            reason,
            {
                "start_price": start_price,
                "end_price": end_price,
                "max_price": max_price,
                "min_price": min_price,
                "profit_pct": round(profit_pct, 4),
                "adverse_pct": round(adverse_pct, 4),
                "snapshot_count": len(snapshots),
                "order_count": len(orders),
            },
        )

    def _make_sample(
        self,
        interaction: LLMInteraction,
        parsed: dict[str, Any],
        tag: str,
        reason: str,
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "interaction_id": interaction.id,
            "created_at": interaction.created_at.isoformat(),
            "order_action": parsed.get("order_action", "NONE"),
            "order_price": parsed.get("order_price"),
            "tag": tag,
            "reason": reason,
            "metrics": metrics or {},
        }
