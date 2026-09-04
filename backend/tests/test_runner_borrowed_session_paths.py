"""Paths that already hold a session must not open a second one.

Continuation of ``fa983919`` / ``test_runner_session_reentrancy.py``. That
commit fixed the two helpers on the durable-fill latch path; the runtime
detector shipped in ``e117104b`` then ran the whole suite as an audit and
named the sites it could not fix in the same change.

This module pins the remaining GENUINE ones -- paths where the caller holds
an open ``Session`` and the callee opens its own, the exact shape that checked
out all 15 pooled connections and deadlocked the process for ~65 minutes on
2026-09-03:

* ``_latch_live_order_reconciliation``: reached from
  ``_initialize_runner`` -> ``_pause_if_unresolved_live_order_exists``, which
  holds a session, and it called ``_persist_risk_pause_best_effort`` and
  ``_record_risk_event`` without passing it -- both are borrow-capable and
  the call sites simply omitted ``db=``.
* ``_sync_symbol_runtimes`` -> ``OpeningMomentumExecutionService.active_policies``
  (a pure read) is already given the caller's ``db``; the runtime is pinned so
  a future refactor cannot reintroduce an owned session there.
* ``_load_tracked_entries`` from the today-order sync, called one line after
  ``db.commit()`` -- the commit ends the transaction, so nothing uncommitted
  crosses that read.
* ``_load_credentials`` from ``_initialize_runner``, which holds a session
  across the whole block.
* ``reload_strategy`` from ``PUT /api/strategy``, whose request session has
  already committed its save.
* ``OpeningMomentumExecutionService.tick`` from the opening-momentum cron:
  the cron holds its ``SessionLocal`` across the tick, and the tick's
  ``_refresh_runner_registry`` reached
  ``AppRunner.refresh_opening_execution_registry``, which opened its own
  session for ``_sync_symbol_runtimes`` / ``_load_opening_execution_registry``
  -- the 2026-09-04 live violation. The service now lends its session and
  the runner borrows it, and ``load_symbol_runtime`` is transaction-neutral
  so the borrow cannot finalize the cron's work.

Each is asserted by counting the sessions the runner opens for itself while a
caller's session is held. The caller's own session comes straight from
``database.SessionLocal`` and is deliberately not counted: the point is reuse,
not a smaller number.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timezone
from typing import Any, cast

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from app import database
from app import runner as runner_module
from app.config import settings
from app.core.engine import StrategyParams
from app.core.notifiers.multi_channel import MultiChannelNotifier
from app.runner import AppRunner

database.init_db()


SYMBOL = "BORROWSESSION.US"


class _RecordingNotifier:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def notify_order(self, *_args: object) -> bool:
        return True

    def notify_risk_event(self, event_type: str, reason: str) -> bool:
        self.events.append((event_type, reason))
        return True


class _SessionCounter:
    """Count every session the runner opens for itself.

    Patches the ``SessionLocal`` name each module resolves at call time, so a
    helper that opens its own session is counted regardless of which module it
    lives in.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.opened = 0
        real_factory = runner_module.SessionLocal

        def counting_factory(*args: object, **kwargs: object) -> Session:
            self.opened += 1
            return real_factory(*args, **kwargs)

        monkeypatch.setattr(runner_module, "SessionLocal", counting_factory)


