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
from app.services import durable_job_lease_service
from app.services.watchlist_quant_v6_deadline import (
    QuantV6EvaluationCancelledError,
    QuantV6EvaluationDeadline,
    QuantV6EvaluationDeadlineExceededError,
)
from app.services.watchlist_quant_v6_historical_provider import (
    QuantV6HistoricalBarFetch,
)


@pytest.fixture(autouse=True)
def _outside_opening_research_quiet_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main_module,
        "_opening_research_quiet_window",
        lambda _now=None: False,
    )


@pytest.fixture(autouse=True)
def _successful_durable_job_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    state = SimpleNamespace(
        services=[],
        acquired_keys=[],
        keepalives=[],
        guards=[],
        fences=[],
        events=[],
    )
    handle = object()

    class _Guard:
        def __init__(self) -> None:
            self.on_lost: object | None = None

        def __enter__(self) -> _Guard:
            state.events.append("keepalive-enter")
            return self

        def __exit__(self, *_args: object) -> bool:
            state.events.append("keepalive-exit")
            return False

        def checkpoint(self) -> object:
            state.events.append("lease-checkpoint")
            return handle

        def fence_in_transaction(self, session: object) -> object:
            self.checkpoint()
            state.fences.append(session)
            return handle

    class _Service:
        def __init__(
            self,
            session_factory: object,
            *,
            default_ttl_seconds: object,
        ) -> None:
            state.services.append((session_factory, default_ttl_seconds))

        def try_acquire(self, lease_key: str) -> object:
            state.acquired_keys.append(lease_key)
            return handle

        def keepalive(
            self,
            acquired: object,
            *,
            interval_seconds: object,
            on_lost: object,
        ) -> _Guard:
            assert acquired is handle
            guard = _Guard()
            guard.on_lost = on_lost
            state.keepalives.append((interval_seconds, on_lost))
            state.guards.append(guard)
            return guard

        def fence_in_transaction(
            self,
            session: object,
            acquired: object,
        ) -> None:
            assert acquired is handle
            state.fences.append(session)

    monkeypatch.setattr(
        durable_job_lease_service,
        "DurableJobLeaseService",
        _Service,
    )
    return state


def test_quant_v6_cron_is_default_disabled_without_io(
    monkeypatch: pytest.MonkeyPatch,
    _successful_durable_job_lease: SimpleNamespace,
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
    assert _successful_durable_job_lease.services == []


def test_quant_v6_quiet_window_skips_before_plan_or_provider(
    monkeypatch: pytest.MonkeyPatch,
    _successful_durable_job_lease: SimpleNamespace,
) -> None:
    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_enabled",
        True,
    )
    monkeypatch.setattr(
        main_module,
        "_opening_research_quiet_window",
        lambda _now=None: True,
    )
    monkeypatch.setattr(
        watchlist_quant_v6_evaluation_service,
        "build_latest_quant_v6_registration_plan",
        lambda **_kwargs: pytest.fail("quiet tick built a plan"),
    )
    monkeypatch.setattr(
        watchlist_quant_v6_historical_provider,
        "QuantV6HistoricalBarProvider",
        lambda **_kwargs: pytest.fail("quiet tick created a provider"),
    )

    assert (
        main_module._watchlist_quant_v6_evaluation_tick_sync()
        is main_module._OPENING_RESEARCH_DEFERRED
    )
    assert _successful_durable_job_lease.services == []


def test_quant_v6_rechecks_quiet_window_after_sync_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions = iter((False, True))
    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_enabled",
        True,
    )
    monkeypatch.setattr(
        main_module,
        "_opening_research_quiet_window",
        lambda _now=None: next(decisions),
    )
    monkeypatch.setattr(
        watchlist_quant_v6_evaluation_service,
        "build_latest_quant_v6_registration_plan",
        lambda **_kwargs: pytest.fail("post-lock quiet tick built a plan"),
    )

    deadline = QuantV6EvaluationDeadline(30)
    assert (
        main_module._watchlist_quant_v6_evaluation_tick_sync(deadline)
        is main_module._OPENING_RESEARCH_DEFERRED
    )
    assert main_module._watchlist_quant_v6_evaluation_sync_lock.acquire(
        blocking=False
    )
    main_module._watchlist_quant_v6_evaluation_sync_lock.release()


