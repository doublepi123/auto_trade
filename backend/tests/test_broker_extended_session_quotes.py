"""Pulled quotes must carry the freshest extended-session last trade.

A live probe on 2026-10-06 showed ``SecurityQuote.last_done`` /
``timestamp`` stay at the prior RTH close during PRE/POST, while the live
price lives on ``pre_market_quote`` / ``post_market_quote``. Selection is
by the latest SDK timestamp among main / pre / post. Overnight is ignored.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.broker import BrokerGateway, Quote


class _SubQuote:
    def __init__(
        self,
        last_done: object,
        timestamp: object,
    ) -> None:
        self.last_done = last_done
        self.timestamp = timestamp


class _QuoteItem:
    def __init__(
        self,
        symbol: str,
        *,
        last_done: object = 100.0,
        timestamp: object = datetime(2026, 10, 5, 16, 0, 0),
        bid: object = 99.5,
        ask: object = 100.5,
        pre_market_quote: object = None,
        post_market_quote: object = None,
        overnight_quote: object = None,
    ) -> None:
        self.symbol = symbol
        self.last_done = last_done
        self.timestamp = timestamp
        self.bid = bid
        self.ask = ask
        self.pre_market_quote = pre_market_quote
        self.post_market_quote = post_market_quote
        self.overnight_quote = overnight_quote


class _QuoteContext:
    def __init__(self, items: list[_QuoteItem]) -> None:
        self.items = items
        self.calls: list[list[str]] = []
        self.depth_calls = 0

    def quote(self, symbols: list[str]) -> list[_QuoteItem]:
        self.calls.append(list(symbols))
        return self.items

    def depth(self, _symbol: str) -> object:
        self.depth_calls += 1
        raise AssertionError("extended-session selection must not pull depth")


def _gateway(items: list[_QuoteItem]) -> tuple[BrokerGateway, _QuoteContext]:
    context = _QuoteContext(items)
    gateway = BrokerGateway()
    gateway._quote_ctx = context
    gateway._trade_ctx = object()
    return gateway, context


def test_pre_fresher_than_main_selects_pre_price_and_timestamp() -> None:
    main_ts = datetime(2026, 10, 5, 16, 0, 0)
    pre_ts = datetime(2026, 10, 6, 4, 13, 0)
    gateway, context = _gateway([
        _QuoteItem(
            "TSLA.US",
            last_done=Decimal("378.730"),
            timestamp=main_ts,
            pre_market_quote=_SubQuote(Decimal("381.000"), pre_ts),
            post_market_quote=_SubQuote(
                Decimal("379.750"),
                datetime(2026, 10, 5, 19, 59, 57),
            ),
        ),
    ])

    result = gateway.get_quotes(["TSLA.US"])

    assert result == [
        Quote("TSLA.US", 381.0, 99.5, 100.5, str(pre_ts)),
    ]
    assert gateway._last_trade_by_symbol["TSLA.US"] == (381.0, str(pre_ts))
    assert context.calls == [["TSLA.US"]]
    assert context.depth_calls == 0


def test_post_fresher_than_main_selects_post() -> None:
    post_ts = datetime(2026, 10, 6, 16, 12, 0)
    gateway, _context = _gateway([
        _QuoteItem(
            "TSLA.US",
            last_done=378.73,
            timestamp=datetime(2026, 10, 6, 16, 0, 0),
            post_market_quote=_SubQuote(Decimal("379.750"), post_ts),
        ),
    ])

    result = gateway.get_quotes(["TSLA.US"])

    assert result[0].last_price == 379.75
    assert result[0].timestamp == str(post_ts)
    assert gateway._last_trade_by_symbol["TSLA.US"] == (379.75, str(post_ts))


def test_main_fresher_during_rth_keeps_main() -> None:
    main_ts = datetime(2026, 10, 6, 10, 0, 0)
    gateway, _context = _gateway([
        _QuoteItem(
            "TSLA.US",
            last_done=380.25,
            timestamp=main_ts,
            pre_market_quote=_SubQuote(
                Decimal("381.000"),
                datetime(2026, 10, 6, 9, 29, 0),
            ),
            post_market_quote=_SubQuote(
                Decimal("379.750"),
                datetime(2026, 10, 5, 19, 59, 0),
            ),
        ),
    ])

    result = gateway.get_quotes(["TSLA.US"])

    assert result[0].last_price == 380.25
    assert result[0].timestamp == str(main_ts)
    assert gateway._last_trade_by_symbol["TSLA.US"] == (380.25, str(main_ts))


def test_equal_timestamps_prefer_main() -> None:
    tied = datetime(2026, 10, 6, 9, 30, 0)
    gateway, _context = _gateway([
        _QuoteItem(
            "TSLA.US",
            last_done=380.0,
            timestamp=tied,
            pre_market_quote=_SubQuote(Decimal("381.000"), tied),
            post_market_quote=_SubQuote(Decimal("379.000"), tied),
        ),
    ])

    result = gateway.get_quotes(["TSLA.US"])

    assert result[0].last_price == 380.0
    assert result[0].timestamp == str(tied)


def test_missing_subquotes_zero_negative_and_none_timestamps_are_ignored() -> None:
    main_ts = datetime(2026, 10, 5, 16, 0, 0)
    gateway, _context = _gateway([
        _QuoteItem(
            "TSLA.US",
            last_done=378.73,
            timestamp=main_ts,
            pre_market_quote=_SubQuote(Decimal("0"), datetime(2026, 10, 6, 5, 0)),
            post_market_quote=_SubQuote(Decimal("-1"), datetime(2026, 10, 6, 17, 0)),
            overnight_quote=_SubQuote(None, None),
        ),
        _QuoteItem(
            "NVDA.US",
            last_done=0,
            timestamp=None,
            pre_market_quote=_SubQuote(Decimal("181.5"), None),
        ),
    ])

    result = gateway.get_quotes(["TSLA.US", "NVDA.US"])

    by_symbol = {quote.symbol: quote for quote in result}
    assert by_symbol["TSLA.US"].last_price == 378.73
    assert by_symbol["TSLA.US"].timestamp == str(main_ts)
    assert by_symbol["NVDA.US"].last_price == 0.0
    assert by_symbol["NVDA.US"].timestamp == "None"
    assert "NVDA.US" not in gateway._last_trade_by_symbol
    assert gateway._last_trade_by_symbol["TSLA.US"] == (378.73, str(main_ts))


def test_overnight_quote_is_selected_when_freshest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings as live_settings

    object.__setattr__(live_settings, "overnight_trading_effective", lambda: True)
    main_ts = datetime(2026, 10, 5, 16, 0, 0)
    gateway, _context = _gateway([
        _QuoteItem(
            "TSLA.US",
            last_done=378.73,
            timestamp=main_ts,
            overnight_quote=_SubQuote(
                Decimal("390.000"),
                datetime(2026, 10, 6, 22, 0, 0),
            ),
        ),
    ])

    result = gateway.get_quotes(["TSLA.US"])

    overnight_ts = datetime(2026, 10, 6, 22, 0, 0)
    assert result[0].last_price == 390.0
    assert result[0].timestamp == str(overnight_ts)
    assert gateway._last_trade_by_symbol["TSLA.US"] == (390.0, str(overnight_ts))


def test_mixed_timestamp_types_skip_the_incomparable_subquote() -> None:
    main_ts = datetime(2026, 10, 5, 16, 0, 0)
    gateway, _context = _gateway([
        _QuoteItem(
            "TSLA.US",
            last_done=378.73,
            timestamp=main_ts,
            pre_market_quote=_SubQuote(Decimal("381.000"), "2026-10-06 04:13:00"),
        ),
    ])

    result = gateway.get_quotes(["TSLA.US"])

    assert result[0].last_price == 378.73
    assert result[0].timestamp == str(main_ts)
    assert gateway._last_trade_by_symbol["TSLA.US"] == (378.73, str(main_ts))


def test_hk_symbol_without_subquotes_is_unchanged() -> None:
    gateway, _context = _gateway([
        _QuoteItem(
            "0700.HK",
            last_done=400.0,
            timestamp="2026-10-06T09:30:00",
            bid=399.8,
            ask=400.2,
        ),
    ])

    result = gateway.get_quotes(["0700.HK"])

    assert result == [
        Quote("0700.HK", 400.0, 399.8, 400.2, "2026-10-06T09:30:00"),
    ]
    assert gateway._last_trade_by_symbol["0700.HK"] == (
        400.0,
        "2026-10-06T09:30:00",
    )


def test_multi_symbol_call_selects_per_symbol() -> None:
    pre_ts = datetime(2026, 10, 6, 5, 1, 0)
    post_ts = datetime(2026, 10, 6, 16, 5, 0)
    main_ts = datetime(2026, 10, 6, 11, 0, 0)
    gateway, context = _gateway([
        _QuoteItem(
            "TSLA.US",
            last_done=378.73,
            timestamp=datetime(2026, 10, 5, 16, 0, 0),
            pre_market_quote=_SubQuote(Decimal("381.2"), pre_ts),
        ),
        _QuoteItem(
            "NVDA.US",
            last_done=180.0,
            timestamp=datetime(2026, 10, 6, 16, 0, 0),
            post_market_quote=_SubQuote(Decimal("181.4"), post_ts),
        ),
        _QuoteItem(
            "AAPL.US",
            last_done=225.5,
            timestamp=main_ts,
            pre_market_quote=_SubQuote(
                Decimal("224.0"),
                datetime(2026, 10, 6, 9, 29, 0),
            ),
        ),
    ])

    result = gateway.get_quotes(["TSLA.US", "NVDA.US", "AAPL.US"])

    by_symbol = {quote.symbol: quote for quote in result}
    assert by_symbol["TSLA.US"].last_price == 381.2
    assert by_symbol["TSLA.US"].timestamp == str(pre_ts)
    assert by_symbol["NVDA.US"].last_price == 181.4
    assert by_symbol["NVDA.US"].timestamp == str(post_ts)
    assert by_symbol["AAPL.US"].last_price == 225.5
    assert by_symbol["AAPL.US"].timestamp == str(main_ts)
    assert gateway._last_trade_by_symbol["TSLA.US"] == (381.2, str(pre_ts))
    assert gateway._last_trade_by_symbol["NVDA.US"] == (181.4, str(post_ts))
    assert gateway._last_trade_by_symbol["AAPL.US"] == (225.5, str(main_ts))
    # Bid/ask stay on the main item; no extra network call.
    assert all(quote.bid == 99.5 and quote.ask == 100.5 for quote in result)
    assert context.calls == [["TSLA.US", "NVDA.US", "AAPL.US"]]
    assert context.depth_calls == 0
