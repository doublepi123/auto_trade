"""Causal, record-only comparison of one frozen range-entry hypothesis.

This module never submits orders, reads a database, or imports a broker.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Literal

from app.core.accounting_fees import ACCOUNTING_FEE_MODEL_US_SEC98, order_fee
from app.core.live_risk_day import risk_day_for
from app.core.market_calendar import (
    get_session, is_closing_window, is_opening_warmup, is_trading_hours, trade_day_for,
)


@dataclass(frozen=True)
class TapeQuote:
    """Prices available at one recorded quote/depth callback, never a future join."""

    symbol: str
    run_id: str
    sequence: int
    observed_at: datetime
    source_at: datetime | None
    depth_at: datetime | None
    last: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    bid_size: int | None
    ask_size: int | None


@dataclass(frozen=True)
class RangeReplayConfig:
    symbol: str = "TSLA.US"
    initial_buy_low: Decimal = Decimal("398.122")
    initial_sell_high: Decimal = Decimal("400.518")
    max_quantity: int = 1000
    max_notional: Decimal = Decimal("85000")
    max_price_risk: Decimal = Decimal("500")
    stop_pct: Decimal = Decimal("0.25")
    profit_activation_pct: Decimal = Decimal("0.4")
    profit_floor_pct: Decimal = Decimal("0.2")
    holding_minutes: int = 60
    warmup_minutes: int = 5
    cutoff_minutes: int = 45
    flatten_minutes: int = 15
    arm_seconds: int = 300
    band_settle_seconds: int = 180
    recenter_minutes: int = 10
    recenter_drift_pct: Decimal = Decimal("0.5")
    recenter_half_width_pct: Decimal = Decimal("0.3")
    max_recenters: int = 12
    cooldown_seconds: int = 60
    max_entries: int = 20
    daily_loss: Decimal = Decimal("2000")
    max_drawdown: Decimal = Decimal("5000")
    max_losses: int = 5
    fill_delay_seconds: int = 1
    order_timeout_seconds: int = 5
    limit_tolerance_bps: Decimal = Decimal("2")

    def __post_init__(self) -> None:
        values = asdict(self)
        for spec in fields(self):
            if type(values[spec.name]) is not type(spec.default):
                raise ValueError(f"{spec.name} has an invalid configuration type")
        for name, value in values.items():
            if isinstance(value, Decimal) and (not value.is_finite() or value < 0):
                raise ValueError(f"{name} must be finite and nonnegative")
            if isinstance(value, bool):
                raise ValueError(f"{name} cannot be boolean")
        if self.symbol != "TSLA.US":
            raise ValueError("range reclaim v1 is registered for TSLA.US only")
        if not 0 < self.initial_buy_low < self.initial_sell_high:
            raise ValueError("initial price band is invalid")
        if not 1 <= self.max_quantity <= 1000:
            raise ValueError("quantity exceeds the frozen paper bound")
        if not 0 < self.max_notional <= 85000 or not 0 < self.max_price_risk <= 500:
            raise ValueError("notional or price risk exceeds the frozen paper bound")
        if not 0 < self.stop_pct <= Decimal("0.25"):
            raise ValueError("stop must be positive and no wider than 0.25 percent")
        if not 0 < self.profit_floor_pct < self.profit_activation_pct:
            raise ValueError("profit-lock thresholds are invalid")
        if self.fill_delay_seconds < 1 or self.order_timeout_seconds <= self.fill_delay_seconds:
            raise ValueError("orders require a delayed, bounded fill window")
        if not 0 <= self.flatten_minutes < self.cutoff_minutes <= 390:
            raise ValueError("session cutoffs are invalid")
        for name in (
            "holding_minutes", "arm_seconds", "recenter_minutes",
            "max_recenters", "max_entries", "max_losses",
        ):
            if values[name] <= 0:
                raise ValueError(f"{name} must be positive")
        if self.cooldown_seconds < 0 or self.band_settle_seconds < 0 or self.warmup_minutes < 0:
            raise ValueError("cooldown, settlement and warmup cannot be negative")

    @property
    def version_hash(self) -> str:
        value = {
            "algorithm_version": "range-reclaim-v1",
            **{key: str(item) if isinstance(item, Decimal) else item for key, item in asdict(self).items()},
        }
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


ReplayArm = Literal["crossing_baseline", "reclaim_candidate"]


def round_trip_outcomes(
    entry: Decimal, exit_: Decimal, quantity: int,
) -> dict[str, dict[str, Decimal]]:
    if (
        any(not p.is_finite() or p <= 0 for p in (entry, exit_))
        or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0
    ):
        raise ValueError("execution prices and integer quantity must be positive")
    gross = (exit_ - entry) * quantity
    turnover = (entry + exit_) * quantity
    measured_fees = sum((
        order_fee(
            model=ACCOUNTING_FEE_MODEL_US_SEC98, market="US",
            price=price, quantity=Decimal(quantity), legacy_rate=Decimal("0.0005"),
        ) for price in (entry, exit_)
    ), Decimal(0))
    results = {}
    for name, fees, extra_bps in (
        ("measured", measured_fees, Decimal("0.031")),
        ("confirmation", measured_fees, Decimal("2.031")),
        ("legacy_stress", turnover * Decimal("0.0005"), Decimal("2.031")),
    ):
        extra = turnover * extra_bps / 10000
        results[name] = {
            "gross": gross, "fees": fees, "extra_costs": extra,
            "net": gross - fees - extra,
        }
    return results


@dataclass
class _Pending:
    side: str
    cause: str
    decision: TapeQuote
    quantity: int
    limit: Decimal
    target: Decimal


@dataclass
class _Position:
    entry: TapeQuote
    decision: TapeQuote
    price: Decimal
    quantity: int
    target: Decimal
    peak_bid: Decimal


def _identity(quote: TapeQuote) -> dict[str, object]:
    return {
        "run_id": quote.run_id, "sequence": quote.sequence,
        "observed_at": quote.observed_at.isoformat(),
        "source_at": quote.source_at.isoformat() if quote.source_at else None,
        "depth_at": quote.depth_at.isoformat() if quote.depth_at else None,
    }


def _money_payload(value: dict[str, dict[str, Decimal]]) -> dict[str, dict[str, str]]:
    return {name: {key: str(amount) for key, amount in fields.items()} for name, fields in value.items()}


class RangeReplayEngine:
    def __init__(self, config: RangeReplayConfig, arm: ReplayArm) -> None:
        if arm not in ("crossing_baseline", "reclaim_candidate"):
            raise ValueError("unknown registered comparison arm")
        self.config = config
        self.arm = arm
        self.buy = config.initial_buy_low
        self.sell = config.initial_sell_high
        self.band_changed_at: datetime | None = None
        self.last_recenter_check: datetime | None = None
        self.armed_at: datetime | None = None
        self.previous_last: Decimal | None = None
        self.last_time: datetime | None = None
        self.last_run: str | None = None
        self.last_sequence = -1
        self.seen_runs: set[str] = set()
        self.session_day: date | None = None
        self.risk_day: date | None = None
        self.day_entries = 0
        self.day_recenters = 0
        self.day_losses = 0
        self.day_realized = Decimal(0)
        self.day_paused = False
        self.drawdown_paused = False
        self.realized = Decimal(0)
        self.equity_peak = Decimal(0)
        self.max_marked_drawdown = Decimal(0)
        self.cooldown_until: datetime | None = None
        self.position: _Position | None = None
        self.entry_order: _Pending | None = None
        self.exit_order: _Pending | None = None
        self.forced_exit_cause: str | None = None
        self.closed: list[dict[str, Any]] = []
        self.signals: Counter[str] = Counter()
        self.quality_rejections: Counter[str] = Counter()
        self.unfilled_entries = 0
        self.exit_retries = 0
        self.valid_events = 0
        self.coverage: dict[date, set[int]] = defaultdict(set)
        self.recenter_events: list[dict[str, str]] = []
        self.cross_session_carries = 0

    @staticmethod
    def _aware(value: datetime) -> bool:
        return value.tzinfo is not None and value.utcoffset() is not None

    def _quality_issue(self, q: TapeQuote) -> str | None:
        if any(v is None or not v.is_finite() or v <= 0 for v in (q.last, q.bid, q.ask)):
            return "MISSING_POSITIVE_BBO"
        assert q.last is not None and q.bid is not None and q.ask is not None
        if q.ask < q.bid:
            return "CROSSED_BBO"
        midpoint = (q.ask + q.bid) / 2
        if (q.ask-q.bid)/midpoint >= Decimal(".005"):
            return "WIDE_SPREAD"
        if abs(q.last-midpoint)/midpoint >= Decimal(".005"):
            return "LAST_BBO_INCONSISTENT"
        if q.source_at is None or not self._aware(q.source_at):
            return "MISSING_SOURCE_TIME"
        if not -2 <= (q.observed_at-q.source_at).total_seconds() <= 30:
            return "STALE_SOURCE"
        if q.depth_at is None or not self._aware(q.depth_at):
            return "MISSING_DEPTH_TIME"
        if not 0 <= (q.observed_at-q.depth_at).total_seconds() <= 5:
            return "STALE_DEPTH"
        return None

    def _roll_clocks(self, q: TapeQuote) -> None:
        day = trade_day_for("US", q.observed_at)
        risk_day = risk_day_for("US", q.observed_at)
        if self.session_day != day:
            if self.session_day is not None and self.position is not None:
                self.cross_session_carries += 1
            self.session_day = day
            self.day_recenters = 0
            self.armed_at = None
            self.previous_last = None
            self.last_recenter_check = None
        if self.risk_day != risk_day:
            self.risk_day = risk_day
            self.day_entries = self.day_losses = 0
            self.day_realized = Decimal(0)
            self.day_paused = False
            # Rolling drawdown peak and latch deliberately survive day rollover.

    def _expire_orders(self, now: datetime) -> None:
        timeout = timedelta(seconds=self.config.order_timeout_seconds)
        if self.entry_order is not None and now > self.entry_order.decision.observed_at+timeout:
            self.unfilled_entries += 1
            self.entry_order = None
        if self.exit_order is not None and now > self.exit_order.decision.observed_at+timeout:
            self.exit_retries += 1
            self.exit_order = None

    def on_quote(self, quote: TapeQuote) -> None:
        if quote.symbol != self.config.symbol or not self._aware(quote.observed_at):
            raise ValueError("quote identity or aware receive time is invalid")
        if (
            not isinstance(quote.run_id, str) or not quote.run_id
            or quote.run_id.strip() != quote.run_id
            or type(quote.sequence) is not int or quote.sequence < 1
        ):
            raise ValueError("capture identity is invalid")
        if self.last_time is not None and quote.observed_at < self.last_time:
            raise ValueError("recorded quote time went backwards")
        if quote.run_id == self.last_run and quote.sequence <= self.last_sequence:
            raise ValueError("duplicate or out-of-order capture sequence")
        if quote.run_id != self.last_run:
            if quote.run_id in self.seen_runs:
                raise ValueError("a previous capture run resumed out of order")
            self.seen_runs.add(quote.run_id)
            self.armed_at = None
            self.previous_last = None
            if self.entry_order is not None:
                self.unfilled_entries += 1
                self.entry_order = None
        self.last_run = quote.run_id
        self.last_sequence = quote.sequence
        self.last_time = quote.observed_at
        self._roll_clocks(quote)
        self._expire_orders(quote.observed_at)
        if not is_trading_hours("US", quote.observed_at):
            self.armed_at = None
            self.previous_last = None
            if self.entry_order is not None:
                self.unfilled_entries += 1
                self.entry_order = None
            return
        day = trade_day_for("US", quote.observed_at)
        self.coverage[day]
        issue = self._quality_issue(quote)
        if issue:
            self.quality_rejections[issue] += 1
            return
        local = get_session("US").local(quote.observed_at)
        if all(isinstance(size, int) and not isinstance(size, bool) and size > 0
               for size in (quote.bid_size, quote.ask_size)):
            self.valid_events += 1
            self.coverage[day].add(local.hour*60+local.minute)
        else:
            # Price-triggered risk exits still get a decision without displayed
            # liquidity. Only a later sufficiently sized event may fill them.
            self.quality_rejections["MISSING_POSITIVE_SIZE"] += 1
        assert quote.last is not None and quote.bid is not None and quote.ask is not None
        previous = self.previous_last
        self.previous_last = quote.last
        if self.entry_order is not None and is_closing_window(
            "US", self.config.cutoff_minutes, quote.observed_at,
        ):
            self.unfilled_entries += 1
            self.entry_order = None
        self._try_entry_fill(quote)
        if self.position is not None:
            self.position.peak_bid = max(self.position.peak_bid, quote.bid)
            self._mark_position(quote.bid)
            cause = self._exit_cause(quote)
            if cause is not None:
                self._request_exit(quote, cause)
            self._try_exit_fill(quote)
            return
        if self.exit_order is not None:
            raise ValueError("exit order cannot exist without a position")
        self._maybe_recenter(quote)
        if not self._entry_allowed(quote):
            self.armed_at = None
            return
        floor = self.buy*(1-self.config.stop_pct/100)
        if quote.last <= floor:
            self.armed_at = None
            self.signals["INVALIDATED_ENTRY_FLOOR"] += 1
            return
        if self.armed_at is not None and quote.observed_at > self.armed_at+timedelta(seconds=self.config.arm_seconds):
            self.signals["ARM_EXPIRED"] += 1
            self.armed_at = None
        down_cross = previous is not None and previous > self.buy and quote.last <= self.buy
        if self.arm == "crossing_baseline" and down_cross:
            self._request_entry(quote)
        elif self.arm == "reclaim_candidate":
            if down_cross:
                self.armed_at = quote.observed_at
                self.signals["ARMED"] += 1
            elif self.armed_at is not None and previous is not None and previous < self.buy <= quote.last:
                self.armed_at = None
                self._request_entry(quote)

    def _entry_allowed(self, q: TapeQuote) -> bool:
        if self.entry_order is not None or self.day_paused or self.drawdown_paused:
            return False
        if self.day_entries >= self.config.max_entries:
            return False
        if self.cooldown_until is not None and q.observed_at < self.cooldown_until:
            return False
        if self.band_changed_at is not None and q.observed_at < self.band_changed_at+timedelta(seconds=self.config.band_settle_seconds):
            return False
        return not is_opening_warmup("US", self.config.warmup_minutes, q.observed_at) and not is_closing_window(
            "US", self.config.cutoff_minutes, q.observed_at,
        )

    def _maybe_recenter(self, q: TapeQuote) -> None:
        if self.entry_order is not None or self.day_recenters >= self.config.max_recenters:
            return
        if is_opening_warmup("US", self.config.warmup_minutes, q.observed_at):
            return
        if self.last_recenter_check is not None and q.observed_at < self.last_recenter_check+timedelta(minutes=self.config.recenter_minutes):
            return
        self.last_recenter_check = q.observed_at
        assert q.last is not None
        drift = (
            (self.buy-q.last)/self.buy if q.last < self.buy
            else (q.last-self.sell)/self.sell if q.last > self.sell else Decimal(0)
        )*100
        if drift < self.config.recenter_drift_pct:
            return
        old_buy, old_sell = self.buy, self.sell
        half = self.config.recenter_half_width_pct/100
        self.buy, self.sell = q.last*(1-half), q.last*(1+half)
        self.band_changed_at = q.observed_at
        self.armed_at = None
        self.previous_last = None
        self.day_recenters += 1
        self.recenter_events.append({
            "at": q.observed_at.isoformat(), "previous_buy": str(old_buy),
            "previous_sell": str(old_sell), "buy": str(self.buy), "sell": str(self.sell),
        })

    def _request_entry(self, q: TapeQuote) -> None:
        assert q.ask is not None
        limit = q.ask*(1+self.config.limit_tolerance_bps/10000)
        quantity = min(
            self.config.max_quantity,
            int((self.config.max_notional/limit).to_integral_value(rounding=ROUND_FLOOR)),
            int((self.config.max_price_risk/(limit*self.config.stop_pct/100)).to_integral_value(rounding=ROUND_FLOOR)),
        )
        self.signals["ENTRY_DECISION"] += 1
        if quantity < 1 or q.ask >= self.sell:
            self.signals["NO_VALID_SIZED_TARGET"] += 1
            return
        self.entry_order = _Pending("BUY", "ENTRY", q, quantity, limit, self.sell)

    def _try_entry_fill(self, q: TapeQuote) -> None:
        order = self.entry_order
        if order is None or q.observed_at < order.decision.observed_at+timedelta(seconds=self.config.fill_delay_seconds):
            return
        assert q.ask is not None and q.bid is not None
        if q.ask <= order.limit and q.ask_size is not None and q.ask_size >= order.quantity:
            self.position = _Position(q, order.decision, q.ask, order.quantity, order.target, q.bid)
            self.day_entries += 1
            self.signals["ENTRY_FILLED"] += 1
            self.entry_order = None
            self.armed_at = None

    def _mark_position(self, bid: Decimal) -> None:
        position = self.position
        assert position is not None
        unrealized = round_trip_outcomes(position.price, bid, position.quantity)["confirmation"]["net"]
        equity = self.realized+unrealized
        self.equity_peak = max(self.equity_peak, equity)
        drawdown = self.equity_peak-equity
        self.max_marked_drawdown = max(self.max_marked_drawdown, drawdown)
        if drawdown >= self.config.max_drawdown:
            self.drawdown_paused = True
        if self.day_realized+unrealized <= -self.config.daily_loss:
            self.day_paused = True

    def _exit_cause(self, q: TapeQuote) -> str | None:
        position = self.position
        assert position is not None and q.bid is not None
        if self.drawdown_paused:
            return "DRAWDOWN_LIMIT"
        if self.day_paused:
            return "DAILY_LOSS"
        if q.bid <= position.price*(1-self.config.stop_pct/100):
            return "PRICE_STOP"
        if position.peak_bid >= position.price*(1+self.config.profit_activation_pct/100) and q.bid <= position.price*(1+self.config.profit_floor_pct/100):
            return "PROFIT_LOCK"
        if is_closing_window("US", self.config.flatten_minutes, q.observed_at):
            return "EOD_FLATTEN"
        if q.observed_at >= position.entry.observed_at+timedelta(minutes=self.config.holding_minutes):
            return "TIME_STOP"
        if self.forced_exit_cause is not None:
            return self.forced_exit_cause
        if q.bid >= position.target:
            return "TARGET"
        return None

    def _request_exit(self, q: TapeQuote, cause: str) -> None:
        priorities = {
            "DRAWDOWN_LIMIT": 0, "DAILY_LOSS": 1, "PRICE_STOP": 2,
            "PROFIT_LOCK": 3, "EOD_FLATTEN": 4, "TIME_STOP": 5, "TARGET": 6,
        }
        if self.exit_order is not None and priorities[self.exit_order.cause] <= priorities[cause]:
            return
        assert self.position is not None and q.bid is not None
        self.exit_order = _Pending(
            "SELL", cause, q, self.position.quantity,
            q.bid*(1-self.config.limit_tolerance_bps/10000), self.position.target,
        )
        if cause != "TARGET":
            self.forced_exit_cause = cause
        self.signals["EXIT_DECISION"] += 1

    def _try_exit_fill(self, q: TapeQuote) -> None:
        order, position = self.exit_order, self.position
        if order is None or position is None or q.observed_at < order.decision.observed_at+timedelta(seconds=self.config.fill_delay_seconds):
            return
        assert q.bid is not None
        if q.bid < order.limit or q.bid_size is None or q.bid_size < order.quantity:
            return
        costs = round_trip_outcomes(position.price, q.bid, position.quantity)
        net = costs["confirmation"]["net"]
        self.realized += net
        self.day_realized += net
        self.day_losses = self.day_losses+1 if net < 0 else 0
        if self.day_losses >= self.config.max_losses or self.day_realized <= -self.config.daily_loss:
            self.day_paused = True
        self.closed.append({
            "entry_at": position.entry.observed_at.isoformat(), "exit_at": q.observed_at.isoformat(),
            "day": trade_day_for("US", q.observed_at).isoformat(),
            "entry_price": str(position.price), "exit_price": str(q.bid),
            "quantity": position.quantity, "target": str(position.target),
            "exit_cause": order.cause, "costs": _money_payload(costs),
            "entry_decision": _identity(position.decision), "entry_fill": _identity(position.entry),
            "exit_decision": _identity(order.decision), "exit_fill": _identity(q),
        })
        self.position = None
        self.exit_order = None
        self.forced_exit_cause = None
        self.cooldown_until = q.observed_at+timedelta(seconds=self.config.cooldown_seconds)
        self.equity_peak = max(self.equity_peak, self.realized)

    def report(self) -> dict[str, object]:
        totals: dict[str, dict[str, str]] = {}
        for scenario in ("measured", "confirmation", "legacy_stress"):
            totals[scenario] = {
                key: str(sum((Decimal(row["costs"][scenario][key]) for row in self.closed), Decimal(0)))
                for key in ("gross", "fees", "extra_costs", "net")
            }
        coverage = {}
        session = get_session("US")
        for day, minutes in sorted(self.coverage.items()):
            start = datetime.combine(day, session.rth_open, tzinfo=session.timezone)
            end = datetime.combine(day, session.close_time(day), tzinfo=session.timezone)
            expected = int((end-start).total_seconds()/60)
            coverage[day.isoformat()] = {
                "valid_minutes": len(minutes), "expected_minutes": expected,
                "fraction": len(minutes)/expected,
            }
        trade_days = {str(row["day"]) for row in self.closed}
        quality_complete = (
            bool(coverage) and self.position is None and self.entry_order is None
            and self.exit_order is None
            and self.cross_session_carries == 0
            and all(row["fraction"] >= .8 for row in coverage.values())
        )
        sample_complete = len(trade_days) >= 20 and len(self.closed) >= 30
        return {
            "algorithm_version": "range-reclaim-v1", "config_version": self.config.version_hash,
            "arm": self.arm, "live_equivalent": False, "automatic_promotion": False,
            "position_open": self.position is not None,
            "pending_entry": self.entry_order is not None, "pending_exit": self.exit_order is not None,
            "unresolved_position": (
                {"entry_at": self.position.entry.observed_at.isoformat(),
                 "entry_price": str(self.position.price), "quantity": self.position.quantity}
                if self.position is not None else None
            ),
            "closed_trades": self.closed.copy(), "cost_totals": totals,
            "signals": dict(self.signals), "valid_events": self.valid_events,
            "quality_rejections": dict(self.quality_rejections),
            "unfilled_entries": self.unfilled_entries, "exit_retries": self.exit_retries,
            "cross_session_carries": self.cross_session_carries,
            "max_mark_to_market_drawdown": str(self.max_marked_drawdown),
            "day_paused": self.day_paused, "drawdown_paused": self.drawdown_paused,
            "daily_coverage": coverage, "closed_trade_days": len(trade_days),
            "quality_complete": quality_complete, "sample_complete": sample_complete,
            "eligible_for_review": quality_complete and sample_complete,
            "status": "REVIEW_REQUIRED" if quality_complete and sample_complete else "INSUFFICIENT_DATA",
            "recenter_events": self.recenter_events.copy(),
        }
