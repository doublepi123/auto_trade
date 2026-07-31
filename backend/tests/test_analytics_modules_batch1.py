from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.autocorrelation import router as autocorrelation_router
from app.api.concentration import router as concentration_router
from app.api.decay_detection import router as decay_detection_router
from app.api.distribution_shape import router as distribution_shape_router
from app.api.edge_quality import router as edge_quality_router
from app.api.holding_time import router as holding_time_router
from app.api.profit_factor import router as profit_factor_router
from app.api.return_calendar import router as return_calendar_router
from app.api.size_impact import router as size_impact_router
from app.api.trade_frequency import router as trade_frequency_router
from app.database import get_db
from app.models import Base, OrderRecord, StrategyConfig
from app.services.autocorrelation_service import AutocorrelationService
from app.services.analytics_trade_sample_service import AnalyticsTradeSample
from app.services.concentration_service import ConcentrationService
from app.services.decay_detection_service import DecayDetectionService
from app.services.distribution_shape_service import DistributionShapeService
from app.services.edge_quality_service import EdgeQualityService
from app.services.holding_time_service import HoldingTimeService
from app.services.profit_factor_service import ProfitFactorService
from app.services.return_calendar_service import ReturnCalendarService
from app.services.size_impact_service import SizeImpactService
from app.services.trade_frequency_service import TradeFrequencyService
from app.services.daily_pnl_service import ClosedRoundTrip
from app.services.statistics_quality_service import StatisticsQualityData


def _engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def _order(
    *,
    broker_order_id: str,
    symbol: str,
    side: str,
    price: float,
    quantity: float,
    filled_at: datetime,
    created_at: datetime | None = None,
    recorded_net_pnl: float | None = None,
) -> OrderRecord:
    return OrderRecord(
        broker_order_id=broker_order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        executed_quantity=quantity,
        price=price,
        executed_price=price,
        status="FILLED",
        created_at=created_at or filled_at,
        filled_at=filled_at,
        net_pnl=recorded_net_pnl,
        pnl_source="UNKNOWN",
    )


def _seed_round_trips(
    db: Session,
    *,
    symbol: str = "AAPL.US",
    count: int = 20,
    start: datetime | None = None,
) -> None:
    base = start or datetime.now(timezone.utc) - timedelta(hours=count + 2)
    for index in range(count):
        entry_at = base + timedelta(hours=index)
        exit_at = entry_at + timedelta(minutes=30)
        entry_price = 100.0 + index
        exit_price = entry_price + (1.0 if index % 2 == 0 else -1.0)
        db.add_all(
            [
                _order(
                    broker_order_id=f"{symbol}-buy-{index}",
                    symbol=symbol,
                    side="BUY",
                    price=entry_price,
                    quantity=1.0,
                    filled_at=entry_at,
                ),
                _order(
                    broker_order_id=f"{symbol}-sell-{index}",
                    symbol=symbol,
                    side="SELL",
                    price=exit_price,
                    quantity=1.0,
                    filled_at=exit_at,
                    created_at=exit_at - timedelta(seconds=2),
                    # A direct OrderRecord.net_pnl reader would consume this
                    # deliberately wrong value instead of replaying FIFO fills.
                    recorded_net_pnl=999.0,
                ),
            ]
        )
    db.commit()


def _analytics_sample_from_pnls(pnls: list[float]) -> AnalyticsTradeSample:
    now = datetime.now(timezone.utc)
    trades = [
        ClosedRoundTrip(
            symbol="AAPL.US",
            side="long",
            entry_order_id=index * 2 + 1,
            exit_order_id=index * 2 + 2,
            entry_at=now - timedelta(hours=2, minutes=len(pnls) - index),
            exit_at=now - timedelta(hours=1, minutes=len(pnls) - index),
            entry_price=100.0,
            exit_price=100.0 + pnl,
            quantity=1.0,
            gross_pnl=pnl,
            est_fees=0.0,
            net_pnl=pnl,
            holding_seconds=3600.0,
        )
        for index, pnl in enumerate(pnls)
    ]
    return AnalyticsTradeSample(
        trades=trades,
        quality=StatisticsQualityData(),
        from_dt=now - timedelta(days=30),
        to_dt=now,
        currencies=("USD",),
    )


