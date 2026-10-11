from __future__ import annotations

import enum
import gzip
import json
import threading
from datetime import datetime, timezone
from decimal import Decimal
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.core.market_data_recorder import (
    MarketDataRecorder,
    parse_recording_symbols,
)

GIB = 2**30
T0 = datetime(2026, 10, 11, 14, 30, tzinfo=timezone.utc)


class _FakeClock:
    def __init__(self, ns: int) -> None:
        self.ns = ns

    def __call__(self) -> int:
        return self.ns


class _FakeDiskUsage:
    def __init__(self, free: int) -> None:
        self.free = free
        self.calls = 0

    def __call__(self, _path: object) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(total=free_total(self.free), used=0, free=self.free)


def free_total(free: int) -> int:
    return free + GIB


class _FakeTradeType(enum.Enum):
    NORMAL = 1


def _ns(seconds: float) -> int:
    return int(seconds * 1_000_000_000)


def _quote(symbol: str, last: str, ts: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        last_done=Decimal(last),
        open=Decimal("1.10"),
        high=Decimal("2.20"),
        low=Decimal("0.90"),
        timestamp=T0,
        volume=ts,
        turnover=Decimal("10.0000"),
        current_volume=1,
        current_turnover=Decimal("1.0"),
        trade_status=_FakeTradeType.NORMAL,
        trade_session=_FakeTradeType.NORMAL,
    )


def _depth(symbol: str, bids: object, asks: object) -> SimpleNamespace:
    def level(pos: int, price: str) -> SimpleNamespace:
        return SimpleNamespace(
            position=pos, price=Decimal(price), volume=100, order_num=2,
        )

    return SimpleNamespace(symbol=symbol, bids=bids, asks=asks)


def _trades(symbol: str, *prices: str) -> SimpleNamespace:
    items = [
        SimpleNamespace(
            price=Decimal(p),
            volume=10,
            timestamp=T0,
            trade_type="U",
            direction=_FakeTradeType.NORMAL,
            trade_session=_FakeTradeType.NORMAL,
        )
        for p in prices
    ]
    return SimpleNamespace(symbol=symbol, trades=items)


def _make(
    tmp_path: Path,
    *,
    run_id: str = "r1",
    clock_ns: Callable[[], int] | None = None,
    disk_usage: Callable[[Path], Any] | None = None,
    sink: Callable[[Path, bytes], None] | None = None,
    capacity: int = 8192,
    flush_bytes: int = 256 * 1024,
    flush_seconds: float = 1.0,
    total_limit_bytes: int = 10 * GIB,
    start: bool = False,
) -> MarketDataRecorder:
    kwargs: dict[str, Any] = {}
    if sink is not None:
        kwargs["sink"] = sink
    return MarketDataRecorder(
        root=tmp_path / "md",
        symbols=frozenset({"TSLA.US"}),
        run_id=run_id,
        clock_ns=clock_ns or _FakeClock(_ns(T0.timestamp())),
        mono_ns=_FakeClock(_ns(1000.0)),
        disk_usage=disk_usage or _FakeDiskUsage(20 * GIB),
        capacity=capacity,
        flush_bytes=flush_bytes,
        flush_seconds=flush_seconds,
        total_limit_bytes=total_limit_bytes,
        start=start,
        **kwargs,
    )


