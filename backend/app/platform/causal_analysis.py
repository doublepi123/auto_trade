"""P233: Causal Discovery — Granger causality + PCMCI-style lag screening.

Lead-lag / causal analysis for paired return series, the way quant
researchers ask "does lagged-X help predict Y, beyond Y's own history?"
and "what is the conditional lagged cross-correlation once we control for
Y's own past?".

* **granger_causality** — Granger (1969): for each lag ``L = 1..max_lag`` fit
  the restricted AR(L) of ``y`` on its own past (intercept) and the
  unrestricted model adding lagged ``x``; the F-statistic
  ``F = ((RSS_r − RSS_u)/L) / (RSS_u/(n − 2L − 1))`` tests whether the lagged
  ``x`` coefficients are jointly zero. The p-value is computed from the F
  distribution via the regularized incomplete beta function
  ``I_x(a, b)`` implemented here from scratch (Numerical Recipes ``betai``
  continued fraction) — **no scipy**. Returns per-lag F/p and the overall
  minimum p-value across lags.
* **partial_correlation_lag** — Runge et al. (2019) PCMCI-style lag screening:
  for each lag ``L`` the partial correlation between ``x_{t−L}`` and ``y_t``
  controlling on ``y_{t−1..t−L}`` (and the optional ``z``). Solved through
  inversion of the small correlation matrix via Gaussian elimination.
* **lead_lag_summary** — picks the lag with the smallest p-value, marks the
  direction (``x -> y`` vs the symmetric ``y -> x`` by swapping inputs) and
  lists the significant lags.

Deterministic, pure Python, no scipy / statsmodels / numpy. Reference:
Granger (1969) "Investigating Causal Relations by Econometric Models and
Cross-Spectral Methods"; Runge et al. (2019) Nature Communications
"Detecting and quantifying causal associations in time series" (PCMCI);
Press et al. Numerical Recipes (``betai``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "GrangerResult",
    "LeadLagResult",
    "granger_causality",
    "partial_correlation_lag",
    "lead_lag_summary",
    "betai",
    "f_cdf",
    "f_sf",
]


# ---------------------------------------------------------------------------
# math helpers
# ---------------------------------------------------------------------------


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


# ---------------------------------------------------------------------------
# regularized incomplete beta function  I_x(a, b)  (Numerical Recipes betai)
# ---------------------------------------------------------------------------


def _gammaln(x: float) -> float:
    """Lanczos approximation of log Gamma (Numerical Recipes)."""
    c = [
        76.18009172947146,
        -86.50532032941677,
        24.01409824083091,
        -1.231739572450155,
        0.1208650973866179e-2,
        -0.5395239384963e-5,
    ]
    y = x
    tmp = x + 5.5
    tmp -= (x + 0.5) * math.log(tmp)
    ser = 1.000000000190015
    for ci in c:
        y += 1.0
        ser += ci / y
    return -tmp + math.log(2.5066282746310005 * ser / x)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (NR betacf)."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, 201):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-10:
            break
    return h


def betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function ``I_x(a, b)``.

    ``I_x(a, b) = B(x; a, b) / B(a, b)`` where ``B(a, b) = Γ(a)Γ(b)/Γ(a+b)``.
    For ``x <= 0`` returns 0, for ``x >= 1`` returns 1; otherwise uses the
    Numerical Recipes ``betai`` continued-fraction expansion with the
    symmetry ``I_x(a, b) = 1 − I_{1−x}(b, a)`` to keep the fraction well
    conditioned (uses the smaller of ``x`` and ``1−x``).
    """
    if a <= 0.0 or b <= 0.0:
        raise ValueError("a and b must be positive")
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = _gammaln(a) + _gammaln(b) - _gammaln(a + b)
    bt = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def f_cdf(f: float, d1: int, d2: int) -> float:
    """CDF of the F distribution with ``(d1, d2)`` degrees of freedom.

    ``F_{d1,d2}(x) = I_{d1 x / (d1 x + d2)}(d1/2, d2/2)``.
    """
    if d1 <= 0 or d2 <= 0:
        raise ValueError("d1 and d2 must be positive")
    if f <= 0.0:
        return 0.0
    x = (d1 * f) / (d1 * f + d2)
    return betai(d1 / 2.0, d2 / 2.0, x)


def f_sf(f: float, d1: int, d2: int) -> float:
    """Survival function ``1 − F_{d1,d2}(f)`` (the upper tail / p-value)."""
    return 1.0 - f_cdf(f, d1, d2)


