"""P268: OHLCV bar data-quality diagnostics.

A pure-Python (stdlib-only) family of checks that flag suspicious bar data
*before* it poisons a backtest or live strategy. The input ``bars`` is a list
of dicts shaped like the Longbridge / platform canonical bar:

    {
        "timestamp": int|float seconds-or-ISO-8601 string,
        "open":      finite number,
        "high":      finite number,
        "low":       finite number,
        "close":     finite number,
        "volume":    finite number (optional),
    }

Three detection families are provided, each returning a list of
:class:`BarQualityIssue` (zero or more). :func:`data_quality_report` aggregates
all three into a :class:`DataQualityResult`:

    - :func:`check_timestamp_quality` — duplicate / out-of-order / oversized gap
    - :func:`check_price_quality`     — stale close, outlier jump, non-positive close
    - :func:`check_ohlc_consistency`  — high/low bound violations

Severity is one of ``"warning"`` (suspicious but recoverable) or ``"critical"``
(unusable bar). Invalid schema / non-finite values / illegal parameters raise
:class:`ValueError` so the platform API can map them uniformly to HTTP 422.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any, Sequence

__all__ = [
    "BarQualityIssue",
    "DataQualityResult",
    "check_timestamp_quality",
    "check_price_quality",
    "check_ohlc_consistency",
    "data_quality_report",
]


# ---------------------------------------------------------------------------
# dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class BarQualityIssue:
    """A single data-quality issue found at a specific bar index.

    ``field`` is the offending bar key (e.g. ``"close"``, ``"timestamp"``,
    ``"high"``). ``severity`` is ``"warning"`` or ``"critical"``. ``message``
    is a short human-readable explanation.
    """

    index: int
    field: str
    severity: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "field": self.field,
            "severity": self.severity,
            "message": self.message,
        }


@dataclasses.dataclass(frozen=True)
class DataQualityResult:
    """Aggregated data-quality report returned by :func:`data_quality_report`."""

    n_bars: int
    issue_count: int
    critical_count: int
    warning_count: int
    issues: list[BarQualityIssue]
    is_clean: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_bars": self.n_bars,
            "issue_count": self.issue_count,
            "critical_count": self.critical_count,
            "warning_count": self.warning_count,
            "issues": [issue.to_dict() for issue in self.issues],
            "is_clean": self.is_clean,
        }


# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------


_REQUIRED_PRICE_FIELDS = ("open", "high", "low", "close")


def _is_real_number(value: Any) -> bool:
    """True iff ``value`` is a non-bool finite int/float."""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _parse_timestamp(value: Any, index: int) -> float:
    """Coerce a bar ``timestamp`` to epoch seconds (float).

    Accepts:

      - non-bool ``int`` / ``float`` -> used directly (must be finite).
      - ``str`` -> parsed via :func:`datetime.fromisoformat` and converted to
        epoch seconds.

    Raises :class:`ValueError` on any other type or malformed string.
    """
    if isinstance(value, bool):
        raise ValueError(f"bars[{index}].timestamp must be a number or ISO string")
    if isinstance(value, (int, float)):
        ts = float(value)
        if not math.isfinite(ts):
            raise ValueError(f"bars[{index}].timestamp must be finite")
        return ts
    if isinstance(value, str):
        ts_str = value.strip()
        if not ts_str:
            raise ValueError(f"bars[{index}].timestamp must be a non-empty ISO string")
        import datetime as _dt

        try:
            dt = _dt.datetime.fromisoformat(ts_str)
        except ValueError as exc:
            raise ValueError(
                f"bars[{index}].timestamp is not a valid ISO-8601 string"
            ) from exc
        return dt.timestamp()
    raise ValueError(f"bars[{index}].timestamp must be a number or ISO string")


def _normalize_bars(bars: Any) -> list[dict[str, Any]]:
    """Validate and normalize ``bars`` to a non-empty list of bar dicts.

    Each bar must:

      - be a ``dict`` (not a list / scalar / nested dict used in its place),
      - carry a ``timestamp`` (int / float / ISO string),
      - carry finite numeric ``open`` / ``high`` / ``low`` / ``close``,
      - ``volume`` is optional but, if present, must be a finite number.

    Raises :class:`ValueError` on every violation for a uniform contract.
    """
    # bool is a subclass of int — but a bool is clearly not a bar list. Reject
    # it (and any other non-list, non-tuple sequence-like) up front. dicts are
    # iterable but iterating yields keys, which would silently produce junk.
    if isinstance(bars, (dict, str, bytes)) or not isinstance(bars, (list, tuple)):
        raise ValueError("bars must be a non-empty list of bar dicts")
    if len(bars) == 0:
        raise ValueError("bars must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    for i, raw in enumerate(bars):
        # Reject non-dict entries at any bar position (string / number / dict
        # used as a top-level bar). Nested dicts for legitimate fields are fine.
        if not isinstance(raw, dict) or isinstance(raw, (str, bytes)):
            raise ValueError(f"bars[{i}] must be a dict")
        bar: dict[str, Any] = {}
        # timestamp
        if "timestamp" not in raw:
            raise ValueError(f"bars[{i}] missing required field: timestamp")
        bar["timestamp"] = _parse_timestamp(raw["timestamp"], i)
        # OHLC price fields
        for field in _REQUIRED_PRICE_FIELDS:
            if field not in raw:
                raise ValueError(f"bars[{i}] missing required field: {field}")
            value = raw[field]
            if not _is_real_number(value):
                raise ValueError(f"bars[{i}].{field} must be a finite number")
            bar[field] = float(value)
        # volume (optional)
        if "volume" in raw:
            vol = raw["volume"]
            if not _is_real_number(vol):
                raise ValueError(f"bars[{i}].volume must be a finite number")
            bar["volume"] = float(vol)
        # Preserve any extra keys verbatim (forward compatibility).
        for k, v in raw.items():
            if k not in bar:
                bar[k] = v
        normalized.append(bar)
    return normalized


def _validate_expected_interval(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("expected_interval_seconds must be a positive number")
    if not isinstance(value, (int, float)):
        raise ValueError("expected_interval_seconds must be a positive number")
    f = float(value)
    if not math.isfinite(f) or f <= 0.0:
        raise ValueError("expected_interval_seconds must be a positive number")
    return f


def _validate_stale_window(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("stale_window must be a positive int")
    if value < 1:
        raise ValueError("stale_window must be a positive int")
    return value


def _validate_jump_threshold(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("jump_threshold must be a non-negative number")
    f = float(value)
    if not math.isfinite(f) or f < 0.0:
        raise ValueError("jump_threshold must be a non-negative number")
    return f


# ---------------------------------------------------------------------------
# check_timestamp_quality
# ---------------------------------------------------------------------------


def check_timestamp_quality(
    bars: Sequence[dict[str, Any]],
    expected_interval_seconds: float | None = None,
) -> list[BarQualityIssue]:
    """Detect timestamp-level quality issues.

    Flags:

      - **duplicate timestamps** — two consecutive bars share the same epoch
        (critical).
      - **non-increasing / out-of-order timestamps** — ``ts[i] < ts[i-1]``
        (critical).
      - **oversized gaps** — when ``expected_interval_seconds`` is provided,
        a gap larger than ``1.5``× the expected interval is flagged (warning).

    Returns a list of :class:`BarQualityIssue` (possibly empty).
    """
    normalized = _normalize_bars(bars)
    interval = _validate_expected_interval(expected_interval_seconds)
    issues: list[BarQualityIssue] = []
    timestamps = [bar["timestamp"] for bar in normalized]
    for i in range(1, len(timestamps)):
        prev_ts = timestamps[i - 1]
        cur_ts = timestamps[i]
        if cur_ts == prev_ts:
            issues.append(
                BarQualityIssue(
                    index=i,
                    field="timestamp",
                    severity="critical",
                    message=f"duplicate timestamp at index {i} (matches index {i - 1})",
                )
            )
            continue
        if cur_ts < prev_ts:
            issues.append(
                BarQualityIssue(
                    index=i,
                    field="timestamp",
                    severity="critical",
                    message=(
                        f"non-increasing timestamp at index {i} "
                        f"({cur_ts} < previous {prev_ts})"
                    ),
                )
            )
            continue
        if interval is not None:
            gap = cur_ts - prev_ts
            if gap > 1.5 * interval:
                issues.append(
                    BarQualityIssue(
                        index=i,
                        field="timestamp",
                        severity="warning",
                        message=(
                            f"timestamp gap of {gap:.3f}s at index {i} exceeds "
                            f"1.5x expected interval ({interval:.3f}s)"
                        ),
                    )
                )
    return issues


# ---------------------------------------------------------------------------
# check_price_quality
# ---------------------------------------------------------------------------


def check_price_quality(
    bars: Sequence[dict[str, Any]],
    stale_window: int = 3,
    jump_threshold: float = 0.2,
) -> list[BarQualityIssue]:
    """Detect price-level quality issues on the ``close`` series.

    Flags:

      - **non-positive close** — ``close <= 0`` (critical).
      - **stale close** — more than ``stale_window`` consecutive identical
        closes (warning).
      - **outlier jump** — ``|close[i] / close[i-1] - 1| > jump_threshold``
        (warning). Skipped when the previous close is non-positive.

    Returns a list of :class:`BarQualityIssue` (possibly empty).
    """
    normalized = _normalize_bars(bars)
    window = _validate_stale_window(stale_window)
    threshold = _validate_jump_threshold(jump_threshold)
    issues: list[BarQualityIssue] = []
    closes = [bar["close"] for bar in normalized]

    # Non-positive close (critical).
    for i, c in enumerate(closes):
        if c <= 0.0:
            issues.append(
                BarQualityIssue(
                    index=i,
                    field="close",
                    severity="critical",
                    message=f"non-positive close at index {i} ({c})",
                )
            )

    # Stale close run detection: flag every bar that extends a run of equal
    # consecutive closes *longer than* ``stale_window``. A run of length L
    # triggers for indices [start + window, start + L - 1].
    n = len(closes)
    i = 0
    while i < n:
        j = i + 1
        while j < n and closes[j] == closes[i]:
            j += 1
        run_len = j - i  # number of identical closes from i .. j-1
        if run_len > window:
            for k in range(i + window, j):
                issues.append(
                    BarQualityIssue(
                        index=k,
                        field="close",
                        severity="warning",
                        message=(
                            f"stale close at index {k}: identical to previous "
                            f"> {window} bars"
                        ),
                    )
                )
        i = j

    # Outlier jump (warning) — only when both previous and current closes are
    # positive. Non-positive closes are already flagged as critical above, so
    # including them here would double-report the same bad bar as both a
    # "non-positive close" (critical) and an "outlier jump" (warning).
    for i in range(1, n):
        prev = closes[i - 1]
        cur = closes[i]
        if prev <= 0.0 or cur <= 0.0:
            continue
        change = abs(cur / prev - 1.0)
        if change > threshold:
            issues.append(
                BarQualityIssue(
                    index=i,
                    field="close",
                    severity="warning",
                    message=(
                        f"outlier jump at index {i}: |close ratio - 1| = "
                        f"{change:.4f} > threshold {threshold:.4f}"
                    ),
                )
            )

    return issues


# ---------------------------------------------------------------------------
# check_ohlc_consistency
# ---------------------------------------------------------------------------


def check_ohlc_consistency(
    bars: Sequence[dict[str, Any]],
) -> list[BarQualityIssue]:
    """Detect OHLC internal consistency violations.

    Flags (critical):

      - ``high < max(open, close, low)``
      - ``low > min(open, close, high)``

    Equality (``high == close`` etc.) is allowed. Returns a list of
    :class:`BarQualityIssue` (possibly empty).
    """
    normalized = _normalize_bars(bars)
    issues: list[BarQualityIssue] = []
    for i, bar in enumerate(normalized):
        o = bar["open"]
        h = bar["high"]
        low = bar["low"]
        c = bar["close"]
        if h < max(o, c, low):
            issues.append(
                BarQualityIssue(
                    index=i,
                    field="high",
                    severity="critical",
                    message=(
                        f"OHLC violation at index {i}: high ({h}) < "
                        f"max(open={o}, close={c}, low={low})"
                    ),
                )
            )
        if low > min(o, c, h):
            issues.append(
                BarQualityIssue(
                    index=i,
                    field="low",
                    severity="critical",
                    message=(
                        f"OHLC violation at index {i}: low ({low}) > "
                        f"min(open={o}, close={c}, high={h})"
                    ),
                )
            )
    return issues


# ---------------------------------------------------------------------------
# all-in-one report
# ---------------------------------------------------------------------------


def data_quality_report(
    bars: Sequence[dict[str, Any]],
    expected_interval_seconds: float | None = None,
    stale_window: int = 3,
    jump_threshold: float = 0.2,
) -> DataQualityResult:
    """Aggregate all data-quality checks into a single :class:`DataQualityResult`.

    Parameters are validated up front; invalid parameters raise
    :class:`ValueError`. The bars are normalized once and the same normalized
    list is reused across all three check families.
    """
    normalized = _normalize_bars(bars)
    interval = _validate_expected_interval(expected_interval_seconds)
    window = _validate_stale_window(stale_window)
    threshold = _validate_jump_threshold(jump_threshold)

    issues: list[BarQualityIssue] = []
    issues.extend(check_timestamp_quality(normalized, expected_interval_seconds=interval))
    issues.extend(check_price_quality(normalized, stale_window=window, jump_threshold=threshold))
    issues.extend(check_ohlc_consistency(normalized))

    critical = sum(1 for i in issues if i.severity == "critical")
    warning = sum(1 for i in issues if i.severity == "warning")
    return DataQualityResult(
        n_bars=len(normalized),
        issue_count=len(issues),
        critical_count=critical,
        warning_count=warning,
        issues=issues,
        is_clean=(len(issues) == 0),
    )