def test_quant_v6_busy_lease_defers_before_plan_provider_or_business_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = pytest.fail
    acquired_keys: list[str] = []

    class _BusyLeaseService:
        def __init__(
            self,
            session_factory: object,
            *,
            default_ttl_seconds: object,
        ) -> None:
            assert session_factory is pytest.fail
            assert default_ttl_seconds == (
                main_module.settings.job_lease_ttl_seconds
            )

        def try_acquire(self, lease_key: str) -> None:
            acquired_keys.append(lease_key)
            return None

        def keepalive(self, *_args: object, **_kwargs: object) -> object:
            pytest.fail("busy lease started keepalive")

    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_enabled",
        True,
    )
    monkeypatch.setattr(main_module, "SessionLocal", session_factory)
    monkeypatch.setattr(
        durable_job_lease_service,
        "DurableJobLeaseService",
        _BusyLeaseService,
    )
    monkeypatch.setattr(
        watchlist_quant_v6_evaluation_service,
        "build_latest_quant_v6_registration_plan",
        lambda **_kwargs: pytest.fail("busy lease built a plan"),
    )
    monkeypatch.setattr(
        watchlist_quant_v6_historical_provider,
        "QuantV6HistoricalBarProvider",
        lambda **_kwargs: pytest.fail("busy lease created a provider"),
    )
    monkeypatch.setattr(
        watchlist_quant_v6_publication_service,
        "WatchlistQuantV6PublicationService",
        lambda *_args, **_kwargs: pytest.fail(
            "busy lease created a publication service"
        ),
    )

    outcome = main_module._watchlist_quant_v6_evaluation_tick_sync(
        QuantV6EvaluationDeadline(30)
    )

    assert outcome is main_module._JOB_LEASE_BUSY_DEFERRED
    assert acquired_keys == [
        main_module._WATCHLIST_QUANT_V6_EVALUATION_LEASE_KEY
    ]
    assert main_module._watchlist_quant_v6_evaluation_sync_lock.acquire(
        blocking=False
    )
    main_module._watchlist_quant_v6_evaluation_sync_lock.release()


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
    _successful_durable_job_lease: SimpleNamespace,
) -> None:
    plan = SimpleNamespace(members=(object(), object()))
    provider = SimpleNamespace(closed=False)
    timestamps: list[object] = []
    provider_deadlines: list[object] = []
    calls: list[tuple[object, object, object]] = []

    def close_provider() -> None:
        provider.closed = True

    provider.close = close_provider

    class FakePublicationService:
        def __init__(
            self,
            session_factory: object,
            *,
            transaction_fence: object,
        ) -> None:
            assert session_factory is main_module.SessionLocal
            self.transaction_fence = transaction_fence

        def register_provider_evaluate_publish(
            self,
            *,
            plan: object,
            provider: object,
            evaluation_deadline: object,
        ) -> object:
            assert callable(self.transaction_fence)
            self.transaction_fence(object())
            calls.append((plan, provider, evaluation_deadline))
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

    def provider_factory(*, evaluation_deadline: object) -> object:
        provider_deadlines.append(evaluation_deadline)
        return provider

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
        provider_factory,
    )
    monkeypatch.setattr(
        watchlist_quant_v6_publication_service,
        "WatchlistQuantV6PublicationService",
        FakePublicationService,
    )

    deadline = QuantV6EvaluationDeadline(30)
    main_module._watchlist_quant_v6_evaluation_tick_sync(deadline)

    assert len(timestamps) == 1
    timestamp = timestamps[0]
    assert getattr(timestamp, "tzinfo", None) is not None
    assert provider_deadlines == [deadline]
    assert calls == [(plan, provider, deadline)]
    assert provider.closed is True
    assert _successful_durable_job_lease.acquired_keys == [
        main_module._WATCHLIST_QUANT_V6_EVALUATION_LEASE_KEY
    ]
    assert len(_successful_durable_job_lease.fences) == 1
    assert _successful_durable_job_lease.events[-1] == "keepalive-exit"


def test_quant_v6_tick_closes_provider_when_publication_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = SimpleNamespace(closed=False)
    provider.close = lambda: setattr(provider, "closed", True)

    class FailingPublicationService:
        def __init__(
            self,
            _session_factory: object,
            *,
            transaction_fence: object,
        ) -> None:
            del transaction_fence
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