def _service_results(db: Session) -> list[dict]:
    return [
        AutocorrelationService(db).analyze(lookback_days=30),
        ConcentrationService(db).analyze(lookback_days=30),
        DecayDetectionService(db).detect(lookback_days=30),
        DistributionShapeService(db).analyze(lookback_days=30),
        EdgeQualityService(db).score(lookback_days=30),
        HoldingTimeService(db).analyze(lookback_days=30),
        ProfitFactorService(db).analyze(lookback_days=30),
        ReturnCalendarService(db).compute(lookback_days=30),
        SizeImpactService(db).analyze(lookback_days=30),
        TradeFrequencyService(db).analyze(lookback_days=30),
    ]


def test_batch1_services_use_fifo_sample_and_attach_quality() -> None:
    engine = _engine()
    with Session(engine) as db:
        db.add(StrategyConfig(fee_rate_us=0.0, fee_rate_hk=0.0))
        _seed_round_trips(db)

        results = _service_results(db)

    assert all("error" not in result for result in results)
    assert all(
        result["statistics_quality"]["status"] == "COMPLETE"
        for result in results
    )
    assert all(result["currency"] == "USD" for result in results)
    concentration = results[1]
    assert concentration["analysis_status"] == "UNAVAILABLE"
    assert concentration["hhi_pnl"] is None
    assert concentration["effective_n_pnl"] is None
    assert concentration["top_symbol"] is None
    assert concentration["breakdown"][0]["pnl_share"] is None
    profit_factor = results[6]
    assert profit_factor["overall"] == {
        "profit_factor": 1.0,
        "profit_factor_state": "FINITE",
        "gross_profit": 10.0,
        "gross_loss": 10.0,
        "net_pnl": 0.0,
    }


def test_holding_time_uses_entry_to_exit_not_exit_order_latency() -> None:
    engine = _engine()
    with Session(engine) as db:
        db.add(StrategyConfig(fee_rate_us=0.0, fee_rate_hk=0.0))
        _seed_round_trips(db, count=5)

        result = HoldingTimeService(db).analyze(lookback_days=30)

    assert result["avg_holding_seconds"] == 1800.0
    assert result["median_holding_seconds"] == 1800.0
    assert next(
        bucket for bucket in result["buckets"] if bucket["bucket"] == "30-60m"
    )["trade_count"] == 5


def test_autocorrelation_is_bounded_and_constant_sequence_is_degenerate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine()
    alternating = _analytics_sample_from_pnls(
        [1.0 if index % 2 == 0 else -1.0 for index in range(20)]
    )
    monkeypatch.setattr(
        "app.services.autocorrelation_service.load_analytics_trade_sample",
        lambda *args, **kwargs: alternating,
    )
    with Session(engine) as db:
        result = AutocorrelationService(db).analyze(lookback_days=30)

    assert result["analysis_status"] == "READY"
    assert all(abs(lag["acf"]) <= 1.0 for lag in result["lags"])

    constant = _analytics_sample_from_pnls([1.0] * 20)
    monkeypatch.setattr(
        "app.services.autocorrelation_service.load_analytics_trade_sample",
        lambda *args, **kwargs: constant,
    )
    with Session(engine) as db:
        result = AutocorrelationService(db).analyze(lookback_days=30)

    assert result["analysis_status"] == "DEGENERATE"
    assert result["pattern"] == "degenerate"
    assert "zero variance" in result["error"]
    assert "lags" not in result


def test_distribution_shape_uses_r7_quantiles_and_rejects_zero_variance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine()
    varying = _analytics_sample_from_pnls([float(index) for index in range(20)])
    monkeypatch.setattr(
        "app.services.distribution_shape_service.load_analytics_trade_sample",
        lambda *args, **kwargs: varying,
    )
    with Session(engine) as db:
        result = DistributionShapeService(db).analyze(lookback_days=30)

    assert result["analysis_status"] == "READY"
    assert result["percentiles"] == {
        "p5": 0.95,
        "p25": 4.75,
        "p50": 9.5,
        "p75": 14.25,
        "p95": 18.05,
    }

    constant = _analytics_sample_from_pnls([2.0] * 10)
    monkeypatch.setattr(
        "app.services.distribution_shape_service.load_analytics_trade_sample",
        lambda *args, **kwargs: constant,
    )
    with Session(engine) as db:
        result = DistributionShapeService(db).analyze(lookback_days=30)

    assert result["analysis_status"] == "DEGENERATE"
    assert result["tail_label"] == "degenerate"
    assert result["asymmetry"] == "undefined"
    assert "zero variance" in result["error"]


