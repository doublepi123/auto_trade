from __future__ import annotations

import multiprocessing
import os
import pickle
import signal
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal
from functools import cache
from multiprocessing.connection import Connection
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.domain.watchlist_quant_v6.semantics as semantics_module
from app.config import Settings
from app.schemas import DatabaseHealthSnapshot
from app.services import database_health_service
from app.services import watchlist_quant_v6_publication_service as publication_module
from app.domain.universe_selection import (
    INDEX_MEMBERSHIP_HISTORY,
    ROTATION_RESEARCH_CANDIDATE_CATALOG,
)
from app.domain.watchlist_quant_v6 import (
    QuantV6Bar,
    quant_v6_expected_rth_bar_starts,
)
from app.services import watchlist_quant_v6_spawn_supervisor as supervisor
from app.services.watchlist_quant_v6_deadline import (
    QuantV6EvaluationCancelledError,
    QuantV6EvaluationDeadline,
    QuantV6EvaluationDeadlineExceededError,
)
from app.services.watchlist_quant_v6_evaluation_service import (
    QuantV6HistoricalEvaluationError,
    _build_registration_plan,
    build_latest_quant_v6_registration_plan,
    evaluate_quant_v6_registration,
)
from app.services.watchlist_quant_v6_historical_provider import (
    QuantV6HistoricalBarFetch,
)
from app.services.watchlist_quant_v6_publication_service import (
    _assemble_prepared_publication,
    _prepare_candidate_publication,
    _prepare_publication,
)


_OBSERVED_AT = datetime(2026, 7, 31, 23, 0, tzinfo=timezone.utc)
_PROCESS_PREFIX = "quant-v6-compute-"