def test_quant_v6_heartbeat_loss_cancels_deadline_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    provider = SimpleNamespace(closed=False)
    provider.close = lambda: (
        events.append("provider-close"),
        setattr(provider, "closed", True),
    )
    handle = object()
    lost = durable_job_lease_service.LeaseLostError(
        "quant-v6 lease heartbeat was lost"
    )
    guard: object | None = None

    class _LostGuard:
        def __init__(self, on_lost: object) -> None:
            self.on_lost = on_lost
            self.failure: Exception | None = None

        def __enter__(self) -> _LostGuard:
            events.append("keepalive-enter")
            return self

        def __exit__(self, *_args: object) -> bool:
            events.append("keepalive-exit")
            return False

        def checkpoint(self) -> object:
            if self.failure is not None:
                raise self.failure
            return handle

        def fence_in_transaction(self, _session: object) -> object:
            return self.checkpoint()

        def lose(self) -> None:
            self.failure = lost
            assert callable(self.on_lost)
            self.on_lost(lost)

    class _LeaseService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def try_acquire(self, _lease_key: str) -> object:
            return handle

        def keepalive(
            self,
            _handle: object,
            *,
            interval_seconds: object,
            on_lost: object,
        ) -> _LostGuard:
            nonlocal guard
            assert interval_seconds == (
                main_module.settings.job_lease_heartbeat_seconds
            )
            guard = _LostGuard(on_lost)
            return guard

    class _PublicationService:
        def __init__(
            self,
            _session_factory: object,
            *,
            transaction_fence: object,
        ) -> None:
            del transaction_fence

        def register_provider_evaluate_publish(
            self,
            *,
            evaluation_deadline: QuantV6EvaluationDeadline,
            **_kwargs: object,
        ) -> object:
            events.append("heartbeat-loss")
            assert isinstance(guard, _LostGuard)
            guard.lose()
            assert evaluation_deadline.cancel_event.is_set()
            evaluation_deadline.checkpoint()
            pytest.fail("cancelled deadline continued publication")

    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_enabled",
        True,
    )
    monkeypatch.setattr(
        durable_job_lease_service,
        "DurableJobLeaseService",
        _LeaseService,
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
        _PublicationService,
    )

    with pytest.raises(
        durable_job_lease_service.LeaseLostError,
        match="heartbeat was lost",
    ):
        main_module._watchlist_quant_v6_evaluation_tick_sync(
            QuantV6EvaluationDeadline(30)
        )

    assert provider.closed is True
    assert events == [
        "keepalive-enter",
        "heartbeat-loss",
        "provider-close",
        "keepalive-exit",
    ]