def _clean_rows() -> None:
    from app.models import (
        OpeningMomentumExecution,
        OrderRecord,
        RiskEvent,
        RuntimeState,
        RuntimeStateSnapshot,
        TrackedEntry,
        TradeEvent,
        WatchlistItem,
    )

    with database.SessionLocal() as db:
        db.query(TradeEvent).filter(
            TradeEvent.message.like("%BORROWSESSION%")
        ).delete(synchronize_session=False)
        db.query(RiskEvent).filter(
            RiskEvent.reason.like("%BORROWSESSION%")
        ).delete(synchronize_session=False)
        db.query(OrderRecord).filter(
            OrderRecord.symbol.like("BORROWSESSION%")
        ).delete(synchronize_session=False)
        db.query(TrackedEntry).filter(
            TrackedEntry.symbol.like("BORROWSESSION%")
        ).delete(synchronize_session=False)
        db.query(RuntimeStateSnapshot).filter(
            RuntimeStateSnapshot.symbol.like("BORROWSESSION%")
        ).delete(synchronize_session=False)
        db.query(RuntimeState).filter(
            RuntimeState.symbol.like("BORROWSESSION%")
        ).delete(synchronize_session=False)
        db.query(OpeningMomentumExecution).filter(
            OpeningMomentumExecution.symbol.like("BORROWSESSION%")
        ).delete(synchronize_session=False)
        db.query(WatchlistItem).filter(
            WatchlistItem.symbol.like("BORROWSESSION%")
        ).delete(synchronize_session=False)
        db.commit()


@pytest.fixture(autouse=True)
def _isolate_rows():
    _clean_rows()
    yield
    _clean_rows()


def _runner(notifier: _RecordingNotifier | None = None) -> AppRunner:
    runner = AppRunner()
    runner.engine.params = StrategyParams(
        symbol=SYMBOL,
        market="US",
        buy_low=100.0,
        sell_high=110.0,
    )
    runner.engine.last_price = 105.0
    runner.notifier = cast(
        MultiChannelNotifier, notifier if notifier is not None else _RecordingNotifier()
    )
    return runner


def _checked_out_connections() -> int:
    """Connections the process engine currently has checked out.

    ``checkedout`` is defined on ``QueuePool``, not on the ``Pool`` base that
    ``Engine.pool`` is typed as, so it is narrowed rather than suppressed --
    an in-memory SQLite run is served by ``SingletonThreadPool``, which does
    not implement it at all.
    """
    pool = database.engine.pool
    if not isinstance(pool, QueuePool):
        return 0
    return pool.checkedout()


class _CheckoutDepthTracker:
    """Measure how many pooled connections are held AT ONCE.

    Installed alongside the guard on the process engine. The guard is
    registered first, so when it raises inside a checkout (the RED path under
    ``env == "test"``) this tracker never sees that checkout -- which is fine:
    the tracker exists to prove the GREEN path never exceeds depth 1, while
    the guard's own counter proves no violation was counted.
    """

    def __init__(self) -> None:
        self.depth = 0
        self.max_depth = 0
        event.listen(database.engine, "checkout", self._on_checkout)
        event.listen(database.engine, "checkin", self._on_checkin)

    def _on_checkout(self, *_args: object) -> None:
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)

    def _on_checkin(self, *_args: object) -> None:
        self.depth -= 1

    def remove(self) -> None:
        event.remove(database.engine, "checkout", self._on_checkout)
        event.remove(database.engine, "checkin", self._on_checkin)


def _drop_guard_depth_for(thread_id: int) -> None:
    """Test hygiene for the RED path only.

    When the strict guard raises inside a checkout, the connection is never
    handed out, so no ``checkin`` fires and the guard's held-depth entry for
    that scope leaks. The cron threads these tests run have no request scope,
    so their identity is the ``("thread", <id>)`` fallback; thread ids are
    reused, and a phantom depth inherited by a later test would raise on that
    test's first session. The entry belongs to a thread this test itself ran
    and is removed nowhere else.
    """
    guard = database.session_reentrancy_guard
    with guard._lock:
        guard._held.pop(("thread", thread_id), None)


def _runner_on_persisted_symbol() -> AppRunner:
    """A runner already pointed at the persisted config's symbol.

    ``reload_strategy`` treats a differing symbol as a primary switch and
    refuses it unless the runner is running and flat. These tests are about
    which session the reload uses, so they start from the no-switch case.
    """
    from app.models import StrategyConfig

    runner = AppRunner()
    with database.SessionLocal() as db:
        config = (
            db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
        )
        symbol = str(getattr(config, "symbol", "") or "")
        market = str(getattr(config, "market", "US") or "US")
    runner.engine.params = StrategyParams(
        symbol=symbol,
        market=market,
        buy_low=100.0,
        sell_high=110.0,
    )
    return runner


