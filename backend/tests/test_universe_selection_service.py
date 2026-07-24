from __future__ import annotations

from multiprocessing import get_context
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol, cast

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.broker import BrokerCandle, Quote
from app.domain.universe_selection import (
    IndexCandidate,
    UniverseSelectionConfig,
)
from app.models import (
    Base,
    StrategyConfig,
    StrategyV2ShadowConfig,
    TrackedEntry,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
    WatchlistItem,
)
from app.schemas import StrategyV2ShadowConfigUpdate
from app.services.universe_selection_service import UniverseSelectionService

_NOW = datetime(2026, 7, 24, 18, 0, tzinfo=timezone.utc)
_CATALOG = (
    IndexCandidate(
        "AAPL.US",
        "Apple",
        "Technology Hardware",
        ("NASDAQ_100", "DJIA"),
    ),
    IndexCandidate(
        "JPM.US",
        "JPMorgan Chase",
        "Financials",
        ("DJIA",),
    ),
)


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _config() -> UniverseSelectionConfig:
    return UniverseSelectionConfig(
        max_selected=2,
        max_per_sector=1,
        min_avg_dollar_volume=100_000_000,
        max_relative_spread_bps=20,
        min_realized_vol_20d=0.01,
        max_realized_vol_20d=3.0,
        min_atr_pct_14d=0.1,
        max_atr_pct_14d=20.0,
    )


def _daily_bars(symbol: str) -> list[BrokerCandle]:
    bars: list[BrokerCandle] = []
    price = 100.0 if symbol == "AAPL.US" else 200.0
    start = datetime(2026, 6, 24, 4, tzinfo=timezone.utc)
    for index in range(30):
        move = 0.012 if index % 2 == 0 else -0.008
        close = price * (1 + move)
        bars.append(
            BrokerCandle(
                timestamp=start + timedelta(days=index),
                open=price,
                high=max(price, close) * 1.01,
                low=min(price, close) * 0.99,
                close=close,
                volume=20_000_000,
            )
        )
        price = close
    return bars


class _FakeBroker:
    def __init__(self, *, failing: bool = False) -> None:
        self.failing = failing
        self.quote_calls = 0
        self.candle_calls = 0

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        self.quote_calls += 1
        if self.failing:
            raise RuntimeError("quotes unavailable")
        return [
            Quote(
                symbol=symbol,
                last_price=100.0,
                bid=99.99,
                ask=100.01,
                timestamp=datetime(
                    2026,
                    7,
                    23,
                    20,
                    tzinfo=timezone.utc,
                ).isoformat(),
            )
            for symbol in symbols
        ]

    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[BrokerCandle]:
        assert period == "DAY"
        self.candle_calls += 1
        if self.failing:
            raise RuntimeError("daily bars unavailable")
        return _daily_bars(symbol)[-count:]


class _EventLike(Protocol):
    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...

    def is_set(self) -> bool: ...


class _QueueLike(Protocol):
    def put(self, item: object) -> None: ...


class _CoordinatedBroker(_FakeBroker):
    def __init__(
        self,
        *,
        failing: bool,
        evaluation_started: _EventLike,
        release_evaluation: _EventLike,
    ) -> None:
        super().__init__(failing=failing)
        self.evaluation_started = evaluation_started
        self.release_evaluation = release_evaluation
        self._announced = False

    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[BrokerCandle]:
        if not self._announced:
            self._announced = True
            self.evaluation_started.set()
            if not self.release_evaluation.wait(timeout=15):
                raise TimeoutError("test did not release catalog evaluation")
        return super().get_candlesticks(symbol, period, count)


def _concurrent_refresh_worker(
    database_path: str,
    *,
    failing: bool,
    worker_started: _EventLike,
    evaluation_started: _EventLike,
    release_evaluation: _EventLike,
    result_queue: _QueueLike,
) -> None:
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"timeout": 15},
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA busy_timeout=15000")
    db = sessionmaker(bind=engine)()
    broker = _CoordinatedBroker(
        failing=failing,
        evaluation_started=evaluation_started,
        release_evaluation=release_evaluation,
    )
    try:
        worker_started.set()
        result = UniverseSelectionService(
            db,
            broker,
            catalog=_CATALOG,
            config=_config(),
            minimum_evaluable_ratio=0.5,
            minimum_residency_days=1,
            apply_to_watchlist=False,
            enable_shadow=False,
            now=_NOW,
        ).refresh(apply_to_watchlist=False)
        result_queue.put(
            {
                "run_id": result.run.id,
                "status": result.run.status,
                "selected_count": result.run.selected_count,
                "selected_symbols": sorted(
                    item.symbol for item in result.items if item.selected
                ),
                "item_count": len(result.items),
            }
        )
    except Exception as exc:
        result_queue.put({"error": repr(exc)})
    finally:
        db.close()
        engine.dispose()


