"""Synthetic, offline behavioral checks for the causal tape reader."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.services.range_tape_service import TapeReadStats, iter_tape_quotes

UTC = timezone.utc
T0 = datetime(2026, 10, 30, 14, 30, tzinfo=UTC)
WALL_NS = int(T0.timestamp()) * 1_000_000_000
SYMBOL = "TSLA.US"


def _row(
    kind: str,
    seq: int,
    second: int = 0,
    *,
    run: str = "run-a",
) -> dict[str, Any]:
    payloads: dict[str, dict[str, Any]] = {
        "quote": {
            "last_done": "400.123400",
            "timestamp": (T0 + timedelta(seconds=second)).isoformat(),
        },
        "depth": {
            # Deliberately unsorted: array order is not the top-of-book contract.
            "bids": [
                {"position": 2, "price": "399", "volume": 9000},
                {"position": 1, "price": "400.10", "volume": 12},
            ],
            "asks": [{"position": 1, "price": "400.20", "volume": 14}],
        },
        "trade": {
            "trades": [{"price": "1", "volume": 100000, "timestamp": T0.isoformat()}],
        },
    }
    return {
        "schema_version": 1,
        "run_id": run,
        "local_seq": seq,
        "symbol": SYMBOL,
        "kind": kind,
        "recv_wall_ns": WALL_NS + second * 1_000_000_000,
        "recv_mono_ns": seq * 1_000_000,
        "payload": payloads[kind],
    }


def _write(path: Path, *rows: dict[str, Any]) -> Path:
    raw = "".join(json.dumps(row) + "\n" for row in rows).encode()
    path.write_bytes(gzip.compress(raw, mtime=0))
    return path


def _read(
    *paths: Path,
    stats: TapeReadStats | None = None,
    start_at: datetime = T0,
    end_at: datetime = T0 + timedelta(hours=1),
) -> list[Any]:
    return list(iter_tape_quotes(
        paths,
        symbol=SYMBOL,
        start_at=start_at,
        end_at=end_at,
        stats=stats if stats is not None else TapeReadStats(),
    ))


def test_join_never_leaks_future_depth_and_retains_callback_clocks(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "tape.gz",
        _row("quote", 1),
        _row("quote", 2, 1),
        _row("depth", 3, 2),
        _row("trade", 4, 3),
        _row("quote", 5, 4),
        _row("depth", 6, 5),
    )
    stats = TapeReadStats()
    quotes = _read(path, stats=stats)
    assert [q.sequence for q in quotes] == [3, 5, 6]
    assert [q.observed_at for q in quotes] == [
        T0 + timedelta(seconds=s) for s in (2, 4, 5)
    ]
    assert [q.depth_at for q in quotes] == [
        T0 + timedelta(seconds=s) for s in (2, 2, 5)
    ]
    assert [q.source_at for q in quotes] == [
        T0 + timedelta(seconds=s) for s in (1, 4, 4)
    ]
    assert all(q.symbol == SYMBOL and q.run_id == "run-a" for q in quotes)
    assert quotes[0].last == Decimal("400.123400")
    assert quotes[0].bid == Decimal("400.10")
    assert quotes[0].ask == Decimal("400.20")
    assert (quotes[0].bid_size, quotes[0].ask_size) == (12, 14)
    assert (stats.rows, stats.quote_events, stats.depth_events, stats.trade_events) == (6, 3, 2, 1)
    assert stats.emitted_quotes == 3


def test_restart_clears_both_caches_and_allows_sequence_reset(tmp_path: Path) -> None:
    quotes = _read(_write(
        tmp_path / "restart.gz",
        _row("quote", 1),
        _row("depth", 2, 1),
        _row("quote", 1, 2, run="run-b"),
        _row("depth", 2, 3, run="run-b"),
        _row("depth", 1, 4, run="run-c"),
        _row("quote", 2, 5, run="run-c"),
    ))
    assert [(q.run_id, q.sequence) for q in quotes] == [
        ("run-a", 2), ("run-b", 2), ("run-c", 2),
    ]
    assert quotes[1].depth_at == T0 + timedelta(seconds=3)
    assert quotes[2].source_at == T0 + timedelta(seconds=5)


def test_window_is_half_open_but_prior_events_seed_caches(tmp_path: Path) -> None:
    stats = TapeReadStats()
    quotes = _read(
        _write(tmp_path / "window.gz", _row("quote", 1), _row("depth", 2, 1),
               _row("quote", 3, 2), _row("quote", 4, 3)),
        start_at=T0 + timedelta(seconds=2),
        end_at=T0 + timedelta(seconds=3),
        stats=stats,
    )
    assert [q.sequence for q in quotes] == [3]
    assert stats.rows == 4
    assert stats.emitted_quotes == 1


def test_files_stay_in_caller_order_and_manifest_hashes_compressed_bytes(tmp_path: Path) -> None:
    first = _write(tmp_path / "z.gz", _row("quote", 1))
    second = _write(tmp_path / "a.gz", _row("depth", 2, 1))
    stats = TapeReadStats()
    assert [q.sequence for q in _read(first, second, stats=stats)] == [2]
    assert stats.input_manifest == [
        {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
         "bytes": path.stat().st_size}
        for path in (first, second)
    ]
    again = TapeReadStats()
    _read(first, second, stats=again)
    assert stats == again
    with pytest.raises(ValueError):
        _read(second, first)
    with pytest.raises(ValueError):
        _read(first, first)


@pytest.mark.parametrize("source", [
    "2026-10-30T14:30:00",
    "2026-10-30T14:30:00Z",
    "2026-10-30T22:30:00+08:00",
])
def test_source_timestamps_are_utc_and_independent_of_local_timezone(
    tmp_path: Path, source: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    row = _row("quote", 1)
    row["payload"]["timestamp"] = source
    (quote,) = _read(_write(tmp_path / "utc.gz", row, _row("depth", 2)))
    assert quote.source_at == T0
    assert quote.source_at.tzinfo == UTC
    assert quote.observed_at == T0
    assert quote.depth_at == T0


def test_receive_time_comparison_keeps_nanosecond_precision(tmp_path: Path) -> None:
    first, second = _row("quote", 1), _row("depth", 2)
    first["recv_wall_ns"] += 2
    second["recv_wall_ns"] += 1
    with pytest.raises(ValueError):
        _read(_write(tmp_path / "ns.gz", first, second))


@pytest.mark.parametrize(("field", "value"), [
    ("local_seq", 1), ("local_seq", 0), ("local_seq", -1),
    ("local_seq", True), ("local_seq", 2.0), ("local_seq", "2"),
    ("recv_wall_ns", WALL_NS - 1), ("recv_mono_ns", 0),
])
def test_duplicate_sequence_and_backwards_receive_clocks_fail(
    tmp_path: Path, field: str, value: object,
) -> None:
    second = _row("depth", 2)
    second[field] = value
    with pytest.raises(ValueError):
        _read(_write(tmp_path / "order.gz", _row("quote", 1), second))


def test_out_of_order_sequence_and_reappearing_run_fail(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _read(_write(tmp_path / "seq.gz", _row("quote", 4), _row("depth", 3, 1)))
    with pytest.raises(ValueError):
        _read(_write(tmp_path / "runs.gz", _row("quote", 1),
                     _row("depth", 1, 1, run="run-b"), _row("quote", 2, 2)))


@pytest.mark.parametrize(("field", "value"), [
    ("schema_version", 2), ("schema_version", True), ("schema_version", "1"),
    ("run_id", ""), ("run_id", None), ("run_id", 42),
    ("symbol", "NVDA.US"), ("symbol", ""), ("kind", "ohlc"),
    ("recv_wall_ns", "2026-10-30"), ("recv_wall_ns", 10**30),
    ("recv_mono_ns", None), ("payload", []),
])
def test_invalid_identity_schema_and_receive_timestamps_fail(
    tmp_path: Path, field: str, value: object,
) -> None:
    row = _row("quote", 1)
    row[field] = value
    with pytest.raises(ValueError):
        _read(_write(tmp_path / "invalid.gz", row))


@pytest.mark.parametrize("field", [
    "schema_version", "run_id", "local_seq", "symbol", "kind",
    "recv_wall_ns", "recv_mono_ns", "payload",
])
def test_missing_identity_fails(tmp_path: Path, field: str) -> None:
    row = _row("quote", 1)
    del row[field]
    with pytest.raises(ValueError):
        _read(_write(tmp_path / "identity.gz", row))


@pytest.mark.parametrize("source", ["not-a-date", "2026-10-30", 123, True])
def test_invalid_source_timestamp_fails(tmp_path: Path, source: object) -> None:
    row = _row("quote", 1)
    row["payload"]["timestamp"] = source
    with pytest.raises(ValueError):
        _read(_write(tmp_path / "source.gz", row))


def test_missing_source_and_missing_zero_bbo_remain_represented(tmp_path: Path) -> None:
    quote = _row("quote", 1)
    quote["payload"] = {"last_done": None}
    depth = _row("depth", 2)
    depth["payload"] = {
        "bids": [{"position": 1, "price": "0"}],
        "asks": [{"position": 2, "price": "401", "volume": 100}],
    }
    (result,) = _read(_write(tmp_path / "missing.gz", quote, depth))
    assert result.source_at is None
    assert result.last is None
    assert result.bid == Decimal("0")
    assert result.ask is None
    assert result.bid_size is None
    assert result.ask_size is None


def test_missing_depth_sides_do_not_carry_forward_previous_bbo(tmp_path: Path) -> None:
    empty_depth = _row("depth", 3, 1)
    empty_depth["payload"] = {"bids": None, "asks": []}
    results = _read(_write(tmp_path / "clear.gz", _row("quote", 1),
                           _row("depth", 2), empty_depth))
    assert results[0].bid_size == 12
    assert results[1].bid is None and results[1].ask is None
    assert results[1].bid_size is None and results[1].ask_size is None


@pytest.mark.parametrize("kind", ["quote", "depth", "trade"])
@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", float("nan"), float("inf")])
def test_nonfinite_prices_fail_even_on_trade_rows(
    tmp_path: Path, kind: str, bad: object,
) -> None:
    row = _row(kind, 1)
    if kind == "quote":
        row["payload"]["last_done"] = bad
    elif kind == "depth":
        row["payload"]["bids"][0]["price"] = bad
    else:
        row["payload"]["trades"][0]["price"] = bad
    with pytest.raises(ValueError):
        _read(_write(tmp_path / "finite.gz", row))


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "1.5", True, -1])
def test_invalid_size_is_never_coerced(tmp_path: Path, bad: object) -> None:
    row = _row("depth", 1)
    row["payload"]["bids"][1]["volume"] = bad
    with pytest.raises(ValueError):
        _read(_write(tmp_path / "size.gz", row))


def test_duplicate_top_positions_are_ambiguous(tmp_path: Path) -> None:
    row = _row("depth", 1)
    row["payload"]["bids"][0]["position"] = 1
    with pytest.raises(ValueError):
        _read(_write(tmp_path / "levels.gz", row))


@pytest.mark.parametrize("raw", [b"not gzip", b"", b"\x1f\x8b", gzip.compress(b"{}\n")[:-4]])
def test_malformed_or_truncated_gzip_fails(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "broken.gz"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        _read(path)


@pytest.mark.parametrize("raw", [b"{\n", b"[]\n", b"\n", b"\xff\n",
                                  b'{"schema_version":1,"schema_version":2}\n'])
def test_malformed_json_and_duplicate_keys_fail(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "json.gz"
    path.write_bytes(gzip.compress(raw))
    with pytest.raises(ValueError):
        _read(path)


def test_concatenated_recorder_gzip_members_are_consumed(tmp_path: Path) -> None:
    path = tmp_path / "members.gz"
    path.write_bytes(
        gzip.compress((json.dumps(_row("quote", 1)) + "\n").encode(), mtime=0)
        + gzip.compress((json.dumps(_row("depth", 2)) + "\n").encode(), mtime=0)
    )
    stats = TapeReadStats()
    assert len(_read(path, stats=stats)) == 1
    assert stats.rows == 2
    assert stats.input_manifest[0]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("mutation", ["append", "replace", "metadata"])
def test_input_changes_during_iteration_fail_without_committing_manifest(
    tmp_path: Path, mutation: str,
) -> None:
    path = _write(tmp_path / "change.gz", _row("quote", 1), _row("depth", 2))
    stats = TapeReadStats()
    iterator = iter_tape_quotes(
        [path], symbol=SYMBOL, start_at=T0, end_at=T0 + timedelta(hours=1), stats=stats,
    )
    assert next(iterator).sequence == 2
    if mutation == "append":
        with path.open("ab") as stream:
            stream.write(gzip.compress(b""))
    elif mutation == "replace":
        replacement = _write(tmp_path / "replacement.gz", _row("quote", 1), _row("depth", 2))
        replacement.replace(path)
    else:
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
    with pytest.raises(ValueError):
        list(iterator)
    assert stats.input_manifest == []


def test_source_errors_do_not_disclose_paths_or_chained_environment_details(tmp_path: Path) -> None:
    secret_path = tmp_path / "credential-super-secret-token.jsonl.gz"
    for path in (secret_path, tmp_path):
        with pytest.raises(ValueError) as caught:
            _read(path)
        rendered = "".join(traceback.format_exception(caught.value))
        assert str(tmp_path) not in rendered
        assert "credential-super-secret-token" not in rendered


def test_corruption_after_window_is_not_silently_ignored(tmp_path: Path) -> None:
    bad = _row("quote", 3, 10)
    bad["schema_version"] = 999
    with pytest.raises(ValueError):
        _read(
            _write(tmp_path / "tail.gz", _row("quote", 1), _row("depth", 2), bad),
            end_at=T0 + timedelta(seconds=1),
        )