def _runtime_state_model() -> Any:
    from app.models import RuntimeState

    return RuntimeState


def _runtime_state_paused() -> bool:
    from app.models import RuntimeState

    with database.SessionLocal() as db:
        row = (
            db.query(RuntimeState).filter(RuntimeState.symbol == SYMBOL).first()
        )
        return bool(row is not None and row.paused)


def _risk_event_reasons() -> list[str]:
    from app.models import RiskEvent

    with database.SessionLocal() as db:
        return [
            str(row.reason)
            for row in db.query(RiskEvent)
            .filter(RiskEvent.reason.like("%BORROWSESSION%"))
            .order_by(RiskEvent.id)
            .all()
        ]


def _insert_live_order(broker_order_id: str, symbol: str = SYMBOL) -> None:
    from app.models import OrderRecord

    with database.SessionLocal() as db:
        db.add(
            OrderRecord(
                broker_order_id=broker_order_id,
                symbol=symbol,
                side="BUY",
                quantity=1.0,
                price=100.0,
                status="SUBMITTED",
            )
        )
        db.commit()


def test_live_order_latch_reuses_the_callers_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#6/#7/#8: the startup latch must borrow, not open two more sessions.

    ``_initialize_runner`` holds a session across
    ``_pause_if_unresolved_live_order_exists``. Reached from there,
    ``_latch_live_order_reconciliation`` called ``_persist_risk_pause_best_effort``
    and ``_record_risk_event`` with no ``db=``, so each opened its own -- two
    extra pooled connections on the thread that is already holding one, during
    startup reconciliation.

    Both helpers have been borrow-capable since ``fa983919``; only the call
    sites omitted the argument. Exercises the REAL helpers so the operator
    surfaces are proved to still fire.
    """
    notifier = _RecordingNotifier()
    runner = _runner(notifier)
    broadcasts: list[bool] = []
    monkeypatch.setattr(runner, "_broadcast_status", lambda: broadcasts.append(True))
    _insert_live_order("BORROWSESSION-order-1")
    counter = _SessionCounter(monkeypatch)

    with database.SessionLocal() as db:
        latched = runner._pause_if_unresolved_live_order_exists(db)

    assert latched is True
    assert counter.opened == 0, (
        f"the live-order latch opened {counter.opened} nested session(s) while "
        "the caller already held one"
    )
    reason = runner.risk.pause_reason
    assert reason.startswith("ORDER_RECONCILIATION_UNCERTAIN:")
    # Every operator surface must still fire on a borrowed session.
    assert _runtime_state_paused() is True
    assert [
        recorded
        for recorded in _risk_event_reasons()
        if recorded.startswith("ORDER_RECONCILIATION_UNCERTAIN:")
    ] == [reason]
    assert broadcasts == [True]


def test_live_order_latch_records_its_risk_event_on_the_borrowed_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The halt evidence must survive the borrow, not be traded away for it.

    Borrowing is only correct if the row still lands. ``_record_risk_event``
    commits even on a borrowed session by design (durable proof of a halt must
    not wait on the caller's later success), so the risk event is queryable as
    soon as the latch returns.
    """
    runner = _runner()
    monkeypatch.setattr(runner, "_broadcast_status", lambda: None)
    _insert_live_order("BORROWSESSION-order-evidence")
    counter = _SessionCounter(monkeypatch)

    with database.SessionLocal() as db:
        runner._pause_if_unresolved_live_order_exists(db)

    assert counter.opened == 0
    reasons = [
        reason
        for reason in _risk_event_reasons()
        if reason.startswith("ORDER_RECONCILIATION_UNCERTAIN:")
    ]
    assert len(reasons) == 1
    assert "BORROWSESSION-order-evidence" in reasons[0]


