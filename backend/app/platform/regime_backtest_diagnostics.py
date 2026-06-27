"""P324: Regime Backtest Diagnostics — per-regime Sharpe, win_rate, trade PnL.

Splits a return series by regime label and computes per-regime diagnostics
(Sharpe ratio annualised ×√252, win rate, mean, std) plus optional
per-regime trade-outcome attribution.

Pure Python, no scipy/numpy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


__all__ = ["RegimeBacktestDiagnosticsResult", "regime_backtest_diagnostics_report"]


@dataclass(frozen=True)
class RegimeBacktestDiagnosticsResult:
    """Frozen result of :func:`regime_backtest_diagnostics_report`.

    * ``diagnostics`` — ``{regime: {sharpe, win_rate, mean, std,
      trade_count?, avg_pnl?}}``.
    """

    diagnostics: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"diagnostics": self.diagnostics}


def _validate_returns_regimes(
    returns: list[float], regimes: list[str]
) -> None:
    """Validate returns and regimes series."""
    if not isinstance(returns, list) or not isinstance(regimes, list):
        raise ValueError("returns and regimes must be lists")
    if len(returns) != len(regimes):
        raise ValueError("returns and regimes must have equal length")
    if len(returns) == 0:
        raise ValueError("returns and regimes must be non-empty")
    for i, r in enumerate(returns):
        if not math.isfinite(r):
            raise ValueError(f"returns[{i}] must be finite")
    for i, r in enumerate(regimes):
        if not isinstance(r, str) or not r:
            raise ValueError(f"regimes[{i}] must be a non-empty string")


def _sharpe(rs: list[float], periods_per_year: int = 252) -> float:
    """Annualised Sharpe ratio from a list of period returns."""
    n = len(rs)
    if n < 2:
        return 0.0
    mean_r = sum(rs) / n
    if mean_r == 0.0:
        return 0.0
    variance = sum((r - mean_r) ** 2 for r in rs) / (n - 1)
    if variance <= 0.0:
        return 0.0
    std_r = math.sqrt(variance)
    return mean_r / std_r * math.sqrt(periods_per_year)


def _win_rate(rs: list[float]) -> float:
    """Fraction of positive returns."""
    if not rs:
        return 0.0
    return sum(1 for r in rs if r > 0) / len(rs)


def regime_backtest_diagnostics_report(
    returns: list[float],
    regimes: list[str],
    trade_outcomes: list[tuple[int, float]] | None = None,
) -> RegimeBacktestDiagnosticsResult:
    """Compute per-regime backtest diagnostics.

    Parameters
    ----------
    returns : list[float]
        Period returns.
    regimes : list[str]
        Regime label for each period (same length as returns).
    trade_outcomes : list[float] | None
        Optional per-trade PnL values (not per-period).

    Returns
    -------
    RegimeBacktestDiagnosticsResult
    """
    _validate_returns_regimes(returns, regimes)

    # Group returns by regime
    regime_returns: dict[str, list[float]] = {}
    for r, regime in zip(returns, regimes):
        regime_returns.setdefault(regime, []).append(r)

    diagnostics: dict[str, dict[str, Any]] = {}
    for regime_name, rs in sorted(regime_returns.items()):
        n = len(rs)
        mean_r = sum(rs) / n if n > 0 else 0.0
        if n > 1:
            variance = sum((r - mean_r) ** 2 for r in rs) / (n - 1)
            std_r = math.sqrt(max(variance, 0.0))
        else:
            std_r = 0.0
        sharpe_val = _sharpe(rs)
        wr = _win_rate(rs)
        diagnostics[regime_name] = {
            "sharpe": sharpe_val,
            "win_rate": wr,
            "mean": mean_r,
            "std": std_r,
        }

    # Per-regime trade outcomes (if provided as (period_index, pnl) pairs)
    if trade_outcomes is not None:
        regime_trades: dict[str, list[float]] = {}
        for pair in trade_outcomes:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError("trade_outcomes entries must be (period_index, pnl) pairs")
            period_idx, pnl = pair[0], pair[1]
            if isinstance(period_idx, bool) or not isinstance(period_idx, int) or period_idx < 0 or period_idx >= len(regimes):
                raise ValueError("trade_outcomes period_index out of range")
            if isinstance(pnl, bool) or not isinstance(pnl, (int, float)) or not math.isfinite(float(pnl)):
                raise ValueError("trade_outcomes pnl must be finite numbers")
            regime = regimes[period_idx]
            regime_trades.setdefault(regime, []).append(float(pnl))

        for regime_name in diagnostics:
            trades = regime_trades.get(regime_name, [])
            if trades:
                diagnostics[regime_name]["trade_count"] = len(trades)
                diagnostics[regime_name]["avg_pnl"] = sum(trades) / len(trades)
            else:
                diagnostics[regime_name]["trade_count"] = 0
                diagnostics[regime_name]["avg_pnl"] = 0.0

    return RegimeBacktestDiagnosticsResult(diagnostics=diagnostics)
