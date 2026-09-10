"""Recenter a stranded live band on the primary symbol's own fresh price.

DEFAULT OFF.

The band is written once — when the primary symbol switches — and never tracks
price afterwards. ``AutoPrimarySwitchService`` centres it on the candidate's
last close at switch time and nothing revisits it, so a sustained trend strands
it: a long-only entry needs ``price <= buy_low``, and once price has walked
above ``sell_high`` no quote can ever cross. The observed deployment ran 34 days
and 40,000+ quote evaluations with zero threshold crossings and, tellingly,
zero skips in every category — nothing was blocking entries, price simply never
arrived. The LLM advisor cannot repair this either: P0 pins it to shadow.

What this does NOT claim
------------------------
Recentering restores the ability to trade the CONFIGURED strategy. It asserts
nothing about that strategy having edge. At the time of writing the live signal
edge verdict is FAIL (net clustered t=-3.59), so enabling this on that signal
resumes a negative-expectancy strategy inside the P0 sizing caps. That is a
product decision and is why this ships default-off behind its own flag.

Safety
------
Recentering mutates live strategy state, so it goes through exactly the gate a
symbol switch does — ``AppRunner.assert_primary_switch_safe`` — which refuses
while a position, pending order, in-flight trigger, unresolved reconciliation
or non-FLAT engine state exists. That gate is what makes moving ``buy_low``
safe: raising it while LONG would be an add-on in disguise, which P0 forbids.

Two bounds stop the band being walked along with price, which would silently
convert a range strategy into a momentum chaser:

* ``interval_recenter_min_drift_pct`` — act only on real drift, never on a
  small excursion.
* ``interval_recenter_max_per_day`` — a hard ceiling per exchange-local trading
  day, so even repeated drift cannot march the band indefinitely in one
  session.

The reference price must be provably fresh on both sides; an unmeasurable or
future-dated age fails closed, because recentering onto a price the market has
left reproduces the stranded band this exists to fix.

This module never submits, prices, or sizes an order.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.market_calendar import trade_day_for
from app.models import StrategyConfig, TradeEvent
from app.services.trade_event_service import (
    encode_event_payload,
    record_trade_event,
)

logger = logging.getLogger("auto_trade.interval_recenter")

OUTCOME_DISABLED = "DISABLED"
OUTCOME_NO_PRIMARY = "NO_PRIMARY_CONFIGURED"
OUTCOME_WITHIN_BAND = "WITHIN_BAND"
OUTCOME_STALE_PRICE = "STALE_REFERENCE_PRICE"
OUTCOME_BLOCKED = "RECENTER_BLOCKED"
OUTCOME_RECENTERED = "RECENTERED"

# Durable provenance vocabulary. Operators grep these, so they must stay stable.
EVENT_INTERVAL_RECENTERED = "INTERVAL_RECENTERED"
EVENT_INTERVAL_RECENTER_BLOCKED = "INTERVAL_RECENTER_BLOCKED"
EVENT_INTERVAL_RECENTER_ROLLED_BACK = "INTERVAL_RECENTER_ROLLED_BACK"

# The SDK stamps quotes at whole-second resolution while the host clock has
# microseconds, so a quote read at hh:mm:27.9 can carry the stamp hh:mm:28 and
# look 0.1s "in the future" purely from truncation. Two seconds absorbs that
# plus ordinary NTP drift; anything larger is still treated as clock corruption
# and fails closed.
_CLOCK_SKEW_TOLERANCE_SECONDS = 2.0


@dataclass(frozen=True)
class IntervalRecenterResult:
    outcome: str
    symbol: str = ""
    reference_price: float | None = None
    drift_pct: float | None = None
    previous_buy_low: float | None = None
    previous_sell_high: float | None = None
    new_buy_low: float | None = None
    new_sell_high: float | None = None
    detail: str = ""


def _as_utc(value: datetime) -> datetime:
    """Read a naive datetime as the UTC instant it was written as.

    SQLite drops the offset on ``DateTime(timezone=True)`` columns, so a stored
    UTC timestamp comes back naive and comparing it to an aware anchor raises.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _coerce_quote_time(value: Any) -> datetime | None:
    """Read a broker quote timestamp, whatever shape the SDK hands back.

    The Longbridge SDK returns ``Quote.timestamp`` as a naive UTC STRING
    ("2026-09-09 14:37:57"), not a datetime. Accepting only datetimes made
    every live quote look undatable, so the freshness check fail-closed on a
    perfectly fresh price and the band was never recentered — the job reported
    healthy ticks while doing nothing, which is worse than an outright error.

    Naive values are read as UTC, matching how the SDK emits them. An
    unparseable value returns ``None`` so the caller still fails closed rather
    than inventing an age.
    """
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return _as_utc(parsed)
    return None


