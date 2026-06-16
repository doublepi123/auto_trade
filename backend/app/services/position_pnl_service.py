"""Live unrealized PnL over open positions.

Joins the persisted weighted-average entry cost (``tracked_entries``, kept
because the broker's ``avg_price`` drifts on partial fills / corporate actions)
with live quotes to show per-position and total unrealized P&L.

Broker-agnostic and offline-testable: quotes come from an injected
``quote_provider`` (the runner's broker in production, a fake in tests).
"""
from __future__ import annotations

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TrackedEntry
from app.schemas import PositionPnlResult, PositionPnlRow


class QuoteProvider(Protocol):
    """Minimal surface this service needs from the broker."""

    def get_quotes(self, symbols: list[str]) -> list: ...  # pragma: no cover


class PositionPnlService:
    def __init__(self, db: Session, quote_provider: QuoteProvider | None = None) -> None:
        self._db = db
        self._quote_provider = quote_provider

    def get_positions_pnl(self) -> PositionPnlResult:
        entries = list(
            self._db.scalars(
                select(TrackedEntry).where(TrackedEntry.quantity != 0)
            )
        )
        symbols = sorted({e.symbol for e in entries if e.symbol})
        available = True
        quote_map = self._fetch_quotes(symbols)
        if symbols and not quote_map:
            # Quotes unavailable (broker down / SDK missing) — still show
            # cost basis; mark unavailable so the UI can say so.
            available = self._quote_provider is None

        rows: list[PositionPnlRow] = []
        for entry in sorted(entries, key=lambda e: e.symbol):
            qty = float(entry.quantity)
            cost = float(entry.cost)
            last = quote_map.get(entry.symbol) if entry.symbol else None
            unrealized, unrealized_pct = _unrealized(qty, cost, last)
            rows.append(PositionPnlRow(
                symbol=entry.symbol,
                quantity=qty,
                avg_entry_cost=cost,
                last_price=last,
                unrealized_pnl=unrealized,
                unrealized_pnl_pct=unrealized_pct,
                market_value=(last or 0.0) * abs(qty),
                cost_value=cost * abs(qty),
                has_quote=last is not None,
            ))

        total_unrealized = sum(r.unrealized_pnl for r in rows if r.has_quote)
        total_cost = sum(r.cost_value for r in rows)
        return PositionPnlResult(
            positions=rows,
            total_unrealized_pnl=total_unrealized,
            total_cost_basis=total_cost,
            total_unrealized_pnl_pct=(
                (total_unrealized / total_cost * 100) if total_cost > 0 and total_unrealized != 0 else None
            ),
            available=available,
            error=None if available else "live quotes unavailable; showing cost basis only",
        )

    def _fetch_quotes(self, symbols: list[str]) -> dict[str, float]:
        if not symbols or self._quote_provider is None:
            return {}
        try:
            quotes = self._quote_provider.get_quotes(symbols)
        except Exception:
            return {}
        result: dict[str, float] = {}
        for quote in quotes:
            symbol = getattr(quote, "symbol", None)
            last_price = getattr(quote, "last_price", 0) or 0
            if symbol and last_price > 0:
                result[symbol] = float(last_price)
        return result


def _unrealized(quantity: float, cost: float, last: float | None) -> tuple[float, float | None]:
    """Unrealized P&L for one position. ``None`` pct when it cannot be computed."""
    if last is None or last <= 0 or cost <= 0:
        return (0.0, None)
    if quantity >= 0:
        pnl = (last - cost) * quantity
    else:
        # Short: entry (sell) at cost, profit when last < cost.
        pnl = (cost - last) * (-quantity)
    basis = cost * abs(quantity)
    pct = (pnl / basis * 100) if basis > 0 else None
    return (pnl, pct)
