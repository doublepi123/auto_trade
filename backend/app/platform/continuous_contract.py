"""P195: continuous contract rollover + adjustment factor.

Synthesizes a single continuous price series from a chain of individual futures
contracts by stitching together front-month segments at rollover dates and
applying an adjustment method so that price jumps at expiry do not create
spurious returns. Mirrors the continuous-contract construction in Nautilus
Trader (rollover) and QuantConnect Lean (``ContinuousContract`` /
``ContractDepthOffset``) and the panama / ratio-adjustment methods used by most
CTA platforms.

Three adjustment methods are supported (Lean-compatible):

* ``RATIO``     — multiply all prior history by the ratio old/new at each roll.
                  Preserves percentage returns (the CTA default).
* ``BACKWARD``  — add the (old - new) price gap to prior history (panama).
                  Preserves absolute levels near the recent end.
* ``NONE``      — raw stitch with no adjustment (price gap intact; for
                  diagnostics / spread analysis only).

The implementation always adjusts *history* and leaves the most recent leg
unadjusted (panama-style), walking backwards from the active contract. This is
the convention used by most CTA platforms for backtesting.

The module is pure-function over ``BarEvent`` lists — no I/O, no state, fully
deterministic. Symbols are tagged so a caller can pass a flat bar list plus a
``rolls`` schedule; the constructor variant :class:`ContinuousContractBuilder`
keeps a streaming-friendly API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from app.platform.events import BarEvent, EventSource


class AdjustMethod(Enum):
    RATIO = "ratio"
    BACKWARD = "back"
    NONE = "none"


@dataclass(frozen=True)
class Roll:
    """A rollover point: at ``timestamp`` we switch from ``from_symbol`` to ``to_symbol``."""

    timestamp: Any  # datetime (kept loose to allow tz-naive/aware interop)
    from_symbol: str
    to_symbol: str


def _bar(timestamp, symbol, close, o=None, h=None, l=None, vol=0) -> BarEvent:  # type: ignore[no-untyped-def]
    o = close if o is None else o
    h = close if h is None else h
    l = close if l is None else l
    return BarEvent(
        timestamp=timestamp,
        source=EventSource.MARKET,
        symbol=symbol,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(close)),
        volume=int(vol),
    )


def build_continuous(
    bars_by_symbol: dict[str, list[BarEvent]],
    rolls: list[Roll],
    method: AdjustMethod = AdjustMethod.RATIO,
    continuous_symbol: str = "CONT",
) -> list[BarEvent]:
    """Stitch a continuous series from per-contract bar lists + a roll schedule.

    ``bars_by_symbol`` maps each individual contract symbol to its time-sorted
    bar list. ``rolls`` lists the timestamps at which the active contract
    switches; the first contract before the first roll is the seed. The
    resulting bars are relabeled with ``continuous_symbol`` and adjusted so that
    price gaps at each roll are removed according to ``method``.

    Adjustment is applied cumulatively from the most recent segment backwards
    (so the current/active leg is unadjusted and history is shifted to match).
    """
    if not bars_by_symbol:
        return []

    # Build the active-symbol timeline from rolls:
    #   (window_start, window_end_exclusive, symbol).
    sorted_rolls = sorted(rolls, key=lambda r: _aware(r.timestamp))
    windows: list[tuple[object, object, str]] = []
    if sorted_rolls:
        # Before the first roll: from_symbol is active.
        windows.append((None, sorted_rolls[0].timestamp, sorted_rolls[0].from_symbol))
        for i, roll in enumerate(sorted_rolls):
            end = sorted_rolls[i + 1].timestamp if i + 1 < len(sorted_rolls) else None
            windows.append((roll.timestamp, end, roll.to_symbol))
    else:
        only_symbol = next(iter(bars_by_symbol))
        windows.append((None, None, only_symbol))

    # Assign every bar to the window whose symbol is active at its timestamp.
    segments: list[tuple[str, list[BarEvent]]] = []
    for start_ts, end_ts, sym in windows:
        raw = bars_by_symbol.get(sym, [])
        seg = [b for b in raw if _in_window(b.timestamp, start_ts, end_ts)]
        segments.append((sym, sorted(seg, key=lambda b: b.timestamp)))

    if not segments:
        return []

    # Walk from the most recent segment backwards, applying cumulative
    # adjustment to each older leg so the current leg stays unadjusted.
    adjusted: list[BarEvent] = list(segments[-1][1])

    for i in range(len(segments) - 2, -1, -1):
        seg = segments[i][1]
        next_seg = segments[i + 1][1]
        if not seg or not next_seg:
            adjusted = list(seg) + adjusted
            continue
        old_close = seg[-1].close  # last bar of outgoing contract
        new_open = next_seg[0].open  # first bar of incoming contract
        factor = _adjustment_factor(method, old_close, new_open)
        offset = _adjustment_offset(method, old_close, new_open)
        adjusted_seg = [_apply_adjustment(b, continuous_symbol, factor, offset) for b in seg]
        adjusted = adjusted_seg + adjusted

    # Relabel every bar (adjusted or not) to the continuous symbol.
    result = [_relabel(b, continuous_symbol) for b in adjusted]
    return sorted(result, key=lambda b: b.timestamp)


def _in_window(ts, start, end) -> bool:  # type: ignore[no-untyped-def]
    ts_n = _aware(ts)
    if start is not None and ts_n < _aware(start):
        return False
    if end is not None and ts_n >= _aware(end):
        return False
    return True


def _aware(ts) -> datetime:  # type: ignore[no-untyped-def]
    """Coerce a naive datetime to UTC so aware/naive timestamps compare safely."""
    if isinstance(ts, datetime) and ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _adjustment_factor(method: AdjustMethod, old_close: Decimal, new_open: Decimal) -> Decimal:
    if method == AdjustMethod.RATIO and new_open != 0:
        return old_close / new_open
    return Decimal("1")


def _adjustment_offset(method: AdjustMethod, old_close: Decimal, new_open: Decimal) -> Decimal:
    if method == AdjustMethod.BACKWARD:
        return old_close - new_open
    return Decimal("0")


def _apply_adjustment(
    bar: BarEvent,
    symbol: str,
    factor: Decimal,
    offset: Decimal,
) -> BarEvent:
    """Apply ratio (multiply) and/or additive offset to a bar's OHLC."""
    if factor == 1 and offset == 0:
        return _relabel(bar, symbol)
    new_open = bar.open * factor + offset
    new_high = bar.high * factor + offset
    new_low = bar.low * factor + offset
    new_close = bar.close * factor + offset
    return BarEvent(
        timestamp=bar.timestamp,
        source=bar.source,
        symbol=symbol,
        open=new_open,
        high=new_high,
        low=new_low,
        close=new_close,
        volume=bar.volume,
    )


