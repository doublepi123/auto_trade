from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from app.services import historical_order_completeness_reader as reader_module
from app.services.historical_order_completeness_reader import (
    HistoricalCompletenessError,
    HistoricalPayloadError,
    HistoricalTransportError,
    LongportHistoricalCompletenessReader,
    build_longport_historical_reader_from_env,
)


_FINGERPRINT = "a" * 64
_START = datetime(2026, 5, 21, 17, 0, tzinfo=timezone.utc)
_END = datetime(2026, 5, 23, 0, 0, tzinfo=timezone.utc)
_OBSERVED = datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc)


class _FakeTransport:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str) -> object:
        self.calls.append((method, path))
        if not self.responses:
            raise AssertionError("unexpected transport request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _timestamp(hour: int, minute: int, second: int = 0, *, day: int = 21) -> str:
    value = datetime(
        2026,
        5,
        day,
        hour,
        minute,
        second,
        tzinfo=timezone.utc,
    )
    return str(int(value.timestamp()))


def _order(
    order_id: str,
    side: str,
    quantity: str,
    price: str,
    submitted_at: str,
    updated_at: str,
    *,
    status: str = "FilledStatus",
    executed_quantity: str | None = None,
    executed_price: str | None = None,
) -> dict[str, object]:
    return {
        "order_id": order_id,
        "symbol": "NVDA.US",
        "side": side,
        "status": status,
        "quantity": quantity,
        "price": price,
        "executed_quantity": (
            quantity if executed_quantity is None else executed_quantity
        ),
        "executed_price": price if executed_price is None else executed_price,
        "submitted_at": submitted_at,
        "updated_at": updated_at,
        "currency": "USD",
    }


def _execution(
    order_id: str,
    trade_id: str,
    quantity: str,
    price: str,
    trade_done_at: str,
) -> dict[str, object]:
    return {
        "order_id": order_id,
        "trade_id": trade_id,
        "symbol": "NVDA.US",
        "quantity": quantity,
        "price": price,
        "trade_done_at": trade_done_at,
    }


def _complete_chain_payloads(
) -> tuple[dict[str, object], dict[str, object]]:
    orders = [
        _order(
            "buy-111",
            "Buy",
            "111",
            "219.9400",
            _timestamp(18, 22),
            _timestamp(18, 22),
        ),
        _order(
            "buy-11",
            "Buy",
            "11",
            "219.9000",
            _timestamp(18, 34, 12),
            _timestamp(18, 34, 12),
        ),
        _order(
            "sell-104",
            "Sell",
            "104",
            "219.6130",
            _timestamp(19, 45, 40),
            _timestamp(19, 45, 40),
        ),
        _order(
            "sell-18",
            "Sell",
            "18",
            "221.1100",
            _timestamp(13, 25, 11, day=22),
            _timestamp(13, 25, 13, day=22),
        ),
        _order(
            "cancelled-zero",
            "Sell",
            "104",
            "217.5700",
            _timestamp(16, 2, 5, day=22),
            _timestamp(16, 3, 12, day=22),
            status="CanceledStatus",
            executed_quantity="0",
            executed_price="",
        ),
    ]
    executions = [
        _execution(
            "buy-111",
            "trade-buy-14",
            "14",
            "219.94",
            _timestamp(18, 22),
        ),
        _execution(
            "buy-111",
            "trade-buy-97",
            "97",
            "219.94",
            _timestamp(18, 22),
        ),
        _execution(
            "buy-11",
            "trade-buy-11",
            "11",
            "219.9",
            _timestamp(18, 34, 12),
        ),
        _execution(
            "sell-104",
            "trade-sell-104",
            "104",
            "219.613",
            _timestamp(19, 45, 40),
        ),
        _execution(
            "sell-18",
            "trade-sell-18",
            "18",
            "221.11",
            _timestamp(13, 25, 13, day=22),
        ),
    ]
    return (
        {"has_more": False, "orders": orders},
        {"has_more": False, "trades": executions},
    )


def _preview(
    transport: _FakeTransport,
):
    return LongportHistoricalCompletenessReader(
        transport,
        broker_identity_fingerprint=_FINGERPRINT,
    ).preview(
        symbol="nvda.us",
        start_at=_START,
        end_at=_END,
        observed_at=_OBSERVED,
    )


def test_complete_raw_pages_build_a_bound_read_only_preview() -> None:
    orders_payload, executions_payload = _complete_chain_payloads()
    transport = _FakeTransport(orders_payload, executions_payload)

    preview = _preview(transport)

    query = urlencode({
        "symbol": "NVDA.US",
        "start_at": int(_START.timestamp()),
        "end_at": int(_END.timestamp()),
    })
    assert transport.calls == [
        ("get", f"/v1/trade/order/history?{query}"),
        ("get", f"/v1/trade/execution/history?{query}"),
    ]
    assert preview.proof.orders_has_more is False
    assert preview.proof.executions_has_more is False
    assert preview.proof.order_count == 5
    assert preview.proof.execution_count == 5
    assert preview.proof.filled_order_count == 4
    assert preview.proof.broker_identity_fingerprint == _FINGERPRINT
    assert len(preview.proof.preview_digest) == 64
    assert [item.order_id for item in preview.filled_orders] == [
        "buy-111",
        "buy-11",
        "sell-104",
        "sell-18",
    ]
    first = preview.filled_orders[0]
    assert first.side == "BUY"
    assert first.executed_quantity == Decimal("111")
    assert first.executed_price == Decimal("219.9400")
    assert [item.quantity for item in first.executions] == [
        Decimal("14"),
        Decimal("97"),
    ]
    assert '"order_id":"buy-111"' in first.raw_json


def test_preview_digest_is_stable_when_broker_item_order_changes() -> None:
    orders_payload, executions_payload = _complete_chain_payloads()
    first = _preview(_FakeTransport(orders_payload, executions_payload))
    raw_orders = orders_payload["orders"]
    raw_executions = executions_payload["trades"]
    assert isinstance(raw_orders, list)
    assert isinstance(raw_executions, list)
    reversed_orders = {
        "has_more": False,
        "orders": list(reversed(raw_orders)),
    }
    reversed_executions = {
        "has_more": False,
        "trades": list(reversed(raw_executions)),
    }

    second = _preview(_FakeTransport(reversed_orders, reversed_executions))

    assert second.proof.preview_digest == first.proof.preview_digest
    assert second.filled_orders == first.filled_orders


def test_orders_has_more_fails_before_requesting_executions() -> None:
    transport = _FakeTransport({"has_more": True, "orders": []})

    with pytest.raises(HistoricalCompletenessError, match="orders.*truncated"):
        _preview(transport)

    assert len(transport.calls) == 1


def test_executions_has_more_fails_closed() -> None:
    transport = _FakeTransport(
        {"has_more": False, "orders": []},
        {"has_more": True, "trades": []},
    )

    with pytest.raises(HistoricalCompletenessError, match="executions.*truncated"):
        _preview(transport)


@pytest.mark.parametrize(
    ("orders_payload", "executions_payload", "missing_key"),
    [
        ({"has_more": False}, None, "orders"),
        ({"has_more": False, "orders": []}, {"has_more": False}, "trades"),
    ],
)
def test_complete_page_requires_the_official_items_list(
    orders_payload: dict[str, object],
    executions_payload: dict[str, object] | None,
    missing_key: str,
) -> None:
    responses: list[object] = [orders_payload]
    if executions_payload is not None:
        responses.append(executions_payload)

    with pytest.raises(HistoricalPayloadError, match=f"required {missing_key} list"):
        _preview(_FakeTransport(*responses))


@pytest.mark.parametrize("has_more", [None, 0, 1, "false", "true"])
def test_has_more_must_be_an_explicit_boolean(has_more: object) -> None:
    transport = _FakeTransport({"has_more": has_more, "orders": []})

    with pytest.raises(HistoricalPayloadError, match="boolean has_more"):
        _preview(transport)


def test_execution_without_order_in_same_proved_window_fails_closed() -> None:
    transport = _FakeTransport(
        {"has_more": False, "orders": []},
        {
            "has_more": False,
            "trades": [
                _execution(
                    "outside-order",
                    "outside-trade",
                    "1",
                    "100",
                    _timestamp(18, 22),
                )
            ],
        },
    )

    with pytest.raises(HistoricalPayloadError, match="no order in the proved window"):
        _preview(transport)


def test_filled_order_execution_quantity_conflict_fails_closed() -> None:
    order = _order(
        "buy-111",
        "Buy",
        "111",
        "219.94",
        _timestamp(18, 22),
        _timestamp(18, 22),
    )
    execution = _execution(
        "buy-111",
        "trade-buy-110",
        "110",
        "219.94",
        _timestamp(18, 22),
    )
    transport = _FakeTransport(
        {"has_more": False, "orders": [order]},
        {"has_more": False, "trades": [execution]},
    )

    with pytest.raises(HistoricalPayloadError, match="quantities do not match"):
        _preview(transport)


def test_non_filled_order_with_execution_quantity_fails_closed() -> None:
    partial = _order(
        "partial",
        "Buy",
        "10",
        "100",
        _timestamp(18, 22),
        _timestamp(18, 23),
        status="PartialFilledStatus",
        executed_quantity="2",
        executed_price="100",
    )
    transport = _FakeTransport(
        {"has_more": False, "orders": [partial]},
        {"has_more": False, "trades": []},
    )

    with pytest.raises(HistoricalPayloadError, match="FILLED-only preview"):
        _preview(transport)


def test_window_and_current_market_day_are_rejected_before_transport() -> None:
    transport = _FakeTransport()
    reader = LongportHistoricalCompletenessReader(
        transport,
        broker_identity_fingerprint=_FINGERPRINT,
    )

    with pytest.raises(ValueError, match="current market day"):
        reader.preview(
            symbol="NVDA.US",
            start_at=datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc),
            observed_at=_OBSERVED,
        )

    assert transport.calls == []


