from __future__ import annotations

from decimal import Decimal

from app.core.broker import AccountInfo, CashBalance, NetAsset, Position, Quote
import pytest

from app.services.account_service import AccountService, AccountUnavailableError


class FakeBroker:
    def get_account(self) -> AccountInfo:
        return AccountInfo(Decimal("1000"), [CashBalance("USD", Decimal("900"), Decimal("100"))], [NetAsset("USD", Decimal("1000"))])

    def get_positions(self) -> list[Position]:
        return [Position("AAPL.US", "LONG", Decimal("2"), Decimal("150"))]

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol, 200.0, 199.0, 201.0, "")


def test_account_service_builds_account_response() -> None:
    response = AccountService(FakeBroker()).get_account_response()

    assert response.total_assets == 1000.0
    assert response.cash_balances[0].available_cash == 900.0
    assert response.positions[0].market_value == 400.0


def test_account_service_raises_when_account_balance_unavailable() -> None:
    class FailingAccountBroker(FakeBroker):
        def get_account(self) -> AccountInfo:
            raise RuntimeError("connection failed")

    with pytest.raises(AccountUnavailableError):
        AccountService(FailingAccountBroker()).get_account_response()