@pytest.mark.parametrize(
    ("pnls", "expected_state", "expected_value"),
    [
        ([2.0, -1.0, 2.0, -1.0, 0.0], "FINITE", 2.0),
        ([1.0] * 5, "INFINITE", None),
        ([0.0] * 5, "UNDEFINED", None),
    ],
)
def test_profit_factor_exposes_finite_infinite_and_undefined_states(
    monkeypatch: pytest.MonkeyPatch,
    pnls: list[float],
    expected_state: str,
    expected_value: float | None,
) -> None:
    sample = _analytics_sample_from_pnls(pnls)
    monkeypatch.setattr(
        "app.services.profit_factor_service.load_analytics_trade_sample",
        lambda *args, **kwargs: sample,
    )

    engine = _engine()
    with Session(engine) as db:
        result = ProfitFactorService(db).analyze(lookback_days=30)

    assert result["overall"]["profit_factor_state"] == expected_state
    assert result["overall"]["profit_factor"] == expected_value
    assert all("profit_factor_state" in row for row in result["by_symbol"])
    assert all("profit_factor_state" in row for row in result["by_size"])


def test_trade_frequency_intervals_stay_within_symbol_and_local_trade_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = datetime(2026, 7, 30, 19, 0, tzinfo=timezone.utc)
    points = [
        ("AAPL.US", base),
        ("MSFT.US", base + timedelta(seconds=10)),
        ("MSFT.US", base + timedelta(seconds=20)),
        ("AAPL.US", base + timedelta(seconds=30)),
        ("AAPL.US", base + timedelta(hours=18, minutes=30)),
        ("AAPL.US", base + timedelta(hours=18, minutes=32)),
    ]
    trades = [
        ClosedRoundTrip(
            symbol=symbol,
            side="long",
            entry_order_id=index * 2 + 1,
            exit_order_id=index * 2 + 2,
            entry_at=exit_at - timedelta(hours=1),
            exit_at=exit_at,
            entry_price=100.0,
            exit_price=101.0,
            quantity=1.0,
            gross_pnl=1.0,
            est_fees=0.0,
            net_pnl=1.0,
            holding_seconds=3600.0,
        )
        for index, (symbol, exit_at) in enumerate(points)
    ]
    sample = AnalyticsTradeSample(
        trades=trades,
        quality=StatisticsQualityData(),
        from_dt=base - timedelta(days=30),
        to_dt=points[-1][1],
        currencies=("USD",),
    )
    monkeypatch.setattr(
        "app.services.trade_frequency_service.load_analytics_trade_sample",
        lambda *args, **kwargs: sample,
    )

    engine = _engine()
    with Session(engine) as db:
        result = TradeFrequencyService(db).analyze(lookback_days=30)

    assert result["interval_pair_count"] == 3
    assert result["avg_interval_seconds"] == 53.3
    assert result["min_interval_seconds"] == 10.0
    assert result["rapid_fire_count"] == 2
    assert result["rapid_fire_pct"] == 0.6667


def test_edge_quality_never_labels_non_profitable_sample_as_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample = _analytics_sample_from_pnls([-1.0] * 100)
    monkeypatch.setattr(
        "app.services.edge_quality_service.load_analytics_trade_sample",
        lambda *args, **kwargs: sample,
    )

    engine = _engine()
    with Session(engine) as db:
        result = EdgeQualityService(db).score(lookback_days=30)

    assert result["underlying"]["expectancy"] == -1.0
    assert result["composite_score"] < 35
    assert result["grade"] == "F"
    assert result["recommendation"].startswith("No detectable edge")