def test_transport_failure_is_wrapped_without_a_partial_preview() -> None:
    transport = _FakeTransport(OSError("network unavailable"))

    with pytest.raises(HistoricalTransportError, match="orders request failed"):
        _preview(transport)


def test_env_factory_uses_official_http_client_and_binds_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orders_payload, executions_payload = _complete_chain_payloads()
    transport = _FakeTransport(orders_payload, executions_payload)

    class _FakeHttpClient:
        calls = 0

        @classmethod
        def from_env(cls) -> _FakeTransport:
            cls.calls += 1
            return transport

    monkeypatch.setenv("LONGPORT_APP_KEY", "app-key")
    monkeypatch.setenv("LONGPORT_APP_SECRET", "app-secret")
    monkeypatch.setenv("LONGPORT_ACCESS_TOKEN", "access-token")
    monkeypatch.setattr(
        reader_module,
        "_import_openapi",
        lambda: SimpleNamespace(HttpClient=_FakeHttpClient),
    )

    preview = build_longport_historical_reader_from_env().preview(
        symbol="NVDA.US",
        start_at=_START,
        end_at=_END,
        observed_at=_OBSERVED,
    )

    expected_fingerprint = hashlib.sha256(
        "app-key\0app-secret\0access-token".encode()
    ).hexdigest()
    assert _FakeHttpClient.calls == 1
    assert preview.proof.broker_identity_fingerprint == expected_fingerprint


def test_env_factory_requires_all_identity_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LONGPORT_APP_KEY", "app-key")
    monkeypatch.delenv("LONGPORT_APP_SECRET", raising=False)
    monkeypatch.delenv("LONGPORT_ACCESS_TOKEN", raising=False)

    with pytest.raises(HistoricalTransportError, match="are all required"):
        build_longport_historical_reader_from_env()
