"""Fixed-barrier exit state machine (PREREGISTRATION §10.6)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.domain.guidance_continuation.exit import (
    EXIT_GAP,
    EXIT_RESOLVED,
    ExitBarriers,
    evaluate_exit,
)
from app.domain.guidance_continuation.quotes import QuoteObservation

_ET = ZoneInfo("America/New_York")

TARGET_DAY = date(2026, 9, 22)
ENTRY_P = Decimal("100.00")
ENTRY_AT = datetime(2026, 9, 22, 9, 46, 1, tzinfo=_ET)
QTY = 100


def _bid(
    price: str,
    *,
    size: int = 1000,
    at: datetime,
    latency_ms: int = 0,
) -> QuoteObservation:
    # ask = bid + 0.01 keeps the spread ≈ 1 bp (≤ 5 bps) so the quote
    # itself is §10.4-qualifying; tests override ask where needed.
    return QuoteObservation(
        bid=Decimal(price),
        ask=Decimal(price) + Decimal("0.01"),
        bid_size=size,
        ask_size=size,
        quote_ts=at - timedelta(milliseconds=latency_ms),
        received_at=at,
    )


def _barriers() -> ExitBarriers:
    return ExitBarriers.for_position(
        entry_price=ENTRY_P, entry_fill_at=ENTRY_AT, target_day=TARGET_DAY
    )


class TestBarriers:
    def test_barrier_prices(self) -> None:
        b = _barriers()
        assert b.stop_price == Decimal("99.55")
        assert b.target_price == Decimal("100.80")

    def test_holding_deadline_is_60_minutes_after_fill(self) -> None:
        b = _barriers()
        assert b.holding_deadline == ENTRY_AT + timedelta(minutes=60)

    def test_flatten_deadline_is_close_minus_15(self) -> None:
        b = _barriers()
        assert b.flatten_deadline == datetime(2026, 9, 22, 15, 45, tzinfo=_ET)

    def test_half_day_flatten_deadline(self) -> None:
        # 2026-11-27 (Black Friday) closes 13:00 ET → flatten 12:45.
        b = ExitBarriers.for_position(
            entry_price=ENTRY_P,
            entry_fill_at=datetime(2026, 11, 27, 9, 46, 1, tzinfo=_ET),
            target_day=date(2026, 11, 27),
        )
        assert b.flatten_deadline == datetime(2026, 11, 27, 12, 45, tzinfo=_ET)


class TestTriggers:
    def test_bid_at_or_below_stop_triggers_price_stop(self) -> None:
        b = _barriers()
        t = ENTRY_AT + timedelta(minutes=10)
        bids = (
            _bid("99.55", at=t),
            _bid("99.55", at=t + timedelta(seconds=1)),
        )
        result = evaluate_exit(quotes=bids, barriers=b, quantity=QTY)
        assert result.trigger == "PRICE_STOP"
        assert result.exit_status == EXIT_RESOLVED
        assert result.fill_price == Decimal("99.55")

    def test_bid_at_target_triggers_profit_target(self) -> None:
        b = _barriers()
        t = ENTRY_AT + timedelta(minutes=10)
        bids = (
            _bid("100.80", at=t),
            _bid("100.80", at=t + timedelta(seconds=1)),
        )
        result = evaluate_exit(quotes=bids, barriers=b, quantity=QTY)
        assert result.trigger == "PROFIT_TARGET"
        assert result.fill_price == Decimal("100.80")

    def test_bid_above_target_fills_at_min_target_bid(self) -> None:
        # Gap through the target: fill at min(target, bid) = target.
        b = _barriers()
        t = ENTRY_AT + timedelta(minutes=10)
        bids = (
            _bid("101.50", at=t),
            _bid("101.50", at=t + timedelta(seconds=1)),
        )
        result = evaluate_exit(quotes=bids, barriers=b, quantity=QTY)
        assert result.trigger == "PROFIT_TARGET"
        assert result.fill_price == Decimal("100.80")

    def test_same_timestamp_priority_stop_over_target(self) -> None:
        # A single bid cannot be ≤ stop and ≥ target simultaneously;
        # construct two bids with the SAME received_at: first the target
        # bid, then the stop bid — the state machine must still pick STOP
        # (priority applies per timestamp, not per list order).
        b = _barriers()
        at = ENTRY_AT + timedelta(minutes=30)
        bids = (
            _bid("100.80", at=at),
            _bid("99.55", at=at),
        )
        result = evaluate_exit(quotes=bids, barriers=b, quantity=QTY)
        assert result.trigger == "PRICE_STOP"

    def test_b6_trigger_quote_index_names_a_barrier_satisfying_quote(
        self,
    ) -> None:
        # Same instant: a target bid at index 0, a stop bid at index 1.
        # PRICE_STOP wins by priority, so trigger_quote_index MUST be 1 —
        # the quote that actually satisfies the stop barrier — not 0.
        b = _barriers()
        at = ENTRY_AT + timedelta(minutes=30)
        bids = (
            _bid("100.80", at=at),  # satisfies target, NOT stop
            _bid("99.55", at=at),  # satisfies stop
        )
        result = evaluate_exit(quotes=bids, barriers=b, quantity=QTY)
        assert result.trigger == "PRICE_STOP"
        assert result.trigger_quote_index == 1
        # And the named quote truly satisfies the winning barrier.
        named = bids[result.trigger_quote_index or 0]
        assert named.bid <= b.stop_price

    def test_same_timestamp_flatten_over_holding(self) -> None:
        # Entry at 15:45:01 → holding = 16:45:01 (past close); flatten
        # 15:45.  A bid exactly at flatten that is also past holding must
        # pick EOD_FLATTEN (priority: stop > flatten > holding > target).
        entry_at = datetime(2026, 9, 22, 15, 46, 0, tzinfo=_ET)
        b = ExitBarriers.for_position(
            entry_price=ENTRY_P, entry_fill_at=entry_at, target_day=TARGET_DAY
        )
        # holding = 16:46 > close; flatten = 15:45 < entry.  Bid at 16:46:
        # past both flatten and holding, no price barrier → flatten wins.
        at = datetime(2026, 9, 22, 15, 46, 5, tzinfo=_ET)
        bids = (_bid("100.00", at=at),)
        result = evaluate_exit(quotes=bids, barriers=b, quantity=QTY)
        assert result.trigger == "EOD_FLATTEN"

    def test_holding_deadline_triggers_max_hold(self) -> None:
        # First post-deadline quote 1 s after the holding deadline: the
        # trigger is MAX_HOLD AT the deadline instant (10:46:01), and the
        # fill needs a NEW qualifying quote ≥ 1 s after that.
        b = _barriers()
        deadline = ENTRY_AT + timedelta(minutes=60)
        first_after = _bid("100.10", at=deadline + timedelta(seconds=1))
        fill = _bid("100.10", at=deadline + timedelta(seconds=2))
        result = evaluate_exit(quotes=(first_after, fill), barriers=b, quantity=QTY)
        assert result.trigger == "MAX_HOLD"
        assert result.trigger_at == deadline
        assert result.fill_price == Decimal("100.10")

    def test_flatten_deadline_triggers_eod_flatten(self) -> None:
        # §10.5 entry cutoff: the latest legal entry is close − 45 min,
        # so a realistic earlier-flatten position enters ~14:50 and its
        # holding deadline (15:50) is past flatten (15:45).  The first
        # qualifying quote after 15:45 yields EOD_FLATTEN AT 15:45.
        entry_at = datetime(2026, 9, 22, 14, 50, 0, tzinfo=_ET)
        b = ExitBarriers.for_position(
            entry_price=ENTRY_P, entry_fill_at=entry_at, target_day=TARGET_DAY
        )
        assert b.holding_deadline == datetime(2026, 9, 22, 15, 50, tzinfo=_ET)
        assert b.flatten_deadline == datetime(2026, 9, 22, 15, 45, tzinfo=_ET)
        first_after = _bid("100.10", at=datetime(2026, 9, 22, 15, 45, 30, tzinfo=_ET))
        spare = _bid("100.10", at=datetime(2026, 9, 22, 15, 45, 31, tzinfo=_ET))
        result = evaluate_exit(
            quotes=(first_after, spare), barriers=b, quantity=QTY
        )
        assert result.trigger == "EOD_FLATTEN"
        assert result.trigger_at == datetime(2026, 9, 22, 15, 45, tzinfo=_ET)
        assert result.fill_at == first_after.received_at


class TestFillSemantics:
    def test_wait_1s_before_fill(self) -> None:
        # §10.6: the trigger quote itself cannot be the fill; a fill needs
        # a NEW qualifying bid ≥ 1 s later.
        b = _barriers()
        trigger_at = ENTRY_AT + timedelta(minutes=10)
        trigger = _bid("99.55", at=trigger_at)
        early = _bid("99.55", at=trigger_at + timedelta(milliseconds=999))
        ok = _bid("99.55", at=trigger_at + timedelta(seconds=1))
        result = evaluate_exit(
            quotes=(trigger, early, ok), barriers=b, quantity=QTY
        )
        assert result.trigger_at == trigger_at
        assert result.fill_at == ok.received_at
        assert result.trigger_to_fill_seconds == Decimal("1")

    def test_bid_size_shortfall_leaves_gap_then_fills(self) -> None:
        b = _barriers()
        trigger_at = ENTRY_AT + timedelta(minutes=10)
        trigger = _bid("99.55", at=trigger_at)
        thin = _bid("99.55", size=99, at=trigger_at + timedelta(seconds=1))
        ok = _bid("99.55", size=100, at=trigger_at + timedelta(seconds=5))
        result = evaluate_exit(
            quotes=(trigger, thin, ok), barriers=b, quantity=QTY
        )
        assert result.fill_at == ok.received_at
        assert result.fill_bid_size == 100
        assert result.trigger_to_fill_seconds == Decimal("5")

    def test_no_fill_quote_is_exit_gap(self) -> None:
        b = _barriers()
        trigger_at = ENTRY_AT + timedelta(minutes=10)
        only = _bid("99.55", at=trigger_at)
        result = evaluate_exit(quotes=(only,), barriers=b, quantity=QTY)
        assert result.exit_status == EXIT_GAP
        assert result.fill_at is None
        assert result.fill_price is None

    def test_exit_gap_never_fabricates_deadline_fill(self) -> None:
        # Morning entry: holding (10:46) < flatten (15:45), so the time
        # exit is MAX_HOLD at the holding deadline.  No bid_size-covered
        # quote afterwards → GAP, and the trigger timestamp stays honest
        # at the deadline rather than drifting to the thin 15:50 quote.
        b = _barriers()
        at = datetime(2026, 9, 22, 15, 50, tzinfo=_ET)
        thin = _bid("100.00", size=10, at=at)
        result = evaluate_exit(quotes=(thin,), barriers=b, quantity=QTY)
        assert result.trigger == "MAX_HOLD"
        assert result.trigger_at == ENTRY_AT + timedelta(minutes=60)
        assert result.exit_status == EXIT_GAP

    def test_delay_recorded_separately(self) -> None:
        b = _barriers()
        trigger_at = ENTRY_AT + timedelta(minutes=10)
        trigger = _bid("99.55", at=trigger_at)
        fill = _bid("99.55", at=trigger_at + timedelta(seconds=3))
        result = evaluate_exit(quotes=(trigger, fill), barriers=b, quantity=QTY)
        assert result.trigger_at == trigger_at
        assert result.fill_at == trigger_at + timedelta(seconds=3)
        assert result.trigger_to_fill_seconds == Decimal("3")

    def test_trigger_quote_itself_never_fills_identical_received_at(
        self,
    ) -> None:
        # Two quotes with IDENTICAL received_at: the trigger is the first
        # stop-crossing group; the fill must be a NEW quote strictly after
        # that group — a same-instant sibling cannot fill, only the later
        # quote (here 1 s after) can.
        b = _barriers()
        trigger_at = ENTRY_AT + timedelta(minutes=10)
        first = _bid("99.55", at=trigger_at)
        twin = _bid("99.60", at=trigger_at)  # same received_at
        later = _bid("99.55", at=trigger_at + timedelta(seconds=1))
        result = evaluate_exit(
            quotes=(first, twin, later), barriers=b, quantity=QTY
        )
        assert result.trigger == "PRICE_STOP"
        # trigger_quote_index points at the FIRST quote of the group
        assert result.trigger_quote_index == 0
        # fill index must be the later quote, not the same-instant twin
        assert result.fill_quote_index == 2
        assert result.fill_at == later.received_at

    def test_zero_or_negative_bid_ignored_for_trigger(self) -> None:
        b = _barriers()
        bad = _bid("0", at=ENTRY_AT + timedelta(minutes=5))
        good = _bid("100.10", at=ENTRY_AT + timedelta(minutes=30))
        result = evaluate_exit(quotes=(bad, good), barriers=b, quantity=QTY)
        assert result.trigger in {"MAX_HOLD", "EOD_FLATTEN"}


class TestQuoteQualification:
    """§10.6 processes only 有效 (qualifying) quotes — same §10.4 rule."""

    def test_stale_quote_neither_triggers_nor_fills(self) -> None:
        # quote_ts 5 s before received_at → stale.  Its bid ≤ stop must
        # NOT trigger; the position survives to the later healthy quote.
        b = _barriers()
        t = ENTRY_AT + timedelta(minutes=10)
        stale = _bid("99.50", at=t, latency_ms=5000)
        healthy = _bid("100.10", at=ENTRY_AT + timedelta(minutes=30))
        result = evaluate_exit(quotes=(stale, healthy), barriers=b, quantity=QTY)
        assert result.trigger != "PRICE_STOP"
        assert result.skipped_quote_count == 1

    def test_stale_quote_cannot_fill_either(self) -> None:
        # Trigger first, then only stale quotes at/after trigger + 1 s
        # with deep bid_size → must remain EXIT_GAP, not RESOLVED.
        b = _barriers()
        trigger_at = ENTRY_AT + timedelta(minutes=10)
        trigger = _bid("99.55", at=trigger_at)
        stale1 = _bid("99.50", at=trigger_at + timedelta(seconds=1), latency_ms=5000)
        result = evaluate_exit(quotes=(trigger, stale1), barriers=b, quantity=QTY)
        assert result.exit_status == EXIT_GAP

    def test_crossed_quote_neither_triggers_nor_fills(self) -> None:
        b = _barriers()
        t = ENTRY_AT + timedelta(minutes=10)
        crossed = QuoteObservation(
            bid=Decimal("99.50"),
            ask=Decimal("99.49"),  # ask < bid → crossed → invalid
            bid_size=1000,
            ask_size=1000,
            quote_ts=t,
            received_at=t,
        )
        healthy = _bid("100.10", at=ENTRY_AT + timedelta(minutes=30))
        result = evaluate_exit(quotes=(crossed, healthy), barriers=b, quantity=QTY)
        assert result.trigger != "PRICE_STOP"
        assert result.skipped_quote_count == 1

    def test_wide_spread_quote_neither_triggers_nor_fills(self) -> None:
        b = _barriers()
        t = ENTRY_AT + timedelta(minutes=10)
        wide = QuoteObservation(
            bid=Decimal("99.50"),
            ask=Decimal("100.50"),  # ~100 bps spread → invalid
            bid_size=1000,
            ask_size=1000,
            quote_ts=t,
            received_at=t,
        )
        healthy = _bid("100.10", at=ENTRY_AT + timedelta(minutes=30))
        result = evaluate_exit(quotes=(wide, healthy), barriers=b, quantity=QTY)
        assert result.trigger != "PRICE_STOP"
        assert result.skipped_quote_count == 1

    def test_qualification_uses_full_quote_observation(self) -> None:
        # The exit input carries the full two-sided observation, so
        # qualify_quote (§10.4) governs both triggering and filling.
        b = _barriers()
        t = ENTRY_AT + timedelta(minutes=10)
        stale_trigger = _bid("99.50", at=t, latency_ms=5000)
        ok_trigger = _bid("99.55", at=ENTRY_AT + timedelta(minutes=20))
        ok_fill = _bid("99.55", at=ENTRY_AT + timedelta(minutes=20, seconds=1))
        result = evaluate_exit(
            quotes=(stale_trigger, ok_trigger, ok_fill), barriers=b, quantity=QTY
        )
        assert result.trigger == "PRICE_STOP"
        assert result.trigger_quote_index == 1
        assert result.fill_quote_index == 2

    def test_freshness_exactly_1s_qualifies_for_exit(self) -> None:
        b = _barriers()
        t = ENTRY_AT + timedelta(minutes=10)
        edge = _bid("99.55", at=t, latency_ms=1000)
        fill = _bid("99.55", at=t + timedelta(seconds=1))
        result = evaluate_exit(quotes=(edge, fill), barriers=b, quantity=QTY)
        assert result.trigger == "PRICE_STOP"
        assert result.exit_status == EXIT_RESOLVED


class TestDeadlineSemantics:
    """Time exits fire AT the deadline instant, not at the next quote."""

    def test_quote_exactly_at_holding_deadline_with_stop_bid_is_price_stop(
        self,
    ) -> None:
        # Same-instant priority: a qualifying bid AT the holding deadline
        # with bid ≤ stop gives PRICE_STOP, not MAX_HOLD.
        b = _barriers()
        at = ENTRY_AT + timedelta(minutes=60)  # exactly holding deadline
        trigger = _bid("99.55", at=at)
        fill = _bid("99.55", at=at + timedelta(seconds=1))
        result = evaluate_exit(quotes=(trigger, fill), barriers=b, quantity=QTY)
        assert result.trigger == "PRICE_STOP"
        assert result.trigger_at == at

    def test_bid_after_deadline_gives_max_hold_at_deadline_instant(self) -> None:
        # First qualifying quote 3 s AFTER the holding deadline with a
        # bid ≤ stop: MAX_HOLD wins (price barriers are not evaluated
        # after the deadline), trigger_at IS the deadline, and the fill
        # is the deadline + 3 s quote itself (already ≥ trigger + 1 s;
        # a time exit has no trigger quote to exclude).
        b = _barriers()
        deadline = ENTRY_AT + timedelta(minutes=60)
        late = _bid("99.50", at=deadline + timedelta(seconds=3))
        spare = _bid("99.50", at=deadline + timedelta(seconds=4))
        result = evaluate_exit(quotes=(late, spare), barriers=b, quantity=QTY)
        assert result.trigger == "MAX_HOLD"
        assert result.trigger_at == deadline
        assert result.fill_at == late.received_at
        assert result.trigger_to_fill_seconds == Decimal("3")

    def test_flatten_deadline_earlier_than_holding_gives_flatten(self) -> None:
        # flatten (15:45) < holding (17:00): a quiet pre-deadline quote at
        # 15:30 triggers nothing; the exit is EOD_FLATTEN AT the flatten
        # instant even though the first post-deadline quote is later.
        b = ExitBarriers(
            stop_price=Decimal("99.55"),
            target_price=Decimal("100.80"),
            holding_deadline=datetime(2026, 9, 22, 17, 0, tzinfo=_ET),
            flatten_deadline=datetime(2026, 9, 22, 15, 45, tzinfo=_ET),
        )
        pre = _bid("100.10", at=datetime(2026, 9, 22, 15, 30, tzinfo=_ET))
        first_after = _bid(
            "100.10", at=datetime(2026, 9, 22, 15, 45, 2, tzinfo=_ET)
        )
        result = evaluate_exit(quotes=(pre, first_after), barriers=b, quantity=QTY)
        assert result.trigger == "EOD_FLATTEN"
        assert result.trigger_at == datetime(2026, 9, 22, 15, 45, tzinfo=_ET)
        assert result.fill_at == first_after.received_at

    def test_price_barrier_still_evaluated_at_deadline_instant(self) -> None:
        # At the deadline instant itself, price barriers remain live:
        # stop > flatten > holding > target at one shared timestamp.
        b = _barriers()
        at = ENTRY_AT + timedelta(minutes=60)  # == holding deadline
        target_bid = _bid("100.80", at=at)
        stop_bid = _bid("99.55", at=at)  # same received_at group
        fill = _bid("99.55", at=at + timedelta(seconds=1))
        result = evaluate_exit(
            quotes=(target_bid, stop_bid, fill), barriers=b, quantity=QTY
        )
        assert result.trigger == "PRICE_STOP"
        assert result.trigger_at == at

    def test_quote_just_before_deadline_still_price_side(self) -> None:
        # 1 µs before the holding deadline the price barrier still rules.
        b = _barriers()
        at = ENTRY_AT + timedelta(minutes=60) - timedelta(microseconds=1)
        trigger = _bid("100.80", at=at)
        fill = _bid("100.80", at=at + timedelta(seconds=1))
        result = evaluate_exit(quotes=(trigger, fill), barriers=b, quantity=QTY)
        assert result.trigger == "PROFIT_TARGET"
        assert result.trigger_at == at
