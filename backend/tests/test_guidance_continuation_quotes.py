"""Quote qualification and entry-fill simulation (PREREGISTRATION §10.4)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.domain.guidance_continuation.config import DEFAULT_GUIDANCE_CONFIG
from app.domain.guidance_continuation.quotes import (
    EntrySimulationResult,
    QuoteObservation,
    attempt_window,
    qualify_quote,
    simulate_entry,
)
from app.domain.guidance_continuation.sizing import position_quantity

_ET = ZoneInfo("America/New_York")
TARGET_DAY = date(2026, 9, 22)
WINDOW_START = datetime(2026, 9, 22, 9, 46, 0, tzinfo=_ET)
WINDOW_END = datetime(2026, 9, 22, 9, 46, 5, tzinfo=_ET)


def _quote(
    *,
    bid: str = "99.98",
    ask: str = "99.99",
    bid_size: int = 3000,
    ask_size: int = 3000,
    received_at: datetime,
    latency_ms: int = 0,
) -> QuoteObservation:
    return QuoteObservation(
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=bid_size,
        ask_size=ask_size,
        quote_ts=received_at - timedelta(milliseconds=latency_ms),
        received_at=received_at,
    )


class TestQualification:
    def test_fresh_two_sided_quote_qualifies(self) -> None:
        q = _quote(received_at=WINDOW_START)
        result = qualify_quote(q)
        assert result.qualifies

    def test_freshness_exactly_1s(self) -> None:
        q = _quote(received_at=WINDOW_START, latency_ms=1000)
        assert qualify_quote(q).qualifies

    def test_freshness_1_001s_fails(self) -> None:
        q = _quote(received_at=WINDOW_START, latency_ms=1001)
        result = qualify_quote(q)
        assert not result.qualifies
        assert result.reason_code == "STALE"

    def test_small_negative_latency_tolerated(self) -> None:
        # REJECTED by review: NO negative-age tolerance.  quote_ts 50 ms
        # AFTER received_at (clock skew) fails closed as STALE.
        q = _quote(received_at=WINDOW_START, latency_ms=-50)
        result = qualify_quote(q)
        assert not result.qualifies
        assert result.reason_code == "STALE"

    def test_large_negative_latency_fails(self) -> None:
        q = _quote(received_at=WINDOW_START, latency_ms=-1000)
        assert not qualify_quote(q).qualifies

    def test_b7_negative_latency_always_fails_closed(self) -> None:
        # REJECTED interpretation: no negative-age tolerance at all.
        # Even 1 µs of quote_ts after received_at (clock skew) fails as
        # STALE — the §10.4 freshness rule is one-sided.
        q = _quote(received_at=WINDOW_START, latency_ms=0)
        q = QuoteObservation(
            bid=q.bid,
            ask=q.ask,
            bid_size=q.bid_size,
            ask_size=q.ask_size,
            quote_ts=WINDOW_START + timedelta(microseconds=1),
            received_at=WINDOW_START,
        )
        result = qualify_quote(q)
        assert not result.qualifies
        assert result.reason_code == "STALE"

    def test_b7_quote_age_across_dst_fold(self) -> None:
        # 2026-11-01 01:30 ET occurs TWICE (EDT fold=0, then EST fold=1).
        # Age must be computed on UTC instants: the fold=0 and fold=1
        # labels of "01:30" are one real hour apart, and only UTC
        # conversion sees that.
        fold0 = datetime(2026, 11, 1, 1, 30, 0, tzinfo=_ET, fold=0)
        fold1 = datetime(2026, 11, 1, 1, 30, 0, tzinfo=_ET, fold=1)
        assert fold1.astimezone(timezone.utc) - fold0.astimezone(timezone.utc) == timedelta(hours=1)

        q_same = QuoteObservation(
            bid=Decimal("100.00"),
            ask=Decimal("100.01"),
            bid_size=100,
            ask_size=100,
            quote_ts=fold0,
            received_at=fold0 + timedelta(seconds=1),
        )
        assert qualify_quote(q_same).qualifies

        # The fold=1 label paired with a fold=0 +1 s received_at is really
        # ~59 minutes of negative age — must fail closed as STALE on the
        # UTC comparison, not look "1 second fresh" on wall clocks.
        q_fold_neg = QuoteObservation(
            bid=Decimal("100.00"),
            ask=Decimal("100.01"),
            bid_size=100,
            ask_size=100,
            quote_ts=fold1,
            received_at=fold0 + timedelta(seconds=1),
        )
        assert not qualify_quote(q_fold_neg).qualifies
        assert qualify_quote(q_fold_neg).reason_code == "STALE"

        # 1 µs negative (fold=1 quote_ts vs fold=1 received_at) → STALE.
        q_neg = QuoteObservation(
            bid=Decimal("100.00"),
            ask=Decimal("100.01"),
            bid_size=100,
            ask_size=100,
            quote_ts=fold1 + timedelta(microseconds=1),
            received_at=fold1,
        )
        assert not qualify_quote(q_neg).qualifies
        assert qualify_quote(q_neg).reason_code == "STALE"

    def test_zero_bid_fails(self) -> None:
        q = _quote(bid="0", received_at=WINDOW_START)
        result = qualify_quote(q)
        assert not result.qualifies
        assert result.reason_code == "ZERO_BID"

    def test_crossed_quote_fails(self) -> None:
        q = _quote(bid="100.00", ask="99.99", received_at=WINDOW_START)
        result = qualify_quote(q)
        assert not result.qualifies
        assert result.reason_code == "CROSSED"

    def test_spread_exactly_5_bps_qualifies(self) -> None:
        # mid 100 → 5 bps = 0.05 spread → bid 99.975 / ask 100.025.
        q = _quote(bid="99.975", ask="100.025", received_at=WINDOW_START)
        result = qualify_quote(q)
        assert result.qualifies
        assert result.spread_bps == Decimal("5.0")

    def test_spread_above_5_bps_fails(self) -> None:
        q = _quote(bid="99.97", ask="100.03", received_at=WINDOW_START)
        result = qualify_quote(q)
        assert not result.qualifies
        assert result.reason_code == "WIDE_SPREAD"


class TestSimulateEntry:
    def _run(
        self, quotes: tuple[QuoteObservation, ...]
    ) -> EntrySimulationResult:
        return simulate_entry(
            quotes,
            target_day=TARGET_DAY,
            inputs_obtained_at=datetime(2026, 9, 22, 9, 45, 59, tzinfo=_ET),
        )

    def test_filled_entry(self) -> None:
        ref = _quote(ask="100.00", received_at=WINDOW_START)
        # L = ceil_to_tick(100.00) = 100.00; q = min(100, 250, 555) = 100.
        fill = _quote(ask="100.00", ask_size=100, received_at=WINDOW_START + timedelta(seconds=1))
        result = self._run((ref, fill))
        assert result.result == "FILLED"
        assert result.limit_price == Decimal("100.00")
        assert result.quantity == 100
        assert result.fill_price == Decimal("100.00")
        assert result.reference_received_at == WINDOW_START
        assert result.fill_received_at == WINDOW_START + timedelta(seconds=1)

    def test_unfilled_when_ask_stays_above_limit(self) -> None:
        # Ask above L but the quote still QUALIFIES (spread ≤ 5 bps):
        # bid 100.05 / ask 100.052 → ~0.2 bps, so only the limit rule can
        # reject it.  L = 100.00 from the reference ask.
        ref = _quote(ask="100.00", received_at=WINDOW_START)
        later = _quote(bid="100.05", ask="100.052", ask_size=100, received_at=WINDOW_START + timedelta(seconds=2))
        result = self._run((ref, later))
        assert result.result == "UNFILLED"
        assert result.fill_price is None

    def test_unfilled_when_fill_window_elapses(self) -> None:
        ref = _quote(ask="100.00", received_at=WINDOW_START)
        late = _quote(ask="100.00", ask_size=100, received_at=WINDOW_START + timedelta(seconds=7))
        result = self._run((ref, late))
        assert result.result == "UNFILLED"

    def test_ask_size_shortfall_is_not_a_fill(self) -> None:
        ref = _quote(ask="100.00", received_at=WINDOW_START)
        thin = _quote(ask="100.00", ask_size=99, received_at=WINDOW_START + timedelta(seconds=1))
        result = self._run((ref, thin))
        assert result.result == "UNFILLED"
        assert result.fill_price is None

    def test_quote_before_wait_1s_ignored_for_fill(self) -> None:
        ref = _quote(ask="100.00", received_at=WINDOW_START)
        early = _quote(ask="100.00", ask_size=100, received_at=WINDOW_START + timedelta(milliseconds=999))
        ok = _quote(ask="100.00", ask_size=100, received_at=WINDOW_START + timedelta(seconds=2))
        result = self._run((ref, early, ok))
        assert result.result == "FILLED"
        assert result.fill_received_at == ok.received_at

    def test_first_qualifying_quote_sets_limit(self) -> None:
        # A stale first quote is skipped; the second (qualifying) sets L.
        stale = _quote(ask="99.00", received_at=WINDOW_START, latency_ms=5000)
        ref = _quote(ask="100.00", received_at=WINDOW_START + timedelta(seconds=2))
        fill = _quote(ask="100.00", ask_size=100, received_at=WINDOW_START + timedelta(seconds=4))
        result = self._run((stale, ref, fill))
        assert result.result == "FILLED"
        assert result.reference_received_at == ref.received_at
        assert result.fill_received_at == fill.received_at

    def test_no_qualifying_quote_no_attempt(self) -> None:
        stale = _quote(ask="99.00", received_at=WINDOW_START, latency_ms=5000)
        result = self._run((stale,))
        assert result.result == "NO_ATTEMPT"

    def test_no_quote_in_window_missed_window(self) -> None:
        early = _quote(ask="100.00", received_at=WINDOW_START - timedelta(seconds=30))
        result = self._run((early,))
        assert result.result == "MISSED_WINDOW"

    def test_quote_outside_window_ignored(self) -> None:
        outside = _quote(ask="100.00", received_at=WINDOW_END + timedelta(seconds=1))
        result = self._run((outside,))
        assert result.result == "MISSED_WINDOW"

    def test_limit_rounded_up_to_tick(self) -> None:
        ref = _quote(ask="100.001", received_at=WINDOW_START)
        fill = _quote(ask="100.00", ask_size=250, received_at=WINDOW_START + timedelta(seconds=1))
        result = self._run((ref, fill))
        assert result.result == "FILLED"
        assert result.limit_price == Decimal("100.01")
        assert result.quantity == position_quantity(Decimal("100.01"))

    def test_fill_at_exactly_wait_deadline_counts(self) -> None:
        # §10.4 lines 532-535: 数量先固定，再等待至少 1 秒 — the wait is
        # “至少” (at least), so a NEW quote received at exactly
        # reference + 1 s (wait_deadline) is INCLUSIVE and fills.
        ref = _quote(ask="100.00", received_at=WINDOW_START)
        exact = _quote(ask="100.00", ask_size=100, received_at=WINDOW_START + timedelta(seconds=1))
        result = self._run((ref, exact))
        assert result.result == "FILLED"
        assert result.fill_received_at == exact.received_at

    def test_fill_window_deadline_inclusive_exactly_6s(self) -> None:
        # 随后 5 秒内 — the confirmation window [wait_deadline,
        # wait_deadline + 5 s] is closed at the right end: a quote at
        # exactly reference + 1 s + 5 s = +6 s still counts...
        ref = _quote(ask="100.00", received_at=WINDOW_START)
        edge = _quote(ask="100.00", ask_size=100, received_at=WINDOW_START + timedelta(seconds=6))
        result = self._run((ref, edge))
        assert result.result == "FILLED"
        assert result.fill_received_at == edge.received_at

    def test_fill_window_deadline_exclusive_beyond_6s(self) -> None:
        # ...but one at +6.000001 s is outside the window and cannot fill.
        ref = _quote(ask="100.00", received_at=WINDOW_START)
        beyond = _quote(
            ask="100.00",
            ask_size=100,
            received_at=WINDOW_START + timedelta(seconds=6, microseconds=1),
        )
        result = self._run((ref, beyond))
        assert result.result == "UNFILLED"
        assert result.unfilled_reason == "FILL_WINDOW_ELAPSED"


class TestEntryTimeGate:
    """B5: the attempt window is frozen to 09:46:00–09:46:05 ET and the
    inputs deadline / entry cutoff are actually enforced."""

    def _run(
        self, quotes, inputs_obtained_at: datetime | None = None
    ) -> EntrySimulationResult:
        return simulate_entry(
            quotes,
            target_day=TARGET_DAY,
            inputs_obtained_at=inputs_obtained_at
            or datetime(2026, 9, 22, 9, 45, 59, tzinfo=_ET),
        )

    def _run_gate(
        self, quotes, inputs_obtained_at: datetime, target_day=TARGET_DAY
    ) -> EntrySimulationResult:
        return simulate_entry(
            quotes,
            target_day=target_day,
            inputs_obtained_at=inputs_obtained_at,
        )

    def test_b5_attempt_window_is_frozen_from_config(self) -> None:
        start, end = attempt_window(TARGET_DAY)
        assert start == WINDOW_START
        assert end == WINDOW_END

    def test_b5_inputs_obtained_at_deadline_inclusive(self) -> None:
        # 09:45:59 sharp is on time (inputs_deadline_et right-inclusive).
        ref = _quote(ask="100.00", received_at=WINDOW_START)
        fill = _quote(ask="100.00", ask_size=100, received_at=WINDOW_START + timedelta(seconds=1))
        result = self._run_gate(
            (ref, fill), inputs_obtained_at=datetime(2026, 9, 22, 9, 45, 59, tzinfo=_ET)
        )
        assert result.result == "FILLED"

    def test_b5_inputs_obtained_after_deadline_rejected(self) -> None:
        # 09:46:00 is late: NO_ATTEMPT even though quotes are in-window.
        ref = _quote(ask="100.00", received_at=WINDOW_START)
        fill = _quote(ask="100.00", ask_size=100, received_at=WINDOW_START + timedelta(seconds=1))
        result = self._run_gate(
            (ref, fill), inputs_obtained_at=datetime(2026, 9, 22, 9, 46, 0, tzinfo=_ET)
        )
        assert result.result == "NO_ATTEMPT"
        assert result.unfilled_reason == "INPUTS_LATE"

    def test_b5_window_is_derived_not_arbitrary(self) -> None:
        # A quote stream living entirely inside a 15:30 window can never
        # fill: the window comes from the config + target_day, so there is
        # no way to pass a different one.
        ref = _quote(ask="100.00", received_at=datetime(2026, 9, 22, 15, 30, 0, tzinfo=_ET))
        fill = _quote(ask="100.00", ask_size=100, received_at=datetime(2026, 9, 22, 15, 30, 1, tzinfo=_ET))
        result = self._run((ref, fill))
        assert result.result in {"MISSED_WINDOW", "NO_ATTEMPT"}
        assert result.result != "FILLED"

    def test_b5_quantity_is_always_package_position_quantity(self) -> None:
        # quantity_fn is gone: q derives only from position_quantity(L)
        # under the frozen caps.  A high price (→ small q) plus a fill
        # quote whose ask_size exactly covers that q must fill with
        # quantity == position_quantity(L) — never an arbitrary number.
        ref = _quote(bid="4999.99", ask="5000.00", received_at=WINDOW_START)
        # L = 5000.00 → q = position_quantity(5000) = 5.
        fill = _quote(
            bid="4999.99", ask="5000.00", ask_size=5,
            received_at=WINDOW_START + timedelta(seconds=1),
        )
        result = self._run((ref, fill))
        assert result.result == "FILLED"
        assert result.quantity == position_quantity(Decimal("5000.00"))
        assert result.quantity == 5

    def test_b5_half_day_entry_cutoff_uses_real_close(self) -> None:
        # 2026-11-27 is a 13:00 half day → entry cutoff 12:15.  The frozen
        # 09:46 window still clears it, so a valid quote pair fills.
        day = date(2026, 11, 27)
        start, _ = attempt_window(day)
        ref = _quote(ask="100.00", received_at=start)
        fill = _quote(
            ask="100.00", ask_size=100, received_at=start + timedelta(seconds=1)
        )
        result = simulate_entry(
            (ref, fill),
            target_day=day,
            inputs_obtained_at=datetime(2026, 11, 27, 9, 45, 59, tzinfo=_ET),
        )
        assert result.result == "FILLED"

    def test_b5_window_passed_cutoff_cannot_attempt(self) -> None:
        # Sanity of the cutoff wiring via the gate on a hypothetical late
        # window: not reachable through simulate_entry (the window is
        # frozen), so prove the gate directly.
        from app.domain.guidance_continuation.quotes import (
            entry_cutoff_instant,
            evaluate_entry_time_gate,
        )

        cutoff = entry_cutoff_instant(TARGET_DAY)
        assert cutoff == datetime(2026, 9, 22, 15, 15, tzinfo=_ET)
        assert (
            evaluate_entry_time_gate(
                target_day=TARGET_DAY,
                inputs_obtained_at=datetime(2026, 9, 22, 9, 45, 59, tzinfo=_ET),
            )
            is None
        )
        assert (
            evaluate_entry_time_gate(
                target_day=TARGET_DAY,
                inputs_obtained_at=datetime(2026, 9, 22, 9, 46, 0, tzinfo=_ET),
            )
            == "INPUTS_LATE"
        )
