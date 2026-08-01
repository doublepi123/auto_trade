from __future__ import annotations

import inspect
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.services import watchlist_quant_v6_historical_provider as provider_module
from app.services.watchlist_quant_v6_historical_provider import (
    QUANT_V6_HISTORICAL_ADJUSTMENT_MODE,
    QUANT_V6_HISTORICAL_PAGE_TIMEOUT_MILLISECONDS,
    QUANT_V6_HISTORICAL_PERIOD,
    QUANT_V6_HISTORICAL_RETRY_BASE_MILLISECONDS,
    QUANT_V6_HISTORICAL_RETRY_MAX,
    QuantV6HistoricalBarProvider,
    QuantV6HistoricalProviderError,
    quant_v6_historical_provider_contract,
    quant_v6_historical_provider_digest_sha256,
)
from app.domain.watchlist_quant_v6 import (
    QUANT_V6_ACQUISITION_SPEC_DIGEST,
    canonical_quant_v6_json,
)


class _Candle:
    def __init__(
        self,
        timestamp: datetime,
        *,
        opened: object = "100",
        high: object = "101",
        low: object = "99",
        close: object = "100.5",
        volume: object = "1000",
    ) -> None:
        self.timestamp = timestamp
        self.open = opened
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


def _module(pages: list[list[_Candle]], *, no_adjust: bool = True):
    class Config:
        @staticmethod
        def from_env() -> object:
            return object()

    class Period:
        Min_5 = "MIN_5_ENUM"

    class AdjustType:
        ForwardAdjust = "FORWARD_ENUM"

    if no_adjust:
        setattr(AdjustType, "NoAdjust", "NO_ADJUST_ENUM")

    class QuoteContext:
        instances: list[QuoteContext] = []

        def __init__(self, _config: object) -> None:
            self.calls: list[tuple[Any, ...]] = []
            self.closed = False
            QuoteContext.instances.append(self)

        def history_candlesticks_by_offset(self, *args: Any):
            self.calls.append(args)
            return pages.pop(0) if pages else []

        def close(self) -> None:
            self.closed = True

    class TradeContext:
        def __init__(self, _config: object) -> None:
            raise AssertionError("the quote-only provider created order authority")

    module = SimpleNamespace(
        Config=Config,
        Period=Period,
        AdjustType=AdjustType,
        QuoteContext=QuoteContext,
        TradeContext=TradeContext,
    )
    return module, QuoteContext


def test_provider_is_quote_only_no_adjust_and_pages_beyond_3000() -> None:
    start = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
    pages: list[list[_Candle]] = []
    for page_index in range(4):
        page_start = start + timedelta(minutes=5 * page_index * 900)
        pages.append([
            _Candle(page_start + timedelta(minutes=5 * index))
            for index in range(900)
        ])
    module, quote_context_type = _module(pages)
    provider = QuantV6HistoricalBarProvider(module_loader=lambda: module)

    result = provider.fetch_five_minute_no_adjust(
        "AAPL.US",
        start_at=start,
        end_at=start + timedelta(minutes=5 * 3_200),
    )

    assert len(result.bars) == 3_200
    assert result.pages == 4
    assert result.rejected_rows == 0
    context = quote_context_type.instances[0]
    assert len(context.calls) == 4
    assert all(call[1:5] == ("MIN_5_ENUM", "NO_ADJUST_ENUM", True, 1_000) for call in context.calls)
    assert context.calls[0][5] == (start - timedelta(minutes=5)).astimezone(
        ZoneInfo("America/New_York")
    )
    assert not hasattr(provider, "get_quotes")
    assert not hasattr(provider, "get_positions")
    assert not hasattr(provider, "submit_order")


def test_provider_never_falls_back_when_no_adjust_is_unavailable() -> None:
    module, quote_context_type = _module([], no_adjust=False)
    provider = QuantV6HistoricalBarProvider(module_loader=lambda: module)
    with pytest.raises(QuantV6HistoricalProviderError, match="NoAdjust"):
        provider.fetch_five_minute_no_adjust(
            "AAPL.US",
            start_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            end_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )
    assert quote_context_type.instances[0].calls == []