class _TrackingProvider:
    def __init__(
        self,
        bars: tuple[QuantV6Bar, ...] = (),
        *,
        delay_seconds: float = 0.0,
    ) -> None:
        self._bars = bars
        self._delay_seconds = delay_seconds
        self._lock = threading.Lock()
        self.calls = 0
        self.active = 0
        self.max_active = 0

    @staticmethod
    def supports_quant_v6_spawn_fetch(
        *,
        evaluation_deadline: QuantV6EvaluationDeadline,
    ) -> bool:
        del evaluation_deadline
        return True

    def fetch_five_minute_no_adjust(
        self,
        symbol: str,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> QuantV6HistoricalBarFetch:
        del symbol, start_at, end_at
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self._delay_seconds:
                time.sleep(self._delay_seconds)
            return QuantV6HistoricalBarFetch(
                bars=self._bars,
                pages=1,
                raw_rows=len(self._bars),
                rejected_rows=0,
            )
        finally:
            with self._lock:
                self.active -= 1


@cache
def _plan(member_count: int):
    full = build_latest_quant_v6_registration_plan(
        observed_at=_OBSERVED_AT,
    )
    if not 1 <= member_count <= len(full.members):
        raise AssertionError("unsupported spawn-test member count")
    candidate_by_symbol = {
        candidate.symbol: candidate
        for candidate in ROTATION_RESEARCH_CANDIDATE_CATALOG
    }
    return _build_registration_plan(
        observed_at=_OBSERVED_AT,
        market="US",
        candidates=tuple(
            candidate_by_symbol[member.symbol]
            for member in full.members[:member_count]
        ),
        membership_history=INDEX_MEMBERSHIP_HISTORY,
    )


def _bar(
    start_at: datetime,
    index: int,
    *,
    opened: Decimal | str = "100",
    closed: Decimal | str | None = None,
) -> QuantV6Bar:
    open_value = Decimal(opened)
    close_value = (
        Decimal(closed)
        if closed is not None
        else Decimal("100") + Decimal(index % 2) / Decimal("10")
    )
    return QuantV6Bar(
        start_at=start_at,
        open=open_value,
        high=max(open_value, close_value) + Decimal("1"),
        low=min(open_value, close_value) - Decimal("1"),
        close=close_value,
        volume=Decimal("1000"),
    )


@cache
def _exact_event_bars(expected_event_count: int) -> tuple[QuantV6Bar, ...]:
    if expected_event_count == 0:
        event_counts = (0,) * 30
    elif expected_event_count == 60:
        event_counts = (2,) * 30
    elif expected_event_count == 135:
        event_counts = (5,) * 15 + (4,) * 15
    else:
        raise AssertionError("unsupported exact event fixture")
    plan = _plan(1)
    training_dates = set(plan.training_session_dates)
    target_ordinal_by_date = {
        session_date: ordinal
        for ordinal, session_date in enumerate(plan.target_session_dates)
    }
    values: list[QuantV6Bar] = []
    for session_date in (
        *plan.training_session_dates,
        *plan.target_session_dates,
    ):
        starts = quant_v6_expected_rth_bar_starts("US", session_date)
        bars = [
            _bar(
                start,
                index,
                closed=(
                    None
                    if session_date in training_dates
                    else Decimal("100")
                    + Decimal(index) / Decimal("1000")
                ),
            )
            for index, start in enumerate(starts)
        ]
        target_ordinal = target_ordinal_by_date.get(session_date)
        if target_ordinal is not None and event_counts[target_ordinal]:
            epsilon = Decimal(1).scaleb(-20 * (target_ordinal + 1))
            signal_indices = tuple(
                1 + 8 * index
                for index in range(event_counts[target_ordinal])
            )
            last_exit_index = signal_indices[-1] + 7
            for index in range(last_exit_index, len(bars)):
                price = (
                    Decimal("102")
                    + Decimal(index - last_exit_index) / Decimal("10")
                )
                bars[index] = _bar(
                    starts[index],
                    index,
                    opened=price,
                    closed=price,
                )
            for signal_index in signal_indices:
                bars[signal_index] = QuantV6Bar(
                    start_at=starts[signal_index],
                    open=Decimal("100"),
                    high=Decimal("101"),
                    low=epsilon / Decimal("2"),
                    close=epsilon,
                    volume=Decimal("1000"),
                )
                exit_index = signal_index + 7
                bars[exit_index] = _bar(
                    starts[exit_index],
                    exit_index,
                    opened="102",
                    closed="102",
                )
        values.extend(bars)
    return tuple(values)


def _send_ready(
    connection: Any,
    *,
    worker_index: int,
    max_frame_bytes: int,
) -> None:
    supervisor._send_wire(
        connection,
        supervisor._ReadyWire(
            protocol_version=supervisor._WIRE_PROTOCOL_VERSION,
            worker_index=worker_index,
            pid=os.getpid(),
        ),
        max_frame_bytes=max_frame_bytes,
    )


def _parallel_prepared_worker(
    connection: Any,
    selective_cancel_reader: Any,
    deadline_at: float,
    worker_index: int,
    max_frame_bytes: int,
    prepared_candidates: tuple[Any, ...],
    concurrency: Any,
) -> None:
    del deadline_at
    try:
        _send_ready(
            connection,
            worker_index=worker_index,
            max_frame_bytes=max_frame_bytes,
        )
        while True:
            if not connection.poll(0.05):
                continue
            value = supervisor._receive_wire(
                connection,
                max_frame_bytes=max_frame_bytes,
            )
            if type(value) is supervisor._StopWire:
                return
            if type(value) is not supervisor._CandidateWorkWire:
                return
            with concurrency.get_lock():
                concurrency[0] += 1
                concurrency[1] = max(concurrency[1], concurrency[0])
            try:
                stop_at = time.monotonic() + 0.5
                while time.monotonic() < stop_at:
                    time.sleep(0.01)
                supervisor._send_wire(
                    connection,
                    supervisor._CandidateSuccessWire(
                        protocol_version=supervisor._WIRE_PROTOCOL_VERSION,
                        ordinal=value.ordinal,
                        prepared=prepared_candidates[value.ordinal],
                        compute_ms=500,
                        closure_ms=0,
                    ),
                    max_frame_bytes=max_frame_bytes,
                )
            finally:
                with concurrency.get_lock():
                    concurrency[0] -= 1
    finally:
        connection.close()
        selective_cancel_reader.close()


def _ordinal_failure_worker(
    connection: Any,
    selective_cancel_reader: Any,
    deadline_at: float,
    worker_index: int,
    max_frame_bytes: int,
    high_ordinal_stats: Any,
) -> None:
    del deadline_at
    child_stop = supervisor._ChildStopSignal(
        selective_cancel_reader,
        max_frame_bytes=max_frame_bytes,
    )
    try:
        _send_ready(
            connection,
            worker_index=worker_index,
            max_frame_bytes=max_frame_bytes,
        )
        while True:
            if not connection.poll(0.05):
                continue
            value = supervisor._receive_wire(
                connection,
                max_frame_bytes=max_frame_bytes,
            )
            if type(value) is supervisor._StopWire:
                return
            if type(value) is not supervisor._CandidateWorkWire:
                return
            ordinal = value.ordinal
            if ordinal == 5:
                # Ordinal 5 failing is what makes the parent stop waiting and
                # tear down, and abnormal teardown terminates children without
                # a cooperative frame. A blind sleep here therefore raced the
                # higher ordinal's cancel observation: the parent reached
                # terminate() about 600ms after the cancel was written, so on a
                # loaded runner the child was killed between two 10ms polls and
                # `[1] == [0]` failed with nothing actually broken. Wait for the
                # observation instead of guessing a duration -- this cannot mask
                # a real lost cancel, it only stops the parent from ending the
                # run before the child could report one.
                release_at = time.monotonic() + 15.0
                while time.monotonic() < release_at:
                    with high_ordinal_stats.get_lock():
                        if high_ordinal_stats[1] >= 1:
                            break
                    time.sleep(0.01)
                failure = supervisor._failure_wire(
                    ordinal=ordinal,
                    kind="EVALUATION",
                    exc=RuntimeError("slow ordinal five failure"),
                )
                supervisor._send_wire(
                    connection,
                    failure,
                    max_frame_bytes=max_frame_bytes,
                )
                return
            if ordinal == 8:
                time.sleep(0.25)
                failure = supervisor._failure_wire(
                    ordinal=ordinal,
                    kind="EVALUATION",
                    exc=RuntimeError("fast ordinal eight failure"),
                )
                supervisor._send_wire(
                    connection,
                    failure,
                    max_frame_bytes=max_frame_bytes,
                )
                return
            if ordinal > 8:
                with high_ordinal_stats.get_lock():
                    high_ordinal_stats[0] += 1
                stop_at = time.monotonic() + 5.0
                while not child_stop.is_set() and time.monotonic() < stop_at:
                    time.sleep(0.01)
                if child_stop.is_set():
                    with high_ordinal_stats.get_lock():
                        high_ordinal_stats[1] += 1
                        high_ordinal_stats[2] += 1
                    failure = supervisor._failure_wire(
                        ordinal=ordinal,
                        kind="CANCELLED",
                        exc=RuntimeError("higher ordinal selectively cancelled"),
                    )
                else:
                    failure = supervisor._failure_wire(
                        ordinal=ordinal,
                        kind="UNEXPECTED",
                        exc=RuntimeError("higher ordinal cancel timed out"),
                    )
                supervisor._send_wire(
                    connection,
                    failure,
                    max_frame_bytes=max_frame_bytes,
                )
                return
            request = value.completed_fetch.request
            from app.services.watchlist_quant_v6_publication_service import (
                _PreparedCandidatePublication,
            )

            prepared = _PreparedCandidatePublication(
                registration_identity_sha256=(
                    request.registration.identity_sha256
                ),
                member=request.member,
                bindings=(),
                artifacts=(),
                acquisition_outcome={},
                assessment_count=0,
                session_input_count=0,
                event_count=0,
            )
            supervisor._send_wire(
                connection,
                supervisor._CandidateSuccessWire(
                    protocol_version=supervisor._WIRE_PROTOCOL_VERSION,
                    ordinal=ordinal,
                    prepared=prepared,
                    compute_ms=0,
                    closure_ms=0,
                ),
                max_frame_bytes=max_frame_bytes,
            )
    finally:
        connection.close()
        selective_cancel_reader.close()


def _cooperative_hang_worker(
    connection: Any,
    selective_cancel_reader: Any,
    deadline_at: float,
    worker_index: int,
    max_frame_bytes: int,
    active: Any,
) -> None:
    del deadline_at
    child_stop = supervisor._ChildStopSignal(
        selective_cancel_reader,
        max_frame_bytes=max_frame_bytes,
    )
    try:
        _send_ready(
            connection,
            worker_index=worker_index,
            max_frame_bytes=max_frame_bytes,
        )
        while not child_stop.is_set():
            if not connection.poll(0.05):
                continue
            value = supervisor._receive_wire(
                connection,
                max_frame_bytes=max_frame_bytes,
            )
            if type(value) is supervisor._StopWire:
                return
            if type(value) is supervisor._CandidateWorkWire:
                active.set()
                while not child_stop.wait(0.02):
                    pass
                return
    finally:
        connection.close()
        selective_cancel_reader.close()


def _crash_worker(
    connection: Any,
    selective_cancel_reader: Any,
    deadline_at: float,
    worker_index: int,
    max_frame_bytes: int,
) -> None:
    del deadline_at
    _send_ready(
        connection,
        worker_index=worker_index,
        max_frame_bytes=max_frame_bytes,
    )
    while not connection.poll(0.05):
        pass
    value = supervisor._receive_wire(
        connection,
        max_frame_bytes=max_frame_bytes,
    )
    if type(value) is supervisor._CandidateWorkWire:
        os._exit(17)
    selective_cancel_reader.close()
    connection.close()


def _sibling_owner_death_worker(
    connection: Any,
    selective_cancel_reader: Any,
    deadline_at: float,
    worker_index: int,
    max_frame_bytes: int,
    poisoned_event: Any,
    poison_acquired: Any,
    lower_completed: Any,
) -> None:
    del deadline_at
    _send_ready(
        connection,
        worker_index=worker_index,
        max_frame_bytes=max_frame_bytes,
    )
    while not connection.poll(0.05):
        pass
    value = supervisor._receive_wire(
        connection,
        max_frame_bytes=max_frame_bytes,
    )
    if type(value) is supervisor._CandidateWorkWire:
        if value.ordinal == 1:
            condition = getattr(poisoned_event, "_cond", None)
            if condition is None:
                os._exit(91)
            condition.acquire()
            poison_acquired.value = 1
            os._exit(17)
        if value.ordinal == 0:
            poison_until = time.monotonic() + 2.0
            while not poison_acquired.value and time.monotonic() < poison_until:
                time.sleep(0.01)
            if not poison_acquired.value:
                os._exit(93)
            child_stop = supervisor._ChildStopSignal(
                selective_cancel_reader,
                max_frame_bytes=max_frame_bytes,
            )
            if child_stop.is_set():
                os._exit(94)
            request = value.completed_fetch.request
            from app.services.watchlist_quant_v6_publication_service import (
                _PreparedCandidatePublication,
            )

            lower_completed.value = 1
            supervisor._send_wire(
                connection,
                supervisor._CandidateSuccessWire(
                    protocol_version=supervisor._WIRE_PROTOCOL_VERSION,
                    ordinal=value.ordinal,
                    prepared=_PreparedCandidatePublication(
                        registration_identity_sha256=(
                            request.registration.identity_sha256
                        ),
                        member=request.member,
                        bindings=(),
                        artifacts=(),
                        acquisition_outcome={},
                        assessment_count=0,
                        session_input_count=0,
                        event_count=0,
                    ),
                    compute_ms=400,
                    closure_ms=0,
                ),
                max_frame_bytes=max_frame_bytes,
            )
            while True:
                if connection.poll(0.05):
                    received = supervisor._receive_wire(
                        connection,
                        max_frame_bytes=max_frame_bytes,
                    )
                    if type(received) is supervisor._StopWire:
                        return
        os._exit(92)
    selective_cancel_reader.close()
    connection.close()


def _uncooperative_ready_worker(
    connection: Any,
    selective_cancel_reader: Any,
    deadline_at: float,
    worker_index: int,
    max_frame_bytes: int,
    ignore_sigterm: bool,
) -> None:
    del deadline_at
    if ignore_sigterm:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    _send_ready(
        connection,
        worker_index=worker_index,
        max_frame_bytes=max_frame_bytes,
    )
    while True:
        time.sleep(1.0)


def _sigstop_before_work_worker(
    connection: Any,
    selective_cancel_reader: Any,
    deadline_at: float,
    worker_index: int,
    max_frame_bytes: int,
) -> None:
    del deadline_at, selective_cancel_reader
    _send_ready(
        connection,
        worker_index=worker_index,
        max_frame_bytes=max_frame_bytes,
    )
    os.kill(os.getpid(), signal.SIGSTOP)
    while True:
        time.sleep(1.0)


def _sigstop_mid_result_worker(
    connection: Any,
    selective_cancel_reader: Any,
    deadline_at: float,
    worker_index: int,
    max_frame_bytes: int,
    parent_receive_started: Any,
) -> None:
    del deadline_at, selective_cancel_reader
    _send_ready(
        connection,
        worker_index=worker_index,
        max_frame_bytes=max_frame_bytes,
    )
    while not connection.poll(0.05):
        pass
    value = supervisor._receive_wire(
        connection,
        max_frame_bytes=max_frame_bytes,
    )
    if type(value) is not supervisor._CandidateWorkWire:
        return
    request = value.completed_fetch.request
    from app.services.watchlist_quant_v6_publication_service import (
        _PreparedCandidatePublication,
    )

    prepared = _PreparedCandidatePublication(
        registration_identity_sha256=request.registration.identity_sha256,
        member=request.member,
        bindings=(),
        artifacts=(),
        acquisition_outcome={"padding": b"x" * (1024 * 1024)},
        assessment_count=0,
        session_input_count=0,
        event_count=0,
    )
    payload = supervisor._serialize_wire(
        supervisor._CandidateSuccessWire(
            protocol_version=supervisor._WIRE_PROTOCOL_VERSION,
            ordinal=value.ordinal,
            prepared=prepared,
            compute_ms=0,
            closure_ms=0,
        ),
        max_frame_bytes=max_frame_bytes,
    )
    framed_prefix = len(payload).to_bytes(4, byteorder="big", signed=True)
    framed_prefix += payload[:4_096]
    view = memoryview(framed_prefix)
    while view:
        written = os.write(connection.fileno(), view)
        view = view[written:]
    if not parent_receive_started.wait(5):
        return
    os.kill(os.getpid(), signal.SIGSTOP)
    while True:
        time.sleep(1.0)


def _spawn_custom_workers(
    *,
    worker_count: int,
    deadline_at: float,
    max_frame_bytes: int,
    target: Any,
    extra_args: tuple[Any, ...] = (),
) -> list[Any]:
    context = multiprocessing.get_context("spawn")
    states: list[Any] = []
    try:
        for worker_index in range(worker_count):
            parent_connection, child_connection = context.Pipe(duplex=True)
            cancel_reader, cancel_writer = context.Pipe(duplex=False)
            process = context.Process(
                target=target,
                args=(
                    child_connection,
                    cancel_reader,
                    deadline_at,
                    worker_index,
                    max_frame_bytes,
                    *extra_args,
                ),
                name=f"{_PROCESS_PREFIX}{worker_index}",
                daemon=False,
            )
            process.start()
            states.append(supervisor._WorkerState(
                worker_index=worker_index,
                process=process,
                connection=cast(Connection, parent_connection),
                cancel_connection=cast(Connection, cancel_writer),
            ))
            child_connection.close()
            cancel_reader.close()
        return states
    except BaseException:
        for state in states:
            if state.process.is_alive():
                state.process.kill()
            state.process.join(timeout=2)
            state.connection.close()
            state.cancel_connection.close()
        raise


def _run_sigstop_work_send_probe(
    result_connection: Any,
    inner_pid: Any,
) -> None:
    original_start_workers = supervisor._start_workers
    original_send_wire = supervisor._send_wire
    work_send_started = False
    work_send_completed = False
    work_frame_bytes = 0

    def _start(**kwargs: Any):
        states = _spawn_custom_workers(
            **kwargs,
            target=_sigstop_before_work_worker,
        )
        with inner_pid.get_lock():
            inner_pid.value = states[0].process.pid or 0
        return states

    def _track_send(
        connection: Any,
        value: object,
        *,
        max_frame_bytes: int,
    ) -> None:
        nonlocal work_send_started, work_send_completed, work_frame_bytes
        if type(value) is supervisor._CandidateWorkWire:
            work_send_started = True
            work_frame_bytes = len(pickle.dumps(value, protocol=5))
        original_send_wire(
            connection,
            value,
            max_frame_bytes=max_frame_bytes,
        )
        if type(value) is supervisor._CandidateWorkWire:
            work_send_completed = True

    supervisor._start_workers = _start
    supervisor._send_wire = _track_send
    bars = _exact_event_bars(0)
    deadline = QuantV6EvaluationDeadline(3.0)
    started_at = time.monotonic()
    error_type = "NO_ERROR"
    error_message = ""
    try:
        supervisor.evaluate_prepare_quant_v6_registration_spawn(
            registration=_plan(1),
            provider=_TrackingProvider(bars),
            evaluation_deadline=deadline,
            worker_count=2,
            memory_limit_mib=2_048,
        )
    except BaseException as exc:
        error_type = type(exc).__name__
        error_message = str(exc)
    finally:
        supervisor._start_workers = original_start_workers
        supervisor._send_wire = original_send_wire

    elapsed = time.monotonic() - started_at
    leak_until = time.monotonic() + 2.0
    child_names: tuple[str, ...] = ()
    thread_names: tuple[str, ...] = ()
    while time.monotonic() < leak_until:
        child_names = tuple(
            child.name
            for child in multiprocessing.active_children()
            if child.name.startswith(_PROCESS_PREFIX)
        )
        thread_names = tuple(
            thread.name
            for thread in threading.enumerate()
            if (
                thread.name.startswith("quant-v6-prefetch")
                or thread.name == "quant-v6-watchdog"
                or thread.name == "QueueFeederThread"
            )
        )
        if not child_names and not thread_names:
            break
        time.sleep(0.05)
    result_connection.send({
        "child_names": child_names,
        "elapsed": elapsed,
        "error_message": error_message,
        "error_type": error_type,
        "thread_names": thread_names,
        "work_frame_bytes": work_frame_bytes,
        "work_send_completed": work_send_completed,
        "work_send_started": work_send_started,
    })
    result_connection.close()


def _run_sibling_owner_death_probe(
    result_connection: Any,
    inner_pids: Any,
) -> None:
    context = multiprocessing.get_context("spawn")
    poisoned_event = context.Event()
    poison_acquired = context.Value("i", 0, lock=False)
    lower_completed = context.Value("i", 0, lock=False)
    original_start_workers = supervisor._start_workers

    def _start(**kwargs: Any):
        states = _spawn_custom_workers(
            **kwargs,
            target=_sibling_owner_death_worker,
            extra_args=(poisoned_event, poison_acquired, lower_completed),
        )
        with inner_pids.get_lock():
            for index, state in enumerate(states):
                inner_pids[index] = state.process.pid or 0
        return states

    supervisor._start_workers = _start
    started_at = time.monotonic()
    error_message = ""
    error_type = "NO_ERROR"
    try:
        supervisor.evaluate_prepare_quant_v6_registration_spawn(
            registration=_plan(2),
            provider=_TrackingProvider(),
            evaluation_deadline=QuantV6EvaluationDeadline(4),
            worker_count=2,
            memory_limit_mib=2_048,
        )
    except BaseException as exc:
        error_message = str(exc)
        error_type = type(exc).__name__
    finally:
        supervisor._start_workers = original_start_workers

    result_connection.send({
        "elapsed": time.monotonic() - started_at,
        "error_message": error_message,
        "error_type": error_type,
        "lower_completed": lower_completed.value,
    })
    result_connection.close()


def _run_sigstop_result_receive_probe(
    result_connection: Any,
    inner_pid: Any,
) -> None:
    context = multiprocessing.get_context("spawn")
    parent_receive_started = context.Event()
    original_start_workers = supervisor._start_workers
    original_receive_wire = supervisor._receive_wire
    ready_received = False
    result_receive_started = False
    result_receive_completed = False

    def _start(**kwargs: Any):
        states = _spawn_custom_workers(
            **kwargs,
            target=_sigstop_mid_result_worker,
            extra_args=(parent_receive_started,),
        )
        with inner_pid.get_lock():
            inner_pid.value = states[0].process.pid or 0
        return states

    def _track_receive(
        connection: Any,
        *,
        max_frame_bytes: int,
    ) -> object:
        nonlocal ready_received
        nonlocal result_receive_started, result_receive_completed
        is_result = ready_received
        if is_result:
            result_receive_started = True
            parent_receive_started.set()
        value = original_receive_wire(
            connection,
            max_frame_bytes=max_frame_bytes,
        )
        if type(value) is supervisor._ReadyWire:
            ready_received = True
        elif is_result:
            result_receive_completed = True
        return value

    supervisor._start_workers = _start
    supervisor._receive_wire = _track_receive
    deadline = QuantV6EvaluationDeadline(3.0)
    started_at = time.monotonic()
    error_type = "NO_ERROR"
    error_message = ""
    try:
        supervisor.evaluate_prepare_quant_v6_registration_spawn(
            registration=_plan(1),
            provider=_TrackingProvider(),
            evaluation_deadline=deadline,
            worker_count=2,
            memory_limit_mib=2_048,
        )
    except BaseException as exc:
        error_type = type(exc).__name__
        error_message = str(exc)
    finally:
        supervisor._start_workers = original_start_workers
        supervisor._receive_wire = original_receive_wire

    elapsed = time.monotonic() - started_at
    leak_until = time.monotonic() + 2.0
    child_names: tuple[str, ...] = ()
    thread_names: tuple[str, ...] = ()
    while time.monotonic() < leak_until:
        child_names = tuple(
            child.name
            for child in multiprocessing.active_children()
            if child.name.startswith(_PROCESS_PREFIX)
        )
        thread_names = tuple(
            thread.name
            for thread in threading.enumerate()
            if (
                thread.name.startswith("quant-v6-prefetch")
                or thread.name == "quant-v6-watchdog"
                or thread.name == "QueueFeederThread"
            )
        )
        if not child_names and not thread_names:
            break
        time.sleep(0.05)
    result_connection.send({
        "child_names": child_names,
        "elapsed": elapsed,
        "error_message": error_message,
        "error_type": error_type,
        "result_receive_completed": result_receive_completed,
        "result_receive_started": result_receive_started,
        "thread_names": thread_names,
    })
    result_connection.close()


def _wait_for_quant_resources_to_close() -> None:
    stop_at = time.monotonic() + 8.0
    while time.monotonic() < stop_at:
        quant_children = [
            child
            for child in multiprocessing.active_children()
            if child.name.startswith(_PROCESS_PREFIX)
        ]
        quant_threads = [
            thread
            for thread in threading.enumerate()
            if (
                thread.name.startswith("quant-v6-prefetch")
                or thread.name == "quant-v6-watchdog"
                or thread.name == "QueueFeederThread"
            )
        ]
        if not quant_children and not quant_threads:
            return
        time.sleep(0.05)
    raise AssertionError(
        "quant-v6 spawn resources survived bounded cleanup: "
        f"children={[(child.pid, child.name) for child in quant_children]} "
        f"threads={[thread.name for thread in quant_threads]}"
    )


@pytest.fixture(autouse=True)
def _no_spawn_resource_leaks() -> Iterator[None]:
    _wait_for_quant_resources_to_close()
    yield
    _wait_for_quant_resources_to_close()


def test_real_workers_use_true_spawn_start_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[int | None, str | None, str]] = []
    original = supervisor._start_workers

    def _capture_start(**kwargs: Any):
        states = original(**kwargs)
        captured.extend(
            (
                state.process.pid,
                getattr(state.process, "_start_method", None),
                state.process.name,
            )
            for state in states
        )
        return states

    monkeypatch.setattr(supervisor, "_start_workers", _capture_start)

    prepared = supervisor.evaluate_prepare_quant_v6_registration_spawn(
        registration=_plan(2),
        provider=_TrackingProvider(),
        evaluation_deadline=QuantV6EvaluationDeadline(60),
        worker_count=4,
        memory_limit_mib=2_048,
    )

    assert prepared.assessment_count == 2
    assert len(captured) == 2
    assert all(pid is not None and pid != os.getpid() for pid, _, _ in captured)
    assert all(method == "spawn" for _, method, _ in captured)
    assert [name for _, _, name in captured] == [
        "quant-v6-compute-0",
        "quant-v6-compute-1",
    ]


