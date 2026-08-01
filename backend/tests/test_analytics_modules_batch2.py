from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, cast

import pytest
from sqlalchemy.orm import Session

from app.services import asymmetry_service
from app.services import capital_efficiency_service
from app.services import daily_consistency_service
from app.services import drawdown_duration_service
from app.services import exit_efficiency_service
from app.services import fee_drag_service
from app.services import first_trade_service
from app.services import intraday_seasonality_service
from app.services import loss_containment_service
from app.services import milestone_service
from app.services.analytics_trade_sample_service import AnalyticsTradeSample
from app.services.asymmetry_service import AsymmetryService
from app.services.capital_efficiency_service import CapitalEfficiencyService
from app.services.daily_consistency_service import DailyConsistencyService
from app.services.daily_pnl_service import ClosedRoundTrip
from app.services.drawdown_duration_service import DrawdownDurationService
from app.services.exit_efficiency_service import ExitEfficiencyService
from app.services.fee_drag_service import FeeDragService
from app.services.first_trade_service import FirstTradeService
from app.services.intraday_seasonality_service import IntradaySeasonalityService
from app.services.loss_containment_service import LossContainmentService
from app.services.milestone_service import MilestoneService
from app.services.statistics_quality_service import StatisticsQualityData


_NOW = datetime(2026, 1, 20, 12, 0, tzinfo=timezone.utc)
_DB = cast(Session, object())


def _trade(
    *,
    exit_order_id: int,
    symbol: str = "AAPL.US",
    exit_at: datetime | None = None,
    quantity: float = 1.0,
    entry_price: float = 100.0,
    exit_price: float = 110.0,
    gross_pnl: float = 10.0,
    est_fees: float = 2.0,
    net_pnl: float = 8.0,
    fee_source: str = "ACTUAL",
    exit_cause: str = "TAKE_PROFIT",
    mfe_amount: float | None = None,
    mae_amount: float | None = None,
    excursion_source: str = "NOT_REQUESTED",
    excursion_interior_observation_count: int = 0,
    excursion_max_gap_seconds: float | None = None,
    holding_seconds: float = 3600.0,
) -> ClosedRoundTrip:
    resolved_exit = exit_at or (_NOW - timedelta(days=1))
    return ClosedRoundTrip(
        symbol=symbol,
        side="long",
        entry_order_id=exit_order_id * 2 - 1,
        exit_order_id=exit_order_id,
        entry_at=resolved_exit - timedelta(seconds=holding_seconds),
        exit_at=resolved_exit,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
        gross_pnl=gross_pnl,
        est_fees=est_fees,
        net_pnl=net_pnl,
        holding_seconds=holding_seconds,
        exit_broker_order_id=f"exit-{exit_order_id}",
        fee_source=fee_source,
        actual_fees=est_fees if fee_source == "ACTUAL" else None,
        exit_cause=exit_cause,
        mfe_amount=mfe_amount,
        mae_amount=mae_amount,
        mfe_pct=(
            mfe_amount / (entry_price * quantity) * 100
            if mfe_amount is not None
            else None
        ),
        mae_pct=(
            mae_amount / (entry_price * quantity) * 100
            if mae_amount is not None
            else None
        ),
        excursion_source=excursion_source,
        excursion_interior_observation_count=(
            excursion_interior_observation_count
        ),
        excursion_max_gap_seconds=excursion_max_gap_seconds,
    )


def _sample(
    trades: list[ClosedRoundTrip],
    *,
    quality: StatisticsQualityData | None = None,
) -> AnalyticsTradeSample:
    currencies = tuple(
        sorted({"HKD" if trade.symbol.endswith(".HK") else "USD" for trade in trades})
    )
    return AnalyticsTradeSample(
        trades=trades,
        quality=quality or StatisticsQualityData(),
        from_dt=_NOW - timedelta(days=90),
        to_dt=_NOW,
        currencies=currencies,
    )


def _install_sample(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    sample: AnalyticsTradeSample,
) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def _load(_db: object, **kwargs: Any) -> AnalyticsTradeSample:
        seen.update(kwargs)
        return sample

    monkeypatch.setattr(module, "load_analytics_trade_sample", _load)
    return seen


_ANALYSES: list[tuple[Any, Callable[[], dict[str, Any]]]] = [
    (asymmetry_service, lambda: AsymmetryService(_DB).analyze()),
    (capital_efficiency_service, lambda: CapitalEfficiencyService(_DB).analyze()),
    (daily_consistency_service, lambda: DailyConsistencyService(_DB).summary()),
    (drawdown_duration_service, lambda: DrawdownDurationService(_DB).analyze()),
    (exit_efficiency_service, lambda: ExitEfficiencyService(_DB).summary()),
    (fee_drag_service, lambda: FeeDragService(_DB).summary()),
    (first_trade_service, lambda: FirstTradeService(_DB).summary()),
    (intraday_seasonality_service, lambda: IntradaySeasonalityService(_DB).analyze()),
    (loss_containment_service, lambda: LossContainmentService(_DB).summary()),
    (milestone_service, lambda: MilestoneService(_DB).track()),
]