def _relabel(bar: BarEvent, symbol: str) -> BarEvent:
    if bar.symbol == symbol:
        return bar
    return BarEvent(
        timestamp=bar.timestamp,
        source=bar.source,
        symbol=symbol,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
    )


class ContinuousContractBuilder:
    """Streaming-friendly builder wrapping :func:`build_continuous`.

    Allows incremental construction: push per-contract bars and rolls, then call
    :meth:`build` with the desired adjustment method. The stateless core
    function remains the single source of truth.
    """

    def __init__(self, continuous_symbol: str = "CONT") -> None:
        self.continuous_symbol = continuous_symbol
        self._bars: dict[str, list[BarEvent]] = {}
        self._rolls: list[Roll] = []

    def add_contract(self, symbol: str, bars: list[BarEvent]) -> "ContinuousContractBuilder":
        merged = sorted(self._bars.get(symbol, []) + list(bars), key=lambda b: b.timestamp)
        self._bars[symbol] = merged
        return self

    def add_roll(self, roll: Roll) -> "ContinuousContractBuilder":
        self._rolls.append(roll)
        return self

    def build(self, method: AdjustMethod = AdjustMethod.RATIO) -> list[BarEvent]:
        return build_continuous(
            self._bars, self._rolls, method=method, continuous_symbol=self.continuous_symbol
        )
