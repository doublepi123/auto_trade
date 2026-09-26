"""Precise entry conditions over the 15 opening bars (PREREGISTRATION §10.4)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.domain.guidance_continuation.entry import (
    EntryBar,
    evaluate_entry,
    expected_bar_starts,
)
from app.domain.guidance_continuation.universe import expected_history_days

_ET = ZoneInfo("America/New_York")

TARGET_DAY = date(2026, 9, 22)
PPREV = Decimal("100")


def _ts(minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 22, 9, 30 + minute, second, tzinfo=_ET)


def _bar(
    minute: int,
    *,
    o: str = "101.5",
    h: str = "102",
    l: str = "101",
    c: str = "101.8",
    v: int = 200_000,
    ts: datetime | None = None,
) -> EntryBar:
    return EntryBar(
        ts=ts or _ts(minute),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(l),
        close=Decimal(c),
        volume=v,
    )


def _bars15() -> tuple[EntryBar, ...]:
    return tuple(_bar(i) for i in range(15))


def _history_pairs(
    n: int = 20, value: int = 100_000
) -> tuple[tuple[date, int], ...]:
    """(trading day, V15) pairs for the exact expected 20-day set."""
    days = expected_history_days(TARGET_DAY)
    return tuple((d, value) for d in days[:n])


def _history_zero_pairs() -> tuple[tuple[date, int], ...]:
    return tuple((d, 0) for d in expected_history_days(TARGET_DAY))


class TestExpectedBars:
    def test_expected_starts_are_0930_to_0944(self) -> None:
        starts = expected_bar_starts(TARGET_DAY)
        assert len(starts) == 15
        assert starts[0] == datetime(2026, 9, 22, 9, 30, tzinfo=_ET)
        assert starts[-1] == datetime(2026, 9, 22, 9, 44, tzinfo=_ET)


class TestGeometry:
    def test_all_four_conditions_met(self) -> None:
        # O=101.5, C15=101.8, VWAP ≈ typical prices, RVOL = 3M/100k = 30.
        bars = _bars15()
        result = evaluate_entry(
            bars=bars, pprev=PPREV, history_v15=_history_pairs(), target_day=TARGET_DAY
        )
        assert result.decision == "ENTRY"
        assert result.o == Decimal("101.5")
        assert result.c15 == Decimal("101.8")
        assert result.v15 == 15 * 200_000
        assert result.conditions is not None and result.conditions.rvol_ok

    def test_missing_bar_blocks_entry(self) -> None:
        bars = tuple(_bar(i) for i in range(14))  # 09:44 bar missing
        result = evaluate_entry(
            bars=bars, pprev=PPREV, history_v15=_history_pairs(), target_day=TARGET_DAY
        )
        assert result.decision == "NO_ENTRY"
        assert result.reason_code == "BAR_COUNT"

    def test_duplicate_bar_blocks_entry(self) -> None:
        bars = tuple(_bar(i) for i in range(14)) + (_bar(0),)
        result = evaluate_entry(
            bars=bars, pprev=PPREV, history_v15=_history_pairs(), target_day=TARGET_DAY
        )
        assert result.decision == "NO_ENTRY"
        assert result.reason_code == "DUPLICATE_BAR"

    def test_out_of_window_bar_blocks_entry(self) -> None:
        bars = tuple(_bar(i) for i in range(14)) + (
            _bar(14, ts=datetime(2026, 9, 22, 9, 45, tzinfo=_ET)),
        )
        result = evaluate_entry(
            bars=bars, pprev=PPREV, history_v15=_history_pairs(), target_day=TARGET_DAY
        )
        assert result.decision == "NO_ENTRY"
        assert result.reason_code in {"BAR_OUT_OF_WINDOW", "BAR_COUNT"}

    def test_zero_volume_bar_blocks_entry(self) -> None:
        bars = tuple(_bar(i) if i != 7 else _bar(i, v=0) for i in range(15))
        result = evaluate_entry(
            bars=bars, pprev=PPREV, history_v15=_history_pairs(), target_day=TARGET_DAY
        )
        assert result.decision == "NO_ENTRY"
        assert result.reason_code == "BAD_VOLUME"

    def test_incomplete_history_blocks_entry(self) -> None:
        bars = tuple(_bar(i) for i in range(15))
        result = evaluate_entry(
            bars=bars, pprev=PPREV, history_v15=_history_pairs(19), target_day=TARGET_DAY
        )
        assert result.decision == "NO_ENTRY"
        assert result.reason_code == "RC_HISTORY_INCOMPLETE"

    def test_non_positive_median_blocks_entry(self) -> None:
        # B4: every (day, v15) value must be > 0, so an all-zero history
        # is rejected as BAD_HISTORY_VALUES before the median is even
        # computed (strictly stronger than the old NON_POSITIVE_MEDIAN).
        bars = tuple(_bar(i) for i in range(15))
        result = evaluate_entry(
            bars=bars,
            pprev=PPREV,
            history_v15=_history_zero_pairs(),
            target_day=TARGET_DAY,
        )
        assert result.decision == "NO_ENTRY"
        assert result.reason_code == "BAD_HISTORY_VALUES"

    def test_b4_v15_history_negative_value_blocks_entry(self) -> None:
        # Every (day, v15) pair must be finite and > 0; a single negative
        # v15 fails even when the median stays positive.
        pairs = list(_history_pairs())
        pairs[3] = (pairs[3][0], -5)
        result = evaluate_entry(
            bars=tuple(_bar(i) for i in range(15)),
            pprev=PPREV,
            history_v15=tuple(pairs),
            target_day=TARGET_DAY,
        )
        assert result.decision == "NO_ENTRY"
        assert result.reason_code in {
            "BAD_HISTORY_VALUES",
            "RC_HISTORY_INCOMPLETE",
        }

    def test_b4_v15_history_wrong_day_set_blocks_entry(self) -> None:
        # 20 pairs but not the expected 20-day set → incomplete history.
        pairs = list(_history_pairs())
        pairs[0] = (pairs[0][0] + timedelta(days=14), 100_000)
        result = evaluate_entry(
            bars=tuple(_bar(i) for i in range(15)),
            pprev=PPREV,
            history_v15=tuple(pairs),
            target_day=TARGET_DAY,
        )
        assert result.decision == "NO_ENTRY"
        assert result.reason_code in {
            "BAD_HISTORY_VALUES",
            "RC_HISTORY_INCOMPLETE",
        }

    def test_b4_impossible_ohlc_blocks_entry(self) -> None:
        # high=1, low=2, close=101.8 is not a possible bar.
        bars = list(_bar(i) for i in range(15))
        bars[0] = EntryBar(
            ts=bars[0].ts,
            open=Decimal("101.5"),
            high=Decimal("1"),
            low=Decimal("2"),
            close=Decimal("101.8"),
            volume=200_000,
        )
        result = evaluate_entry(
            bars=tuple(bars),
            pprev=PPREV,
            history_v15=_history_pairs(),
            target_day=TARGET_DAY,
        )
        assert result.decision == "NO_ENTRY"
        assert result.reason_code == "BAD_PRICE"


class TestConditions:
    def _eval(
        self,
        bars: tuple[EntryBar, ...],
        pprev: Decimal,
        history: tuple[tuple[date, int], ...],
    ):
        return evaluate_entry(
            bars=bars, pprev=pprev, history_v15=history, target_day=TARGET_DAY
        )

    def test_gap_exactly_at_0_01_passes(self) -> None:
        # O = 101.0 exactly over Pprev=100.
        bars = tuple(_bar(i, o="101") for i in range(15))
        result = self._eval(bars, Decimal("100"), _history_pairs())
        cond = result.conditions
        assert cond is not None
        assert cond.gap_in_band

    def test_gap_just_below_0_01_fails(self) -> None:
        # OHLC-consistent bars around O=100.99 (low must be ≤ open).
        bars = tuple(
            _bar(i, o="100.99", h="101.05", l="100.9", c="101.0")
            for i in range(15)
        )
        result = self._eval(bars, Decimal("100"), _history_pairs())
        assert result.decision == "NO_ENTRY"
        assert result.reason_code == "GAP_BELOW_MIN"

    def test_gap_exactly_at_0_05_passes(self) -> None:
        bars = tuple(
            _bar(i, o="105", h="105.5", l="104.5", c="105.2")
            for i in range(15)
        )
        result = self._eval(bars, Decimal("100"), _history_pairs())
        cond = result.conditions
        assert cond is not None
        assert cond.gap_in_band

    def test_gap_just_above_0_05_fails(self) -> None:
        bars = tuple(
            _bar(i, o="105.01", h="105.5", l="104.5", c="105.2")
            for i in range(15)
        )
        result = self._eval(bars, Decimal("100"), _history_pairs())
        assert result.decision == "NO_ENTRY"
        assert result.reason_code == "GAP_ABOVE_MAX"

    def test_c15_over_o_exactly_at_0_002_passes(self) -> None:
        # O=100 → C15 must be 100.2 exactly.
        bars = tuple(
            _bar(i, o="100", h="100.3", l="99.9", c="100.2") for i in range(15)
        )
        bars = (bars[0],) + tuple(
            _bar(i, o="100", h="100.3", l="99.9", c="100.2") for i in range(1, 15)
        )
        result = self._eval(bars, Decimal("98.5"), _history_pairs())
        cond = result.conditions
        assert cond is not None
        assert cond.c15_gain_ok

    def test_c15_over_o_just_below_0_002_fails(self) -> None:
        bars = tuple(
            _bar(i, o="100", h="100.3", l="99.9", c="100.19") for i in range(15)
        )
        result = self._eval(bars, Decimal("98.5"), _history_pairs())
        assert result.decision == "NO_ENTRY"
        assert result.reason_code == "C15_GAIN"

    def test_c15_below_vwap_fails(self) -> None:
        # Early tall bars lift VWAP above the final close while O=100,
        # C15=100.3 over Pprev=99.5 keeps gap and C15/O - 1 = 0.003 in band.
        bars = tuple(
            _bar(
                i,
                o="100",
                h="104" if i < 5 else "100.5",
                l="99.8",
                c="103" if i < 5 else "100.3",
            )
            for i in range(15)
        )
        result = self._eval(bars, Decimal("99"), _history_pairs())
        assert result.vwap15 is not None and result.c15 is not None
        assert result.c15 < result.vwap15
        assert result.decision == "NO_ENTRY"
        assert result.reason_code == "C15_BELOW_VWAP"

    def test_rvol_exactly_at_2_passes(self) -> None:
        # V15 = 15×100k = 1.5M; median history = 750k → RVOL = 2 exactly.
        bars = tuple(_bar(i, v=100_000) for i in range(15))
        result = self._eval(
            bars,
            Decimal("100"),
            tuple((d, 750_000) for d in expected_history_days(TARGET_DAY)),
        )
        cond = result.conditions
        assert cond is not None
        assert cond.rvol_ok
        assert result.rvol15 == Decimal("2")

    def test_rvol_just_below_2_fails(self) -> None:
        bars = tuple(_bar(i, v=100_000) for i in range(15))
        result = self._eval(
            bars,
            Decimal("100"),
            tuple((d, 750_001) for d in expected_history_days(TARGET_DAY)),
        )
        assert result.decision == "NO_ENTRY"
        assert result.reason_code == "RVOL_BELOW_MIN"

    def test_vwap15_is_typical_price_weighted(self) -> None:
        # 15 identical bars → VWAP15 = (H+L+C)/3 exactly.
        bars = tuple(
            _bar(i, o="100", h="110", l="90", c="105") for i in range(15)
        )
        expected = (Decimal(110) + Decimal(90) + Decimal(105)) / Decimal(3)
        result = self._eval(bars, Decimal("100"), _history_pairs())
        assert result.vwap15 is not None
        assert abs(result.vwap15 - expected) < Decimal("1e-20")

    def test_non_positive_pprev_fails(self) -> None:
        bars = tuple(_bar(i) for i in range(15))
        result = self._eval(bars, Decimal("0"), _history_pairs())
        assert result.decision == "NO_ENTRY"
        assert result.reason_code == "NON_POSITIVE_PPREV"
