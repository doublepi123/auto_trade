from __future__ import annotations

import asyncio
import ast
import inspect
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app import database
from app import main as main_module
from app.models import Base
from app.services import watchlist_quant_v6_evaluation_service
from app.services import watchlist_quant_v6_historical_provider
from app.services import watchlist_quant_v6_publication_service
from app.services.watchlist_quant_v6_historical_provider import (
    QuantV6HistoricalBarFetch,
)


def test_quant_v6_cron_is_default_disabled_without_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = pytest.fail
    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_enabled",
        False,
    )
    monkeypatch.setattr(main_module, "SessionLocal", session_factory)
    monkeypatch.setattr(
        watchlist_quant_v6_evaluation_service,
        "build_latest_quant_v6_registration_plan",
        lambda **_kwargs: pytest.fail("disabled tick built a plan"),
    )
    monkeypatch.setattr(
        watchlist_quant_v6_historical_provider,
        "QuantV6HistoricalBarProvider",
        lambda: pytest.fail("disabled tick created a quote provider"),
    )

    main_module._watchlist_quant_v6_evaluation_tick_sync()


@pytest.mark.asyncio
async def test_quant_v6_disabled_cron_does_not_sleep_or_start_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_enabled",
        False,
    )

    async def unexpected_sleep(_delay: float) -> None:
        pytest.fail("disabled cron slept")

    async def unexpected_worker() -> None:
        pytest.fail("disabled cron started a worker")

    monkeypatch.setattr(main_module.asyncio, "sleep", unexpected_sleep)
    monkeypatch.setattr(
        main_module,
        "_run_watchlist_quant_v6_evaluation_tick",
        unexpected_worker,
    )

    await main_module._watchlist_quant_v6_evaluation_cron()


def test_quant_v6_tick_orchestrates_once_and_closes_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = SimpleNamespace(members=(object(), object()))
    provider = SimpleNamespace(closed=False)
    timestamps: list[object] = []
    calls: list[tuple[object, object]] = []

    def close_provider() -> None:
        provider.closed = True

    provider.close = close_provider

    class FakePublicationService:
        def __init__(self, session_factory: object) -> None:
            assert session_factory is main_module.SessionLocal

        def register_provider_evaluate_publish(
            self,
            *,
            plan: object,
            provider: object,
        ) -> object:
            calls.append((plan, provider))
            return SimpleNamespace(
                publication_id=7,
                registration_id=3,
                binding_count=62,
                created=True,
                manifest_sha256="a" * 64,
            )

    def frozen_plan_builder(*, observed_at: object) -> object:
        # Retain the exact clock value to prove the cron freezes one plan.
        timestamps.append(observed_at)
        return plan

    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_enabled",
        True,
    )
    monkeypatch.setattr(
        watchlist_quant_v6_evaluation_service,
        "build_latest_quant_v6_registration_plan",
        frozen_plan_builder,
    )
    monkeypatch.setattr(
        watchlist_quant_v6_historical_provider,
        "QuantV6HistoricalBarProvider",
        lambda **_kwargs: provider,
    )
    monkeypatch.setattr(
        watchlist_quant_v6_publication_service,
        "WatchlistQuantV6PublicationService",
        FakePublicationService,
    )

    main_module._watchlist_quant_v6_evaluation_tick_sync()

    assert len(timestamps) == 1
    timestamp = timestamps[0]
    assert getattr(timestamp, "tzinfo", None) is not None
    assert calls == [(plan, provider)]
    assert provider.closed is True


def test_quant_v6_tick_closes_provider_when_publication_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = SimpleNamespace(closed=False)
    provider.close = lambda: setattr(provider, "closed", True)

    class FailingPublicationService:
        def __init__(self, _session_factory: object) -> None:
            pass

        def register_provider_evaluate_publish(self, **_kwargs: object) -> object:
            raise RuntimeError("publication failed")

    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_enabled",
        True,
    )
    monkeypatch.setattr(
        watchlist_quant_v6_evaluation_service,
        "build_latest_quant_v6_registration_plan",
        lambda **_kwargs: SimpleNamespace(members=()),
    )
    monkeypatch.setattr(
        watchlist_quant_v6_historical_provider,
        "QuantV6HistoricalBarProvider",
        lambda **_kwargs: provider,
    )
    monkeypatch.setattr(
        watchlist_quant_v6_publication_service,
        "WatchlistQuantV6PublicationService",
        FailingPublicationService,
    )

    with pytest.raises(RuntimeError, match="publication failed"):
        main_module._watchlist_quant_v6_evaluation_tick_sync()

    assert provider.closed is True


