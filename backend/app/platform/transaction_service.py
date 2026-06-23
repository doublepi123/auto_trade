from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Transaction
from app.platform.events import Event, FillEvent

__all__ = ["TransactionService", "TransactionLogger"]


class TransactionService:
    """Transaction ledger (pyfolio-style ``transactions``): one row per fill.

    Designed to be used either with an injected Session (tests, request-scoped)
    or with its own SessionLocal-derived session (background bus subscriber).
    """

    def __init__(self, db: Session | None = None) -> None:
        self._db = db

    def _session(self) -> Session:
        return self._db if self._db is not None else SessionLocal()

    def _owns_session(self) -> bool:
        return self._db is None

    def record(self, fill: FillEvent, source: str = "paper") -> Transaction:
        session = self._session()
        try:
            row = Transaction(
                broker_order_id=fill.broker_order_id,
                symbol=fill.symbol or "",
                side=fill.side,
                quantity=fill.quantity,
                price=float(fill.price),
                commission=float(fill.commission),
                source=source,
                timestamp=fill.timestamp,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row
        finally:
            if self._owns_session():
                session.close()

    def list(
        self,
        symbol: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        session = self._session()
        try:
            query = session.query(Transaction)
            if symbol:
                query = query.filter(Transaction.symbol == symbol)
            if since:
                query = query.filter(Transaction.timestamp >= since)
            if until:
                query = query.filter(Transaction.timestamp <= until)
            rows = (
                query.order_by(Transaction.timestamp.desc(), Transaction.id.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "id": r.id,
                    "broker_order_id": r.broker_order_id,
                    "symbol": r.symbol,
                    "side": r.side,
                    "quantity": r.quantity,
                    "price": r.price,
                    "commission": r.commission,
                    "source": r.source,
                    "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                }
                for r in rows
            ]
        finally:
            if self._owns_session():
                session.close()


class TransactionLogger:
    """Bus subscriber: persists each FillEvent into the transaction ledger."""

    def __init__(self, service: TransactionService | None = None, source: str = "paper") -> None:
        self.service = service or TransactionService()
        self.source = source

    def on_fill(self, event: Event) -> None:
        if not isinstance(event, FillEvent):
            return
        self.service.record(event, source=self.source)

    def subscribe(self, bus: Any) -> None:
        bus.subscribe("fill", self.on_fill)
