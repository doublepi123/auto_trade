from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Callable

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import Base, OrderRecord, StrategyConfig, TradeEvent
from app.services.momentum_ranking_service import MomentumRankingService
from app.services.prediction_score_service import PredictionScoreService
from app.services.profit_concentration_service import (
    ProfitConcentrationService,
)
from app.services.r_multiples_service import RMultiplesService
from app.services.reentry_analysis_service import ReentryAnalysisService
from app.services.regime_sensitivity_service import RegimeSensitivityService
from app.services.robustness_service import RobustnessService
from app.services.rolling_var_service import RollingVarService
from app.services.scratch_analysis_service import ScratchAnalysisService
from app.services.skip_analytics_service import SkipAnalyticsService


def _db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(StrategyConfig(fee_rate_us=0.0, fee_rate_hk=0.0))
    db.commit()
    return db


def _add_order(
    db: Session,
    *,
    broker_order_id: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    filled_at: datetime,
) -> OrderRecord:
    row = OrderRecord(
        broker_order_id=broker_order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        executed_quantity=quantity,
        price=price,
        executed_price=price,
        status="FILLED",
        created_at=filled_at,
        filled_at=filled_at,
    )
    db.add(row)
    db.flush()
    return row


def _add_round_trip(
    db: Session,
    *,
    key: str,
    entry_at: datetime,
    exit_at: datetime,
    exit_price: float,
    symbol: str = "AAPL.US",
    entry_price: float = 100.0,
    quantity: float = 1.0,
) -> tuple[OrderRecord, OrderRecord]:
    entry = _add_order(
        db,
        broker_order_id=f"{key}-entry",
        symbol=symbol,
        side="BUY",
        quantity=quantity,
        price=entry_price,
        filled_at=entry_at,
    )
    exit_order = _add_order(
        db,
        broker_order_id=f"{key}-exit",
        symbol=symbol,
        side="SELL",
        quantity=quantity,
        price=exit_price,
        filled_at=exit_at,
    )
    return entry, exit_order


def _sequential_trades(
    db: Session,
    pnls: list[float],
    *,
    prefix: str,
    symbol: str = "AAPL.US",
    start: datetime | None = None,
) -> None:
    current = start or datetime.now(timezone.utc) - timedelta(days=20)
    for index, pnl in enumerate(pnls):
        entry_at = current + timedelta(hours=index * 2)
        _add_round_trip(
            db,
            key=f"{prefix}-{index}",
            symbol=symbol,
            entry_at=entry_at,
            exit_at=entry_at + timedelta(hours=1),
            exit_price=100.0 + pnl,
        )
    db.commit()


def test_momentum_ranking_uses_return_not_position_size() -> None:
    db = _db()
    try:
        base = datetime.now(timezone.utc) - timedelta(days=20)
        returns = (0.01, 0.02, 0.03)
        for index, net_return in enumerate(returns):
            for symbol, quantity in (("AAPL.US", 1.0), ("MSFT.US", 100.0)):
                entry_at = base + timedelta(hours=index * 3)
                _add_round_trip(
                    db,
                    key=f"{symbol}-{index}",
                    symbol=symbol,
                    quantity=quantity,
                    entry_at=entry_at,
                    exit_at=entry_at + timedelta(hours=1),
                    exit_price=100.0 * (1.0 + net_return),
                )
        db.commit()

        result = MomentumRankingService(db).rank(
            lookback_days=365,
            min_trades=3,
        )

        assert [row["symbol"] for row in result["rankings"]] == [
            "AAPL.US",
            "MSFT.US",
        ]
        assert result["rankings"][0]["momentum_slope"] == pytest.approx(
            result["rankings"][1]["momentum_slope"]
        )
        assert result["rankings"][1]["total_pnl"] == pytest.approx(
            result["rankings"][0]["total_pnl"] * 100
        )
        assert "recent_return" in result["rankings"][0]
        assert result["statistics_quality"]["status"] == "COMPLETE"
    finally:
        db.close()


def test_prediction_uses_entry_local_time_and_only_known_prior_outcomes() -> None:
    db = _db()
    try:
        day = datetime.now(timezone.utc).date() - timedelta(days=10)
        base = datetime.combine(day, time(14, 0), tzinfo=timezone.utc)
        for index in range(20):
            _add_order(
                db,
                broker_order_id=f"prediction-entry-{index}",
                symbol="AAPL.US",
                side="BUY",
                quantity=1,
                price=100,
                filled_at=base + timedelta(minutes=index),
            )
        for index in range(20):
            _add_order(
                db,
                broker_order_id=f"prediction-exit-{index}",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=101 if index % 2 == 0 else 99,
                filled_at=base + timedelta(days=1, hours=1, minutes=index),
            )
        db.commit()

        result = PredictionScoreService(db).analyze(lookback_days=365)

        assert set(result["hour_win_rates"]) == {10}
        assert result["streak_win_rates"] == {"neutral": 0.5}
        assert result["evidence_mode"] == (
            "RETROSPECTIVE_CONDITIONAL_FREQUENCY"
        )
        assert result["live_decision_allowed"] is False
        assert result["statistics_quality"]["status"] == "COMPLETE"
    finally:
        db.close()


