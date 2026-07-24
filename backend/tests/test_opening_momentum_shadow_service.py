from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

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
    ) -> None:
        self.missing_entry_for = missing_entry_for
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
        opening_return_bps = (
            100.0 if symbol_index == 7 else float(symbol_index)
        )
        bars: list[BrokerCandle] = []
        for index in range(61):
            if (
                symbol == self.missing_entry_for
                and index == 30
            ):
                continue
            open_price = 100.0
            close_price = 100.0
            if index == 29:
                close_price = 100.0 * (
                    1 + opening_return_bps / 10_000
                )
            if index == 30:
                open_price = 100.5 if symbol_index == 7 else 100.0
            if index == 60:
                open_price = 101.5 if symbol_index == 7 else 100.0
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


def _seed_universe(db: Session) -> UniverseSelectionRun:
    run = UniverseSelectionRun(
        as_of_date=date(2026, 7, 22),
        algorithm_version="test-v1",
        source_version="test",
        status="COMPLETE",
        candidate_count=8,
        evaluable_count=8,
        selected_count=8,
        coverage_ratio=1.0,
        completed_at=_SESSION_OPEN - timedelta(days=1),
    )
    db.add(run)
    db.flush()
    for rank, symbol in enumerate(_SYMBOLS, start=1):
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

        opened = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=31, seconds=10),
        )

        assert opened.state == "OPEN"
        assert opened.latest is not None
        assert opened.latest.status == "OPEN"
        assert opened.latest.selection_run_id == run.id
        assert opened.latest.candidate_symbol == "S7.US"
        assert opened.latest.entry_at == _SESSION_OPEN + timedelta(minutes=30)
        assert opened.latest.entry_price == 100.5
        assert opened.latest.estimated_cost_bps == 14.0
        assert opened.latest.universe == list(_SYMBOLS)
        assert opened.latest.excluded_symbols == {}

        closed = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=61, seconds=10),
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


def test_challenger_uses_one_market_snapshot_and_closes_both_variants(
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
        candles = _FakeCandles()
        service = OpeningMomentumShadowService(
            db,
            candles,
            config=OpeningMomentumConfig(
                minimum_universe_size=2,
                minimum_excess_return_bps=0,
            ),
        )

        opened = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=31, seconds=10),
        )

        assert db.query(OpeningMomentumShadowRun).count() == 2
        assert candles.calls == list(_SYMBOLS[:4])
        assert opened.state == "OPEN"
        assert opened.latest is not None
        assert opened.latest.universe_source == "UNIVERSE_SELECTION"
        assert opened.latest.candidate_symbol == "S1.US"
        assert opened.latest.selection_run_id == run.id
        assert len(opened.variants) == 2
        incumbent, challenger = opened.variants
        assert incumbent.variant == "INCUMBENT"
        assert incumbent.comparison_sessions == 1
        assert incumbent.latest is not None
        assert incumbent.latest.candidate_symbol == "S1.US"
        assert challenger.variant == "CONTINUATION_CHALLENGER"
        assert challenger.comparison_sessions == 1
        assert challenger.latest is not None
        assert challenger.latest.universe == ["S2.US", "S3.US"]
        assert challenger.latest.candidate_symbol == "S3.US"

        closed = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=61, seconds=10),
        )

        rows = db.query(OpeningMomentumShadowRun).all()
        assert {row.status for row in rows} == {"CLOSED"}
        assert closed.state == "COLLECTING"
        assert [item.metrics.closed_trades for item in closed.variants] == [
            1,
            1,
        ]
        assert [item.metrics.cumulative_net_return_bps for item in closed.variants] == [
            -14.0,
            -14.0,
        ]
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


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
            now=_SESSION_OPEN + timedelta(minutes=31, seconds=10),
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
        config = OpeningMomentumConfig()
        exit_at = _SESSION_OPEN + timedelta(minutes=60)
        db.add(
            OpeningMomentumShadowRun(
                session_date=date(2026, 7, 23),
                algorithm_version=ALGORITHM_VERSION,
                config_version=config.version_hash(),
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
        candles = _HistoricalExitCandles(exit_at)

        status = OpeningMomentumShadowService(
            db,
            candles,
        ).tick(
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
