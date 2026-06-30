"""P201: backtest overfitting diagnostics — PBO + Deflated Sharpe Ratio.

Two pure-function diagnostics from the López de Prado backtest-overfitting
literature:

* **Probability of Backtest Overfitting (PBO)** — Bailey, Borwein, López de
  Prado, Zhu (2017). Given a panel of strategy returns split into in-sample
  (IS) and out-of-sample (OOS) halves, PBO is the probability that the strategy
  selected by IS ranking underperforms the median OOS. Combinatorially symmetric
  cross-validation (CSCV) enumerates all IS/OOS splits of the return blocks,
  ranks strategies by IS Sharpe, then measures where the IS-winner lands in the
  OOS rank distribution via a logit transform. PBO > 0.5 indicates the IS
  optimum does not generalize — i.e. the search is overfit.

* **Deflated Sharpe Ratio (DSR)** — Bailey & López de Prado (2014). Adjusts an
  observed Sharpe ratio for the multiple-testing inflation caused by trying N
  strategies, and for non-Normality (skew/kurtosis). Returns the probability
  that the "true" Sharpe exceeds zero after deflation.

Both functions are deterministic (no RNG): PBO uses exact CSCV enumeration over
block subsets; DSR uses the closed-form standard-normal CDF approximation from
the paper. They take simple numeric inputs so they can be layered on top of the
existing :class:`OptimizerService` / walk-forward results without new tables.
"""

from __future__ import annotations

import math
from itertools import combinations
from typing import Sequence

__all__ = ["probability_of_backtest_overfitting", "deflated_sharpe_ratio", "_norm_cdf"]


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function (exact, deterministic)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _sharpe(returns: Sequence[float]) -> float:
    if len(returns) < 2:
        return 0.0
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = var ** 0.5
    if std == 0:
        return 0.0
    return mean / std * (n ** 0.5)


