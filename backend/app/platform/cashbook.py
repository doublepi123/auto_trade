from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.platform.events import Event, FillEvent

__all__ = ["currency_for", "CashBook"]


def currency_for(symbol: str) -> str:
    """标的对应币种（参考 Nautilus currencies）。`.HK`/`.SG`(港币) 其余 USD；简单映射，不含节假日历。"""
    s = symbol.upper()
    if s.endswith(".HK"):
        return "HKD"
    return "USD"


class CashBook:
    """多币种现金账本（参考 Nautilus currencies / Lean CashBook）：按币种记账 + FX 汇率 → 基币 NAV。"""

    def __init__(self, base_currency: str = "USD") -> None:
        self.base_currency = base_currency
        self.balances: dict[str, Decimal] = {}
        # fx rate = units of base per 1 unit of currency; base is always 1
        self.fx: dict[str, Decimal] = {base_currency: Decimal("1")}

    def deposit(self, currency: str, amount: Decimal) -> None:
        if amount < 0:
            raise ValueError("deposit amount must be non-negative")
        self.balances[currency] = self.balances.get(currency, Decimal("0")) + amount

    def withdraw(self, currency: str, amount: Decimal) -> None:
        if amount < 0:
            raise ValueError("withdraw amount must be non-negative")
        current = self.balances.get(currency, Decimal("0"))
        if amount > current:
            raise ValueError(f"insufficient {currency} balance: have {current}, need {amount}")
        self.balances[currency] = current - amount

    def balance(self, currency: str) -> Decimal:
        return self.balances.get(currency, Decimal("0"))

    def set_fx_rate(self, currency: str, rate_to_base: Decimal) -> None:
        """1 unit of `currency` = `rate_to_base` units of base currency."""
        if currency == self.base_currency and rate_to_base != Decimal("1"):
            raise ValueError("base currency rate must be 1")
        self.fx[currency] = rate_to_base

    def nav(self) -> Decimal:
        """所有币种余额按 FX 汇率折算为基币的总 NAV。未设置汇率的币种按 1 处理（保守：等同基币）。"""
        total = Decimal("0")
        for currency, amount in self.balances.items():
            rate = self.fx.get(currency, Decimal("1"))
            total += amount * rate
        return total

    def on_fill(self, event: Event) -> None:
        if not isinstance(event, FillEvent):
            return
        currency = currency_for(event.symbol or "")
        notional = Decimal(event.quantity) * event.price
        if event.side == "BUY":
            # pay notional + commission
            self._ensure(currency)
            self.balances[currency] -= notional + event.commission
        else:  # SELL
            self._ensure(currency)
            self.balances[currency] += notional - event.commission

    def _ensure(self, currency: str) -> None:
        if currency not in self.balances:
            self.balances[currency] = Decimal("0")