def test_reentry_combines_split_exits_by_independent_entry_order() -> None:
    db = _db()
    try:
        base = datetime.now(timezone.utc) - timedelta(days=20)
        first_entry = _add_order(
            db,
            broker_order_id="split-entry",
            symbol="AAPL.US",
            side="BUY",
            quantity=2,
            price=100,
            filled_at=base,
        )
        _add_order(
            db,
            broker_order_id="split-exit-1",
            symbol="AAPL.US",
            side="SELL",
            quantity=1,
            price=101,
            filled_at=base + timedelta(hours=1),
        )
        _add_order(
            db,
            broker_order_id="split-exit-2",
            symbol="AAPL.US",
            side="SELL",
            quantity=1,
            price=102,
            filled_at=base + timedelta(hours=2),
        )
        assert first_entry.id > 0
        for index, pnl in enumerate((-1, 2, -2, 3, -3), start=1):
            entry_at = base + timedelta(hours=3 * index)
            _add_round_trip(
                db,
                key=f"reentry-{index}",
                entry_at=entry_at,
                exit_at=entry_at + timedelta(hours=1),
                exit_price=100 + pnl,
            )
        db.commit()

        result = ReentryAnalysisService(db).summary(days=365)

        assert result["sample_size"] == 6
        assert result["first_of_symbol"]["trades"] == 1
        classified = sum(
            result[key]["trades"]
            for key in (
                "after_win",
                "after_loss",
                "after_scratch",
                "first_of_symbol",
                "overlapping_entry",
            )
        )
        assert classified == 6
        assert result["overlapping_entry"]["trades"] == 0
    finally:
        db.close()


@pytest.mark.parametrize(
    ("pnls", "confidence", "expected_var", "expected_cvar", "tail_count"),
    (
        ([1, 2, 3, 4, 5], 0.8, 0.0, 0.0, 1),
        ([-10, -5, 1, 2, 3], 0.6, 5.0, 7.5, 2),
    ),
)
def test_rolling_var_uses_one_consistent_nonnegative_tail(
    pnls: list[float],
    confidence: float,
    expected_var: float,
    expected_cvar: float,
    tail_count: int,
) -> None:
    db = _db()
    try:
        _sequential_trades(db, pnls, prefix="var")

        result = RollingVarService(db).compute(
            lookback_days=365,
            window=5,
            confidence=confidence,
        )

        assert result["tail_count"] == tail_count
        assert result["summary"]["latest_var"] == pytest.approx(expected_var)
        assert result["summary"]["latest_cvar"] == pytest.approx(
            expected_cvar
        )
        assert 0 <= result["summary"]["latest_var"] <= result["summary"][
            "latest_cvar"
        ]
        assert result["points"][-1]["at"].endswith(("-04:00", "-05:00"))
    finally:
        db.close()


def test_losing_strategy_cannot_receive_robustness_credit() -> None:
    db = _db()
    try:
        _sequential_trades(db, [-1.0] * 20, prefix="losing")

        result = RobustnessService(db).score(lookback_days=365)

        assert result["total_pnl"] < 0
        assert result["grade"] == "F"
        assert result["composite_score"] < 30
        assert "non-positive" in result["recommendation"]
    finally:
        db.close()


def test_regime_contract_identifies_prior_strategy_outcome_basis() -> None:
    db = _db()
    try:
        _sequential_trades(
            db,
            [
                10,
                -10,
                10,
                -10,
                10,
                0,
                0,
                0,
                0,
                0,
                10,
                -10,
                10,
                -10,
                10,
            ],
            prefix="regime",
        )

        result = RegimeSensitivityService(db).analyze(
            lookback_days=365,
            window=5,
        )

        assert result["regime_basis"] == (
            "PRIOR_CLOSED_TRADE_PNL_VOLATILITY"
        )
        assert result["classified_trades"] > 0
        assert all(
            state["trade_count"] > 0 for state in result["regimes"]
        )
        assert "turbulence" not in result["interpretation"]
        assert "reducing size" not in result["interpretation"]
    finally:
        db.close()


def test_regime_fails_closed_without_two_comparable_states() -> None:
    db = _db()
    try:
        _sequential_trades(
            db,
            [1, -1] * 6,
            prefix="constant-regime",
        )

        result = RegimeSensitivityService(db).analyze(
            lookback_days=365,
            window=5,
        )

        assert result["error"] == "No causal volatility variation detected."
        assert result["regime_basis"] == (
            "PRIOR_CLOSED_TRADE_PNL_VOLATILITY"
        )
        assert result["statistics_quality"]["status"] == "COMPLETE"
    finally:
        db.close()