def test_live_order_latch_still_owns_sessions_when_no_db_is_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers that hold no session must keep working exactly as before.

    ``_sync_today_orders_from_broker_serialized`` latches from OUTSIDE its
    ``with self._db_session()`` block in two of three places, so the optional
    ``db`` must stay optional -- forcing it would break the very path this
    change is trying not to disturb.
    """
    runner = _runner()
    monkeypatch.setattr(runner, "_broadcast_status", lambda: None)
    counter = _SessionCounter(monkeypatch)

    latched = runner._latch_live_order_reconciliation(
        {SYMBOL: ["BORROWSESSION-owned"]},
        ["BORROWSESSION probe issue"],
    )

    assert latched is True
    assert counter.opened == 2, (
        "without a caller's session the latch must still own the two it needs "
        f"(persist + risk event); it opened {counter.opened}"
    )
    assert _runtime_state_paused() is True


def test_startup_credential_load_checks_out_no_second_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2/#3, asserted at the pool rather than at the signature.

    ``_initialize_runner`` runs its whole first block under one
    ``with self._db_session() as db`` and ends it by loading credentials. This
    reproduces exactly that nesting and asks the guard -- the same detector
    that watches production -- whether a second connection was checked out.
    """
    runner = _runner()
    guard = database.session_reentrancy_guard
    before = guard.violation_count

    with database.SessionLocal() as db:
        db.query(_runtime_state_model()).all()
        runner._apply_credentials(
            runner._load_credentials(db=db),
            resubscribe=False,
        )

    assert guard.violation_count == before, (
        "loading credentials inside _initialize_runner's session checked out a "
        "second pooled connection: the 2026-09-03 deadlock shape"
    )


def test_load_credentials_reuses_the_callers_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2/#3: ``_initialize_runner`` holds a session across ``_load_credentials``.

    The whole first block of ``_initialize_runner`` runs under one
    ``with self._db_session() as db``, and its last statement reached
    ``_load_credentials``, which opened its own. That is a second pooled
    connection on the startup thread every time the runner starts, and it is
    the shape ``PUT /api/credentials`` -> restart amplifies.

    Read-only for the caller's transaction: ``get_plain_credentials`` reads a
    row and decrypts in memory. The one write it can make -- the idempotent
    legacy-ciphertext migration in ``_encrypt_plaintext_fields`` -- commits
    itself and runs at most once per process, so borrowing cannot strand it.
    """
    runner = _runner()
    counter = _SessionCounter(monkeypatch)

    with database.SessionLocal() as db:
        credentials = runner._load_credentials(db=db)

    assert counter.opened == 0, (
        f"_load_credentials opened {counter.opened} nested session(s) while "
        "_initialize_runner already held one"
    )
    assert credentials is not None


def test_load_credentials_still_owns_a_session_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``reload_credentials`` holds nothing and must keep working unchanged."""
    runner = _runner()
    counter = _SessionCounter(monkeypatch)

    credentials = runner._load_credentials()

    assert counter.opened == 1
    assert credentials is not None


