from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core.broker import BrokerCandle
from app.domain.opening_momentum import (
    ALGORITHM_VERSION,
    OpeningMomentumConfig,
)
from app.models import (
    Base,
    OpeningMomentumShadowRun,
    StrategyV2ShadowConfig,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
)
from app.services.opening_momentum_shadow_service import (
    OpeningMomentumShadowService,
)


_SESSION_OPEN = datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc)
_SYMBOLS = tuple(f"S{index}.US" for index in range(8))


class _FakeCandles:
    def __init__(
        self,
        *,
        missing_entry_for: str | None = None,
        opening_returns_bps: dict[str, float] | None = None,
        early_opening_returns_bps: dict[str, float] | None = None,
        negative_last_five_for: str | None = None,
    ) -> None:
        self.missing_entry_for = missing_entry_for
        self.opening_returns_bps = opening_returns_bps or {}
        self.early_opening_returns_bps = (
            early_opening_returns_bps or {}
        )
        self.negative_last_five_for = negative_last_five_for
        self.calls: list[str] = []

    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[BrokerCandle]:
        self.calls.append(symbol)
        assert period == "MIN_1"
        assert count == 500
        symbol_index = _SYMBOLS.index(symbol)
        opening_return_bps = self.opening_returns_bps.get(
            symbol,
            100.0 if symbol_index == 7 else float(symbol_index),
        )
        bars: list[BrokerCandle] = []
        for index in range(126):
            if (
                symbol == self.missing_entry_for
                and index == 31
            ):
                continue
            open_price = 100.0
            close_price = 100.0
            if index == 2:
                early_return_bps = self.early_opening_returns_bps.get(
                    symbol,
                    0.0,
                )
                close_price = 100.0 * (
                    1 + early_return_bps / 10_000
                )
            if index == 29:
                close_price = 100.0 * (
                    1 + opening_return_bps / 10_000
                )
            if (
                index == 25
                and symbol == self.negative_last_five_for
            ):
                open_price = 102.0
            if index == 31:
                open_price = 100.5 if symbol_index == 7 else 100.0
            if index == 4:
                open_price = 100.5 if symbol_index == 7 else 100.0
            if index == 61:
                open_price = 101.5 if symbol_index == 7 else 100.0
            if index == 91:
                open_price = 102.5 if symbol_index == 7 else 100.0
            if index == 124:
                open_price = 102.5 if symbol_index == 7 else 100.0
            bars.append(
                BrokerCandle(
                    timestamp=_SESSION_OPEN
                    + timedelta(minutes=index),
                    open=open_price,
                    high=max(open_price, close_price) + 0.1,
                    low=min(open_price, close_price) - 0.1,
                    close=close_price,
                    volume=1000,
                )
            )
        return bars


class _HistoricalExitCandles:
    def __init__(self, exit_at: datetime) -> None:
        self.exit_at = exit_at
        self.history_calls: list[datetime] = []

    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[BrokerCandle]:
        return []

    def get_history_candlesticks_by_offset(
        self,
        symbol: str,
        period: str,
        count: int,
        after: datetime,
    ) -> list[BrokerCandle]:
        self.history_calls.append(after)
        return [
            BrokerCandle(
                timestamp=self.exit_at,
                open=102.0,
                high=102.1,
                low=101.9,
                close=102.0,
                volume=1000,
            )
        ]