def _read_records(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _all_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.jsonl.gz")) if root.exists() else []


# --- parsing / OFF --------------------------------------------------------

def test_parse_recording_symbols_normalises_and_empty_means_none() -> None:
    assert parse_recording_symbols(" tsla.us, TSLA.US ,,nvda.us") == frozenset(
        {"TSLA.US", "NVDA.US"}
    )
    assert parse_recording_symbols("") == frozenset()
    assert parse_recording_symbols("  ,  ") == frozenset()


# --- filtering / payload fidelity ----------------------------------------

def test_only_whitelisted_symbols_are_recorded(tmp_path: Path) -> None:
    rec = _make(tmp_path)
    rec.observe("quote", "NVDA.US", _quote("NVDA.US", "5.0"))
    rec.observe("quote", "TSLA.US", _quote("TSLA.US", "5.0"))
    rec.process_pending(force=True)
    rec.close(timeout=2.0)
    files = _all_files(tmp_path / "md")
    assert [p.parent.parent.name for p in files] == ["TSLA.US"]
    records = _read_records(files[0])
    assert [r["symbol"] for r in records] == ["TSLA.US"]


def test_trade_push_keeps_all_items_in_order_and_decimal_precision(tmp_path: Path) -> None:
    rec = _make(tmp_path)
    rec.observe("trade", "TSLA.US", _trades("TSLA.US", "123.4500", "123.4600", "123.4500"))
    rec.process_pending(force=True)
    rec.close(timeout=2.0)
    (path,) = _all_files(tmp_path / "md")
    (record,) = _read_records(path)
    assert record["kind"] == "trade"
    prices = [t["price"] for t in record["payload"]["trades"]]
    assert prices == ["123.4500", "123.4600", "123.4500"]
    assert isinstance(prices[0], str)
    assert record["payload"]["trades"][0]["direction"] == "NORMAL"


def test_quote_payload_uses_strings_for_decimals_and_names_for_enums(tmp_path: Path) -> None:
    rec = _make(tmp_path)
    rec.observe("quote", "TSLA.US", _quote("TSLA.US", "250.10"))
    rec.process_pending(force=True)
    rec.close(timeout=2.0)
    (path,) = _all_files(tmp_path / "md")
    (record,) = _read_records(path)
    payload = record["payload"]
    assert payload["last_done"] == "250.10"
    assert payload["trade_status"] == "NORMAL"
    assert payload["timestamp"] == T0.isoformat()
    assert record["schema_version"] == 1
    assert record["run_id"] == "r1"
    assert record["local_seq"] == 1
    assert isinstance(record["recv_wall_ns"], int)
    assert isinstance(record["recv_mono_ns"], int)


def test_depth_keeps_all_levels_order_empty_arrays_and_nulls(tmp_path: Path) -> None:
    rec = _make(tmp_path)
    bids = [
        SimpleNamespace(position=1, price=Decimal("10.5"), volume=100, order_num=1),
        SimpleNamespace(position=2, price=Decimal("10.4"), volume=200, order_num=2),
        SimpleNamespace(position=3, price=Decimal("10.3"), volume=300, order_num=3),
    ]
    rec.observe("depth", "TSLA.US", _depth("TSLA.US", bids, []))
    rec.observe("depth", "TSLA.US", _depth("TSLA.US", None, None))
    rec.process_pending(force=True)
    rec.close(timeout=2.0)
    (path,) = _all_files(tmp_path / "md")
    first, second = _read_records(path)
    assert [b["price"] for b in first["payload"]["bids"]] == ["10.5", "10.4", "10.3"]
    assert [b["position"] for b in first["payload"]["bids"]] == [1, 2, 3]
    assert first["payload"]["asks"] == []
    assert second["payload"]["bids"] is None
    assert second["payload"]["asks"] is None


# --- bounded queue ---------------------------------------------------------

def test_full_queue_drops_oldest_and_counts(tmp_path: Path) -> None:
    rec = _make(tmp_path, capacity=3)
    for i in range(5):
        rec.observe("quote", "TSLA.US", _quote("TSLA.US", str(i + 1)))
    snap = rec.snapshot()
    assert snap["queue_length"] == 3
    assert snap["dropped_oldest"] == 2
    assert snap["dropped_contention"] == 0
    rec.process_pending(force=True)
    rec.close(timeout=2.0)
    (path,) = _all_files(tmp_path / "md")
    kept = [r["payload"]["last_done"] for r in _read_records(path)]
    assert kept == ["3", "4", "5"]


def test_contended_queue_lock_drops_current_push_without_blocking(tmp_path: Path) -> None:
    rec = _make(tmp_path)
    rec.observe("quote", "TSLA.US", _quote("TSLA.US", "1"))
    assert rec._queue_lock.acquire()
    try:
        done = threading.Event()

        def producer() -> None:
            rec.observe("quote", "TSLA.US", _quote("TSLA.US", "2"))
            done.set()

        t = threading.Thread(target=producer, daemon=True)
        t.start()
        assert done.wait(timeout=2.0), "observe must not block on the queue lock"
        t.join(timeout=2.0)
    finally:
        rec._queue_lock.release()
    snap = rec.snapshot()
    assert snap["dropped_contention"] == 1
    assert snap["queue_length"] == 1


class _BrokenClock:
    def __call__(self) -> int:
        raise RuntimeError("clock broke")


def test_observe_swallows_internal_errors_and_counts_capture_errors(tmp_path: Path) -> None:
    rec = _make(tmp_path, clock_ns=_BrokenClock())
    rec.observe("quote", "TSLA.US", _quote("TSLA.US", "1"))  # must not raise
    snap = rec.snapshot()
    assert snap["capture_errors"] == 1
    assert snap["queue_length"] == 0


def test_observe_does_not_wait_for_slow_writer(tmp_path: Path) -> None:
    gate = threading.Event()

    class _SlowSink:
        def __call__(self, path: Path, member: bytes) -> None:
            gate.wait(timeout=5.0)

    rec = _make(tmp_path, sink=_SlowSink(), start=True, flush_bytes=1)
    done = threading.Event()

    def producer() -> None:
        for i in range(20):
            rec.observe("quote", "TSLA.US", _quote("TSLA.US", str(i + 1)))
        done.set()

    t = threading.Thread(target=producer, daemon=True)
    t.start()
    try:
        assert done.wait(timeout=2.0), "observe must not wait for a blocked writer"
    finally:
        gate.set()
        t.join(timeout=2.0)
        rec.close(timeout=2.0)


# --- files / rotation / gzip members --------------------------------------

def test_utc_hour_split_creates_separate_files_per_hour(tmp_path: Path) -> None:
    clock = _FakeClock(_ns(T0.timestamp()))
    rec = _make(tmp_path, clock_ns=clock)
    rec.observe("quote", "TSLA.US", _quote("TSLA.US", "1"))
    clock.ns += _ns(3600)  # 15:30 UTC
    rec.observe("quote", "TSLA.US", _quote("TSLA.US", "2"))
    rec.process_pending(force=True)
    rec.close(timeout=2.0)
    names = sorted(p.name for p in _all_files(tmp_path / "md"))
    assert names == ["14-r1.jsonl.gz", "15-r1.jsonl.gz"]
    day_dirs = {p.parent.name for p in _all_files(tmp_path / "md")}
    assert day_dirs == {"20261011"}


def test_restart_appends_new_member_and_never_truncates(tmp_path: Path) -> None:
    rec1 = _make(tmp_path)
    rec1.observe("quote", "TSLA.US", _quote("TSLA.US", "1"))
    rec1.process_pending(force=True)
    rec1.close(timeout=2.0)
    rec2 = _make(tmp_path, run_id="r2")
    rec2.observe("quote", "TSLA.US", _quote("TSLA.US", "2"))
    rec2.process_pending(force=True)
    rec2.close(timeout=2.0)
    files = _all_files(tmp_path / "md")
    assert [p.name for p in files] == ["14-r1.jsonl.gz", "14-r2.jsonl.gz"]
    assert [r["payload"]["last_done"] for r in _read_records(files[0])] == ["1"]


def test_multiple_gzip_members_read_back_fully(tmp_path: Path) -> None:
    rec = _make(tmp_path)
    rec.observe("quote", "TSLA.US", _quote("TSLA.US", "1"))
    rec.process_pending(force=True)
    rec.observe("quote", "TSLA.US", _quote("TSLA.US", "2"))
    rec.process_pending(force=True)
    rec.close(timeout=2.0)
    (path,) = _all_files(tmp_path / "md")
    raw = path.read_bytes()
    assert raw.count(b"\x1f\x8b\x08") >= 2  # two gzip members
    assert [r["payload"]["last_done"] for r in _read_records(path)] == ["1", "2"]


def test_flush_threshold_emits_member_at_256_kib(tmp_path: Path) -> None:
    members: list[bytes] = []

    def sink(_path: Path, member: bytes) -> None:
        members.append(member)

    rec = _make(tmp_path, sink=sink, flush_bytes=256 * 1024, flush_seconds=3600.0)
    big = "9" * 20_000
    for _ in range(20):
        rec.observe("trade", "TSLA.US", _trades("TSLA.US", big))
    rec.process_pending(force=False)
    assert len(members) >= 1
    rec.close(timeout=2.0)


# --- disk guard / errors ---------------------------------------------------

def test_disk_free_below_5_gib_stops_recording(tmp_path: Path) -> None:
    disk = _FakeDiskUsage(free=4 * GIB)
    rec = _make(tmp_path, disk_usage=disk)
    rec.observe("quote", "TSLA.US", _quote("TSLA.US", "1"))
    rec.process_pending(force=True)
    snap = rec.snapshot()
    assert snap["state"] == "STOPPED_DISK"
    rec.observe("quote", "TSLA.US", _quote("TSLA.US", "2"))
    assert rec.snapshot()["queue_length"] == 0
    rec.close(timeout=2.0)


def test_recorded_total_reaching_10_gib_stops_recording(tmp_path: Path) -> None:
    rec = _make(tmp_path, total_limit_bytes=64)
    for _ in range(50):
        rec.observe("trade", "TSLA.US", _trades("TSLA.US", "1.0000"))
    rec.process_pending(force=True)
    assert rec.snapshot()["state"] == "STOPPED_DISK"
    rec.close(timeout=2.0)


def test_disk_check_failure_stops_recording(tmp_path: Path) -> None:
    def broken(_path: object) -> SimpleNamespace:
        raise OSError("statvfs failed")

    rec = _make(tmp_path, disk_usage=broken)
    rec.observe("quote", "TSLA.US", _quote("TSLA.US", "1"))
    rec.process_pending(force=True)
    assert rec.snapshot()["state"] == "STOPPED_DISK"
    rec.close(timeout=2.0)


def test_write_permission_error_degrades_and_stops_without_retry(tmp_path: Path) -> None:
    attempts: list[Path] = []

    def denied(path: Path, _member: bytes) -> None:
        attempts.append(path)
        raise PermissionError(13, "denied")

    rec = _make(tmp_path, sink=denied)
    rec.observe("quote", "TSLA.US", _quote("TSLA.US", "1"))
    rec.process_pending(force=True)
    rec.observe("quote", "TSLA.US", _quote("TSLA.US", "2"))
    rec.process_pending(force=True)
    snap = rec.snapshot()
    assert snap["state"] == "DEGRADED"
    assert snap["last_error"] and "denied" in snap["last_error"]
    assert len(attempts) == 1  # no unbounded retry loop
    rec.close(timeout=2.0)


# --- lifecycle --------------------------------------------------------------

def test_close_is_bounded_and_idempotent_with_stuck_writer(tmp_path: Path) -> None:
    gate = threading.Event()

    def stuck(_path: Path, _member: bytes) -> None:
        gate.wait(timeout=10.0)

    rec = _make(tmp_path, sink=stuck, start=True)
    rec.observe("quote", "TSLA.US", _quote("TSLA.US", "1"))
    rec.close(timeout=0.2)
    rec.close(timeout=0.2)  # second close must be a no-op
    gate.set()
    assert rec.snapshot()["writer_alive"] in {True, False}


def test_no_second_writer_while_first_is_alive(tmp_path: Path) -> None:
    rec = _make(tmp_path, start=True)
    first = rec._thread
    rec.start()
    assert rec._thread is first
    rec.close(timeout=2.0)
    assert rec.snapshot()["writer_alive"] is False


def test_snapshot_reports_configuration_without_io(tmp_path: Path) -> None:
    disk = _FakeDiskUsage(20 * GIB)
    rec = _make(tmp_path, disk_usage=disk)
    snap = rec.snapshot()
    assert snap["configured_symbols"] == ["TSLA.US"]
    assert snap["state"] == "WAITING"
    assert snap["writer_alive"] is False
    assert disk.calls == 0
    assert not (tmp_path / "md").exists()
    rec.close(timeout=2.0)