def test_unbounded_provider_is_rejected_before_workers_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnboundedProvider(_TrackingProvider):
        @staticmethod
        def supports_quant_v6_spawn_fetch(
            *,
            evaluation_deadline: QuantV6EvaluationDeadline,
        ) -> bool:
            del evaluation_deadline
            return False

    worker_started = False

    def _unexpected_start(**_kwargs: Any) -> None:
        nonlocal worker_started
        worker_started = True
        raise AssertionError("spawn workers started for an unbounded provider")

    monkeypatch.setattr(supervisor, "_start_workers", _unexpected_start)

    with pytest.raises(
        ValueError,
        match="historical provider with bounded fetches",
    ):
        supervisor.evaluate_prepare_quant_v6_registration_spawn(
            registration=_plan(1),
            provider=_UnboundedProvider(),
            evaluation_deadline=QuantV6EvaluationDeadline(60),
            worker_count=2,
            memory_limit_mib=2_048,
        )

    assert worker_started is False


def test_provider_is_serial_while_four_spawn_workers_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(4)
    evaluations = evaluate_quant_v6_registration(
        registration=plan,
        provider=_TrackingProvider(),
    )
    prepared_candidates = tuple(
        _prepare_candidate_publication(plan=plan, evaluation=evaluation)
        for evaluation in evaluations
    )
    expected = _assemble_prepared_publication(
        plan=plan,
        candidates=prepared_candidates,
    )
    context = multiprocessing.get_context("spawn")
    concurrency = context.Array("i", (0, 0), lock=True)
    captured_count = 0

    def _start(**kwargs: Any):
        nonlocal captured_count
        states = _spawn_custom_workers(
            **kwargs,
            target=_parallel_prepared_worker,
            extra_args=(prepared_candidates, concurrency),
        )
        captured_count = len(states)
        return states

    monkeypatch.setattr(supervisor, "_start_workers", _start)
    provider = _TrackingProvider(delay_seconds=0.02)

    actual = supervisor.evaluate_prepare_quant_v6_registration_spawn(
        registration=plan,
        provider=provider,
        evaluation_deadline=QuantV6EvaluationDeadline(60),
        worker_count=4,
        memory_limit_mib=2_048,
    )

    assert actual == expected
    assert captured_count == 4
    assert concurrency[1] >= 2
    assert concurrency[1] <= 4
    assert provider.calls == 4
    assert provider.max_active == 1

    with pytest.raises(ValueError, match="worker_count must be between 2 and 4"):
        supervisor.evaluate_prepare_quant_v6_registration_spawn(
            registration=plan,
            provider=provider,
            evaluation_deadline=QuantV6EvaluationDeadline(60),
            worker_count=5,
            memory_limit_mib=2_048,
        )