def _database() -> tuple[Engine, Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return engine, Session(bind=engine)


def _seed_universe(
    db: Session,
    *,
    symbols: tuple[str, ...] = _SYMBOLS,
    algorithm_version: str = "test-v1",
    as_of_date: date = date(2026, 7, 22),
    completed_at: datetime = _SESSION_OPEN - timedelta(days=1),
) -> UniverseSelectionRun:
    run = UniverseSelectionRun(
        as_of_date=as_of_date,
        algorithm_version=algorithm_version,
        source_version="test",
        status="COMPLETE",
        candidate_count=len(symbols),
        evaluable_count=len(symbols),
        selected_count=len(symbols),
        coverage_ratio=1.0,
        completed_at=completed_at,
    )
    db.add(run)
    db.flush()
    for rank, symbol in enumerate(symbols, start=1):
        db.add(
            UniverseSelectionCandidate(
                run_id=run.id,
                symbol=symbol,
                market="US",
                selected=True,
                rank=rank,
                score=float(100 - rank),
            )
        )
    db.commit()
    return run


def test_tick_uses_only_a_universe_completed_before_session_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_shadow_enabled",
        True,
    )
    engine, db = _database()
    try:
        frozen = _seed_universe(db)
        late = _seed_universe(
            db,
            symbols=tuple(reversed(_SYMBOLS)),
            algorithm_version="test-v2",
            completed_at=_SESSION_OPEN + timedelta(minutes=1),
        )
        same_session = _seed_universe(
            db,
            symbols=_SYMBOLS[1:] + _SYMBOLS[:1],
            algorithm_version="test-v3",
            as_of_date=date(2026, 7, 23),
            completed_at=_SESSION_OPEN - timedelta(minutes=1),
        )
        candles = _FakeCandles()

        status = OpeningMomentumShadowService(
            db,
            candles,
        ).tick(
            now=_SESSION_OPEN + timedelta(minutes=32, seconds=10),
        )

        assert status.latest is not None
        assert status.latest.selection_run_id == frozen.id
        assert status.latest.selection_run_id != late.id
        assert status.latest.selection_run_id != same_session.id
        assert status.latest.universe == list(_SYMBOLS)
        assert candles.calls == list(_SYMBOLS)
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_tick_skips_when_no_preopen_universe_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_shadow_enabled",
        True,
    )
    engine, db = _database()
    try:
        _seed_universe(
            db,
            completed_at=_SESSION_OPEN + timedelta(minutes=1),
        )
        candles = _FakeCandles()

        status = OpeningMomentumShadowService(
            db,
            candles,
        ).tick(
            now=_SESSION_OPEN + timedelta(minutes=32, seconds=10),
        )

        assert status.latest is not None
        assert status.latest.status == "SKIPPED"
        assert status.latest.reason == "PREOPEN_UNIVERSE_UNAVAILABLE"
        assert status.latest.selection_run_id is None
        assert status.latest.universe == []
        assert candles.calls == []
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def _seed_variant_universe(db: Session) -> UniverseSelectionRun:
    run = UniverseSelectionRun(
        as_of_date=date(2026, 7, 22),
        algorithm_version="test-v1",
        source_version="test",
        status="COMPLETE",
        candidate_count=4,
        evaluable_count=4,
        selected_count=2,
        coverage_ratio=1.0,
        completed_at=_SESSION_OPEN - timedelta(days=1),
    )
    db.add(run)
    db.flush()
    for index, symbol in enumerate(_SYMBOLS[:4]):
        incumbent_selected = index < 2
        strong_continuation = index >= 2
        metrics = {
            "avg_dollar_volume": (
                2_000_000_000.0
                if strong_continuation
                else 600_000_000.0
            ),
            "relative_spread_bps": (
                0.5 if strong_continuation else 5.0
            ),
            "opportunity_to_cost_ratio": (
                20.0 if strong_continuation else 5.0
            ),
            "momentum_5d_pct": (
                float(index + 5)
                if strong_continuation
                else float(index - 5)
            ),
            "trend_efficiency_10d": (
                0.8 + index / 100
                if strong_continuation
                else 0.1 + index / 100
            ),
        }
        db.add(
            UniverseSelectionCandidate(
                run_id=run.id,
                symbol=symbol,
                market="US",
                sector=f"Sector {index}",
                selected=incumbent_selected,
                rank=index + 1 if incumbent_selected else None,
                score=float(100 - index),
                metrics_json=json.dumps(metrics),
                exclusion_reasons_json=(
                    "[]"
                    if incumbent_selected
                    else '["BELOW_SELECTION_CUTOFF"]'
                ),
            )
        )
    db.commit()
    return run


def _seed_active_broad_pool(db: Session) -> None:
    for index, symbol in enumerate(_SYMBOLS):
        db.add(StrategyV2ShadowConfig(
            symbol=symbol,
            enabled=True,
            universe_managed=index % 2 == 0,
        ))
    db.add(StrategyV2ShadowConfig(
        symbol="DISABLED.US",
        enabled=False,
        universe_managed=True,
    ))
    db.commit()