def _service(
    db: Session,
    broker: _FakeBroker,
    *,
    enable_shadow: bool = False,
) -> UniverseSelectionService:
    return UniverseSelectionService(
        db,
        broker,
        catalog=_CATALOG,
        config=_config(),
        minimum_evaluable_ratio=0.5,
        minimum_residency_days=1,
        apply_to_watchlist=True,
        enable_shadow=enable_shadow,
        now=_NOW,
    )


def test_default_selection_config_uses_active_strategy_fee_rate() -> None:
    db = _db()
    try:
        db.add(StrategyConfig(symbol="NVDA.US", fee_rate_us=0.0012))
        db.commit()

        service = UniverseSelectionService(
            db,
            _FakeBroker(),
            catalog=_CATALOG,
            minimum_evaluable_ratio=0.5,
            minimum_residency_days=1,
            apply_to_watchlist=False,
            enable_shadow=False,
            now=_NOW,
        )

        assert service.config.round_trip_fee_bps == 24.0
        assert not db.dirty
    finally:
        db.close()


def test_refresh_persists_and_applies_read_only_candidates_idempotently() -> None:
    db = _db()
    broker = _FakeBroker()
    try:
        db.add(StrategyConfig(symbol="NVDA.US", market="US"))
        db.add(
            WatchlistItem(
                symbol="NVDA.US",
                market="US",
                alias="NVIDIA",
                source="manual",
                is_active=True,
            )
        )
        db.commit()
        service = _service(db, broker)

        first = service.refresh()
        calls_after_first = (broker.quote_calls, broker.candle_calls)
        second = service.refresh()

        assert first.run.status == "COMPLETE"
        assert first.applied is True
        assert set(first.added_symbols) == {"AAPL.US", "JPM.US"}
        assert second.run.id == first.run.id
        assert set(second.retained_symbols) == {"AAPL.US", "JPM.US"}
        assert (broker.quote_calls, broker.candle_calls) == calls_after_first
        rows = {
            row.symbol: row
            for row in db.query(WatchlistItem).all()
        }
        assert rows["NVDA.US"].source == "manual"
        assert rows["NVDA.US"].is_active is True
        assert rows["AAPL.US"].source == "universe"
        assert rows["AAPL.US"].is_active is False
        assert db.query(UniverseSelectionRun).count() == 1
        assert db.query(UniverseSelectionCandidate).count() == 2
    finally:
        db.close()


def test_degraded_same_day_run_retries_and_recovers_in_place() -> None:
    db = _db()
    broker = _FakeBroker(failing=True)
    try:
        service = _service(db, broker)

        degraded = service.refresh()
        degraded_status = degraded.run.status
        degraded_run_id = degraded.run.id
        broker.failing = False
        recovered = service.refresh()

        assert degraded_status == "DEGRADED"
        assert recovered.run.status == "COMPLETE"
        assert recovered.run.id == degraded_run_id
        assert recovered.run.selected_count == 2
        assert db.query(UniverseSelectionRun).count() == 1
        assert db.query(UniverseSelectionCandidate).count() == 2
    finally:
        db.close()


def test_abandoned_running_claim_is_taken_over_after_lease() -> None:
    db = _db()
    try:
        degraded = _service(
            db,
            _FakeBroker(failing=True),
        ).refresh(apply_to_watchlist=False)
        run = db.get(UniverseSelectionRun, degraded.run.id)
        assert run is not None
        run.status = "RUNNING"
        run.error = "refresh-claim:abandoned"
        run.started_at = datetime.now(timezone.utc) - timedelta(
            minutes=10,
        )
        run.completed_at = None
        db.commit()

        recovered = _service(
            db,
            _FakeBroker(),
        ).refresh(apply_to_watchlist=False)

        assert recovered.run.id == degraded.run.id
        assert recovered.run.status == "COMPLETE"
        assert recovered.run.selected_count == 2
        assert len(recovered.items) == recovered.run.candidate_count == 2
        assert (
            sum(item.selected for item in recovered.items)
            == recovered.run.selected_count
        )
    finally:
        db.close()


