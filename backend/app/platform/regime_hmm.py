"""P234: Hidden Markov Model regime detector (Hamilton 1989).

The classic Bull/Bear/Sideways HMM of returns: a Gaussian-emission hidden
Markov model trained with deterministic Baum-Welch (forward-backward E-step +
closed-form Gaussian moment M-step, transition-count updates) and decoded
with the Viterbi algorithm in log-space.

The hidden state is the unobserved market regime; the observed emission is
the daily return, modelled as ``N(μ_s, σ_s²)`` per state ``s``. Given a return
series we estimate the parameter set ``λ = (π, A, μ, σ)`` and recover the most
likely state path; the per-state means then drive the regime labels
(highest mean = BULL, lowest = BEAR, middle = SIDEWAYS for ``n_states=3``;
positive mean = BULL else BEAR for ``n_states=2``).

* **fit_hmm** — Baum-Welch EM with the scaling-factor forward/backward trick
  (Rabiner 1989 §III.B) to avoid underflow on long series. Deterministic
  quantile-based initialization (no RNG). Fixed iteration cap ``n_iter`` with
  early stop on log-likelihood improvement ``< tol``.
* **viterbi** — most likely state path via the Viterbi recursion in log-space
  (Rabiner 1989 §III.C).
* **regime_label** — map per-state means to ``BULL``/``BEAR``/``SIDWAYS``.

Reference: Hamilton (1989) "A New Approach to the Economic Analysis of
Nonstationary Time Series", Rabiner (1989) "A Tutorial on HMM and Selected
Applications in Speech Recognition", Nautilus ``MarketRegimeModel``.
Pure Python, no scipy / no hmmlearn.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

__all__ = [
    "HMMParams",
    "HmmFitResult",
    "fit_hmm",
    "viterbi",
    "state_means",
    "state_stds",
    "regime_label",
    "forward_probs",
    "backward_probs",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_NEG_INF = -1e300


def _gaussian_log_pdf(x: float, mean: float, std: float) -> float:
    """Log of the Gaussian density ``N(x; μ, σ²)``."""
    if std <= 0:
        # degenerate: point mass at mean
        return 0.0 if x == mean else _NEG_INF
    var = std * std
    return -0.5 * math.log(2.0 * math.pi * var) - 0.5 * (x - mean) ** 2 / var


def _validate_returns(returns: Sequence[float]) -> list[float]:
    if returns is None:
        raise ValueError("returns must be non-empty")
    n = len(returns)
    if n < 2:
        raise ValueError(f"need >=2 returns, got {n}")
    out = [float(r) for r in returns]
    if any(math.isnan(v) or math.isinf(v) for v in out):
        raise ValueError("returns must be finite")
    return out


# ---------------------------------------------------------------------------
# dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HMMParams:
    """Hidden Markov model parameter set ``λ = (π, A, μ, σ)``.

    ``init_probs`` is the length-``n_states`` starting distribution ``π``;
    ``trans_matrix`` is the ``n_states × n_states`` row-stochastic transition
    matrix ``A`` with ``A[i][j] = P(s_{t+1}=j | s_t=i)``; ``means`` and ``stds``
    are the per-state Gaussian emission parameters.
    """

    n_states: int
    init_probs: list[float]
    trans_matrix: list[list[float]]
    means: list[float]
    stds: list[float]

    def to_dict(self) -> dict:
        return {
            "n_states": self.n_states,
            "init_probs": list(self.init_probs),
            "trans_matrix": [list(row) for row in self.trans_matrix],
            "means": list(self.means),
            "stds": list(self.stds),
        }


@dataclass(frozen=True)
class HmmFitResult:
    """Baum-Welch fit result."""

    params: HMMParams
    log_likelihood: float
    converged: bool
    n_iter_run: int
    regime_labels: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "params": self.params.to_dict(),
            "log_likelihood": self.log_likelihood,
            "converged": self.converged,
            "n_iter_run": self.n_iter_run,
            "regime_labels": list(self.regime_labels),
        }


# ---------------------------------------------------------------------------
# deterministic initialization
# ---------------------------------------------------------------------------


def _quantile_init(returns: list[float], n_states: int) -> HMMParams:
    """Deterministic quantile-bin initialization.

    Sort the returns, slice into ``n_states`` equal-frequency quantile bins,
    use each bin's mean as the initial state mean and the bin's (population)
    standard deviation as the initial state std; ``π`` uniform; ``A`` uniform
    with a diagonal boost so each state favours staying put.
    """
    n = len(returns)
    sorted_r = sorted(returns)
    # equal-frequency bin edges: bin i covers indices [i*n/n_states, (i+1)*n/n_states)
    means: list[float] = []
    stds: list[float] = []
    for i in range(n_states):
        lo = (i * n) // n_states
        hi = ((i + 1) * n) // n_states
        if hi <= lo:
            hi = lo + 1
        chunk = sorted_r[lo:hi]
        m = sum(chunk) / len(chunk)
        var = sum((v - m) ** 2 for v in chunk) / len(chunk)
        s = math.sqrt(var) if var > 0 else 1e-6
        # guard against zero std (identical values in a bin)
        if s < 1e-9:
            s = 1e-6
        means.append(m)
        stds.append(s)
    init_probs = [1.0 / n_states] * n_states
    # uniform + diagonal boost, then renormalize rows
    base = 1.0 / n_states
    diag_boost = 0.5
    trans_matrix: list[list[float]] = []
    for i in range(n_states):
        row = []
        for j in range(n_states):
            row.append(base + (diag_boost if i == j else 0.0))
        row_sum = sum(row)
        row = [v / row_sum for v in row]
        trans_matrix.append(row)
    return HMMParams(
        n_states=n_states,
        init_probs=init_probs,
        trans_matrix=trans_matrix,
        means=means,
        stds=stds,
    )


# ---------------------------------------------------------------------------
# forward / backward with scaling (Rabiner 1989)
# ---------------------------------------------------------------------------


def forward_probs(returns: Sequence[float], params: HMMParams) -> tuple[list[list[float]], list[float]]:
    """Scaled forward probabilities (alpha) and per-step scale factors (c).

    ``α_t(j) = P(o_1, …, o_t, s_t = j | λ)`` rescaled by ``c_t`` so values stay
    in representable range (Rabiner 1989 §III.B eqs. 84–90). Returns the scaled
    ``α`` matrix ``T × N`` and the scale-factor vector ``c`` of length ``T``.
    """
    T = len(returns)
    N = params.n_states
    alpha: list[list[float]] = [[0.0] * N for _ in range(T)]
    c = [0.0] * T
    # init t=0
    for j in range(N):
        alpha[0][j] = params.init_probs[j] * math.exp(_gaussian_log_pdf(returns[0], params.means[j], params.stds[j]))
    c0 = sum(alpha[0])
    if c0 <= 0:
        c0 = 1e-300
    c[0] = c0
    for j in range(N):
        alpha[0][j] /= c0
    # induction
    for t in range(1, T):
        for j in range(N):
            ssum = 0.0
            for i in range(N):
                ssum += alpha[t - 1][i] * params.trans_matrix[i][j]
            ssum *= math.exp(_gaussian_log_pdf(returns[t], params.means[j], params.stds[j]))
            alpha[t][j] = ssum
        ct = sum(alpha[t])
        if ct <= 0:
            ct = 1e-300
        c[t] = ct
        for j in range(N):
            alpha[t][j] /= ct
    return alpha, c


def backward_probs(returns: Sequence[float], params: HMMParams, c: Sequence[float]) -> list[list[float]]:
    """Scaled backward probabilities (beta) using scale factors ``c`` from forward.

    ``β_t(j) = P(o_{t+1}, …, o_T | s_t = j, λ)`` rescaled by the same ``c_t``
    (Rabiner 1989 §III.B eqs. 91–93).
    """
    T = len(returns)
    N = params.n_states
    beta: list[list[float]] = [[0.0] * N for _ in range(T)]
    for j in range(N):
        beta[T - 1][j] = 1.0
    for t in range(T - 2, -1, -1):
        for j in range(N):
            ssum = 0.0
            for i in range(N):
                ssum += (
                    params.trans_matrix[j][i]
                    * math.exp(_gaussian_log_pdf(returns[t + 1], params.means[i], params.stds[i]))
                    * beta[t + 1][i]
                )
            beta[t][j] = ssum / (c[t + 1] if c[t + 1] > 0 else 1e-300)
    return beta


def _log_likelihood_from_scales(c: Sequence[float]) -> float:
    """Log-likelihood ``log P(O | λ) = Σ_t log c_t`` for the scaled forward pass."""
    return sum(math.log(cc) for cc in c if cc > 0)


# ---------------------------------------------------------------------------
# Baum-Welch fit
# ---------------------------------------------------------------------------


def fit_hmm(
    returns: Sequence[float],
    n_states: int = 2,
    n_iter: int = 50,
    tol: float = 1e-5,
) -> HmmFitResult:
    """Baum-Welch EM for a Gaussian-emission HMM.

    E-step: forward-backward with the scaling-factor trick to obtain the
    posterior ``γ_t(i) = P(s_t=i | O, λ)`` and the joint
    ``ξ_t(i,j) = P(s_t=i, s_{t+1}=j | O, λ)``.

    M-step (closed-form, Rabiner 1989 §III.C eqs. 103a–103d):

        π_i      = γ_0(i)
        a_{ij}   = Σ_t ξ_t(i,j) / Σ_t γ_t(i)
        μ_i      = Σ_t γ_t(i) o_t / Σ_t γ_t(i)
        σ²_i     = Σ_t γ_t(i) (o_t − μ_i)² / Σ_t γ_t(i)

    Deterministic quantile initialization; fixed iteration cap ``n_iter`` with
    early stop when ``|logL_new − logL_old| < tol``. No RNG.
    """
    if n_states < 2:
        raise ValueError("n_states must be >= 2")
    if n_iter < 1:
        raise ValueError("n_iter must be >= 1")
    if tol < 0:
        raise ValueError("tol must be non-negative")
    obs = _validate_returns(returns)
    T = len(obs)
    params = _quantile_init(obs, n_states)

    prev_ll = _NEG_INF
    converged = False
    iter_run = 0
    for it in range(n_iter):
        iter_run = it + 1
        alpha, c = forward_probs(obs, params)
        beta = backward_probs(obs, params, c)
        ll = _log_likelihood_from_scales(c)

        # gamma_t(i) = alpha_t(i) * beta_t(i) / sum_j alpha_t(j)*beta_t(j)
        gamma: list[list[float]] = [[0.0] * n_states for _ in range(T)]
        for t in range(T):
            denom = 0.0
            for j in range(n_states):
                val = alpha[t][j] * beta[t][j]
                gamma[t][j] = val
                denom += val
            if denom <= 0:
                denom = 1e-300
            for j in range(n_states):
                gamma[t][j] /= denom

        # xi_sum[i][j] = sum_{t=0..T-2} xi_t(i,j)
        xi_sum: list[list[float]] = [[0.0] * n_states for _ in range(n_states)]
        for t in range(T - 1):
            denom = 0.0
            tmp = [[0.0] * n_states for _ in range(n_states)]
            for i in range(n_states):
                for j in range(n_states):
                    val = (
                        alpha[t][i]
                        * params.trans_matrix[i][j]
                        * math.exp(_gaussian_log_pdf(obs[t + 1], params.means[j], params.stds[j]))
                        * beta[t + 1][j]
                    )
                    tmp[i][j] = val
                    denom += val
            if denom <= 0:
                denom = 1e-300
            for i in range(n_states):
                for j in range(n_states):
                    xi_sum[i][j] += tmp[i][j] / denom

        # M-step
        new_init = list(gamma[0])
        s_init = sum(new_init)
        if s_init > 0:
            new_init = [v / s_init for v in new_init]
        else:
            new_init = [1.0 / n_states] * n_states

        new_trans: list[list[float]] = [[0.0] * n_states for _ in range(n_states)]
        for i in range(n_states):
            denom = sum(xi_sum[i])
            if denom <= 0:
                # fallback: uniform row
                for j in range(n_states):
                    new_trans[i][j] = 1.0 / n_states
            else:
                for j in range(n_states):
                    new_trans[i][j] = xi_sum[i][j] / denom

        new_means: list[float] = [0.0] * n_states
        new_vars: list[float] = [0.0] * n_states
        for i in range(n_states):
            denom = sum(gamma[t][i] for t in range(T))
            if denom <= 0:
                # state never visited — keep previous emission params
                new_means[i] = params.means[i]
                new_vars[i] = params.stds[i] ** 2
                continue
            m = sum(gamma[t][i] * obs[t] for t in range(T)) / denom
            v = sum(gamma[t][i] * (obs[t] - m) ** 2 for t in range(T)) / denom
            new_means[i] = m
            new_vars[i] = v if v > 0 else 1e-12

        new_stds = [math.sqrt(v) for v in new_vars]
        # floor std to avoid degenerate emissions
        new_stds = [max(s, 1e-9) for s in new_stds]

        params = HMMParams(
            n_states=n_states,
            init_probs=new_init,
            trans_matrix=new_trans,
            means=new_means,
            stds=new_stds,
        )

        # convergence check on log-likelihood
        if abs(ll - prev_ll) < tol:
            converged = True
            prev_ll = ll
            break
        prev_ll = ll

    final_ll = prev_ll
    # decode training states via Viterbi under final params
    states = viterbi(obs, params)
    labels = regime_label(states, params.means)
    return HmmFitResult(
        params=params,
        log_likelihood=final_ll,
        converged=converged,
        n_iter_run=iter_run,
        regime_labels=labels,
    )


# ---------------------------------------------------------------------------
# Viterbi
# ---------------------------------------------------------------------------


def viterbi(returns: Sequence[float], params: HMMParams) -> list[int]:
    """Most likely state path via Viterbi in log-space (Rabiner 1989 §III.C).

    ``δ_t(j) = max_{s_0..s_{t-1}} log P(s_0..s_t=j, o_0..o_t | λ)`` with
    recursion ``δ_t(j) = max_i [δ_{t-1}(i) + log a_{ij}] + log b_j(o_t)`` and
    backpointer ``ψ_t(j) = argmax_i [δ_{t-1}(i) + log a_{ij}]``.
    """
    if returns is None or len(returns) == 0:
        raise ValueError("returns must be non-empty")
    T = len(returns)
    N = params.n_states
    log_trans = [[math.log(a) if a > 0 else _NEG_INF for a in row] for row in params.trans_matrix]
    log_init = [math.log(p) if p > 0 else _NEG_INF for p in params.init_probs]
    # delta[t][j], psi[t][j]
    delta = [[_NEG_INF] * N for _ in range(T)]
    psi = [[0] * N for _ in range(T)]
    for j in range(N):
        delta[0][j] = log_init[j] + _gaussian_log_pdf(returns[0], params.means[j], params.stds[j])
        psi[0][j] = 0
    for t in range(1, T):
        for j in range(N):
            best = _NEG_INF
            best_i = 0
            for i in range(N):
                val = delta[t - 1][i] + log_trans[i][j]
                if val > best:
                    best = val
                    best_i = i
            delta[t][j] = best + _gaussian_log_pdf(returns[t], params.means[j], params.stds[j])
            psi[t][j] = best_i
    # backtrack
    path = [0] * T
    last = 0
    best = _NEG_INF
    for j in range(N):
        if delta[T - 1][j] > best:
            best = delta[T - 1][j]
            last = j
    path[T - 1] = last
    for t in range(T - 2, -1, -1):
        path[t] = psi[t + 1][path[t + 1]]
    return path


# ---------------------------------------------------------------------------
# helpers / labels
# ---------------------------------------------------------------------------


def state_means(returns: Sequence[float], states: Sequence[int], n_states: int) -> list[float]:
    """Empirical mean of returns assigned to each state (by state index)."""
    if len(returns) != len(states):
        raise ValueError("returns and states must have equal length")
    sums = [0.0] * n_states
    counts = [0] * n_states
    for r, s in zip(returns, states):
        if 0 <= s < n_states:
            sums[s] += float(r)
            counts[s] += 1
    return [sums[i] / counts[i] if counts[i] > 0 else 0.0 for i in range(n_states)]


def state_stds(returns: Sequence[float], states: Sequence[int], n_states: int) -> list[float]:
    """Empirical population std of returns assigned to each state."""
    if len(returns) != len(states):
        raise ValueError("returns and states must have equal length")
    sums = [0.0] * n_states
    sqsums = [0.0] * n_states
    counts = [0] * n_states
    for r, s in zip(returns, states):
        if 0 <= s < n_states:
            v = float(r)
            sums[s] += v
            sqsums[s] += v * v
            counts[s] += 1
    out: list[float] = []
    for i in range(n_states):
        if counts[i] <= 1:
            out.append(0.0)
        else:
            m = sums[i] / counts[i]
            var = sqsums[i] / counts[i] - m * m
            out.append(math.sqrt(var) if var > 0 else 0.0)
    return out


def regime_label(states: list[int], means: list[float]) -> list[str]:
    """Map each state index to a regime label string.

    For ``n_states == 3``: highest mean → ``BULL``, lowest mean → ``BEAR``,
    middle → ``SIDWAYS``. For ``n_states == 2``: positive mean → ``BULL``
    else ``BEAR``. For other ``n_states``: rank ascending and label the top
    ``BULL``, the bottom ``BEAR``, the rest ``SIDWAYS``.
    """
    if not means:
        return []
    n = len(means)
    labels_for_state: list[str] = [""] * n
    if n == 2:
        for i in range(n):
            labels_for_state[i] = "BULL" if means[i] > 0 else "BEAR"
    elif n == 3:
        order = sorted(range(n), key=lambda i: means[i])
        labels_for_state[order[0]] = "BEAR"
        labels_for_state[order[-1]] = "BULL"
        labels_for_state[order[1]] = "SIDWAYS"
    else:
        order = sorted(range(n), key=lambda i: means[i])
        labels_for_state[order[0]] = "BEAR"
        labels_for_state[order[-1]] = "BULL"
        for k in range(1, n - 1):
            labels_for_state[order[k]] = "SIDWAYS"
    return [labels_for_state[s] for s in states]