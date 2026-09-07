from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
from threading import Barrier

import pytest

from app.core.board_lot import BoardLotCache, BoardLotResolution, quantize_to_board_lot


@pytest.mark.parametrize("quantity,lot,expected", [
    ("1250", 500, "1000"), ("499", 500, "0"), ("1", 1, "1"),
    ("999.999", 500, "500"), ("0", 500, "0"),
])
def test_quantize_floors_when_quantity_is_valid(quantity: str, lot: int, expected: str) -> None:
    # Given / When / Then
    assert quantize_to_board_lot(Decimal(quantity), lot) == Decimal(expected)


@pytest.mark.parametrize("quantity,lot", [
    ("1", 0), ("1", -1), ("-1", 500), ("NaN", 500),
    ("sNaN", 500), ("Infinity", 500), ("-Infinity", 500),
])
def test_quantize_rejects_when_input_is_invalid(quantity: str, lot: int) -> None:
    # Given / When / Then
    with pytest.raises(ValueError):
        quantize_to_board_lot(Decimal(quantity), lot)


def test_quantize_never_rounds_up_when_decimal_context_is_small() -> None:
    # Given
    with localcontext() as context:
        context.prec = 3
        # When / Then
        assert quantize_to_board_lot(Decimal("999.999"), 500) == Decimal("500")


def test_us_resolves_without_session_lookup() -> None:
    # Given
    def forbidden_lookup(market: str) -> date:
        raise AssertionError(f"unexpected lookup: {market}")
    cache = BoardLotCache(session_day_for=forbidden_lookup)
    # When / Then
    assert cache.resolve("AAPL.US") == BoardLotResolution("AAPL.US", 1, "FRESH")


@pytest.mark.parametrize("symbol,lot,source", [
    ("AAPL.US", 1, "FRESH"), ("0700.HK", None, "UNKNOWN"),
])
def test_unresolved_defaults_when_no_metadata(symbol: str, lot: int | None, source: str) -> None:
    # Given / When
    result = BoardLotResolution.for_unresolved(symbol)
    # Then
    assert (result.symbol, result.lot_size, result.source) == (symbol, lot, source)


def test_hk_is_unknown_when_not_cached() -> None:
    # Given
    cache = BoardLotCache(session_day_for=lambda _: date(2026, 9, 8))
    # When / Then
    assert cache.resolve("0700.HK") == BoardLotResolution("0700.HK", None, "UNKNOWN")


@pytest.mark.parametrize("in_rth", [True, False])
def test_hk_expires_when_session_changes(in_rth: bool) -> None:
    # Given
    today = [date(2026, 9, 8)]
    markets: list[str] = []
    def session_day(market: str) -> date:
        markets.append(market)
        return today[0]
    cache = BoardLotCache(session_day_for=session_day)
    cache.put("0700.HK", 500, session_day=today[0], in_rth=in_rth)
    assert cache.resolve("0700.HK") == BoardLotResolution(
        "0700.HK", 500, "FRESH", validated_for_session=today[0])
    # When
    today[0] = date(2026, 9, 9)
    result = cache.resolve("0700.HK")
    # Then
    assert result == BoardLotResolution("0700.HK", None, "STALE", 500, date(2026, 9, 8))
    assert markets == ["HK", "HK"]


@pytest.mark.parametrize("lot", [0, -500])
def test_put_rejects_when_lot_is_nonpositive(lot: int) -> None:
    # Given
    cache = BoardLotCache()
    # When / Then
    with pytest.raises(ValueError):
        cache.put("0700.HK", lot, session_day=date(2026, 9, 8), in_rth=True)


def test_missing_or_stale_returns_only_unresolved_hk() -> None:
    # Given
    today = date(2026, 9, 8)
    cache = BoardLotCache(session_day_for=lambda _: today)
    cache.put("0700.HK", 500, session_day=today, in_rth=True)
    cache.put("0005.HK", 400, session_day=date(2026, 9, 7), in_rth=False)
    # When / Then
    assert cache.missing_or_stale_hk(iter(["AAPL.US", "0700.HK", "0005.HK", "9988.HK"])) == [
        "0005.HK", "9988.HK"]


def test_snapshot_preserves_metadata_and_is_detached() -> None:
    # Given
    today = date(2026, 9, 8)
    observed = datetime(2026, 9, 8, tzinfo=timezone.utc)
    cache = BoardLotCache(session_day_for=lambda _: today)
    cache.put("0700.HK", 500, session_day=today, in_rth=False, observed_at=observed)
    # When
    snapshot = cache.snapshot()
    # Then
    assert snapshot == [{"symbol": "0700.HK", "lot_size": 500,
        "validated_for_session": today.isoformat(), "observed_at": observed.isoformat(),
        "validated_in_rth": False}]
    snapshot[0]["lot_size"] = 1
    assert cache.resolve("0700.HK").lot_size == 500


def test_cache_is_thread_safe_when_put_and_resolve_overlap() -> None:
    # Given
    today = date(2026, 9, 8)
    cache = BoardLotCache(session_day_for=lambda _: today)
    barrier = Barrier(2)
    def exercise(lot: int) -> None:
        barrier.wait(timeout=5)
        for _ in range(100):
            cache.put("0700.HK", lot, session_day=today, in_rth=True)
            assert cache.resolve("0700.HK").lot_size in (100, 500)
            cache.snapshot()
    # When / Then: future.result propagates worker exceptions.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(exercise, lot) for lot in (100, 500)]
        for future in futures:
            future.result(timeout=10)
