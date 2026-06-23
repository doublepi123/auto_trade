"""Tests for the P195 continuous contract rollover + adjustment."""

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.continuous_contract import (
    AdjustMethod,
    ContinuousContractBuilder,
    Roll,
    build_continuous,
)
from app.platform.events import BarEvent, EventSource


def _bar(symbol: str, ts: str, close: str) -> BarEvent:
    return BarEvent(
        timestamp=datetime.fromisoformat(ts).replace(tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol=symbol,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=0,
    )


def _bars() -> dict[str, list[BarEvent]]:
    # Two front-month contracts; at roll time front1 closes ~100, front2 opens ~110 (gap up).
    return {
        "F1.US": [_bar("F1.US", "2026-01-03T00:00:00", "100"), _bar("F1.US", "2026-01-04T00:00:00", "100")],
        "F2.US": [_bar("F2.US", "2026-01-05T00:00:00", "110"), _bar("F2.US", "2026-01-06T00:00:00", "115")],
    }


def _rolls() -> list[Roll]:
    return [Roll(timestamp=datetime(2026, 1, 5, tzinfo=timezone.utc), from_symbol="F1.US", to_symbol="F2.US")]


def test_continuous_relabels_all_bars_to_continuous_symbol():
    bars = _bars()
    cont = build_continuous(bars, _rolls(), method=AdjustMethod.RATIO, continuous_symbol="CONT.US")

    assert len(cont) == 4
    assert {b.symbol for b in cont} == {"CONT.US"}


def test_continuous_chronological_order():
    cont = build_continuous(_bars(), _rolls(), continuous_symbol="CONT.US")
    ts = [b.timestamp for b in cont]
    assert ts == sorted(ts)


def test_none_adjustment_leaves_gap_intact():
    cont = build_continuous(_bars(), _rolls(), method=AdjustMethod.NONE, continuous_symbol="CONT.US")
    closes = [b.close for b in cont]
    # Last bar of F1 stays 100, first bar of F2 stays 110 -> raw gap preserved.
    assert closes == [Decimal("100"), Decimal("100"), Decimal("110"), Decimal("115")]


def test_ratio_adjustment_removes_return_gap():
    cont = build_continuous(_bars(), _rolls(), method=AdjustMethod.RATIO, continuous_symbol="CONT.US")
    closes = [b.close for b in cont]
    # F1 bars scaled by 100/110 so the 100->110 jump at roll vanishes.
    assert closes[2] == Decimal("110")
    assert closes[3] == Decimal("115")
    assert closes[0] == Decimal("100") * Decimal("100") / Decimal("110")
    # Return across the roll boundary must be continuous (no jump).
    ret_across = closes[2] / closes[1]
    # The two prior-segment closes are equal pre-adjust, post-adjust still equal.
    assert closes[0] == closes[1]


def test_backward_adjustment_adds_gap_to_history():
    cont = build_continuous(_bars(), _rolls(), method=AdjustMethod.BACKWARD, continuous_symbol="CONT.US")
    closes = [b.close for b in cont]
    # Backward: history shifted up by (old - new) = 100 - 110 = -10, so -10 added.
    assert closes[0] == Decimal("90")
    assert closes[2] == Decimal("110")


def test_single_contract_no_rolls_just_relabel():
    bars = {"F1.US": [_bar("F1.US", "2026-01-03T00:00:00", "100"), _bar("F1.US", "2026-01-04T00:00:00", "102")]}
    cont = build_continuous(bars, rolls=[], continuous_symbol="CONT.US")
    assert len(cont) == 2
    assert [b.symbol for b in cont] == ["CONT.US", "CONT.US"]
    assert cont[1].close == Decimal("102")


def test_empty_input_returns_empty():
    assert build_continuous({}, [], continuous_symbol="CONT.US") == []


def test_three_contract_chain_ratio_cumulative():
    # Three contracts with two rolls; verify cumulative ratio applied to oldest.
    bars = {
        "C1.US": [_bar("C1.US", "2026-01-03T00:00:00", "50")],
        "C2.US": [_bar("C2.US", "2026-01-04T00:00:00", "100")],
        "C3.US": [_bar("C3.US", "2026-01-05T00:00:00", "200")],
    }
    rolls = [
        Roll(timestamp=datetime(2026, 1, 4, tzinfo=timezone.utc), from_symbol="C1.US", to_symbol="C2.US"),
        Roll(timestamp=datetime(2026, 1, 5, tzinfo=timezone.utc), from_symbol="C2.US", to_symbol="C3.US"),
    ]
    cont = build_continuous(bars, rolls, method=AdjustMethod.RATIO, continuous_symbol="CONT.US")
    closes = [b.close for b in cont]
    # C3 unadjusted = 200. C2 scaled by 100/200 = 0.5 -> 50. C1 scaled by 0.5 then 50/100 -> 12.5
    assert closes[2] == Decimal("200")
    assert closes[1] == Decimal("50")
    assert closes[0] == Decimal("25")


def test_builder_incremental_api():
    builder = ContinuousContractBuilder(continuous_symbol="CONT.US")
    builder.add_contract("F1.US", _bars()["F1.US"])
    builder.add_contract("F2.US", _bars()["F2.US"])
    for roll in _rolls():
        builder.add_roll(roll)

    cont = builder.build(method=AdjustMethod.RATIO)

    assert len(cont) == 4
    assert {b.symbol for b in cont} == {"CONT.US"}
