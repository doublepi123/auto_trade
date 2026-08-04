from __future__ import annotations

import logging
import math
import multiprocessing
import os
import pickle
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from multiprocessing.connection import Connection
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from multiprocessing.process import BaseProcess

    from app.services.watchlist_quant_v6_deadline import (
        QuantV6EvaluationDeadline,
    )
    from app.services.watchlist_quant_v6_evaluation_service import (
        QuantV6HistoricalProvider,
        QuantV6RegistrationPlan,
        _CompletedCandidateFetch,
    )
    from app.services.watchlist_quant_v6_publication_service import (
        _PreparedCandidatePublication,
        _PreparedPublication,
    )


logger = logging.getLogger("auto_trade.watchlist_quant_v6_spawn_supervisor")


_WIRE_PROTOCOL_VERSION = 1
_DEFAULT_MAX_FRAME_BYTES = 128 * 1024 * 1024
_MAX_FAILURE_MESSAGE_BYTES = 2_048
_POLL_SECONDS = 0.05
_READY_TIMEOUT_SECONDS = 30.0
_COOPERATIVE_STOP_SECONDS = 1.0
_TERMINATE_JOIN_SECONDS = 1.0
_KILL_JOIN_SECONDS = 1.0
_WATCHDOG_TERMINATE_JOIN_SECONDS = 0.2
_WATCHDOG_KILL_JOIN_SECONDS = 1.0


class QuantV6SpawnSupervisorError(RuntimeError):
    """Base error for the bounded quant-v6 spawn supervisor."""


class QuantV6SpawnProtocolError(QuantV6SpawnSupervisorError):
    """Raised when one worker violates the bounded IPC contract."""


class QuantV6SpawnResourceLimitError(QuantV6SpawnSupervisorError):
    """Raised when the pipeline exceeds its resident-memory budget."""


class QuantV6SpawnWorkerError(QuantV6SpawnSupervisorError):
    """Raised when a worker exits outside a known domain error."""


def validate_quant_v6_spawn_provider(
    provider: QuantV6HistoricalProvider,
    *,
    evaluation_deadline: QuantV6EvaluationDeadline,
) -> None:
    """Require fetches that are bounded by the exact pipeline deadline."""
    capability = getattr(provider, "supports_quant_v6_spawn_fetch", None)
    if not callable(capability) or capability(
        evaluation_deadline=evaluation_deadline,
    ) is not True:
        raise ValueError(
            "spawn evaluation requires a historical provider with bounded "
            "fetches bound to the same evaluation_deadline"
        )


@dataclass(frozen=True)
class _ReadyWire:
    protocol_version: int
    worker_index: int
    pid: int


@dataclass(frozen=True)
class _CandidateWorkWire:
    protocol_version: int
    ordinal: int
    completed_fetch: _CompletedCandidateFetch


@dataclass(frozen=True)
class _StopWire:
    protocol_version: int


@dataclass(frozen=True)
class _CancelWire:
    protocol_version: int


@dataclass(frozen=True)
class _CandidateSuccessWire:
    protocol_version: int
    ordinal: int
    prepared: _PreparedCandidatePublication
    compute_ms: int
    closure_ms: int


_FailureKind = Literal[
    "CANCELLED",
    "DEADLINE",
    "EVALUATION",
    "PROVIDER",
    "PUBLICATION",
    "RESOURCE",
    "PROTOCOL",
    "UNEXPECTED",
]


@dataclass(frozen=True)
class _CandidateFailureWire:
    protocol_version: int
    ordinal: int | None
    kind: _FailureKind
    exception_type: str
    message: str


@dataclass
class _WorkerState:
    worker_index: int
    process: BaseProcess
    connection: Connection
    cancel_connection: Connection
    ready: bool = False
    active_ordinal: int | None = None
    cancel_sent: bool = False
    expected_exit: bool = False


@dataclass(frozen=True)
class QuantV6PipelineMemoryFence:
    """One parent baseline and hard RSS limit shared by the whole pipeline."""

    parent_baseline_bytes: int
    memory_limit_bytes: int

    def __post_init__(self) -> None:
        if (
            type(self.parent_baseline_bytes) is not int
            or self.parent_baseline_bytes < 0
            or type(self.memory_limit_bytes) is not int
            or self.memory_limit_bytes <= 0
        ):
            raise ValueError("pipeline memory fence values are invalid")

    @classmethod
    def capture(cls, *, memory_limit_mib: int) -> QuantV6PipelineMemoryFence:
        if (
            type(memory_limit_mib) is not int
            or not 512 <= memory_limit_mib <= 8_192
        ):
            raise ValueError("memory_limit_mib must be between 512 and 8192")
        return cls(
            parent_baseline_bytes=_resident_bytes(os.getpid()),
            memory_limit_bytes=memory_limit_mib * 1024 * 1024,
        )

    def checkpoint(
        self,
        states: Sequence[_WorkerState] = (),
    ) -> None:
        _check_memory_budget(
            states,
            parent_baseline_bytes=self.parent_baseline_bytes,
            memory_limit_bytes=self.memory_limit_bytes,
        )


