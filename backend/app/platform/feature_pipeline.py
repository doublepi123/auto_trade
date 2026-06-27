"""P285: safe declarative feature pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence
import math
from typing import Any

from app.platform.factor_utils import mean, std


@dataclass(frozen=True)
class FeaturePipelineResult:
    features: dict[str, dict[str, list[float | None]]]
    feature_count: int
    asset_count: int
    length: int

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def run_feature_pipeline(price_panel: dict[str, list[float]], features: list[dict[str, Any]]) -> FeaturePipelineResult:
    panel: dict[str, list[float | None]] = {name: list(series) for name, series in _validate_panel(price_panel).items()}
    if not isinstance(features, list) or not features:
        raise ValueError("features must be non-empty")
    outputs: dict[str, dict[str, list[float | None]]] = {}
    for spec in features:
        if not isinstance(spec, dict):
            raise ValueError("feature specs must be dicts")
        name = str(spec.get("name"))
        op = str(spec.get("op"))
        source: dict[str, list[float | None]] | None = outputs.get(str(spec.get("input"))) if "input" in spec else panel
        if source is None:
            raise ValueError("input feature does not exist")
        window_raw = spec.get("window", 1)
        if isinstance(window_raw, bool) or not isinstance(window_raw, int):
            raise ValueError("window must be an int")
        window = window_raw
        if window < 1:
            raise ValueError("window must be positive")
        if op == "return":
            result = {asset: _returns(series, window) for asset, series in source.items()}
        elif op == "sma":
            result = {asset: _sma(series, window) for asset, series in source.items()}
        elif op == "lag":
            result = {asset: _lag(series, window) for asset, series in source.items()}
        elif op == "delta":
            result = {asset: _delta(series, window) for asset, series in source.items()}
        elif op == "zscore":
            result = {asset: _zscore(series, window) for asset, series in source.items()}
        elif op == "rank":
            result = _rank_panel(source)
        else:
            raise ValueError("unknown feature op")
        outputs[name] = result
    length = len(next(iter(panel.values())))
    return FeaturePipelineResult(outputs, len(outputs), len(panel), length)


def _validate_panel(panel: dict[str, list[float]]) -> dict[str, list[float]]:
    if not isinstance(panel, dict) or not panel:
        raise ValueError("price_panel must be non-empty")
    out: dict[str, list[float]] = {}
    for key, values in panel.items():
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise ValueError("panel series must be sequences")
        if not values:
            raise ValueError("panel series must be non-empty")
        series: list[float] = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("panel entries must be finite numbers")
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("panel entries must be finite numbers")
            series.append(number)
        out[str(key)] = series
    lengths = {len(v) for v in out.values()}
    if len(lengths) != 1:
        raise ValueError("panel series must have equal length")
    return out


def _returns(series: Sequence[float | None], window: int) -> list[float | None]:
    out: list[float | None] = []
    for i, value in enumerate(series):
        prev = series[i - window] if i >= window else None
        out.append(None if i < window or value is None or prev is None or prev == 0 else float(value) / float(prev) - 1.0)
    return out


def _sma(series: Sequence[float | None], window: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(series)):
        vals = list(series[i - window + 1 : i + 1])
        out.append(None if i + 1 < window or any(x is None for x in vals) else mean([float(x) for x in vals if x is not None]))
    return out


def _lag(series: Sequence[float | None], window: int) -> list[float | None]:
    return [None if i < window else series[i - window] for i in range(len(series))]


def _delta(series: Sequence[float | None], window: int) -> list[float | None]:
    out: list[float | None] = []
    for i, value in enumerate(series):
        prev = series[i - window] if i >= window else None
        out.append(None if i < window or value is None or prev is None else float(value) - float(prev))
    return out


def _zscore(series: Sequence[float | None], window: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(series)):
        vals = series[i-window+1:i+1]
        if i + 1 < window or any(x is None for x in vals):
            out.append(None)
        else:
            nums = [float(x) for x in vals if x is not None]
            sigma = std(nums)
            out.append(0.0 if sigma == 0 else (nums[-1] - mean(nums)) / sigma)
    return out


def _rank_panel(panel: dict[str, list[float | None]]) -> dict[str, list[float | None]]:
    assets = list(panel)
    length = len(next(iter(panel.values())))
    out: dict[str, list[float | None]] = {asset: [None] * length for asset in assets}
    for i in range(length):
        vals = [(asset, panel[asset][i]) for asset in assets if panel[asset][i] is not None]
        ordered = sorted(vals, key=lambda item: float(item[1]) if item[1] is not None else 0.0)
        denom = max(1, len(ordered) - 1)
        for rank, (asset, _) in enumerate(ordered):
            out[asset][i] = rank / denom
    return out


__all__ = ["FeaturePipelineResult", "run_feature_pipeline"]
