from __future__ import annotations

import logging
from decimal import Decimal
from typing import Protocol

from app.core.broker import AccountInfo, Position, Quote
from app.schemas import AccountResponse, CashBalanceSchema, PositionSchema

logger = logging.getLogger("auto_trade.account_service")


class AccountUnavailableError(RuntimeError):
    pass


class AccountBroker(Protocol):
    def get_account(self) -> AccountInfo:
        ...

    def get_positions(self) -> list[Position]:
        ...

    def get_quote(self, symbol: str) -> Quote:
        ...


class AccountService:
    def __init__(self, broker: AccountBroker) -> None:
        self._broker = broker

    def get_account_response(self) -> AccountResponse:
        total_assets, cash_balances = self._get_cash_balances()
        positions = self._get_positions()
        return AccountResponse(
            total_assets=total_assets,
            cash_balances=cash_balances,
            positions=positions,
        )

    def _get_cash_balances(self) -> tuple[float, list[CashBalanceSchema]]:
        try:
            account = self._broker.get_account()
            return float(account.total_assets), [
                CashBalanceSchema(
                    currency=cb.currency,
                    available_cash=float(cb.available_cash),
                    frozen_cash=float(cb.frozen_cash),
                )
                for cb in account.cash_balances
            ]
        except Exception:
            logger.exception("failed to get account balance")
            raise AccountUnavailableError("account data unavailable")

    def _get_positions(self) -> list[PositionSchema]:
        try:
            return [self._build_position(pos) for pos in self._broker.get_positions()]
        except Exception:
            logger.exception("failed to get positions")
            return []

    def _build_position(self, pos: Position) -> PositionSchema:
        try:
            quote = self._broker.get_quote(pos.symbol)
            market_value = float(pos.quantity * Decimal(str(quote.last_price)))
        except Exception:
            logger.warning("failed to get quote for %s, using avg_price fallback", pos.symbol)
            market_value = float(pos.quantity * pos.avg_price)

        return PositionSchema(
            symbol=pos.symbol,
            side=pos.side,
            quantity=float(pos.quantity),
            avg_price=float(pos.avg_price),
            market_value=market_value,
        )