def test_tick_opens_then_closes_one_cost_adjusted_shadow_trade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_shadow_enabled",
        True,
    )
    engine, db = _database()
    try:
        run = _seed_universe(db)
        candles = _FakeCandles()
        service = OpeningMomentumShadowService(db, candles)

        waiting = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=31, seconds=10),
        )

        assert waiting.state == "WAITING"
        assert db.query(OpeningMomentumShadowRun).count() == 0
        assert candles.calls == []

        opened = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=32, seconds=10),
        )

        assert opened.state == "OPEN"
        assert opened.latest is not None
        assert opened.latest.status == "OPEN"
        assert opened.latest.selection_run_id == run.id
        assert opened.latest.candidate_symbol == "S7.US"
        assert opened.latest.entry_at == _SESSION_OPEN + timedelta(minutes=31)
        assert opened.latest.entry_price == 100.5
        assert opened.latest.estimated_cost_bps == 14.0
        assert opened.latest.universe == list(_SYMBOLS)
        assert opened.latest.excluded_symbols == {}
        assert opened.latest.candidate_first_five_return_bps == 0.0
        assert opened.latest.candidate_last_five_return_bps == pytest.approx(
            100.0
        )
        assert opened.latest.candidate_path_efficiency == pytest.approx(
            1.0
        )
        assert opened.latest.candidate_max_pullback_bps == pytest.approx(
            (99.9 / 101.1 - 1) * 10_000
        )
        assert opened.latest.candidate_opening_range_bps == pytest.approx(
            120.0
        )

        closed = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=62, seconds=10),
        )

        assert closed.state == "COLLECTING"
        assert closed.latest is not None
        assert closed.latest.status == "CLOSED"
        assert closed.latest.reason == "FIXED_HOLD_EXIT"
        assert closed.latest.exit_price == 101.5
        expected_gross = (101.5 / 100.5 - 1) * 10_000
        assert closed.latest.gross_return_bps == pytest.approx(
            expected_gross
        )
        assert closed.latest.net_return_bps == pytest.approx(
            expected_gross - 14
        )
        assert closed.metrics.closed_trades == 1
        assert closed.metrics.wins == 1
        assert closed.metrics.win_rate == 1.0
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_candle_coercion_keeps_decision_prices_when_range_is_missing() -> None:
    candle = SimpleNamespace(
        timestamp=_SESSION_OPEN,
        open=100.0,
        close=101.0,
    )

    result = OpeningMomentumShadowService._coerce_candles([candle])

    assert len(result) == 1
    assert result[0].open == 100.0
    assert result[0].high == 101.0
    assert result[0].low == 100.0
    assert result[0].close == 101.0


def test_opening_path_efficiency_is_bounded_for_compounding_path() -> None:
    candles = [
        SimpleNamespace(
            timestamp=_SESSION_OPEN + timedelta(minutes=index),
            open=100.0 + index,
            high=101.0 + index,
            low=100.0 + index,
            close=101.0 + index,
        )
        for index in range(5)
    ]
    coerced = OpeningMomentumShadowService._coerce_candles(candles)

    features = OpeningMomentumShadowService._opening_path_features(coerced)

    assert features.path_efficiency == 1.0


