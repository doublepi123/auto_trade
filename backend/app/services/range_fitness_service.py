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

from app.models import (
    StrategyConfig,
    StrategyV2ShadowDecision,
    StrategyV2ShadowTrade,
)

TREND_GATE_REASON = "ADX_REGIME_BLOCKED"

VERDICT_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
VERDICT_TREND_UNSUITABLE = "TREND_UNSUITABLE"
VERDICT_MIXED = "MIXED"
VERDICT_RANGE_SUITABLE = "RANGE_SUITABLE"

# Fraction of entry price a closed shadow trade's peak favourable excursion must
# reach to count toward the reach-rate. ``mfe_pct`` is stored as a fraction, so
# 0.004 is 0.4%: the level a profit target must clear to beat the ~0.14%
# round-trip cost with margin.
REACH_MFE_THRESHOLD = 0.004


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
    last_close_price: float | None = None
    # Closed-trade reach evidence. ``None`` when the symbol has no closed shadow
    # trades in the window: absent evidence, not evidence of a zero reach-rate.
    closed_trades: int = 0
    reach_count: int = 0
    reach_rate_pct: float | None = None


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
                StrategyV2ShadowDecision.bar_at,
                StrategyV2ShadowDecision.close_price,
            ).where(StrategyV2ShadowDecision.bar_at >= cutoff)
        ).all()

        primary = self._primary_symbol()
        reach = self._reach_rates(cutoff)
        stats: dict[str, dict[str, float]] = {}
        latest_close: dict[str, tuple[datetime, float]] = {}
        for symbol, gate_passed, reasons_json, adx_5m, bar_at, close_price in rows:
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
            if close_price is not None and float(close_price) > 0 and bar_at is not None:
                current = latest_close.get(key)
                if current is None or bar_at > current[0]:
                    latest_close[key] = (bar_at, float(close_price))

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
                last_close_price=(
                    latest_close[symbol][1] if symbol in latest_close else None
                ),
                closed_trades=reach.get(symbol, (0, 0))[0],
                reach_count=reach.get(symbol, (0, 0))[1],
                reach_rate_pct=self._reach_rate_pct(reach.get(symbol)),
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

    def _reach_rates(self, cutoff: datetime) -> dict[str, tuple[int, int]]:
        """Return ``symbol -> (closed_trades, reach_count)`` over the window.

        Counts CLOSED shadow trades only: an open trade has no final excursion
        yet, so including it would understate the reach-rate. Trades missing
        ``mfe_pct`` are excluded from both counts rather than treated as misses,
        so a backfill gap cannot fabricate an unreachable symbol.
        """
        rows = self._db.execute(
            select(
                StrategyV2ShadowTrade.symbol,
                StrategyV2ShadowTrade.mfe_pct,
            ).where(
                StrategyV2ShadowTrade.status != "OPEN",
                StrategyV2ShadowTrade.exit_at.is_not(None),
                StrategyV2ShadowTrade.exit_at >= cutoff,
                StrategyV2ShadowTrade.mfe_pct.is_not(None),
            )
        ).all()

        out: dict[str, tuple[int, int]] = {}
        for symbol, mfe_pct in rows:
            key = str(symbol or "").strip().upper()
            if not key:
                continue
            closed, reached = out.get(key, (0, 0))
            hit = 1 if float(mfe_pct) >= REACH_MFE_THRESHOLD else 0
            out[key] = (closed + 1, reached + hit)
        return out

    @staticmethod
    def _reach_rate_pct(entry: tuple[int, int] | None) -> float | None:
        if entry is None or entry[0] <= 0:
            return None
        return round(entry[1] / entry[0] * 100, 2)

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