class _SpawnWatchdog:
    """Hard-stop workers when the shared deadline or RSS fence wins."""

    def __init__(
        self,
        states: list[_WorkerState],
        *,
        evaluation_deadline: QuantV6EvaluationDeadline,
        parent_baseline_bytes: int,
        memory_limit_bytes: int,
    ) -> None:
        self._states = states
        self._evaluation_deadline = evaluation_deadline
        self._parent_baseline_bytes = parent_baseline_bytes
        self._memory_limit_bytes = memory_limit_bytes
        self._stop_event = threading.Event()
        self._failure_lock = threading.Lock()
        self._failure: RuntimeError | None = None
        self._started = False
        self._thread = threading.Thread(
            target=self._run,
            name="quant-v6-watchdog",
            daemon=False,
        )

    def start(self) -> None:
        if self._started:
            raise QuantV6SpawnSupervisorError(
                "quant-v6 watchdog cannot be started twice"
            )
        self._thread.start()
        self._started = True

    def failure(self) -> RuntimeError | None:
        with self._failure_lock:
            return self._failure

    def raise_if_failed(
        self,
        *,
        cause: BaseException | None = None,
    ) -> None:
        failure = self.failure()
        if failure is None:
            return
        if cause is None or cause is failure:
            raise failure
        raise failure from cause

    def stop_and_join(self) -> None:
        if not self._started:
            return
        self._stop_event.set()
        # Every operation in ``_run`` has a hard bound. A strong join ensures
        # no watchdog thread is abandoned into a later evaluation tick.
        self._thread.join()

    def _record_failure(self, failure: RuntimeError) -> None:
        with self._failure_lock:
            if self._failure is None:
                self._failure = failure

    def _hard_stop_workers(self) -> None:
        for state in self._states:
            try:
                if state.process.is_alive():
                    state.process.terminate()
            except (AssertionError, OSError, ValueError):
                logger.exception(
                    "quant-v6 watchdog could not terminate worker %d",
                    state.worker_index,
                )
        terminate_until = (
            time.monotonic() + _WATCHDOG_TERMINATE_JOIN_SECONDS
        )
        for state in self._states:
            try:
                state.process.join(
                    timeout=max(0.0, terminate_until - time.monotonic())
                )
            except (AssertionError, ValueError):
                logger.exception(
                    "quant-v6 watchdog could not join terminated worker %d",
                    state.worker_index,
                )
        for state in self._states:
            try:
                if state.process.is_alive():
                    state.process.kill()
            except (AssertionError, OSError, ValueError):
                logger.exception(
                    "quant-v6 watchdog could not kill worker %d",
                    state.worker_index,
                )
        kill_until = time.monotonic() + _WATCHDOG_KILL_JOIN_SECONDS
        for state in self._states:
            try:
                state.process.join(
                    timeout=max(0.0, kill_until - time.monotonic())
                )
            except (AssertionError, ValueError):
                logger.exception(
                    "quant-v6 watchdog could not join killed worker %d",
                    state.worker_index,
                )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            failure: RuntimeError | None = None
            try:
                # Preserve the deadline object's exact forced-timeout versus
                # operator-cancel classification before inspecting resources.
                self._evaluation_deadline.checkpoint()
                _check_memory_budget(
                    self._states,
                    parent_baseline_bytes=self._parent_baseline_bytes,
                    memory_limit_bytes=self._memory_limit_bytes,
                )
            except RuntimeError as exc:
                failure = exc
            except BaseException as exc:
                failure = QuantV6SpawnWorkerError(
                    "quant-v6 watchdog failed while enforcing lifecycle fences: "
                    f"{type(exc).__name__}"
                )
                failure.__cause__ = exc
            if failure is not None:
                self._record_failure(failure)
                self._hard_stop_workers()
                return
            self._stop_event.wait(_POLL_SECONDS)


class _ChildStopSignal:
    """Expose one per-worker selective-cancel pipe as a deadline stop token."""

    def __init__(
        self,
        selective_cancel: Connection,
        *,
        max_frame_bytes: int,
    ) -> None:
        self._selective_cancel = selective_cancel
        self._max_frame_bytes = max_frame_bytes
        self._selective_stopped = False

    def _poll_selective(self) -> None:
        if self._selective_stopped:
            return
        try:
            if not self._selective_cancel.poll():
                return
            value = _receive_wire(
                self._selective_cancel,
                max_frame_bytes=self._max_frame_bytes,
            )
        except (EOFError, OSError):
            self._selective_stopped = True
            return
        if (
            type(value) is not _CancelWire
            or value.protocol_version != _WIRE_PROTOCOL_VERSION
        ):
            raise QuantV6SpawnProtocolError(
                "quant-v6 worker received an invalid selective-cancel frame"
            )
        self._selective_stopped = True

    def is_set(self) -> bool:
        self._poll_selective()
        return self._selective_stopped

    def set(self) -> None:
        self._selective_stopped = True

    def wait(self, timeout: float | None = None) -> bool:
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or float(timeout) < 0
        ):
            raise ValueError("timeout must be finite and non-negative")
        if self.is_set():
            return True
        if timeout is None:
            while not self.is_set():
                time.sleep(_POLL_SECONDS)
            return True
        stop_at = time.monotonic() + float(timeout)
        while True:
            remaining = stop_at - time.monotonic()
            if remaining <= 0:
                return self.is_set()
            time.sleep(min(_POLL_SECONDS, remaining))
            if self.is_set():
                return True


def _serialize_wire(value: object, *, max_frame_bytes: int) -> bytes:
    try:
        payload = pickle.dumps(value, protocol=5)
    except (pickle.PickleError, TypeError, ValueError, AttributeError) as exc:
        raise QuantV6SpawnProtocolError(
            "quant-v6 worker wire could not be serialized"
        ) from exc
    if len(payload) > max_frame_bytes:
        raise QuantV6SpawnProtocolError(
            "quant-v6 worker wire exceeded the bounded frame size"
        )
    return payload


def _send_wire(
    connection: Connection,
    value: object,
    *,
    max_frame_bytes: int,
) -> None:
    connection.send_bytes(
        _serialize_wire(value, max_frame_bytes=max_frame_bytes)
    )


def _receive_wire(
    connection: Connection,
    *,
    max_frame_bytes: int,
) -> object:
    try:
        payload = connection.recv_bytes(maxlength=max_frame_bytes)
    except (EOFError, OSError) as exc:
        raise QuantV6SpawnProtocolError(
            "quant-v6 worker wire closed or exceeded its frame limit"
        ) from exc
    try:
        return pickle.loads(payload)
    except (
        pickle.PickleError,
        TypeError,
        ValueError,
        AttributeError,
        EOFError,
    ) as exc:
        raise QuantV6SpawnProtocolError(
            "quant-v6 worker wire payload is invalid"
        ) from exc


