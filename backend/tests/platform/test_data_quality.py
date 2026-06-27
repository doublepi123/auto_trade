"""Tests for P268 OHLCV data-quality diagnostics.

Pure unit tests — no FastAPI / app import. Covers the documented detection
categories (timestamp gaps/duplicates/out-of-order, stale prices, outlier
jumps, non-positive close, OHLC consistency), the valid-bars clean path,
invalid-input handling (ValueError on bad schema / non-finite / bad params),
frozen dataclass behaviour, and ``to_dict`` serialization.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from app.platform.data_quality import (
    BarQualityIssue,
    DataQualityResult,
    check_ohlc_consistency,
    check_price_quality,
    check_timestamp_quality,
    data_quality_report,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _good_bars(n: int = 5, interval: float = 60.0) -> list[dict]:
    """Build ``n`` clean bars at a fixed ``interval`` (seconds) starting at t0."""
    base = 1_700_000_000.0
    return [
        {
            "timestamp": base + i * interval,
            "open": 100.0 + i,
            "high": 101.0 + i,
            "low": 99.0 + i,
            "close": 100.5 + i,
            "volume": 1000 + i,
        }
        for i in range(n)
    ]


def _severities(issues: list[BarQualityIssue]) -> list[str]:
    return [i.severity for i in issues]


def _fields(issues: list[BarQualityIssue], field: str = "field") -> list[str]:
    return [getattr(i, field) for i in issues]


# ---------------------------------------------------------------------------
# check_timestamp_quality
# ---------------------------------------------------------------------------


def test_timestamp_quality_clean_bars_no_issues():
    issues = check_timestamp_quality(_good_bars(5))
    assert issues == []


def test_timestamp_quality_detects_duplicate():
    bars = _good_bars(4)
    bars[2] = {**bars[2], "timestamp": bars[1]["timestamp"]}
    issues = check_timestamp_quality(bars)
    assert len(issues) == 1
    assert issues[0].field == "timestamp"
    assert issues[0].severity == "critical"
    assert "duplicate" in issues[0].message.lower()


def test_timestamp_quality_detects_non_increasing():
    bars = _good_bars(4)
    # swap two adjacent timestamps -> out of order
    bars[1], bars[2] = bars[2], bars[1]
    issues = check_timestamp_quality(bars)
    assert len(issues) >= 1
    assert all(i.field == "timestamp" for i in issues)
    assert all(i.severity == "critical" for i in issues)


def test_timestamp_quality_detects_gap_when_expected_interval_given():
    bars = _good_bars(4, interval=60.0)
    # Insert a 200s gap (> 1.5 * 60 = 90s) between bars[1] and bars[2].
    bars[2] = {**bars[2], "timestamp": bars[1]["timestamp"] + 200.0}
    bars[3] = {**bars[3], "timestamp": bars[2]["timestamp"] + 60.0}
    issues = check_timestamp_quality(bars, expected_interval_seconds=60.0)
    gaps = [i for i in issues if "gap" in i.message.lower()]
    assert len(gaps) == 1
    assert gaps[0].severity == "warning"
    assert gaps[0].index == 2


def test_timestamp_quality_no_gap_when_interval_not_given():
    bars = _good_bars(4, interval=60.0)
    bars[2] = {**bars[2], "timestamp": bars[1]["timestamp"] + 200.0}
    bars[3] = {**bars[3], "timestamp": bars[2]["timestamp"] + 60.0}
    issues = check_timestamp_quality(bars)
    # No expected_interval_seconds -> gap detection is skipped.
    assert issues == []


def test_timestamp_quality_small_gap_within_tolerance():
    bars = _good_bars(4, interval=60.0)
    # 80s gap < 90s (1.5x) -> not flagged.
    bars[2] = {**bars[2], "timestamp": bars[1]["timestamp"] + 80.0}
    bars[3] = {**bars[3], "timestamp": bars[2]["timestamp"] + 60.0}
    issues = check_timestamp_quality(bars, expected_interval_seconds=60.0)
    assert issues == []


def test_timestamp_quality_accepts_iso_string_timestamps():
    bars = [
        {"timestamp": "2024-01-01T09:30:00", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},
        {"timestamp": "2024-01-01T09:31:00", "open": 1.5, "high": 2.0, "low": 1.0, "close": 1.8},
    ]
    issues = check_timestamp_quality(bars)
    assert issues == []


def test_timestamp_quality_iso_string_duplicate_detected():
    bars = [
        {"timestamp": "2024-01-01T09:30:00", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},
        {"timestamp": "2024-01-01T09:30:00", "open": 1.5, "high": 2.0, "low": 1.0, "close": 1.8},
    ]
    issues = check_timestamp_quality(bars)
    assert len(issues) == 1
    assert issues[0].severity == "critical"


# ---------------------------------------------------------------------------
# check_price_quality
# ---------------------------------------------------------------------------


def test_price_quality_clean_bars_no_issues():
    issues = check_price_quality(_good_bars(5))
    assert issues == []


def test_price_quality_detects_stale_close_above_window():
    # 4 consecutive equal closes with stale_window=3 -> flagged.
    bars = _good_bars(5)
    for i in range(1, 5):
        bars[i] = {**bars[i], "close": bars[0]["close"]}
    issues = check_price_quality(bars, stale_window=3)
    stale = [i for i in issues if "stale" in i.message.lower()]
    assert len(stale) >= 1
    assert all(i.severity == "warning" for i in stale)
    assert all(i.field == "close" for i in stale)


def test_price_quality_no_stale_within_window():
    # 3 consecutive equal closes with stale_window=3 -> NOT flagged (need > window).
    bars = _good_bars(4)
    for i in range(1, 3):
        bars[i] = {**bars[i], "close": bars[0]["close"]}
    issues = check_price_quality(bars, stale_window=3)
    stale = [i for i in issues if "stale" in i.message.lower()]
    assert stale == []


def test_price_quality_detects_outlier_jump():
    # Build an isolated single jump: flat closes around one spike.
    base = 1_700_000_000.0
    bars = [
        {"timestamp": base + i * 60.0, "open": 100.0, "high": 105.0,
         "low": 95.0, "close": c}
        for i, c in enumerate([100.0, 100.0, 100.0, 200.0, 100.0])
    ]
    issues = check_price_quality(bars, jump_threshold=0.2, stale_window=10)
    jumps = [i for i in issues if "jump" in i.message.lower()]
    # Only the spike (index 3) and its reversion (index 4) are jumps; here we
    # assert at least the spike is detected and is a warning.
    assert issues[0].index == 3
    assert all(i.severity == "warning" for i in jumps)
    # The 200 -> 100 reversion at index 4 is also a jump; ensure both are
    # captured to confirm bidirectional detection.
    assert any(i.index == 4 for i in jumps)


def test_price_quality_no_jump_within_threshold():
    bars = _good_bars(5)
    # Small move within threshold.
    bars[3] = {**bars[3], "close": bars[2]["close"] * 1.05}
    issues = check_price_quality(bars, jump_threshold=0.2)
    jumps = [i for i in issues if "jump" in i.message.lower()]
    assert jumps == []


def test_price_quality_detects_non_positive_close_critical():
    bars = _good_bars(5)
    bars[2] = {**bars[2], "close": 0.0}
    bars[3] = {**bars[3], "close": -5.0}
    issues = check_price_quality(bars)
    nonpos = [i for i in issues if "positive" in i.message.lower() or "non-positive" in i.message.lower()]
    assert len(nonpos) == 2
    assert all(i.severity == "critical" for i in nonpos)
    assert {i.index for i in nonpos} == {2, 3}


def test_price_quality_single_bar_no_jump_check():
    # With only 1 bar there is no previous close to compare -> no jump issue.
    issues = check_price_quality(_good_bars(1))
    assert issues == []


# ---------------------------------------------------------------------------
# check_ohlc_consistency
# ---------------------------------------------------------------------------


def test_ohlc_consistency_clean_bars_no_issues():
    issues = check_ohlc_consistency(_good_bars(5))
    assert issues == []


def test_ohlc_consistency_high_below_max_of_olc():
    # Construct a bar that violates ONLY the high constraint: open=close=100,
    # low=95, high=97 -> high(97) < max(100,100,95)=100 (violation), and
    # low(95) <= min(100,100,97)=95 (ok via equality).
    bars = [
        {"timestamp": 1.0, "open": 100.0, "high": 105.0, "low": 95.0, "close": 100.0},
        {"timestamp": 2.0, "open": 100.0, "high": 97.0, "low": 95.0, "close": 100.0},
        {"timestamp": 3.0, "open": 100.0, "high": 105.0, "low": 95.0, "close": 100.0},
    ]
    issues = check_ohlc_consistency(bars)
    assert len(issues) == 1
    assert issues[0].field == "high"
    assert issues[0].severity == "critical"
    assert issues[0].index == 1


def test_ohlc_consistency_low_above_min_of_olc():
    # Construct a bar that violates ONLY the low constraint: open=close=100,
    # high=105, low=103 -> high(105) >= max(100,100,103)=103 (ok), and
    # low(103) > min(100,100,105)=100 (violation).
    bars = [
        {"timestamp": 1.0, "open": 100.0, "high": 105.0, "low": 95.0, "close": 100.0},
        {"timestamp": 2.0, "open": 100.0, "high": 105.0, "low": 103.0, "close": 100.0},
        {"timestamp": 3.0, "open": 100.0, "high": 105.0, "low": 95.0, "close": 100.0},
    ]
    issues = check_ohlc_consistency(bars)
    assert len(issues) == 1
    assert issues[0].field == "low"
    assert issues[0].severity == "critical"
    assert issues[0].index == 1


def test_ohlc_consistency_high_equal_to_close_is_ok():
    bars = [
        {"timestamp": 1.0, "open": 100.0, "high": 105.0, "low": 95.0, "close": 100.0},
        {"timestamp": 2.0, "open": 100.0, "high": 102.0, "low": 95.0, "close": 102.0},
    ]
    issues = check_ohlc_consistency(bars)
    assert issues == []


def test_ohlc_consistency_low_equal_to_close_is_ok():
    bars = [
        {"timestamp": 1.0, "open": 100.0, "high": 105.0, "low": 95.0, "close": 100.0},
        {"timestamp": 2.0, "open": 100.0, "high": 105.0, "low": 98.0, "close": 98.0},
    ]
    issues = check_ohlc_consistency(bars)
    assert issues == []


# ---------------------------------------------------------------------------
# data_quality_report
# ---------------------------------------------------------------------------


def test_report_clean_bars_is_clean_true():
    rep = data_quality_report(_good_bars(5))
    assert rep.n_bars == 5
    assert rep.issue_count == 0
    assert rep.critical_count == 0
    assert rep.warning_count == 0
    assert rep.issues == []
    assert rep.is_clean is True


def test_report_aggregates_all_categories():
    bars = _good_bars(5)
    # Add a duplicate timestamp (critical), a stale close (warning),
    # an OHLC violation (critical).
    bars[4] = {**bars[4], "timestamp": bars[3]["timestamp"]}  # duplicate
    for i in range(1, 5):
        bars[i] = {**bars[i], "close": bars[0]["close"]}  # stale > 3
    bars[2] = {**bars[2], "high": 10.0}  # OHLC violation
    rep = data_quality_report(bars, stale_window=3, jump_threshold=0.2)
    assert rep.n_bars == 5
    assert rep.issue_count == len(rep.issues)
    assert rep.critical_count >= 2  # duplicate + OHLC
    assert rep.warning_count >= 1  # stale
    assert rep.is_clean is False


def test_report_to_dict_structure():
    rep = data_quality_report(_good_bars(3))
    d = rep.to_dict()
    assert set(d.keys()) == {
        "n_bars",
        "issue_count",
        "critical_count",
        "warning_count",
        "issues",
        "is_clean",
    }
    assert d["n_bars"] == 3
    assert d["issue_count"] == 0
    assert d["critical_count"] == 0
    assert d["warning_count"] == 0
    assert d["issues"] == []
    assert d["is_clean"] is True


def test_report_to_dict_with_issues_serializes():
    # Isolate a non-positive close: surround it with bars whose closes are 0
    # too (so no outlier jump — prev<=0 is skipped) while keeping OHLC
    # consistent (high >= max(open, close, low), low <= min(...)).
    bars = [
        {"timestamp": 1.0, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0},
        {"timestamp": 2.0, "open": 1.0, "high": 1.0, "low": 0.0, "close": 0.0},
    ]
    rep = data_quality_report(bars)
    d = rep.to_dict()
    assert d["is_clean"] is False
    assert d["critical_count"] == 1
    assert len(d["issues"]) == 1
    issue = d["issues"][0]
    assert set(issue.keys()) == {"index", "field", "severity", "message"}
    assert issue["index"] == 1
    assert issue["field"] == "close"
    assert issue["severity"] == "critical"


def test_report_uses_custom_params():
    bars = _good_bars(5, interval=60.0)
    # 200s gap
    bars[2] = {**bars[2], "timestamp": bars[1]["timestamp"] + 200.0}
    bars[3] = {**bars[3], "timestamp": bars[2]["timestamp"] + 60.0}
    bars[4] = {**bars[4], "timestamp": bars[3]["timestamp"] + 60.0}
    # With expected_interval_seconds the gap is flagged.
    rep = data_quality_report(bars, expected_interval_seconds=60.0)
    assert rep.issue_count >= 1
    # Without it, the gap is not flagged.
    rep2 = data_quality_report(bars)
    gaps = [i for i in rep2.issues if "gap" in i.message.lower()]
    assert gaps == []


# ---------------------------------------------------------------------------
# invalid-input handling (ValueError)
# ---------------------------------------------------------------------------


def test_invalid_bars_non_list_raises():
    with pytest.raises(ValueError):
        data_quality_report("not a list")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        data_quality_report({"a": 1})  # type: ignore[arg-type]


def test_invalid_bars_empty_raises():
    with pytest.raises(ValueError):
        data_quality_report([])


def test_invalid_bars_element_not_dict_raises():
    with pytest.raises(ValueError):
        data_quality_report([1.0, 2.0])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        data_quality_report(["x"])  # type: ignore[list-item]


def test_invalid_bars_pair_list_not_dict_raises():
    # A list of key/value pairs is iterable of pairs, so ``dict(b)`` would
    # silently coerce it into a valid bar dict. The validator must reject any
    # non-dict element — the API layer must never pre-coerce these.
    pair_list_bar = [
        ["timestamp", 1],
        ["open", 1],
        ["high", 1],
        ["low", 1],
        ["close", 1],
    ]
    with pytest.raises(ValueError):
        data_quality_report([pair_list_bar])  # type: ignore[list-item]


def test_invalid_bars_missing_required_field_raises():
    bars = [
        {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},  # no timestamp
    ]
    with pytest.raises(ValueError):
        data_quality_report(bars)


def test_invalid_bars_non_finite_price_raises():
    bars = _good_bars(2)
    bars[1] = {**bars[1], "close": float("nan")}
    with pytest.raises(ValueError):
        data_quality_report(bars)


def test_invalid_bars_inf_price_raises():
    bars = _good_bars(2)
    bars[1] = {**bars[1], "high": float("inf")}
    with pytest.raises(ValueError):
        data_quality_report(bars)


def test_invalid_bars_bool_price_rejected():
    bars = _good_bars(2)
    bars[1] = {**bars[1], "close": True}  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        data_quality_report(bars)


def test_invalid_bars_string_price_rejected():
    bars = _good_bars(2)
    bars[1] = {**bars[1], "close": "100.5"}  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        data_quality_report(bars)


def test_invalid_bars_dict_in_position_rejected():
    bars = _good_bars(2)
    bars[1] = {"x": 1.0}  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        data_quality_report(bars)


def test_invalid_expected_interval_non_positive_raises():
    with pytest.raises(ValueError):
        data_quality_report(_good_bars(3), expected_interval_seconds=0.0)
    with pytest.raises(ValueError):
        data_quality_report(_good_bars(3), expected_interval_seconds=-1.0)


def test_invalid_stale_window_raises():
    with pytest.raises(ValueError):
        data_quality_report(_good_bars(3), stale_window=0)
    with pytest.raises(ValueError):
        data_quality_report(_good_bars(3), stale_window=-1)


def test_invalid_jump_threshold_raises():
    with pytest.raises(ValueError):
        data_quality_report(_good_bars(3), jump_threshold=-0.1)


def test_invalid_jump_threshold_bool_rejected():
    with pytest.raises(ValueError):
        data_quality_report(_good_bars(3), jump_threshold=True)  # type: ignore[arg-type]


def test_invalid_expected_interval_bool_rejected():
    with pytest.raises(ValueError):
        data_quality_report(_good_bars(3), expected_interval_seconds=True)  # type: ignore[arg-type]


def test_invalid_stale_window_bool_rejected():
    with pytest.raises(ValueError):
        data_quality_report(_good_bars(3), stale_window=True)  # type: ignore[arg-type]


def test_check_timestamp_quality_invalid_bars_raises():
    with pytest.raises(ValueError):
        check_timestamp_quality([])
    with pytest.raises(ValueError):
        check_timestamp_quality("x")  # type: ignore[arg-type]


def test_check_price_quality_invalid_bars_raises():
    with pytest.raises(ValueError):
        check_price_quality([])
    with pytest.raises(ValueError):
        check_price_quality([{"open": 1.0}])


def test_check_ohlc_consistency_invalid_bars_raises():
    with pytest.raises(ValueError):
        check_ohlc_consistency([])
    with pytest.raises(ValueError):
        check_ohlc_consistency([{"open": 1.0}])


def test_volume_optional_not_required():
    bars = [
        {"timestamp": 1.0, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},
        {"timestamp": 2.0, "open": 1.5, "high": 2.0, "low": 1.0, "close": 1.8},
    ]
    rep = data_quality_report(bars)
    assert rep.is_clean is True
    assert rep.n_bars == 2


def test_volume_present_does_not_break_check():
    bars = _good_bars(3)
    rep = data_quality_report(bars)
    assert rep.is_clean is True


# ---------------------------------------------------------------------------
# dataclass behaviour (frozen / to_dict)
# ---------------------------------------------------------------------------


def test_bar_quality_issue_is_frozen():
    issue = BarQualityIssue(index=0, field="close", severity="critical", message="bad")
    with pytest.raises(dataclasses.FrozenInstanceError):
        issue.index = 5  # type: ignore[misc]


def test_bar_quality_issue_to_dict():
    issue = BarQualityIssue(index=2, field="high", severity="critical", message="high<low")
    d = issue.to_dict()
    assert d == {"index": 2, "field": "high", "severity": "critical", "message": "high<low"}


def test_data_quality_result_is_frozen():
    rep = data_quality_report(_good_bars(2))
    with pytest.raises(dataclasses.FrozenInstanceError):
        rep.n_bars = 999  # type: ignore[misc]


def test_bar_quality_issue_fields():
    fields = {f.name for f in dataclasses.fields(BarQualityIssue)}
    assert fields == {"index", "field", "severity", "message"}


def test_data_quality_result_fields():
    fields = {f.name for f in dataclasses.fields(DataQualityResult)}
    assert fields == {
        "n_bars",
        "issue_count",
        "critical_count",
        "warning_count",
        "issues",
        "is_clean",
    }


def test_to_dict_json_serializable_with_issues():
    import json

    bars = _good_bars(3)
    bars[1] = {**bars[1], "close": 0.0}
    bars[2] = {**bars[2], "timestamp": bars[1]["timestamp"]}
    rep = data_quality_report(bars)
    d = rep.to_dict()
    # Must round-trip through json without error.
    json.dumps(d)