@pytest.mark.parametrize(
    "analyze",
    [
        lambda db: AutocorrelationService(db).analyze(lookback_days=30),
        lambda db: ConcentrationService(db).analyze(lookback_days=30),
        lambda db: DecayDetectionService(db).detect(lookback_days=30),
        lambda db: DistributionShapeService(db).analyze(lookback_days=30),
        lambda db: EdgeQualityService(db).score(lookback_days=30),
        lambda db: HoldingTimeService(db).analyze(lookback_days=30),
        lambda db: ProfitFactorService(db).analyze(lookback_days=30),
        lambda db: ReturnCalendarService(db).compute(lookback_days=30),
        lambda db: SizeImpactService(db).analyze(lookback_days=30),
    ],
)
def test_monetary_analytics_fail_closed_for_mixed_currencies(
    analyze: Callable[[Session], dict],
) -> None:
    engine = _engine()
    with Session(engine) as db:
        db.add(StrategyConfig(fee_rate_us=0.0, fee_rate_hk=0.0))
        _seed_round_trips(db, symbol="AAPL.US", count=1)
        _seed_round_trips(db, symbol="0700.HK", count=1)

        result = analyze(db)

    assert result["currency"] == "MIXED"
    assert result["totals_comparable"] is False
    assert "Mixed USD/HKD" in result["error"]
    assert result["statistics_quality"]["status"] == "COMPLETE"


def test_size_impact_uses_entry_notional_and_balanced_rank_buckets() -> None:
    engine = _engine()
    with Session(engine) as db:
        db.add(StrategyConfig(fee_rate_us=0.0, fee_rate_hk=0.0))
        _seed_round_trips(db, count=8)

        result = SizeImpactService(db).analyze(lookback_days=30)

    assert [bucket["trade_count"] for bucket in result["quartiles"]] == [2, 2, 2, 2]
    assert all("avg_entry_notional" in bucket for bucket in result["quartiles"])
    assert all("avg_return_pct" in bucket for bucket in result["quartiles"])
    assert all("avg_quantity" not in bucket for bucket in result["quartiles"])


def test_size_impact_negative_baseline_does_not_reverse_trend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    trades: list[ClosedRoundTrip] = []
    returns = [-0.01, -0.01, -0.005, -0.005, -0.005, -0.005, -0.011, -0.011]
    for index, net_return in enumerate(returns):
        entry_price = float((index + 1) * 100)
        net_pnl = entry_price * net_return
        trades.append(
            ClosedRoundTrip(
                symbol="AAPL.US",
                side="long",
                entry_order_id=index * 2 + 1,
                exit_order_id=index * 2 + 2,
                entry_at=now - timedelta(hours=2),
                exit_at=now - timedelta(hours=1, minutes=7 - index),
                entry_price=entry_price,
                exit_price=entry_price * (1 + net_return),
                quantity=1.0,
                gross_pnl=net_pnl,
                est_fees=0.0,
                net_pnl=net_pnl,
                holding_seconds=3600.0,
            )
        )
    sample = AnalyticsTradeSample(
        trades=trades,
        quality=StatisticsQualityData(),
        from_dt=now - timedelta(days=30),
        to_dt=now,
        currencies=("USD",),
    )
    monkeypatch.setattr(
        "app.services.size_impact_service.load_analytics_trade_sample",
        lambda *args, **kwargs: sample,
    )

    engine = _engine()
    with Session(engine) as db:
        result = SizeImpactService(db).analyze(lookback_days=30)

    assert result["quartiles"][0]["avg_return_pct"] == -1.0
    assert result["quartiles"][-1]["avg_return_pct"] == -1.1
    assert result["size_efficiency_trend"] == "stable"


def test_all_batch1_endpoints_return_quality_on_insufficient_sample() -> None:
    engine = _engine()
    app = FastAPI()
    for router in (
        autocorrelation_router,
        concentration_router,
        decay_detection_router,
        distribution_shape_router,
        edge_quality_router,
        holding_time_router,
        profit_factor_router,
        return_calendar_router,
        size_impact_router,
        trade_frequency_router,
    ):
        app.include_router(router)

    def override_get_db():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    paths = (
        "/api/autocorrelation/analyze",
        "/api/concentration/analyze",
        "/api/decay-detection/detect",
        "/api/distribution-shape/analyze",
        "/api/edge-quality/score",
        "/api/holding-time/analyze",
        "/api/profit-factor/decompose",
        "/api/return-calendar/compute",
        "/api/size-impact/analyze",
        "/api/trade-frequency/analyze",
    )

    with TestClient(app) as client:
        responses = [client.get(path) for path in paths]

    assert all(response.status_code == 200 for response in responses)
    assert all("error" in response.json() for response in responses)
    assert all(
        response.json()["statistics_quality"]["status"] == "COMPLETE"
        for response in responses
    )
