from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

from app.domain.universe_selection import IndexCandidate


def _load_cli_module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "evaluate_rotation_walk_forward.py"
    )
    spec = importlib.util.spec_from_file_location(
        "rotation_walk_forward_cli_under_test",
        path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_cli_module()


def _evaluation_payload(
    *,
    data_scope: str,
    blockers: list[str] | None = None,
    missing_symbols: list[str] | None = None,
) -> dict[str, object]:
    performance = {
        "periods": 12,
        "annualized_return_pct": 12.5,
        "sharpe": 1.25,
        "max_drawdown_pct": 5.0,
        "win_rate_pct": 60.0,
        "average_turnover_pct": 20.0,
        "total_cost_pct": 0.5,
        "average_holdings": 8.0,
        "qqq_annualized_return_pct": 10.0,
        "qqq_sharpe": 1.0,
        "qqq_max_drawdown_pct": 6.0,
        "dia_annualized_return_pct": 8.0,
        "dia_sharpe": 0.8,
        "dia_max_drawdown_pct": 7.0,
        "excess_annualized_return_vs_qqq_pct": 2.5,
        "excess_annualized_return_vs_dia_pct": 4.5,
        "total_return_pct": 99.0,
    }
    return {
        "algorithm_version": "rotation-test-v1",
        "status": "COMPLETE",
        "benchmark_symbols": ["QQQ.US", "DIA.US"],
        "data_scope": data_scope,
        "survivorship_bias": True,
        "validation_periods": 12,
        "expanding_validation_min_training_periods": 12,
        "expanding_validation_fold_periods": 12,
        "selected_variant": "variant-a",
        "selected_variant_validation_passed": True,
        "validated_challenger_variant": "variant-a",
        "automatic_promotion_allowed": False,
        "promotion_blockers": blockers or [
            "ROTATION_FORWARD_OBSERVATIONS_REQUIRED"
        ],
        "point_in_time_data_missing_symbols": missing_symbols or [],
        "variants": [
            {
                "variant": {
                    "name": "variant-a",
                    "lookback_bars": 252,
                },
                "training_score": 1.0,
                "validation_passed": True,
                "validation_blockers": [],
                "expanding_validation_passed": True,
                "expanding_validation_blockers": [],
                "expanding_folds_passed": 2,
                "expanding_folds_total": 3,
                "expanding_validation": performance,
                "expanding_folds": [{"fold": 1, "detail": "large"}],
                "full": performance,
                "training": performance,
                "validation": performance,
            }
        ],
        "selected_variant_periods": [{"period": "large"}],
        "validated_challenger_periods": [{"period": "large"}],
    }


def test_compact_summary_keeps_decision_fields_without_fold_details() -> None:
    payload = _evaluation_payload(
        data_scope="POINT_IN_TIME_RESEARCH_CATALOG",
        missing_symbols=["OLD.US"],
    )

    summary = cli._summary(payload)

    assert summary["point_in_time_data_missing_symbols"] == ["OLD.US"]
    assert "selected_variant_periods" not in summary
    variant = summary["variants"][0]
    assert "expanding_folds" not in variant
    assert variant["expanding_folds_passed"] == 2
    assert variant["expanding_folds_total"] == 3
    assert variant["full"]["annualized_return_pct"] == 12.5
    assert "total_return_pct" not in variant["full"]


def test_report_marks_pit_primary_and_merges_fail_closed_blockers() -> None:
    point_in_time = _evaluation_payload(
        data_scope="POINT_IN_TIME_RESEARCH_CATALOG",
        blockers=["POINT_IN_TIME_MEMBER_DATA_PARTIAL"],
        missing_symbols=["OLD.US"],
    )
    current = _evaluation_payload(
        data_scope="CURRENT_CONSTITUENTS_ONLY",
        blockers=["CURRENT_CONSTITUENTS_SURVIVORSHIP_BIAS"],
    )
    errors = [
        {
            "symbol": "OLD.US",
            "catalog_scope": "HISTORICAL_CANDIDATE",
            "status": "FETCH_ERROR",
        },
        {
            "symbol": "QQQ.US",
            "catalog_scope": "BENCHMARK",
            "status": "EMPTY_RESPONSE",
        },
    ]

    report = cli._report(
        current_payload=current,
        point_in_time_payload=point_in_time,
        membership_history={"source_version": "history-v1"},
        acquisition_errors=errors,
        history_bars=1000,
        current_candidate_count=123,
        historical_candidate_count=48,
        full=False,
    )

    assert report["primary_evidence"] == "point_in_time_primary"
    assert report["research_only"] is True
    assert report["order_submission_allowed"] is False
    assert report["automatic_promotion_allowed"] is False
    assert report["membership_history"] == {
        "source_version": "history-v1"
    }
    assert report["acquisition"]["error_count"] == 2
    assert report["fail_closed_blockers"] == [
        "POINT_IN_TIME_MEMBER_DATA_PARTIAL",
        "ROTATION_HISTORICAL_MEMBER_DATA_ACQUISITION_PARTIAL",
        "ROTATION_BENCHMARK_DATA_ACQUISITION_PARTIAL",
        "ROTATION_FORWARD_OBSERVATIONS_REQUIRED",
    ]
    assert (
        report["point_in_time_primary"][
            "point_in_time_data_missing_symbols"
        ]
        == ["OLD.US"]
    )


def test_main_runs_current_and_pit_and_reports_fetch_failures(
    monkeypatch,
    capsys,
) -> None:
    current = IndexCandidate(
        "CUR.US",
        "Current",
        "Software",
        ("NASDAQ_100",),
    )
    historical = IndexCandidate(
        "OLD.US",
        "Former",
        "Software",
        ("NASDAQ_100",),
    )

    class _MembershipHistory:
        def metadata(
            self,
            candidates: tuple[IndexCandidate, ...],
        ) -> dict[str, object]:
            return {
                "source_version": "history-v1",
                "catalog_size": len(candidates),
            }

    membership_history = _MembershipHistory()

    class _Broker:
        def __init__(self) -> None:
            self.closed = False

        def get_forward_adjusted_candlesticks(
            self,
            symbol: str,
            period: str,
            count: int,
        ) -> list[object]:
            assert period == "DAY"
            assert count == 1000
            if symbol == "QQQ.US":
                return []
            return [object()]

        def close(self) -> None:
            self.closed = True

    broker = _Broker()
    calls: list[dict[str, object]] = []

    class _Result:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def to_dict(self) -> dict[str, object]:
            return self.payload

    def _evaluate(**kwargs: object) -> _Result:
        calls.append(kwargs)
        is_pit = kwargs.get("membership_history") is membership_history
        return _Result(
            _evaluation_payload(
                data_scope=(
                    "POINT_IN_TIME_RESEARCH_CATALOG"
                    if is_pit
                    else "CURRENT_CONSTITUENTS_ONLY"
                ),
                blockers=(
                    ["POINT_IN_TIME_MEMBER_DATA_PARTIAL"]
                    if is_pit
                    else ["CURRENT_CONSTITUENTS_SURVIVORSHIP_BIAS"]
                ),
                missing_symbols=["OLD.US"] if is_pit else [],
            )
        )

    def _historical_fetch(*args: object, **kwargs: object) -> list[object]:
        raise RuntimeError("sensitive broker failure text")

    monkeypatch.setattr(cli, "INDEX_CANDIDATE_CATALOG", (current,))
    monkeypatch.setattr(
        cli,
        "ROTATION_RESEARCH_CANDIDATE_CATALOG",
        (current, historical),
    )
    monkeypatch.setattr(
        cli,
        "INDEX_MEMBERSHIP_HISTORY",
        membership_history,
    )
    monkeypatch.setattr(cli, "BrokerGateway", lambda: broker)
    monkeypatch.setattr(
        cli,
        "_configure_longport_environment",
        lambda: None,
    )
    monkeypatch.setattr(
        cli,
        "historical_membership_end",
        lambda candidate: date(2024, 1, 2),
    )
    monkeypatch.setattr(
        cli,
        "historical_research_before",
        lambda candidate: cli.datetime(
            2024,
            1,
            2,
            12,
            tzinfo=cli.timezone.utc,
        ),
    )
    monkeypatch.setattr(
        cli,
        "historical_research_candlesticks",
        _historical_fetch,
    )
    monkeypatch.setattr(
        cli,
        "completed_daily_bars",
        lambda raw, **kwargs: raw,
    )
    monkeypatch.setattr(
        cli,
        "selection_config_from_settings",
        lambda: object(),
    )
    monkeypatch.setattr(cli, "evaluate_rotation_walk_forward", _evaluate)
    monkeypatch.setattr(
        cli.sys,
        "argv",
        ["evaluate_rotation_walk_forward.py"],
    )

    assert cli.main() == 0

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert len(calls) == 2
    assert calls[0]["candidates"] == (current,)
    assert calls[0].get("membership_history") is None
    assert calls[1]["candidates"] == (current, historical)
    assert calls[1]["membership_history"] is membership_history
    assert broker.closed is True
    assert "sensitive broker failure text" not in captured.out
    assert "sensitive broker failure text" not in captured.err

    errors = report["acquisition"]["errors"]
    assert errors == [
        {
            "bars_received": None,
            "catalog_scope": "HISTORICAL_CANDIDATE",
            "completed_bars": 0,
            "error_type": "RuntimeError",
            "membership_end": "2024-01-02",
            "requested_before": "2024-01-02T12:00:00+00:00",
            "requested_bars": 1000,
            "status": "FETCH_ERROR",
            "symbol": "OLD.US",
        },
        {
            "bars_received": 0,
            "catalog_scope": "BENCHMARK",
            "completed_bars": 0,
            "error_type": None,
            "membership_end": None,
            "requested_before": None,
            "requested_bars": 1000,
            "status": "EMPTY_RESPONSE",
            "symbol": "QQQ.US",
        },
    ]
    assert report["membership_history"] == {
        "source_version": "history-v1",
        "catalog_size": 2,
    }
    assert report["point_in_time_primary"][
        "point_in_time_data_missing_symbols"
    ] == ["OLD.US"]
    assert "expanding_folds" not in (
        report["point_in_time_primary"]["variants"][0]
    )


def test_open_membership_research_symbol_falls_back_to_recent_bars(
    monkeypatch,
) -> None:
    candidate = IndexCandidate(
        "GOOG.US",
        "Alphabet Class C",
        "Communication Services",
        ("NASDAQ_100",),
    )

    class _Broker:
        def __init__(self) -> None:
            self.requests: list[tuple[str, str, int]] = []

        def get_forward_adjusted_candlesticks(
            self,
            symbol: str,
            period: str,
            count: int,
        ) -> list[object]:
            self.requests.append((symbol, period, count))
            return [object()]

    broker = _Broker()
    monkeypatch.setattr(
        cli,
        "historical_membership_end",
        lambda candidate: None,
    )
    monkeypatch.setattr(
        cli,
        "historical_research_before",
        lambda candidate: None,
    )
    monkeypatch.setattr(
        cli,
        "historical_research_candlesticks",
        lambda broker, candidate, count: None,
    )
    monkeypatch.setattr(
        cli,
        "research_candidate_uses_recent_candlesticks",
        lambda candidate: True,
    )
    monkeypatch.setattr(
        cli,
        "completed_daily_bars",
        lambda raw, **kwargs: raw,
    )

    bars, errors = cli._collect_candidate_bars(
        broker,
        candidates=(candidate,),
        current_symbols=frozenset(),
        history_bars=1000,
        now=cli.datetime.now(cli.timezone.utc),
    )

    assert broker.requests == [("GOOG.US", "DAY", 1000)]
    assert len(bars["GOOG.US"]) == 1
    assert errors == []


def test_uncovered_research_symbol_does_not_fall_back_to_recent_bars(
    monkeypatch,
) -> None:
    candidate = IndexCandidate(
        "MISSING.US",
        "Missing",
        "Software",
        ("NASDAQ_100",),
    )

    class _Broker:
        def get_forward_adjusted_candlesticks(
            self,
            symbol: str,
            period: str,
            count: int,
        ) -> list[object]:
            raise AssertionError("uncovered symbol must fail closed")

    monkeypatch.setattr(
        cli,
        "historical_membership_end",
        lambda candidate: None,
    )
    monkeypatch.setattr(
        cli,
        "historical_research_before",
        lambda candidate: None,
    )
    monkeypatch.setattr(
        cli,
        "historical_research_candlesticks",
        lambda broker, candidate, count: None,
    )
    monkeypatch.setattr(
        cli,
        "research_candidate_uses_recent_candlesticks",
        lambda candidate: False,
    )

    bars, errors = cli._collect_candidate_bars(
        _Broker(),
        candidates=(candidate,),
        current_symbols=frozenset(),
        history_bars=1000,
        now=cli.datetime.now(cli.timezone.utc),
    )

    assert bars["MISSING.US"] == ()
    assert errors == [
        {
            "symbol": "MISSING.US",
            "catalog_scope": "HISTORICAL_CANDIDATE",
            "status": "HISTORICAL_CURSOR_UNAVAILABLE",
            "error_type": None,
            "requested_bars": 1000,
            "bars_received": None,
            "completed_bars": 0,
            "membership_end": None,
            "requested_before": None,
        }
    ]