# ---------------------------------------------------------------------------
# small linear-algebra (Gaussian elimination)
# ---------------------------------------------------------------------------


def _solve(A: list[list[float]], b: list[float]) -> list[float]:
    """Solve ``A x = b`` via Gaussian elimination with partial pivoting."""
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-14:
            raise ValueError("singular system in OLS solve")
        M[col], M[piv] = M[piv], M[col]
        pivval = M[col][col]
        for r in range(col + 1, n):
            factor = M[r][col] / pivval
            if factor == 0.0:
                continue
            for c in range(col, n + 1):
                M[r][c] -= factor * M[col][c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        s = M[i][n]
        for j in range(i + 1, n):
            s -= M[i][j] * x[j]
        x[i] = s / M[i][i]
    return x


def _inverse(A: list[list[float]]) -> list[list[float]]:
    """Invert a small dense matrix via Gauss-Jordan; raises on singular."""
    n = len(A)
    M = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            raise ValueError("singular matrix")
        M[col], M[piv] = M[piv], M[col]
        pivval = M[col][col]
        for c in range(2 * n):
            M[col][c] /= pivval
        for r in range(n):
            if r == col:
                continue
            factor = M[r][col]
            if factor == 0.0:
                continue
            for c in range(2 * n):
                M[r][c] -= factor * M[col][c]
    return [row[n:] for row in M]


def _ols_rss(X: list[list[float]], y: list[float]) -> float:
    """OLS via normal equations; returns the residual sum of squares.

    ``X`` rows are observations, each row is the regressor vector (intercept
    must already be a column of ones). Solves ``(XᵀX)β = Xᵀy`` and returns
    ``RSS = Σ(y_i − x_i·β)²``.
    """
    n = len(X)
    k = len(X[0]) if n else 0
    XtX = [[0.0] * k for _ in range(k)]
    Xty = [0.0] * k
    for row, yi in zip(X, y):
        for a in range(k):
            Xty[a] += row[a] * yi
            for b_ in range(a, k):
                XtX[a][b_] += row[a] * row[b_]
                XtX[b_][a] = XtX[a][b_]
    try:
        beta = _solve(XtX, Xty)
    except ValueError:
        # singular XtX (collinear regressors) — add a tiny ridge to the
        # diagonal so the projection is well defined; the RSS is unchanged
        # up to numerical precision because the collinear directions carry no
        # independent explanatory power.
        ridge = 1e-10 * (max(XtX[a][a] for a in range(k)) if k else 1.0)
        for a in range(k):
            XtX[a][a] += ridge
        beta = _solve(XtX, Xty)
    rss = 0.0
    for i in range(n):
        pred = 0.0
        row = X[i]
        for j in range(k):
            pred += beta[j] * row[j]
        resid = y[i] - pred
        rss += resid * resid
    return rss


# ---------------------------------------------------------------------------
# Granger causality
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GrangerResult:
    max_lag: int
    n: int  # number of usable observations
    f_stats: dict[int, float]
    p_values: dict[int, float]
    min_p: float
    best_lag: int  # lag with smallest p-value
    significant_lags: list[int]  # lags with p < 0.05

    def to_dict(self) -> dict:
        return {
            "max_lag": self.max_lag,
            "n": self.n,
            "f_stats": self.f_stats,
            "p_values": self.p_values,
            "min_p": self.min_p,
            "best_lag": self.best_lag,
            "significant_lags": self.significant_lags,
        }


def _lag_matrix(series: Sequence[float], L: int) -> list[list[float]]:
    """Build the AR(L) design matrix (intercept + lags 1..L)."""
    rows: list[list[float]] = []
    for t in range(L, len(series)):
        row = [1.0] + [series[t - lag] for lag in range(1, L + 1)]
        rows.append(row)
    return rows


def granger_causality(
    x: Sequence[float],
    y: Sequence[float],
    max_lag: int,
) -> GrangerResult:
    """Granger 1969 causality test for ``x`` causing ``y``.

    For each lag ``L = 1..max_lag`` we estimate two OLS models via normal
    equations::

        restricted:    y_t = α + Σ_{k=1..L} φ_k · y_{t−k} + ε
        unrestricted:  y_t = α + Σ φ_k y_{t−k} + Σ ψ_k x_{t−k} + ε

    with residual sums of squares ``RSS_r`` and ``RSS_u``. The F-statistic

        F = ((RSS_r − RSS_u) / L) / (RSS_u / (n − 2L − 1))

    is distributed ``F(L, n − 2L − 1)`` under the null that lagged ``x``
    carries no predictive information. The p-value is ``1 − F_cdf(F; L,
    n − 2L − 1)`` evaluated via the regularized incomplete beta function
    (see :func:`betai`). A small p-value ⇒ reject the null ⇒ ``x`` Granger-
    causes ``y``.

    Raises ``ValueError`` for empty/mismatched inputs or ``max_lag < 1``.
    """
    n_full = len(x)
    if n_full != len(y):
        raise ValueError("x and y must be equal-length series")
    if max_lag < 1:
        raise ValueError("max_lag must be >= 1")
    if n_full < 2 * max_lag + 2:
        raise ValueError(
            f"need at least 2*max_lag+2 = {2 * max_lag + 2} observations "
            f"for max_lag={max_lag}, got {n_full}"
        )

    f_stats: dict[int, float] = {}
    p_values: dict[int, float] = {}
    n_used: int | None = None
    for L in range(1, max_lag + 1):
        X_r = _lag_matrix(y, L)
        y_r = [y[t] for t in range(L, len(y))]
        rss_r = _ols_rss(X_r, y_r)
        n = len(y_r)
        n_used = n
        X_u: list[list[float]] = []
        for t in range(L, n_full):
            row = [1.0]
            row += [y[t - lag] for lag in range(1, L + 1)]
            row += [x[t - lag] for lag in range(1, L + 1)]
            X_u.append(row)
        rss_u = _ols_rss(X_u, y_r)
        df_num = L
        df_den = n - 2 * L - 1
        if df_den <= 0:
            raise ValueError(
                f"insufficient degrees of freedom for lag {L}; "
                f"need n - 2L - 1 > 0"
            )
        if rss_u <= 0:
            f_stat = 0.0
            p_val = 1.0
        else:
            f_stat = ((rss_r - rss_u) / df_num) / (rss_u / df_den)
            f_stat = max(0.0, f_stat)
            p_val = f_sf(f_stat, df_num, df_den)
        f_stats[L] = f_stat
        p_values[L] = p_val

    min_p = min(p_values.values())
    best_lag = min(p_values, key=lambda k: p_values[k])
    alpha_sig = 0.05
    significant = [k for k in range(1, max_lag + 1) if p_values[k] < alpha_sig]
    return GrangerResult(
        max_lag=max_lag,
        n=n_used or 0,
        f_stats=f_stats,
        p_values=p_values,
        min_p=min_p,
        best_lag=best_lag,
        significant_lags=significant,
    )


# ---------------------------------------------------------------------------
# PCMCI-style partial-correlation lag screening
# ---------------------------------------------------------------------------


def _pearson(a: Sequence[float], b: Sequence[float]) -> float:
    n = len(a)
    if n < 2:
        raise ValueError("need >=2 paired points for correlation")
    ma = sum(a) / n
    mb = sum(b) / n
    sab = sum((ai - ma) * (bi - mb) for ai, bi in zip(a, b))
    saa = sum((ai - ma) ** 2 for ai in a)
    sbb = sum((bi - mb) ** 2 for bi in b)
    if saa <= 0 or sbb <= 0:
        return 0.0
    return sab / math.sqrt(saa * sbb)


def partial_correlation_lag(
    x: Sequence[float],
    y: Sequence[float],
    z: Sequence[float] | None,
    max_lag: int,
) -> dict[int, float]:
    """PCMCI-style partial-correlation lag screening (Runge et al. 2019).

    For each lag ``L = 1..max_lag`` compute the partial Pearson correlation

        ρ(x_{t−L}, y_t | y_{t−1..t−L}, z_t)

    (``z`` optional). We align the rows so that ``t`` runs over the indices
    where ``y_t`` and ``y_{t−1..t−L}`` (and ``x_{t−L}`` and optional ``z_t``)
    are all defined, build the full correlation matrix of the aligned
    variables ``[x_{t−L}, y_t, y_{t−1}, …, y_{t−L}, z_t]``, and extract the
    partial correlation between the first two via the inverse-correlation
    formula: with ``K = C⁻¹``, ``r_{12·rest} = −K_{01} / √(K_{00}·K_{11})``
    (pure-Python Gauss-Jordan inversion). On a singular conditioning matrix
    we fall back to the recursive first-order partial-correlation formula
    ``r_{ij·k} = (r_{ij} − r_{ik} r_{jk}) / √((1−r_{ik}²)(1−r_{jk}²))``
    applied one conditioning variable at a time.

    Returns a dict ``{lag: partial_correlation}`` in ``[−1, 1]``. Raises
    ``ValueError`` on empty/mismatched inputs or ``max_lag < 1``.
    """
    n_full = len(x)
    if n_full != len(y):
        raise ValueError("x and y must be equal-length series")
    if z is not None and len(z) != n_full:
        raise ValueError("z must have the same length as x and y")
    if max_lag < 1:
        raise ValueError("max_lag must be >= 1")
    if n_full < max_lag + 2:
        raise ValueError(
            f"need at least max_lag+2 = {max_lag + 2} observations, got {n_full}"
        )

    out: dict[int, float] = {}
    for L in range(1, max_lag + 1):
        x_lag_col: list[float] = []
        y_cur_col: list[float] = []
        cond_cols: list[list[float]] = []
        for _ in range(L):
            cond_cols.append([])
        if z is not None:
            cond_cols.append([])
        n_cond = len(cond_cols)

        for t in range(L, n_full):
            x_lag_col.append(x[t - L])
            y_cur_col.append(y[t])
            for k in range(L):
                cond_cols[k].append(y[t - 1 - k])
            if z is not None:
                cond_cols[n_cond - 1].append(z[t])

        vars_ = [x_lag_col, y_cur_col] + cond_cols
        m = len(vars_)
        C = [[0.0] * m for _ in range(m)]
        for i in range(m):
            for j in range(i, m):
                c = _pearson(vars_[i], vars_[j])
                C[i][j] = c
                C[j][i] = c
        try:
            K = _inverse(C)
            denom = math.sqrt(K[0][0] * K[1][1])
            if denom <= 0:
                out[L] = 0.0
            else:
                val = -K[0][1] / denom
                out[L] = max(-1.0, min(1.0, val))
        except ValueError:
            out[L] = max(-1.0, min(1.0, _partial_recursive(C, 0, 1, list(range(2, m)))))
    return out


def _partial_recursive(C: list[list[float]], i: int, j: int, cond: list[int]) -> float:
    """Recursive partial correlation via the standard first-order recursion.

    ``r_{ij·k} = (r_{ij} − r_{ik}·r_{jk}) / √((1−r_{ik}²)(1−r_{jk}²))``;
    higher orders peel off one conditioning variable at a time using the
    lower-order partials in place of the raw correlations.
    """
    if not cond:
        return C[i][j]
    k = cond[0]
    rest = cond[1:]
    rij = _partial_recursive(C, i, j, rest)
    rik = _partial_recursive(C, i, k, rest)
    rjk = _partial_recursive(C, j, k, rest)
    denom = math.sqrt((1.0 - rik * rik) * (1.0 - rjk * rjk))
    if denom <= 0:
        return 0.0
    return (rij - rik * rjk) / denom


# ---------------------------------------------------------------------------
# lead-lag summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeadLagResult:
    max_lag: int
    best_lag: int
    best_p: float
    direction: str  # "x->y" or "y->x" or "none"
    significant_lags: list[int]
    forward: GrangerResult  # x -> y
    reverse: GrangerResult  # y -> x

    def to_dict(self) -> dict:
        return {
            "max_lag": self.max_lag,
            "best_lag": self.best_lag,
            "best_p": self.best_p,
            "direction": self.direction,
            "significant_lags": self.significant_lags,
            "forward": self.forward.to_dict(),
            "reverse": self.reverse.to_dict(),
        }


def lead_lag_summary(
    x: Sequence[float],
    y: Sequence[float],
    max_lag: int,
) -> LeadLagResult:
    """Lead-lag summary: best lag + direction + significant lags.

    Runs :func:`granger_causality` both directions (``x -> y`` and ``y -> x``
    by swapping inputs) and picks the side with the smaller minimum p-value.
    ``direction`` is ``"x->y"`` when the forward test is significant (min-p
    below ``alpha=0.05``) and at least as significant as the reverse,
    ``"y->x"`` when the reverse is strictly more significant, otherwise
    ``"none"``. ``best_lag`` is the lag achieving the overall best p-value
    (taken from whichever side wins). ``significant_lags`` is from the winning
    direction.
    """
    fwd = granger_causality(x, y, max_lag)
    rev = granger_causality(y, x, max_lag)
    alpha = 0.05
    if fwd.min_p <= rev.min_p:
        winner = fwd
        direction = "x->y" if winner.min_p < alpha else "none"
    else:
        winner = rev
        direction = "y->x" if winner.min_p < alpha else "none"
    return LeadLagResult(
        max_lag=max_lag,
        best_lag=winner.best_lag,
        best_p=winner.min_p,
        direction=direction,
        significant_lags=winner.significant_lags,
        forward=fwd,
        reverse=rev,
    )