def test_quant_v6_release_failure_wins_over_normal_return_after_provider_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    provider = SimpleNamespace(closed=False)

    def _close_provider() -> None:
        provider.closed = True
        events.append("provider-close")

    provider.close = _close_provider
    handle = object()

    class _ReleaseFailingGuard:
        def __enter__(self) -> _ReleaseFailingGuard:
            events.append("keepalive-enter")
            return self

        def __exit__(
            self,
            exc_type: object,
            _exc_value: object,
            _traceback: object,
        ) -> bool:
            assert exc_type is None
            assert provider.closed is True
            events.append("keepalive-exit")
            raise durable_job_lease_service.LeaseBackendError(
                "quant-v6 lease release failed"
            )

        def checkpoint(self) -> object:
            return handle

        def fence_in_transaction(self, _session: object) -> object:
            events.append("publication-fence")
            return handle

    class _LeaseService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def try_acquire(self, _lease_key: str) -> object:
            return handle

        def keepalive(self, *_args: object, **_kwargs: object) -> object:
            return _ReleaseFailingGuard()

    class _PublicationService:
        def __init__(
            self,
            _session_factory: object,
            *,
            transaction_fence: object,
        ) -> None:
            self.transaction_fence = transaction_fence

        def register_provider_evaluate_publish(
            self,
            **_kwargs: object,
        ) -> object:
            assert callable(self.transaction_fence)
            self.transaction_fence(object())
            return SimpleNamespace(
                publication_id=7,
                registration_id=3,
                binding_count=0,
                created=True,
                manifest_sha256="a" * 64,
            )

    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_enabled",
        True,
    )
    monkeypatch.setattr(
        durable_job_lease_service,
        "DurableJobLeaseService",
        _LeaseService,
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
        _PublicationService,
    )

    with pytest.raises(
        durable_job_lease_service.LeaseBackendError,
        match="release failed",
    ):
        main_module._watchlist_quant_v6_evaluation_tick_sync(
            QuantV6EvaluationDeadline(30)
        )

    assert events == [
        "keepalive-enter",
        "publication-fence",
        "provider-close",
        "keepalive-exit",
    ]


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
    deadlines: list[QuantV6EvaluationDeadline] = []

    def blocking_tick(
        evaluation_deadline: QuantV6EvaluationDeadline,
    ) -> None:
        deadlines.append(evaluation_deadline)
        started.set()
        assert release.wait(2)
        evaluation_deadline.checkpoint()

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
    assert await asyncio.to_thread(
        deadlines[0].cancel_event.wait,
        2,
    )
    assert task.done() is False
    with pytest.raises(QuantV6EvaluationCancelledError):
        deadlines[0].checkpoint()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_quant_v6_worker_join_resists_repeated_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    deadlines: list[QuantV6EvaluationDeadline] = []

    def blocking_tick(
        evaluation_deadline: QuantV6EvaluationDeadline,
    ) -> None:
        deadlines.append(evaluation_deadline)
        started.set()
        try:
            assert release.wait(2)
            evaluation_deadline.checkpoint()
        finally:
            finished.set()

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

    assert task.cancel() is True
    assert await asyncio.to_thread(
        deadlines[0].cancel_event.wait,
        2,
    )
    await asyncio.sleep(0)
    assert task.cancel() is True
    await asyncio.sleep(0)
    assert task.done() is False
    assert finished.is_set() is False

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set() is True


@pytest.mark.asyncio
async def test_quant_v6_outer_timeout_expires_and_joins_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    expired = threading.Event()
    release = threading.Event()
    observed_errors: list[Exception] = []

    def blocking_tick(
        evaluation_deadline: QuantV6EvaluationDeadline,
    ) -> None:
        started.set()
        assert evaluation_deadline.cancel_event.wait(2)
        try:
            evaluation_deadline.checkpoint()
        except Exception as exc:
            observed_errors.append(exc)
            expired.set()
        assert release.wait(2)
        raise observed_errors[0]

    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_enabled",
        True,
    )
    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_timeout_seconds",
        0.05,
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
    assert await asyncio.to_thread(expired.wait, 2)
    assert isinstance(
        observed_errors[0],
        QuantV6EvaluationDeadlineExceededError,
    )
    assert task.done() is False

    release.set()
    with pytest.raises(QuantV6EvaluationDeadlineExceededError):
        await task


@pytest.mark.asyncio
async def test_quant_v6_outer_timeout_accepts_started_atomic_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    atomic_completion_started = threading.Event()
    release = threading.Event()
    receipt = object()

    def atomic_tick(
        evaluation_deadline: QuantV6EvaluationDeadline,
    ) -> object:
        started.set()
        assert evaluation_deadline.cancel_event.wait(2)
        atomic_completion_started.set()
        assert release.wait(2)
        return receipt

    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_enabled",
        True,
    )
    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_timeout_seconds",
        0.05,
    )
    monkeypatch.setattr(
        main_module,
        "_watchlist_quant_v6_evaluation_tick_sync",
        atomic_tick,
    )

    task = asyncio.create_task(
        main_module._run_watchlist_quant_v6_evaluation_tick()
    )
    assert await asyncio.to_thread(started.wait, 2)
    assert await asyncio.to_thread(atomic_completion_started.wait, 2)
    assert task.done() is False

    release.set()
    assert await task is receipt


@pytest.mark.asyncio
async def test_quant_v6_cancel_after_timeout_still_joins_atomic_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    atomic_completion_started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def atomic_tick(
        evaluation_deadline: QuantV6EvaluationDeadline,
    ) -> object:
        started.set()
        assert evaluation_deadline.cancel_event.wait(2)
        atomic_completion_started.set()
        assert release.wait(2)
        finished.set()
        return object()

    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_enabled",
        True,
    )
    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_timeout_seconds",
        0.05,
    )
    monkeypatch.setattr(
        main_module,
        "_watchlist_quant_v6_evaluation_tick_sync",
        atomic_tick,
    )

    task = asyncio.create_task(
        main_module._run_watchlist_quant_v6_evaluation_tick()
    )
    assert await asyncio.to_thread(started.wait, 2)
    assert await asyncio.to_thread(atomic_completion_started.wait, 2)

    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False
    assert finished.is_set() is False

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set() is True