@pytest.mark.parametrize(("module", "analyze"), _ANALYSES)
def test_every_response_branch_exposes_statistics_quality(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    analyze: Callable[[], dict[str, Any]],
) -> None:
    quality = StatisticsQualityData(
        status="UNRESOLVED",
        unresolved_issue_count=1,
        omitted_day_count=1,
    )
    _install_sample(monkeypatch, module, _sample([], quality=quality))

    result = analyze()

    assert result["error"]
    assert result["statistics_quality"]["status"] == "UNRESOLVED"
    assert result["totals_comparable"] is True


@pytest.mark.parametrize(("module", "analyze"), _ANALYSES)
def test_money_analytics_fail_closed_for_mixed_currencies(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    analyze: Callable[[], dict[str, Any]],
) -> None:
    trades = [
        _trade(exit_order_id=1),
        _trade(exit_order_id=2, symbol="0700.HK"),
    ]
    _install_sample(monkeypatch, module, _sample(trades))

    result = analyze()

    assert result["currency"] == "MIXED"
    assert result["totals_comparable"] is False
    assert "Mixed USD/HKD" in result["error"]
    assert result["statistics_quality"]["status"] == "COMPLETE"


def test_asymmetry_ratio_uses_unrounded_side_means(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pnls = [0.004] * 5 + [-0.006] * 5
    trades = [
        _trade(exit_order_id=index, net_pnl=pnl)
        for index, pnl in enumerate(pnls, start=1)
    ]
    _install_sample(monkeypatch, asymmetry_service, _sample(trades))

    result = AsymmetryService(_DB).analyze()

    assert result["win_stats"]["avg"] == 0.0
    assert result["loss_stats"]["avg"] == -0.01
    assert result["asymmetry_ratio"] == pytest.approx(0.6667)


@pytest.mark.parametrize(
    ("pnls", "missing_side", "assessment_fragment"),
    (
        ([1.0] * 10, "loss_stats", "No losing trades"),
        ([-1.0] * 10, "win_stats", "No winning trades"),
    ),
)
def test_asymmetry_is_undefined_when_either_side_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    pnls: list[float],
    missing_side: str,
    assessment_fragment: str,
) -> None:
    trades = [
        _trade(exit_order_id=index, net_pnl=pnl)
        for index, pnl in enumerate(pnls, start=1)
    ]
    _install_sample(monkeypatch, asymmetry_service, _sample(trades))

    result = AsymmetryService(_DB).analyze()

    assert result["asymmetry_ratio"] is None
    assert result[missing_side]["count"] == 0
    assert assessment_fragment in result["assessment"]
    assert "undefined" in result["assessment"]


def test_asymmetry_exposes_unambiguous_largest_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pnls = [1.0] * 5 + [-1.0, -2.0, -3.0, -4.0, -5.0]
    trades = [
        _trade(exit_order_id=index, net_pnl=pnl)
        for index, pnl in enumerate(pnls, start=1)
    ]
    _install_sample(monkeypatch, asymmetry_service, _sample(trades))

    result = AsymmetryService(_DB).analyze()
    loss_stats = result["loss_stats"]

    assert loss_stats["largest_magnitude"] == -5.0
    assert loss_stats["smallest_magnitude"] == -1.0
    assert loss_stats["max"] == -1.0
    assert loss_stats["min"] == -5.0


def test_drawdown_separates_left_censored_completed_and_open_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pnls = [-2.0, 2.0, 1.0, -1.0, 1.0, 1.0, -1.0, -1.0, 0.0, 0.0]
    trades = [
        _trade(exit_order_id=index, net_pnl=pnl)
        for index, pnl in enumerate(pnls, start=1)
    ]
    _install_sample(
        monkeypatch,
        drawdown_duration_service,
        _sample(trades),
    )

    result = DrawdownDurationService(_DB).analyze()

    assert result["evidence_scope"] == "WINDOW_LOCAL_UNDERWATER_RUNS"
    assert result["pre_window_high_water_known"] is False
    assert result["left_censored"] is True
    assert result["excluded_left_censored_duration"] == 1
    assert result["completed_episodes"] == 1
    assert result["episodes"] == 1
    assert result["durations"] == [1]
    assert result["histogram"] == [{"duration": 1, "count": 1}]
    assert result["summary"] == {
        "avg": 1.0,
        "max": 1,
        "median": 1.0,
        "p25": 1.0,
        "p75": 1.0,
    }
    assert result["is_underwater"] is True
    assert result["current_open_duration"] == 4
    assert result["observed_underwater_trade_count"] == 6
    assert result["pct_time_underwater"] == 60.0