def _bounded_failure_message(exc: BaseException) -> str:
    value = str(exc)
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_FAILURE_MESSAGE_BYTES:
        return value
    return encoded[:_MAX_FAILURE_MESSAGE_BYTES].decode(
        "utf-8",
        errors="ignore",
    )


def _failure_wire(
    *,
    ordinal: int | None,
    kind: _FailureKind,
    exc: BaseException,
) -> _CandidateFailureWire:
    return _CandidateFailureWire(
        protocol_version=_WIRE_PROTOCOL_VERSION,
        ordinal=ordinal,
        kind=kind,
        exception_type=type(exc).__name__,
        message=_bounded_failure_message(exc),
    )


def _safe_send_failure(
    connection: Connection,
    failure: _CandidateFailureWire,
    *,
    max_frame_bytes: int,
) -> None:
    try:
        _send_wire(
            connection,
            failure,
            max_frame_bytes=max_frame_bytes,
        )
    except BaseException:
        return


def _classify_worker_failure(exc: BaseException) -> _FailureKind:
    name = type(exc).__name__
    if name == "QuantV6EvaluationDeadlineExceededError":
        return "DEADLINE"
    if name == "QuantV6EvaluationCancelledError":
        return "CANCELLED"
    if name == "QuantV6HistoricalEvaluationError":
        return "EVALUATION"
    if name in {"QuantV6PublicationError", "QuantV6PublicationConflictError"}:
        return "PUBLICATION"
    if isinstance(exc, QuantV6SpawnResourceLimitError):
        return "RESOURCE"
    if isinstance(exc, QuantV6SpawnProtocolError):
        return "PROTOCOL"
    return "UNEXPECTED"


def _classify_fetch_failure(exc: BaseException) -> _FailureKind:
    name = type(exc).__name__
    if name == "QuantV6EvaluationDeadlineExceededError":
        return "DEADLINE"
    if name == "QuantV6EvaluationCancelledError":
        return "CANCELLED"
    if isinstance(exc, QuantV6SpawnResourceLimitError):
        return "RESOURCE"
    if isinstance(exc, QuantV6SpawnProtocolError):
        return "PROTOCOL"
    if name == "QuantV6HistoricalProviderError":
        return "PROVIDER"
    return "EVALUATION"


def _quant_v6_spawn_worker_main(
    connection: Connection,
    selective_cancel_reader: Connection,
    deadline_at: float,
    worker_index: int,
    max_frame_bytes: int,
) -> None:
    active_ordinal: int | None = None
    try:
        from app.services.watchlist_quant_v6_deadline import (
            QuantV6EvaluationDeadline,
        )
        from app.services.watchlist_quant_v6_evaluation_service import (
            _CompletedCandidateFetch,
            _evaluate_completed_candidate_with_logging,
            validate_quant_v6_registration_plan,
        )
        from app.services.watchlist_quant_v6_publication_service import (
            _prepare_candidate_publication,
        )
    except BaseException as exc:
        _safe_send_failure(
            connection,
            _failure_wire(ordinal=None, kind="UNEXPECTED", exc=exc),
            max_frame_bytes=max_frame_bytes,
        )
        connection.close()
        selective_cancel_reader.close()
        return

    child_stop = _ChildStopSignal(
        selective_cancel_reader,
        max_frame_bytes=max_frame_bytes,
    )
    try:
        deadline = QuantV6EvaluationDeadline.from_deadline_at(
            deadline_at,
            cancel_event=child_stop,
        )
        _send_wire(
            connection,
            _ReadyWire(
                protocol_version=_WIRE_PROTOCOL_VERSION,
                worker_index=worker_index,
                pid=os.getpid(),
            ),
            max_frame_bytes=max_frame_bytes,
        )
        while True:
            deadline.checkpoint()
            if not connection.poll(_POLL_SECONDS):
                continue
            value = _receive_wire(
                connection,
                max_frame_bytes=max_frame_bytes,
            )
            if type(value) is _StopWire:
                if value.protocol_version != _WIRE_PROTOCOL_VERSION:
                    raise QuantV6SpawnProtocolError(
                        "quant-v6 worker received an invalid stop version"
                    )
                return
            if (
                type(value) is not _CandidateWorkWire
                or value.protocol_version != _WIRE_PROTOCOL_VERSION
                or type(value.ordinal) is not int
                or value.ordinal < 0
                or type(value.completed_fetch) is not _CompletedCandidateFetch
            ):
                raise QuantV6SpawnProtocolError(
                    "quant-v6 worker received an invalid work frame"
                )
            active_ordinal = value.ordinal
            completed_fetch = value.completed_fetch
            registration = completed_fetch.request.registration
            member = completed_fetch.request.member
            if member.ordinal != active_ordinal:
                raise QuantV6SpawnProtocolError(
                    "quant-v6 worker ordinal conflicts with fetched evidence"
                )
            deadline.checkpoint()
            validate_quant_v6_registration_plan(registration)
            compute_started_at = time.monotonic_ns()
            evaluation = _evaluate_completed_candidate_with_logging(
                completed_fetch=completed_fetch,
                total=len(registration.members),
                evaluation_deadline=deadline,
            )
            compute_ms = max(
                0,
                time.monotonic_ns() - compute_started_at,
            ) // 1_000_000
            deadline.checkpoint()
            closure_started_at = time.monotonic_ns()
            prepared = _prepare_candidate_publication(
                plan=registration,
                evaluation=evaluation,
                evaluation_deadline=deadline,
            )
            closure_ms = max(
                0,
                time.monotonic_ns() - closure_started_at,
            ) // 1_000_000
            deadline.checkpoint()
            _send_wire(
                connection,
                _CandidateSuccessWire(
                    protocol_version=_WIRE_PROTOCOL_VERSION,
                    ordinal=active_ordinal,
                    prepared=prepared,
                    compute_ms=compute_ms,
                    closure_ms=closure_ms,
                ),
                max_frame_bytes=max_frame_bytes,
            )
            active_ordinal = None
            # Do not retain the just-sent fetch/evaluation/artifact graph while
            # this reusable worker is idle or computing a later candidate.
            del value, completed_fetch, registration, member, evaluation, prepared
    except BaseException as exc:
        _safe_send_failure(
            connection,
            _failure_wire(
                ordinal=active_ordinal,
                kind=_classify_worker_failure(exc),
                exc=exc,
            ),
            max_frame_bytes=max_frame_bytes,
        )
    finally:
        connection.close()
        selective_cancel_reader.close()


