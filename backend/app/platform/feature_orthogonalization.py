"""P265: Feature orthogonalization utilities.

Pure-Python (standard library only) toolkit for de-correlating a panel of
features prior to modelling. Provides:

* :func:`dot` / :func:`norm` — vector primitives with the same validation
  contract as the rest of this module.
* :func:`gram_schmidt` — modified Gram-Schmidt producing mutually orthogonal
  output vectors. A feature that is linearly dependent on the already-
  orthogonalized set collapses to a **zero vector** (its projection removes
  everything); such features are *not* dropped, the caller decides.
* :func:`residualize` — ordinary-least-squares projection residual of a
  target onto a set of exposure features (no intercept; demean externally
  if a mean-removed fit is desired).
* :func:`correlation_prune` — greedy, order-preserving feature pruning on
  pairwise |Pearson correlation|. The first feature is always kept; any
  later feature whose |corr| against *any* already-kept feature is at or
  above ``threshold`` is dropped.
* :func:`vif_scores` — simplified variance-inflation-factor approximation
  ``1 / (1 - max_abs_corr**2 + eps)`` (no full matrix inversion). Duplicate
  / highly-correlated features therefore score higher than independent ones.
* :func:`orthogonalization_report` — convenience aggregator returning an
  :class:`OrthogonalizationResult`.

Error-handling contract (mirrors the other platform modules, see
``rolling_features.py``): every invalid argument raises ``ValueError``
uniformly — including empty panels, length mismatch, non-sequence feature
values, ``dict`` values, ``bool`` entries, non-finite numbers, and
out-of-range thresholds. The platform HTTP layer translates this single
exception family (plus ``TypeError``) into HTTP 422.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "OrthogonalizationResult",
    "correlation_prune",
    "dot",
    "gram_schmidt",
    "norm",
    "orthogonalization_report",
    "residualize",
    "vif_scores",
]


_EPS = 1e-12
_RIDGE = 1e-10


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _as_finite_vector(value: Any, *, label: str) -> list[float]:
    """Coerce ``value`` into a list of finite floats.

    Rejects non-sequences, strings, dicts, ``bool`` entries, and non-finite
    numbers (NaN / inf), raising :class:`ValueError` uniformly.
    """
    # bool is a subclass of int; reject bool panels explicitly. Strings and
    # dicts are technically sequences / mappings but are clearly not numeric
    # vectors — reject them up front to give an actionable message.
    if isinstance(value, (str, dict)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be a sequence of finite numbers")
    out: list[float] = []
    for entry in value:
        if isinstance(entry, bool) or not isinstance(entry, (int, float)):
            raise ValueError(f"{label} entries must be finite numbers")
        number = float(entry)
        if not math.isfinite(number):
            raise ValueError(f"{label} entries must be finite numbers")
        out.append(number)
    return out


def _validate_panel(panel: Any) -> dict[str, list[float]]:
    """Validate a ``{feature: series}`` panel.

    Rejects non-dict inputs, empty dicts, non-sequence / dict / str feature
    values, ``bool`` entries, non-finite numbers, and length mismatch across
    features.
    """
    if not isinstance(panel, dict) or not panel:
        raise ValueError("panel must be a non-empty dict of feature series")
    validated: dict[str, list[float]] = {}
    length: int | None = None
    for name, series in panel.items():
        vector = _as_finite_vector(series, label=f"panel['{name}']")
        if not vector:
            raise ValueError(f"panel['{name}'] must be a non-empty series")
        if length is None:
            length = len(vector)
        elif len(vector) != length:
            raise ValueError("panel feature series must have equal length")
        validated[str(name)] = vector
    return validated


def _validate_threshold(threshold: float) -> float:
    """Validate a correlation threshold in ``[0, 1]``."""
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ValueError("threshold must be a finite number in [0, 1]")
    t = float(threshold)
    if not math.isfinite(t) or t < 0.0 or t > 1.0:
        raise ValueError("threshold must be a finite number in [0, 1]")
    return t


# ---------------------------------------------------------------------------
# vector primitives
# ---------------------------------------------------------------------------


def dot(x: Sequence[float], y: Sequence[float]) -> float:
    """Euclidean inner product of two equal-length numeric vectors."""
    a = _as_finite_vector(x, label="x")
    b = _as_finite_vector(y, label="y")
    if len(a) != len(b):
        raise ValueError("x and y must have equal length")
    return sum(ai * bi for ai, bi in zip(a, b))


def norm(x: Sequence[float]) -> float:
    """Euclidean (L2) norm of a numeric vector."""
    vector = _as_finite_vector(x, label="x")
    return math.sqrt(sum(v * v for v in vector))


# ---------------------------------------------------------------------------
# Gram-Schmidt
# ---------------------------------------------------------------------------


def gram_schmidt(panel: Mapping[str, Sequence[float]]) -> dict[str, list[float]]:
    """Modified Gram-Schmidt orthogonalization of a feature panel.

    Returns a ``{feature: orthogonal_vector}`` dict in the input order. Each
    output vector is the input vector minus its projection onto the span of
    the *already-orthogonalized* predecessors. Output vectors are mutually
    near-orthogonal (up to floating-point error).

    A feature that is linearly dependent on the predecessors collapses to a
    **zero vector** (its projection removes everything); it is *not* dropped
    so the caller retains the full input shape. Callers can detect
    near-zero norms to identify redundant features.
    """
    validated = _validate_panel(panel)
    orthogonal: dict[str, list[float]] = {}
    for name, vector in validated.items():
        v = list(vector)
        for prev in orthogonal.values():
            denom = sum(p * p for p in prev)
            # Avoid division by zero: a zero predecessor contributes nothing.
            if denom > _EPS:
                coeff = sum(p * vi for p, vi in zip(prev, v)) / denom
                v = [vi - coeff * p for vi, p in zip(v, prev)]
        orthogonal[name] = v
    return orthogonal


# ---------------------------------------------------------------------------
# OLS residualization
# ---------------------------------------------------------------------------


def residualize(
    target: Sequence[float], exposures: Sequence[Sequence[float]]
) -> list[float]:
    """Residual of ``target`` after OLS projection onto ``exposures``.

    Solves the normal equations ``beta = (XᵀX)⁻¹ Xᵀy`` with a small ridge
    term on the diagonal for numerical stability, then returns ``y - Xβ``.
    With no exposures the target is returned unchanged. A single exposure
    removes its linear component exactly (residual is orthogonal to the
    exposure up to floating-point error).
    """
    y = _as_finite_vector(target, label="target")
    if not y:
        raise ValueError("target must be a non-empty series")
    if not exposures:
        return list(y)
    # Validate + equal-length-check the exposure matrix.
    x_matrix: list[list[float]] = []
    n = len(y)
    for idx, exp in enumerate(exposures):
        vector = _as_finite_vector(exp, label=f"exposures[{idx}]")
        if len(vector) != n:
            raise ValueError("exposures must have the same length as target")
        x_matrix.append(vector)
    k = len(x_matrix)
    # Build XᵀX (k×k) and Xᵀy (k). Add a tiny ridge to guarantee invertibility
    # even when exposures are collinear.
    xtx = [
        [sum(x_matrix[i][t] * x_matrix[j][t] for t in range(n)) for j in range(k)]
        for i in range(k)
    ]
    xty = [sum(x_matrix[i][t] * y[t] for t in range(n)) for i in range(k)]
    ridge = _RIDGE
    for i in range(k):
        xtx[i][i] += ridge
    beta = _solve_linear(xtx, xty)
    residual = list(y)
    for i in range(k):
        for t in range(n):
            residual[t] -= beta[i] * x_matrix[i][t]
    return residual


def _solve_linear(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """Gauss-Jordan elimination with partial pivoting (pure stdlib)."""
    n = len(rhs)
    # Augmented copy.
    aug: list[list[float]] = [list(matrix[i]) + [rhs[i]] for i in range(n)]
    for col in range(n):
        # Partial pivot: largest absolute value in this column at/below row.
        pivot_row = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot_row][col]) < _EPS:
            # Singular column → skip; ridge in the caller normally prevents this.
            continue
        aug[col], aug[pivot_row] = aug[pivot_row], aug[col]
        pivot = aug[col][col]
        aug[col] = [v / pivot for v in aug[col]]
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col]
            if factor != 0.0:
                aug[r] = [a - factor * b for a, b in zip(aug[r], aug[col])]
    return [aug[i][n] for i in range(n)]


# ---------------------------------------------------------------------------
# correlation + pruning
# ---------------------------------------------------------------------------


def _pearson(a: Sequence[float], b: Sequence[float]) -> float:
    n = len(a)
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    var_a = sum((ai - mean_a) ** 2 for ai in a)
    var_b = sum((bi - mean_b) ** 2 for bi in b)
    denom = math.sqrt(var_a * var_b)
    if denom < _EPS:
        # Zero-variance feature: correlation undefined; treat as perfectly
        # correlated so a duplicate zero-variance feature gets pruned.
        return 1.0 if var_a < _EPS and var_b < _EPS else 0.0
    return cov / denom


def correlation_prune(
    panel: Mapping[str, Sequence[float]], threshold: float = 0.95
) -> tuple[list[str], list[str]]:
    """Greedy correlation-based feature pruning.

    Iterates features in input order. The first feature is always kept. A
    later feature is dropped iff its ``|Pearson correlation|`` against *any*
    already-kept feature is ``>= threshold``. Returns ``(kept, dropped)``.
    """
    validated = _validate_panel(panel)
    t = _validate_threshold(threshold)
    kept: list[str] = []
    dropped: list[str] = []
    for name in validated:
        keep = True
        for k in kept:
            if abs(_pearson(validated[name], validated[k])) >= t:
                keep = False
                break
        if keep:
            kept.append(name)
        else:
            dropped.append(name)
    return kept, dropped


def vif_scores(panel: Mapping[str, Sequence[float]]) -> dict[str, float]:
    """Simplified variance-inflation-factor approximation.

    For each feature ``i`` the VIF is approximated by
    ``1 / (1 - max_abs_corr**2 + eps)`` where ``max_abs_corr`` is the largest
    ``|Pearson correlation|`` between feature ``i`` and any *other* feature.
    A duplicate / highly-correlated feature therefore scores higher than an
    independent one. Single-feature panels return VIF 1.0 (no collinearity).
    """
    validated = _validate_panel(panel)
    names = list(validated.keys())
    scores: dict[str, float] = {}
    for i, name in enumerate(names):
        max_abs = 0.0
        for j, other in enumerate(names):
            if i == j:
                continue
            max_abs = max(max_abs, abs(_pearson(validated[name], validated[other])))
        denom = 1.0 - max_abs * max_abs + _EPS
        scores[name] = 1.0 / denom
    return scores


def _pairwise_correlations(panel: dict[str, list[float]]) -> dict[str, float]:
    """All unique pair Pearson correlations labelled ``"A|B"`` (A before B)."""
    names = list(panel.keys())
    out: dict[str, float] = {}
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            out[f"{a}|{b}"] = _pearson(panel[a], panel[b])
    return out


# ---------------------------------------------------------------------------
# result + report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrthogonalizationResult:
    """Aggregated orthogonalization report.

    Attributes
    ----------
    orthogonal_features:
        Gram-Schmidt orthogonalized vectors for the *kept* features (so the
        output panel does not include dropped duplicates).
    residualized:
        ``target`` residualized against the kept features, or ``None`` when
        no target was supplied.
    kept_features / dropped_features:
        Correlation-prune partition of the input feature names.
    vif_scores:
        Simplified VIF per feature (computed over the *full* input panel,
        so dropped features still get a diagnostic score).
    correlations:
        Pairwise Pearson correlations, labelled ``"A|B"``.
    """

    orthogonal_features: dict[str, list[float]]
    residualized: list[float] | None
    kept_features: list[str]
    dropped_features: list[str]
    vif_scores: dict[str, float]
    correlations: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "orthogonal_features": self.orthogonal_features,
            "residualized": self.residualized,
            "kept_features": list(self.kept_features),
            "dropped_features": list(self.dropped_features),
            "vif_scores": dict(self.vif_scores),
            "correlations": dict(self.correlations),
        }


def orthogonalization_report(
    panel: Mapping[str, Sequence[float]],
    target: Sequence[float] | None = None,
    threshold: float = 0.95,
) -> OrthogonalizationResult:
    """Run the full de-correlation pipeline on a feature panel.

    Steps:

    1. Validate the panel and threshold.
    2. Correlation-prune the panel into ``kept`` / ``dropped`` partitions.
    3. Gram-Schmidt orthogonalize the *kept* features.
    4. Residualize ``target`` against the kept features (when supplied).
    5. Compute simplified VIF over the full input panel + pairwise Pearson
       correlations for diagnostics.
    """
    validated = _validate_panel(panel)
    t = _validate_threshold(threshold)
    kept, dropped = correlation_prune(validated, threshold=t)
    kept_panel = {name: validated[name] for name in kept}
    orthogonal = gram_schmidt(kept_panel)
    residual: list[float] | None = None
    if target is not None:
        target_vec = _as_finite_vector(target, label="target")
        if not target_vec:
            raise ValueError("target must be a non-empty series")
        # Length must match the panel.
        sample = next(iter(validated.values()))
        if len(target_vec) != len(sample):
            raise ValueError("target must have the same length as the panel")
        residual = residualize(target_vec, [validated[name] for name in kept])
    return OrthogonalizationResult(
        orthogonal_features=orthogonal,
        residualized=residual,
        kept_features=kept,
        dropped_features=dropped,
        vif_scores=vif_scores(validated),
        correlations=_pairwise_correlations(validated),
    )