def test_drawdown_uses_statistics_median_and_inclusive_quantiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pnls = [10.0, -1.0, 1.0, -1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0]
    trades = [
        _trade(exit_order_id=index, net_pnl=pnl)
        for index, pnl in enumerate(pnls, start=1)
    ]
    _install_sample(
        monkeypatch,
        drawdown_duration_service,
        _sample(trades),
    )

    result = DrawdownDurationService(_DB).analyze()

    assert result["durations"] == [1, 2]
    assert result["summary"]["median"] == 1.5
    assert result["summary"]["p25"] == 1.2
    assert result["summary"]["p75"] == 1.8
    assert result["median_method"] == "statistics.median"
    assert result["quantile_method"] == (
        "statistics.quantiles(n=4, method='inclusive')"
    )
    assert result["is_underwater"] is False
    assert result["current_open_duration"] == 0
    assert result["left_censored"] is False


def test_capital_efficiency_uses_trade_prices_for_true_notional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trades = [
        _trade(
            exit_order_id=index,
            quantity=3.0,
            entry_price=25.0,
            exit_price=35.0,
            gross_pnl=12.0,
            net_pnl=10.0,
        )
        for index in range(1, 6)
    ]
    seen = _install_sample(
        monkeypatch,
        capital_efficiency_service,
        _sample(trades),
    )

    result = CapitalEfficiencyService(_DB).analyze(capital_base=1000.0)

    assert result["turnover_ratio"] == 0.9
    assert result["pnl_per_unit_traded"] == round(50.0 / 900.0, 6)
    assert result["total_entry_notional"] == 375.0
    assert result["winning_entry_notional_share"] == 1.0
    assert result["average_closed_round_trip_capital"] == 0.17
    assert result["capital_time_utilization_rate"] == 0.000174
    assert result["utilization_rate"] == result["capital_time_utilization_rate"]
    assert result["exit_active_days"] == 1
    assert result["exit_active_day_rate"] == round(1 / 180, 4)
    assert result["capital_base_currency"] == "USD"
    assert result["evidence_scope"] == "CLOSED_ROUND_TRIPS_ONLY"
    assert "open positions are not included" in result["evidence_note"]
    assert result["sample_size"] == 5
    assert seen["include_excursions"] is False


def test_capital_time_utilization_changes_with_holding_time_not_exit_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    short_trades = [
        _trade(exit_order_id=index, holding_seconds=60.0)
        for index in range(1, 6)
    ]
    long_trades = [
        _trade(exit_order_id=index, holding_seconds=86400.0)
        for index in range(1, 6)
    ]
    _install_sample(
        monkeypatch,
        capital_efficiency_service,
        _sample(short_trades),
    )
    short_result = CapitalEfficiencyService(_DB).analyze(capital_base=1000.0)
    _install_sample(
        monkeypatch,
        capital_efficiency_service,
        _sample(long_trades),
    )
    long_result = CapitalEfficiencyService(_DB).analyze(capital_base=1000.0)

    assert short_result["exit_active_day_rate"] == long_result["exit_active_day_rate"]
    assert (
        short_result["capital_time_utilization_rate"]
        < long_result["capital_time_utilization_rate"]
    )


def test_first_trade_tone_compares_first_close_with_rest_of_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trades: list[ClosedRoundTrip] = []
    for day_offset in range(5):
        day = _NOW - timedelta(days=day_offset + 1)
        trades.extend(
            [
                _trade(
                    exit_order_id=day_offset * 2 + 1,
                    exit_at=day,
                    gross_pnl=12.0,
                    net_pnl=10.0,
                ),
                _trade(
                    exit_order_id=day_offset * 2 + 2,
                    exit_at=day + timedelta(minutes=1),
                    exit_price=94.0,
                    gross_pnl=-6.0,
                    net_pnl=-5.0,
                ),
            ]
        )
    trades.sort(key=lambda trade: (trade.exit_at, trade.exit_order_id))
    _install_sample(monkeypatch, first_trade_service, _sample(trades))

    result = FirstTradeService(_DB).summary()

    assert result["tone_sample_days"] == 5
    assert result["tone_min_sample_days"] == 5
    assert result["tone_sample_sufficient"] is True
    assert result["tone_match_count"] == 0
    assert result["tone_match_pct"] == 0.0


