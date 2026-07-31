"""Window-local underwater-run duration service.

Measures fully observed recovery durations in number of closed trades while
separating the current open run and a possible left-censored window-boundary
run. The pre-window equity high-water mark is not available, so this does not
claim to be a complete historical drawdown record. Read-only.

Inspired by QuantStats' underwater duration analysis and VectorBT's
drawdown analytics.
"""
from __future__ import annotations

from collections import Counter
import math
from statistics import median, quantiles
from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
)

__all__ = ["DrawdownDurationService"]

_EVIDENCE_SCOPE = "WINDOW_LOCAL_UNDERWATER_RUNS"
_MEDIAN_METHOD = "statistics.median"
_QUANTILE_METHOD = "statistics.quantiles(n=4, method='inclusive')"
_SCOPE_NOTE = (
    "Durations are window-local underwater runs measured from a zeroed "
    "window-local cumulative-PnL baseline. The pre-window equity high-water "
    "mark is unavailable, so this is not a complete historical recovery "
    "record."
)


class DrawdownDurationService:
    """Fully observed window-local recovery-duration distribution."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self, symbol: str | None = None, lookback_days: int = 365
    ) -> dict[str, Any]:
        sample = load_analytics_trade_sample(
            self._db,
            symbol=symbol,
            lookback_days=lookback_days,
            include_excursions=False,
        )
        pnls = [trade.net_pnl for trade in sample.trades]
        base_payload = {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": len(pnls),
            "evidence_scope": _EVIDENCE_SCOPE,
            "pre_window_high_water_known": False,
            "duration_unit": "closed_trades",
            "scope_note": _SCOPE_NOTE,
        }
        mixed_error = mixed_currency_error(
            sample,
            payload=base_payload,
        )
        if mixed_error is not None:
            return mixed_error
        if len(pnls) < 10:
            return analytics_response(
                sample,
                {
                    **base_payload,
                    "error": "Need at least 10 closed trades.",
                },
            )

        # This curve starts from a window-local zero baseline. A run beginning
        # on the first observation is left-censored because its true start may
        # precede the requested window. Only runs whose local start and
        # recovery are both observed enter the recovery-duration distribution.
        cum = 0.0
        peak = 0.0
        completed_durations: list[int] = []
        current_open_duration = 0
        current_run_left_censored = False
        left_censored = False
        excluded_left_censored_duration: int | None = None
        observed_underwater_trade_count = 0

        for index, p in enumerate(pnls):
            cum += p
            recovered = cum > peak or math.isclose(
                cum,
                peak,
                rel_tol=1e-12,
                abs_tol=1e-9,
            )
            if recovered:
                if current_open_duration > 0:
                    if current_run_left_censored:
                        excluded_left_censored_duration = (
                            current_open_duration
                        )
                    else:
                        completed_durations.append(
                            current_open_duration
                        )
                    current_open_duration = 0
                    current_run_left_censored = False
                peak = max(peak, cum)
            else:
                observed_underwater_trade_count += 1
                if current_open_duration == 0:
                    current_run_left_censored = index == 0
                    if current_run_left_censored:
                        left_censored = True
                current_open_duration += 1

        is_underwater = current_open_duration > 0
        if is_underwater and current_run_left_censored:
            excluded_left_censored_duration = current_open_duration

        durations_sorted = sorted(completed_durations)
        completed_episodes = len(durations_sorted)

        hist = Counter(durations_sorted)
        histogram = [
            {"duration": k, "count": v}
            for k, v in sorted(hist.items())
        ]

        payload: dict[str, Any] = {
            **base_payload,
            # ``episodes`` remains as a compatibility alias, but now has the
            # precise meaning of fully observed completed episodes only.
            "episodes": completed_episodes,
            "completed_episodes": completed_episodes,
            "durations": durations_sorted[-20:],
            "histogram": histogram,
            "summary": _duration_summary(durations_sorted),
            "median_method": _MEDIAN_METHOD,
            "quantile_method": _QUANTILE_METHOD,
            "current_open_duration": (
                current_open_duration if is_underwater else 0
            ),
            "is_underwater": is_underwater,
            "left_censored": left_censored,
            "excluded_left_censored_duration": (
                excluded_left_censored_duration
            ),
            "observed_underwater_trade_count": (
                observed_underwater_trade_count
            ),
            "pct_time_underwater": round(
                observed_underwater_trade_count / len(pnls) * 100,
                1,
            ),
        }
        note = _result_note(
            completed_episodes=completed_episodes,
            is_underwater=is_underwater,
            current_run_left_censored=current_run_left_censored,
            left_censored=left_censored,
            observed_underwater_trade_count=(
                observed_underwater_trade_count
            ),
        )
        if note is not None:
            payload["note"] = note
        return analytics_response(
            sample,
            payload,
        )


def _duration_summary(durations: list[int]) -> dict[str, float | int | None]:
    if not durations:
        return {
            "avg": None,
            "max": None,
            "median": None,
            "p25": None,
            "p75": None,
        }

    if len(durations) == 1:
        p25 = p75 = float(durations[0])
    else:
        p25, _p50, p75 = quantiles(
            durations,
            n=4,
            method="inclusive",
        )
    return {
        "avg": round(sum(durations) / len(durations), 1),
        "max": max(durations),
        "median": round(float(median(durations)), 1),
        "p25": round(float(p25), 1),
        "p75": round(float(p75), 1),
    }


def _result_note(
    *,
    completed_episodes: int,
    is_underwater: bool,
    current_run_left_censored: bool,
    left_censored: bool,
    observed_underwater_trade_count: int,
) -> str | None:
    if observed_underwater_trade_count == 0:
        return "No window-local underwater runs detected."
    if completed_episodes == 0 and is_underwater:
        if current_run_left_censored:
            return (
                "The boundary run is left-censored and remains open; no "
                "fully observed recovery duration is available."
            )
        return (
            "The current window-local run remains open and is excluded from "
            "recovery-duration statistics."
        )
    if completed_episodes == 0 and left_censored:
        return (
            "Only a left-censored boundary run was observed; it is excluded "
            "from recovery-duration statistics."
        )
    if is_underwater:
        return (
            "The current open run is right-censored and excluded from "
            "recovery-duration statistics."
        )
    if left_censored:
        return (
            "A left-censored boundary run was excluded from recovery-duration "
            "statistics."
        )
    return None
