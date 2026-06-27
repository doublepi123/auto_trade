"""P266: Signal combination utilities.

Pure-Python (standard library only) toolkit for combining a panel of
equal-length finite-numeric signal series into a single weighted composite.
Provides:

* :func:`standardize_signal` — z-score standardization (constant signal → 0).
* :func:`rank_signal` — average-rank transform preserving order and ties.
* :func:`normalize_weights` — L1-normalize a weight vector preserving sign.
* :func:`risk_budget_weights` — inverse-volatility weights (high-volatility
  signals receive a smaller absolute weight than low-volatility ones).
* :func:`combine_signals` — combine a ``{name: series}`` panel via one of
  ``"zscore"``, ``"rank"`` or ``"raw"`` using optional explicit weights
  (default equal weights with ``|w|`` summing to 1). Returns a
  :class:`SignalCombinationResult` with the combined series, the normalized
  weights, the standardized per-signal series, the chosen method and the
  signal count.

Error-handling contract mirrors the other platform modules (see
``feature_orthogonalization.py``): every invalid argument raises
``ValueError`` uniformly — including empty panels, length mismatch,
non-sequence feature values, ``dict`` / ``str`` values, ``bool`` entries,
non-finite numbers, an unknown ``method``, a weights key mismatch and
all-zero weights. The platform HTTP layer translates this single exception
family (plus ``TypeError``) into HTTP 422.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "SignalCombinationResult",
    "combine_signals",
    "normalize_weights",
    "rank_signal",
    "risk_budget_weights",
    "standardize_signal",
]


_EPS = 1e-12

_VALID_METHODS = frozenset({"zscore", "rank", "raw"})


# ---------------------------------------------------------------------------
# validation primitives
# ---------------------------------------------------------------------------


def _as_finite_vector(value: Any, *, label: str) -> list[float]:
    """Coerce ``value`` into a list of finite floats.

    Rejects non-sequences, strings, dicts, ``bool`` entries, and non-finite
    numbers (NaN / inf), raising :class:`ValueError` uniformly. Mirrors the
    contract of :func:`app.platform.feature_orthogonalization._as_finite_vector`.
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


def _validate_panel(signals: Any) -> dict[str, list[float]]:
    """Validate a ``{name: series}`` panel of equal-length finite series.

    Rejects non-dict inputs, empty dicts, non-sequence / dict / str feature
    values, ``bool`` entries, non-finite numbers, and length mismatch across
    features.
    """
    if not isinstance(signals, Mapping) or not signals:
        raise ValueError("signals must be a non-empty dict of finite series")
    validated: dict[str, list[float]] = {}
    length: int | None = None
    for name, series in signals.items():
        vector = _as_finite_vector(series, label=f"signals['{name}']")
        if not vector:
            raise ValueError(f"signals['{name}'] must be a non-empty series")
        if length is None:
            length = len(vector)
        elif len(vector) != length:
            raise ValueError("signal series must have equal length")
        validated[str(name)] = vector
    return validated


# ---------------------------------------------------------------------------
# transforms
# ---------------------------------------------------------------------------


def standardize_signal(signal: Sequence[float]) -> list[float]:
    """Z-score standardize a numeric series (mean 0, sample std 1).

    A constant signal (zero variance) returns a vector of zeros so the
    transform is well-defined and contributes no dispersion to a downstream
    combination.
    """
    vector = _as_finite_vector(signal, label="signal")
    if not vector:
        raise ValueError("signal must be a non-empty series")
    n = len(vector)
    mean = sum(vector) / n
    var = sum((x - mean) ** 2 for x in vector) / n
    std = math.sqrt(var)
    if std <= _EPS:
        return [0.0] * n
    inv = 1.0 / std
    return [(x - mean) * inv for x in vector]