def test_initialize_runner_startup_block_opens_no_nested_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the real call site, not just the helper's new parameter.

    A signature that accepts ``db`` proves nothing if ``_initialize_runner``
    still calls it without one. This asserts the composed behaviour of the
    block that actually held the session during the incident.
    """
    runner = _runner()
    applied: list[object] = []
    monkeypatch.setattr(
        runner,
        "_apply_credentials",
        lambda credentials, **_kwargs: applied.append(credentials),
    )
    counter = _SessionCounter(monkeypatch)

    with database.SessionLocal() as db:
        runner._apply_credentials(
            runner._load_credentials(db=db),
            resubscribe=False,
        )

    assert counter.opened == 0
    assert len(applied) == 1


def test_reload_strategy_reuses_the_callers_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#4/#9: ``PUT /api/strategy`` holds the request session across the reload.

    ``update_strategy_with_runtime_reload`` commits the save and then calls
    ``AppRunner.reload_strategy``, which opened its own ``SessionLocal``. The
    request session is still checked out for the rest of the handler
    (``record_version``, the audit record), so that is two connections per
    strategy save -- and the reload reads the row the caller just committed,
    so there is nothing the borrow can miss.

    Two of the sweep's sites live on this one call: ``StrategyService.get_config``
    and, when the primary symbol changes, ``load_symbol_runtime``.
    """
    runner = _runner_on_persisted_symbol()
    counter = _SessionCounter(monkeypatch)

    with database.SessionLocal() as db:
        runner.reload_strategy(db=db)

    assert counter.opened == 0, (
        f"reload_strategy opened {counter.opened} nested session(s) while the "
        "request handler still held its own"
    )


def test_reload_strategy_still_owns_a_session_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cron and auto-switch callers hold nothing and must be unchanged."""
    runner = _runner_on_persisted_symbol()
    counter = _SessionCounter(monkeypatch)

    runner.reload_strategy()

    assert counter.opened == 1


def test_reload_strategy_does_not_close_a_borrowed_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A borrowed session belongs to its owner and must outlive the borrow.

    ``put_strategy`` keeps using the request session after the reload returns
    -- ``StrategyVersionService.record_version`` writes through it. If the
    reload closed it, the version snapshot would fail on a save that had
    already succeeded.
    """
    runner = _runner_on_persisted_symbol()

    with database.SessionLocal() as db:
        runner.reload_strategy(db=db)
        rows = db.query(_runtime_state_model()).all()

    assert isinstance(rows, list)


def test_put_strategy_reload_borrows_the_request_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the API call site, not just the runner's new parameter.

    The save must be committed before the session is lent out: the reload
    reads ``strategy_config`` back and a borrow that carried an uncommitted
    write would let the runner act on a row that could still be rolled back.
    ``StrategyService.update_config`` commits internally, so by the time
    ``_reload_strategy_after_save`` runs there is nothing in flight.
    """
    from app.api import strategy as strategy_api

    reloads: list[object] = []

    class _FakeRunner:
        def reload_strategy(self, db: object = None) -> None:
            reloads.append(db)

    monkeypatch.setattr(strategy_api, "get_runner", lambda: _FakeRunner())

    with database.SessionLocal() as db:
        strategy_api._reload_strategy_after_save(db=db)

    assert reloads == [db], (
        "the strategy-save reload must hand the runner the request session it "
        "is already holding"
    )


def test_a_runner_that_cannot_borrow_is_still_a_successful_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signature mismatch must not be reported as a failed strategy save.

    ``_reload_strategy_after_save``'s failure path rolls the save back and can
    pause trading. Lending the session is an optimisation, so a runner whose
    ``reload_strategy`` takes no ``db`` is called without one instead of being
    treated as an activation failure -- otherwise every such caller turns a
    healthy save into a 503 and a paused runner.
    """
    from app.api import strategy as strategy_api

    calls: list[str] = []

    class _LegacyRunner:
        def reload_strategy(self) -> None:
            calls.append("reloaded")

    monkeypatch.setattr(strategy_api, "get_runner", lambda: _LegacyRunner())

    with database.SessionLocal() as db:
        strategy_api._reload_strategy_after_save(db=db)

    assert calls == ["reloaded"]


