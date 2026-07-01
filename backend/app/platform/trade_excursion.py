"""P218: Trade MFE/MAE & Holding-Period Analyzer.

For each closed (or still-open) trade, compute the **Max Favorable Excursion**
(MFE — the best price reached in the trade's favor after entry) and the **Max
Adverse Excursion** (MAE — the worst price reached against the trade), plus the
holding period in bars and entry/exit timing ranks. Aggregates per-trade
excursions into percentile summaries (median / p05 / p95 of MFE%, MAE%, holding
bars) plus a global MFE/MAE ratio and expectancy.

Reference: vectorbt ``portfolio.trades`` (MFE/MAE per trade, holding duration);
NautilusTrader ``PositionAnalysis``; pyfolio's trade table. Pure Python over a
``BarEvent`` (or dict) series + pre-paired :class:`TradeExcursionInput` records.
A :func:`trades_from_fills` helper pairs BUY/SELL fills via the same FIFO
deque logic as :mod:`app.platform.analyzers.TradeAnalyzer`, so excursions can
be derived from the existing fill ledger with no new tables.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Sequence

from app.platform.events import BarEvent, FillEvent

__all__ = [
    "TradeExcursionInput",
    "TradeExcursion",
    "ExcursionSummary",
    "analyze_trades",
    "trades_from_fills",
    "ExcursionAnalyzer",
]


# ---------------------------------------------------------------------------
# data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeExcursionInput:
    entry_time: datetime
    exit_time: datetime | None
    side: str
    entry_price: float
    exit_price: float | None = None
    symbol: str | None = None
    quantity: float | None = None
    trade_id: str | None = None


@dataclass(frozen=True)
class TradeExcursion:
    trade_id: str | None
    symbol: str | None
    side: str
    entry_time: datetime
    exit_time: datetime | None
    entry_price: float
    exit_price: float | None
    mfe_price: float
    mae_price: float
    mfe: float
    mae: float
    mfe_pct: float
    mae_pct: float
    realized_pnl_pct: float | None
    holding_bars: int
    entry_bar_index: int | None
    exit_bar_index: int | None
    entry_timing_rank: float | None
    exit_timing_rank: float | None


@dataclass(frozen=True)
class ExcursionSummary:
    num_trades: int
    num_closed: int
    num_open: int
    avg_holding_bars: float
    median_holding_bars: float
    p05_holding_bars: float
    p95_holding_bars: float
    avg_mfe_pct: float
    avg_mae_pct: float
    median_mfe_pct: float
    median_mae_pct: float
    p05_mae_pct: float
    p95_mfe_pct: float
    mfe_mae_ratio: float | None
    avg_realized_pnl_pct: float | None
    expectancy: float | None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _to_dt(x: Any) -> datetime:
    if isinstance(x, datetime):
        return x
    if isinstance(x, date):
        return datetime(x.year, x.month, x.day)
    if isinstance(x, str):
        return datetime.fromisoformat(x)
    raise ValueError(f"cannot coerce {x!r} to datetime")


def _coerce_bars(bars: Sequence[Any]) -> list[BarEvent]:
    out: list[BarEvent] = []
    for b in bars:
        if isinstance(b, BarEvent):
            out.append(b)
        elif isinstance(b, dict):
            out.append(BarEvent.from_dict(b))
    return out


def _coerce_trades(trades: Sequence[Any]) -> list[TradeExcursionInput]:
    out: list[TradeExcursionInput] = []
    for t in trades:
        if isinstance(t, TradeExcursionInput):
            out.append(t)
        elif isinstance(t, dict):
            out.append(TradeExcursionInput(
                entry_time=_to_dt(t["entry_time"]),
                exit_time=_to_dt(t["exit_time"]) if t.get("exit_time") is not None else None,
                side=str(t["side"]),
                entry_price=float(t["entry_price"]),
                exit_price=float(t["exit_price"]) if t.get("exit_price") is not None else None,
                symbol=t.get("symbol"),
                quantity=t.get("quantity"),
                trade_id=t.get("trade_id"),
            ))
    return out


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile (q in [0,1]). Returns 0.0 for empty input."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    rank = q * (len(sorted_vals) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return float(sorted_vals[lo])
    frac = rank - lo
    return float(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)


# ---------------------------------------------------------------------------
# core
# ---------------------------------------------------------------------------


def analyze_trades(
    trades: Sequence[TradeExcursionInput],
    bars: Sequence[BarEvent] | Sequence[dict[str, Any]],
    percentiles: Sequence[float] = (0.05, 0.25, 0.5, 0.75, 0.95),
) -> tuple[list[TradeExcursion], ExcursionSummary]:
    """Compute per-trade MFE/MAE + holding-period and an aggregate summary.

    ``trades`` are pre-paired (entry_time/exit_time/side/entry_price); ``bars``
    are the OHLCV series the trades ran over (sorted internally by timestamp).
    Returns ``(per_trade, summary)``. Deterministic; no RNG.
    """
    trade_list = _coerce_trades(trades)
    bar_list = sorted(_coerce_bars(bars), key=lambda b: b.timestamp)
    ts = [b.timestamp for b in bar_list]
    n = len(bar_list)

    per_trade: list[TradeExcursion] = []
    if not trade_list:
        return per_trade, _empty_summary()

    # entry timing ranks: sort trades by entry_time
    entry_order = sorted(range(len(trade_list)), key=lambda i: trade_list[i].entry_time)
    entry_rank = {i: (r / max(1, len(trade_list) - 1)) for r, i in enumerate(entry_order)}
    exit_times = [
        (i, (trade_list[i].exit_time or bar_list[-1].timestamp if bar_list else trade_list[i].entry_time))
        for i in range(len(trade_list))
    ]
    exit_order = sorted(range(len(trade_list)), key=lambda i: exit_times[i][1])
    exit_rank = {i: (r / max(1, len(trade_list) - 1)) for r, i in enumerate(exit_order)}

    for idx, t in enumerate(trade_list):
        side = t.side.upper()
        direction = 1.0 if side in ("BUY", "LONG") else -1.0
        entry_t = t.entry_time
        exit_t = t.exit_time if t.exit_time is not None else (ts[-1] if ts else entry_t)

        # bisect to find entry/exit bar indices
        entry_i = _bisect_left(ts, entry_t)
        exit_i = _bisect_right(ts, exit_t) - 1
        if exit_i < entry_i:
            exit_i = entry_i
        if entry_i >= n:
            entry_i = n - 1
        if exit_i >= n:
            exit_i = n - 1
        if entry_i < 0:
            entry_i = 0
        if exit_i < 0:
            exit_i = 0

        window = bar_list[entry_i : exit_i + 1] if n > 0 else []
        highs = [float(b.high) for b in window if math.isfinite(float(b.high))]
        lows = [float(b.low) for b in window if math.isfinite(float(b.low))]
        if not highs or not lows:
            mfe_price = t.entry_price
            mae_price = t.entry_price
        elif direction > 0:
            mfe_price = max(highs)
            mae_price = min(lows)
        else:
            mfe_price = min(lows)
            mae_price = max(highs)

        mfe = (mfe_price - t.entry_price) * direction
        mae = (mae_price - t.entry_price) * direction
        mfe_pct = mfe / t.entry_price if t.entry_price != 0 else 0.0
        mae_pct = mae / t.entry_price if t.entry_price != 0 else 0.0
        realized_pnl_pct: float | None = None
        if t.exit_price is not None and t.entry_price != 0:
            realized_pnl_pct = (t.exit_price - t.entry_price) * direction / t.entry_price
        holding = max(0, exit_i - entry_i + 1) if window else 0

        per_trade.append(TradeExcursion(
            trade_id=t.trade_id,
            symbol=t.symbol,
            side=t.side,
            entry_time=t.entry_time,
            exit_time=t.exit_time,
            entry_price=t.entry_price,
            exit_price=t.exit_price,
            mfe_price=mfe_price,
            mae_price=mae_price,
            mfe=mfe,
            mae=mae,
            mfe_pct=mfe_pct,
            mae_pct=mae_pct,
            realized_pnl_pct=realized_pnl_pct,
            holding_bars=holding,
            entry_bar_index=entry_i if window else None,
            exit_bar_index=exit_i if window else None,
            entry_timing_rank=entry_rank.get(idx),
            exit_timing_rank=exit_rank.get(idx),
        ))

    return per_trade, _summarize(per_trade, percentiles)


def _bisect_left(ts: list[datetime], target: datetime) -> int:
    lo, hi = 0, len(ts)
    while lo < hi:
        mid = (lo + hi) // 2
        if ts[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _bisect_right(ts: list[datetime], target: datetime) -> int:
    lo, hi = 0, len(ts)
    while lo < hi:
        mid = (lo + hi) // 2
        if ts[mid] <= target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _empty_summary() -> ExcursionSummary:
    return ExcursionSummary(
        num_trades=0, num_closed=0, num_open=0,
        avg_holding_bars=0.0, median_holding_bars=0.0,
        p05_holding_bars=0.0, p95_holding_bars=0.0,
        avg_mfe_pct=0.0, avg_mae_pct=0.0,
        median_mfe_pct=0.0, median_mae_pct=0.0,
        p05_mae_pct=0.0, p95_mfe_pct=0.0,
        mfe_mae_ratio=None, avg_realized_pnl_pct=None, expectancy=None,
    )


def _summarize(per_trade: list[TradeExcursion], percentiles: Sequence[float]) -> ExcursionSummary:
    if not per_trade:
        return _empty_summary()
    holdings = sorted(float(t.holding_bars) for t in per_trade)
    mfes = sorted(t.mfe_pct for t in per_trade)
    maes = sorted(t.mae_pct for t in per_trade)
    closed = [t for t in per_trade if t.realized_pnl_pct is not None]
    opens = len(per_trade) - len(closed)
    avg_holding = sum(holdings) / len(holdings)
    avg_mfe = sum(mfes) / len(mfes)
    avg_mae = sum(maes) / len(maes)
    med_hold = _percentile(holdings, 0.5)
    med_mfe = _percentile(mfes, 0.5)
    med_mae = _percentile(maes, 0.5)
    p05_hold = _percentile(holdings, 0.05)
    p95_hold = _percentile(holdings, 0.95)
    p05_mae = _percentile(maes, 0.05)
    p95_mfe = _percentile(mfes, 0.95)
    median_mae_abs = abs(med_mae)
    ratio = (med_mfe / median_mae_abs) if median_mae_abs > 1e-12 else None
    realized_pnls = [float(t.realized_pnl_pct) for t in closed if t.realized_pnl_pct is not None]
    avg_pnl = (sum(realized_pnls) / len(realized_pnls)) if realized_pnls else None
    expectancy = avg_pnl  # per-trade expected return %
    return ExcursionSummary(
        num_trades=len(per_trade),
        num_closed=len(closed),
        num_open=opens,
        avg_holding_bars=avg_holding,
        median_holding_bars=med_hold,
        p05_holding_bars=p05_hold,
        p95_holding_bars=p95_hold,
        avg_mfe_pct=avg_mfe,
        avg_mae_pct=avg_mae,
        median_mfe_pct=med_mfe,
        median_mae_pct=med_mae,
        p05_mae_pct=p05_mae,
        p95_mfe_pct=p95_mfe,
        mfe_mae_ratio=ratio,
        avg_realized_pnl_pct=avg_pnl,
        expectancy=expectancy,
    )


def trades_from_fills(fills: Sequence[FillEvent] | Sequence[dict[str, Any]]) -> list[TradeExcursionInput]:
    """FIFO-pair BUY/SELL fills into trades (mirrors TradeAnalyzer pairing)."""
    coerced: list[FillEvent] = []
    for f in fills:
        if isinstance(f, FillEvent):
            coerced.append(f)
        elif isinstance(f, dict):
            coerced.append(FillEvent.from_dict(f))
    lots: dict[str, deque[tuple[int, Decimal, datetime]]] = {}
    trades: list[TradeExcursionInput] = []
    for fill in coerced:
        sym = fill.symbol or ""
        q = lots.setdefault(sym, deque())
        if fill.side == "BUY":
            q.append((fill.quantity, fill.price, fill.timestamp))
        else:  # SELL pairs against the oldest BUY lot
            remaining = fill.quantity
            while remaining > 0 and q:
                lot_qty, lot_price, lot_ts = q[0]
                matched = min(remaining, lot_qty)
                trades.append(TradeExcursionInput(
                    entry_time=lot_ts,
                    exit_time=fill.timestamp,
                    side="BUY",
                    entry_price=float(lot_price),
                    exit_price=float(fill.price),
                    symbol=sym,
                    quantity=float(matched),
                ))
                remaining -= matched
                if matched == lot_qty:
                    q.popleft()
                else:
                    q[0] = (lot_qty - matched, lot_price, lot_ts)
    # any unmatched BUY lots → still-open trades
    for sym, q in lots.items():
        for lot_qty, lot_price, lot_ts in q:
            trades.append(TradeExcursionInput(
                entry_time=lot_ts, exit_time=None, side="BUY",
                entry_price=float(lot_price), exit_price=None, symbol=sym,
                quantity=float(lot_qty),
            ))
    return trades


class ExcursionAnalyzer:
    """Convenience wrapper."""

    def analyze(
        self,
        trades: Sequence[TradeExcursionInput],
        bars: Sequence[BarEvent] | Sequence[dict[str, Any]],
    ) -> tuple[list[TradeExcursion], ExcursionSummary]:
        return analyze_trades(trades, bars)
