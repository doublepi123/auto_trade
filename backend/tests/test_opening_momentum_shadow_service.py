from __future__ import annotations

import json
from dataclasses import replace
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


_SESSION_OPEN = datetime(2026, 7, 29, 13, 30, tzinfo=timezone.utc)
_SYMBOLS = tuple(f"S{index}.US" for index in range(8))
_SNDK_SYMBOL = "SNDK.US"
_EXTENSION_SYMBOLS = (
    "RKLB.US",
    "WDAY.US",
    _SNDK_SYMBOL,
    "ALAB.US",
    "LITE.US",
    "QCOM.US",
)
_EXTENSION_VARIANTS = (
    "EARLY_RKLB_CHALLENGER",
    "EARLY_WDAY_CHALLENGER",
    "EARLY_SNDK_CHALLENGER",
    "EARLY_ALAB_CHALLENGER",
    "EARLY_LITE_CHALLENGER",
    "EARLY_QCOM_CHALLENGER",
)
_EXECUTION_EXTENSION_SYMBOLS = (
    _SNDK_SYMBOL,
    "INTC.US",
    "QCOM.US",
    "RKLB.US",
    "PANW.US",
    "CRWD.US",
)
_EXECUTION_EXTENSION_VARIANTS = (
    "EXECUTION_SNDK_CHALLENGER",
    "EXECUTION_INTC_CHALLENGER",
    "EXECUTION_QCOM_CHALLENGER",
    "EXECUTION_RKLB_CHALLENGER",
    "EXECUTION_PANW_CHALLENGER",
    "EXECUTION_CRWD_CHALLENGER",
)
_ETF_REGIME_VARIANTS = (
    "ETF_REGIME_PATH_CHALLENGER",
    "ETF_REGIME_CRWD_CHALLENGER",
    "ETF_REGIME_TRV_CHALLENGER",
)
_ALL_CHALLENGER_VARIANTS = (
    "EARLY_BROAD_CHALLENGER",
    *_EXTENSION_VARIANTS,
    "EXECUTION_BROAD_CHALLENGER",
    "EXECUTION_PATH_EFFICIENCY_CHALLENGER",
    "WEAK_BREADTH_PATH_CHALLENGER",
    "WEAK_BREADTH_RELAXED_CHALLENGER",
    "MODERATE_BREADTH_PATH_CHALLENGER",
    "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER",
    "QUALITY_FIRST_PATH_RERANK_CHALLENGER",
    "EXCEPTIONAL_PATH_PANW_COHORT_CHALLENGER",
    "WEAK_BREADTH_INDEX_COHORT_CHALLENGER",
    "WEAK_BREADTH_SPARSE_INDEX_COHORT_CHALLENGER",
    "WEAK_BREADTH_MRVL_EXCLUSION_CHALLENGER",
    "WEAK_BREADTH_WIDE_STOP_CHALLENGER",
    *_ETF_REGIME_VARIANTS,
    "OPENING_RANGE_STOP_CHALLENGER",
    *_EXECUTION_EXTENSION_VARIANTS,
)


class _FakeCandles:
    def __init__(
        self,
        *,
        missing_entry_for: str | None = None,
        opening_returns_bps: dict[str, float] | None = None,
        early_opening_returns_bps: dict[str, float] | None = None,
        early_path_returns_bps: (
            dict[str, tuple[float, float, float]] | None
        ) = None,
        negative_last_five_for: str | None = None,
        low_efficiency_for: str | None = None,
        unavailable_symbols: set[str] | None = None,
        turnover_per_minute_by_symbol: dict[str, float] | None = None,
    ) -> None:
        self.missing_entry_for = missing_entry_for
        self.opening_returns_bps = opening_returns_bps or {}
        self.early_opening_returns_bps = (
            early_opening_returns_bps or {}
        )
        self.early_path_returns_bps = early_path_returns_bps or {}
        self.negative_last_five_for = negative_last_five_for
        self.low_efficiency_for = low_efficiency_for
        self.unavailable_symbols = unavailable_symbols or set()
        self.turnover_per_minute_by_symbol = (
            turnover_per_minute_by_symbol or {}
        )
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
        if symbol in self.unavailable_symbols:
            raise RuntimeError("test candle unavailable")
        symbol_index = (
            _SYMBOLS.index(symbol)
            if symbol in _SYMBOLS
            else len(_SYMBOLS)
        )
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
            if symbol in self.early_path_returns_bps and index < 3:
                close_price = 100.0 * (
                    1
                    + self.early_path_returns_bps[symbol][index]
                    / 10_000
                )
            elif symbol == self.low_efficiency_for and index < 3:
                choppy_returns = (200.0, -100.0, 100.0)
                close_price = 100.0 * (
                    1 + choppy_returns[index] / 10_000
                )
            elif index == 2:
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
                    turnover=self.turnover_per_minute_by_symbol.get(
                        symbol,
                        0.0,
                    ),
                )
            )
        return bars


class _OpeningContextCandles(_FakeCandles):
    def __init__(
        self,
        *,
        benchmark_qqq_return_bps: float = 20.0,
        benchmark_dia_return_bps: float = -10.0,
        early_opening_returns_bps: dict[str, float] | None = None,
    ) -> None:
        super().__init__(
            early_opening_returns_bps=early_opening_returns_bps,
        )
        self.benchmark_returns_bps = {
            "QQQ.US": benchmark_qqq_return_bps,
            "DIA.US": benchmark_dia_return_bps,
        }
        self.history_calls: list[tuple[str, str, datetime]] = []

    def get_forward_adjusted_history_candlesticks_before(
        self,
        symbol: str,
        period: str,
        count: int,
        before: datetime,
    ) -> list[BrokerCandle]:
        self.history_calls.append((symbol, period, before))
        if period == "DAY":
            assert count == 10
            return [
                BrokerCandle(
                    timestamp=_SESSION_OPEN - timedelta(days=1),
                    open=98.0,
                    high=100.0,
                    low=97.5,
                    close=99.0,
                    volume=1_000,
                )
            ]

        assert period == "MIN_1"
        assert count == 500
        terminal_return_bps = self.benchmark_returns_bps[symbol]
        bars: list[BrokerCandle] = []
        for index in range(30):
            close = (
                100.0 * (1 + terminal_return_bps / 10_000)
                if index in {2, 29}
                else 100.0
            )
            bars.append(BrokerCandle(
                timestamp=_SESSION_OPEN + timedelta(minutes=index),
                open=100.0,
                high=max(100.0, close) + 0.1,
                low=min(100.0, close) - 0.1,
                close=close,
                volume=1_000,
            ))
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
    avg_dollar_volume: float | None = None,
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
                metrics_json=(
                    json.dumps({
                        "avg_dollar_volume": avg_dollar_volume,
                    })
                    if avg_dollar_volume is not None
                    else "{}"
                ),
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
            as_of_date=_SESSION_OPEN.date(),
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