@pytest.mark.parametrize("expected_event_count", (0, 60, 135))
def test_spawn_prepared_bytes_match_serial_exact_event_fixtures(
    expected_event_count: int,
) -> None:
    plan = _plan(1)
    bars = _exact_event_bars(expected_event_count)
    try:
        evaluations = evaluate_quant_v6_registration(
            registration=plan,
            provider=_TrackingProvider(bars),
        )
        assert evaluations[0].event_count == expected_event_count
        serial = _prepare_publication(
            plan=plan,
            evaluations=evaluations,
        )
        spawned = supervisor.evaluate_prepare_quant_v6_registration_spawn(
            registration=plan,
            provider=_TrackingProvider(bars),
            evaluation_deadline=QuantV6EvaluationDeadline(180),
            worker_count=4,
            memory_limit_mib=2_048,
        )

        assert spawned == serial
        assert spawned.identity_sha256 == serial.identity_sha256
        assert spawned.manifest_sha256 == serial.manifest_sha256
        assert spawned.publication_json.encode() == serial.publication_json.encode()
        assert spawned.bindings == serial.bindings
        assert tuple(
            artifact.payload for artifact in spawned.artifacts
        ) == tuple(artifact.payload for artifact in serial.artifacts)
        assert tuple(
            binding.artifact.payload for binding in spawned.bindings
        ) == tuple(
            binding.artifact.payload for binding in serial.bindings
        )
    finally:
        semantics_module._threshold_calculation.cache_clear()
        semantics_module._training_session_absolute_returns.cache_clear()


