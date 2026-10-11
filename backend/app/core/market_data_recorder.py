"""Read-only market-data recorder for SDK quote/depth/trade pushes.

The recorder never submits orders, never updates broker caches, and never
calls back into the runner. Producers only enqueue a reference to the SDK
event; JSON conversion, compression and disk I/O run on the writer thread.
See docs/research/scalping-20261011/recorder-spec.md.
"""

from __future__ import annotations

import enum
import gzip
import json
import logging
import math
import os
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.core.log_throttle import RepeatedLogThrottle

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
GIB = 2**30
DEFAULT_CAPACITY = 8192
DEFAULT_FLUSH_BYTES = 256 * 1024
DEFAULT_FLUSH_SECONDS = 1.0
DEFAULT_TOTAL_LIMIT_BYTES = 10 * GIB
DEFAULT_MIN_FREE_BYTES = 5 * GIB
DEFAULT_DISK_CHECK_SECONDS = 5.0
_WRITER_POLL_SECONDS = 0.1
_SUMMARY_WINDOW_SECONDS = 60.0

STATE_WAITING = "WAITING"
STATE_RECORDING = "RECORDING"
STATE_DEGRADED = "DEGRADED"
STATE_STOPPED_DISK = "STOPPED_DISK"


def parse_recording_symbols(raw: str) -> frozenset[str]:
    """Normalise the whitelist: strip, uppercase, dedupe. Empty means none."""
    return frozenset(
        item.strip().upper() for item in raw.split(",") if item.strip()
    )


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.name
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    return str(value)


def _attr(obj: Any, name: str) -> Any:
    return _to_jsonable(getattr(obj, name, None))


_QUOTE_FIELDS = (
    "last_done", "open", "high", "low", "timestamp", "volume", "turnover",
    "current_volume", "current_turnover", "trade_status", "trade_session",
)
_TRADE_FIELDS = (
    "price", "volume", "timestamp", "trade_type", "direction", "trade_session",
)
_DEPTH_LEVEL_FIELDS = ("position", "price", "volume", "order_num")


def _quote_payload(event: Any) -> dict[str, Any]:
    return {name: _attr(event, name) for name in _QUOTE_FIELDS}


def _trade_payload(event: Any) -> dict[str, Any]:
    items = getattr(event, "trades", None) or ()
    return {
        "trades": [
            {name: _attr(item, name) for name in _TRADE_FIELDS} for item in items
        ]
    }


def _levels(levels: Any) -> list[dict[str, Any]] | None:
    if levels is None:
        return None
    return [
        {name: _attr(level, name) for name in _DEPTH_LEVEL_FIELDS}
        for level in levels
    ]


def _depth_payload(event: Any) -> dict[str, Any]:
    return {
        "bids": _levels(getattr(event, "bids", None)),
        "asks": _levels(getattr(event, "asks", None)),
    }


_PAYLOAD_BUILDERS: dict[str, Callable[[Any], dict[str, Any]]] = {
    "quote": _quote_payload,
    "depth": _depth_payload,
    "trade": _trade_payload,
}


def _default_disk_usage(path: Path) -> Any:
    return shutil.disk_usage(path)