def test_universe_free_status_preserves_forward_variant_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_challenger_enabled",
        True,
    )
    engine, db = _database()
    try:
        variants = OpeningMomentumShadowService(db)._universe_variants()
        exclusion = {
            item.variant: item for item in variants
        }["WEAK_BREADTH_MRVL_EXCLUSION_CHALLENGER"]

        assert exclusion.universe_source == "NONE"
        assert exclusion.minimum_path_efficiency == 0.70
        assert exclusion.maximum_market_return_bps == 0.0
        assert exclusion.excluded_symbols == ("MRVL.US",)
        assert exclusion.forward_evidence_start_date == date(2026, 7, 28)
        assert exclusion.symbols == ()
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_observation_only_symbols_are_isolated_from_opening_execution_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_challenger_enabled",
        True,
    )
    engine, db = _database()
    try:
        _seed_universe(db)
        _seed_active_broad_pool(db)
        for symbol in ("CRWD.US", "TRV.US"):
            db.add(StrategyV2ShadowConfig(
                symbol=symbol,
                enabled=True,
                universe_managed=False,
                opening_momentum_execution_eligible=False,
            ))
        db.add(StrategyV2ShadowConfig(
            symbol="MRVL.US",
            enabled=True,
            universe_managed=False,
            opening_momentum_execution_eligible=True,
        ))
        db.commit()

        variants = OpeningMomentumShadowService(db)._universe_variants()
        by_variant = {item.variant: item for item in variants}

        assert "CRWD.US" not in by_variant[
            "WEAK_BREADTH_PATH_CHALLENGER"
        ].symbols
        assert "TRV.US" not in by_variant[
            "WEAK_BREADTH_PATH_CHALLENGER"
        ].symbols
        assert "MRVL.US" in by_variant[
            "WEAK_BREADTH_PATH_CHALLENGER"
        ].symbols
        assert "MRVL.US" not in by_variant[
            "WEAK_BREADTH_MRVL_EXCLUSION_CHALLENGER"
        ].symbols
        assert by_variant[
            "WEAK_BREADTH_MRVL_EXCLUSION_CHALLENGER"
        ].excluded_symbols == ("MRVL.US",)
        assert "CRWD.US" in by_variant[
            "ETF_REGIME_CRWD_CHALLENGER"
        ].symbols
        assert "TRV.US" in by_variant[
            "ETF_REGIME_TRV_CHALLENGER"
        ].symbols
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_paper_execution_variant_applies_required_and_excluded_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_challenger_enabled",
        True,
    )
    engine, db = _database()
    try:
        run = _seed_universe(db)
        _seed_active_broad_pool(db)
        service = OpeningMomentumShadowService(db)
        identity = service.paper_execution_variant_identity()
        monkeypatch.setattr(
            service,
            "paper_execution_variant_identity",
            lambda: replace(
                identity,
                required_symbols=("REQUIRED.US",),
                excluded_symbols=(_SYMBOLS[0],),
            ),
        )

        variant = service.paper_execution_variant()

        assert variant is not None
        assert variant.selection_run_id == run.id
        assert variant.symbols == (*_SYMBOLS[1:], "REQUIRED.US")
        assert variant.required_symbols == ("REQUIRED.US",)
        assert variant.excluded_symbols == (_SYMBOLS[0],)
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_execution_signal_uses_only_completed_three_minute_bars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_challenger_enabled",
        True,
    )
    monkeypatch.setattr(
        settings,
        "opening_momentum_execution_max_entry_delay_seconds",
        30,
    )

    class _SignalOnlyCandles(_FakeCandles):
        def get_candlesticks(
            self,
            symbol: str,
            period: str,
            count: int,
        ) -> list[BrokerCandle]:
            return [
                bar
                for bar in super().get_candlesticks(
                    symbol,
                    period,
                    count,
                )
                if bar.timestamp
                <= _SESSION_OPEN + timedelta(minutes=2)
            ]

    engine, db = _database()
    try:
        run = _seed_universe(db, avg_dollar_volume=120_000_000.0)
        _seed_active_broad_pool(db)
        candles = _SignalOnlyCandles(
            early_opening_returns_bps={_SYMBOLS[-1]: 100.0},
            turnover_per_minute_by_symbol={
                _SYMBOLS[-1]: 2_000_000.0,
            },
        )

        signal = OpeningMomentumShadowService(
            db,
            candles,
        ).evaluate_execution_signal(
            now=_SESSION_OPEN + timedelta(minutes=3, seconds=5),
        )

        assert signal is not None
        assert signal.action == "ENTER_LONG"
        assert signal.symbol == _SYMBOLS[-1]
        assert signal.selection_run_id == run.id
        assert signal.signal_at == _SESSION_OPEN + timedelta(minutes=2)
        assert signal.entry_due_at == _SESSION_OPEN + timedelta(minutes=4)
        assert signal.reference_entry_price == pytest.approx(101.0)
        assert signal.stop_loss_pct == 1.0
        assert signal.max_holding_minutes == 60
        assert signal.universe_source == (
            "OPENING_EXECUTION_WEAK_BREADTH_EXCEPTIONAL_PATH"
        )
        assert signal.context["candidate_path_efficiency"] == 1.0
        assert signal.context["candidate_signal_turnover"] == pytest.approx(
            6_000_000.0
        )
        assert signal.context["candidate_avg_dollar_volume"] == pytest.approx(
            120_000_000.0
        )
        assert (
            signal.context["candidate_signal_turnover_ratio"]
            == pytest.approx(0.05)
        )
        assert signal.context["minimum_path_efficiency"] == 0.70
        assert signal.context["maximum_market_return_bps"] == 0.0
        assert (
            signal.context["exceptional_minimum_path_efficiency"]
            == 0.90
        )
        assert (
            signal.context["exceptional_maximum_market_return_bps"]
            == 5.0
        )
        assert signal.context[
            "effective_maximum_market_return_bps"
        ] == 5.0
        assert sorted(candles.calls) == sorted(_SYMBOLS)
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_execution_signal_skips_a_choppy_opening_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_challenger_enabled",
        True,
    )
    engine, db = _database()
    try:
        _seed_universe(db)
        _seed_active_broad_pool(db)

        signal = OpeningMomentumShadowService(
            db,
            _FakeCandles(low_efficiency_for=_SYMBOLS[-1]),
        ).evaluate_execution_signal(
            now=_SESSION_OPEN + timedelta(minutes=3, seconds=5),
        )

        assert signal is not None
        assert signal.action == "SKIP"
        assert signal.reason == "PATH_EFFICIENCY_FILTER"
        assert signal.symbol == _SYMBOLS[-1]
        assert signal.context[
            "candidate_path_efficiency"
        ] == pytest.approx(1 / 7)
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_execution_signal_skips_when_opening_breadth_is_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_challenger_enabled",
        True,
    )
    engine, db = _database()
    try:
        _seed_universe(db)
        _seed_active_broad_pool(db)
        early_returns = {
            symbol: (100.0 if symbol == _SYMBOLS[-1] else 20.0)
            for symbol in _SYMBOLS
        }

        signal = OpeningMomentumShadowService(
            db,
            _FakeCandles(
                early_opening_returns_bps=early_returns,
            ),
        ).evaluate_execution_signal(
            now=_SESSION_OPEN + timedelta(minutes=3, seconds=5),
        )

        assert signal is not None
        assert signal.action == "SKIP"
        assert signal.reason == "MAXIMUM_MARKET_RETURN_FILTER"
        assert signal.symbol == _SYMBOLS[-1]
        assert signal.market_return_bps == pytest.approx(20.0)
        assert signal.context["candidate_path_efficiency"] == 1.0
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_execution_signal_accepts_exceptional_path_at_five_bps_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_challenger_enabled",
        True,
    )
    engine, db = _database()
    try:
        _seed_universe(db)
        _seed_active_broad_pool(db)
        early_returns = {
            symbol: (100.0 if symbol == _SYMBOLS[-1] else 5.0)
            for symbol in _SYMBOLS
        }

        signal = OpeningMomentumShadowService(
            db,
            _FakeCandles(
                early_opening_returns_bps=early_returns,
            ),
        ).evaluate_execution_signal(
            now=_SESSION_OPEN + timedelta(minutes=3, seconds=5),
        )

        assert signal is not None
        assert signal.action == "ENTER_LONG"
        assert signal.reason == "OPENING_LEADER"
        assert signal.symbol == _SYMBOLS[-1]
        assert signal.market_return_bps == pytest.approx(5.0)
        assert signal.context["candidate_path_efficiency"] == 1.0
        assert signal.context[
            "effective_maximum_market_return_bps"
        ] == 5.0
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


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


def test_tick_records_only_causal_opening_context_telemetry(
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
        candles = _OpeningContextCandles()

        status = OpeningMomentumShadowService(db, candles).tick(
            now=_SESSION_OPEN + timedelta(minutes=32, seconds=10),
        )

        assert status.latest is not None
        assert status.latest.candidate_symbol == "S7.US"
        assert status.latest.candidate_overnight_gap_bps == pytest.approx(
            (100.0 / 99.0 - 1) * 10_000
        )
        assert (
            status.latest.candidate_prev_close_to_signal_bps
            == pytest.approx((101.0 / 99.0 - 1) * 10_000)
        )
        assert status.latest.benchmark_qqq_return_bps == pytest.approx(
            20.0
        )
        assert status.latest.benchmark_dia_return_bps == pytest.approx(
            -10.0
        )
        assert {
            (symbol, period)
            for symbol, period, _ in candles.history_calls
        } == {
            ("QQQ.US", "MIN_1"),
            ("DIA.US", "MIN_1"),
            ("S7.US", "DAY"),
        }
        assert all(
            before <= _SESSION_OPEN + timedelta(minutes=32, seconds=10)
            for _, _, before in candles.history_calls
        )
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_tick_records_signal_turnover_against_frozen_liquidity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_shadow_enabled",
        True,
    )
    engine, db = _database()
    try:
        _seed_universe(db, avg_dollar_volume=100_000_000.0)
        candles = _FakeCandles(
            turnover_per_minute_by_symbol={"S7.US": 1_000_000.0},
        )

        status = OpeningMomentumShadowService(db, candles).tick(
            now=_SESSION_OPEN + timedelta(minutes=32, seconds=10),
        )

        assert status.latest is not None
        assert status.latest.candidate_symbol == "S7.US"
        assert status.latest.candidate_signal_turnover == pytest.approx(
            30_000_000.0
        )
        assert status.latest.candidate_avg_dollar_volume == pytest.approx(
            100_000_000.0
        )
        assert (
            status.latest.candidate_signal_turnover_ratio
            == pytest.approx(0.30)
        )
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
    assert result[0].turnover is None


def test_stop_aware_exit_uses_first_intraday_breach_and_tracks_path() -> None:
    entry_at = _SESSION_OPEN + timedelta(minutes=4)
    exit_at = entry_at + timedelta(minutes=2)
    candles = tuple(OpeningMomentumShadowService._coerce_candles([
        BrokerCandle(
            timestamp=entry_at,
            open=100.0,
            high=101.0,
            low=99.5,
            close=100.5,
            volume=1000,
        ),
        BrokerCandle(
            timestamp=entry_at + timedelta(minutes=1),
            open=100.0,
            high=100.2,
            low=98.0,
            close=98.5,
            volume=1000,
        ),
        BrokerCandle(
            timestamp=exit_at,
            open=102.0,
            high=102.1,
            low=101.9,
            close=102.0,
            volume=1000,
        ),
    ]))

    outcome = OpeningMomentumShadowService._exit_outcome(
        candles,
        entry_at=entry_at,
        exit_due_at=exit_at,
        entry_price=100.0,
        stop_loss_pct=1.0,
    )

    assert outcome.reason == "STOP_LOSS_EXIT"
    assert outcome.exited_at == entry_at + timedelta(minutes=1)
    assert outcome.price == 99.0
    assert outcome.maximum_adverse_excursion_bps == pytest.approx(-100.0)
    assert outcome.maximum_favorable_excursion_bps == pytest.approx(100.0)


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


def test_opening_path_efficiency_measures_choppy_three_minute_path() -> None:
    candles = [
        SimpleNamespace(
            timestamp=_SESSION_OPEN + timedelta(minutes=index),
            open=100.0,
            high=max(100.0, close),
            low=min(100.0, close),
            close=close,
        )
        for index, close in enumerate((102.0, 99.0, 101.0))
    ]
    coerced = OpeningMomentumShadowService._coerce_candles(candles)

    efficiency = OpeningMomentumShadowService._opening_path_efficiency(
        coerced
    )

    assert efficiency == pytest.approx(1 / 7)