def test_degraded_retry_has_one_cross_process_claim_and_atomic_evidence(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "universe-cas.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"timeout": 15},
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        connection.exec_driver_sql("PRAGMA busy_timeout=15000")
    Base.metadata.create_all(engine)
    seed_db = sessionmaker(bind=engine)()
    try:
        seeded = UniverseSelectionService(
            seed_db,
            _FakeBroker(failing=True),
            catalog=_CATALOG,
            config=_config(),
            minimum_evaluable_ratio=0.5,
            minimum_residency_days=1,
            apply_to_watchlist=False,
            enable_shadow=False,
            now=_NOW,
        ).refresh(apply_to_watchlist=False)
        seeded_run_id = seeded.run.id
        assert seeded.run.status == "DEGRADED"
    finally:
        seed_db.close()
        engine.dispose()

    context = get_context("spawn")
    healthy_worker_started = context.Event()
    healthy_evaluation_started = context.Event()
    release_healthy = context.Event()
    healthy_results = context.Queue()
    failing_worker_started = context.Event()
    failing_evaluation_started = context.Event()
    release_failing = context.Event()
    failing_results = context.Queue()
    healthy_process = context.Process(
        target=_concurrent_refresh_worker,
        kwargs={
            "database_path": str(database_path),
            "failing": False,
            "worker_started": healthy_worker_started,
            "evaluation_started": healthy_evaluation_started,
            "release_evaluation": release_healthy,
            "result_queue": healthy_results,
        },
    )
    failing_process = context.Process(
        target=_concurrent_refresh_worker,
        kwargs={
            "database_path": str(database_path),
            "failing": True,
            "worker_started": failing_worker_started,
            "evaluation_started": failing_evaluation_started,
            "release_evaluation": release_failing,
            "result_queue": failing_results,
        },
    )
    try:
        healthy_process.start()
        assert healthy_worker_started.wait(timeout=10)
        assert healthy_evaluation_started.wait(timeout=10)

        # The healthy worker owns the retry but is deliberately paused after
        # claiming it. Under the old read/evaluate/write flow this delayed
        # failing worker also evaluated the same DEGRADED run and could
        # replace the healthy candidate rows after COMPLETE was committed.
        failing_process.start()
        assert failing_worker_started.wait(timeout=10)
        failing_worker_evaluated = failing_evaluation_started.wait(
            timeout=1,
        )

        release_healthy.set()
        healthy_process.join(timeout=15)
        assert not healthy_process.is_alive()
        assert healthy_process.exitcode == 0

        release_failing.set()
        failing_process.join(timeout=15)
        assert not failing_process.is_alive()
        assert failing_process.exitcode == 0
    finally:
        release_healthy.set()
        release_failing.set()
        for process in (healthy_process, failing_process):
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)

    healthy_result = cast(
        dict[str, object],
        healthy_results.get(timeout=5),
    )
    failing_result = cast(
        dict[str, object],
        failing_results.get(timeout=5),
    )
    assert "error" not in healthy_result
    assert "error" not in failing_result
    assert healthy_result == failing_result
    assert healthy_result == {
        "run_id": seeded_run_id,
        "status": "COMPLETE",
        "selected_count": 2,
        "selected_symbols": ["AAPL.US", "JPM.US"],
        "item_count": 2,
    }
    assert failing_worker_evaluated is False
    assert failing_evaluation_started.is_set() is False

    verify_engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"timeout": 15},
    )
    verify_db = sessionmaker(bind=verify_engine)()
    try:
        final_run = verify_db.get(
            UniverseSelectionRun,
            seeded_run_id,
        )
        assert final_run is not None
        final_items = (
            verify_db.query(UniverseSelectionCandidate)
            .filter(
                UniverseSelectionCandidate.run_id == seeded_run_id,
            )
            .all()
        )
        assert final_run.status == "COMPLETE"
        assert len(final_items) == final_run.candidate_count == 2
        assert (
            sum(item.selected for item in final_items)
            == final_run.selected_count
            == 2
        )
        assert all(
            "DATA_" not in item.exclusion_reasons_json
            for item in final_items
        )
    finally:
        verify_db.close()
        verify_engine.dispose()


def test_stale_consensus_session_fails_closed_on_expected_run_date() -> None:
    class _StaleBroker(_FakeBroker):
        def get_candlesticks(
            self,
            symbol: str,
            period: str,
            count: int,
        ) -> list[BrokerCandle]:
            return super().get_candlesticks(
                symbol,
                period,
                count,
            )[:-3]

    db = _db()
    try:
        result = _service(db, _StaleBroker()).refresh()

        assert result.run.as_of_date.isoformat() == "2026-07-23"
        assert result.run.status == "DEGRADED"
        assert result.run.selected_count == 0
        assert result.run.evaluable_count == 0
        assert all(
            "DATA_STALE_SESSION_DATE" in row.exclusion_reasons_json
            for row in result.items
        )
        assert db.query(WatchlistItem).count() == 0
    finally:
        db.close()


