"""P267: Backtest diagnostics — expectancy, payoff, streaks, bootstrap CI.

A pure-Python, dependency-free family of trade-level (per-trade PnL) summary
statistics that complement the return-level ratios in
:mod:`app.platform.risk_ratios`. The input ``trades`` is a list of per-trade
profit-and-loss values (the dollar/return outcome of each closed trade):

    trade > 0  -> win
    trade < 0  -> loss
    trade == 0 -> neutral (counts toward ``n_trades`` / expectancy, but is
                  excluded from win/loss tallies and *resets* any active
                  streak without itself contributing to it).

Implemented:

    - ``trade_expectancy(trades)``    mean PnL per trade
    - ``profit_factor(trades)``       gross profit / |gross loss|
    - ``payoff_ratio(trades)``        avg win / |avg loss|
    - ``streaks(trades)``             (max win streak, max loss streak)
    - ``bootstrap_expectancy_ci(...)`` deterministic percentile bootstrap CI
    - ``backtest_diagnostics_report(...)`` all of the above, bundled

Reference: Tharp (2007) "Trade Your Way to Financial Freedom" for expectancy
and payoff ratio definitions. Pure standard library, no external deps.
"""

from __future__ import annotations

import dataclasses
import math
import random
from typing import Any, Sequence

__all__ = [
    "BootstrapCI",
    "BacktestDiagnosticsResult",
    "trade_expectancy",
    "profit_factor",
    "payoff_ratio",
    "streaks",
    "bootstrap_expectancy_ci",
    "backtest_diagnostics_report",
]


# ---------------------------------------------------------------------------
# dataclasses
# ---------------------------------------------------------------------------


def _json_safe_float(value: float) -> float | str:
    """Render a float as a JSON-serializable value.

    FastAPI / ``json.dumps`` (default ``allow_nan=False`` semantics via
    ``jsonable_encoder``) reject or silently null-out non-finite floats
    (``math.inf`` / ``-math.inf`` / ``NaN``). The pure ``profit_factor`` /
    ``payoff_ratio`` helpers legitimately return ``math.inf`` for a no-loss
    trade series, so the *serialization* layer (``to_dict``) is responsible
    for emitting the standard JSON convention strings instead:

      - ``+inf`` -> ``"Infinity"``
      - ``-inf`` -> ``"-Infinity"``
      - ``NaN``  -> ``"NaN"``

    Finite floats pass through unchanged. This keeps the mathematical
    behaviour of the pure functions intact while guaranteeing every dict
    produced by ``to_dict`` round-trips through ``json.dumps``.
    """
    if value == math.inf:
        return "Infinity"
    if value == -math.inf:
        return "-Infinity"
    if math.isnan(value):
        return "NaN"
    return value


@dataclasses.dataclass(frozen=True)
class BootstrapCI:
    """Percentile-bootstrap confidence interval for expectancy.

    ``low`` / ``high`` are the bootstrap percentiles (α/2 and 1−α/2 of the
    resampled mean distribution, with α ≈ 0.05 by convention here). ``seed``
    is ``None`` when the caller did not fix the RNG; ``n_bootstrap`` is the
    number of resamples actually used.
    """

    low: float
    high: float
    seed: int | None
    n_bootstrap: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "low": _json_safe_float(self.low),
            "high": _json_safe_float(self.high),
            "seed": self.seed,
            "n_bootstrap": self.n_bootstrap,
        }


