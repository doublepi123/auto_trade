"""P300: transfer entropy diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from app.platform.factor_utils import validate_pair


@dataclass(frozen=True)
class TransferEntropyResult:
    forward_te: float
    reverse_te: float
    net_te: float

    def to_dict(self) -> dict[str, Any]:
        return {"forward_te": self.forward_te, "reverse_te": self.reverse_te, "net_te": self.net_te}


def transfer_entropy_report(source: list[float], target: list[float], *, lag: int = 1, bins: int = 10) -> TransferEntropyResult:
    src, tgt = validate_pair(source, target, x_name="source", y_name="target")
    if isinstance(lag, bool) or not isinstance(lag, int) or lag < 1:
        raise ValueError("lag must be a positive int")
    if len(src) <= lag + 1:
        raise ValueError("source/target must contain more than lag+1 values")
    if isinstance(bins, bool) or not isinstance(bins, int) or bins < 2 or bins > 50:
        raise ValueError("bins must be an int in [2, 50]")
    forward = _te(src, tgt, lag, bins)
    reverse = _te(tgt, src, lag, bins)
    return TransferEntropyResult(forward, reverse, forward - reverse)


def _te(source: list[float], target: list[float], lag: int, bins: int) -> float:
    n = len(source) - lag
    src_edges = _edges(source, bins)
    tgt_edges = _edges(target, bins)
    p_next = _hist([_bucket(target[i + lag], tgt_edges) for i in range(n)], bins)
    p_curr_next: dict[int, float] = {}
    for i in range(n):
        key = _bucket(target[i], tgt_edges) * bins + _bucket(target[i + lag], tgt_edges)
        p_curr_next[key] = p_curr_next.get(key, 0.0) + 1.0 / n
    p_src_next: dict[tuple[int, int, int], float] = {}
    for i in range(n):
        key = (_bucket(source[i], src_edges), _bucket(target[i], tgt_edges), _bucket(target[i + lag], tgt_edges))
        p_src_next[key] = p_src_next.get(key, 0.0) + 1.0 / n
    te = 0.0
    for (s, curr, nxt), p in p_src_next.items():
        p_next_given_curr = p_curr_next.get(curr * bins + nxt, 1e-12)
        p_next_val = p_next.get(nxt, 1e-12)
        te += p * math.log((p / p_next_given_curr) / max(p_next_val, 1e-12), 2)
    return te


def _edges(values: list[float], bins: int) -> list[float]:
    lo = min(values)
    hi = max(values)
    if hi == lo:
        return [lo - 1.0, hi + 1.0]
    step = (hi - lo) / bins
    return [lo + i * step for i in range(bins + 1)]


def _bucket(value: float, edges: list[float]) -> int:
    for i in range(len(edges) - 1):
        if edges[i] <= value < edges[i + 1]:
            return i
    return len(edges) - 2


def _hist(values: list[int], bins: int) -> dict[int, float]:
    out = {b: 0.0 for b in range(bins)}
    for v in values:
        out[v] = out.get(v, 0.0) + 1.0
    total = len(values) or 1
    return {k: v / total for k, v in out.items()}


__all__ = ["TransferEntropyResult", "transfer_entropy_report"]