@pytest.mark.asyncio
async def test_quant_v6_repeated_cancel_after_timeout_joins_atomic_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    atomic_completion_started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def atomic_tick(
        evaluation_deadline: QuantV6EvaluationDeadline,
    ) -> object:
        started.set()
        assert evaluation_deadline.cancel_event.wait(2)
        atomic_completion_started.set()
        assert release.wait(2)
        finished.set()
        return object()

    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_enabled",
        True,
    )
    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_timeout_seconds",
        0.05,
    )
    monkeypatch.setattr(
        main_module,
        "_watchlist_quant_v6_evaluation_tick_sync",
        atomic_tick,
    )

    task = asyncio.create_task(
        main_module._run_watchlist_quant_v6_evaluation_tick()
    )
    assert await asyncio.to_thread(started.wait, 2)
    assert await asyncio.to_thread(atomic_completion_started.wait, 2)

    assert task.cancel() is True
    await asyncio.sleep(0)
    assert task.cancel() is True
    await asyncio.sleep(0)
    assert task.done() is False
    assert finished.is_set() is False

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set() is True


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


@pytest.mark.asyncio
async def test_quant_v6_cron_rechecks_after_quiet_window_defer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    outcomes = iter((main_module._OPENING_RESEARCH_DEFERRED, object()))

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    async def next_outcome() -> object:
        return next(outcomes)

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
    monkeypatch.setattr(main_module.asyncio, "sleep", record_sleep)
    monkeypatch.setattr(
        main_module,
        "_run_watchlist_quant_v6_evaluation_tick",
        next_outcome,
    )

    with pytest.raises(asyncio.CancelledError):
        await main_module._watchlist_quant_v6_evaluation_cron()

    assert sleeps == [
        main_module._WATCHLIST_QUANT_V6_INITIAL_DELAY_SECONDS,
        main_module._OPENING_RESEARCH_DEFER_RETRY_SECONDS,
        1_440 * 60,
    ]


@pytest.mark.asyncio
async def test_quant_v6_cron_rechecks_busy_lease_after_sixty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    outcomes = iter((main_module._JOB_LEASE_BUSY_DEFERRED, object()))

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    async def next_outcome() -> object:
        return next(outcomes)

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
    monkeypatch.setattr(main_module.asyncio, "sleep", record_sleep)
    monkeypatch.setattr(
        main_module,
        "_run_watchlist_quant_v6_evaluation_tick",
        next_outcome,
    )

    with pytest.raises(asyncio.CancelledError):
        await main_module._watchlist_quant_v6_evaluation_cron()

    assert sleeps == [
        main_module._WATCHLIST_QUANT_V6_INITIAL_DELAY_SECONDS,
        main_module._JOB_LEASE_RETRY_SECONDS,
        1_440 * 60,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lease_error",
    [
        durable_job_lease_service.LeaseBackendError("lease database failed"),
        durable_job_lease_service.LeaseLostError("lease ownership was lost"),
    ],
)
async def test_quant_v6_cron_retries_lease_failures_after_sixty_seconds(
    monkeypatch: pytest.MonkeyPatch,
    lease_error: Exception,
) -> None:
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    async def fail_lease() -> None:
        raise lease_error

    monkeypatch.setattr(
        main_module.settings,
        "watchlist_quant_v6_evaluation_enabled",
        True,
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
        fail_lease,
    )

    with pytest.raises(asyncio.CancelledError):
        await main_module._watchlist_quant_v6_evaluation_cron()

    assert sleeps == [
        main_module._WATCHLIST_QUANT_V6_INITIAL_DELAY_SECONDS,
        main_module._JOB_LEASE_RETRY_SECONDS,
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

        # Lease contention is a legitimate no-work result, not a job failure.
        async def noop_tick() -> object:
            return main_module._JOB_LEASE_BUSY_DEFERRED

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