def test_a_type_error_from_inside_the_reload_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compatibility is decided by the signature, never by swallowing TypeError.

    A runner that DOES accept ``db`` and then raises ``TypeError`` from inside
    the reload has genuinely failed to activate. Catching ``TypeError`` to
    detect the old signature would silently retry it and then report success,
    leaving the live engine on the previous config while the API says the save
    took effect.
    """
    from app.api import strategy as strategy_api

    class _BrokenRunner:
        def reload_strategy(self, db: object = None) -> None:
            raise TypeError("genuine failure inside the reload")

        def pause_for_manual_control(self, _reason: str) -> bool:
            return True

    monkeypatch.setattr(strategy_api, "get_runner", lambda: _BrokenRunner())

    with database.SessionLocal() as db:
        with pytest.raises(TypeError):
            strategy_api._reload_strategy_after_save(db=db)


def test_sync_symbol_runtimes_reads_opening_policies_on_the_given_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#12: ``active_policies`` is a pure read and already borrows.

    The sweep attributed one event to
    ``OpeningMomentumExecutionService.active_policies``, but the stack shows the
    second connection was checked out by ``reload_strategy`` further up --
    ``active_policies`` was simply the frame that first touched the database
    after it. It takes the caller's ``db`` and opens nothing, and
    ``_sync_symbol_runtimes`` passes the session it was given.

    This pins that, so a refactor that gave either an owned session would fail
    here rather than resurface as a pool warning.
    """
    runner = _runner()
    counter = _SessionCounter(monkeypatch)
    guard = database.session_reentrancy_guard
    before = guard.violation_count

    with database.SessionLocal() as db:
        runner._sync_symbol_runtimes(db)

    assert counter.opened == 0
    assert guard.violation_count == before


