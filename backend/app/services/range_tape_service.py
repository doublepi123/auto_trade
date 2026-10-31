"""Read-only recorded-market-data input for range research; never submits orders."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import stat
import zlib
from collections.abc import Generator, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import BinaryIO, cast

from app.domain.range_replay import TapeQuote

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass
class TapeReadStats:
    """Counts cover all validated rows; emissions cover [start_at, end_at).

    Manifest entries contain the supplied path, compressed-file SHA-256 and
    compressed byte count. Only fully consumed, stable files enter the manifest.
    A failed/partially consumed iterator is not a valid research input.
    """

    rows: int = 0
    quote_events: int = 0
    depth_events: int = 0
    trade_events: int = 0
    emitted_quotes: int = 0
    input_manifest: list[dict[str, str | int]] = field(default_factory=list)


def iter_tape_quotes(
    paths: Sequence[Path],
    *,
    symbol: str,
    start_at: datetime,
    end_at: datetime,
    stats: TapeReadStats,
) -> Iterator[TapeQuote]:
    """Join callback history without sorting, future depth or cross-run caches.

    All times are UTC; naive SDK source times (and naive window bounds) mean
    UTC explicitly, never the host timezone. Rows outside the half-open window
    still seed caches and undergo validation. Exhaust the iterator to validate
    every file, including its gzip trailer and final metadata.
    """
    _identity(symbol, "symbol")
    start_ns, end_ns = _datetime_ns(start_at), _datetime_ns(end_at)
    if start_ns >= end_ns:
        raise ValueError("Tape window must have start_at before end_at")

    active_run: str | None = None
    seen_runs: set[str] = set()
    previous_wall: int | None = None
    previous_mono: int | None = None
    previous_seq: int | None = None
    quote: tuple[Decimal | None, datetime | None] | None = None
    depth: tuple[Decimal | None, Decimal | None, int | None, int | None, datetime] | None = None

    for index, path in enumerate(paths, 1):
        records = _iter_records(path, index, stats)
        try:
            for row in records:
                if type(row.get("schema_version")) is not int or row["schema_version"] != 1:
                    raise ValueError("Unsupported tape schema_version")
                run_id = _identity(row.get("run_id"), "run_id")
                if _identity(row.get("symbol"), "symbol") != symbol:
                    raise ValueError("Tape symbol does not match requested symbol")
                sequence = _integer(row.get("local_seq"), "local_seq", minimum=1)
                wall = _integer(row.get("recv_wall_ns"), "recv_wall_ns")
                mono = _integer(row.get("recv_mono_ns"), "recv_mono_ns")
                observed = _wall_time(wall)
                payload = _object(row.get("payload"), "payload")
                kind = row.get("kind")
                if kind not in ("quote", "depth", "trade"):
                    raise ValueError("Unknown tape event kind")
                if previous_wall is not None and wall < previous_wall:
                    raise ValueError("Tape receive wall clock moved backwards")
                if run_id != active_run:
                    if run_id in seen_runs:
                        raise ValueError("Tape run reappeared out of order")
                    seen_runs.add(run_id)
                    active_run = run_id
                    previous_seq = previous_mono = None
                    quote = depth = None
                if previous_seq is not None and sequence <= previous_seq:
                    raise ValueError("Tape sequence is duplicate or out of order")
                if previous_mono is not None and mono < previous_mono:
                    raise ValueError("Tape receive monotonic clock moved backwards")
                previous_wall, previous_mono, previous_seq = wall, mono, sequence

                if kind == "quote":
                    quote = _quote_payload(payload)
                    stats.quote_events += 1
                elif kind == "depth":
                    bid, bid_size = _top_level(payload.get("bids"))
                    ask, ask_size = _top_level(payload.get("asks"))
                    depth = bid, ask, bid_size, ask_size, observed
                    stats.depth_events += 1
                else:
                    _trade_payload(payload)
                    stats.trade_events += 1
                stats.rows += 1

                if (
                    kind != "trade" and quote is not None and depth is not None
                    and start_ns <= wall < end_ns
                ):
                    last, source_at = quote
                    bid, ask, bid_size, ask_size, depth_at = depth
                    stats.emitted_quotes += 1
                    yield TapeQuote(
                        symbol=symbol, run_id=run_id, sequence=sequence,
                        observed_at=observed, source_at=source_at, depth_at=depth_at,
                        last=last, bid=bid, ask=ask,
                        bid_size=bid_size, ask_size=ask_size,
                    )
        finally:
            # Propagate early close to the file reader so its final stability
            # check runs even if validation above fails or the caller stops.
            records.close()


def _identity(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"Missing or invalid tape {field_name}")
    return value


def _integer(value: object, field_name: str, *, minimum: int = 0) -> int:
    # bool and JSON floats are not recorder integer identities or clocks.
    if type(value) is not int or value < minimum:
        raise ValueError(f"Missing or invalid tape {field_name}")
    return value


def _object(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid tape {field_name} object")
    return cast(dict[str, object], value)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("Invalid tape datetime")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _datetime_ns(value: datetime) -> int:
    delta = _utc(value) - _EPOCH
    return ((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1000


def _wall_time(value: int) -> datetime:
    try:
        # Compare original integer ns for ordering; datetime has microsecond
        # precision and must never round a callback into the following instant.
        return _EPOCH + timedelta(microseconds=value // 1000)
    except OverflowError:
        raise ValueError("Invalid tape receive timestamp") from None


def _source_time(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or ("T" not in value and " " not in value):
        raise ValueError("Invalid tape source timestamp")
    try:
        return _utc(datetime.fromisoformat(value))
    except (ValueError, OverflowError):
        raise ValueError("Invalid tape source timestamp") from None


def _number(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Invalid tape numeric field")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid tape numeric field") from None
    if not result.is_finite():
        raise ValueError("Non-finite tape numeric field")
    return result


def _size(value: object) -> int | None:
    number = _number(value)
    if number is None:
        return None
    if number < 0 or number != number.to_integral_value():
        raise ValueError("Invalid tape integer quantity")
    return int(number)


def _quote_payload(payload: dict[str, object]) -> tuple[Decimal | None, datetime | None]:
    for field_name in ("open", "high", "low", "turnover", "current_turnover"):
        _number(payload.get(field_name))
    for field_name in ("volume", "current_volume"):
        _size(payload.get(field_name))
    return _number(payload.get("last_done")), _source_time(payload.get("timestamp"))


def _top_level(value: object) -> tuple[Decimal | None, int | None]:
    if value is None:
        return None, None
    if not isinstance(value, list):
        raise ValueError("Invalid tape depth levels")
    seen: set[int] = set()
    top: tuple[Decimal | None, int | None] = None, None
    for item in cast(list[object], value):
        level = _object(item, "depth level")
        position = _integer(level.get("position"), "depth position", minimum=1)
        if position in seen:
            raise ValueError("Ambiguous duplicate tape depth position")
        seen.add(position)
        price, volume = _number(level.get("price")), _size(level.get("volume"))
        _size(level.get("order_num"))
        if position == 1:
            top = price, volume
    return top


def _trade_payload(payload: dict[str, object]) -> None:
    trades = payload.get("trades")
    if not isinstance(trades, list):
        raise ValueError("Invalid tape trades")
    for item in cast(list[object], trades):
        trade = _object(item, "trade")
        _number(trade.get("price"))
        _size(trade.get("volume"))
        _source_time(trade.get("timestamp"))


def _json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate tape JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise ValueError("Non-finite tape JSON numeric field")


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _assert_stable(path: Path, stream: BinaryIO, before: os.stat_result) -> None:
    if (
        _signature(os.fstat(stream.fileno())) != _signature(before)
        or _signature(path.stat()) != _signature(before)
    ):
        raise ValueError("Tape input changed during read")


def _iter_records(
    path: Path, index: int, stats: TapeReadStats,
) -> Generator[dict[str, object], None, None]:
    try:
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Tape input must be a regular gzip JSONL file")
        with path.open("rb") as stream:
            _assert_stable(path, stream, before)
            if stream.read(2) != b"\x1f\x8b":
                raise ValueError("Tape input is not gzip JSONL")
            stream.seek(0)
            # Hash in bounded chunks, then decode through the same descriptor.
            # ctime/mtime/size/inode checks bind this digest to the decoded file.
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            _assert_stable(path, stream, before)
            stream.seek(0)
            try:
                with gzip.GzipFile(fileobj=stream, mode="rb") as archive:
                    for line in archive:
                        try:
                            value: object = json.loads(
                                line.decode("utf-8"), parse_float=Decimal,
                                parse_constant=_reject_constant,
                                object_pairs_hook=_json_pairs,
                            )
                        except (json.JSONDecodeError, UnicodeError, RecursionError):
                            raise ValueError("Malformed tape JSON row") from None
                        yield _object(value, "row")
            finally:
                _assert_stable(path, stream, before)
            stats.input_manifest.append({
                "path": str(path), "sha256": digest, "bytes": before.st_size,
            })
    except (OSError, EOFError, zlib.error, UnicodeError):
        # Do not interpolate paths, payloads, or OS exception text: source paths
        # may contain deployment details or secrets. Suppress the source chain.
        raise ValueError(f"Tape input {index}: unreadable or malformed gzip JSONL") from None