def _resident_bytes(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/statm", encoding="utf-8") as handle:
            fields = handle.read().split()
        if len(fields) < 2:
            raise ValueError("statm is incomplete")
        resident_pages = int(fields[1])
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError, TypeError) as exc:
        raise QuantV6SpawnResourceLimitError(
            f"cannot inspect resident memory for pid {pid}"
        ) from exc
    if resident_pages < 0 or page_size <= 0:
        raise QuantV6SpawnResourceLimitError(
            f"resident memory accounting is invalid for pid {pid}"
        )
    return resident_pages * page_size


def _check_memory_budget(
    states: Sequence[_WorkerState],
    *,
    parent_baseline_bytes: int,
    memory_limit_bytes: int,
) -> None:
    parent_increment = max(
        0,
        _resident_bytes(os.getpid()) - parent_baseline_bytes,
    )
    worker_bytes = 0
    for state in states:
        pid = state.process.pid
        if pid is None or not state.process.is_alive():
            continue
        try:
            worker_bytes += _resident_bytes(pid)
        except QuantV6SpawnResourceLimitError:
            # A worker can exit between ``is_alive`` and opening /proc. Let the
            # lifecycle pass classify that exit with its candidate ordinal.
            if not state.process.is_alive():
                continue
            raise
    if parent_increment + worker_bytes > memory_limit_bytes:
        raise QuantV6SpawnResourceLimitError(
            "quant-v6 spawn pipeline exceeded its resident-memory budget"
        )


def _start_workers(
    *,
    worker_count: int,
    deadline_at: float,
    max_frame_bytes: int,
) -> list[_WorkerState]:
    context = multiprocessing.get_context("spawn")
    if context.get_start_method() != "spawn":
        raise QuantV6SpawnSupervisorError(
            "quant-v6 workers require the spawn start method"
        )
    states: list[_WorkerState] = []
    pending_connections: tuple[Connection, Connection, Connection, Connection] | None = None
    try:
        for worker_index in range(worker_count):
            parent_pipe, child_pipe = context.Pipe(duplex=True)
            cancel_reader_pipe, cancel_writer_pipe = context.Pipe(duplex=False)
            parent_connection = cast(Connection, parent_pipe)
            child_connection = cast(Connection, child_pipe)
            cancel_reader = cast(Connection, cancel_reader_pipe)
            cancel_writer = cast(Connection, cancel_writer_pipe)
            pending_connections = (
                parent_connection,
                child_connection,
                cancel_reader,
                cancel_writer,
            )
            process = context.Process(
                target=_quant_v6_spawn_worker_main,
                args=(
                    child_connection,
                    cancel_reader,
                    deadline_at,
                    worker_index,
                    max_frame_bytes,
                ),
                name=f"quant-v6-compute-{worker_index}",
                daemon=False,
            )
            process.start()
            states.append(_WorkerState(
                worker_index=worker_index,
                process=process,
                connection=parent_connection,
                cancel_connection=cancel_writer,
            ))
            child_connection.close()
            cancel_reader.close()
            pending_connections = None
        return states
    except BaseException:
        if pending_connections is not None:
            for connection in pending_connections:
                connection.close()
        if states:
            try:
                _stop_and_join_workers(
                    states,
                    normal=False,
                    max_frame_bytes=max_frame_bytes,
                )
            except BaseException:
                logger.exception(
                    "quant-v6 worker startup cleanup failed"
                )
        raise


def _send_selective_cancel(
    state: _WorkerState,
    *,
    max_frame_bytes: int,
    watchdog: _SpawnWatchdog | None = None,
) -> None:
    if state.cancel_sent:
        return
    state.cancel_sent = True
    try:
        _send_wire(
            state.cancel_connection,
            _CancelWire(protocol_version=_WIRE_PROTOCOL_VERSION),
            max_frame_bytes=max_frame_bytes,
        )
    except (
        BrokenPipeError,
        EOFError,
        OSError,
        QuantV6SpawnProtocolError,
    ) as exc:
        if watchdog is not None:
            watchdog.raise_if_failed(cause=exc)
        return


