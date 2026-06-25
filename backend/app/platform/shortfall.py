"""P219: Implementation Shortfall TCA (Perold 1988).

Perold's implementation shortfall decomposes the gap between the *decision*
price (the arrival price at the moment the trading decision was made) and the
*realized* execution price into economic components:

* **realized cost** — paid (or received) vs arrival on the filled portion.
* **opportunity cost** — the missed move on the unfilled portion.
* **timing cost** — drift between arrival and the chosen terminal benchmark
  (VWAP / close) on the filled portion.
* **fees** — commissions.

Signed so that **positive = a cost / loss vs the decision to trade at
arrival**. Reference: Perold, "The Implementation Shortfall: Paper Versus
Reality" (1988); NautilusTrader ``CostModel`` / slippage decomposition; Kissell
cost decomposition. Builds on the platform's existing
:mod:`app.platform.tca` (signed-slippage convention, ``TcaFill``,
``ReferencePriceProvider``) and reads the same ``transactions`` ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Transaction
from app.platform.tca import ConstReferencePriceProvider, ReferencePriceProvider, TcaFill

__all__ = [
    "ShortfallOrder",
    "ShortfallFill",
    "ShortfallBreakdown",
    "implementation_shortfall",
    "shortfall_from_tca",
    "ShortfallAnalyzer",
]


@dataclass(frozen=True)
class ShortfallOrder:
    symbol: str
    side: str
    ordered_quantity: Decimal
    limit_price: Decimal | None = None
    arrival_price: Decimal | None = None
    benchmark: str = "arrival"
    benchmark_price: Decimal | None = None
    fees: Decimal = Decimal("0")


@dataclass
class ShortfallFill:
    quantity: Decimal
    price: Decimal
    commission: Decimal = Decimal("0")


@dataclass
class ShortfallBreakdown:
    realized_cost: Decimal
    opportunity_cost: Decimal
    timing_cost: Decimal
    fees: Decimal
    total_shortfall: Decimal
    filled_quantity: Decimal
    unfilled_quantity: Decimal
    avg_fill_price: Decimal | None
    vwap: Decimal | None
    arrival_price: Decimal
    benchmark: str
    benchmark_price: Decimal
    participation_rate: Decimal
    realized_bps: Decimal
    total_bps: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "realized_cost": float(self.realized_cost),
            "opportunity_cost": float(self.opportunity_cost),
            "timing_cost": float(self.timing_cost),
            "fees": float(self.fees),
            "total_shortfall": float(self.total_shortfall),
            "filled_quantity": float(self.filled_quantity),
            "unfilled_quantity": float(self.unfilled_quantity),
            "avg_fill_price": float(self.avg_fill_price) if self.avg_fill_price is not None else None,
            "vwap": float(self.vwap) if self.vwap is not None else None,
            "arrival_price": float(self.arrival_price),
            "benchmark": self.benchmark,
            "benchmark_price": float(self.benchmark_price),
            "participation_rate": float(self.participation_rate),
            "realized_bps": float(self.realized_bps),
            "total_bps": float(self.total_bps),
        }


def _side_sign(side: str) -> int:
    s = side.upper()
    if s in ("BUY", "LONG"):
        return 1
    if s in ("SELL", "SHORT"):
        return -1
    raise ValueError(f"unknown side: {side}")


def _finite_check(*vals: Decimal) -> None:
    for v in vals:
        if not v.is_finite():
            raise ValueError("NaN/inf not allowed in shortfall inputs")


def implementation_shortfall(
    order: ShortfallOrder,
    fills: list[ShortfallFill],
    arrival_price: Decimal | None = None,
    benchmark: str | None = None,
    benchmark_price: Decimal | None = None,
) -> ShortfallBreakdown:
    """Decompose execution cost vs the arrival-price decision.

    Arrival price resolution: explicit ``arrival_price`` arg >
    ``order.arrival_price`` > ``order.limit_price``. Benchmark resolution:
    explicit arg > ``order.benchmark``; price: explicit > ``order.benchmark_price``
    > computed VWAP (for ``vwap``) / arrival (for ``arrival``) / required for
    ``close``.
    """
    sign = _side_sign(order.side)
    # arrival price
    arrival = arrival_price if arrival_price is not None else (
        order.arrival_price if order.arrival_price is not None else order.limit_price
    )
    if arrival is None:
        raise ValueError("arrival_price is required (no limit_price fallback)")
    _finite_check(arrival, order.ordered_quantity)

    # fills aggregation (Decimal throughout)
    filled_qty = Decimal("0")
    exec_notional = Decimal("0")
    total_commission = Decimal("0")
    for f in fills:
        _finite_check(f.quantity, f.price, f.commission)
        filled_qty += f.quantity
        exec_notional += f.price * f.quantity
        total_commission += f.commission
    vwap = (exec_notional / filled_qty) if filled_qty > 0 else None
    unfilled_qty = max(order.ordered_quantity - filled_qty, Decimal("0"))

    # fees: explicit non-zero order.fees wins; otherwise sum fill commissions.
    fees = order.fees if order.fees != Decimal("0") else total_commission

    # benchmark resolution
    bench = benchmark or order.benchmark
    bench_price = benchmark_price if benchmark_price is not None else order.benchmark_price
    if bench_price is None:
        if bench == "arrival":
            bench_price = arrival
        elif bench == "vwap":
            if vwap is None:
                raise ValueError("vwap benchmark requires fills or explicit benchmark_price")
            bench_price = vwap
        elif bench == "close":
            raise ValueError("close benchmark requires explicit benchmark_price")
        else:
            raise ValueError(f"unknown benchmark: {bench}")

    # Perold components (signed: positive = cost vs decision).
    realized_cost = sign * (vwap - arrival) * filled_qty if filled_qty > 0 else Decimal("0")
    timing_cost = sign * (bench_price - arrival) * filled_qty if filled_qty > 0 else Decimal("0")
    opportunity_cost = sign * (bench_price - arrival) * unfilled_qty if unfilled_qty > 0 else Decimal("0")
    total = realized_cost + opportunity_cost + timing_cost + fees
    # Wait — realized already uses vwap; timing uses benchmark. Under the
    # arrival benchmark timing==0 and realized captures vwap-arrival. Under
    # vwap/close benchmark, realized stays vwap-arrival and timing captures
    # benchmark-arrival. Total = realized + timing + opportunity + fees.
    total = realized_cost + timing_cost + opportunity_cost + fees

    participation = (filled_qty / order.ordered_quantity) if order.ordered_quantity > 0 else Decimal("0")
    # bps guards
    if arrival > 0 and filled_qty > 0:
        realized_bps = realized_cost / (arrival * filled_qty) * Decimal("10000")
    else:
        realized_bps = Decimal("0")
    if arrival > 0 and order.ordered_quantity > 0:
        total_bps = total / (arrival * order.ordered_quantity) * Decimal("10000")
    else:
        total_bps = Decimal("0")

    return ShortfallBreakdown(
        realized_cost=realized_cost,
        opportunity_cost=opportunity_cost,
        timing_cost=timing_cost,
        fees=fees,
        total_shortfall=total,
        filled_quantity=filled_qty,
        unfilled_quantity=unfilled_qty,
        avg_fill_price=vwap,
        vwap=vwap,
        arrival_price=arrival,
        benchmark=bench,
        benchmark_price=bench_price,
        participation_rate=participation,
        realized_bps=realized_bps,
        total_bps=total_bps,
    )


def shortfall_from_tca(
    order: ShortfallOrder,
    tca_fills: list[TcaFill],
    benchmark: str = "arrival",
    benchmark_price: Decimal | None = None,
) -> ShortfallBreakdown:
    """Build a ShortfallBreakdown from :class:`TcaFill` records.

    Uses each fill's ``reference`` as its arrival price contribution when the
    order has no explicit arrival; the order-level arrival is taken from the
    first fill's reference (or ``order.arrival_price`` / ``order.limit_price``).
    """
    fills = [ShortfallFill(quantity=Decimal(f.quantity), price=f.price, commission=f.commission)
            for f in tca_fills]
    arrival = order.arrival_price
    if arrival is None and tca_fills:
        arrival = tca_fills[0].reference
    return implementation_shortfall(order, fills, arrival_price=arrival, benchmark=benchmark,
                                    benchmark_price=benchmark_price)


class ShortfallAnalyzer:
    """Convenience wrapper over the ``transactions`` ledger (mirrors TcaAnalyzer)."""

    def __init__(self, reference_provider: ReferencePriceProvider | None = None, db: Session | None = None) -> None:
        self._provider = reference_provider or ConstReferencePriceProvider({})
        self._db = db

    def _session(self) -> Session:
        return self._db if self._db is not None else SessionLocal()

    def _owns_session(self) -> bool:
        return self._db is None

    def analyze_order(
        self,
        broker_order_id: str,
        benchmark: str = "arrival",
        benchmark_price: Decimal | None = None,
    ) -> ShortfallBreakdown | None:
        session = self._session()
        try:
            rows = session.query(Transaction).filter(Transaction.broker_order_id == broker_order_id).all()
            if not rows:
                return None
            # infer side/symbol/ordered qty from the first row; ordered_quantity
            # is not stored per-transaction, so use the sum of fill quantities as
            # a lower bound and the max as the intended quantity proxy.
            symbol = rows[0].symbol
            side = rows[0].side
            ordered_qty = Decimal(max(int(r.quantity) for r in rows)) if rows else Decimal("0")
            fills = [ShortfallFill(quantity=Decimal(r.quantity), price=Decimal(r.price),
                                   commission=Decimal(r.commission or 0)) for r in rows]
            arrival = self._provider.price(symbol, rows[0].timestamp) if rows else None
            order = ShortfallOrder(
                symbol=symbol, side=side, ordered_quantity=ordered_qty,
                arrival_price=arrival,
            )
            return implementation_shortfall(order, fills, benchmark=benchmark, benchmark_price=benchmark_price)
        finally:
            if self._owns_session():
                session.close()