def test_first_trade_tone_excludes_single_trade_and_zero_direction_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _NOW - timedelta(days=10)
    trades = [
        _trade(exit_order_id=1, exit_at=base, net_pnl=5.0),
        _trade(exit_order_id=2, exit_at=base + timedelta(days=1), net_pnl=5.0),
        _trade(
            exit_order_id=3,
            exit_at=base + timedelta(days=1, minutes=1),
            gross_pnl=0.0,
            net_pnl=0.0,
        ),
        _trade(exit_order_id=4, exit_at=base + timedelta(days=2), net_pnl=5.0),
        _trade(
            exit_order_id=5,
            exit_at=base + timedelta(days=2, minutes=1),
            exit_price=94.0,
            gross_pnl=-6.0,
            net_pnl=-5.0,
        ),
        _trade(exit_order_id=6, exit_at=base + timedelta(days=3), net_pnl=4.0),
        _trade(
            exit_order_id=7,
            exit_at=base + timedelta(days=3, minutes=1),
            net_pnl=3.0,
        ),
    ]
    _install_sample(monkeypatch, first_trade_service, _sample(trades))

    result = FirstTradeService(_DB).summary()

    assert result["tone_sample_days"] == 2
    assert result["tone_sample_sufficient"] is False
    assert result["tone_match_pct"] is None


def test_fee_drag_counts_round_trip_fee_once_and_uses_market_local_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trade = _trade(
        exit_order_id=1,
        exit_at=datetime(2026, 1, 16, 0, 30, tzinfo=timezone.utc),
        gross_pnl=10.0,
        est_fees=2.0,
        net_pnl=8.0,
    )
    _install_sample(monkeypatch, fee_drag_service, _sample([trade]))

    result = FeeDragService(_DB).summary()

    assert result["sample_size"] == 1
    assert result["total_fees"] == 2.0
    assert result["total_gross_pnl"] == 10.0
    assert result["total_net_pnl"] == 8.0
    assert result["daily_fees"] == [{"date": "2026-01-15", "fees": 2.0}]


def test_exit_efficiency_uses_gross_pnl_and_requests_excursions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trades = [
        _trade(
            exit_order_id=index,
            gross_pnl=8.0,
            est_fees=2.0,
            net_pnl=6.0,
            mfe_amount=10.0,
            mae_amount=-3.0,
            excursion_source="SNAPSHOT_OBSERVED",
            excursion_interior_observation_count=2,
            excursion_max_gap_seconds=1800,
        )
        for index in range(1, 4)
    ]
    seen = _install_sample(
        monkeypatch,
        exit_efficiency_service,
        _sample(trades),
    )

    result = ExitEfficiencyService(_DB).summary()

    assert result["avg_capture_rate"] == 0.8
    assert result["avg_giveback"] == 2.0
    assert result["winners_with_mfe"] == 3
    assert result["excursion_quality"]["status"] == "COMPLETE"
    assert seen["include_excursions"] is True


def test_exit_efficiency_rejects_endpoint_only_and_inconsistent_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trades = [
        _trade(
            exit_order_id=1,
            mfe_amount=10.0,
            mae_amount=0.0,
            excursion_source="ENDPOINT_ONLY",
        ),
        _trade(
            exit_order_id=2,
            mfe_amount=5.0,
            mae_amount=-1.0,
            excursion_source="SNAPSHOT_OBSERVED",
            excursion_interior_observation_count=1,
        ),
        _trade(
            exit_order_id=3,
            mfe_amount=10.0,
            mae_amount=-1.0,
            excursion_source="LEGACY_UNKNOWN",
        ),
    ]
    _install_sample(monkeypatch, exit_efficiency_service, _sample(trades))

    result = ExitEfficiencyService(_DB).summary()

    assert "error" in result
    assert result["eligible_excursion_count"] == 0
    assert result["excursion_quality"]["excluded_by_reason"] == {
        "ENDPOINT_ONLY": 1,
        "INCONSISTENT_VALUES": 1,
        "LEGACY_UNKNOWN": 1,
    }


def test_intraday_seasonality_buckets_exit_in_market_local_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_at = datetime(2026, 1, 15, 14, 45, tzinfo=timezone.utc)
    trades = [
        _trade(exit_order_id=index, exit_at=exit_at + timedelta(seconds=index))
        for index in range(1, 11)
    ]
    _install_sample(
        monkeypatch,
        intraday_seasonality_service,
        _sample(trades),
    )

    result = IntradaySeasonalityService(_DB).analyze()

    opening_bucket = result["buckets"][0]
    assert opening_bucket["bucket"] == "09:30-10:00"
    assert opening_bucket["trade_count"] == 10
    assert result["unmatched_count"] == 0