def probability_of_backtest_overfitting(
    returns_panel: list[list[float]],
    block_size: int = 0,
) -> dict[str, float]:
    """Compute PBO via combinatorially symmetric cross-validation (CSCV).

    ``returns_panel`` is a list of per-strategy return series (each a list of
    float returns). All series must be the same length. The series are split
    into ``2 * n_blocks`` equal blocks; CSCV enumerates every way to choose half
    the blocks as IS and the rest as OOS. For each split:

    1. Rank strategies by IS Sharpe; pick the IS-best.
    2. Compute the IS-best's relative rank in the OOS Sharpe distribution (0..1).
    3. Apply the logit transform ``ln(r / (1 - r))``.

    PBO = fraction of splits whose OOS relative rank is below 0.5 (IS winner
    lands in the OOS bottom half). Higher PBO = more overfitting.

    Returns ``{"pbo": float, "logit_mean": float, "n_splits": int}``.
    """
    if not returns_panel:
        return {"pbo": 0.0, "logit_mean": 0.0, "n_splits": 0}
    length = min(len(r) for r in returns_panel)
    if length < 4:
        return {"pbo": 0.0, "logit_mean": 0.0, "n_splits": 0}
    # Trim every series to the common length.
    panel = [list(r[:length]) for r in returns_panel]
    n_strategies = len(panel)

    # Decide block count: 2 * n_blocks blocks total; IS/OOS each get n_blocks.
    if block_size > 0:
        n_blocks = max(1, length // (2 * block_size))
    else:
        # default: 4 blocks (2 IS + 2 OOS) when enough data, else 2 blocks.
        n_blocks = 2 if length >= 4 else 1
    total_blocks = 2 * n_blocks
    # Even block boundaries.
    block_len = length // total_blocks
    if block_len < 1:
        n_blocks = 1
        total_blocks = 2
        block_len = length // 2 or 1

    blocks: list[list[int]] = []
    for b in range(total_blocks):
        start = b * block_len
        end = (b + 1) * block_len if b < total_blocks - 1 else length
        blocks.append(list(range(start, end)))

    logit_values: list[float] = []
    pbo_count = 0
    n_splits = 0
    for is_block_indices in combinations(range(total_blocks), n_blocks):
        is_idx = sorted(i for bi in is_block_indices for i in blocks[bi])
        oos_idx = sorted(
            i for bi in range(total_blocks) if bi not in is_block_indices for i in blocks[bi]
        )
        is_returns = [[panel[s][i] for i in is_idx] for s in range(n_strategies)]
        oos_returns = [[panel[s][i] for i in oos_idx] for s in range(n_strategies)]

        is_sharpes = [_sharpe(r) for r in is_returns]
        oos_sharpes = [_sharpe(r) for r in oos_returns]
        is_best = max(range(n_strategies), key=lambda s: is_sharpes[s])

        # Relative rank of the IS-best in the OOS Sharpe distribution.
        sorted_oos = sorted(oos_sharpes)
        # rank: 1-indexed position; relative_rank in (0, 1].
        rank = 1
        best_oos = oos_sharpes[is_best]
        for v in sorted_oos:
            if v < best_oos:
                rank += 1
        relative_rank = rank / (n_strategies + 1)
        # PBO event: IS-best lands in the OOS bottom half.
        if relative_rank <= 0.5:
            pbo_count += 1
        # Logit transform (guard against 0/1).
        r = min(max(relative_rank, 1e-6), 1 - 1e-6)
        logit_values.append(math.log(r / (1 - r)))
        n_splits += 1

    if n_splits == 0:
        return {"pbo": 0.0, "logit_mean": 0.0, "n_splits": 0}
    pbo = pbo_count / n_splits
    logit_mean = sum(logit_values) / len(logit_values)
    return {"pbo": pbo, "logit_mean": logit_mean, "n_splits": n_splits}


def deflated_sharpe_ratio(
    observed_sharpe: float,
    n_trials: int,
    sample_size: int,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
) -> dict[str, float]:
    """Deflated Sharpe Ratio (Bailey & López de Prado 2014).

    Given an ``observed_sharpe`` from the best of ``n_trials`` strategy trials,
    a return ``sample_size`` (number of observations), and the return series'
    ``skewness``/raw ``kurtosis`` (defaults assume Normality, K=3), compute:

    * the expected maximum Sharpe under the null (zero true Sharpe) across
      ``n_trials`` trials — the multiple-testing benchmark;
    * the variance of the Sharpe estimate adjusted for non-Normality;
    * the Deflated Sharpe — the observed Sharpe re-centered by the expected
      max null and scaled by the adjusted standard deviation;
    * ``psr`` — the Probabilistic Sharpe Ratio: probability the true Sharpe > 0.

    A DSR <= 0 (or PSR <= 0.5) means the observed edge is not statistically
    distinguishable from luck given the number of trials.
    """
    if n_trials < 1 or sample_size < 2:
        return {
            "observed_sharpe": observed_sharpe,
            "expected_max_null_sharpe": 0.0,
            "sharpe_std": 0.0,
            "deflated_sharpe": observed_sharpe,
            "psr": 0.5,
        }
    # Expected max of n_trials standard normals (Bailey/LdP approximation).
    n = n_trials
    expected_max = (1.0 - 0.5 / (1.0 + n)) * _norm_inv_cdf(1.0 - 1.0 / n) if n > 1 else 0.0

    # Variance of annualized Sharpe estimator adjusted for skew/kurtosis.
    # Lo (2002): Var(SR) ~= (1 - skew*SR + (kurt-1)/4 * SR^2) / (T-1)
    sr = observed_sharpe
    var_sr = (1.0 - skewness * sr + (kurtosis - 3.0 + 1.0) / 4.0 * sr ** 2) / (sample_size - 1)
    std_sr = max(var_sr ** 0.5, 1e-9)

    deflated = (sr - expected_max) / std_sr if std_sr > 0 else 0.0
    psr = _norm_cdf((sr - 0.0) * ((sample_size - 1) ** 0.5) / ((1.0 - skewness * sr + (kurtosis - 2.0) / 4.0 * sr ** 2) ** 0.5)) if sample_size > 1 else 0.5

    return {
        "observed_sharpe": observed_sharpe,
        "expected_max_null_sharpe": expected_max,
        "sharpe_std": std_sr,
        "deflated_sharpe": deflated,
        "psr": min(max(psr, 0.0), 1.0),
    }


def _norm_inv_cdf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's algorithm)."""
    if p <= 0.0:
        return -float("inf")
    if p >= 1.0:
        return float("inf")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow = 0.02425
    phigh = 1.0 - plow
    if p < plow:
        q = (-2.0 * math.log(p)) ** 0.5
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    q = (-2.0 * math.log(1.0 - p)) ** 0.5
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
           ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
