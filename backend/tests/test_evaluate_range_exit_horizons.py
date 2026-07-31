from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from app.core.backtest import BacktestBar


def _load_cli_module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "evaluate_range_exit_horizons.py"
    )
    spec = importlib.util.spec_from_file_location(
        "range_exit_horizon_cli_under_test",
        path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_cli_module()


def _bar(timestamp: datetime, *, open_: float = 101.0, high: float = 101.0,
         low: float = 99.0, close: float = 100.0) -> BacktestBar:
    return BacktestBar(
        timestamp=timestamp,
        open=open_,
        high=high,
        low=low,
        close=close,
    )


def _research_bars() -> list[BacktestBar]:
    result: list[BacktestBar] = []
    for day in (6, 7, 8, 9):
        entry_at = datetime(2026, 7, day, 14, 0, tzinfo=timezone.utc)
        result.extend((
            _bar(entry_at),
            _bar(
                entry_at + timedelta(minutes=20),
                open_=99.5,
                high=101.0,
                low=99.0,
                close=99.5,
            ),
            _bar(
                entry_at + timedelta(minutes=25),
                open_=101.0,
                high=103.0,
                low=100.5,
                close=102.0,
            ),
        ))
    return result


def _config(**overrides: object) -> object:
    values: dict[str, object] = {
        "symbol": "TEST.US",
        "market": "US",
        "buy_low": 100.0,
        "sell_high": 102.0,
        "stop_loss_pct": 0.0,
        "fee_rate": 0.0,
        "slippage_pct": 0.0,
        "quantity": 10.0,
        "horizons": (15, 30),
        "bar_timestamp": "end",
        "bar_minutes": 1,
        "entry_crossing_required": True,
        "max_entries_per_symbol_per_day": 1,
        "discovery_ratio": 0.5,
    }
    values.update(overrides)
    return cli.ResearchConfig(**values)


def test_loads_array_rows_sorts_and_normalizes_start_to_observation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "minute.json"
    source.write_text(json.dumps([
        ["2026-07-06T13:31:00+00:00", 101, 102, 100, 101],
        ["2026-07-06T13:30:00Z", 100, 101, 99, 100.5],
    ]), encoding="utf-8")

    bars = cli.load_range_bars(
        source,
        bar_timestamp="start",
        bar_minutes=5,
    )

    assert [bar.timestamp for bar in bars] == [
        datetime(2026, 7, 6, 13, 35, tzinfo=timezone.utc),
        datetime(2026, 7, 6, 13, 36, tzinfo=timezone.utc),
    ]
    assert bars[0].volume == 0.0


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"bars": []}, "non-empty array"),
        ([["2026-07-06T13:30:00+00:00", 100, 101, 99]], "expected"),
        ([["2026-07-06T13:30:00", 100, 101, 99, 100]], "UTC offset"),
        ([["2026-07-06T13:30:00+00:00", 100, 99, 101, 100]], "high"),
    ],
)
def test_rejects_invalid_input_rows(
    tmp_path: Path,
    payload: object,
    message: str,
) -> None:
    source = tmp_path / "bad.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        cli.load_range_bars(source, bar_timestamp="end", bar_minutes=1)


def test_split_is_chronological_and_keeps_exchange_local_days_whole() -> None:
    bars: list[BacktestBar] = []
    for offset in range(10):
        # 02:00 UTC is 10:00 in Hong Kong, including dates that are weekends;
        # this unit tests local-date grouping rather than holiday filtering.
        timestamp = datetime(2026, 1, 1 + offset, 2, 0, tzinfo=timezone.utc)
        bars.extend((_bar(timestamp), _bar(timestamp + timedelta(minutes=1))))

    split = cli.split_bars_by_local_day(
        list(reversed(bars)),
        market="HK",
        discovery_ratio=0.70,
    )

    assert len(split.discovery_dates) == 7
    assert len(split.holdout_dates) == 3
    assert split.discovery_dates[-1] < split.holdout_dates[0]
    assert {
        cli.trade_day_for("HK", bar.timestamp)
        for bar in split.discovery_bars
    } == set(split.discovery_dates)
    assert {
        cli.trade_day_for("HK", bar.timestamp)
        for bar in split.holdout_bars
    } == set(split.holdout_dates)


def test_exchange_local_date_filter_is_inclusive_and_whole_day() -> None:
    bars: list[BacktestBar] = []
    for offset in range(4):
        timestamp = datetime(2026, 1, 1 + offset, 2, 0, tzinfo=timezone.utc)
        bars.extend((_bar(timestamp), _bar(timestamp + timedelta(hours=1))))

    filtered = cli.filter_bars_by_local_date(
        bars,
        market="HK",
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 3),
    )

    assert len(filtered) == 4
    assert {
        cli.trade_day_for("HK", bar.timestamp)
        for bar in filtered
    } == {date(2026, 1, 2), date(2026, 1, 3)}