def test_challenger_variants_isolate_universe_and_entry_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_challenger_enabled",
        True,
    )
    monkeypatch.setattr(
        settings,
        "universe_selection_max_symbols",
        12,
    )
    engine, db = _database()
    try:
        service = OpeningMomentumShadowService(db)
        identities = service._variant_identities()
        by_variant = {
            identity.variant: identity for identity in identities
        }

        assert set(by_variant) == {
            "INCUMBENT",
            "EARLY_BROAD_CHALLENGER",
            "REVERSAL_CHALLENGER",
            "CONTINUATION_CHALLENGER",
            "BREADTH_GATED_CHALLENGER",
            "LAST5_POSITIVE_CHALLENGER",
            "LAST5_ONLY_CHALLENGER",
        }
        early = by_variant["EARLY_BROAD_CHALLENGER"]
        reversal = by_variant["REVERSAL_CHALLENGER"]
        continuation = by_variant["CONTINUATION_CHALLENGER"]
        breadth = by_variant["BREADTH_GATED_CHALLENGER"]
        last_five = by_variant["LAST5_POSITIVE_CHALLENGER"]
        last_five_only = by_variant["LAST5_ONLY_CHALLENGER"]
        assert early.decision_config.signal_minutes == 3
        assert early.decision_config.holding_minutes == 120
        assert (
            early.decision_config.minimum_market_return_bps
            == -50.0
        )
        assert (
            early.decision_config.minimum_candidate_return_bps
            == 50.0
        )
        assert early.decision_config.minimum_excess_return_bps == 25.0
        assert early.minimum_data_coverage == 0.95
        assert early.universe_source == "OPENING_EARLY_BROAD"
        assert continuation.decision_config.holding_minutes == 30
        assert (
            continuation.decision_config.minimum_market_return_bps
            == -25.0
        )
        assert reversal.decision_config == continuation.decision_config
        assert reversal.signal_model == "REVERSAL"
        assert reversal.universe_source == "OPENING_REVERSAL"
        assert breadth.decision_config.holding_minutes == 30
        assert breadth.decision_config.minimum_market_return_bps == 0.0
        assert last_five.decision_config == breadth.decision_config
        assert last_five.require_nonnegative_last_five is True
        assert last_five_only.decision_config.holding_minutes == 30
        assert (
            last_five_only.decision_config.minimum_market_return_bps
            == -25.0
        )
        assert last_five_only.require_nonnegative_last_five is True
        assert len(
            {
                identity.config_version
                for identity in identities
            }
        ) == 7
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_variant_comparisons_are_paired_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_challenger_enabled",
        True,
    )
    engine, db = _database()
    try:
        service = OpeningMomentumShadowService(db)
        identities = service._variant_identities()
        identities_by_variant = {
            identity.variant: identity for identity in identities
        }
        for variant in (
            "INCUMBENT",
            "CONTINUATION_CHALLENGER",
        ):
            identity = identities_by_variant[variant]
            db.add(
                OpeningMomentumShadowRun(
                    session_date=date(2026, 7, 22),
                    algorithm_version=identity.algorithm_version,
                    config_version=identity.config_version,
                    status="SKIPPED",
                    reason="MARKET_FILTER",
                    signal_at=_SESSION_OPEN - timedelta(days=1),
                    observed_at=_SESSION_OPEN - timedelta(days=1),
                    universe_source=identity.universe_source,
                    universe_size=12,
                    estimated_cost_bps=14.0,
                )
            )
        db.commit()

        by_variant = {
            item.variant: item
            for item in service._variant_responses()
        }

        continuation = by_variant["CONTINUATION_CHALLENGER"]
        breadth = by_variant["BREADTH_GATED_CHALLENGER"]
        assert continuation.comparison_sessions == 1
        assert continuation.comparison is not None
        assert continuation.comparison.resolved_sessions == 1
        assert continuation.comparison.mean_delta_bps == 0.0
        assert breadth.comparison_sessions == 0
        assert breadth.comparison is not None
        assert breadth.comparison.resolved_sessions == 0
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_challengers_use_one_market_snapshot_and_close_all_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_shadow_enabled",
        True,
    )
    monkeypatch.setattr(
        settings,
        "opening_momentum_challenger_enabled",
        True,
    )
    monkeypatch.setattr(
        settings,
        "universe_selection_max_symbols",
        2,
    )
    monkeypatch.setattr(
        settings,
        "universe_selection_max_per_sector",
        2,
    )
    engine, db = _database()
    try:
        run = _seed_variant_universe(db)
        candles = _FakeCandles(
            opening_returns_bps={"S0.US": -40.0},
        )
        service = OpeningMomentumShadowService(
            db,
            candles,
            config=OpeningMomentumConfig(
                minimum_universe_size=2,
                minimum_excess_return_bps=0,
            ),
        )

        opened = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=32, seconds=10),
        )

        assert db.query(OpeningMomentumShadowRun).count() == 6
        assert candles.calls == list(_SYMBOLS[:4])
        assert opened.state == "OPEN"
        assert opened.latest is not None
        assert opened.latest.universe_source == "UNIVERSE_SELECTION"
        assert opened.latest.candidate_symbol == "S1.US"
        assert opened.latest.selection_run_id == run.id
        assert len(opened.variants) == 7
        (
            incumbent,
            early,
            reversal,
            challenger,
            breadth,
            last_five,
            last_five_only,
        ) = opened.variants
        assert incumbent.variant == "INCUMBENT"
        assert incumbent.comparison_sessions == 1
        assert incumbent.comparison is None
        assert incumbent.minimum_market_return_bps == -25.0
        assert incumbent.holding_minutes == 30
        assert incumbent.latest is not None
        assert incumbent.latest.candidate_symbol == "S1.US"
        assert early.variant == "EARLY_BROAD_CHALLENGER"
        assert early.signal_minutes == 3
        assert early.minimum_market_return_bps == -50.0
        assert early.minimum_candidate_return_bps == 50.0
        assert early.minimum_excess_return_bps == 25.0
        assert early.minimum_data_coverage == 0.95
        assert early.holding_minutes == 120
        assert early.comparison_sessions == 0
        assert early.latest is None
        assert reversal.variant == "REVERSAL_CHALLENGER"
        assert reversal.comparison_sessions == 1
        assert reversal.minimum_market_return_bps == -25.0
        assert reversal.holding_minutes == 30
        assert reversal.latest is not None
        assert reversal.latest.universe == ["S0.US", "S1.US"]
        assert reversal.latest.candidate_symbol == "S0.US"
        assert reversal.latest.candidate_return_bps == pytest.approx(
            -40.0
        )
        assert reversal.latest.excess_return_bps == pytest.approx(
            -20.5
        )
        assert (
            reversal.latest.reason
            == "OPENING_LAGGARD_REVERSAL"
        )
        assert challenger.variant == "CONTINUATION_CHALLENGER"
        assert challenger.comparison_sessions == 1
        assert challenger.minimum_market_return_bps == -25.0
        assert challenger.holding_minutes == 30
        assert challenger.latest is not None
        assert challenger.latest.universe == ["S2.US", "S3.US"]
        assert challenger.latest.candidate_symbol == "S3.US"
        assert breadth.variant == "BREADTH_GATED_CHALLENGER"
        assert breadth.comparison_sessions == 1
        assert breadth.minimum_market_return_bps == 0.0
        assert breadth.holding_minutes == 30
        assert breadth.latest is not None
        assert breadth.latest.universe == ["S2.US", "S3.US"]
        assert breadth.latest.candidate_symbol == "S3.US"
        assert last_five.variant == (
            "LAST5_POSITIVE_CHALLENGER"
        )
        assert last_five.comparison_sessions == 1
        assert last_five.minimum_market_return_bps == 0.0
        assert last_five.holding_minutes == 30
        assert last_five.latest is not None
        assert last_five.latest.universe == ["S2.US", "S3.US"]
        assert last_five.latest.candidate_symbol == "S3.US"
        assert last_five_only.variant == (
            "LAST5_ONLY_CHALLENGER"
        )
        assert last_five_only.comparison_sessions == 1
        assert last_five_only.minimum_market_return_bps == -25.0
        assert last_five_only.holding_minutes == 30
        assert last_five_only.latest is not None
        assert last_five_only.latest.universe == ["S2.US", "S3.US"]
        assert last_five_only.latest.candidate_symbol == "S3.US"
        assert last_five_only.latest.exit_due_at == (
            _SESSION_OPEN + timedelta(minutes=61)
        )
        for item in opened.variants[1:]:
            assert item.comparison is not None
            assert item.comparison.resolved_sessions == 0
            assert item.comparison.recommendation == "COLLECTING"
            assert item.comparison.promotion_ready is False
        assert len(
            {
                item.config_version
                for item in opened.variants
            }
        ) == 7

        still_open = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=47, seconds=10),
        )

        rows = db.query(OpeningMomentumShadowRun).all()
        assert {row.status for row in rows} == {"OPEN"}
        assert still_open.state == "OPEN"
        assert [
            item.metrics.closed_trades
            for item in still_open.variants
        ] == [
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        ]

        closed = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=62, seconds=10),
        )

        rows = db.query(OpeningMomentumShadowRun).all()
        assert {row.status for row in rows} == {"CLOSED"}
        assert closed.state == "COLLECTING"
        assert [
            item.metrics.closed_trades for item in closed.variants
        ] == [
            1,
            0,
            1,
            1,
            1,
            1,
            1,
        ]
        assert [
            item.metrics.cumulative_net_return_bps
            for item in closed.variants
        ] == [
            -14.0,
            0.0,
            -14.0,
            -14.0,
            -14.0,
            -14.0,
            -14.0,
        ]
        assert early.comparison is not None
        assert early.comparison.resolved_sessions == 0
        for item in closed.variants[2:]:
            assert item.comparison is not None
            assert item.comparison.resolved_sessions == 1
            assert item.comparison.mean_delta_bps == 0.0
            assert item.comparison.confidence_lower_bps is None
            assert item.comparison.recommendation == "COLLECTING"
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_early_broad_challenger_keeps_independent_observation_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_shadow_enabled",
        True,
    )
    monkeypatch.setattr(
        settings,
        "opening_momentum_challenger_enabled",
        True,
    )
    monkeypatch.setattr(
        settings,
        "universe_selection_max_symbols",
        2,
    )
    monkeypatch.setattr(
        settings,
        "universe_selection_max_per_sector",
        2,
    )
    engine, db = _database()
    try:
        run = _seed_variant_universe(db)
        _seed_active_broad_pool(db)
        candles = _FakeCandles(
            early_opening_returns_bps={"S7.US": 100.0},
        )
        service = OpeningMomentumShadowService(
            db,
            candles,
            config=OpeningMomentumConfig(
                minimum_universe_size=2,
                minimum_excess_return_bps=0,
            ),
        )

        early_opened = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=5, seconds=10),
        )

        rows = db.query(OpeningMomentumShadowRun).all()
        assert len(rows) == 1
        assert candles.calls == list(_SYMBOLS)
        assert early_opened.state == "OPEN"
        assert early_opened.latest is None
        early = {
            item.variant: item for item in early_opened.variants
        }["EARLY_BROAD_CHALLENGER"]
        assert early.latest is not None
        assert early.latest.status == "OPEN"
        assert early.latest.selection_run_id == run.id
        assert early.latest.universe_source == "OPENING_EARLY_BROAD"
        assert early.latest.universe == list(_SYMBOLS)
        assert early.latest.candidate_symbol == "S7.US"
        assert early.latest.signal_at == (
            _SESSION_OPEN + timedelta(minutes=2)
        )
        assert early.latest.entry_at == (
            _SESSION_OPEN + timedelta(minutes=4)
        )
        assert early.latest.exit_due_at == (
            _SESSION_OPEN + timedelta(minutes=124)
        )
        assert early.latest.entry_price == 100.5
        assert early.latest.candidate_path_efficiency is None
        assert early.latest.candidate_max_pullback_bps is None

        candles.calls.clear()
        standard_opened = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=32, seconds=10),
        )

        assert db.query(OpeningMomentumShadowRun).count() == 7
        assert candles.calls == list(_SYMBOLS[:4])
        by_variant = {
            item.variant: item for item in standard_opened.variants
        }
        assert by_variant["INCUMBENT"].latest is not None
        assert by_variant["EARLY_BROAD_CHALLENGER"].latest is not None
        assert (
            by_variant["EARLY_BROAD_CHALLENGER"].latest.status
            == "OPEN"
        )

        standard_closed = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=62, seconds=10),
        )

        assert standard_closed.state == "OPEN"
        rows = db.query(OpeningMomentumShadowRun).all()
        assert sum(row.status == "CLOSED" for row in rows) == 5
        assert sum(row.status == "SKIPPED" for row in rows) == 1
        assert sum(row.status == "OPEN" for row in rows) == 1
        by_variant = {
            item.variant: item for item in standard_closed.variants
        }
        assert by_variant["INCUMBENT"].metrics.closed_trades == 1
        assert (
            by_variant["EARLY_BROAD_CHALLENGER"]
            .metrics.closed_trades
            == 0
        )

        all_closed = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=125, seconds=10),
        )

        assert all_closed.state == "COLLECTING"
        early_closed = {
            item.variant: item for item in all_closed.variants
        }["EARLY_BROAD_CHALLENGER"]
        assert early_closed.latest is not None
        assert early_closed.latest.status == "CLOSED"
        assert early_closed.latest.exit_price == 102.5
        expected_gross = (102.5 / 100.5 - 1) * 10_000
        assert early_closed.latest.net_return_bps == pytest.approx(
            expected_gross - 14.0
        )
        assert early_closed.metrics.closed_trades == 1
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_breadth_challenger_skips_a_negative_market_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_shadow_enabled",
        True,
    )
    monkeypatch.setattr(
        settings,
        "opening_momentum_challenger_enabled",
        True,
    )
    monkeypatch.setattr(
        settings,
        "universe_selection_max_symbols",
        2,
    )
    monkeypatch.setattr(
        settings,
        "universe_selection_max_per_sector",
        2,
    )
    engine, db = _database()
    try:
        _seed_variant_universe(db)
        candles = _FakeCandles(
            opening_returns_bps={
                symbol: -80.0 if index % 2 == 0 else 20.0
                for index, symbol in enumerate(_SYMBOLS[:4])
            }
        )
        service = OpeningMomentumShadowService(
            db,
            candles,
            config=OpeningMomentumConfig(
                minimum_universe_size=2,
                minimum_market_return_bps=-50.0,
                minimum_excess_return_bps=0,
            ),
        )

        status = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=32, seconds=10),
        )

        assert candles.calls == list(_SYMBOLS[:4])
        assert len(status.variants) == 7
        (
            incumbent,
            early,
            reversal,
            continuation,
            breadth,
            last_five,
            last_five_only,
        ) = status.variants
        assert incumbent.latest is not None
        assert incumbent.latest.status == "OPEN"
        assert early.variant == "EARLY_BROAD_CHALLENGER"
        assert early.latest is None
        assert reversal.latest is not None
        assert reversal.latest.status == "OPEN"
        assert reversal.latest.reason == "OPENING_LAGGARD_REVERSAL"
        assert reversal.latest.candidate_symbol == "S0.US"
        assert continuation.latest is not None
        assert continuation.latest.status == "OPEN"
        assert breadth.latest is not None
        assert breadth.latest.status == "SKIPPED"
        assert breadth.latest.reason == "MARKET_FILTER"
        assert breadth.latest.market_return_bps == pytest.approx(-30.0)
        assert breadth.minimum_market_return_bps == 0.0
        assert breadth.holding_minutes == 30
        assert last_five.latest is not None
        assert last_five.latest.status == "SKIPPED"
        assert last_five.latest.reason == "MARKET_FILTER"
        assert last_five.latest.market_return_bps == pytest.approx(
            -30.0
        )
        assert last_five.minimum_market_return_bps == 0.0
        assert last_five.holding_minutes == 30
        assert last_five_only.latest is not None
        assert last_five_only.latest.status == "OPEN"
        assert last_five_only.latest.reason == "OPENING_LEADER"
        assert last_five_only.latest.market_return_bps == pytest.approx(
            -30.0
        )
        assert last_five_only.minimum_market_return_bps == -50.0
        assert last_five_only.holding_minutes == 30
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_last_five_challenger_skips_a_fading_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_shadow_enabled",
        True,
    )
    monkeypatch.setattr(
        settings,
        "opening_momentum_challenger_enabled",
        True,
    )
    monkeypatch.setattr(
        settings,
        "universe_selection_max_symbols",
        2,
    )
    monkeypatch.setattr(
        settings,
        "universe_selection_max_per_sector",
        2,
    )
    engine, db = _database()
    try:
        _seed_variant_universe(db)
        service = OpeningMomentumShadowService(
            db,
            _FakeCandles(negative_last_five_for="S3.US"),
            config=OpeningMomentumConfig(
                minimum_universe_size=2,
                minimum_excess_return_bps=0,
            ),
        )

        status = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=32, seconds=10),
        )

        by_variant = {
            item.variant: item for item in status.variants
        }
        breadth = by_variant["BREADTH_GATED_CHALLENGER"]
        last_five = by_variant["LAST5_POSITIVE_CHALLENGER"]
        last_five_only = by_variant["LAST5_ONLY_CHALLENGER"]
        assert breadth.latest is not None
        assert breadth.latest.status == "OPEN"
        assert last_five.latest is not None
        assert last_five.latest.status == "SKIPPED"
        assert last_five.latest.reason == "LAST_FIVE_RETURN_FILTER"
        assert (
            last_five.latest.candidate_last_five_return_bps
            is not None
        )
        assert last_five.latest.candidate_last_five_return_bps < 0
        assert last_five.latest.entry_price is None
        assert last_five_only.latest is not None
        assert last_five_only.latest.status == "SKIPPED"
        assert (
            last_five_only.latest.reason
            == "LAST_FIVE_RETURN_FILTER"
        )
        assert last_five_only.latest.entry_price is None
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_paired_policy_return_excludes_unresolved_and_data_failures() -> None:
    assert OpeningMomentumShadowService._paired_policy_return(
        OpeningMomentumShadowRun(
            status="CLOSED",
            reason="FIXED_HOLD_EXIT",
            net_return_bps=12.5,
        )
    ) == 12.5
    assert OpeningMomentumShadowService._paired_policy_return(
        OpeningMomentumShadowRun(
            status="SKIPPED",
            reason="MARKET_FILTER",
        )
    ) == 0.0
    assert OpeningMomentumShadowService._paired_policy_return(
        OpeningMomentumShadowRun(
            status="SKIPPED",
            reason="LAST_FIVE_RETURN_FILTER",
        )
    ) == 0.0
    for status, reason in (
        ("OPEN", "OPENING_LEADER"),
        ("SKIPPED", "DATA_INCOMPLETE"),
        ("SKIPPED", "ENTRY_BAR_MISSING"),
        ("SKIPPED", "INSUFFICIENT_UNIVERSE"),
    ):
        assert OpeningMomentumShadowService._paired_policy_return(
            OpeningMomentumShadowRun(
                status=status,
                reason=reason,
            )
        ) is None