def test_opening_range_stop_uses_range_low_with_four_percent_cap() -> None:
    service = OpeningMomentumShadowService

    assert service._opening_range_stop_loss_pct(
        opening_range_low=99.9,
        entry_price=100.5,
        maximum_stop_loss_pct=4.0,
    ) == pytest.approx((1 - 99.9 / 100.5) * 100)
    assert service._opening_range_stop_loss_pct(
        opening_range_low=90.0,
        entry_price=100.0,
        maximum_stop_loss_pct=4.0,
    ) == pytest.approx(4.0)


@pytest.mark.parametrize(
    ("opening_range_low", "entry_price", "maximum_stop_loss_pct"),
    (
        (None, 100.0, 4.0),
        (100.0, 100.0, 4.0),
        (101.0, 100.0, 4.0),
        (99.0, 0.0, 4.0),
        (99.0, 100.0, 0.0),
    ),
)
def test_opening_range_stop_rejects_invalid_levels(
    opening_range_low: float | None,
    entry_price: float,
    maximum_stop_loss_pct: float,
) -> None:
    assert OpeningMomentumShadowService._opening_range_stop_loss_pct(
        opening_range_low=opening_range_low,
        entry_price=entry_price,
        maximum_stop_loss_pct=maximum_stop_loss_pct,
    ) is None


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
            "EARLY_RKLB_CHALLENGER",
            "EARLY_WDAY_CHALLENGER",
            "EARLY_SNDK_CHALLENGER",
            "EARLY_ALAB_CHALLENGER",
            "EARLY_LITE_CHALLENGER",
            "EARLY_QCOM_CHALLENGER",
            "EXECUTION_BROAD_CHALLENGER",
            "EXECUTION_PATH_EFFICIENCY_CHALLENGER",
            "WEAK_BREADTH_PATH_CHALLENGER",
            "WEAK_BREADTH_RELAXED_CHALLENGER",
            "MODERATE_BREADTH_PATH_CHALLENGER",
            "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER",
            "QUALITY_FIRST_PATH_RERANK_CHALLENGER",
            "EXCEPTIONAL_PATH_PANW_COHORT_CHALLENGER",
            "WEAK_BREADTH_INDEX_COHORT_CHALLENGER",
            "WEAK_BREADTH_SPARSE_INDEX_COHORT_CHALLENGER",
            "WEAK_BREADTH_MRVL_EXCLUSION_CHALLENGER",
            "WEAK_BREADTH_WIDE_STOP_CHALLENGER",
            "ETF_REGIME_PATH_CHALLENGER",
            "ETF_REGIME_CRWD_CHALLENGER",
            "ETF_REGIME_TRV_CHALLENGER",
            "OPENING_RANGE_STOP_CHALLENGER",
            "EXECUTION_SNDK_CHALLENGER",
            "EXECUTION_INTC_CHALLENGER",
            "EXECUTION_QCOM_CHALLENGER",
            "EXECUTION_RKLB_CHALLENGER",
            "EXECUTION_PANW_CHALLENGER",
            "EXECUTION_CRWD_CHALLENGER",
            "REVERSAL_CHALLENGER",
            "CONTINUATION_CHALLENGER",
            "BREADTH_GATED_CHALLENGER",
            "LAST5_POSITIVE_CHALLENGER",
            "LAST5_ONLY_CHALLENGER",
        }
        early = by_variant["EARLY_BROAD_CHALLENGER"]
        early_sndk = by_variant["EARLY_SNDK_CHALLENGER"]
        execution = by_variant["EXECUTION_BROAD_CHALLENGER"]
        path_efficiency = by_variant[
            "EXECUTION_PATH_EFFICIENCY_CHALLENGER"
        ]
        weak_breadth_path = by_variant[
            "WEAK_BREADTH_PATH_CHALLENGER"
        ]
        weak_breadth_relaxed = by_variant[
            "WEAK_BREADTH_RELAXED_CHALLENGER"
        ]
        moderate_breadth_path = by_variant[
            "MODERATE_BREADTH_PATH_CHALLENGER"
        ]
        weak_breadth_exceptional_path = by_variant[
            "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER"
        ]
        quality_first_path_rerank = by_variant[
            "QUALITY_FIRST_PATH_RERANK_CHALLENGER"
        ]
        exceptional_path_panw_cohort = by_variant[
            "EXCEPTIONAL_PATH_PANW_COHORT_CHALLENGER"
        ]
        weak_breadth_index_cohort = by_variant[
            "WEAK_BREADTH_INDEX_COHORT_CHALLENGER"
        ]
        weak_breadth_sparse_index_cohort = by_variant[
            "WEAK_BREADTH_SPARSE_INDEX_COHORT_CHALLENGER"
        ]
        weak_breadth_mrvl_exclusion = by_variant[
            "WEAK_BREADTH_MRVL_EXCLUSION_CHALLENGER"
        ]
        weak_breadth_wide_stop = by_variant[
            "WEAK_BREADTH_WIDE_STOP_CHALLENGER"
        ]
        etf_regime = by_variant["ETF_REGIME_PATH_CHALLENGER"]
        opening_range_stop = by_variant[
            "OPENING_RANGE_STOP_CHALLENGER"
        ]
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
        assert early.required_symbols == ()
        assert early_sndk.decision_config == early.decision_config
        assert early_sndk.minimum_data_coverage == 0.95
        assert early_sndk.required_symbols == (_SNDK_SYMBOL,)
        assert early_sndk.universe_source == "OPENING_EARLY_SNDK"
        for variant, symbol in zip(
            _EXTENSION_VARIANTS,
            _EXTENSION_SYMBOLS,
            strict=True,
        ):
            extension = by_variant[variant]
            assert extension.decision_config == early.decision_config
            assert extension.minimum_data_coverage == 0.95
            assert extension.required_symbols == (symbol,)
            assert extension.universe_source == (
                f"OPENING_EARLY_{symbol.removesuffix('.US')}"
            )
        assert execution.decision_config.signal_minutes == 3
        assert execution.decision_config.holding_minutes == 60
        assert execution.decision_config.stop_loss_pct == 1.0
        assert execution.minimum_data_coverage == 0.95
        assert execution.universe_source == "OPENING_EXECUTION_BROAD"
        assert execution.required_symbols == ()
        assert path_efficiency.decision_config == execution.decision_config
        assert path_efficiency.minimum_data_coverage == 0.95
        assert path_efficiency.minimum_path_efficiency == 0.70
        assert path_efficiency.required_symbols == ()
        assert path_efficiency.universe_source == (
            "OPENING_EXECUTION_PATH_EFFICIENCY"
        )
        assert (
            weak_breadth_path.decision_config
            == execution.decision_config
        )
        assert weak_breadth_path.minimum_data_coverage == 0.95
        assert weak_breadth_path.minimum_path_efficiency == 0.70
        assert weak_breadth_path.maximum_market_return_bps == 0.0
        assert weak_breadth_path.required_symbols == ()
        assert weak_breadth_path.universe_source == (
            "OPENING_EXECUTION_WEAK_BREADTH_PATH"
        )
        assert (
            weak_breadth_relaxed.decision_config
            == execution.decision_config
        )
        assert weak_breadth_relaxed.minimum_data_coverage == 0.95
        assert weak_breadth_relaxed.minimum_path_efficiency == 0.70
        assert weak_breadth_relaxed.maximum_market_return_bps == 5.0
        assert weak_breadth_relaxed.required_symbols == ()
        assert weak_breadth_relaxed.universe_source == (
            "OPENING_EXECUTION_WEAK_BREADTH_RELAXED"
        )
        assert (
            moderate_breadth_path.decision_config
            == execution.decision_config
        )
        assert moderate_breadth_path.minimum_data_coverage == 0.95
        assert moderate_breadth_path.minimum_path_efficiency == 0.70
        assert moderate_breadth_path.maximum_market_return_bps == 20.0
        assert moderate_breadth_path.required_symbols == ()
        assert moderate_breadth_path.universe_source == (
            "OPENING_EXECUTION_MODERATE_BREADTH_PATH"
        )
        assert "forward-only-post-20260727" in (
            moderate_breadth_path.algorithm_version
        )
        assert moderate_breadth_path.forward_evidence_start_date == date(
            2026,
            7,
            28,
        )
        assert (
            weak_breadth_exceptional_path.decision_config
            == execution.decision_config
        )
        assert weak_breadth_exceptional_path.minimum_data_coverage == 0.95
        assert (
            weak_breadth_exceptional_path.minimum_path_efficiency
            == 0.70
        )
        assert (
            weak_breadth_exceptional_path.maximum_market_return_bps
            == 0.0
        )
        assert (
            weak_breadth_exceptional_path
            .exceptional_minimum_path_efficiency
            == 0.90
        )
        assert (
            weak_breadth_exceptional_path
            .exceptional_maximum_market_return_bps
            == 5.0
        )
        assert weak_breadth_exceptional_path.required_symbols == ()
        assert weak_breadth_exceptional_path.universe_source == (
            "OPENING_EXECUTION_WEAK_BREADTH_EXCEPTIONAL_PATH"
        )
        assert "forward-only-post-20260727" in (
            weak_breadth_exceptional_path.algorithm_version
        )
        assert (
            weak_breadth_exceptional_path
            .effective_maximum_market_return_bps(0.89)
            == 0.0
        )
        assert (
            weak_breadth_exceptional_path
            .effective_maximum_market_return_bps(0.90)
            == 5.0
        )
        assert (
            weak_breadth_exceptional_path.forward_evidence_start_date
            == date(2026, 7, 28)
        )
        assert (
            quality_first_path_rerank.decision_config
            == execution.decision_config
        )
        assert (
            quality_first_path_rerank.candidate_selection_mode
            == "PATH_ELIGIBLE_RERANK"
        )
        assert quality_first_path_rerank.minimum_data_coverage == 0.95
        assert quality_first_path_rerank.minimum_path_efficiency == 0.70
        assert quality_first_path_rerank.maximum_market_return_bps == 0.0
        assert (
            quality_first_path_rerank
            .exceptional_minimum_path_efficiency
            == 0.90
        )
        assert (
            quality_first_path_rerank
            .exceptional_maximum_market_return_bps
            == 5.0
        )
        assert quality_first_path_rerank.universe_source == (
            "OPENING_EXECUTION_QUALITY_FIRST_PATH_RERANK"
        )
        assert quality_first_path_rerank.forward_evidence_start_date == date(
            2026,
            7,
            28,
        )
        assert (
            service.paper_execution_variant_identity()
            .candidate_selection_mode
            == "TOP_THEN_GATE"
        )
        assert (
            exceptional_path_panw_cohort.decision_config
            == execution.decision_config
        )
        assert exceptional_path_panw_cohort.minimum_path_efficiency == 0.70
        assert exceptional_path_panw_cohort.maximum_market_return_bps == 0.0
        assert (
            exceptional_path_panw_cohort
            .exceptional_minimum_path_efficiency
            == 0.90
        )
        assert (
            exceptional_path_panw_cohort
            .exceptional_maximum_market_return_bps
            == 5.0
        )
        assert exceptional_path_panw_cohort.required_symbols == ("PANW.US",)
        assert exceptional_path_panw_cohort.universe_source == (
            "OPENING_EXECUTION_EXCEPTIONAL_PANW_COHORT"
        )
        assert "holdout-contradicted" in (
            exceptional_path_panw_cohort.algorithm_version
        )
        assert (
            exceptional_path_panw_cohort.forward_evidence_start_date
            == date(2026, 7, 28)
        )
        assert (
            weak_breadth_index_cohort.decision_config
            == execution.decision_config
        )
        assert weak_breadth_index_cohort.minimum_data_coverage == 0.95
        assert weak_breadth_index_cohort.minimum_path_efficiency == 0.70
        assert weak_breadth_index_cohort.maximum_market_return_bps == 0.0
        assert weak_breadth_index_cohort.required_symbols == ("PANW.US",)
        assert weak_breadth_index_cohort.universe_source == (
            "OPENING_EXECUTION_WEAK_BREADTH_INDEX_COHORT"
        )
        assert "forward-only-discovery-joint-subset" in (
            weak_breadth_index_cohort.algorithm_version
        )
        assert (
            weak_breadth_sparse_index_cohort.decision_config
            == execution.decision_config
        )
        assert (
            weak_breadth_sparse_index_cohort.minimum_data_coverage
            == 0.95
        )
        assert (
            weak_breadth_sparse_index_cohort.minimum_path_efficiency
            == 0.70
        )
        assert (
            weak_breadth_sparse_index_cohort.maximum_market_return_bps
            == 0.0
        )
        assert weak_breadth_sparse_index_cohort.required_symbols == (
            "SNDK.US",
            "STX.US",
            "CRWD.US",
            "ABNB.US",
            "CPRT.US",
        )
        assert weak_breadth_sparse_index_cohort.universe_source == (
            "OPENING_EXECUTION_WEAK_BREADTH_SPARSE_INDEX_COHORT"
        )
        assert "forward-only-discovery-joint-sparse-index" in (
            weak_breadth_sparse_index_cohort.algorithm_version
        )
        assert (
            weak_breadth_mrvl_exclusion.decision_config
            == execution.decision_config
        )
        assert weak_breadth_mrvl_exclusion.minimum_data_coverage == 0.95
        assert weak_breadth_mrvl_exclusion.minimum_path_efficiency == 0.70
        assert (
            weak_breadth_mrvl_exclusion.maximum_market_return_bps
            == 0.0
        )
        assert weak_breadth_mrvl_exclusion.required_symbols == ()
        assert weak_breadth_mrvl_exclusion.excluded_symbols == (
            "MRVL.US",
        )
        assert weak_breadth_mrvl_exclusion.universe_source == (
            "OPENING_EXECUTION_WEAK_BREADTH_EX_MRVL"
        )
        assert "holdout-contradicted" in (
            weak_breadth_mrvl_exclusion.algorithm_version
        )
        assert (
            weak_breadth_mrvl_exclusion.forward_evidence_start_date
            == date(2026, 7, 28)
        )
        assert etf_regime.decision_config == execution.decision_config
        assert etf_regime.minimum_data_coverage == 0.95
        assert etf_regime.minimum_path_efficiency == 0.70
        assert etf_regime.maximum_market_return_bps is None
        assert (
            etf_regime.maximum_benchmark_average_return_bps
            == 0.0
        )
        assert etf_regime.forward_evidence_start_date == date(2026, 7, 28)
        assert etf_regime.required_symbols == ()
        assert etf_regime.universe_source == (
            "OPENING_EXECUTION_ETF_REGIME"
        )
        for variant, symbol in (
            ("ETF_REGIME_CRWD_CHALLENGER", "CRWD.US"),
            ("ETF_REGIME_TRV_CHALLENGER", "TRV.US"),
        ):
            extension = by_variant[variant]
            assert extension.decision_config == execution.decision_config
            assert extension.minimum_path_efficiency == 0.70
            assert (
                extension.maximum_benchmark_average_return_bps
                == 0.0
            )
            assert extension.forward_evidence_start_date == date(2026, 7, 28)
            assert extension.required_symbols == (symbol,)
        assert (
            weak_breadth_wide_stop.decision_config.signal_minutes
            == weak_breadth_path.decision_config.signal_minutes
        )
        assert (
            weak_breadth_wide_stop.decision_config.holding_minutes
            == 60
        )
        assert (
            weak_breadth_wide_stop.decision_config.stop_loss_pct
            == 4.0
        )
        assert weak_breadth_wide_stop.minimum_data_coverage == 0.95
        assert weak_breadth_wide_stop.minimum_path_efficiency == 0.70
        assert (
            weak_breadth_wide_stop.maximum_market_return_bps
            == 0.0
        )
        assert weak_breadth_wide_stop.required_symbols == ()
        assert weak_breadth_wide_stop.universe_source == (
            "OPENING_EXECUTION_WEAK_BREADTH_WIDE_STOP"
        )
        assert opening_range_stop.decision_config.signal_minutes == 3
        assert opening_range_stop.decision_config.holding_minutes == 60
        assert opening_range_stop.decision_config.stop_loss_pct == 4.0
        assert opening_range_stop.minimum_data_coverage == 0.95
        assert opening_range_stop.opening_range_stop is True
        assert opening_range_stop.required_symbols == ()
        assert opening_range_stop.universe_source == (
            "OPENING_EXECUTION_RANGE_STOP"
        )
        for variant, symbol in zip(
            _EXECUTION_EXTENSION_VARIANTS,
            _EXECUTION_EXTENSION_SYMBOLS,
            strict=True,
        ):
            extension = by_variant[variant]
            assert extension.decision_config == execution.decision_config
            assert extension.minimum_data_coverage == 0.95
            assert extension.required_symbols == (symbol,)
            assert extension.universe_source == (
                f"OPENING_EXECUTION_{symbol.removesuffix('.US')}"
            )
        crwd = by_variant["EXECUTION_CRWD_CHALLENGER"]
        assert "forward-only-two-slice-positive-tail" in (
            crwd.algorithm_version
        )
        assert crwd.config_version != by_variant[
            "EXECUTION_PANW_CHALLENGER"
        ].config_version
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
        ) == len(identities)
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_forward_scorecard_excludes_rows_before_the_frozen_start(
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
        identities = {
            identity.variant: identity
            for identity in service._variant_identities()
        }
        baseline = identities["WEAK_BREADTH_PATH_CHALLENGER"]
        challenger = identities[
            "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER"
        ]
        for session_date, baseline_return, challenger_return in (
            (date(2026, 7, 27), 0.0, 100.0),
            (date(2026, 7, 28), 0.0, 20.0),
        ):
            timestamp = datetime.combine(
                session_date,
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
            for identity, net_return_bps in (
                (baseline, baseline_return),
                (challenger, challenger_return),
            ):
                db.add(OpeningMomentumShadowRun(
                    session_date=session_date,
                    algorithm_version=identity.algorithm_version,
                    config_version=identity.config_version,
                    status="CLOSED",
                    reason="FIXED_HOLD_EXIT",
                    signal_at=timestamp,
                    observed_at=timestamp,
                    universe_source=identity.universe_source,
                    universe_size=41,
                    candidate_symbol="ISRG.US",
                    estimated_cost_bps=14.0,
                    net_return_bps=net_return_bps,
                ))
        db.commit()

        response = {
            item.variant: item
            for item in service._variant_responses()
        }["WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER"]

        assert response.forward_evidence_start_date == date(2026, 7, 28)
        assert response.excluded_pre_forward_sessions == 1
        assert response.comparison_sessions == 1
        assert response.latest is not None
        assert response.latest.session_date == date(2026, 7, 28)
        assert response.metrics.observed_sessions == 1
        assert response.metrics.closed_trades == 1
        assert response.metrics.cumulative_net_return_bps == 20.0
        assert response.comparison is not None
        assert response.comparison.resolved_sessions == 1
        assert response.comparison.cumulative_delta_bps == 20.0
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
            "EARLY_BROAD_CHALLENGER",
            "EARLY_SNDK_CHALLENGER",
            "EXECUTION_BROAD_CHALLENGER",
            "WEAK_BREADTH_PATH_CHALLENGER",
            "WEAK_BREADTH_WIDE_STOP_CHALLENGER",
            "OPENING_RANGE_STOP_CHALLENGER",
            "EXECUTION_SNDK_CHALLENGER",
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
        early_sndk = by_variant["EARLY_SNDK_CHALLENGER"]
        execution_sndk = by_variant["EXECUTION_SNDK_CHALLENGER"]
        weak_breadth_wide_stop = by_variant[
            "WEAK_BREADTH_WIDE_STOP_CHALLENGER"
        ]
        opening_range_stop = by_variant[
            "OPENING_RANGE_STOP_CHALLENGER"
        ]
        breadth = by_variant["BREADTH_GATED_CHALLENGER"]
        assert continuation.comparison_sessions == 1
        assert continuation.comparison is not None
        assert continuation.comparison.resolved_sessions == 1
        assert continuation.comparison.mean_delta_bps == 0.0
        assert continuation.comparison_baseline == "INCUMBENT"
        assert early_sndk.comparison_sessions == 1
        assert early_sndk.comparison is not None
        assert early_sndk.comparison.resolved_sessions == 1
        assert early_sndk.comparison.mean_delta_bps == 0.0
        assert (
            early_sndk.comparison_baseline
            == "EARLY_BROAD_CHALLENGER"
        )
        assert execution_sndk.comparison_sessions == 1
        assert execution_sndk.comparison is not None
        assert execution_sndk.comparison.resolved_sessions == 1
        assert execution_sndk.comparison.mean_delta_bps == 0.0
        assert (
            execution_sndk.comparison_baseline
            == "EXECUTION_BROAD_CHALLENGER"
        )
        assert weak_breadth_wide_stop.comparison_sessions == 1
        assert weak_breadth_wide_stop.comparison is not None
        assert (
            weak_breadth_wide_stop.comparison.resolved_sessions
            == 1
        )
        assert weak_breadth_wide_stop.comparison.mean_delta_bps == 0.0
        assert weak_breadth_wide_stop.comparison_baseline == (
            "WEAK_BREADTH_PATH_CHALLENGER"
        )
        assert opening_range_stop.comparison_sessions == 1
        assert opening_range_stop.comparison is not None
        assert opening_range_stop.comparison.resolved_sessions == 1
        assert opening_range_stop.comparison.mean_delta_bps == 0.0
        assert opening_range_stop.comparison_baseline == (
            "EXECUTION_BROAD_CHALLENGER"
        )
        assert breadth.comparison_sessions == 0
        assert breadth.comparison is not None
        assert breadth.comparison.resolved_sessions == 0
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_extension_promotion_requires_actual_policy_displacements(
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
        identities = {
            identity.variant: identity
            for identity in service._variant_identities()
        }
        baseline = identities["EARLY_BROAD_CHALLENGER"]
        candidates = (
            (
                identities["EARLY_RKLB_CHALLENGER"],
                "RKLB.US",
                8,
            ),
            (
                identities["EARLY_WDAY_CHALLENGER"],
                "WDAY.US",
                2,
            ),
            (
                identities["EARLY_ALAB_CHALLENGER"],
                "ALAB.US",
                5,
            ),
        )
        for index in range(20):
            session_date = _SESSION_OPEN.date() + timedelta(days=index)
            timestamp = _SESSION_OPEN + timedelta(days=index)
            db.add(OpeningMomentumShadowRun(
                session_date=session_date,
                algorithm_version=baseline.algorithm_version,
                config_version=baseline.config_version,
                status="SKIPPED",
                reason="MARKET_FILTER",
                signal_at=timestamp,
                observed_at=timestamp,
                universe_source=baseline.universe_source,
                universe_size=40,
                estimated_cost_bps=14.0,
            ))
            for identity, symbol, displacement_count in candidates:
                displaced = index < displacement_count
                db.add(OpeningMomentumShadowRun(
                    session_date=session_date,
                    algorithm_version=identity.algorithm_version,
                    config_version=identity.config_version,
                    status="CLOSED" if displaced else "SKIPPED",
                    reason=(
                        "OPENING_LEADER"
                        if displaced
                        else "MARKET_FILTER"
                    ),
                    signal_at=timestamp,
                    observed_at=timestamp,
                    universe_source=identity.universe_source,
                    universe_size=41,
                    candidate_symbol=symbol if displaced else None,
                    estimated_cost_bps=14.0,
                    net_return_bps=100.0 if displaced else None,
                ))
        db.commit()

        responses = {
            item.variant: item
            for item in service._variant_responses()
        }
        rklb = responses["EARLY_RKLB_CHALLENGER"].comparison
        assert rklb is not None
        assert rklb.resolved_sessions == 20
        assert rklb.policy_displacement_sessions == 8
        assert rklb.minimum_policy_displacement_sessions == 3
        assert rklb.displacement_outperformance_rate == 1.0
        assert rklb.evidence_gate_passed is True
        assert rklb.confidence_lower_bps is not None
        assert rklb.confidence_lower_bps > 0
        assert rklb.multiple_testing_method == "HOLM_BONFERRONI"
        assert rklb.multiple_testing_family_size == 6
        assert rklb.multiple_testing_adjusted_pvalue is not None
        assert rklb.multiple_testing_adjusted_pvalue < 0.05
        assert rklb.multiple_testing_evidence_passed is True
        assert rklb.promotion_ready is True
        assert rklb.recommendation == "PROMOTION_CANDIDATE"

        wday = responses["EARLY_WDAY_CHALLENGER"].comparison
        assert wday is not None
        assert wday.resolved_sessions == 20
        assert wday.policy_displacement_sessions == 2
        assert wday.displacement_outperformance_rate == 1.0
        assert wday.evidence_gate_passed is False
        assert wday.multiple_testing_evidence_passed is False
        assert wday.promotion_ready is False
        assert wday.recommendation == "COLLECTING"

        alab = responses["EARLY_ALAB_CHALLENGER"].comparison
        assert alab is not None
        assert alab.confidence_lower_bps is not None
        assert alab.confidence_lower_bps > 0
        assert alab.evidence_gate_passed is True
        assert alab.multiple_testing_adjusted_pvalue is not None
        assert alab.multiple_testing_adjusted_pvalue > 0.05
        assert alab.multiple_testing_evidence_passed is False
        assert alab.promotion_ready is False
        assert alab.recommendation == "INCONCLUSIVE"
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
        assert len(opened.variants) == 35
        by_variant = {
            item.variant: item for item in opened.variants
        }
        incumbent = by_variant["INCUMBENT"]
        early = by_variant["EARLY_BROAD_CHALLENGER"]
        early_sndk = by_variant["EARLY_SNDK_CHALLENGER"]
        reversal = by_variant["REVERSAL_CHALLENGER"]
        challenger = by_variant["CONTINUATION_CHALLENGER"]
        breadth = by_variant["BREADTH_GATED_CHALLENGER"]
        last_five = by_variant["LAST5_POSITIVE_CHALLENGER"]
        last_five_only = by_variant["LAST5_ONLY_CHALLENGER"]
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
        assert early_sndk.variant == "EARLY_SNDK_CHALLENGER"
        assert early_sndk.required_symbols == [_SNDK_SYMBOL]
        assert (
            early_sndk.comparison_baseline
            == "EARLY_BROAD_CHALLENGER"
        )
        assert early_sndk.comparison_sessions == 0
        assert early_sndk.latest is None
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
        ) == len(opened.variants)

        still_open = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=47, seconds=10),
        )

        rows = db.query(OpeningMomentumShadowRun).all()
        assert {row.status for row in rows} == {"OPEN"}
        assert still_open.state == "OPEN"
        assert all(
            item.metrics.closed_trades == 0
            for item in still_open.variants
        )

        closed = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=62, seconds=10),
        )

        rows = db.query(OpeningMomentumShadowRun).all()
        assert {row.status for row in rows} == {"CLOSED"}
        assert closed.state == "COLLECTING"
        closed_by_variant = {
            item.variant: item for item in closed.variants
        }
        standard_variants = (
            "INCUMBENT",
            "REVERSAL_CHALLENGER",
            "CONTINUATION_CHALLENGER",
            "BREADTH_GATED_CHALLENGER",
            "LAST5_POSITIVE_CHALLENGER",
            "LAST5_ONLY_CHALLENGER",
        )
        for variant in standard_variants:
            metrics = closed_by_variant[variant].metrics
            assert metrics.closed_trades == 1
            assert metrics.cumulative_net_return_bps == -14.0
        for variant in _ALL_CHALLENGER_VARIANTS:
            metrics = closed_by_variant[variant].metrics
            assert metrics.closed_trades == 0
            assert metrics.cumulative_net_return_bps == 0.0
        assert early.comparison is not None
        assert early.comparison.resolved_sessions == 0
        assert early_sndk.comparison is not None
        assert early_sndk.comparison.resolved_sessions == 0
        for variant in standard_variants[1:]:
            item = closed_by_variant[variant]
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
        config = OpeningMomentumConfig(
            minimum_universe_size=2,
            minimum_excess_return_bps=0,
        )
        service = OpeningMomentumShadowService(
            db,
            candles,
            config=config,
        )

        early_opened = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=5, seconds=10),
        )

        rows = db.query(OpeningMomentumShadowRun).all()
        assert len(rows) == 29
        assert candles.calls == [
            *_SYMBOLS,
            *_EXTENSION_SYMBOLS,
            "PANW.US",
            "STX.US",
            "CRWD.US",
            "ABNB.US",
            "CPRT.US",
            "TRV.US",
            "INTC.US",
        ]
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
        assert early.latest.candidate_path_efficiency == pytest.approx(1.0)
        assert early.latest.candidate_max_pullback_bps is None
        early_sndk = {
            item.variant: item for item in early_opened.variants
        }["EARLY_SNDK_CHALLENGER"]
        assert early_sndk.latest is not None
        assert early_sndk.latest.status == "OPEN"
        assert early_sndk.latest.universe == [
            *_SYMBOLS,
            _SNDK_SYMBOL,
        ]
        assert early_sndk.required_symbols == [_SNDK_SYMBOL]
        assert (
            early_sndk.comparison_baseline
            == "EARLY_BROAD_CHALLENGER"
        )
        early_by_variant = {
            item.variant: item for item in early_opened.variants
        }
        for variant, symbol in zip(
            _EXTENSION_VARIANTS,
            _EXTENSION_SYMBOLS,
            strict=True,
        ):
            extension = early_by_variant[variant]
            assert extension.latest is not None
            assert extension.latest.universe == [*_SYMBOLS, symbol]
            assert extension.required_symbols == [symbol]
            assert (
                extension.comparison_baseline
                == "EARLY_BROAD_CHALLENGER"
            )

        execution = early_by_variant["EXECUTION_BROAD_CHALLENGER"]
        assert execution.latest is not None
        assert execution.latest.status == "OPEN"
        assert execution.latest.universe == list(_SYMBOLS)
        assert execution.latest.exit_due_at == (
            _SESSION_OPEN + timedelta(minutes=64)
        )
        assert execution.latest.stop_loss_pct == 1.0
        assert execution.holding_minutes == 60
        assert execution.comparison_baseline == "INCUMBENT"
        path_efficiency = early_by_variant[
            "EXECUTION_PATH_EFFICIENCY_CHALLENGER"
        ]
        assert path_efficiency.latest is not None
        assert path_efficiency.latest.status == "OPEN"
        assert path_efficiency.minimum_path_efficiency == 0.70
        assert path_efficiency.comparison_baseline == (
            "EXECUTION_BROAD_CHALLENGER"
        )
        weak_breadth_wide_stop = early_by_variant[
            "WEAK_BREADTH_WIDE_STOP_CHALLENGER"
        ]
        assert weak_breadth_wide_stop.latest is not None
        assert weak_breadth_wide_stop.latest.status == "OPEN"
        assert weak_breadth_wide_stop.latest.stop_loss_pct == 4.0
        assert weak_breadth_wide_stop.comparison_baseline == (
            "WEAK_BREADTH_PATH_CHALLENGER"
        )
        weak_breadth_mrvl_exclusion = early_by_variant[
            "WEAK_BREADTH_MRVL_EXCLUSION_CHALLENGER"
        ]
        assert weak_breadth_mrvl_exclusion.latest is not None
        assert weak_breadth_mrvl_exclusion.latest.status == "OPEN"
        assert weak_breadth_mrvl_exclusion.excluded_symbols == ["MRVL.US"]
        assert weak_breadth_mrvl_exclusion.latest.universe == list(_SYMBOLS)
        assert weak_breadth_mrvl_exclusion.comparison_baseline == (
            "WEAK_BREADTH_PATH_CHALLENGER"
        )
        etf_regime = early_by_variant[
            "ETF_REGIME_PATH_CHALLENGER"
        ]
        assert etf_regime.latest is not None
        assert etf_regime.latest.status == "SKIPPED"
        assert etf_regime.latest.reason == (
            "BENCHMARK_DATA_INCOMPLETE"
        )
        assert etf_regime.latest.candidate_symbol == "S7.US"
        assert etf_regime.comparison_baseline == (
            "WEAK_BREADTH_PATH_CHALLENGER"
        )
        opening_range_stop = early_by_variant[
            "OPENING_RANGE_STOP_CHALLENGER"
        ]
        assert opening_range_stop.latest is not None
        assert opening_range_stop.latest.status == "OPEN"
        assert opening_range_stop.latest.stop_loss_pct == pytest.approx(
            (1 - 99.9 / 100.5) * 100
        )
        assert opening_range_stop.comparison_baseline == (
            "EXECUTION_BROAD_CHALLENGER"
        )
        for variant, symbol in zip(
            _EXECUTION_EXTENSION_VARIANTS,
            _EXECUTION_EXTENSION_SYMBOLS,
            strict=True,
        ):
            extension = early_by_variant[variant]
            assert extension.latest is not None
            assert extension.latest.universe == [*_SYMBOLS, symbol]
            assert extension.required_symbols == [symbol]
            assert (
                extension.comparison_baseline
                == "EXECUTION_BROAD_CHALLENGER"
            )

        candles.calls.clear()
        standard_opened = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=32, seconds=10),
        )

        assert db.query(OpeningMomentumShadowRun).count() == 35
        assert candles.calls == list(_SYMBOLS[:4])
        by_variant = {
            item.variant: item for item in standard_opened.variants
        }
        assert by_variant["INCUMBENT"].latest is not None
        assert by_variant["EARLY_BROAD_CHALLENGER"].latest is not None
        assert by_variant["EARLY_SNDK_CHALLENGER"].latest is not None
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
        assert sum(row.status == "SKIPPED" for row in rows) == 4
        assert sum(row.status == "OPEN" for row in rows) == 26
        by_variant = {
            item.variant: item for item in standard_closed.variants
        }
        assert by_variant["INCUMBENT"].metrics.closed_trades == 1
        assert (
            by_variant["EARLY_BROAD_CHALLENGER"]
            .metrics.closed_trades
            == 0
        )
        assert (
            by_variant["EARLY_SNDK_CHALLENGER"]
            .metrics.closed_trades
            == 0
        )
        for variant in _ALL_CHALLENGER_VARIANTS:
            assert by_variant[variant].metrics.closed_trades == 0

        service = OpeningMomentumShadowService(
            db,
            candles,
            config=config,
        )
        candles.calls.clear()
        execution_closed = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=65, seconds=10),
        )

        rows = db.query(OpeningMomentumShadowRun).all()
        assert candles.calls == ["S7.US"]
        assert execution_closed.state == "OPEN"
        assert sum(row.status == "CLOSED" for row in rows) == 24
        assert sum(row.status == "SKIPPED" for row in rows) == 4
        assert sum(row.status == "OPEN" for row in rows) == 7
        execution_by_variant = {
            item.variant: item for item in execution_closed.variants
        }
        assert (
            execution_by_variant["EXECUTION_BROAD_CHALLENGER"]
            .metrics.closed_trades
            == 1
        )
        assert (
            execution_by_variant[
                "EXECUTION_PATH_EFFICIENCY_CHALLENGER"
            ].metrics.closed_trades
            == 1
        )
        assert (
            execution_by_variant[
                "WEAK_BREADTH_PATH_CHALLENGER"
            ].metrics.closed_trades
            == 1
        )
        assert (
            execution_by_variant[
                "WEAK_BREADTH_RELAXED_CHALLENGER"
            ].metrics.closed_trades
            == 1
        )
        assert (
            execution_by_variant[
                "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER"
            ].metrics.closed_trades
            == 1
        )
        assert (
            execution_by_variant[
                "QUALITY_FIRST_PATH_RERANK_CHALLENGER"
            ].metrics.closed_trades
            == 1
        )
        assert (
            execution_by_variant[
                "EXCEPTIONAL_PATH_PANW_COHORT_CHALLENGER"
            ].metrics.closed_trades
            == 1
        )
        assert (
            execution_by_variant[
                "WEAK_BREADTH_MRVL_EXCLUSION_CHALLENGER"
            ].metrics.closed_trades
            == 1
        )
        assert (
            execution_by_variant[
                "WEAK_BREADTH_INDEX_COHORT_CHALLENGER"
            ].metrics.closed_trades
            == 1
        )
        assert (
            execution_by_variant[
                "WEAK_BREADTH_WIDE_STOP_CHALLENGER"
            ].metrics.closed_trades
            == 1
        )
        assert (
            execution_by_variant[
                "OPENING_RANGE_STOP_CHALLENGER"
            ].metrics.closed_trades
            == 1
        )
        opening_range_closed = execution_by_variant[
            "OPENING_RANGE_STOP_CHALLENGER"
        ].latest
        assert opening_range_closed is not None
        assert opening_range_closed.reason == "STOP_LOSS_EXIT"
        assert opening_range_closed.exit_at == (
            _SESSION_OPEN + timedelta(minutes=4)
        )
        assert opening_range_closed.exit_price == pytest.approx(99.9)
        for variant in _EXECUTION_EXTENSION_VARIANTS:
            assert (
                execution_by_variant[variant].metrics.closed_trades
                == 1
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
        early_sndk_closed = {
            item.variant: item for item in all_closed.variants
        }["EARLY_SNDK_CHALLENGER"]
        assert early_sndk_closed.latest is not None
        assert early_sndk_closed.latest.status == "CLOSED"
        assert early_sndk_closed.metrics.closed_trades == 1
        assert early_sndk_closed.comparison is not None
        assert early_sndk_closed.comparison.resolved_sessions == 1
        assert early_sndk_closed.comparison.mean_delta_bps == 0.0
        assert (
            early_sndk_closed.comparison
            .policy_displacement_sessions
            == 0
        )
        assert (
            early_sndk_closed.comparison
            .minimum_policy_displacement_sessions
            == 3
        )
        assert (
            early_sndk_closed.comparison.evidence_gate_passed
            is False
        )
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_early_sndk_challenger_requires_sndk_market_data(
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
    engine, db = _database()
    try:
        _seed_variant_universe(db)
        _seed_active_broad_pool(db)
        candles = _FakeCandles(
            early_opening_returns_bps={"S7.US": 100.0},
            unavailable_symbols={_SNDK_SYMBOL},
        )
        service = OpeningMomentumShadowService(
            db,
            candles,
            config=OpeningMomentumConfig(
                minimum_universe_size=2,
                minimum_excess_return_bps=0,
            ),
        )

        status = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=5, seconds=10),
        )

        by_variant = {
            item.variant: item for item in status.variants
        }
        early = by_variant["EARLY_BROAD_CHALLENGER"]
        early_sndk = by_variant["EARLY_SNDK_CHALLENGER"]
        assert early.latest is not None
        assert early.latest.status == "OPEN"
        assert early_sndk.latest is not None
        assert early_sndk.latest.status == "SKIPPED"
        assert early_sndk.latest.reason == "DATA_INCOMPLETE"
        assert early_sndk.latest.excluded_symbols == {
            _SNDK_SYMBOL: "BROKER_ERROR:RuntimeError",
        }
        assert early_sndk.metrics.signals == 0
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_execution_path_efficiency_challenger_skips_choppy_leader(
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
    engine, db = _database()
    try:
        _seed_variant_universe(db)
        _seed_active_broad_pool(db)
        service = OpeningMomentumShadowService(
            db,
            _FakeCandles(low_efficiency_for="S7.US"),
            config=OpeningMomentumConfig(
                minimum_universe_size=2,
                minimum_excess_return_bps=0,
            ),
        )

        status = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=5, seconds=10),
        )

        by_variant = {
            item.variant: item for item in status.variants
        }
        baseline = by_variant["EXECUTION_BROAD_CHALLENGER"]
        challenger = by_variant[
            "EXECUTION_PATH_EFFICIENCY_CHALLENGER"
        ]
        assert baseline.latest is not None
        assert baseline.latest.status == "OPEN"
        assert baseline.latest.candidate_symbol == "S7.US"
        assert challenger.latest is not None
        assert challenger.latest.status == "SKIPPED"
        assert challenger.latest.reason == "PATH_EFFICIENCY_FILTER"
        assert challenger.latest.candidate_symbol == "S7.US"
        assert (
            challenger.latest.candidate_path_efficiency
            == pytest.approx(1 / 7)
        )
        assert challenger.latest.entry_price is None
        assert challenger.minimum_path_efficiency == 0.70
        assert challenger.comparison_baseline == (
            "EXECUTION_BROAD_CHALLENGER"
        )
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_quality_first_rerank_uses_next_path_eligible_candidate(
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
    engine, db = _database()
    try:
        _seed_variant_universe(db)
        _seed_active_broad_pool(db)
        service = OpeningMomentumShadowService(
            db,
            _FakeCandles(
                early_opening_returns_bps={"S6.US": 80.0},
                low_efficiency_for="S7.US",
            ),
            config=OpeningMomentumConfig(
                minimum_universe_size=2,
                minimum_excess_return_bps=0,
            ),
        )

        status = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=5, seconds=10),
        )

        by_variant = {
            item.variant: item for item in status.variants
        }
        paper_policy = by_variant[
            "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER"
        ]
        quality_first = by_variant[
            "QUALITY_FIRST_PATH_RERANK_CHALLENGER"
        ]
        assert paper_policy.latest is not None
        assert paper_policy.latest.status == "SKIPPED"
        assert paper_policy.latest.reason == "PATH_EFFICIENCY_FILTER"
        assert paper_policy.latest.candidate_symbol == "S7.US"
        assert quality_first.latest is not None
        assert quality_first.latest.status == "OPEN"
        assert quality_first.latest.reason == (
            "PATH_ELIGIBLE_OPENING_LEADER"
        )
        assert quality_first.latest.candidate_symbol == "S6.US"
        assert quality_first.latest.candidate_return_bps == pytest.approx(
            80.0
        )
        assert quality_first.latest.candidate_path_efficiency == 1.0
        assert quality_first.candidate_selection_mode == (
            "PATH_ELIGIBLE_RERANK"
        )
        assert quality_first.comparison_baseline == (
            "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER"
        )
        assert quality_first.forward_evidence_start_date == date(
            2026,
            7,
            28,
        )
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.mark.parametrize(
    ("market_return_bps", "expected_status", "expected_reason"),
    [
        (20.0, "SKIPPED", "MAXIMUM_MARKET_RETURN_FILTER"),
        (-20.0, "OPEN", "OPENING_LEADER"),
    ],
)
def test_weak_breadth_path_challenger_applies_maximum_market_gate(
    monkeypatch: pytest.MonkeyPatch,
    market_return_bps: float,
    expected_status: str,
    expected_reason: str,
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
    engine, db = _database()
    try:
        _seed_variant_universe(db)
        _seed_active_broad_pool(db)
        early_returns = {
            symbol: (
                100.0 if symbol == "S7.US" else market_return_bps
            )
            for symbol in _SYMBOLS
        }
        service = OpeningMomentumShadowService(
            db,
            _FakeCandles(
                early_opening_returns_bps=early_returns,
            ),
            config=OpeningMomentumConfig(
                minimum_universe_size=2,
                minimum_excess_return_bps=0,
            ),
        )

        status = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=5, seconds=10),
        )

        by_variant = {
            item.variant: item for item in status.variants
        }
        baseline = by_variant["EXECUTION_BROAD_CHALLENGER"]
        challenger = by_variant["WEAK_BREADTH_PATH_CHALLENGER"]
        assert baseline.latest is not None
        assert baseline.latest.status == "OPEN"
        assert challenger.latest is not None
        assert challenger.latest.status == expected_status
        assert challenger.latest.reason == expected_reason
        assert challenger.latest.market_return_bps == pytest.approx(
            market_return_bps
        )
        assert challenger.minimum_path_efficiency == 0.70
        assert challenger.maximum_market_return_bps == 0.0
        assert challenger.comparison_baseline == (
            "EXECUTION_BROAD_CHALLENGER"
        )
        assert challenger.latest.entry_price == (
            100.5 if expected_status == "OPEN" else None
        )
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_relaxed_weak_breadth_challenger_is_forward_only_at_three_bps(
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
    engine, db = _database()
    try:
        _seed_variant_universe(db)
        _seed_active_broad_pool(db)
        early_returns = {
            symbol: 100.0 if symbol == "S7.US" else 3.0
            for symbol in _SYMBOLS
        }
        service = OpeningMomentumShadowService(
            db,
            _FakeCandles(
                early_opening_returns_bps=early_returns,
            ),
            config=OpeningMomentumConfig(
                minimum_universe_size=2,
                minimum_excess_return_bps=0,
            ),
        )

        status = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=5, seconds=10),
        )

        by_variant = {
            item.variant: item for item in status.variants
        }
        production = by_variant["WEAK_BREADTH_PATH_CHALLENGER"]
        relaxed = by_variant["WEAK_BREADTH_RELAXED_CHALLENGER"]
        assert production.latest is not None
        assert production.latest.status == "SKIPPED"
        assert production.latest.reason == "MAXIMUM_MARKET_RETURN_FILTER"
        assert relaxed.latest is not None
        assert relaxed.latest.status == "OPEN"
        assert relaxed.latest.reason == "OPENING_LEADER"
        assert relaxed.latest.market_return_bps == pytest.approx(3.0)
        assert relaxed.latest.entry_price == 100.5
        assert relaxed.maximum_market_return_bps == 5.0
        assert relaxed.comparison_baseline == (
            "WEAK_BREADTH_PATH_CHALLENGER"
        )

        closed_status = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=65, seconds=10),
        )
        closed_relaxed = {
            item.variant: item for item in closed_status.variants
        }["WEAK_BREADTH_RELAXED_CHALLENGER"]
        assert closed_relaxed.latest is not None
        assert closed_relaxed.latest.status == "CLOSED"
        assert closed_relaxed.comparison is not None
        assert closed_relaxed.comparison.resolved_sessions == 1
        assert (
            closed_relaxed.comparison.policy_displacement_sessions
            == 1
        )
        assert (
            closed_relaxed.comparison
            .minimum_policy_displacement_sessions
            == 3
        )
        assert (
            closed_relaxed.comparison.displacement_outperformance_rate
            == 0.0
        )
        assert closed_relaxed.comparison.evidence_gate_passed is False
        assert closed_relaxed.comparison.promotion_ready is False
        assert closed_relaxed.comparison.recommendation == "COLLECTING"
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.mark.parametrize(
    ("market_return_bps", "expected_status"),
    [
        (20.0, "OPEN"),
        (20.1, "SKIPPED"),
    ],
)
def test_moderate_breadth_path_challenger_is_forward_only(
    monkeypatch: pytest.MonkeyPatch,
    market_return_bps: float,
    expected_status: str,
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
    engine, db = _database()
    try:
        _seed_variant_universe(db)
        _seed_active_broad_pool(db)
        early_returns = {
            symbol: (
                100.0 if symbol == "S7.US" else market_return_bps
            )
            for symbol in _SYMBOLS
        }
        service = OpeningMomentumShadowService(
            db,
            _FakeCandles(
                early_opening_returns_bps=early_returns,
            ),
            config=OpeningMomentumConfig(
                minimum_universe_size=2,
                minimum_excess_return_bps=0,
            ),
        )

        status = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=5, seconds=10),
        )

        by_variant = {
            item.variant: item for item in status.variants
        }
        executor = by_variant[
            "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER"
        ]
        challenger = by_variant[
            "MODERATE_BREADTH_PATH_CHALLENGER"
        ]
        assert executor.latest is not None
        assert executor.latest.status == "SKIPPED"
        assert executor.latest.reason == "MAXIMUM_MARKET_RETURN_FILTER"
        assert challenger.latest is not None
        assert challenger.latest.status == expected_status
        assert challenger.latest.reason == (
            "OPENING_LEADER"
            if expected_status == "OPEN"
            else "MAXIMUM_MARKET_RETURN_FILTER"
        )
        assert challenger.latest.market_return_bps == pytest.approx(
            market_return_bps
        )
        assert challenger.minimum_path_efficiency == 0.70
        assert challenger.maximum_market_return_bps == 20.0
        assert challenger.forward_evidence_start_date == date(
            2026,
            7,
            28,
        )
        assert challenger.comparison_baseline == (
            "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER"
        )
        assert service.paper_execution_variant_identity().variant == (
            "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER"
        )
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.mark.parametrize(
    (
        "market_return_bps",
        "candidate_path_returns_bps",
        "expected_status",
        "expected_path_efficiency",
    ),
    (
        (3.0, None, "OPEN", 1.0),
        (3.0, (8.823529, 0.0, 100.0), "SKIPPED", 0.85),
        (6.0, None, "SKIPPED", 1.0),
    ),
)
def test_exceptional_path_gate_only_relaxes_mild_positive_breadth(
    monkeypatch: pytest.MonkeyPatch,
    market_return_bps: float,
    candidate_path_returns_bps: tuple[float, float, float] | None,
    expected_status: str,
    expected_path_efficiency: float,
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
    engine, db = _database()
    try:
        _seed_variant_universe(db)
        _seed_active_broad_pool(db)
        early_returns = {
            symbol: (
                100.0 if symbol == "S7.US" else market_return_bps
            )
            for symbol in _SYMBOLS
        }
        early_paths = (
            {"S7.US": candidate_path_returns_bps}
            if candidate_path_returns_bps is not None
            else None
        )
        service = OpeningMomentumShadowService(
            db,
            _FakeCandles(
                early_opening_returns_bps=early_returns,
                early_path_returns_bps=early_paths,
            ),
            config=OpeningMomentumConfig(
                minimum_universe_size=2,
                minimum_excess_return_bps=0,
            ),
        )

        status = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=5, seconds=10),
        )

        by_variant = {
            item.variant: item for item in status.variants
        }
        production = by_variant["WEAK_BREADTH_PATH_CHALLENGER"]
        exceptional = by_variant[
            "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER"
        ]
        assert production.latest is not None
        assert production.latest.status == "SKIPPED"
        assert production.latest.reason == (
            "MAXIMUM_MARKET_RETURN_FILTER"
        )
        assert exceptional.latest is not None
        assert exceptional.latest.status == expected_status
        assert exceptional.latest.reason == (
            "OPENING_LEADER"
            if expected_status == "OPEN"
            else "MAXIMUM_MARKET_RETURN_FILTER"
        )
        assert exceptional.latest.market_return_bps == pytest.approx(
            market_return_bps
        )
        assert (
            exceptional.latest.candidate_path_efficiency
            == pytest.approx(expected_path_efficiency)
        )
        assert exceptional.latest.entry_price == (
            100.5 if expected_status == "OPEN" else None
        )
        assert exceptional.minimum_path_efficiency == 0.70
        assert exceptional.maximum_market_return_bps == 0.0
        assert exceptional.exceptional_minimum_path_efficiency == 0.90
        assert exceptional.exceptional_maximum_market_return_bps == 5.0
        assert exceptional.comparison_baseline == (
            "WEAK_BREADTH_PATH_CHALLENGER"
        )
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_weak_breadth_index_cohort_can_displace_production_candidate(
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
    engine, db = _database()
    try:
        _seed_variant_universe(db)
        _seed_active_broad_pool(db)
        early_returns = {
            symbol: 100.0 if symbol == "S7.US" else 0.0
            for symbol in _SYMBOLS
        }
        early_returns["PANW.US"] = 200.0
        service = OpeningMomentumShadowService(
            db,
            _FakeCandles(
                early_opening_returns_bps=early_returns,
            ),
            config=OpeningMomentumConfig(
                minimum_universe_size=2,
                minimum_excess_return_bps=0,
            ),
        )

        status = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=5, seconds=10),
        )

        by_variant = {
            item.variant: item for item in status.variants
        }
        production = by_variant["WEAK_BREADTH_PATH_CHALLENGER"]
        cohort = by_variant[
            "WEAK_BREADTH_INDEX_COHORT_CHALLENGER"
        ]
        exceptional_cohort = by_variant[
            "EXCEPTIONAL_PATH_PANW_COHORT_CHALLENGER"
        ]
        sparse_cohort = by_variant[
            "WEAK_BREADTH_SPARSE_INDEX_COHORT_CHALLENGER"
        ]
        assert production.latest is not None
        assert production.latest.status == "OPEN"
        assert production.latest.candidate_symbol == "S7.US"
        assert cohort.latest is not None
        assert cohort.latest.status == "OPEN"
        assert cohort.latest.reason == "OPENING_LEADER"
        assert cohort.latest.candidate_symbol == "PANW.US"
        assert cohort.latest.entry_price == 100.0
        assert cohort.latest.market_return_bps == pytest.approx(0.0)
        assert cohort.latest.universe == [
            *_SYMBOLS,
            "PANW.US",
        ]
        assert cohort.minimum_path_efficiency == 0.70
        assert cohort.maximum_market_return_bps == 0.0
        assert cohort.required_symbols == ["PANW.US"]
        assert cohort.comparison_baseline == (
            "WEAK_BREADTH_PATH_CHALLENGER"
        )
        assert cohort.comparison is not None
        assert cohort.comparison.minimum_policy_displacement_sessions == 3
        assert exceptional_cohort.latest is not None
        assert exceptional_cohort.latest.status == "OPEN"
        assert exceptional_cohort.latest.candidate_symbol == "PANW.US"
        assert exceptional_cohort.latest.universe == [
            *_SYMBOLS,
            "PANW.US",
        ]
        assert exceptional_cohort.minimum_path_efficiency == 0.70
        assert exceptional_cohort.maximum_market_return_bps == 0.0
        assert exceptional_cohort.exceptional_minimum_path_efficiency == 0.90
        assert exceptional_cohort.exceptional_maximum_market_return_bps == 5.0
        assert exceptional_cohort.required_symbols == ["PANW.US"]
        assert exceptional_cohort.comparison_baseline == (
            "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER"
        )
        assert exceptional_cohort.comparison is not None
        assert (
            exceptional_cohort.comparison.minimum_policy_displacement_sessions
            == 3
        )
        assert sparse_cohort.latest is not None
        assert sparse_cohort.latest.status == "OPEN"
        assert sparse_cohort.latest.candidate_symbol == "S7.US"
        assert sparse_cohort.latest.universe == [
            *_SYMBOLS,
            "SNDK.US",
            "STX.US",
            "CRWD.US",
            "ABNB.US",
            "CPRT.US",
        ]
        assert sparse_cohort.required_symbols == [
            "SNDK.US",
            "STX.US",
            "CRWD.US",
            "ABNB.US",
            "CPRT.US",
        ]
        assert sparse_cohort.comparison_baseline == (
            "WEAK_BREADTH_PATH_CHALLENGER"
        )
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.mark.parametrize(
    (
        "benchmark_qqq_return_bps",
        "benchmark_dia_return_bps",
        "expected_status",
        "expected_reason",
    ),
    [
        (20.0, 10.0, "SKIPPED", "BENCHMARK_AVERAGE_RETURN_FILTER"),
        (-20.0, 10.0, "OPEN", "OPENING_LEADER"),
    ],
)
def test_etf_regime_path_challenger_uses_benchmark_average_gate(
    monkeypatch: pytest.MonkeyPatch,
    benchmark_qqq_return_bps: float,
    benchmark_dia_return_bps: float,
    expected_status: str,
    expected_reason: str,
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
    engine, db = _database()
    try:
        _seed_variant_universe(db)
        _seed_active_broad_pool(db)
        service = OpeningMomentumShadowService(
            db,
            _OpeningContextCandles(
                benchmark_qqq_return_bps=benchmark_qqq_return_bps,
                benchmark_dia_return_bps=benchmark_dia_return_bps,
                early_opening_returns_bps={
                    symbol: (
                        100.0 if symbol == "S7.US" else 20.0
                    )
                    for symbol in _SYMBOLS
                },
            ),
            config=OpeningMomentumConfig(
                minimum_universe_size=2,
                minimum_excess_return_bps=0,
            ),
        )

        status = service.tick(
            now=_SESSION_OPEN + timedelta(minutes=5, seconds=10),
        )

        by_variant = {
            item.variant: item for item in status.variants
        }
        weak_breadth = by_variant[
            "WEAK_BREADTH_PATH_CHALLENGER"
        ]
        challenger = by_variant["ETF_REGIME_PATH_CHALLENGER"]
        assert weak_breadth.latest is not None
        assert weak_breadth.latest.status == "SKIPPED"
        assert weak_breadth.latest.reason == (
            "MAXIMUM_MARKET_RETURN_FILTER"
        )
        assert challenger.latest is not None
        assert challenger.latest.status == expected_status
        assert challenger.latest.reason == expected_reason
        assert challenger.latest.candidate_symbol == "S7.US"
        assert challenger.latest.benchmark_qqq_return_bps == pytest.approx(
            benchmark_qqq_return_bps
        )
        assert challenger.latest.benchmark_dia_return_bps == pytest.approx(
            benchmark_dia_return_bps
        )
        assert (
            challenger.latest.benchmark_average_return_bps
            == pytest.approx(
                (
                    benchmark_qqq_return_bps
                    + benchmark_dia_return_bps
                )
                / 2
            )
        )
        assert (
            challenger.maximum_benchmark_average_return_bps
            == 0.0
        )
        assert challenger.comparison_baseline == (
            "WEAK_BREADTH_PATH_CHALLENGER"
        )
        assert challenger.latest.entry_price == (
            100.5 if expected_status == "OPEN" else None
        )
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
        assert len(status.variants) == 35
        by_variant = {
            item.variant: item for item in status.variants
        }
        incumbent = by_variant["INCUMBENT"]
        early = by_variant["EARLY_BROAD_CHALLENGER"]
        early_sndk = by_variant["EARLY_SNDK_CHALLENGER"]
        reversal = by_variant["REVERSAL_CHALLENGER"]
        continuation = by_variant["CONTINUATION_CHALLENGER"]
        breadth = by_variant["BREADTH_GATED_CHALLENGER"]
        last_five = by_variant["LAST5_POSITIVE_CHALLENGER"]
        last_five_only = by_variant["LAST5_ONLY_CHALLENGER"]
        assert incumbent.latest is not None
        assert incumbent.latest.status == "OPEN"
        assert early.variant == "EARLY_BROAD_CHALLENGER"
        assert early.latest is None
        assert early_sndk.variant == "EARLY_SNDK_CHALLENGER"
        assert early_sndk.latest is None
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
    assert OpeningMomentumShadowService._paired_policy_return(
        OpeningMomentumShadowRun(
            status="SKIPPED",
            reason="PATH_EFFICIENCY_FILTER",
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
                session_date=_SESSION_OPEN.date(),
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