def test_quant_v6_tick_only_mutates_immutable_evidence_tables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'quant-v6-cron-isolation.db'}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def enable_sqlite_contract(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA recursive_triggers=ON")
        finally:
            cursor.close()

    Base.metadata.create_all(engine)
    database._ensure_watchlist_quant_v6_tables(engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    plan = (
        watchlist_quant_v6_evaluation_service
        .build_latest_quant_v6_registration_plan(
            observed_at=datetime(2026, 7, 31, 23, tzinfo=timezone.utc),
        )
    )

    class EmptyHistoricalProvider:
        def __init__(self) -> None:
            self.closed = False

        def fetch_five_minute_no_adjust(
            self,
            symbol: str,
            *,
            start_at: datetime,
            end_at: datetime,
        ) -> QuantV6HistoricalBarFetch:
            del symbol, start_at, end_at
            return QuantV6HistoricalBarFetch(
                bars=(),
                pages=1,
                raw_rows=0,
                rejected_rows=0,
            )

        def close(self) -> None:
            self.closed = True

    provider = EmptyHistoricalProvider()
    protected_tables = (
        "orders",
        "paper_orders",
        "tracked_entries",
        "runtime_state",
        "runtime_state_snapshots",
        "watchlist_items",
        "watchlist_scores",
        "universe_selection_runs",
        "universe_selection_candidates",
        "portfolio_config",
        "strategy_v2_portfolio_registrations",
        "strategy_v2_portfolio_observations",
    )

    def protected_counts() -> dict[str, int]:
        with engine.connect() as connection:
            return {
                table_name: int(connection.scalar(text(
                    f"SELECT count(*) FROM {table_name}"
                )) or 0)
                for table_name in protected_tables
            }

    before = protected_counts()
    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_enabled",
        True,
    )
    monkeypatch.setattr(main_module, "SessionLocal", session_factory)
    monkeypatch.setattr(
        watchlist_quant_v6_evaluation_service,
        "build_latest_quant_v6_registration_plan",
        lambda **_kwargs: plan,
    )
    monkeypatch.setattr(
        watchlist_quant_v6_historical_provider,
        "QuantV6HistoricalBarProvider",
        lambda **_kwargs: provider,
    )

    main_module._watchlist_quant_v6_evaluation_tick_sync()

    assert protected_counts() == before
    with engine.connect() as connection:
        assert connection.scalar(text(
            "SELECT count(*) FROM watchlist_quant_v6_publications"
        )) == 1
    assert provider.closed is True


@pytest.mark.asyncio
async def test_quant_v6_worker_is_joined_during_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_tick() -> None:
        started.set()
        assert release.wait(2)

    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_enabled",
        True,
    )
    monkeypatch.setattr(
        main_module,
        "_watchlist_quant_v6_evaluation_tick_sync",
        blocking_tick,
    )
    task = asyncio.create_task(
        main_module._run_watchlist_quant_v6_evaluation_tick()
    )
    assert await asyncio.to_thread(started.wait, 2)

    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False
    assert main_module._watchlist_quant_v6_evaluation_stop_event.is_set()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_quant_v6_cron_uses_its_bounded_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    ticks = 0

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    async def record_tick() -> None:
        nonlocal ticks
        ticks += 1

    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_enabled",
        True,
    )
    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_interval_minutes",
        90,
    )
    monkeypatch.setattr(main_module.asyncio, "sleep", record_sleep)
    monkeypatch.setattr(
        main_module,
        "_run_watchlist_quant_v6_evaluation_tick",
        record_tick,
    )

    with pytest.raises(asyncio.CancelledError):
        await main_module._watchlist_quant_v6_evaluation_cron()

    assert ticks == 1
    assert sleeps == [
        main_module._WATCHLIST_QUANT_V6_INITIAL_DELAY_SECONDS,
        90 * 60,
    ]


@pytest.mark.asyncio
async def test_quant_v6_cron_retries_failed_window_then_resumes_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    attempts = 0

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    async def fail_once_then_succeed() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("transient quote failure")

    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_enabled",
        True,
    )
    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_interval_minutes",
        1_440,
    )
    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_retry_interval_minutes",
        30,
    )
    monkeypatch.setattr(main_module.asyncio, "sleep", record_sleep)
    monkeypatch.setattr(
        main_module,
        "_run_watchlist_quant_v6_evaluation_tick",
        fail_once_then_succeed,
    )

    with pytest.raises(asyncio.CancelledError):
        await main_module._watchlist_quant_v6_evaluation_cron()

    # The second attempt happens after the short retry delay, before the
    # regular daily window advances. A successful retry restores daily cadence.
    assert attempts == 2
    assert sleeps == [
        main_module._WATCHLIST_QUANT_V6_INITIAL_DELAY_SECONDS,
        30 * 60,
        1_440 * 60,
    ]


def test_quant_v6_tick_is_statically_isolated_from_live_paths() -> None:
    source = inspect.getsource(
        main_module._watchlist_quant_v6_evaluation_tick_sync
    )
    tree = ast.parse(source)
    referenced = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    forbidden_names = {
        "BrokerGateway",
        "OrderRecord",
        "PaperOrder",
        "PortfolioService",
        "TradeContext",
        "TrackedEntry",
        "UniverseSelectionCandidate",
        "UniverseSelectionRun",
        "WatchlistItem",
        "WatchlistQuantService",
        "WatchlistScore",
        "UniverseSelectionService",
        "get_runner",
        "list_orders",
        "list_positions",
        "submit_order",
    }

    assert referenced.isdisjoint(forbidden_names)
    assert "portfolio" not in source.lower()
    assert "app.services.trade" not in source
    assert main_module._watchlist_quant_v6_evaluation_lock is not (
        main_module._watchlist_quant_lock
    )
    assert main_module._watchlist_quant_v6_evaluation_lock is not (
        main_module._universe_selection_lock
    )
    assert main_module._watchlist_quant_v6_evaluation_sync_lock is not (
        main_module._watchlist_quant_sync_lock
    )