def test_missing_leader_entry_bar_records_skip_without_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_shadow_enabled",
        True,
    )
    engine, db = _database()
    try:
        _seed_universe(db)
        service = OpeningMomentumShadowService(
            db,
            _FakeCandles(missing_entry_for="S7.US"),
        )

        status = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=32, seconds=10),
        )

        assert status.latest is not None
        assert status.latest.status == "SKIPPED"
        assert status.latest.reason == "ENTRY_BAR_MISSING"
        assert status.latest.candidate_symbol == "S7.US"
        assert status.latest.entry_price is None
        assert status.metrics.skipped_sessions == 1
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_late_start_does_not_backfill_a_missed_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_shadow_enabled",
        True,
    )
    engine, db = _database()
    try:
        _seed_universe(db)
        service = OpeningMomentumShadowService(db, _FakeCandles())

        status = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=40),
        )

        assert status.state == "WAITING"
        assert status.latest is None
        assert db.query(OpeningMomentumShadowRun).count() == 0
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_disabled_service_never_fetches_market_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_shadow_enabled",
        False,
    )
    engine, db = _database()
    try:
        _seed_universe(db)
        candles = _FakeCandles()

        status = OpeningMomentumShadowService(
            db,
            candles,
        ).tick(
            now=_SESSION_OPEN + timedelta(minutes=31, seconds=10),
        )

        assert status.state == "DISABLED"
        assert candles.calls == []
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_disabled_service_closes_stale_open_run_from_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_shadow_enabled",
        False,
    )
    engine, db = _database()
    try:
        exit_at = _SESSION_OPEN + timedelta(minutes=60)
        candles = _HistoricalExitCandles(exit_at)
        service = OpeningMomentumShadowService(db, candles)
        db.add(
            OpeningMomentumShadowRun(
                session_date=date(2026, 7, 23),
                algorithm_version=ALGORITHM_VERSION,
                config_version=service._incumbent_config_version(),
                status="OPEN",
                reason="OPENING_LEADER",
                signal_at=_SESSION_OPEN + timedelta(minutes=29),
                observed_at=_SESSION_OPEN + timedelta(minutes=31),
                universe_source="UNIVERSE_SELECTION",
                universe_size=8,
                universe_json="[]",
                excluded_symbols_json="{}",
                ranking_json="[]",
                candidate_symbol="S7.US",
                entry_at=_SESSION_OPEN + timedelta(minutes=30),
                entry_price=100.0,
                exit_due_at=exit_at,
                estimated_cost_bps=14.0,
            )
        )
        db.commit()

        status = service.tick(
            now=_SESSION_OPEN + timedelta(days=3),
        )

        assert status.latest is not None
        assert status.latest.status == "CLOSED"
        assert status.latest.exit_price == 102.0
        assert status.latest.net_return_bps == pytest.approx(186.0)
        assert candles.history_calls == [exit_at - timedelta(minutes=1)]
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