def _stop_and_join_workers(
    states: list[_WorkerState],
    *,
    normal: bool,
    max_frame_bytes: int,
) -> None:
    if normal:
        for state in states:
            if not state.process.is_alive():
                continue
            try:
                _send_wire(
                    state.connection,
                    _StopWire(protocol_version=_WIRE_PROTOCOL_VERSION),
                    max_frame_bytes=max_frame_bytes,
                )
            except (
                BrokenPipeError,
                EOFError,
                OSError,
                QuantV6SpawnProtocolError,
            ) as exc:
                logger.debug(
                    "quant-v6 cooperative stop could not be delivered to "
                    "worker_index=%d: %s",
                    state.worker_index,
                    exc,
                )
    # Abnormal cleanup deliberately sends no cooperative frames. A wedged child
    # can block in IPC or die while owning an unrelated process-shared primitive;
    # only bounded OS process-handle operations run below before pipes close.
    if normal:
        cooperative_until = time.monotonic() + _COOPERATIVE_STOP_SECONDS
        for state in states:
            remaining = max(0.0, cooperative_until - time.monotonic())
            state.process.join(timeout=remaining)
    for state in states:
        if state.process.is_alive():
            state.process.terminate()
    terminate_until = time.monotonic() + _TERMINATE_JOIN_SECONDS
    for state in states:
        remaining = max(0.0, terminate_until - time.monotonic())
        state.process.join(timeout=remaining)
    for state in states:
        if state.process.is_alive():
            state.process.kill()
    kill_until = time.monotonic() + _KILL_JOIN_SECONDS
    for state in states:
        remaining = max(0.0, kill_until - time.monotonic())
        state.process.join(timeout=remaining)
    alive = [
        state.process.pid
        for state in states
        if state.process.is_alive()
    ]
    for state in states:
        state.connection.close()
        state.cancel_connection.close()
    if alive:
        raise QuantV6SpawnWorkerError(
            f"quant-v6 workers survived hard cleanup: {alive}"
        )
    failed = (
        [
            (state.process.pid, state.process.exitcode)
            for state in states
            if state.process.exitcode != 0
        ]
        if normal
        else []
    )
    for state in states:
        state.process.close()
    if failed:
        raise QuantV6SpawnWorkerError(
            f"quant-v6 workers exited abnormally: {failed}"
        )


def _rebuild_failure(failure: _CandidateFailureWire) -> RuntimeError:
    from app.services.watchlist_quant_v6_deadline import (
        QuantV6EvaluationCancelledError,
        QuantV6EvaluationDeadlineExceededError,
    )
    from app.services.watchlist_quant_v6_evaluation_service import (
        QuantV6HistoricalEvaluationError,
    )
    from app.services.watchlist_quant_v6_historical_provider import (
        QuantV6HistoricalProviderError,
    )
    from app.services.watchlist_quant_v6_publication_service import (
        QuantV6PublicationError,
    )

    prefix = (
        "quant-v6 worker"
        if failure.ordinal is None
        else f"quant-v6 candidate ordinal {failure.ordinal}"
    )
    message = f"{prefix}: {failure.message or failure.exception_type}"
    if failure.kind == "DEADLINE":
        return QuantV6EvaluationDeadlineExceededError(message)
    if failure.kind == "CANCELLED":
        return QuantV6EvaluationCancelledError(message)
    if failure.kind == "EVALUATION":
        return QuantV6HistoricalEvaluationError(message)
    if failure.kind == "PROVIDER":
        return QuantV6HistoricalProviderError(message)
    if failure.kind == "PUBLICATION":
        return QuantV6PublicationError(message)
    if failure.kind == "RESOURCE":
        return QuantV6SpawnResourceLimitError(message)
    if failure.kind == "PROTOCOL":
        return QuantV6SpawnProtocolError(message)
    return QuantV6SpawnWorkerError(message)


def _wait_for_ready(
    states: list[_WorkerState],
    *,
    evaluation_deadline: QuantV6EvaluationDeadline,
    parent_baseline_bytes: int,
    memory_limit_bytes: int,
    max_frame_bytes: int,
    watchdog: _SpawnWatchdog | None = None,
) -> None:
    ready_until = min(
        evaluation_deadline.deadline_at,
        time.monotonic() + _READY_TIMEOUT_SECONDS,
    )
    while not all(state.ready for state in states):
        if watchdog is not None:
            watchdog.raise_if_failed()
        evaluation_deadline.checkpoint()
        _check_memory_budget(
            states,
            parent_baseline_bytes=parent_baseline_bytes,
            memory_limit_bytes=memory_limit_bytes,
        )
        for state in states:
            if state.ready:
                continue
            try:
                readable = state.connection.poll()
            except (OSError, ValueError) as exc:
                if watchdog is not None:
                    watchdog.raise_if_failed(cause=exc)
                raise QuantV6SpawnProtocolError(
                    "quant-v6 worker ready pipe could not be polled"
                ) from exc
            if readable:
                try:
                    value = _receive_wire(
                        state.connection,
                        max_frame_bytes=max_frame_bytes,
                    )
                except QuantV6SpawnProtocolError as exc:
                    if watchdog is not None:
                        watchdog.raise_if_failed(cause=exc)
                    if isinstance(exc.__cause__, EOFError):
                        raise QuantV6SpawnWorkerError(
                            f"quant-v6 worker {state.worker_index} exited "
                            "before ready"
                        ) from exc
                    raise
                if watchdog is not None:
                    watchdog.raise_if_failed()
                if type(value) is _CandidateFailureWire:
                    raise _rebuild_failure(value)
                if (
                    type(value) is not _ReadyWire
                    or value.protocol_version != _WIRE_PROTOCOL_VERSION
                    or value.worker_index != state.worker_index
                    or value.pid != state.process.pid
                ):
                    raise QuantV6SpawnProtocolError(
                        "quant-v6 worker returned an invalid ready frame"
                    )
                state.ready = True
            if not state.process.is_alive() and not state.ready:
                raise QuantV6SpawnWorkerError(
                    f"quant-v6 worker {state.worker_index} exited before ready"
                )
        if time.monotonic() >= ready_until:
            # Preserve the deadline branch when the absolute deadline, rather
            # than the independent startup cap, is what ended readiness.
            evaluation_deadline.checkpoint()
            raise QuantV6SpawnWorkerError(
                "quant-v6 workers did not become ready within the bounded startup"
            )
        time.sleep(_POLL_SECONDS)


def _ordinary_failure(
    current: _CandidateFailureWire | None,
    candidate: _CandidateFailureWire,
) -> _CandidateFailureWire:
    if candidate.ordinal is None:
        raise QuantV6SpawnProtocolError(
            "ordinary candidate failure is missing its ordinal"
        )
    if current is None:
        return candidate
    assert current.ordinal is not None
    return candidate if candidate.ordinal < current.ordinal else current