def test_minimum_ordinal_failure_waits_and_selectively_cancels_higher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = multiprocessing.get_context("spawn")
    high_ordinal_stats = context.Array("i", (0, 0, 0), lock=True)

    def _start(**kwargs: Any):
        return _spawn_custom_workers(
            **kwargs,
            target=_ordinal_failure_worker,
            extra_args=(high_ordinal_stats,),
        )

    monkeypatch.setattr(supervisor, "_start_workers", _start)

    with pytest.raises(
        QuantV6HistoricalEvaluationError,
        match="candidate ordinal 5: slow ordinal five failure",
    ) as caught:
        supervisor.evaluate_prepare_quant_v6_registration_spawn(
            registration=_plan(12),
            provider=_TrackingProvider(),
            evaluation_deadline=QuantV6EvaluationDeadline(60),
            worker_count=4,
            memory_limit_mib=2_048,
        )

    assert "ordinal 8" not in str(caught.value)
    assert high_ordinal_stats[0] >= 1
    assert high_ordinal_stats[1] == high_ordinal_stats[0]
    assert high_ordinal_stats[2] == high_ordinal_stats[0]


@pytest.mark.parametrize(
    ("stop_method", "expected_error"),
    (
        ("cancel", QuantV6EvaluationCancelledError),
        ("expire", QuantV6EvaluationDeadlineExceededError),
    ),
)
def test_external_cancel_and_deadline_stop_active_spawn_worker(
    monkeypatch: pytest.MonkeyPatch,
    stop_method: str,
    expected_error: type[RuntimeError],
) -> None:
    context = multiprocessing.get_context("spawn")
    active = context.Event()

    def _start(**kwargs: Any):
        return _spawn_custom_workers(
            **kwargs,
            target=_cooperative_hang_worker,
            extra_args=(active,),
        )

    monkeypatch.setattr(supervisor, "_start_workers", _start)
    deadline = QuantV6EvaluationDeadline(60)
    stop_sent = threading.Event()

    def _stop_when_active() -> None:
        if active.wait(10):
            getattr(deadline, stop_method)()
            stop_sent.set()

    stopper = threading.Thread(target=_stop_when_active)
    stopper.start()
    started_at = time.monotonic()
    try:
        with pytest.raises(expected_error):
            supervisor.evaluate_prepare_quant_v6_registration_spawn(
                registration=_plan(2),
                provider=_TrackingProvider(),
                evaluation_deadline=deadline,
                worker_count=2,
                memory_limit_mib=2_048,
            )
    finally:
        stopper.join(timeout=12)

    assert stop_sent.is_set()
    assert not stopper.is_alive()
    assert time.monotonic() - started_at < 12


def test_active_worker_crash_is_reported_and_cleaned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _start(**kwargs: Any):
        return _spawn_custom_workers(
            **kwargs,
            target=_crash_worker,
        )

    monkeypatch.setattr(supervisor, "_start_workers", _start)

    with pytest.raises(
        supervisor.QuantV6SpawnWorkerError,
        match="exit code 17",
    ):
        supervisor.evaluate_prepare_quant_v6_registration_spawn(
            registration=_plan(1),
            provider=_TrackingProvider(),
            evaluation_deadline=QuantV6EvaluationDeadline(30),
            worker_count=2,
            memory_limit_mib=2_048,
        )


