"""P204: Advanced drawdown analytics.

Beyond the simple "max drawdown" the existing ``analyzers.DrawDownAnalyzer``
already returns, this module computes the *shape* of the drawdown: how long it
lasted, how long it took to recover, when the worst underwater period hit,
and the rolling Calmar ratio at multiple windows.

Reference: Magdon-Ismail, Atiya, "Maximum Drawdown" (2004); Chekhlov,
Uryasev & Young, "Drawdown measure in portfolio optimization" (2005).
Pure-Python, dict-based I/O, deterministic — the platform's existing
``analyzers.DrawDownAnalyzer`` is the single-tick max-DD source of truth and
we re-derive from the same equity curve to keep both views consistent.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

__all__ = [
    "drawdown_events",
    "drawdown_summary",
    "rolling_calmar",
    "underwater_curve",
    "drawdown_acceleration",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _running_peak(equity: Sequence[float]) -> list[float]:
    """Cumulative running maximum of ``equity`` (length-equivalent)."""
    if not equity:
        return []
    out = [equity[0]]
    for x in equity[1:]:
        out.append(max(out[-1], x))
    return out


def _drawdown_series(equity: Sequence[float]) -> list[float]:
    """Per-tick drawdown: (equity - peak) / peak. Negative or zero."""
    if not equity:
        return []
    peak = _running_peak(equity)
    return [(equity[i] - peak[i]) / peak[i] if peak[i] > 0 else 0.0 for i in range(len(equity))]


# ---------------------------------------------------------------------------
# Drawdown events
# ---------------------------------------------------------------------------


def _drawdown_events(equity: Sequence[float]) -> list[dict[str, Any]]:
    """Detect drawdown episodes: each returns ``{start, trough, end, depth, duration, recovery_time}``.

    A drawdown begins when the equity falls below the prior running peak, hits
    its lowest point at the trough, and ends when equity first equals/exceeds
    the prior peak. ``duration`` is the trough-index minus start-index;
    ``recovery_time`` is the end-index minus trough-index (None if the equity
    never recovered by the end of the series).
    """
    if not equity:
        return []
    n = len(equity)
    peak = _running_peak(equity)
    events: list[dict[str, Any]] = []
    in_dd = False
    start = 0
    trough = 0
    for i in range(n):
        is_underwater = equity[i] < peak[i - 1] if i > 0 else False
        if not in_dd and is_underwater:
            in_dd = True
            # The drawdown *starts* at the tick of the prior peak (peak[i-1]).
            start = i - 1
            trough = i
        elif in_dd:
            if equity[i] < equity[trough]:
                trough = i
            if equity[i] >= peak[start]:
                depth = (equity[trough] - peak[start]) / peak[start] if peak[start] > 0 else 0.0
                events.append({
                    "start": start,
                    "trough": trough,
                    "end": i,
                    "depth": depth,  # negative number (e.g. -0.25)
                    "duration": trough - start,
                    "recovery_time": i - trough,
                })
                in_dd = False
    if in_dd:
        depth = (equity[trough] - peak[start]) / peak[start] if peak[start] > 0 else 0.0
        events.append({
            "start": start,
            "trough": trough,
            "end": None,
            "depth": depth,
            "duration": trough - start,
            "recovery_time": None,
        })
    return events


def drawdown_events(equity: Sequence[float]) -> list[dict[str, Any]]:
    """Public alias: list of drawdown episodes with depth/duration/recovery_time."""
    return _drawdown_events(equity)


def underwater_curve(equity: Sequence[float]) -> list[float]:
    """Per-tick underwater depth: (equity − peak) / peak. Negative or zero."""
    return _drawdown_series(equity)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def drawdown_summary(equity: Sequence[float]) -> dict[str, Any]:
    """Aggregate drawdown statistics over the whole curve.

    Returns: ``max_drawdown`` (≤0), ``max_drawdown_duration`` (longest single
    episode trough-to-trough), ``longest_recovery_time`` (trough-to-recovery
    of the worst episode), ``n_episodes`` (count of completed+open drawdowns),
    ``avg_drawdown`` (mean episode depth, completed only), and
    ``time_underwater_pct`` (fraction of ticks spent below the prior peak).
    """
    if not equity:
        return {
            "max_drawdown": 0.0,
            "max_drawdown_duration": 0,
            "longest_recovery_time": 0,
            "n_episodes": 0,
            "avg_drawdown": 0.0,
            "time_underwater_pct": 0.0,
        }
    dd = _drawdown_series(equity)
    events = _drawdown_events(equity)
    max_dd = min(dd) if dd else 0.0
    max_duration = max((e["duration"] for e in events), default=0)
    completed = [e for e in events if e["recovery_time"] is not None]
    longest_recovery = max((e["recovery_time"] for e in completed), default=0)
    n_episodes = len(events)
    completed_depths = [e["depth"] for e in completed if e["depth"] < 0]
    avg_dd = sum(completed_depths) / len(completed_depths) if completed_depths else 0.0
    ticks_underwater = sum(1 for x in dd if x < 0)
    time_underwater_pct = ticks_underwater / len(dd) if dd else 0.0
    return {
        "max_drawdown": max_dd,
        "max_drawdown_duration": max_duration,
        "longest_recovery_time": longest_recovery,
        "n_episodes": n_episodes,
        "avg_drawdown": avg_dd,
        "time_underwater_pct": time_underwater_pct,
    }


# ---------------------------------------------------------------------------
# Rolling Calmar
# ---------------------------------------------------------------------------


def _annualized_return(equity_window: Sequence[float]) -> float:
    """Window total return (unused for rolling Calmar, kept for callers)."""
    if len(equity_window) < 2 or equity_window[0] <= 0:
        return 0.0
    return (equity_window[-1] / equity_window[0]) - 1.0


def rolling_calmar(equity: Sequence[float], window: int) -> list[float]:
    """Rolling Calmar ratio (annualized return / |max drawdown|) over ``window`` ticks.

    Returns a series the same length as ``equity`` with ``0.0`` for tick positions
    where the window isn't yet full. The window return is annualized *geometrically*
    — ``(1 + r) ^ (252 / window) − 1`` — so large window returns no longer produce
    the absurd linear-scaled values the previous ``r · 252/window`` produced.
    ``252`` is the daily-bar annualization factor; for other frequencies pass a
    ``window`` consistent with that frequency (52 for weekly, 12 for monthly, etc.).
    """
    if window < 1 or not equity:
        return []
    annualization_factor = 252.0 / window
    out: list[float] = []
    for i in range(len(equity)):
        if i + 1 < window:
            out.append(0.0)
            continue
        seg = equity[i + 1 - window : i + 1]
        if seg[0] <= 0:
            out.append(0.0)
            continue
        total_ret = (seg[-1] / seg[0]) - 1.0
        # geometric annualization; (1+r)^a - 1, with a = 252/window
        ann_ret = (1.0 + total_ret) ** annualization_factor - 1.0
        dd = min(_drawdown_series(seg)) if seg else 0.0
        if dd >= 0:
            out.append(0.0)
        else:
            out.append(ann_ret / abs(dd))
    return out


# ---------------------------------------------------------------------------
# Drawdown acceleration
# ---------------------------------------------------------------------------


def drawdown_acceleration(equity: Sequence[float]) -> list[float]:
    """Second derivative of the underwater curve.

    Positive values mean the drawdown is *accelerating* (getting worse faster);
    negative values mean recovery acceleration. Zeros where the curve is flat.
    Useful as a regime indicator in dashboards: sustained positive
    acceleration is a "free-fall" signal.
    """
    uw = _drawdown_series(equity)
    if len(uw) < 3:
        return [0.0] * len(uw)
    out = [0.0, 0.0]
    for i in range(2, len(uw)):
        out.append(uw[i] - 2.0 * uw[i - 1] + uw[i - 2])
    return out