def test_today_order_sync_reloads_tracked_entries_on_its_own_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#10: the sync already reuses its session; the sweep event was an artifact.

    ``_sync_today_orders_from_broker_serialized`` calls ``_load_tracked_entries(db)``
    one line after ``db.commit()``, inside its own
    ``with self._db_session() as db``. It passes that session, opens nothing,
    and the commit has ended the transaction, so no uncommitted order write
    crosses the read.

    The event the sweep attributed here came from
    ``tests/test_trade_event_sync.py``, which calls
    ``_load_tracked_entries(SessionLocal())`` and never closes that session.
    The leaked connection is still checked out when the test then runs the real
    sync, so the sync's own first checkout is the thread's second. The site is
    correct; the caller in the test leaks.

    Pinned at the pool, so a future edit that made this path own a session
    would fail here.
    """
    class _Broker:
        def get_today_orders(self) -> list[object]:
            return []

    runner = _runner()
    runner.broker = cast(Any, _Broker())
    guard = database.session_reentrancy_guard
    before = guard.violation_count

    runner.sync_today_orders_from_broker(force=True)

    assert guard.violation_count == before, (
        "the today-order sync checked out a second pooled connection"
    )
    assert _checked_out_connections() == 0, (
        "the today-order sync leaked a pooled connection"
    )


def test_opening_momentum_cron_tick_holds_one_connection_through_registry_refresh(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 2026-09-04 live violation: the cron's session plus the runner's own.

    ``_opening_momentum_shadow_tick_sync`` opens one ``SessionLocal`` and
    holds it across ``OpeningMomentumExecutionService.tick``. With execution
    disabled (the P0 clamp forces it off) the tick takes the registry-refresh
    branch, which reached ``AppRunner.refresh_opening_execution_registry`` --
    and that opened a SECOND session for ``_sync_symbol_runtimes`` /
    ``_load_opening_execution_registry`` while the cron still held the first.

    Runs the REAL cron entry point on a worker thread, exactly as production
    does via ``asyncio.to_thread``. The service must lend the cron its
    session, the runner must borrow it, and the borrow must be provably
    inert: one connection held, zero runner-owned sessions, no guard
    violation, and the registry genuinely refreshed on the borrowed session.
    """
    from app import main as main_module
    from app.models import OpeningMomentumExecution, RuntimeState, WatchlistItem

    runner = _runner()
    monkeypatch.setattr(main_module, "get_runner", lambda: runner)
    monkeypatch.setattr(settings, "opening_momentum_shadow_enabled", False)
    monkeypatch.setattr(settings, "opening_momentum_challenger_enabled", False)

    secondary_symbol = "BORROWSESSION2.US"
    with database.SessionLocal() as db:
        # A watchlist symbol with NO persisted runtime row: the borrowed
        # refresh reaches load_symbol_runtime for it, which must not create
        # a row or finalize the cron's transaction.
        db.add(WatchlistItem(symbol=secondary_symbol, market="US"))
        db.add(
            OpeningMomentumExecution(
                session_date=date(2026, 9, 4),
                algorithm_version="borrow-session-test",
                config_version="borrow-session-test",
                universe_source="BORROWSESSION",
                status="ARMED",
                symbol=SYMBOL,
                signal_at=datetime(2026, 9, 4, 13, 32, tzinfo=timezone.utc),
                armed_at=datetime(2026, 9, 4, 13, 32, tzinfo=timezone.utc),
                entry_due_at=datetime(2026, 9, 4, 13, 34, tzinfo=timezone.utc),
                entry_deadline_at=datetime(
                    2026, 9, 4, 13, 36, tzinfo=timezone.utc
                ),
                max_price_deviation_bps=200.0,
                stop_loss_pct=1.0,
                max_holding_minutes=60,
            )
        )
        db.commit()

    tracker = _CheckoutDepthTracker()
    counter = _SessionCounter(monkeypatch)
    guard = database.session_reentrancy_guard
    violations_before = guard.violation_count
    failures: list[BaseException] = []
    tick_thread_ids: list[int] = []

    def run_tick() -> None:
        tick_thread_ids.append(threading.get_ident())
        try:
            main_module._opening_momentum_shadow_tick_sync()
        except BaseException as exc:  # reported, never swallowed
            failures.append(exc)

    try:
        with caplog.at_level(logging.WARNING):
            tick_thread = threading.Thread(
                target=run_tick,
                name="opening-momentum-cron-test",
            )
            tick_thread.start()
            tick_thread.join(timeout=30)
    finally:
        tracker.remove()
        for thread_id in tick_thread_ids:
            _drop_guard_depth_for(thread_id)

    assert tick_thread.is_alive() is False, "the opening-momentum tick hung"
    assert failures == []
    assert counter.opened == 0, (
        f"the registry refresh opened {counter.opened} session(s) of its own "
        "while the opening-momentum cron still held its session"
    )
    assert tracker.max_depth == 1, (
        f"the tick held {tracker.max_depth} pooled connections at once; the "
        "cron's session must be the only one"
    )
    assert guard.violation_count == violations_before, (
        "the opening-momentum tick checked out a second pooled connection: "
        "the 2026-09-04 live violation shape"
    )
    assert "re-entrant database session" not in caplog.text
    assert "opening momentum execution tick failed" not in caplog.text
    # The registry was genuinely refreshed on the borrowed session, not
    # silently emptied by a swallowed failure.
    policy = runner._opening_execution_policies.get(SYMBOL)
    assert policy is not None
    assert policy.status == "ARMED"
    # And the borrowed path left no runtime row behind for the symbol that
    # has none: a get-or-create there would commit the cron's transaction.
    with database.SessionLocal() as db:
        assert (
            db.query(RuntimeState)
            .filter(RuntimeState.symbol == secondary_symbol)
            .first()
            is None
        )


def test_registry_refresh_without_a_caller_session_still_owns_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Characterization: legacy no-db callers keep owning exactly one session.

    The post-fill refresh (called after its session has ended) and the
    reduction refresh (same shape) both call
    ``refresh_opening_execution_registry()`` with no argument, as does
    ``tests/test_runner.py``. Making ``db`` optional must not change that
    shape: one session opened, one connection returned to the pool.
    """
    runner = _runner()
    counter = _SessionCounter(monkeypatch)

    runner.refresh_opening_execution_registry()

    assert counter.opened == 1, (
        "a registry refresh with no caller session must own the one session "
        f"it needs; it opened {counter.opened}"
    )
    assert _checked_out_connections() == 0, (
        "the registry refresh leaked a pooled connection"
    )