def test_provider_preserves_bad_grid_evidence_as_rejected_rows() -> None:
    start = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
    pages = [[
        _Candle(start),
        _Candle(start + timedelta(minutes=5), low="102"),
        _Candle(start + timedelta(minutes=10)),
    ]]
    module, _quote_context_type = _module(pages)
    result = QuantV6HistoricalBarProvider(
        module_loader=lambda: module,
    ).fetch_five_minute_no_adjust(
        "AAPL.US",
        start_at=start,
        end_at=start + timedelta(minutes=15),
    )
    assert [bar.start_at for bar in result.bars] == [
        start,
        start + timedelta(minutes=10),
    ]
    assert result.raw_rows == 3
    assert result.rejected_rows == 1


def test_provider_interprets_naive_sdk_timestamps_under_utc_contract() -> None:
    start = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
    pages = [[
        _Candle(start.replace(tzinfo=None)),
        _Candle(start + timedelta(minutes=5)),
    ]]
    module, _quote_context_type = _module(pages)

    result = QuantV6HistoricalBarProvider(
        module_loader=lambda: module,
    ).fetch_five_minute_no_adjust(
        "AAPL.US",
        start_at=start,
        end_at=start + timedelta(minutes=10),
    )

    assert [bar.start_at for bar in result.bars] == [
        start,
        start + timedelta(minutes=5),
    ]
    assert result.raw_rows == 2
    assert result.rejected_rows == 0


def test_provider_rejects_non_utc_process_timezone_before_sdk_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, quote_context_type = _module([])
    monkeypatch.setattr(
        provider_module,
        "_runtime_local_timezone_is_utc",
        lambda: False,
    )
    provider = QuantV6HistoricalBarProvider(module_loader=lambda: module)

    with pytest.raises(QuantV6HistoricalProviderError, match="UTC process"):
        provider.fetch_five_minute_no_adjust(
            "AAPL.US",
            start_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            end_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )

    assert quote_context_type.instances == []


def test_provider_sorts_rows_and_rejects_duplicate_or_oversized_pages() -> None:
    start = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
    module, _quote_context_type = _module([[
        _Candle(start + timedelta(minutes=5)),
        _Candle(start),
    ]])
    result = QuantV6HistoricalBarProvider(
        module_loader=lambda: module,
    ).fetch_five_minute_no_adjust(
        "AAPL.US",
        start_at=start,
        end_at=start + timedelta(minutes=10),
    )
    assert [bar.start_at for bar in result.bars] == [
        start,
        start + timedelta(minutes=5),
    ]

    duplicate_module, _duplicate_context_type = _module([[
        _Candle(start),
        _Candle(start),
    ]])
    with pytest.raises(
        QuantV6HistoricalProviderError,
        match="duplicate timestamps",
    ):
        QuantV6HistoricalBarProvider(
            module_loader=lambda: duplicate_module,
        ).fetch_five_minute_no_adjust(
            "AAPL.US",
            start_at=start,
            end_at=start + timedelta(minutes=5),
        )

    oversized_module, _oversized_context_type = _module([[
        _Candle(start + timedelta(minutes=5 * index))
        for index in range(1_001)
    ]])
    with pytest.raises(
        QuantV6HistoricalProviderError,
        match="page exceeds the row limit",
    ):
        QuantV6HistoricalBarProvider(
            module_loader=lambda: oversized_module,
        ).fetch_five_minute_no_adjust(
            "AAPL.US",
            start_at=start,
            end_at=start + timedelta(days=4),
        )


def test_provider_rejects_nonadvancing_pages_and_closes_context() -> None:
    start = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
    pages = [[_Candle(start - timedelta(minutes=5))]]
    module, quote_context_type = _module(pages)
    provider = QuantV6HistoricalBarProvider(module_loader=lambda: module)
    with pytest.raises(QuantV6HistoricalProviderError, match="did not advance"):
        provider.fetch_five_minute_no_adjust(
            "AAPL.US",
            start_at=start,
            end_at=start + timedelta(minutes=5),
        )
    provider.close()
    assert quote_context_type.instances[0].closed is True


def test_provider_contract_and_source_exclude_order_capabilities() -> None:
    contract = quant_v6_historical_provider_contract()
    assert contract["period"] == QUANT_V6_HISTORICAL_PERIOD
    assert contract["adjustment_mode"] == QUANT_V6_HISTORICAL_ADJUSTMENT_MODE
    assert contract["fallback_allowed"] is False
    assert contract["quote_context_only"] is True
    assert contract["acquisition_spec_sha256"] == (
        QUANT_V6_ACQUISITION_SPEC_DIGEST
    )
    assert contract["page_timeout_milliseconds"] == (
        QUANT_V6_HISTORICAL_PAGE_TIMEOUT_MILLISECONDS
    )
    assert isinstance(contract["page_timeout_milliseconds"], int)
    assert contract["retry_base_milliseconds"] == (
        QUANT_V6_HISTORICAL_RETRY_BASE_MILLISECONDS
    )
    assert contract["retry_max"] == QUANT_V6_HISTORICAL_RETRY_MAX
    assert canonical_quant_v6_json(contract)
    assert len(quant_v6_historical_provider_digest_sha256()) == 64
    source = inspect.getsource(provider_module)
    assert "app.core.broker" not in source
    assert "get_runner" not in source
    assert "BrokerGateway" not in source
    assert "submit_order" not in source
    assert "TradeContext" not in source