def rank_signal(signal: Sequence[float]) -> list[float]:
    """Average-rank transform of a numeric series.

    Output length equals input length; the ordering of the input is preserved
    (larger input → larger rank) and ties share the mean of the positions they
    span (the standard "fractional"/"average" rank used in non-parametric
    statistics).
    """
    vector = _as_finite_vector(signal, label="signal")
    if not vector:
        raise ValueError("signal must be a non-empty series")
    n = len(vector)
    # Sort indices by value (stable keeps ties in input order, but average-
    # rank is order-independent anyway).
    order = sorted(range(n), key=lambda i: vector[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        # advance over the tie block (values equal within epsilon)
        while j + 1 < n and abs(vector[order[j + 1]] - vector[order[i]]) <= _EPS:
            j += 1
        avg_pos = (i + j) / 2.0  # 0-based average position
        for k in range(i, j + 1):
            ranks[order[k]] = avg_pos
        i = j + 1
    return ranks


# ---------------------------------------------------------------------------
# weighting
# ---------------------------------------------------------------------------


def normalize_weights(weights: Sequence[float]) -> list[float]:
    """L1-normalize a weight vector preserving sign (``sum|w| == 1``).

    Rejects empty input, non-finite entries, ``bool`` entries, and a zero
    absolute sum (which makes normalization undefined).
    """
    vector = _as_finite_vector(weights, label="weights")
    if not vector:
        raise ValueError("weights must be a non-empty sequence")
    sum_abs = sum(abs(w) for w in vector)
    if sum_abs <= _EPS:
        raise ValueError("weights absolute sum must be positive")
    inv = 1.0 / sum_abs
    return [w * inv for w in vector]


def risk_budget_weights(signals: Mapping[str, Sequence[float]]) -> dict[str, float]:
    """Inverse-volatility risk-budget weights for a signal panel.

    Each signal's weight is proportional to ``1 / vol_i`` (the standard
    textbook risk-budget / naive risk-parity allocation for signals), so
    high-volatility signals receive a *smaller* absolute weight than
    low-volatility ones. The resulting weights are L1-normalized so
    ``sum|w| == 1``.

    Volatility is measured as the population standard deviation of each
    signal. A constant signal (zero volatility) collapses the panel: when
    every signal is constant the weights fall back to equal weights; when
    only some are constant, those receive a weight of zero (their inverse
    volatility is undefined) and the remaining non-constant signals share
    the budget.
    """
    validated = _validate_panel(signals)
    inv_vols: dict[str, float] = {}
    for name, series in validated.items():
        n = len(series)
        mean = sum(series) / n
        var = sum((x - mean) ** 2 for x in series) / n
        std = math.sqrt(var)
        if std <= _EPS:
            inv_vols[name] = 0.0
        else:
            inv_vols[name] = 1.0 / std
    total = sum(inv_vols.values())
    if total <= _EPS:
        # every signal is constant → equal weight fallback
        k = len(validated)
        eq = 1.0 / k
        return {name: eq for name in validated}
    inv_total = 1.0 / total
    return {name: inv_vols[name] * inv_total for name in validated}


# ---------------------------------------------------------------------------
# combination + result
# ---------------------------------------------------------------------------


def _equal_weights(names: list[str]) -> dict[str, float]:
    k = len(names)
    inv = 1.0 / k
    return {name: inv for name in names}


def _standardize_for_method(
    validated: dict[str, list[float]], method: str
) -> dict[str, list[float]]:
    if method == "zscore":
        return {name: standardize_signal(series) for name, series in validated.items()}
    if method == "rank":
        return {name: rank_signal(series) for name, series in validated.items()}
    # raw → use the series as-is (still finite per _validate_panel)
    return {name: list(series) for name, series in validated.items()}


@dataclass(frozen=True)
class SignalCombinationResult:
    """Aggregated signal-combination report.

    Attributes
    ----------
    combined:
        The per-point weighted composite series (length matches each input).
    weights:
        The L1-normalized weights actually applied (``sum|w| == 1``). When the
        caller did not supply explicit weights, these are the equal weights.
    standardized:
        The per-signal transformed series used inside the combination. For
        ``method == "zscore"`` these are z-scores; for ``"rank"`` average
        ranks; for ``"raw"`` the original series.
    method:
        The combination method actually used (one of ``"zscore"``, ``"rank"``,
        ``"raw"``).
    n_signals:
        Number of input signals.
    """

    combined: list[float]
    weights: dict[str, float]
    standardized: dict[str, list[float]]
    method: str
    n_signals: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "combined": list(self.combined),
            "weights": dict(self.weights),
            "standardized": {name: list(vec) for name, vec in self.standardized.items()},
            "method": self.method,
            "n_signals": self.n_signals,
        }


def combine_signals(
    signals: Mapping[str, Sequence[float]],
    weights: Mapping[str, float] | None = None,
    method: str = "zscore",
) -> SignalCombinationResult:
    """Combine a signal panel into a single weighted composite.

    Parameters
    ----------
    signals:
        ``{name: equal-length finite-numeric series}`` panel. Empty panels,
        length mismatches, non-sequence / dict / str values, ``bool`` entries
        and non-finite numbers raise :class:`ValueError`.
    weights:
        Optional ``{name: weight}`` mapping. When ``None`` equal weights are
        used (each ``1 / n_signals``, so ``|w|`` sums to 1). When supplied the
        keys must exactly match the signal names and the absolute sum must be
        positive; the weights are L1-normalized (sign-preserving) before use.
    method:
        One of ``"zscore"`` (default), ``"rank"`` or ``"raw"``. Anything else
        raises :class:`ValueError`.
    """
    if not isinstance(method, str) or method not in _VALID_METHODS:
        raise ValueError(f"method must be one of {sorted(_VALID_METHODS)}")
    validated = _validate_panel(signals)
    names = list(validated.keys())

    if weights is None:
        w = _equal_weights(names)
    else:
        if not isinstance(weights, Mapping):
            raise ValueError("weights must be a mapping of name to number")
        w_raw: dict[str, float] = {}
        for name in names:
            if name not in weights:
                raise ValueError(f"weights missing entry for signal '{name}'")
            value = weights[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"weights['{name}'] must be a finite number")
            value = float(value)
            if not math.isfinite(value):
                raise ValueError(f"weights['{name}'] must be a finite number")
            w_raw[name] = value
        # reject extra/unknown weight keys
        for key in weights:
            if key not in names:
                raise ValueError(f"weights has unknown signal '{key}'")
        normalized = normalize_weights([w_raw[name] for name in names])
        w = {name: normalized[i] for i, name in enumerate(names)}

    standardized = _standardize_for_method(validated, method)
    n_points = len(next(iter(standardized.values())))
    combined = [0.0] * n_points
    for name in names:
        vec = standardized[name]
        weight = w[name]
        for i in range(n_points):
            combined[i] += weight * vec[i]
    return SignalCombinationResult(
        combined=combined,
        weights=w,
        standardized=standardized,
        method=method,
        n_signals=len(names),
    )