def _existing_ancestor(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _append_member(path: Path, member: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as fh:
        fh.write(member)
        fh.flush()
        os.fsync(fh.fileno())


class MarketDataRecorder:
    """Bounded, non-blocking recorder. ``observe`` never raises or waits."""

    def __init__(
        self,
        *,
        root: Path,
        symbols: frozenset[str],
        run_id: str,
        clock_ns: Callable[[], int] = time.time_ns,
        mono_ns: Callable[[], int] = time.monotonic_ns,
        disk_usage: Callable[[Path], Any] = _default_disk_usage,
        sink: Callable[[Path, bytes], None] = _append_member,
        capacity: int = DEFAULT_CAPACITY,
        flush_bytes: int = DEFAULT_FLUSH_BYTES,
        flush_seconds: float = DEFAULT_FLUSH_SECONDS,
        total_limit_bytes: int = DEFAULT_TOTAL_LIMIT_BYTES,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        disk_check_seconds: float = DEFAULT_DISK_CHECK_SECONDS,
        start: bool = True,
    ) -> None:
        self._root = Path(root)
        self._symbols = frozenset(symbols)
        self._run_id = run_id
        self._clock_ns = clock_ns
        self._mono_ns = mono_ns
        self._disk_usage = disk_usage
        self._sink = sink
        self._capacity = capacity
        self._flush_bytes = flush_bytes
        self._flush_seconds = flush_seconds
        self._total_limit_bytes = total_limit_bytes
        self._min_free_bytes = min_free_bytes
        self._disk_check_seconds = disk_check_seconds

        self._queue: deque[tuple[str, str, Any, int, int, int]] = deque()
        self._queue_lock = threading.Lock()
        self._seq = 0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False

        self._state = STATE_WAITING
        self._last_error: str | None = None
        self._received: dict[str, int] = {"quote": 0, "depth": 0, "trade": 0}
        self._written: dict[str, int] = {"quote": 0, "depth": 0, "trade": 0}
        self._dropped_oldest = 0
        self._dropped_contention = 0
        self._capture_errors = 0
        self._bytes_recorded = 0
        self._free_bytes: int | None = None
        self._last_write_at: float | None = None
        self._throttle = RepeatedLogThrottle(window_seconds=_SUMMARY_WINDOW_SECONDS)
        # Writer-thread only: encoded lines awaiting one gzip member per file.
        self._pending_members: list[tuple[Path, bytes, str]] = []
        self._pending_bytes = 0
        self._start_requested = start
        if start:
            self.start()

    # --- producer side (SDK callback thread) ---------------------------

    def observe(self, kind: str, symbol: str, event: Any) -> None:
        """Enqueue one SDK push. Never raises, never blocks on the writer."""
        try:
            self._observe(kind, symbol, event)
        except Exception:
            self._capture_errors += 1

    def _observe(self, kind: str, symbol: str, event: Any) -> None:
        if self._closed or self._state == STATE_STOPPED_DISK:
            return
        if symbol not in self._symbols:
            return
        wall_ns = int(self._clock_ns())
        mono_ns = int(self._mono_ns())
        if not self._queue_lock.acquire(blocking=False):
            self._dropped_contention += 1
            return
        try:
            self._seq += 1
            if len(self._queue) >= self._capacity:
                self._queue.popleft()
                self._dropped_oldest += 1
            self._queue.append((kind, symbol, event, wall_ns, mono_ns, self._seq))
            self._received[kind] = self._received.get(kind, 0) + 1
        finally:
            self._queue_lock.release()

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._closed:
            return
        with self._queue_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._writer_loop,
                name=f"market-data-recorder-{self._run_id}",
                daemon=True,
            )
            self._thread.start()

    def close(self, timeout: float = 2.0) -> None:
        """Stop accepting pushes, drain what fits in ``timeout``, join."""
        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def process_pending(self, *, force: bool = False) -> None:
        """Write everything queued now. Used by the writer loop and tests."""
        self._check_disk()
        batch = self._drain()
        if batch:
            self._write_batch(batch, force_flush=force)
        elif force:
            self._flush_pending_member()

    # --- writer side -----------------------------------------------------

    def _writer_loop(self) -> None:
        last_flush = time.monotonic()
        last_disk_check = 0.0
        while True:
            stopping = self._stop_event.is_set()
            batch = self._drain()
            now = time.monotonic()
            if now - last_disk_check >= self._disk_check_seconds:
                last_disk_check = now
                self._check_disk()
            # Size-triggered flush happens inside _write_batch; time-triggered here.
            self._write_batch(batch, force_flush=False)
            if self._pending_members and (
                stopping or (now - last_flush) >= self._flush_seconds
            ):
                self._flush_pending_member()
                last_flush = now
            if stopping:
                break
            if not batch:
                self._stop_event.wait(_WRITER_POLL_SECONDS)

    def _drain(self) -> list[tuple[str, str, Any, int, int, int]]:
        with self._queue_lock:
            items = list(self._queue)
            self._queue.clear()
        return items

    def _discard_queue(self) -> None:
        with self._queue_lock:
            self._queue.clear()

    def _check_disk(self) -> None:
        if self._state == STATE_STOPPED_DISK:
            return
        try:
            probe = _existing_ancestor(self._root)
            usage = self._disk_usage(probe)
            self._free_bytes = int(usage.free)
        except Exception as exc:
            self._stop_for_disk(f"disk check failed: {exc}")
            return
        if self._free_bytes < self._min_free_bytes:
            self._stop_for_disk(f"free space {self._free_bytes} below minimum")
        elif self._bytes_recorded >= self._total_limit_bytes:
            self._stop_for_disk("recorded total reached limit")

    def _stop_for_disk(self, reason: str) -> None:
        self._state = STATE_STOPPED_DISK
        self._last_error = reason
        self._discard_queue()
        self._log_throttled("disk", "market data recording stopped: %s", reason)

    def _log_throttled(self, key: str, message: str, *args: object) -> None:
        if self._throttle.should_log(key):
            suppressed = self._throttle.take_suppressed_count()
            logger.warning(message + " (suppressed=%d)", *args, suppressed)

    def _write_batch(
        self,
        batch: list[tuple[str, str, Any, int, int, int]],
        *,
        force_flush: bool = False,
    ) -> None:
        if self._state == STATE_STOPPED_DISK:
            return
        for kind, symbol, event, wall_ns, mono_ns, seq in batch:
            try:
                path, line = self._encode(kind, symbol, event, wall_ns, mono_ns, seq)
            except Exception as exc:
                self._capture_errors += 1
                self._last_error = f"encode failed: {exc}"
                continue
            self._pending_members.append((path, line, kind))
            self._pending_bytes += len(line)
        if force_flush or self._pending_bytes >= self._flush_bytes:
            self._flush_pending_member()

    def _encode(
        self,
        kind: str,
        symbol: str,
        event: Any,
        wall_ns: int,
        mono_ns: int,
        seq: int,
    ) -> tuple[Path, bytes]:
        builder = _PAYLOAD_BUILDERS[kind]
        recv_at = datetime.fromtimestamp(wall_ns / 1_000_000_000, tz=timezone.utc)
        record = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self._run_id,
            "local_seq": seq,
            "symbol": symbol,
            "kind": kind,
            "recv_wall_ns": wall_ns,
            "recv_mono_ns": mono_ns,
            "payload": builder(event),
        }
        path = self._path_for(symbol, recv_at)
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        return path, (line + "\n").encode("utf-8")

    def _path_for(self, symbol: str, recv_at: datetime) -> Path:
        day = recv_at.strftime("%Y%m%d")
        hour = recv_at.strftime("%H")
        return self._root / symbol / day / f"{hour}-{self._run_id}.jsonl.gz"

    def _flush_pending_member(self) -> None:
        """Write each path's buffered lines as one gzip member, then clear."""
        if not self._pending_members:
            return
        grouped: dict[Path, list[tuple[bytes, str]]] = {}
        for path, line, kind in self._pending_members:
            grouped.setdefault(path, []).append((line, kind))
        self._pending_members = []
        self._pending_bytes = 0
        for path, items in grouped.items():
            raw = b"".join(line for line, _ in items)
            member = gzip.compress(raw, compresslevel=1)
            try:
                self._sink(path, member)
            except Exception as exc:
                self._on_write_error(exc)
                return
            for _, kind in items:
                self._written[kind] = self._written.get(kind, 0) + 1
            self._bytes_recorded += len(member)
            self._last_write_at = time.time()
            self._state = STATE_RECORDING
        self._check_disk_limits()

    def _on_write_error(self, exc: Exception) -> None:
        self._state = STATE_DEGRADED
        self._last_error = f"write failed: {exc}"
        self._discard_queue()
        self._log_throttled("write", "market data write failed: %s", exc)
        self._closed = True
        self._stop_event.set()

    def _check_disk_limits(self) -> None:
        if self._bytes_recorded >= self._total_limit_bytes:
            self._stop_for_disk("recorded total reached limit")

    def configured_symbols(self) -> frozenset[str]:
        return self._symbols

    # --- diagnostics (cached state only: no I/O, no SDK calls) -----------

    def snapshot(self) -> dict[str, Any]:
        thread = self._thread
        with self._queue_lock:
            queue_length = len(self._queue)
        return {
            "enabled": True,
            "state": self._state,
            "configured_symbols": sorted(self._symbols),
            "writer_alive": bool(thread is not None and thread.is_alive()),
            "queue_length": queue_length,
            "queue_capacity": self._capacity,
            "received": dict(self._received),
            "written": dict(self._written),
            "dropped_oldest": self._dropped_oldest,
            "dropped_contention": self._dropped_contention,
            "capture_errors": self._capture_errors,
            "bytes_recorded": self._bytes_recorded,
            "total_limit_bytes": self._total_limit_bytes,
            "free_bytes": self._free_bytes,
            "min_free_bytes": self._min_free_bytes,
            "last_write_at": self._last_write_at,
            "last_error": self._last_error,
        }