def test_provider_hard_times_out_a_hanging_sdk_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    module, quote_context_type = _module([])

    def hanging_reader(self: object, *args: object) -> list[_Candle]:
        del self, args
        started.set()
        try:
            release.wait(2)
            return []
        finally:
            finished.set()

    monkeypatch.setattr(
        quote_context_type,
        "history_candlesticks_by_offset",
        hanging_reader,
    )
    monkeypatch.setattr(
        provider_module,
        "QUANT_V6_HISTORICAL_PAGE_TIMEOUT_MILLISECONDS",
        50,
    )
    provider = QuantV6HistoricalBarProvider(module_loader=lambda: module)
    began_at = time.monotonic()
    try:
        with pytest.raises(QuantV6HistoricalProviderError, match="timed out"):
            provider.fetch_five_minute_no_adjust(
                "AAPL.US",
                start_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                end_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
            )
        assert started.is_set()
        assert time.monotonic() - began_at < 1
        second_module, _second_context_type = _module([])
        with pytest.raises(
            QuantV6HistoricalProviderError,
            match="previous.*still running",
        ):
            QuantV6HistoricalBarProvider(
                module_loader=lambda: second_module,
            ).fetch_five_minute_no_adjust(
                "MSFT.US",
                start_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                end_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
            )
        provider.close()
        assert quote_context_type.instances[0].closed is False
    finally:
        release.set()
        assert finished.wait(1)
        assert provider_module._SDK_CALL_SLOT.acquire(timeout=1)
        provider_module._SDK_CALL_SLOT.release()


def test_provider_hard_times_out_hanging_context_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    module, _quote_context_type = _module([])

    class HangingQuoteContext:
        def __init__(self, _config: object) -> None:
            started.set()
            try:
                release.wait(2)
            finally:
                finished.set()

    monkeypatch.setattr(module, "QuoteContext", HangingQuoteContext)
    monkeypatch.setattr(
        provider_module,
        "QUANT_V6_HISTORICAL_PAGE_TIMEOUT_MILLISECONDS",
        50,
    )
    provider = QuantV6HistoricalBarProvider(module_loader=lambda: module)
    try:
        with pytest.raises(
            QuantV6HistoricalProviderError,
            match="context creation timed out",
        ):
            provider.fetch_five_minute_no_adjust(
                "AAPL.US",
                start_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                end_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
            )
        assert started.is_set()
    finally:
        release.set()
        assert finished.wait(1)
        assert provider_module._SDK_CALL_SLOT.acquire(timeout=1)
        provider_module._SDK_CALL_SLOT.release()


def test_provider_cancels_a_hanging_sdk_page_before_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    cancelled = threading.Event()
    module, quote_context_type = _module([])

    def hanging_reader(self: object, *args: object) -> list[_Candle]:
        del self, args
        started.set()
        try:
            release.wait(2)
            return []
        finally:
            finished.set()

    def cancel_after_start() -> None:
        if started.wait(1):
            cancelled.set()

    monkeypatch.setattr(
        quote_context_type,
        "history_candlesticks_by_offset",
        hanging_reader,
    )
    provider = QuantV6HistoricalBarProvider(
        module_loader=lambda: module,
        cancel_event=cancelled,
    )
    canceller = threading.Thread(target=cancel_after_start, daemon=True)
    canceller.start()
    began_at = time.monotonic()
    try:
        with pytest.raises(QuantV6HistoricalProviderError, match="cancelled"):
            provider.fetch_five_minute_no_adjust(
                "AAPL.US",
                start_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                end_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
            )
        assert time.monotonic() - began_at < 1
        provider.close()
        assert quote_context_type.instances[0].closed is False
    finally:
        release.set()
        assert finished.wait(1)
        assert provider_module._SDK_CALL_SLOT.acquire(timeout=1)
        provider_module._SDK_CALL_SLOT.release()
        canceller.join(1)