def _drain_fetch(
    future: Future[_CompletedCandidateFetch] | None,
    *,
    evaluation_deadline: QuantV6EvaluationDeadline,
) -> None:
    if future is None or future.done() or future.cancel():
        return
    # Entry validation guarantees that an accepted provider binds every fetch
    # wait to this exact deadline. Keep strong cleanup here: abandoning a live
    # ThreadPoolExecutor worker would leak one thread on every failed retry.
    evaluation_deadline.cancel()
    while not future.done():
        time.sleep(_POLL_SECONDS)
    try:
        future.result()
    except BaseException:
        return


def evaluate_prepare_quant_v6_registration_spawn(
    *,
    registration: QuantV6RegistrationPlan,
    provider: QuantV6HistoricalProvider,
    evaluation_deadline: QuantV6EvaluationDeadline,
    worker_count: int,
    memory_limit_mib: int,
    max_frame_bytes: int = _DEFAULT_MAX_FRAME_BYTES,
    memory_fence: QuantV6PipelineMemoryFence | None = None,
) -> _PreparedPublication:
    """Fetch serially with a bounded provider, then prepare in spawn workers."""
    if type(worker_count) is not int or not 2 <= worker_count <= 4:
        raise ValueError("worker_count must be between 2 and 4")
    if (
        type(memory_limit_mib) is not int
        or not 512 <= memory_limit_mib <= 8_192
    ):
        raise ValueError("memory_limit_mib must be between 512 and 8192")
    if (
        type(max_frame_bytes) is not int
        or max_frame_bytes <= 0
        or max_frame_bytes > _DEFAULT_MAX_FRAME_BYTES
    ):
        raise ValueError("max_frame_bytes is outside the supported bound")
    validate_quant_v6_spawn_provider(
        provider,
        evaluation_deadline=evaluation_deadline,
    )

    from app.services import watchlist_quant_v6_evaluation_service as evaluation_module
    from app.services.watchlist_quant_v6_publication_service import (
        _PreparedCandidatePublication,
        _assemble_prepared_publication,
        _pipeline_evaluation_checkpoint,
    )

    evaluation_deadline.checkpoint()
    evaluation_module.validate_quant_v6_registration_plan(registration)
    total = len(registration.members)
    if total < 1:
        raise QuantV6SpawnProtocolError(
            "quant-v6 registration has no members"
        )
    memory_limit_bytes = memory_limit_mib * 1024 * 1024
    pipeline_memory_fence = memory_fence or QuantV6PipelineMemoryFence.capture(
        memory_limit_mib=memory_limit_mib,
    )
    if pipeline_memory_fence.memory_limit_bytes != memory_limit_bytes:
        raise ValueError(
            "memory_fence conflicts with the requested memory_limit_mib"
        )
    parent_baseline_bytes = pipeline_memory_fence.parent_baseline_bytes
    executor: ThreadPoolExecutor | None = None
    pending_fetch: Future[_CompletedCandidateFetch] | None = None
    pending_ordinal: int | None = None
    fetched_ready: _CompletedCandidateFetch | None = None
    next_fetch_ordinal = 0
    prepared_by_ordinal: dict[int, _PreparedCandidatePublication] = {}
    best_failure: _CandidateFailureWire | None = None
    global_failure: RuntimeError | None = None
    workers_stopped = False
    watchdog: _SpawnWatchdog | None = None
    watchdog_stopped = False
    states: list[_WorkerState] = []
    try:
        states = _start_workers(
            worker_count=min(worker_count, total),
            deadline_at=evaluation_deadline.deadline_at,
            max_frame_bytes=max_frame_bytes,
        )
        active_watchdog = _SpawnWatchdog(
            states,
            evaluation_deadline=evaluation_deadline,
            parent_baseline_bytes=parent_baseline_bytes,
            memory_limit_bytes=memory_limit_bytes,
        )
        watchdog = active_watchdog
        active_watchdog.start()
        _wait_for_ready(
            states,
            evaluation_deadline=evaluation_deadline,
            parent_baseline_bytes=parent_baseline_bytes,
            memory_limit_bytes=memory_limit_bytes,
            max_frame_bytes=max_frame_bytes,
            watchdog=active_watchdog,
        )
        executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="quant-v6-prefetch",
        )

        def submit_fetch(ordinal: int) -> Future[_CompletedCandidateFetch]:
            evaluation_deadline.checkpoint()
            evaluation_module.validate_quant_v6_registration_plan(registration)
            member = registration.members[ordinal]
            request = (
                evaluation_module._verified_candidate_fetch_request_from_validated_registration(
                    registration=registration,
                    member=member,
                )
            )
            evaluation_module._log_candidate_start(request, total=total)
            assert executor is not None
            return executor.submit(
                evaluation_module._fetch_quant_v6_candidate,
                request=request,
                provider=provider,
            )

        pending_ordinal = 0
        pending_fetch = submit_fetch(0)
        next_fetch_ordinal = 1

        while len(prepared_by_ordinal) < total:
            try:
                active_watchdog.raise_if_failed()
                evaluation_deadline.checkpoint()
                _check_memory_budget(
                    states,
                    parent_baseline_bytes=parent_baseline_bytes,
                    memory_limit_bytes=memory_limit_bytes,
                )
            except RuntimeError as exc:
                global_failure = exc
                break

            for state in states:
                # A worker exits after its terminal failure frame. Its pipe can
                # remain poll-readable solely because EOF is pending; the
                # failure was already consumed and must not be reclassified as
                # a later idle-worker crash.
                if state.expected_exit:
                    continue
                try:
                    readable = state.connection.poll()
                except (OSError, ValueError) as exc:
                    active_watchdog.raise_if_failed(cause=exc)
                    raise QuantV6SpawnProtocolError(
                        "quant-v6 worker result pipe could not be polled"
                    ) from exc
                if not readable:
                    continue
                try:
                    value = _receive_wire(
                        state.connection,
                        max_frame_bytes=max_frame_bytes,
                    )
                except QuantV6SpawnProtocolError as exc:
                    active_watchdog.raise_if_failed(cause=exc)
                    if isinstance(exc.__cause__, EOFError):
                        if state.active_ordinal is None:
                            global_failure = QuantV6SpawnWorkerError(
                                f"quant-v6 idle worker "
                                f"{state.worker_index} exited"
                            )
                            break
                        failure = _CandidateFailureWire(
                            protocol_version=_WIRE_PROTOCOL_VERSION,
                            ordinal=state.active_ordinal,
                            kind="UNEXPECTED",
                            exception_type="WorkerExit",
                            message=(
                                "worker pipe closed with exit code "
                                f"{state.process.exitcode}"
                            ),
                        )
                        state.expected_exit = True
                        state.active_ordinal = None
                        best_failure = _ordinary_failure(
                            best_failure,
                            failure,
                        )
                        continue
                    global_failure = exc
                    break
                active_watchdog.raise_if_failed()
                if type(value) is _CandidateSuccessWire:
                    if (
                        value.protocol_version != _WIRE_PROTOCOL_VERSION
                        or state.active_ordinal != value.ordinal
                        or type(value.compute_ms) is not int
                        or value.compute_ms < 0
                        or type(value.closure_ms) is not int
                        or value.closure_ms < 0
                        or type(value.prepared)
                        is not _PreparedCandidatePublication
                    ):
                        global_failure = QuantV6SpawnProtocolError(
                            "quant-v6 worker returned an invalid success frame"
                        )
                        break
                    evaluation_module.validate_quant_v6_registration_plan(
                        registration
                    )
                    expected_member = registration.members[value.ordinal]
                    if (
                        value.prepared.registration_identity_sha256
                        != registration.identity_sha256
                        or value.prepared.member.canonical_payload()
                        != expected_member.canonical_payload()
                        or value.ordinal in prepared_by_ordinal
                    ):
                        global_failure = QuantV6SpawnProtocolError(
                            "quant-v6 worker result conflicts with registration"
                        )
                        break
                    prepared_by_ordinal[value.ordinal] = value.prepared
                    logger.info(
                        "quant-v6 spawn candidate ordinal=%d completed=%d/%d "
                        "compute_ms=%d closure_ms=%d",
                        value.ordinal,
                        len(prepared_by_ordinal),
                        total,
                        value.compute_ms,
                        value.closure_ms,
                    )
                    state.active_ordinal = None
                    state.cancel_sent = False
                elif type(value) is _CandidateFailureWire:
                    if (
                        value.protocol_version != _WIRE_PROTOCOL_VERSION
                        or value.ordinal != state.active_ordinal
                    ):
                        global_failure = QuantV6SpawnProtocolError(
                            "quant-v6 worker returned an invalid failure frame"
                        )
                        break
                    state.expected_exit = True
                    state.active_ordinal = None
                    if value.kind == "CANCELLED" and state.cancel_sent:
                        # A higher ordinal is intentionally stopped after a
                        # lower ordinary failure. Its cooperative cancellation
                        # is cleanup evidence, not a global operator cancel.
                        continue
                    if value.kind in {
                        "DEADLINE",
                        "CANCELLED",
                        "RESOURCE",
                        "PROTOCOL",
                    }:
                        global_failure = _rebuild_failure(value)
                        break
                    best_failure = _ordinary_failure(best_failure, value)
                else:
                    global_failure = QuantV6SpawnProtocolError(
                        "quant-v6 worker returned an unsupported frame"
                    )
                    break
            if global_failure is not None:
                break

            for state in states:
                if state.process.is_alive() or state.expected_exit:
                    continue
                if state.active_ordinal is None:
                    global_failure = QuantV6SpawnWorkerError(
                        f"quant-v6 idle worker {state.worker_index} exited"
                    )
                    break
                failure = _CandidateFailureWire(
                    protocol_version=_WIRE_PROTOCOL_VERSION,
                    ordinal=state.active_ordinal,
                    kind="UNEXPECTED",
                    exception_type="WorkerExit",
                    message=(
                        f"worker exited with code {state.process.exitcode}"
                    ),
                )
                state.expected_exit = True
                state.active_ordinal = None
                best_failure = _ordinary_failure(best_failure, failure)
            if global_failure is not None:
                break

            if pending_fetch is not None and pending_fetch.done():
                assert pending_ordinal is not None
                try:
                    fetched_ready = pending_fetch.result()
                    if (
                        fetched_ready.request.registration is not registration
                        or fetched_ready.request.member
                        is not registration.members[pending_ordinal]
                    ):
                        raise QuantV6SpawnProtocolError(
                            "prefetched evidence is outside registration order"
                        )
                except Exception as exc:
                    failure = _failure_wire(
                        ordinal=pending_ordinal,
                        kind=_classify_fetch_failure(exc),
                        exc=exc,
                    )
                    if failure.kind in {
                        "DEADLINE",
                        "CANCELLED",
                        "RESOURCE",
                        "PROTOCOL",
                    }:
                        global_failure = _rebuild_failure(failure)
                    else:
                        best_failure = _ordinary_failure(
                            best_failure,
                            failure,
                        )
                pending_fetch = None
                pending_ordinal = None
                if global_failure is not None:
                    break

            if best_failure is not None:
                assert best_failure.ordinal is not None
                if pending_fetch is not None:
                    pending_fetch.cancel()
                for state in states:
                    if (
                        state.active_ordinal is not None
                        and state.active_ordinal > best_failure.ordinal
                    ):
                        _send_selective_cancel(
                            state,
                            max_frame_bytes=max_frame_bytes,
                            watchdog=active_watchdog,
                        )
                if not any(
                    state.active_ordinal is not None
                    and state.active_ordinal < best_failure.ordinal
                    for state in states
                ):
                    break
                time.sleep(_POLL_SECONDS)
                continue

            idle_state = next((
                state
                for state in states
                if (
                    state.ready
                    and state.active_ordinal is None
                    and state.process.is_alive()
                    and not state.expected_exit
                )
            ), None)
            if fetched_ready is not None and idle_state is not None:
                ordinal = fetched_ready.request.member.ordinal
                evaluation_module.validate_quant_v6_registration_plan(
                    registration
                )
                try:
                    _send_wire(
                        idle_state.connection,
                        _CandidateWorkWire(
                            protocol_version=_WIRE_PROTOCOL_VERSION,
                            ordinal=ordinal,
                            completed_fetch=fetched_ready,
                        ),
                        max_frame_bytes=max_frame_bytes,
                    )
                except (
                    BrokenPipeError,
                    EOFError,
                    OSError,
                    QuantV6SpawnProtocolError,
                ) as exc:
                    active_watchdog.raise_if_failed(cause=exc)
                    if isinstance(exc, QuantV6SpawnProtocolError):
                        raise
                    raise QuantV6SpawnWorkerError(
                        "quant-v6 worker closed while accepting candidate work"
                    ) from exc
                active_watchdog.raise_if_failed()
                idle_state.active_ordinal = ordinal
                fetched_ready = None
                if next_fetch_ordinal < total:
                    pending_ordinal = next_fetch_ordinal
                    pending_fetch = submit_fetch(next_fetch_ordinal)
                    next_fetch_ordinal += 1

            if (
                pending_fetch is None
                and fetched_ready is None
                and next_fetch_ordinal < total
                and idle_state is not None
            ):
                pending_ordinal = next_fetch_ordinal
                pending_fetch = submit_fetch(next_fetch_ordinal)
                next_fetch_ordinal += 1

            if (
                pending_fetch is None
                and fetched_ready is None
                and not any(
                    state.active_ordinal is not None for state in states
                )
                and len(prepared_by_ordinal) < total
            ):
                global_failure = QuantV6SpawnProtocolError(
                    "quant-v6 spawn pipeline stalled before cohort completion"
                )
                break
            time.sleep(_POLL_SECONDS)

        if global_failure is not None:
            raise global_failure
        if best_failure is not None:
            evaluation_deadline.checkpoint()
            raise _rebuild_failure(best_failure)
        if len(prepared_by_ordinal) != total:
            raise QuantV6SpawnProtocolError(
                "quant-v6 spawn pipeline returned an incomplete cohort"
            )
        pipeline_memory_fence.checkpoint(states)
        evaluation_deadline.checkpoint()
        ordered = tuple(
            prepared_by_ordinal[ordinal]
            for ordinal in range(total)
        )

        def checkpoint_assembly_resources() -> None:
            active_watchdog.raise_if_failed()
            pipeline_memory_fence.checkpoint(states)
            active_watchdog.raise_if_failed()

        assembly_checkpoint = _pipeline_evaluation_checkpoint(
            evaluation_deadline,
            resource_checkpoint=checkpoint_assembly_resources,
        )
        prepared = _assemble_prepared_publication(
            plan=registration,
            candidates=ordered,
            evaluation_deadline=assembly_checkpoint,
        )
        assembly_checkpoint.checkpoint()
        active_watchdog.stop_and_join()
        watchdog_stopped = True
        active_watchdog.raise_if_failed()
        _stop_and_join_workers(
            states,
            normal=True,
            max_frame_bytes=max_frame_bytes,
        )
        workers_stopped = True
        pipeline_memory_fence.checkpoint()
        evaluation_deadline.checkpoint()
        return prepared
    except BaseException as exc:
        if watchdog is not None:
            watchdog.raise_if_failed(cause=exc)
        raise
    finally:
        active_error = sys.exc_info()[0] is not None
        cleanup_failure: BaseException | None = None
        if watchdog is not None and not watchdog_stopped:
            try:
                watchdog.stop_and_join()
                watchdog_stopped = True
            except BaseException as exc:
                if cleanup_failure is None:
                    cleanup_failure = exc
                logger.exception("quant-v6 watchdog cleanup failed")
        watchdog_failure = watchdog.failure() if watchdog is not None else None
        if watchdog_failure is not None and cleanup_failure is None:
            cleanup_failure = watchdog_failure
        if not workers_stopped:
            try:
                _stop_and_join_workers(
                    states,
                    normal=False,
                    max_frame_bytes=max_frame_bytes,
                )
            except BaseException as exc:
                if cleanup_failure is None:
                    cleanup_failure = exc
                logger.exception("quant-v6 spawn worker cleanup failed")
        try:
            _drain_fetch(
                pending_fetch,
                evaluation_deadline=evaluation_deadline,
            )
        except BaseException as exc:
            if cleanup_failure is None:
                cleanup_failure = exc
            logger.exception("quant-v6 fetch cleanup failed")
        if executor is not None:
            try:
                executor.shutdown(wait=True, cancel_futures=True)
            except BaseException as exc:
                if cleanup_failure is None:
                    cleanup_failure = exc
                logger.exception("quant-v6 fetch executor cleanup failed")
        if cleanup_failure is not None and not active_error:
            raise cleanup_failure


__all__ = [
    "QuantV6PipelineMemoryFence",
    "QuantV6SpawnProtocolError",
    "QuantV6SpawnResourceLimitError",
    "QuantV6SpawnSupervisorError",
    "QuantV6SpawnWorkerError",
    "evaluate_prepare_quant_v6_registration_spawn",
    "validate_quant_v6_spawn_provider",
]
