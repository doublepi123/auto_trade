"""P199: Transaction Cost Analysis (TCA) analyzer.

Quantifies realized execution cost: for each fill, compares the executed price
against a reference (arrival/mid/benchmark) price to derive signed slippage,
then attributes total cost (slippage notional + commission) by symbol, side,
source, and time bucket. Mirrors the cost analytics in Nautilus Trader's
``CostModel`` and QuantConnect Lean's slippage analysis, computed over the
existing ``transactions`` ledger rather than a new data source.

The reference price is injected via a ``ReferencePriceProvider`` so the analyzer
stays I/O-free and deterministic in tests; the API endpoint supplies a provider
that reads the nearest preceding bar from ``event_log``.

Signed slippage convention: positive = unfavorable (paid above reference on a
BUY, sold below reference on a SELL). This normalizes cost direction across
sides so aggregation by symbol is meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Protocol, runtime_checkable

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Transaction

__all__ = [
    "ReferencePriceProvider",
    "ConstReferencePriceProvider",
    "TcaAnalyzer",
    "TcaFill",
    "TcaAttribution",
]


@runtime_checkable
class ReferencePriceProvider(Protocol):
    def price(self, symbol: str, timestamp: datetime) -> Decimal | None:
        ...


@dataclass
class ConstReferencePriceProvider:
    """Deterministic provider: returns a fixed price per symbol (tests)."""

    prices: dict[str, Decimal]

    def price(self, symbol: str, timestamp: datetime) -> Decimal | None:
        return self.prices.get(symbol)


@dataclass
class TcaFill:
    broker_order_id: str
    symbol: str
    side: str
    quantity: int
    price: Decimal
    commission: Decimal
    reference: Decimal | None
    timestamp: datetime
    source: str = "paper"

    @property
    def signed_slippage(self) -> Decimal:
        """Positive = unfavorable (cost incurred)."""
        if self.reference is None or self.reference == 0:
            return Decimal("0")
        direction = Decimal("1") if self.side.upper() == "BUY" else Decimal("-1")
        return direction * (self.price - self.reference)

    @property
    def slippage_cost(self) -> Decimal:
        """Signed notional cost from slippage = signed_slippage * quantity.

        Positive = cost paid; negative = favorable (price improvement rebate)
        that offsets other costs in aggregation.
        """
        return self.signed_slippage * Decimal(self.quantity)

    @property
    def total_cost(self) -> Decimal:
        """Slippage notional (signed) + commission."""
        return self.slippage_cost + self.commission


@dataclass
class TcaAttribution:
    by_symbol: dict[str, dict[str, Decimal]]
    by_side: dict[str, dict[str, Decimal]]
    by_source: dict[str, dict[str, Decimal]]
    by_bucket: dict[str, dict[str, Decimal]]
    totals: dict[str, Decimal]


class TcaAnalyzer:
    """Compute realized execution-cost attribution over the transaction ledger."""

    def __init__(
        self,
        reference_provider: ReferencePriceProvider | None = None,
        db: Session | None = None,
        bucket: str = "day",  # "day" | "hour" | "none"
    ) -> None:
        self._provider = reference_provider or ConstReferencePriceProvider({})
        self._db = db
        self._bucket = bucket

    def _session(self) -> Session:
        return self._db if self._db is not None else SessionLocal()

    def _owns_session(self) -> bool:
        return self._db is None

    def analyze(
        self,
        symbol: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 5000,
    ) -> TcaAttribution:
        session = self._session()
        try:
            query = session.query(Transaction)
            if symbol:
                query = query.filter(Transaction.symbol == symbol)
            if since:
                query = query.filter(Transaction.timestamp >= since)
            if until:
                query = query.filter(Transaction.timestamp <= until)
            rows = query.order_by(Transaction.timestamp.asc()).limit(max(1, min(limit, 50000))).all()
            fills = [self._to_fill(r) for r in rows]
            return self._attribute(fills)
        finally:
            if self._owns_session():
                session.close()

    def analyze_fills(self, fills: list[TcaFill]) -> TcaAttribution:
        """Attribute a pre-built list of TcaFill (pure, no DB)."""
        return self._attribute(fills)

    def _to_fill(self, row: Transaction) -> TcaFill:
        ref = None
        if row.symbol and row.timestamp:
            ref = self._provider.price(row.symbol, row.timestamp)
        return TcaFill(
            broker_order_id=row.broker_order_id,
            symbol=row.symbol,
            side=row.side,
            quantity=int(row.quantity),
            price=Decimal(str(row.price)),
            commission=Decimal(str(row.commission or 0.0)),
            reference=ref,
            timestamp=row.timestamp,
            source=row.source or "paper",
        )

    def _bucket_key(self, ts: datetime | None) -> str:
        if ts is None:
            return "unknown"
        if self._bucket == "hour":
            return ts.strftime("%Y-%m-%dT%H")
        if self._bucket == "none":
            return "all"
        return ts.strftime("%Y-%m-%d")

    def _attribute(self, fills: list[TcaFill]) -> TcaAttribution:
        by_symbol: dict[str, dict[str, Decimal]] = {}
        by_side: dict[str, dict[str, Decimal]] = {}
        by_source: dict[str, dict[str, Decimal]] = {}
        by_bucket: dict[str, dict[str, Decimal]] = {}

        def _accumulate(bucket: dict[str, dict[str, Decimal]], key: str, fill: TcaFill) -> None:
            slot = bucket.setdefault(key, {"fills": Decimal("0"), "quantity": Decimal("0"),
                                           "slippage_cost": Decimal("0"), "commission": Decimal("0"),
                                           "total_cost": Decimal("0")})
            slot["fills"] += Decimal("1")
            slot["quantity"] += Decimal(fill.quantity)
            slot["slippage_cost"] += fill.slippage_cost
            slot["commission"] += fill.commission
            slot["total_cost"] += fill.total_cost

        total_slip = Decimal("0")
        total_comm = Decimal("0")
        total_cost = Decimal("0")
        total_qty = Decimal("0")
        for fill in fills:
            _accumulate(by_symbol, fill.symbol, fill)
            _accumulate(by_side, fill.side.upper(), fill)
            _accumulate(by_source, fill.source, fill)
            _accumulate(by_bucket, self._bucket_key(fill.timestamp), fill)
            total_slip += fill.slippage_cost
            total_comm += fill.commission
            total_cost += fill.total_cost
            total_qty += Decimal(fill.quantity)

        avg_slip_bps = Decimal("0")
        if total_qty > 0 and fills:
            notional = sum((f.price * Decimal(f.quantity) for f in fills), Decimal("0"))
            if notional > 0:
                avg_slip_bps = (total_slip / notional) * Decimal("10000")

        return TcaAttribution(
            by_symbol=by_symbol,
            by_side=by_side,
            by_source=by_source,
            by_bucket=by_bucket,
            totals={
                "fills": Decimal(len(fills)),
                "quantity": total_qty,
                "slippage_cost": total_slip,
                "commission": total_comm,
                "total_cost": total_cost,
                "avg_slippage_bps": avg_slip_bps,
            },
        )

    @staticmethod
    def to_dict(attr: TcaAttribution) -> dict[str, Any]:
        """JSON-friendly serialization of an attribution result."""

        def _dump(d: dict[str, dict[str, Decimal]]) -> dict[str, dict[str, float]]:
            return {
                k: {kk: float(vv) for kk, vv in v.items()} for k, v in d.items()
            }

        return {
            "by_symbol": _dump(attr.by_symbol),
            "by_side": _dump(attr.by_side),
            "by_source": _dump(attr.by_source),
            "by_bucket": _dump(attr.by_bucket),
            "totals": {k: float(v) for k, v in attr.totals.items()},
        }
