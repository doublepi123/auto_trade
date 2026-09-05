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
from typing import Final, Sequence

DSR_CONFIDENCE_LEVEL: Final[float] = 0.95

__all__ = ["probability_of_backtest_overfitting", "deflated_sharpe_ratio", "_norm_cdf"]

# Euler-Mascheroni constant, the weight on the two terms of the expected-maximum
# Sharpe benchmark in Bailey & López de Prado (2014) eq. (2)/(6).
_EULER_MASCHERONI = 0.5772156649015329


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
    trial_sharpe_variance: float | None = None,
) -> dict[str, float | bool]:
    """Deflated Sharpe Ratio (Bailey & López de Prado 2014).

    Bailey, D. H. and López de Prado, M. (2014), "The Deflated Sharpe Ratio:
    Correcting for Selection Bias, Backtest Overfitting, and Non-Normality",
    *Journal of Portfolio Management* 40(5) 94-107.

    Given an ``observed_sharpe`` from the best of ``n_trials`` strategy trials,
    a return ``sample_size`` (number of observations) and the return series'
    ``skewness`` / ``kurtosis``, compute:

    * ``expected_max_null_sharpe`` — the multiple-testing benchmark, i.e. the
      Sharpe the luckiest of ``n_trials`` zero-edge trials would be expected to
      show. Paper eq. (2)/(6)::

          E[max SR] ~= sqrt(V[SR_n]) * ( (1 - g) * Phi^-1(1 - 1/N)
                                         + g * Phi^-1(1 - 1/(N * e)) )

      with ``g`` the Euler-Mascheroni constant, ``Phi^-1`` the standard-normal
      inverse CDF, ``e`` Euler's number and ``V[SR_n]`` the **cross-trial
      variance of the trial Sharpe estimates**. Both terms are required: a
      single ``Phi^-1(1 - 1/N)`` term understates the benchmark by up to ~35%
      at small N, which makes a lucky search winner look significant.
    * ``sharpe_std`` — the standard deviation of the Sharpe estimator adjusted
      for non-Normality.
    * ``deflated_sharpe`` — the observed Sharpe re-centred on the benchmark and
      scaled by ``sharpe_std``: a z-score, NOT a probability (legacy key).
    * ``dsr_probability`` — the DSR probability ``Phi(deflated_sharpe)``.
    * ``psr`` — the Probabilistic Sharpe Ratio: probability the true Sharpe
      exceeds zero. It does **not** see ``n_trials``, so it can never stand in
      for the multiple-testing correction on its own.
    * ``distinguishable_from_luck`` — requires ``dsr_probability >= 0.95``
      (95% level; z >= 1.6448536269514722), retains ``psr > 0.5`` as defence
      in depth, and is never true on an assumed benchmark (see below).

    With fewer than one trial or two observations, both probabilities are the
    neutral 0.5 and certification is False. The legacy ``deflated_sharpe``
    fallback remains the raw observed Sharpe, not a usable z-score.

    **Kurtosis convention (one convention, both paths).** ``kurtosis`` is RAW
    (``g4``; Normal = 3, the default), not excess. The estimator variance is
    the paper's::

        Var(SR) = (1 - g3 * SR + (g4 - 1) / 4 * SR^2) / (T - 1)

    so at ``g3 = 0``, ``g4 = 3``, ``SR = 1`` the bracket is 1.5. Both
    ``deflated_sharpe`` and ``psr`` are built from this one ``sharpe_std``;
    they cannot drift apart.

    **Units contract for ``trial_sharpe_variance`` (``V[SR_n]``).** The
    benchmark is only comparable to ``observed_sharpe`` when both are in the
    same units, and ``V[SR_n]`` is what carries those units. When the caller
    does not supply it this function assumes ``1.0``, reports
    ``trial_variance_assumed=True``, and **fails closed**: for ``n_trials > 1``
    the benchmark is non-zero and unit-unverified, so
    ``distinguishable_from_luck`` is forced ``False``. An assumed benchmark can
    still be inspected — ``deflated_sharpe`` and ``psr`` are returned as
    computed — but it can never certify a swept winner. At ``n_trials == 1``
    the benchmark is exactly zero in every unit system, so nothing is assumed
    and the verdict stands on its own.

    **Effective N.** ``n_trials`` is the *nominal* trial count. A dense sweep
    grid has strongly correlated neighbours, so the effective number of
    independent trials is smaller — often far smaller — than the nominal one
    (paper, Appendix 3). Passing a nominal N is therefore conservative in the
    right direction for the benchmark itself, but no ``N_eff`` shrinkage is
    applied here: an unprincipled correction would be worse than none. Callers
    that can measure the trial Sharpe correlation structure should reduce
    ``n_trials`` themselves and say so.
    """
    if n_trials < 1 or sample_size < 2:
        return {
            "observed_sharpe": observed_sharpe,
            "expected_max_null_sharpe": 0.0,
            "sharpe_std": 0.0,
            "deflated_sharpe": observed_sharpe,
            "dsr_probability": 0.5,
            "psr": 0.5,
            "trial_sharpe_variance": (
                1.0 if trial_sharpe_variance is None else float(trial_sharpe_variance)
            ),
            "trial_variance_assumed": trial_sharpe_variance is None,
            "distinguishable_from_luck": False,
        }

    variance_assumed = trial_sharpe_variance is None
    trial_variance = 1.0 if trial_sharpe_variance is None else float(trial_sharpe_variance)
    if trial_variance < 0.0:
        raise ValueError("trial_sharpe_variance cannot be negative")

    # Expected max Sharpe over n_trials zero-edge trials — Bailey & López de
    # Prado (2014) eq. (2)/(6), both Euler-Mascheroni-weighted terms.
    n = n_trials
    if n > 1:
        bracket = (1.0 - _EULER_MASCHERONI) * _norm_inv_cdf(1.0 - 1.0 / n) + (
            _EULER_MASCHERONI * _norm_inv_cdf(1.0 - 1.0 / (n * math.e))
        )
        expected_max = math.sqrt(trial_variance) * bracket
    else:
        expected_max = 0.0

    # Variance of the Sharpe estimator adjusted for skew/raw kurtosis.
    # Bailey & López de Prado (2014), after Lo (2002):
    #   Var(SR) = (1 - g3*SR + (g4 - 1)/4 * SR^2) / (T - 1)   with RAW g4.
    sr = observed_sharpe
    var_sr = (
        1.0 - skewness * sr + (kurtosis - 1.0) / 4.0 * sr ** 2
    ) / (sample_size - 1)
    std_sr = max(var_sr ** 0.5, 1e-9)

    deflated = (sr - expected_max) / std_sr
    dsr_probability = _norm_cdf(deflated)
    # Same std_sr, so PSR and the deflated Sharpe cannot disagree about the
    # distributional assumptions.
    psr = min(max(_norm_cdf(sr / std_sr), 0.0), 1.0)

    # Fail closed: a non-zero benchmark whose units were assumed rather than
    # measured must not certify anything.
    benchmark_is_unit_bearing = n > 1
    certified = (
        dsr_probability >= DSR_CONFIDENCE_LEVEL
        and psr > 0.5
        and not (variance_assumed and benchmark_is_unit_bearing)
    )

    return {
        "observed_sharpe": observed_sharpe,
        "expected_max_null_sharpe": expected_max,
        "sharpe_std": std_sr,
        "deflated_sharpe": deflated,
        "dsr_probability": dsr_probability,
        "psr": psr,
        "trial_sharpe_variance": trial_variance,
        "trial_variance_assumed": variance_assumed,
        "distinguishable_from_luck": certified,
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
