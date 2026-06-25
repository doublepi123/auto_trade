"""P245: Linear Kalman filter and Rauch-Tung-Striebel smoother.

A pure-Python, dependency-free implementation of the discrete linear
Kalman filter for a (possibly time-varying) model

    xₜ = Fₜ xₜ₋₁ + Bₜ uₜ + wₜ,   w ~ N(0, Qₜ)
    zₜ = Hₜ xₜ   + vₜ,           v ~ N(0, Rₜ)

with the standard *predict* / *update* recursion and the Joseph stabilised
covariance update. The Rauch-Tung-Striebel (1965) fixed-interval smoother
then runs the filtered estimates backward, using the per-step *predicted*
covariances recorded during the forward pass, to produce the smoothed posterior.

Reference: Kalman (1960); Rauch, Tung & Striebel (1965); filterpy / pykalman
for the abstraction shape. Matrices are plain ``list[list[float]]``; matrix
ops are local helpers (no numpy). Deterministic — no RNG.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "KalmanResult",
    "kalman_filter",
    "rts_smoother",
]

Matrix = list[list[float]]
Vector = list[float]


def _mat_mul(a: Matrix, b: Matrix) -> Matrix:
    n, m, p = len(a), len(a[0]), len(b[0])
    out = [[0.0] * p for _ in range(n)]
    for i in range(n):
        ai = a[i]
        oi = out[i]
        for k in range(m):
            aik = ai[k]
            if aik == 0.0:
                continue
            bk = b[k]
            for j in range(p):
                oi[j] += aik * bk[j]
    return out


def _mat_vec(a: Matrix, v: Vector) -> Vector:
    n, m = len(a), len(a[0])
    out = [0.0] * n
    for i in range(n):
        ai = a[i]
        s = 0.0
        for j in range(m):
            s += ai[j] * v[j]
        out[i] = s
    return out


def _mat_transpose(a: Matrix) -> Matrix:
    n, m = len(a), len(a[0])
    return [[a[i][j] for i in range(n)] for j in range(m)]


def _mat_add(a: Matrix, b: Matrix) -> Matrix:
    return [[a[i][j] + b[i][j] for j in range(len(a[0]))] for i in range(len(a))]


def _mat_sub(a: Matrix, b: Matrix) -> Matrix:
    return [[a[i][j] - b[i][j] for j in range(len(a[0]))] for i in range(len(a))]


def _mat_eye(n: int) -> Matrix:
    return [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]


def _mat_inverse(a: Matrix) -> Matrix:
    """Invert a small square matrix via Gauss-Jordan with partial pivoting."""
    n = len(a)
    aug = [a[i][:] + [1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for col in range(n):
        pivot = col
        best = abs(aug[col][col])
        for r in range(col + 1, n):
            if abs(aug[r][col]) > best:
                best = abs(aug[r][col])
                pivot = r
        if best < 1e-18:
            raise ValueError("singular matrix in Kalman update")
        if pivot != col:
            aug[col], aug[pivot] = aug[pivot], aug[col]
        piv = aug[col][col]
        inv_piv = 1.0 / piv
        for c in range(2 * n):
            aug[col][c] *= inv_piv
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col]
            if factor == 0.0:
                continue
            for c in range(2 * n):
                aug[r][c] -= factor * aug[col][c]
    return [[aug[i][n + j] for j in range(n)] for i in range(n)]


def _seq_matrices(value: object, n_steps: int) -> list[Matrix]:
    """Normalise a static matrix / per-step list into a per-step list of matrices.

    A "static matrix" is ``list[list[float]]`` whose rows are floats; a
    "per-step list" is ``list[list[list[float]]]`` (list of matrices).
    """
    if not isinstance(value, list) or not value:
        raise ValueError("matrix input must be non-empty")
    if not isinstance(value[0], list):
        raise ValueError("matrix input must be list[list[float]] or list[list[list[float]]]")
    if value[0] and isinstance(value[0][0], list):
        mats = value  # type: list[Matrix]
        if len(mats) != n_steps:
            raise ValueError("per-step matrix list length must match observations")
        return mats
    return [value] * n_steps  # type: ignore[list-item]


@dataclass(frozen=True)
class KalmanResult:
    filtered_means: list[Vector]
    filtered_covs: list[Matrix]
    predicted_means: list[Vector]
    predicted_covs: list[Matrix]
    innovations: list[Vector]
    smoothed_means: list[Vector]
    smoothed_covs: list[Matrix]

    def to_dict(self) -> dict:
        return {
            "filtered_means": self.filtered_means,
            "filtered_covs": self.filtered_covs,
            "predicted_means": self.predicted_means,
            "predicted_covs": self.predicted_covs,
            "innovations": self.innovations,
            "smoothed_means": self.smoothed_means,
            "smoothed_covs": self.smoothed_covs,
        }


def kalman_filter(
    observations: Sequence[Sequence[float]],
    F: Matrix,
    H: Matrix,
    Q: Matrix,
    R: Matrix,
    x0: Vector,
    P0: Matrix,
    *,
    B: Matrix | None = None,
    u: Sequence[Sequence[float]] | None = None,
) -> KalmanResult:
    """Run the forward Kalman filter.

    Parameters
    ----------
    observations : list of measurement vectors zₜ (each ``len == len(H)``).
    F, H, Q, R : static model matrices (or per-step lists of matrices).
        F: state transition (n×n), H: observation (m×n), Q: process noise
        cov (n×n), R: measurement noise cov (m×m).
    x0, P0 : initial state mean (n) and covariance (n×n).
    B, u : optional control-input model (n×c) and per-step control vectors (c).

    Returns :class:`KalmanResult`. Records per-step *predicted* means/covs so
    :func:`rts_smoother` can run without re-supplying Q.
    """
    n = len(x0)
    steps = len(observations)
    if steps == 0:
        raise ValueError("observations must be non-empty")
    Fs = _seq_matrices(F, steps)
    Hs = _seq_matrices(H, steps)
    Qs = _seq_matrices(Q, steps)
    Rs = _seq_matrices(R, steps)
    Bs = _seq_matrices(B, steps) if B is not None else None
    if u is not None and len(u) != steps:
        raise ValueError("control input length must match observations")

    x = list(x0)
    P = [row[:] for row in P0]
    filtered_means: list[Vector] = []
    filtered_covs: list[Matrix] = []
    predicted_means: list[Vector] = []
    predicted_covs: list[Matrix] = []
    innovations: list[Vector] = []

    for t in range(steps):
        Ft = Fs[t]
        Ht = Hs[t]
        Qt = Qs[t]
        Rt = Rs[t]
        # ---- predict ----
        x_pred = _mat_vec(Ft, x)
        if Bs is not None and u is not None:
            ctrl = _mat_vec(Bs[t], list(u[t]))
            x_pred = [a + b for a, b in zip(x_pred, ctrl)]
        P_pred = _mat_add(_mat_mul(_mat_mul(Ft, P), _mat_transpose(Ft)), Qt)
        predicted_means.append(list(x_pred))
        predicted_covs.append([row[:] for row in P_pred])

        # ---- update ----
        z = [float(v) for v in observations[t]]
        Hx = _mat_vec(Ht, x_pred)
        y = [zv - hv for zv, hv in zip(z, Hx)]  # innovation
        S = _mat_add(_mat_mul(_mat_mul(Ht, P_pred), _mat_transpose(Ht)), Rt)
        S_inv = _mat_inverse(S)
        K = _mat_mul(_mat_mul(P_pred, _mat_transpose(Ht)), S_inv)  # Kalman gain
        x = [x_pred[i] + sum(K[i][j] * y[j] for j in range(len(y))) for i in range(n)]
        # Joseph-form covariance update for numerical stability.
        I = _mat_eye(n)
        KH = _mat_mul(K, Ht)
        ImKH = _mat_sub(I, KH)
        P = _mat_add(
            _mat_mul(_mat_mul(ImKH, P_pred), _mat_transpose(ImKH)),
            _mat_mul(_mat_mul(K, Rt), _mat_transpose(K)),
        )
        filtered_means.append(list(x))
        filtered_covs.append([row[:] for row in P])
        innovations.append(list(y))

    return KalmanResult(
        filtered_means=filtered_means,
        filtered_covs=filtered_covs,
        predicted_means=predicted_means,
        predicted_covs=predicted_covs,
        innovations=innovations,
        smoothed_means=[],
        smoothed_covs=[],
    )


def rts_smoother(filtered: KalmanResult, F: Matrix) -> KalmanResult:
    """Rauch-Tung-Striebel fixed-interval smoother.

    Uses the per-step predicted means/covariances stored on ``filtered`` (from
    :func:`kalman_filter`) and the (static) transition matrix ``F``. Returns a
    new :class:`KalmanResult` with ``smoothed_means`` / ``smoothed_covs``
    populated. The last step is its own smoother (boundary condition).
    """
    means = filtered.filtered_means
    covs = filtered.filtered_covs
    pred_means = filtered.predicted_means
    pred_covs = filtered.predicted_covs
    steps = len(means)
    if steps == 0:
        raise ValueError("nothing to smooth")
    sm_means: list[Vector] = [list(means[-1])]
    sm_covs: list[Matrix] = [[row[:] for row in covs[-1]]]
    for t in range(steps - 2, -1, -1):
        x_t = means[t]
        P_t = covs[t]
        x_next_pred = pred_means[t + 1]
        P_next_pred = pred_covs[t + 1]
        P_next_pred_inv = _mat_inverse(P_next_pred)
        J = _mat_mul(_mat_mul(P_t, _mat_transpose(F)), P_next_pred_inv)
        x_smooth = [
            x_t[i] + sum(J[i][j] * (sm_means[-1][j] - x_next_pred[j]) for j in range(len(x_t)))
            for i in range(len(x_t))
        ]
        P_smooth = _mat_add(
            P_t,
            _mat_mul(_mat_mul(J, _mat_sub(sm_covs[-1], P_next_pred)), _mat_transpose(J)),
        )
        sm_means.append(list(x_smooth))
        sm_covs.append([row[:] for row in P_smooth])
    sm_means.reverse()
    sm_covs.reverse()
    return KalmanResult(
        filtered_means=filtered.filtered_means,
        filtered_covs=filtered.filtered_covs,
        predicted_means=filtered.predicted_means,
        predicted_covs=filtered.predicted_covs,
        innovations=filtered.innovations,
        smoothed_means=sm_means,
        smoothed_covs=sm_covs,
    )