@dataclasses.dataclass(frozen=True)
class BacktestDiagnosticsResult:
    """Full diagnostics bundle returned by :func:`backtest_diagnostics_report`."""

    expectancy: float
    profit_factor: float
    payoff_ratio: float
    win_rate: float
    loss_rate: float
    max_win_streak: int
    max_loss_streak: int
    bootstrap_expectancy_ci: BootstrapCI
    n_trades: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "expectancy": _json_safe_float(self.expectancy),
            "profit_factor": _json_safe_float(self.profit_factor),
            "payoff_ratio": _json_safe_float(self.payoff_ratio),
            "win_rate": _json_safe_float(self.win_rate),
            "loss_rate": _json_safe_float(self.loss_rate),
            "max_win_streak": self.max_win_streak,
            "max_loss_streak": self.max_loss_streak,
            "bootstrap_expectancy_ci": self.bootstrap_expectancy_ci.to_dict(),
            "n_trades": self.n_trades,
        }


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _validate_trades(trades: Any) -> list[float]:
    """Coerce ``trades`` to a non-empty ``list[float]`` of finite numbers.

    Rejects:
      - non-sequence inputs (``TypeError`` from iteration, normalised to
        ``ValueError`` so callers only need to catch one type)
      - empty sequences
      - booleans (``bool`` is a subclass of ``int`` but is semantically
        not a trade PnL — rejected explicitly)
      - strings / dicts / other non-numeric elements
      - non-finite numbers (NaN, ±inf)

    Raises ``ValueError`` on every rejection path for a uniform contract.
    """
    # Reject dicts up front: dict is iterable but iterating yields keys, not
    # PnL values, which would silently produce nonsense statistics.
    if isinstance(trades, (dict, str)):
        raise ValueError("trades must be a sequence of finite numbers")
    try:
        iterator = iter(trades)
    except TypeError as exc:  # pragma: no cover - defensive
        raise ValueError("trades must be a sequence of finite numbers") from exc
    # Sequences are preferred (fast len / indexing) but generators are accepted.
    out: list[float] = []
    for value in iterator:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("trades must be a sequence of finite numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("trades must contain only finite numbers")
        out.append(number)
    if not out:
        raise ValueError("trades must be a non-empty sequence")
    return out


def _validate_n_bootstrap(n_bootstrap: Any) -> int:
    """Validate ``n_bootstrap``: a non-bool int ``>= 1``."""
    if isinstance(n_bootstrap, bool) or not isinstance(n_bootstrap, int):
        raise ValueError("n_bootstrap must be an int >= 1")
    if n_bootstrap < 1:
        raise ValueError("n_bootstrap must be an int >= 1")
    return n_bootstrap


def _validate_seed(seed: Any) -> int | None:
    """Validate ``seed``: ``None`` or a non-bool int."""
    if seed is None:
        return None
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be None or an int")
    return seed


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------


def trade_expectancy(trades: Sequence[float]) -> float:
    """Mean per-trade PnL (Tharp's "expectancy").

    Neutrals (0) are included in the average and the count.
    """
    series = _validate_trades(trades)
    return sum(series) / len(series)


def profit_factor(trades: Sequence[float]) -> float:
    """Gross profit divided by absolute gross loss.

      - No losses *and* some profit  -> ``math.inf`` (perfect profit).
      - No profit *and* no losses    -> ``0.0`` (degenerate, no edge).
      - No profit but some losses    -> ``0.0`` (gross_profit == 0).
    """
    series = _validate_trades(trades)
    gross_profit = sum(t for t in series if t > 0.0)
    gross_loss = sum(-t for t in series if t < 0.0)  # positive magnitude
    if gross_loss <= 0.0:
        return math.inf if gross_profit > 0.0 else 0.0
    return gross_profit / gross_loss


def payoff_ratio(trades: Sequence[float]) -> float:
    """Average win magnitude divided by average loss magnitude.

      - No losses *and* some wins  -> ``math.inf``.
      - No wins (all losses / neutrals) -> ``0.0`` (avg_win == 0).
    """
    series = _validate_trades(trades)
    wins = [t for t in series if t > 0.0]
    losses = [t for t in series if t < 0.0]
    if not losses:
        return math.inf if wins else 0.0
    if not wins:
        return 0.0
    avg_win = sum(wins) / len(wins)
    avg_loss = sum(-t for t in losses) / len(losses)  # positive magnitude
    return avg_win / avg_loss


def streaks(trades: Sequence[float]) -> tuple[int, int]:
    """Maximum consecutive win and loss streaks.

    Neutrals (exactly ``0.0``) break the current streak but are themselves
    neither a win nor a loss, so they do not extend either count. A run of
    neutrals therefore resets the active run to zero length.
    """
    series = _validate_trades(trades)
    max_win = max_loss = 0
    cur_win = cur_loss = 0
    for t in series:
        if t > 0.0:
            cur_win += 1
            cur_loss = 0
        elif t < 0.0:
            cur_loss += 1
            cur_win = 0
        else:  # neutral resets both runs without counting
            cur_win = 0
            cur_loss = 0
        if cur_win > max_win:
            max_win = cur_win
        if cur_loss > max_loss:
            max_loss = cur_loss
    return max_win, max_loss


def bootstrap_expectancy_ci(
    trades: Sequence[float],
    n_bootstrap: int = 1000,
    seed: int | None = None,
) -> BootstrapCI:
    """Percentile-bootstrap confidence interval for expectancy.

    Draws ``n_bootstrap`` resamples of size ``len(trades)`` *with replacement*,
    computes the mean PnL of each, and returns the 2.5 / 97.5 percentile pair
    as the ~95 % CI. Pass a fixed ``seed`` for deterministic, reproducible
    output; ``None`` leaves the RNG unseeded (non-deterministic).
    """
    series = _validate_trades(trades)
    n = len(series)
    n_boot = _validate_n_bootstrap(n_bootstrap)
    fixed_seed = _validate_seed(seed)
    rng = random.Random(fixed_seed)
    means: list[float] = []
    for _ in range(n_boot):
        total = 0.0
        for _ in range(n):
            total += series[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    low = _percentile(means, 2.5)
    high = _percentile(means, 97.5)
    # Guard against the (rare) percentile interpolation producing low > high
    # on degenerate or heavily-tied distributions.
    if low > high:
        low, high = high, low
    return BootstrapCI(low=low, high=high, seed=fixed_seed, n_bootstrap=n_boot)


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile on an already-sorted list.

    ``pct`` is in ``[0, 100]``. Mirrors numpy's default ``linear`` method so
    that ``pct=2.5`` / ``pct=97.5`` produce the conventional 95 % bounds even
    on small ``n_bootstrap`` values.
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_values[lo]
    frac = rank - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


# ---------------------------------------------------------------------------
# all-in-one report
# ---------------------------------------------------------------------------


def backtest_diagnostics_report(
    trades: Sequence[float],
    n_bootstrap: int = 1000,
    seed: int | None = None,
) -> BacktestDiagnosticsResult:
    """Compute the full diagnostics bundle for a per-trade PnL series."""
    series = _validate_trades(trades)
    wins = [t for t in series if t > 0.0]
    losses = [t for t in series if t < 0.0]
    decisive = len(wins) + len(losses)
    win_rate = len(wins) / decisive if decisive > 0 else 0.0
    loss_rate = len(losses) / decisive if decisive > 0 else 0.0
    max_win, max_loss = streaks(series)
    ci = bootstrap_expectancy_ci(series, n_bootstrap=n_bootstrap, seed=seed)
    return BacktestDiagnosticsResult(
        expectancy=trade_expectancy(series),
        profit_factor=profit_factor(series),
        payoff_ratio=payoff_ratio(series),
        win_rate=win_rate,
        loss_rate=loss_rate,
        max_win_streak=max_win,
        max_loss_streak=max_loss,
        bootstrap_expectancy_ci=ci,
        n_trades=len(series),
    )