@pytest.mark.asyncio
async def test_quant_v6_cron_records_success_when_tick_returns_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Success observation: a normal tick return (including a legitimate
    no-due-work no-op) records success via the CronHealthService."""
    from app.services.cron_health_service import (
        CronHealthService,
        set_cron_health_service,
    )

    isolated = CronHealthService()
    set_cron_health_service(isolated)
    try:
        monkeypatch.setattr(
            main_module.settings,
            "watchlist_quant_v6_evaluation_enabled",
            True,
        )
        monkeypatch.setattr(
            main_module.settings,
            "watchlist_quant_v6_evaluation_interval_minutes",
            1_440,
        )

        # Register jobs into the isolated service so observation is recorded.
        main_module._register_cron_health_jobs()
        main_module._activate_cron_health_jobs()

        # The tick returns normally (legitimate no-due-work no-op counts).
        async def noop_tick() -> None:
            return

        monkeypatch.setattr(
            main_module,
            "_run_watchlist_quant_v6_evaluation_tick",
            noop_tick,
        )

        # Allow the initial delay sleep, then cancel on the post-tick sleep.
        sleep_count = 0

        async def sleep_then_cancel(delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count == 2:
                raise asyncio.CancelledError

        monkeypatch.setattr(main_module.asyncio, "sleep", sleep_then_cancel)

        with pytest.raises(asyncio.CancelledError):
            await main_module._watchlist_quant_v6_evaluation_cron()

        rows = {row.name: row for row in isolated.snapshot()}
        v6 = rows[main_module._CRON_WATCHLIST_QUANT_V6_EVALUATION]
        assert v6.tick_count == 1
        assert v6.failure_count == 0
        assert v6.last_outcome == "success"
        assert v6.status == "healthy"
    finally:
        set_cron_health_service(None)


@pytest.mark.asyncio
async def test_quant_v6_cron_records_sanitized_failure_when_tick_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure observation: a raised tick records a sanitized failure code via
    the CronHealthService without altering the retry cadence."""
    from app.services.cron_health_service import (
        CronHealthService,
        set_cron_health_service,
    )

    isolated = CronHealthService()
    set_cron_health_service(isolated)
    try:
        monkeypatch.setattr(
            main_module.settings,
            "watchlist_quant_v6_evaluation_enabled",
            True,
        )
        monkeypatch.setattr(
            main_module.settings,
            "watchlist_quant_v6_evaluation_interval_minutes",
            1_440,
        )
        monkeypatch.setattr(
            main_module.settings,
            "watchlist_quant_v6_evaluation_retry_interval_minutes",
            30,
        )

        # Register jobs into the isolated service so observation is recorded.
        main_module._register_cron_health_jobs()
        main_module._activate_cron_health_jobs()

        async def failing_tick() -> None:
            raise ConnectionError("transient quote failure with secret=sk-xxx")

        monkeypatch.setattr(
            main_module,
            "_run_watchlist_quant_v6_evaluation_tick",
            failing_tick,
        )

        sleeps: list[float] = []
        sleep_count = 0

        async def record_sleep(delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            sleeps.append(delay)
            if sleep_count == 2:
                raise asyncio.CancelledError

        monkeypatch.setattr(main_module.asyncio, "sleep", record_sleep)

        with pytest.raises(asyncio.CancelledError):
            await main_module._watchlist_quant_v6_evaluation_cron()

        # Retry cadence unchanged: initial delay then retry interval.
        assert sleeps == [
            main_module._WATCHLIST_QUANT_V6_INITIAL_DELAY_SECONDS,
            30 * 60,
        ]
        rows = {row.name: row for row in isolated.snapshot()}
        v6 = rows[main_module._CRON_WATCHLIST_QUANT_V6_EVALUATION]
        assert v6.tick_count == 1
        assert v6.failure_count == 1
        assert v6.last_outcome == "failure"
        assert v6.last_failure_code == "ConnectionError"
        # Sanitized: no raw message leaked.
        assert "secret" not in (v6.last_failure_code or "")
        assert "sk-xxx" not in (v6.last_failure_code or "")
    finally:
        set_cron_health_service(None)


@pytest.mark.asyncio
async def test_quant_v6_cron_observation_does_not_alter_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation semantics are preserved: CancelledError propagates and is
    not swallowed by the observer."""
    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_enabled",
        True,
    )

    async def cancel_tick() -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(
        main_module,
        "_run_watchlist_quant_v6_evaluation_tick",
        cancel_tick,
    )

    # Allow the initial delay, then the tick raises CancelledError.
    async def allow_initial(_delay: float) -> None:
        return

    monkeypatch.setattr(main_module.asyncio, "sleep", allow_initial)

    with pytest.raises(asyncio.CancelledError):
        await main_module._watchlist_quant_v6_evaluation_cron()
