"""Small shared helpers for factor research diagnostics."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

_MAX_SERIES = 5000


def validate_series(values: Sequence[float], *, name: str, min_len: int = 1) -> list[float]:
    if isinstance(values, Mapping) or isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a sequence of finite numbers")
    try:
        raw = list(values)
    except TypeError as exc:
        raise ValueError(f"{name} must be a sequence of finite numbers") from exc
    if len(raw) < min_len:
        raise ValueError(f"{name} must contain at least {min_len} values")
    if len(raw) > _MAX_SERIES:
        raise ValueError(f"{name} must contain at most {_MAX_SERIES} values")
    out: list[float] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} entries must be finite numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{name} entries must be finite numbers")
        out.append(number)
    return out


def validate_pair(x: Sequence[float], y: Sequence[float], *, x_name: str = "x", y_name: str = "y") -> tuple[list[float], list[float]]:
    xs = validate_series(x, name=x_name, min_len=2)
    ys = validate_series(y, name=y_name, min_len=2)
    if len(xs) != len(ys):
        raise ValueError(f"{x_name} and {y_name} must have the same length")
    return xs, ys


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def std(values: Sequence[float], *, sample: bool = False) -> float:
    if not values:
        return 0.0
    if sample and len(values) < 2:
        return 0.0
    mu = mean(values)
    denom = len(values) - 1 if sample else len(values)
    return math.sqrt(sum((value - mu) ** 2 for value in values) / denom) if denom > 0 else 0.0


def pearson(x: Sequence[float], y: Sequence[float]) -> float:
    xs, ys = validate_pair(x, y)
    mx = mean(xs)
    my = mean(ys)
    sx = math.sqrt(sum((value - mx) ** 2 for value in xs))
    sy = math.sqrt(sum((value - my) ** 2 for value in ys))
    if sx == 0.0 or sy == 0.0:
        return 0.0
    return sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / (sx * sy)


def ranks(values: Sequence[float]) -> list[float]:
    vals = validate_series(values, name="values", min_len=1)
    indexed = sorted(enumerate(vals), key=lambda item: (item[1], item[0]))
    out = [0.0] * len(vals)
    i = 0
    while i < len(indexed):
        j = i + 1
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            out[indexed[k][0]] = avg_rank
        i = j
    return out


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    return pearson(ranks(x), ranks(y))


__all__ = ["mean", "pearson", "ranks", "spearman", "std", "validate_pair", "validate_series"]