def _reference_is_fresh(age_seconds: float | None) -> bool:
    """Fresh means measurable and inside the bound on BOTH sides.

    A future-dated quote yields a negative age that satisfies any upper bound,
    yet a price the market has not printed yet is clock-skewed or corrupt, not
    fresh. Unmeasurable and future-dated ages both fail closed.
    """
    if age_seconds is None:
        return False
    return (
        -_CLOCK_SKEW_TOLERANCE_SECONDS
        <= age_seconds
        <= settings.interval_recenter_max_price_age_seconds
    )


def _drift_pct(price: float, buy_low: float, sell_high: float) -> float:
    """How far price sits OUTSIDE the band, as a percentage of the near bound.

    Zero while price is inside, so a positive threshold can only ever fire on
    real drift. Mirrors ``AlertRuleService._interval_deviation_pct`` so the
    alert and the remedy agree on what "drifted" means.
    """
    if price > sell_high:
        return (price - sell_high) / sell_high * 100
    if price < buy_low:
        return (buy_low - price) / buy_low * 100
    return 0.0


class IntervalRecenterService:
    def __init__(
        self,
        db: Session,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def evaluate(self, runner: Any) -> IntervalRecenterResult:
        if not settings.interval_recenter_enabled:
            return IntervalRecenterResult(OUTCOME_DISABLED)

        anchor = _as_utc(self._clock())
        config = self._db.scalar(
            select(StrategyConfig).order_by(StrategyConfig.id.desc())
        )
        symbol = (config.symbol or "").strip().upper() if config else ""
        if config is None or not symbol:
            return IntervalRecenterResult(OUTCOME_NO_PRIMARY)

        market = (config.market or "US").strip().upper()
        try:
            buy_low = float(config.buy_low or 0)
            sell_high = float(config.sell_high or 0)
        except (TypeError, ValueError):
            return IntervalRecenterResult(OUTCOME_NO_PRIMARY, symbol=symbol)
        if buy_low <= 0 or sell_high <= buy_low:
            # An already-invalid band is a configuration problem, not drift.
            # Recentering would paper over it, so leave it for a human.
            return IntervalRecenterResult(
                OUTCOME_NO_PRIMARY,
                symbol=symbol,
                detail="configured interval is not a valid band",
            )

        price, price_at = self._reference_price(runner, symbol)
        if price is None or price <= 0:
            return IntervalRecenterResult(
                OUTCOME_STALE_PRICE,
                symbol=symbol,
                detail="no usable reference price",
            )
        # Age is measured from the clock AFTER the fetch returned, not from the
        # anchor read at the top of evaluate(). get_quotes is a network round
        # trip that was observed taking ~10s on the SDK's first call after a
        # reconnect; measured against the earlier anchor, the broker's
        # timestamp landed in the future and a fresh quote was rejected.
        observed_at = _as_utc(self._clock())
        age = (
            None
            if price_at is None
            else (observed_at - _as_utc(price_at)).total_seconds()
        )
        if not _reference_is_fresh(age):
            return IntervalRecenterResult(
                OUTCOME_STALE_PRICE,
                symbol=symbol,
                reference_price=price,
                detail=(
                    "reference price age unknown"
                    if age is None
                    else f"reference price age {age:.0f}s outside bound "
                    f"{settings.interval_recenter_max_price_age_seconds}s"
                ),
            )

        drift = _drift_pct(price, buy_low, sell_high)
        if drift < settings.interval_recenter_min_drift_pct:
            return IntervalRecenterResult(
                OUTCOME_WITHIN_BAND,
                symbol=symbol,
                reference_price=price,
                drift_pct=drift,
                previous_buy_low=buy_low,
                previous_sell_high=sell_high,
            )

        used_today = self._recenters_today(symbol, market, anchor)
        if used_today >= settings.interval_recenter_max_per_day:
            detail = (
                f"daily recenter cap reached ({used_today}/"
                f"{settings.interval_recenter_max_per_day})"
            )
            self._record_blocked(symbol, price, drift, detail)
            return IntervalRecenterResult(
                OUTCOME_BLOCKED,
                symbol=symbol,
                reference_price=price,
                drift_pct=drift,
                detail=detail,
            )

        half_width = price * (settings.llm_interval_volatility_threshold_pct / 100)
        new_buy_low = round(price - half_width, 4)
        new_sell_high = round(price + half_width, 4)
        if new_buy_low <= 0 or new_sell_high <= new_buy_low:
            return IntervalRecenterResult(
                OUTCOME_BLOCKED,
                symbol=symbol,
                reference_price=price,
                drift_pct=drift,
                detail="cannot derive a valid interval from the reference price",
            )

        # The same gate a symbol switch passes. Moving buy_low while a position
        # is open would be an add-on in disguise, so this must precede the write.
        try:
            runner.assert_primary_switch_safe(symbol, market)
        except Exception as exc:
            detail = f"live safety gate refused: {exc}"
            self._record_blocked(symbol, price, drift, detail)
            logger.info("interval recenter blocked for %s: %s", symbol, exc)
            return IntervalRecenterResult(
                OUTCOME_BLOCKED,
                symbol=symbol,
                reference_price=price,
                drift_pct=drift,
                previous_buy_low=buy_low,
                previous_sell_high=sell_high,
                detail=detail,
            )

        self._commit(
            runner,
            symbol=symbol,
            previous_buy_low=buy_low,
            previous_sell_high=sell_high,
            new_buy_low=new_buy_low,
            new_sell_high=new_sell_high,
            price=price,
            drift=drift,
        )
        return IntervalRecenterResult(
            OUTCOME_RECENTERED,
            symbol=symbol,
            reference_price=price,
            drift_pct=drift,
            previous_buy_low=buy_low,
            previous_sell_high=sell_high,
            new_buy_low=new_buy_low,
            new_sell_high=new_sell_high,
        )

    # --- internals -------------------------------------------------------

    def _reference_price(
        self,
        runner: Any,
        symbol: str,
    ) -> tuple[float | None, datetime | None]:
        """Read the primary symbol's own latest quote from the broker.

        A broker outage must never look like a healthy price: it returns
        ``None`` and the caller fails closed rather than recentering onto a
        fabricated value.
        """
        broker = getattr(runner, "broker", None)
        if broker is None:
            return None, None
        try:
            quotes = broker.get_quotes([symbol])
        except Exception:
            logger.warning(
                "interval recenter reference quote fetch failed", exc_info=True
            )
            return None, None
        for quote in quotes or []:
            if (getattr(quote, "symbol", "") or "").strip().upper() != symbol:
                continue
            try:
                price = float(getattr(quote, "last_price", 0) or 0)
            except (TypeError, ValueError):
                return None, None
            return price, _coerce_quote_time(getattr(quote, "timestamp", None))
        return None, None

    def _recenters_today(
        self,
        symbol: str,
        market: str,
        anchor: datetime,
    ) -> int:
        """Count today's successful recenters for this symbol.

        Counted on the EXCHANGE-local trading day, matching how PnL and risk
        reset, so the cap tracks a session rather than a UTC midnight that
        falls mid-session for US markets.
        """
        day = trade_day_for(market, anchor)
        rows = self._db.scalars(
            select(TradeEvent)
            .where(
                TradeEvent.event_type == EVENT_INTERVAL_RECENTERED,
                TradeEvent.symbol == symbol,
            )
            .order_by(TradeEvent.id.desc())
            .limit(200)
        )
        count = 0
        for row in rows:
            created = getattr(row, "created_at", None)
            if created is None:
                continue
            if trade_day_for(market, _as_utc(created)) == day:
                count += 1
        return count

    def _record_blocked(
        self,
        symbol: str,
        price: float,
        drift: float,
        detail: str,
    ) -> None:
        try:
            record_trade_event(
                self._db,
                event_type=EVENT_INTERVAL_RECENTER_BLOCKED,
                symbol=symbol,
                status=OUTCOME_BLOCKED,
                message=f"interval recenter blocked for {symbol}: {detail}",
                payload={
                    "reference_price": price,
                    "drift_pct": drift,
                    "detail": detail,
                },
            )
            self._db.commit()
        except Exception:
            # Provenance must never take down the caller; the outcome is
            # already returned to it and logged.
            logger.warning("interval recenter block event failed", exc_info=True)
            self._db.rollback()

    def _commit(
        self,
        runner: Any,
        *,
        symbol: str,
        previous_buy_low: float,
        previous_sell_high: float,
        new_buy_low: float,
        new_sell_high: float,
        price: float,
        drift: float,
    ) -> None:
        """Persist the new band and reload the engine, rolling back on failure.

        A band the engine could not load must not stay committed, or the
        persisted config and the live engine disagree about where entries are.
        """
        from app.services.strategy_service import StrategyService

        svc = StrategyService(self._db)
        payload: dict[str, Any] = {
            "symbol": symbol,
            "previous_buy_low": previous_buy_low,
            "previous_sell_high": previous_sell_high,
            "new_buy_low": new_buy_low,
            "new_sell_high": new_sell_high,
            "reference_price": price,
            "drift_pct": drift,
        }
        # Staged BEFORE update_config, which commits the session itself.
        # Staging afterwards would leave a crash window in which the live band
        # has changed with no record of why.
        event = record_trade_event(
            self._db,
            event_type=EVENT_INTERVAL_RECENTERED,
            symbol=symbol,
            status=OUTCOME_RECENTERED,
            message=(
                f"interval recentered for {symbol}: "
                f"[{previous_buy_low}, {previous_sell_high}] -> "
                f"[{new_buy_low}, {new_sell_high}] at {price}"
            ),
            payload=payload,
        )
        svc.update_config({"buy_low": new_buy_low, "sell_high": new_sell_high})
        reload_strategy = getattr(runner, "reload_strategy", None)
        try:
            if callable(reload_strategy):
                reload_strategy()
        except Exception as exc:
            logger.exception(
                "interval recenter could not be completed for %s; rolling back",
                symbol,
            )
            event.event_type = EVENT_INTERVAL_RECENTER_ROLLED_BACK
            event.status = "ROLLED_BACK"
            event.message = (
                f"interval recenter for {symbol} rolled back: {exc}"
            )
            event.payload_json = encode_event_payload(
                {**payload, "rollback_reason": str(exc)}
            )
            self._db.add(event)
            svc.update_config({
                "buy_low": previous_buy_low,
                "sell_high": previous_sell_high,
            })
            try:
                if callable(reload_strategy):
                    reload_strategy()
            except Exception:
                logger.critical(
                    "interval recenter rollback reload failed", exc_info=True
                )
            raise
