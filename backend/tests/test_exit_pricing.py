from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.exit_pricing import (
    ReferencePrice,
    degraded_exit_limit,
    parse_quote_source_timestamp,
    select_reference_price,
)


@pytest.mark.parametrize("side", ["LONG", "SELL", "SHORT", "BUY_TO_COVER"])
def test_select_reference_price_prefers_newest_trusted_quote(side: str) -> None:
    # Given: history is appended oldest to newest, as in the runner.
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    older = now - timedelta(seconds=20)
    newest = now - timedelta(seconds=1)
    quotes = [
        {"bid": 99.0, "ask": 100.0, "timestamp": older, "observed_at": older, "trusted": True},
        {"bid": 101.0, "ask": 102.0, "timestamp": newest, "observed_at": now, "trusted": True},
    ]
    # When
    result = select_reference_price(quotes, side=side, now=now, max_age_seconds=300)
    # Then
    is_long = side in ("LONG", "SELL")
    assert result == ReferencePrice(101.0 if is_long else 102.0, "trusted_bid" if is_long else "trusted_ask", newest, now)


@pytest.mark.parametrize("trusted", [False, 1, "true", None])
def test_select_reference_price_ignores_untrusted_entries(trusted: object) -> None:
    # Given
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    older = now - timedelta(seconds=20)
    quotes = [
        {"bid": 99.0, "timestamp": older, "observed_at": older, "trusted": True},
        {"bid": 101.0, "timestamp": now, "observed_at": now, "trusted": trusted},
    ]
    # When
    result = select_reference_price(quotes, side="LONG", now=now, max_age_seconds=300)
    # Then
    assert result == ReferencePrice(99.0, "trusted_bid", older, older)


@pytest.mark.parametrize("age,accepted", [(301, False), (299, True), (300, True), (-5, True)])
def test_select_reference_price_expires_after_max_age(age: int, accepted: bool) -> None:
    # Given: a fresh observation must not rejuvenate the source timestamp.
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    source = now - timedelta(seconds=age)
    quotes = [{"bid": 100.0, "timestamp": source, "observed_at": now, "trusted": True}]
    # When
    result = select_reference_price(quotes, side="LONG", now=now, max_age_seconds=300)
    # Then
    assert result == (ReferencePrice(100.0, "trusted_bid", source, now) if accepted else None)


@pytest.mark.parametrize("timestamp", ["", "garbage", "2026-09-20T00:00:10Z"])
def test_select_reference_price_rejects_unparseable_or_future_timestamp(timestamp: str) -> None:
    # Given
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    quotes = [{"bid": 100.0, "timestamp": timestamp, "observed_at": now, "trusted": True}]
    # When
    result = select_reference_price(quotes, side="LONG", now=now, max_age_seconds=300)
    # Then
    assert result is None


@pytest.mark.parametrize("price", [float("inf"), float("nan"), 0.0, -1.0, None, "garbage"])
def test_select_reference_price_rejects_non_finite_price(price: object) -> None:
    # Given
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    quotes = [
        {"bid": 99.0, "timestamp": now, "observed_at": now, "trusted": True},
        {"bid": price, "timestamp": now, "observed_at": now, "trusted": True},
    ]
    # When
    result = select_reference_price(quotes, side="LONG", now=now, max_age_seconds=300)
    # Then
    assert result == ReferencePrice(99.0, "trusted_bid", now, now)


def test_select_reference_price_handles_missing_fields() -> None:
    # Given: missing observation time falls back to source time, never wall time.
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    quotes = [{"bid": 99.0, "timestamp": now, "trusted": True}, {}, {"trusted": True}]
    # When
    result = select_reference_price(quotes, side="LONG", now=now, max_age_seconds=300)
    # Then
    assert result == ReferencePrice(99.0, "trusted_bid", now, now)


def test_floor_rejects_trv_bid() -> None:
    # Given: the production quote that exposed the degraded-book defect.
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    reference = ReferencePrice(370.582, "trusted_bid", now, now)
    # When
    result = degraded_exit_limit(side="LONG", bid=358.35, ask=371.0, reference=reference, max_adverse_deviation_pct=0.5)
    # Then
    assert result is not None
    assert result.marketable is False
    assert result.limit_price == pytest.approx(368.7291, abs=1e-4)
    assert result.floor_price == pytest.approx(368.7291, abs=1e-4)
    assert result.reference is reference


@pytest.mark.parametrize("side", ["LONG", "SELL"])
def test_floor_passes_normal_spread(side: str) -> None:
    # Given
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    reference = ReferencePrice(100.4, "trusted_bid", now, now)
    # When
    result = degraded_exit_limit(side=side, bid=100.39, ask=100.41, reference=reference, max_adverse_deviation_pct=0.5)
    # Then
    assert result is not None
    assert result.marketable is True
    assert result.limit_price == pytest.approx(100.39)


@pytest.mark.parametrize("bid", [float("nan"), 0.0, float("inf"), -1.0])
def test_invalid_bid_returns_none_not_floor(bid: float) -> None:
    # Given
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    reference = ReferencePrice(100.0, "trusted_bid", now, now)
    # When
    result = degraded_exit_limit(side="LONG", bid=bid, ask=101.0, reference=reference, max_adverse_deviation_pct=0.5)
    # Then
    assert result is None


@pytest.mark.parametrize("side", ["SHORT", "BUY_TO_COVER"])
@pytest.mark.parametrize("ask,limit,marketable", [(102.0, 100.5, False), (100.1, 100.1, True), (100.0 * 1.005, 100.5, True)])
def test_short_side_mirrors_the_floor(side: str, ask: float, limit: float, marketable: bool) -> None:
    # Given
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    reference = ReferencePrice(100.0, "trusted_ask", now, now)
    # When
    result = degraded_exit_limit(side=side, bid=float("nan"), ask=ask, reference=reference, max_adverse_deviation_pct=0.5)
    # Then
    assert result is not None
    assert result.floor_price == pytest.approx(100.5)
    assert result.limit_price == pytest.approx(limit)
    assert result.marketable is marketable


@pytest.mark.parametrize("price,pct", [(float("inf"), 0.5), (float("nan"), 0.5), (0.0, 0.5), (-1.0, 0.5), (100.0, float("inf")), (100.0, float("nan")), (100.0, 0.0), (100.0, -1.0)])
def test_floor_rejects_invalid_reference_or_deviation(price: float, pct: float) -> None:
    # Given
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    reference = ReferencePrice(price, "trusted_bid", now, now)
    # When
    result = degraded_exit_limit(side="LONG", bid=100.0, ask=101.0, reference=reference, max_adverse_deviation_pct=pct)
    # Then
    assert result is None


@pytest.mark.parametrize("value", ["1726790400", "1726790400000", "1726790400.0", "2024-09-20T00:00:00Z", "2024-09-20T00:00:00", "2024-09-20T08:00:00+08:00", datetime(2024, 9, 20), datetime(2024, 9, 20, tzinfo=timezone.utc), "", " ", "garbage", None, "9" * 400])
def test_parse_quote_source_timestamp_handles_epoch_iso_and_garbage(value: object) -> None:
    # Given
    expected = None if value in ("", " ", "garbage", None, "9" * 400) else datetime(2024, 9, 20, tzinfo=timezone.utc)
    # When
    result = parse_quote_source_timestamp(value)
    # Then
    assert result == expected