def test_reconcile_removes_expired_auto_item_but_keeps_live_exposure() -> None:
    db = _db()
    broker = _FakeBroker()
    try:
        old = _NOW - timedelta(days=3)
        db.add_all(
            [
                WatchlistItem(
                    symbol="REMOVE.US",
                    market="US",
                    alias="Remove",
                    source="universe",
                    created_at=old,
                ),
                WatchlistItem(
                    symbol="KEEP.US",
                    market="US",
                    alias="Keep",
                    source="universe",
                    created_at=old,
                ),
                TrackedEntry(
                    symbol="KEEP.US",
                    side="LONG",
                    quantity=1,
                    cost=100,
                ),
            ]
        )
        db.commit()

        result = _service(db, broker).refresh()

        symbols = {
            row.symbol
            for row in db.query(WatchlistItem).all()
        }
        assert "REMOVE.US" not in symbols
        assert "KEEP.US" in symbols
        assert "REMOVE.US" in result.removed_symbols
        assert "KEEP.US" in result.retained_symbols
    finally:
        db.close()


def test_reconcile_disables_shadow_owned_by_removed_universe_item() -> None:
    db = _db()
    try:
        db.add(
            WatchlistItem(
                symbol="REMOVE.US",
                market="US",
                alias="Remove",
                source="universe",
                created_at=_NOW - timedelta(days=3),
            ),
        )
        db.add(
            StrategyV2ShadowConfig(
                symbol="REMOVE.US",
                enabled=True,
                universe_managed=True,
            ),
        )
        db.commit()

        result = _service(
            db,
            _FakeBroker(),
            enable_shadow=True,
        ).refresh()

        config = (
            db.query(StrategyV2ShadowConfig)
            .filter(StrategyV2ShadowConfig.symbol == "REMOVE.US")
            .one()
        )
        assert "REMOVE.US" in result.removed_symbols
        assert config.enabled is False
        assert config.universe_managed is True
        assert result.shadow_disabled_symbols == ("REMOVE.US",)
    finally:
        db.close()


def test_reconcile_never_disables_manually_enabled_shadow() -> None:
    db = _db()
    try:
        db.add(
            StrategyV2ShadowConfig(
                symbol="MANUAL.US",
                enabled=True,
                universe_managed=False,
            ),
        )
        db.commit()

        _service(
            db,
            _FakeBroker(),
            enable_shadow=True,
        ).refresh()

        config = (
            db.query(StrategyV2ShadowConfig)
            .filter(StrategyV2ShadowConfig.symbol == "MANUAL.US")
            .one()
        )
        assert config.enabled is True
        assert config.universe_managed is False
    finally:
        db.close()


def test_manual_disable_is_not_undone_by_next_universe_refresh() -> None:
    from app.services.strategy_v2_shadow_service import (
        StrategyV2ShadowService,
    )

    db = _db()
    try:
        service = _service(
            db,
            _FakeBroker(),
            enable_shadow=True,
        )
        first = service.refresh()
        symbol = first.items[0].symbol
        StrategyV2ShadowService(db).update_config(
            StrategyV2ShadowConfigUpdate(enabled=False),
            symbol=symbol,
        )

        second = service.refresh()
        config = (
            db.query(StrategyV2ShadowConfig)
            .filter(StrategyV2ShadowConfig.symbol == symbol)
            .one()
        )

        assert second.run.id == first.run.id
        assert config.enabled is False
        assert config.universe_managed is False
        assert symbol not in second.shadow_enabled_symbols
    finally:
        db.close()


def test_shadow_enable_failure_does_not_leave_orphaned_ownership(
    monkeypatch,
) -> None:
    from app.services.strategy_v2_shadow_service import (
        StrategyV2ShadowService,
    )

    db = _db()
    original_update = StrategyV2ShadowService.update_config

    def fail_enable(
        service,
        payload,
        *,
        symbol=None,
        preserve_universe_management=False,
    ):
        if payload.enabled:
            raise RuntimeError("injected enable failure")
        return original_update(
            service,
            payload,
            symbol=symbol,
            preserve_universe_management=preserve_universe_management,
        )

    monkeypatch.setattr(
        StrategyV2ShadowService,
        "update_config",
        fail_enable,
    )
    try:
        result = _service(
            db,
            _FakeBroker(),
            enable_shadow=True,
        ).refresh()

        configs = db.query(StrategyV2ShadowConfig).all()
        assert result.shadow_failed_symbols
        assert configs
        assert all(row.enabled is False for row in configs)
        assert all(row.universe_managed is False for row in configs)
    finally:
        db.close()