def test_horizon_report_is_deterministic_and_has_required_metrics() -> None:
    config = _config()

    first = cli.evaluate_range_exit_horizons(_research_bars(), config)
    second = cli.evaluate_range_exit_horizons(_research_bars(), config)

    assert first == second
    assert first["live_equivalent"] is False
    assert first["automatic_promotion_allowed"] is False
    horizons = cast(list[dict[str, object]], first["horizons"])
    assert [row["horizon_minutes"] for row in horizons] == [15, 30]

    short_full = cast(dict[str, object], horizons[0]["full"])
    long_full = cast(dict[str, object], horizons[1]["full"])
    assert short_full == {
        "session_days": 4,
        "positive_days": 0,
        "positive_day_ratio": 0.0,
        "total_net_pnl": -20.0,
        "closed_trades": 4,
        "win_rate_pct": 0.0,
        "profit_factor": 0.0,
        "exit_cause_counts": {"TIME_STOP": 4},
        "median_net_pnl": -5.0,
        "median_net_bps": -50.0,
        "avg_holding_minutes": 20.0,
        "fees_paid": 0.0,
        "max_drawdown_amount": 20.0,
        "max_drawdown_pct": 0.02,
        "engine_mark_to_market_total_pnl": -20.0,
        "open_position_at_end": False,
    }
    assert long_full["total_net_pnl"] == 80.0
    assert long_full["positive_day_ratio"] == 1.0
    assert long_full["exit_cause_counts"] == {"TARGET": 4}
    assert set(cast(dict[str, object], horizons[0]["discovery"])) == set(short_full)
    assert set(cast(dict[str, object], horizons[0]["holdout"])) == set(short_full)


def test_report_rejects_symbol_market_mismatch() -> None:
    with pytest.raises(ValueError, match="symbol suffix .HK does not match market US"):
        cli.evaluate_range_exit_horizons(
            _research_bars(),
            _config(symbol="0700.HK", market="US"),
        )


def test_report_date_filter_boundaries_reach_split_and_output() -> None:
    report = cli.evaluate_range_exit_horizons(
        _research_bars(),
        _config(
            start_date=date(2026, 7, 7),
            end_date=date(2026, 7, 9),
            trailing_stop_pct=1.25,
        ),
    )

    input_summary = cast(dict[str, object], report["input"])
    split = cast(dict[str, object], report["split"])
    parameters = cast(dict[str, object], report["parameters"])
    limitations = cast(list[str], report["limitations"])
    assert input_summary["source_bars"] == 12
    assert input_summary["evaluated_bars"] == 9
    assert split["all_dates"] == ["2026-07-07", "2026-07-08", "2026-07-09"]
    assert split["discovery_dates"] == ["2026-07-07"]
    assert split["holdout_dates"] == ["2026-07-08", "2026-07-09"]
    assert parameters["start_date"] == "2026-07-07"
    assert parameters["end_date"] == "2026-07-09"
    assert parameters["trailing_stop_pct"] == 1.25
    assert any(
        "realized-plus-unrealized" in item
        and "bar close" in item
        and "executable BBO" in item
        and "last-price pre-pause" in item
        for item in limitations
    )
    assert any(
        "non-auto-resumable" in item
        and "operator-resume" in item
        and "rest of the run" in item
        for item in limitations
    )
    assert any(
        "RTH_ONLY" in item
        and "DAILY_LOSS" in item
        and "PRICE_STOP" in item
        and "next open" in item
        for item in limitations
    )


def test_main_prints_json_and_writes_only_explicit_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "bars.json"
    output = tmp_path / "reports" / "result.json"
    rows: list[list[object]] = []
    for day in (6, 7):
        rows.extend((
            [f"2026-07-{day:02d}T14:00:00+00:00", 101, 101, 99, 100],
            [f"2026-07-{day:02d}T14:20:00+00:00", 100, 103, 99.5, 102],
        ))
    source.write_text(json.dumps(rows), encoding="utf-8")

    result = cli.main([
        "--input", str(source),
        "--output", str(output),
        "--symbol", "test.us",
        "--buy-low", "100",
        "--sell-high", "102",
        "--quantity", "10",
        "--horizons", "15",
        "--crossing",
        "--max-entries", "1",
        "--max-daily-loss", "0",
        "--bar-timestamp", "end",
        "--bar-minutes", "1",
    ])

    assert result == 0
    stdout_payload = json.loads(capsys.readouterr().out)
    assert stdout_payload == json.loads(output.read_text(encoding="utf-8"))
    assert stdout_payload["parameters"]["symbol"] == "TEST.US"
    assert stdout_payload["parameters"]["max_daily_loss"] == 0
    assert stdout_payload["fidelity_mode"] == "OHLC_BAR_CLOSE_APPROXIMATION"
