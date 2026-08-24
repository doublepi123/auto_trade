"""Range-strategy fitness — read-only view over Strategy v2 shadow evidence.

The live range strategy assumes price oscillates inside an interval. A
sustained trend breaks that assumption, and the shadow engine already records
it per bar: ``ADX_REGIME_BLOCKED`` fires whenever ``adx_5m`` exceeds the
configured trend ceiling. Aggregating that flag per symbol answers "is this
symbol still range-like?" without collecting any new data.

Read-only: never mutates shadow evidence, strategy config, or the order path.
It produces evidence for human review, never an automatic promotion.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import StrategyConfig, StrategyV2ShadowDecision

TREND_GATE_REASON = "ADX_REGIME_BLOCKED"

VERDICT_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
VERDICT_TREND_UNSUITABLE = "TREND_UNSUITABLE"
VERDICT_MIXED = "MIXED"
VERDICT_RANGE_SUITABLE = "RANGE_SUITABLE"


@dataclass(frozen=True)
class RangeFitnessRow:
    symbol: str
    is_primary: bool
    samples: int
    trend_blocked: int
    trend_blocked_pct: float
    gate_passed: int
    gate_passed_pct: float
    avg_adx_5m: float | None
    verdict: str


class RangeFitnessService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def assess(
        self,
        *,
        lookback_days: int = 3,
        min_samples: int = 60,
        trend_unsuitable_pct: float = 60.0,
        range_suitable_pct: float = 30.0,
        now: datetime | None = None,
    ) -> list[RangeFitnessRow]:
        if lookback_days <= 0:
            raise ValueError("lookback_days must be positive")
        if min_samples <= 0:
            raise ValueError("min_samples must be positive")
        if not 0 <= range_suitable_pct <= trend_unsuitable_pct <= 100:
            raise ValueError(
                "require 0 <= range_suitable_pct <= trend_unsuitable_pct <= 100"
            )

        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=lookback_days)
        rows = self._db.execute(
            select(
                StrategyV2ShadowDecision.symbol,
                StrategyV2ShadowDecision.gate_passed,
                StrategyV2ShadowDecision.gate_reasons_json,
                StrategyV2ShadowDecision.adx_5m,
            ).where(StrategyV2ShadowDecision.bar_at >= cutoff)
        ).all()

        primary = self._primary_symbol()
        stats: dict[str, dict[str, float]] = {}
        for symbol, gate_passed, reasons_json, adx_5m in rows:
            key = str(symbol or "")
            if not key:
                continue
            bucket = stats.setdefault(
                key,
                {"samples": 0.0, "trend": 0.0, "passed": 0.0, "adx_sum": 0.0, "adx_n": 0.0},
            )
            bucket["samples"] += 1
            if gate_passed:
                bucket["passed"] += 1
            if TREND_GATE_REASON in self._reasons(reasons_json):
                bucket["trend"] += 1
            if adx_5m is not None:
                bucket["adx_sum"] += float(adx_5m)
                bucket["adx_n"] += 1

        out: list[RangeFitnessRow] = []
        for symbol, bucket in stats.items():
            samples = int(bucket["samples"])
            trend = int(bucket["trend"])
            passed = int(bucket["passed"])
            trend_pct = (trend / samples * 100) if samples else 0.0
            passed_pct = (passed / samples * 100) if samples else 0.0
            avg_adx = (
                bucket["adx_sum"] / bucket["adx_n"] if bucket["adx_n"] else None
            )
            out.append(RangeFitnessRow(
                symbol=symbol,
                is_primary=symbol == primary,
                samples=samples,
                trend_blocked=trend,
                trend_blocked_pct=round(trend_pct, 2),
                gate_passed=passed,
                gate_passed_pct=round(passed_pct, 2),
                avg_adx_5m=round(avg_adx, 3) if avg_adx is not None else None,
                verdict=self._verdict(
                    samples,
                    trend_pct,
                    min_samples=min_samples,
                    trend_unsuitable_pct=trend_unsuitable_pct,
                    range_suitable_pct=range_suitable_pct,
                ),
            ))

        out.sort(key=lambda row: (not row.is_primary, row.trend_blocked_pct, row.symbol))
        return out

    @staticmethod
    def _verdict(
        samples: int,
        trend_pct: float,
        *,
        min_samples: int,
        trend_unsuitable_pct: float,
        range_suitable_pct: float,
    ) -> str:
        if samples < min_samples:
            return VERDICT_INSUFFICIENT_DATA
        if trend_pct >= trend_unsuitable_pct:
            return VERDICT_TREND_UNSUITABLE
        if trend_pct <= range_suitable_pct:
            return VERDICT_RANGE_SUITABLE
        return VERDICT_MIXED

    @staticmethod
    def _reasons(reasons_json: object) -> tuple[str, ...]:
        if not reasons_json:
            return ()
        try:
            parsed = json.loads(str(reasons_json))
        except (TypeError, ValueError):
            return ()
        if not isinstance(parsed, list):
            return ()
        return tuple(str(item) for item in parsed)

    def _primary_symbol(self) -> str:
        config = self._db.scalar(
            select(StrategyConfig).order_by(StrategyConfig.id.desc())
        )
        if config is None:
            return ""
        return (config.symbol or "").strip().upper()
