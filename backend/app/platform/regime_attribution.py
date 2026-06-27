"""P326: Regime attribution — decompose returns by market regime.

Slice a return series by regime labels and compute per-regime alpha, beta,
contribution (mean × proportion), and supporting statistics. Designed for
multi-regime backtest decomposition: "how much did each regime contribute
to total return?"

Reference: Grinold & Kahn Ch.17 "Performance Attribution"; Anson (2004)
"Regime-switching portfolio analysis". Pure Python, no new deps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RegimeAttributionEntry:
    regime: str
    n_observations: int
    proportion: float
    mean_return: float
    alpha: float
    beta: float | None
    contribution: float
    volatility: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime": self.regime,
            "n_observations": self.n_observations,
            "proportion": self.proportion,
            "mean_return": self.mean_return,
            "alpha": self.alpha,
            "beta": self.beta,
            "contribution": self.contribution,
            "volatility": self.volatility,
        }


@dataclass(frozen=True)
class RegimeAttributionResult:
    regimes: list[RegimeAttributionEntry]
    n_observations: int
    total_return: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "regimes": [r.to_dict() for r in self.regimes],
            "n_observations": self.n_observations,
            "total_return": self.total_return,
        }


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _cov(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = _mean(xs)
    my = _mean(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (n - 1)


def regime_attribution_report(
    returns: list[float],
    regimes: list[str],
    benchmark: list[float] | None = None,
) -> RegimeAttributionResult:
    """Decompose returns by regime.

    Args:
        returns: Period returns list.
        regimes: Regime label per period (same length as returns).
        benchmark: Optional benchmark returns for alpha/beta computation.

    Returns:
        RegimeAttributionResult with per-regime entries.

    Raises:
        ValueError: Length mismatch, empty inputs, or non-finite values.
    """
    if not returns:
        raise ValueError("returns must be non-empty")
    if len(returns) != len(regimes):
        raise ValueError(
            f"returns length {len(returns)} != regimes length {len(regimes)}"
        )
    if benchmark is not None and len(benchmark) != len(returns):
        raise ValueError(
            f"benchmark length {len(benchmark)} != returns length {len(returns)}"
        )

    for v in returns:
        if not math.isfinite(float(v)):
            raise ValueError("returns must contain only finite numbers")

    total_n = len(returns)
    total_return = sum(float(r) for r in returns)

    # Group by regime
    groups: dict[str, list[tuple[float, float | None]]] = {}
    for i, r in enumerate(returns):
        regime = str(regimes[i])
        bench_val = float(benchmark[i]) if benchmark is not None else None
        groups.setdefault(regime, []).append((float(r), bench_val))

    entries: list[RegimeAttributionEntry] = []
    for regime in sorted(groups.keys()):
        pairs = groups[regime]
        n = len(pairs)
        prop = n / total_n
        regime_returns = [p[0] for p in pairs]
        mr = _mean(regime_returns)
        vol = _std(regime_returns)

        # Alpha
        if benchmark is not None:
            regime_bench = [p[1] for p in pairs if p[1] is not None]
            bench_mean = _mean(regime_bench)
            alpha = mr - bench_mean
        else:
            alpha = mr

        # Beta (regime returns vs benchmark returns)
        beta: float | None = None
        if benchmark is not None and len(regime_returns) >= 2:
            regime_bench = [p[1] for p in pairs if p[1] is not None]
            if len(regime_bench) >= 2:
                bench_var = _std(regime_bench) ** 2
                if bench_var > 0:
                    beta = _cov(regime_returns, regime_bench) / bench_var
                else:
                    beta = 0.0

        # Contribution = mean × proportion
        contribution = mr * prop

        entries.append(RegimeAttributionEntry(
            regime=regime,
            n_observations=n,
            proportion=prop,
            mean_return=mr,
            alpha=alpha,
            beta=beta,
            contribution=contribution,
            volatility=vol,
        ))

    return RegimeAttributionResult(
        regimes=entries,
        n_observations=total_n,
        total_return=total_return,
    )


__all__ = [
    "RegimeAttributionEntry",
    "RegimeAttributionResult",
    "regime_attribution_report",
]