def test_r_multiple_contract_discloses_realized_loss_proxy() -> None:
    db = _db()
    try:
        _sequential_trades(db, [-2, 1, 2, -1, 3], prefix="r-proxy")

        result = RMultiplesService(db).distribution(days=365)

        assert result["risk_unit_method"] == "MEAN_REALIZED_LOSS_PROXY"
        assert result["true_initial_risk_available"] is False
        assert result["statistics_quality"]["status"] == "COMPLETE"
    finally:
        db.close()


def test_skip_payload_non_objects_are_retained_with_independent_quality() -> None:
    db = _db()
    try:
        naive_now = datetime.combine(
            datetime.now(timezone.utc).date(),
            time(12, 0),
        )
        payloads = (
            '{"skip_category":"FEE"}',
            "[]",
            "null",
            "{not-json",
        )
        for index, payload in enumerate(payloads):
            db.add(TradeEvent(
                event_type="ORDER_SKIPPED",
                symbol="" if index == 1 else "AAPL.US",
                side="BUY",
                message=f"reason-{index}",
                payload_json=payload,
                created_at=naive_now - timedelta(minutes=index),
            ))
        db.commit()

        result = SkipAnalyticsService(db).summary(days=30)

        assert result["sample_size"] == 4
        assert result["event_quality"] == {
            "status": "DEGRADED",
            "total_event_count": 4,
            "valid_event_count": 1,
            "invalid_event_count": 3,
            "issues": [
                {"code": "MALFORMED_JSON", "count": 1},
                {"code": "PAYLOAD_NOT_OBJECT", "count": 2},
            ],
        }
        unknown = next(
            row for row in result["by_category"]
            if row["category"] == "UNKNOWN"
        )
        assert unknown["count"] == 3
        assert naive_now.date().isoformat() in {
            row["date"] for row in result["daily"]
        }
    finally:
        db.close()


AnalyticsCall = Callable[[Session], dict[str, object]]


@pytest.mark.parametrize(
    "call",
    (
        lambda db: MomentumRankingService(db).rank(),
        lambda db: PredictionScoreService(db).analyze(),
        lambda db: ProfitConcentrationService(db).summary(),
        lambda db: RMultiplesService(db).distribution(),
        lambda db: ReentryAnalysisService(db).summary(),
        lambda db: RegimeSensitivityService(db).analyze(window=5),
        lambda db: RobustnessService(db).score(),
        lambda db: RollingVarService(db).compute(window=5),
        lambda db: ScratchAnalysisService(db).summary(),
    ),
)
def test_every_trade_analytics_error_branch_has_evidence(
    call: AnalyticsCall,
) -> None:
    db = _db()
    try:
        result = call(db)
        assert "error" in result
        assert result["statistics_quality"] == {
            "status": "COMPLETE",
            "known_exclusion_count": 0,
            "unresolved_issue_count": 0,
            "omitted_day_count": 0,
            "items": [],
        }
        assert result["currency"] is None
        assert result["currencies"] == []
        assert result["totals_comparable"] is True
    finally:
        db.close()


@pytest.mark.parametrize(
    "call",
    (
        lambda db: MomentumRankingService(db).rank(min_trades=1),
        lambda db: ProfitConcentrationService(db).summary(),
        lambda db: RMultiplesService(db).distribution(),
        lambda db: ReentryAnalysisService(db).summary(),
        lambda db: RegimeSensitivityService(db).analyze(window=5),
        lambda db: RobustnessService(db).score(),
        lambda db: RollingVarService(db).compute(window=5),
        lambda db: ScratchAnalysisService(db).summary(),
    ),
)
def test_native_currency_amount_analytics_fail_closed_on_mixed_sample(
    call: AnalyticsCall,
) -> None:
    db = _db()
    try:
        base = datetime.now(timezone.utc) - timedelta(days=5)
        for index, symbol in enumerate(("AAPL.US", "0700.HK")):
            entry_at = base + timedelta(hours=index * 2)
            _add_round_trip(
                db,
                key=f"mixed-{index}",
                symbol=symbol,
                entry_at=entry_at,
                exit_at=entry_at + timedelta(hours=1),
                exit_price=101,
            )
        db.commit()

        result = call(db)

        assert "Mixed USD/HKD" in str(result["error"])
        assert result["currency"] == "MIXED"
        assert result["totals_comparable"] is False
        quality = result["statistics_quality"]
        assert isinstance(quality, dict)
        assert quality["status"] == "COMPLETE"
    finally:
        db.close()


def test_empty_skip_sample_reports_complete_event_quality() -> None:
    db = _db()
    try:
        result = SkipAnalyticsService(db).summary(days=30)
        assert result["event_quality"] == {
            "status": "COMPLETE",
            "total_event_count": 0,
            "valid_event_count": 0,
            "invalid_event_count": 0,
            "issues": [],
        }
    finally:
        db.close()