def test_nth_worker_start_failure_hard_cleans_prior_workers_and_all_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NthStartFailure(RuntimeError):
        pass

    spawn_context = multiprocessing.get_context("spawn")
    connections: list[Connection] = []
    processes: list[Any] = []

    class _TrackedProcess:
        def __init__(self, process: Any, *, ordinal: int) -> None:
            self._process = process
            self._ordinal = ordinal
            self.events: list[tuple[str, float | None]] = []
            self.started_pid: int | None = None
            self.closed = False

        @property
        def pid(self) -> int | None:
            return self._process.pid

        @property
        def exitcode(self) -> int | None:
            return self._process.exitcode

        def start(self) -> None:
            if self._ordinal == 2:
                self.events.append(("start-failed", None))
                raise _NthStartFailure("third worker start failed")
            self.events.append(("start", None))
            self._process.start()
            self.started_pid = self._process.pid

        def is_alive(self) -> bool:
            return self._process.is_alive()

        def terminate(self) -> None:
            # Keep the real child alive through the terminate phase so the
            # regression also proves bounded escalation to kill.
            self.events.append(("terminate", None))

        def join(self, timeout: float | None = None) -> None:
            self.events.append(("join", timeout))
            self._process.join(timeout=timeout)

        def kill(self) -> None:
            self.events.append(("kill", None))
            self._process.kill()

        def close(self) -> None:
            self.events.append(("close", None))
            self._process.close()
            self.closed = True

    class _TrackingSpawnContext:
        @staticmethod
        def get_start_method() -> str:
            return "spawn"

        @staticmethod
        def Pipe(*, duplex: bool = True) -> tuple[Connection, Connection]:
            pair = cast(
                tuple[Connection, Connection],
                spawn_context.Pipe(duplex=duplex),
            )
            connections.extend(pair)
            return pair

        @staticmethod
        def Process(**kwargs: Any) -> _TrackedProcess:
            tracked = _TrackedProcess(
                spawn_context.Process(**kwargs),
                ordinal=len(processes),
            )
            processes.append(tracked)
            return tracked

    monkeypatch.setattr(
        supervisor.multiprocessing,
        "get_context",
        lambda method: _TrackingSpawnContext(),
    )
    monkeypatch.setattr(supervisor, "_TERMINATE_JOIN_SECONDS", 0.1)
    monkeypatch.setattr(supervisor, "_KILL_JOIN_SECONDS", 0.5)

    started_at = time.monotonic()
    try:
        with pytest.raises(
            _NthStartFailure,
            match="third worker start failed",
        ):
            supervisor._start_workers(
                worker_count=4,
                deadline_at=time.monotonic() + 30,
                max_frame_bytes=1024 * 1024,
            )
        elapsed = time.monotonic() - started_at

        assert len(processes) == 3
        assert len(connections) == 12
        assert all(connection.closed for connection in connections)
        assert processes[2].events == [("start-failed", None)]
        assert processes[2].started_pid is None

        started_pids = {
            process.started_pid
            for process in processes[:2]
            if process.started_pid is not None
        }
        assert len(started_pids) == 2
        assert started_pids.isdisjoint(
            child.pid for child in multiprocessing.active_children()
        )
        assert elapsed < 3.0

        for process in processes[:2]:
            actions = [action for action, _timeout in process.events]
            assert actions == [
                "start",
                "terminate",
                "join",
                "kill",
                "join",
                "close",
            ]
            join_timeouts = [
                timeout
                for action, timeout in process.events
                if action == "join"
            ]
            assert len(join_timeouts) == 2
            assert all(
                timeout is not None and 0 <= timeout <= 0.5
                for timeout in join_timeouts
            )
            assert process.closed is True
    finally:
        for connection in connections:
            connection.close()
        for tracked in processes:
            process = tracked._process
            try:
                if process.is_alive():
                    process.kill()
                    process.join(timeout=2)
                process.close()
            except ValueError:
                pass


def test_watchdog_constructor_failure_hard_cleans_started_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _WatchdogConstructorFailure(RuntimeError):
        pass

    captured_states: list[supervisor._WorkerState] = []
    original_start = supervisor._start_workers

    def _capture_start(**kwargs: Any) -> list[supervisor._WorkerState]:
        states = original_start(**kwargs)
        captured_states.extend(states)
        return states

    def _fail_watchdog(*_args: object, **_kwargs: object) -> object:
        raise _WatchdogConstructorFailure("watchdog constructor failed")

    monkeypatch.setattr(supervisor, "_start_workers", _capture_start)
    monkeypatch.setattr(supervisor, "_SpawnWatchdog", _fail_watchdog)

    try:
        with pytest.raises(
            _WatchdogConstructorFailure,
            match="watchdog constructor failed",
        ):
            supervisor.evaluate_prepare_quant_v6_registration_spawn(
                registration=_plan(1),
                provider=_TrackingProvider(),
                evaluation_deadline=QuantV6EvaluationDeadline(30),
                worker_count=2,
                memory_limit_mib=2_048,
            )

        assert len(captured_states) == 1
        for state in captured_states:
            assert state.connection.closed is True
            assert state.cancel_connection.closed is True
            with pytest.raises(ValueError, match="process object is closed"):
                state.process.is_alive()
    finally:
        for state in captured_states:
            try:
                if state.process.is_alive():
                    state.process.kill()
                    state.process.join(timeout=2)
                state.process.close()
            except ValueError:
                pass
            state.connection.close()
            state.cancel_connection.close()


@pytest.mark.skipif(os.name != "posix", reason="requires SemLock owner death")
def test_sibling_owner_death_cannot_poison_worker_cancellation() -> None:
    context = multiprocessing.get_context("spawn")
    result_reader, result_writer = context.Pipe(duplex=False)
    inner_pids = context.Array("i", (0, 0), lock=True)
    outer = context.Process(
        target=_run_sibling_owner_death_probe,
        args=(result_writer, inner_pids),
        name="quant-v6-sibling-owner-death-probe",
        daemon=False,
    )
    outer.start()
    result_writer.close()
    outer.join(timeout=10)
    if outer.is_alive():
        with inner_pids.get_lock():
            stalled_pids = (inner_pids[0], inner_pids[1])
        for stalled_pid in stalled_pids:
            if stalled_pid <= 0:
                continue
            try:
                os.kill(stalled_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        outer.join(timeout=2)
    if outer.is_alive():
        outer.kill()
        outer.join(timeout=2)
        result_reader.close()
        outer.close()
        pytest.fail("sibling owner death poisoned worker cancellation")

    try:
        assert outer.exitcode == 0
        assert result_reader.poll(2)
        result = result_reader.recv()
    finally:
        result_reader.close()
        outer.close()

    assert result["error_type"] == "QuantV6SpawnWorkerError"
    assert "candidate ordinal 1" in result["error_message"]
    assert "exit code 17" in result["error_message"]
    assert result["lower_completed"] == 1
    assert result["elapsed"] < 3


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal escalation")
@pytest.mark.parametrize(
    ("ignore_sigterm", "expected_signal"),
    ((False, signal.SIGTERM), (True, signal.SIGKILL)),
)
def test_hard_cleanup_terminates_then_kills_uncooperative_worker(
    monkeypatch: pytest.MonkeyPatch,
    ignore_sigterm: bool,
    expected_signal: signal.Signals,
) -> None:
    monkeypatch.setattr(supervisor, "_COOPERATIVE_STOP_SECONDS", 5.0)
    monkeypatch.setattr(supervisor, "_TERMINATE_JOIN_SECONDS", 0.25)
    monkeypatch.setattr(supervisor, "_KILL_JOIN_SECONDS", 0.5)
    deadline = QuantV6EvaluationDeadline(10)
    states = _spawn_custom_workers(
        worker_count=1,
        deadline_at=deadline.deadline_at,
        max_frame_bytes=supervisor._DEFAULT_MAX_FRAME_BYTES,
        target=_uncooperative_ready_worker,
        extra_args=(ignore_sigterm,),
    )
    supervisor._wait_for_ready(
        states,
        evaluation_deadline=deadline,
        parent_baseline_bytes=supervisor._resident_bytes(os.getpid()),
        memory_limit_bytes=2_048 * 1024 * 1024,
        max_frame_bytes=supervisor._DEFAULT_MAX_FRAME_BYTES,
    )
    process = states[0].process
    exitcodes: list[int | None] = []
    original_close = process.close

    def _recording_close() -> None:
        exitcodes.append(process.exitcode)
        original_close()

    monkeypatch.setattr(process, "close", _recording_close)
    started_at = time.monotonic()

    supervisor._stop_and_join_workers(
        states,
        normal=False,
        max_frame_bytes=supervisor._DEFAULT_MAX_FRAME_BYTES,
    )

    assert exitcodes == [-int(expected_signal)]
    assert time.monotonic() - started_at < 2


@pytest.mark.skipif(os.name != "posix", reason="requires SIGSTOP/SIGKILL")
def test_watchdog_breaks_blocked_large_work_send_at_absolute_deadline() -> None:
    context = multiprocessing.get_context("spawn")
    result_reader, result_writer = context.Pipe(duplex=False)
    inner_pid = context.Value("i", 0, lock=True)
    outer = context.Process(
        target=_run_sigstop_work_send_probe,
        args=(result_writer, inner_pid),
        name="quant-v6-watchdog-probe",
        daemon=False,
    )
    outer.start()
    result_writer.close()
    outer.join(timeout=12)
    if outer.is_alive():
        with inner_pid.get_lock():
            stalled_pid = inner_pid.value
        if stalled_pid > 0:
            try:
                os.kill(stalled_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        outer.join(timeout=2)
        if outer.is_alive():
            outer.kill()
            outer.join(timeout=2)
        result_reader.close()
        outer.close()
        pytest.fail("outer watchdog probe exceeded its hard test timeout")

    try:
        assert outer.exitcode == 0
        assert result_reader.poll(2)
        result = result_reader.recv()
    finally:
        result_reader.close()
        outer.close()

    assert result["work_send_started"] is True
    assert result["work_send_completed"] is False
    assert result["work_frame_bytes"] > 256 * 1024
    assert result["error_type"] == "QuantV6EvaluationDeadlineExceededError"
    assert "deadline exceeded" in result["error_message"]
    assert 2.5 <= result["elapsed"] < 8
    assert result["child_names"] == ()
    assert result["thread_names"] == ()


@pytest.mark.skipif(os.name != "posix", reason="requires SIGSTOP/SIGKILL")
def test_watchdog_breaks_blocked_partial_result_at_absolute_deadline() -> None:
    context = multiprocessing.get_context("spawn")
    result_reader, result_writer = context.Pipe(duplex=False)
    inner_pid = context.Value("i", 0, lock=True)
    outer = context.Process(
        target=_run_sigstop_result_receive_probe,
        args=(result_writer, inner_pid),
        name="quant-v6-result-watchdog-probe",
        daemon=False,
    )
    outer.start()
    result_writer.close()
    outer.join(timeout=12)
    if outer.is_alive():
        with inner_pid.get_lock():
            stalled_pid = inner_pid.value
        if stalled_pid > 0:
            try:
                os.kill(stalled_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        outer.join(timeout=2)
        if outer.is_alive():
            outer.kill()
            outer.join(timeout=2)
        result_reader.close()
        outer.close()
        pytest.fail("outer result watchdog probe exceeded its hard test timeout")

    try:
        assert outer.exitcode == 0
        assert result_reader.poll(2)
        result = result_reader.recv()
    finally:
        result_reader.close()
        outer.close()

    assert result["result_receive_started"] is True
    assert result["result_receive_completed"] is False
    assert result["error_type"] == "QuantV6EvaluationDeadlineExceededError"
    assert "deadline exceeded" in result["error_message"]
    assert 2.5 <= result["elapsed"] < 8
    assert result["child_names"] == ()
    assert result["thread_names"] == ()


def test_wire_frame_accepts_exact_boundary_and_rejects_one_byte_less() -> None:
    wire = supervisor._StopWire(
        protocol_version=supervisor._WIRE_PROTOCOL_VERSION,
    )
    payload = pickle.dumps(wire, protocol=5)

    assert supervisor._serialize_wire(
        wire,
        max_frame_bytes=len(payload),
    ) == payload
    with pytest.raises(
        supervisor.QuantV6SpawnProtocolError,
        match="exceeded the bounded frame size",
    ):
        supervisor._serialize_wire(
            wire,
            max_frame_bytes=len(payload) - 1,
        )


def test_parent_rss_failure_skips_cooperative_worker_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller_thread = threading.current_thread()
    workers_ready = False
    captured_states: list[supervisor._WorkerState] = []
    original_wait_for_ready = supervisor._wait_for_ready

    def _start(**kwargs: Any) -> list[supervisor._WorkerState]:
        states = _spawn_custom_workers(
            **kwargs,
            target=_uncooperative_ready_worker,
            extra_args=(False,),
        )
        captured_states.extend(states)
        return states

    def _wait_for_ready(*args: Any, **kwargs: Any) -> None:
        nonlocal workers_ready
        original_wait_for_ready(*args, **kwargs)
        workers_ready = True

    def _parent_resource_fence(*_args: Any, **_kwargs: Any) -> None:
        if workers_ready and threading.current_thread() is caller_thread:
            raise supervisor.QuantV6SpawnResourceLimitError(
                "parent RSS exceeded the hard budget"
            )

    monkeypatch.setattr(supervisor, "_start_workers", _start)
    monkeypatch.setattr(supervisor, "_wait_for_ready", _wait_for_ready)
    monkeypatch.setattr(supervisor, "_check_memory_budget", _parent_resource_fence)
    monkeypatch.setattr(supervisor, "_COOPERATIVE_STOP_SECONDS", 5.0)
    monkeypatch.setattr(supervisor, "_TERMINATE_JOIN_SECONDS", 0.5)
    monkeypatch.setattr(supervisor, "_KILL_JOIN_SECONDS", 0.5)
    started_at = time.monotonic()

    try:
        with pytest.raises(
            supervisor.QuantV6SpawnResourceLimitError,
            match="parent RSS exceeded the hard budget",
        ):
            supervisor.evaluate_prepare_quant_v6_registration_spawn(
                registration=_plan(1),
                provider=_TrackingProvider(),
                evaluation_deadline=QuantV6EvaluationDeadline(30),
                worker_count=2,
                memory_limit_mib=2_048,
            )

        assert workers_ready is True
        assert captured_states
        assert time.monotonic() - started_at < 3.0
    finally:
        for state in captured_states:
            try:
                if state.process.is_alive():
                    state.process.kill()
                    state.process.join(timeout=2)
                state.process.close()
            except ValueError:
                pass
            state.connection.close()
            state.cancel_connection.close()


def test_oversized_work_frame_fails_closed_without_process_leak() -> None:
    with pytest.raises(
        supervisor.QuantV6SpawnProtocolError,
        match="exceeded the bounded frame size",
    ):
        supervisor.evaluate_prepare_quant_v6_registration_spawn(
            registration=_plan(1),
            provider=_TrackingProvider(),
            evaluation_deadline=QuantV6EvaluationDeadline(30),
            worker_count=2,
            memory_limit_mib=2_048,
            max_frame_bytes=512,
        )


def test_rss_budget_accepts_exact_boundary_and_rejects_one_byte_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_pid = 987_654
    state = SimpleNamespace(
        process=SimpleNamespace(
            pid=worker_pid,
            is_alive=lambda: True,
        )
    )
    parent_pid = os.getpid()
    resident = {
        parent_pid: 150,
        worker_pid: 462,
    }
    # The hard budget is parent job growth (150 - 100) plus total worker RSS.
    monkeypatch.setattr(
        supervisor,
        "_resident_bytes",
        lambda pid: resident[pid],
    )
    states = cast(list[supervisor._WorkerState], [state])

    supervisor._check_memory_budget(
        states,
        parent_baseline_bytes=100,
        memory_limit_bytes=512,
    )

    resident[worker_pid] = 463
    with pytest.raises(
        supervisor.QuantV6SpawnResourceLimitError,
        match="exceeded its resident-memory budget",
    ):
        supervisor._check_memory_budget(
            states,
            parent_baseline_bytes=100,
            memory_limit_bytes=512,
        )


def test_rss_is_rechecked_after_final_success_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    success_received = False
    original_receive = supervisor._receive_wire

    def _track_receive(*args: Any, **kwargs: Any):
        nonlocal success_received
        value = original_receive(*args, **kwargs)
        if type(value) is supervisor._CandidateSuccessWire:
            success_received = True
        return value

    def _fail_after_success(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        if success_received:
            raise supervisor.QuantV6SpawnResourceLimitError(
                "final success frame exceeded resident-memory budget"
            )

    monkeypatch.setattr(supervisor, "_receive_wire", _track_receive)
    monkeypatch.setattr(supervisor, "_check_memory_budget", _fail_after_success)

    with pytest.raises(
        supervisor.QuantV6SpawnResourceLimitError,
        match="final success frame exceeded resident-memory budget",
    ):
        supervisor.evaluate_prepare_quant_v6_registration_spawn(
            registration=_plan(1),
            provider=_TrackingProvider(),
            evaluation_deadline=QuantV6EvaluationDeadline(30),
            worker_count=2,
            memory_limit_mib=2_048,
        )


def test_rss_fence_remains_active_during_final_parent_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assembly_started = False
    original_assemble = publication_module._assemble_prepared_publication

    def _track_assembly(*args: Any, **kwargs: Any):
        nonlocal assembly_started
        assembly_started = True
        return original_assemble(*args, **kwargs)

    def _fail_during_assembly(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        if assembly_started:
            raise supervisor.QuantV6SpawnResourceLimitError(
                "final parent assembly exceeded resident-memory budget"
            )

    monkeypatch.setattr(
        publication_module,
        "_assemble_prepared_publication",
        _track_assembly,
    )
    monkeypatch.setattr(
        supervisor,
        "_check_memory_budget",
        _fail_during_assembly,
    )

    with pytest.raises(
        supervisor.QuantV6SpawnResourceLimitError,
        match="final parent assembly exceeded resident-memory budget",
    ):
        supervisor.evaluate_prepare_quant_v6_registration_spawn(
            registration=_plan(1),
            provider=_TrackingProvider(),
            evaluation_deadline=QuantV6EvaluationDeadline(30),
            worker_count=2,
            memory_limit_mib=2_048,
        )

    assert assembly_started is True


_DB_SIZE_BUDGET_ENV = "AUTO_TRADE_WATCHLIST_QUANT_V6_DB_SIZE_BUDGET_MB"


def _database_snapshot(
    database_size_bytes: int | None,
) -> DatabaseHealthSnapshot:
    return DatabaseHealthSnapshot(
        checked_at=datetime.now(timezone.utc),
        dialect="sqlite",
        journal_mode="wal",
        page_size_bytes=4_096,
        page_count=None,
        freelist_count=None,
        used_page_count=None,
        database_size_bytes=database_size_bytes,
        free_space_bytes=None,
        wal_size_bytes=None,
    )


def test_database_size_fence_accepts_exact_budget_and_rejects_one_byte_over() -> None:
    budget_bytes = 512 * 1024 * 1024

    supervisor.QuantV6DatabaseSizeFence(
        database_size_bytes=budget_bytes,
        size_budget_bytes=budget_bytes,
    ).checkpoint()

    with pytest.raises(
        supervisor.QuantV6SpawnResourceLimitError,
        match="exceeded its database-size budget",
    ):
        supervisor.QuantV6DatabaseSizeFence(
            database_size_bytes=budget_bytes + 1,
            size_budget_bytes=budget_bytes,
        ).checkpoint()


@pytest.mark.parametrize("budget_mb", [511, 16_385, 4_096.0])
def test_database_size_fence_capture_rejects_out_of_bounds_budget(
    budget_mb: object,
) -> None:
    def _unexpected_session() -> object:
        pytest.fail("bounds validation opened a database session")

    with pytest.raises(ValueError, match="size_budget_mb"):
        supervisor.QuantV6DatabaseSizeFence.capture(
            size_budget_mb=cast(int, budget_mb),
            session_factory=cast(Any, _unexpected_session),
        )


def test_database_size_fence_capture_measures_bound_engine_read_only() -> None:
    engine = create_engine("sqlite://")
    session_factory = sessionmaker(bind=engine)

    fence = supervisor.QuantV6DatabaseSizeFence.capture(
        size_budget_mb=512,
        session_factory=cast(Any, session_factory),
    )

    assert fence.size_budget_bytes == 512 * 1024 * 1024
    assert 0 <= fence.database_size_bytes < fence.size_budget_bytes
    fence.checkpoint()


def test_database_size_fence_capture_fails_closed_when_size_is_unmeasurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        database_health_service,
        "snapshot_from_session",
        lambda _session: _database_snapshot(None),
    )
    engine = create_engine("sqlite://")
    session_factory = sessionmaker(bind=engine)

    with pytest.raises(ValueError, match="database size is unmeasurable"):
        supervisor.QuantV6DatabaseSizeFence.capture(
            size_budget_mb=512,
            session_factory=cast(Any, session_factory),
        )


def test_db_size_budget_setting_defaults_above_the_current_footprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_DB_SIZE_BUDGET_ENV, raising=False)

    assert Settings().watchlist_quant_v6_db_size_budget_mb == 4_096


@pytest.mark.parametrize("value", ["512", "16384"])
def test_db_size_budget_setting_accepts_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv(_DB_SIZE_BUDGET_ENV, value)

    assert Settings().watchlist_quant_v6_db_size_budget_mb == int(value)


@pytest.mark.parametrize("value", ["511", "16385"])
def test_db_size_budget_setting_rejects_out_of_bounds(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv(_DB_SIZE_BUDGET_ENV, value)

    with pytest.raises(ValidationError):
        Settings()
