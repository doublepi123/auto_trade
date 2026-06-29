from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.models import OrderRecord

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


def _order_pnl(order: OrderRecord) -> Optional[float]:
    """Realised PnL for a filled order, or None if not yet closed.

    For BUY orders, PnL is (current_price - filled_price) * qty, but we
    don't have a reliable mark-to-market at the API layer. Instead, we
    treat the realised PnL as the price difference between consecutive
    BUY/SELL pairs on the same symbol, computed in the caller.
    """
    if order.status != "FILLED" or order.executed_price is None or order.executed_quantity is None:
        return None
    return float(order.executed_price) * float(order.executed_quantity)


def _compute_sharpe(pnls: list[float]) -> Optional[float]:
    """Annualised Sharpe ratio assuming per-trade observations (252 trading days).

    Returns None when fewer than 2 trades or zero standard deviation.
    """
    if len(pnls) < 2:
        return None
    n = len(pnls)
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
    if var <= 0:
        return None
    return (mean / math.sqrt(var)) * math.sqrt(252)


def _compute_metrics(pnls: list[float]) -> dict[str, Any]:
    if not pnls:
        return {
            "trade_count": 0,
            "win_rate": 0.0,
            "profit_factor": None,
            "sharpe_ratio": None,
            "avg_pnl": 0.0,
            "max_drawdown": 0.0,
        }
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None
    # Max drawdown from cumulative PnL series.
    cumulative: list[float] = []
    running = 0.0
    for p in pnls:
        running += p
        cumulative.append(running)
    peak = cumulative[0]
    max_dd = 0.0
    for c in cumulative:
        if c > peak:
            peak = c
        # Drawdown is a percentage drop from the high-water mark. Only
        # meaningful when the peak is strictly positive; if the entire curve
        # is below zero (never recovered above the starting point) there is no
        # positive reference to measure a percentage drop from, so skip.
        if peak > 0:
            dd = (peak - c) / peak
            if dd > max_dd:
                max_dd = dd
    return {
        "trade_count": len(pnls),
        "win_rate": (len(wins) / len(pnls)) * 100.0,
        "profit_factor": profit_factor,
        "sharpe_ratio": _compute_sharpe(pnls),
        "avg_pnl": sum(pnls) / len(pnls),
        "max_drawdown": max_dd * 100.0,
    }


@router.get("/summary", dependencies=[Depends(require_api_key())])
def metrics_summary(
    days: int = Query(default=30, ge=1, le=365, description="Lookback window in days"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Aggregate trading metrics over the last ``days`` days.

    Computes simple round-trip PnL by pairing SELL orders with their most
    recent BUY on the same symbol and using the broker's filled price.
    The metric is a coarse proxy (no lot-level FIFO) and is intended for
    dashboard at-a-glance only — for precise reporting use the
    ``/api/reports`` endpoint.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    orders = (
        db.query(OrderRecord)
        .filter(OrderRecord.created_at >= cutoff)
        .filter(OrderRecord.status == "FILLED")
        .order_by(OrderRecord.created_at.asc())
        .all()
    )
    # Pair each SELL with the most recent BUY on the same symbol.
    # Track remaining buy quantity in a side-car float instead of mutating the
    # ORM instance, which would dirty the session and risk a spurious flush.
    buy_queue: dict[str, list[tuple[OrderRecord, float]]] = {}
    pnls: list[float] = []
    for order in orders:
        if order.side == "BUY" and order.executed_price is not None and order.executed_quantity is not None:
            buy_queue.setdefault(order.symbol, []).append((order, float(order.executed_quantity)))
        elif order.side == "SELL" and order.executed_price is not None and order.executed_quantity is not None:
            symbol_queue = buy_queue.get(order.symbol, [])
            if not symbol_queue:
                continue
            # Consume from the head of the queue to approximate FIFO.
            matched_buy, remaining_qty = symbol_queue[0]
            qty = min(remaining_qty, float(order.executed_quantity))
            pnl = (float(order.executed_price) - float(matched_buy.executed_price)) * qty
            pnls.append(pnl)
            remaining_qty -= qty
            if remaining_qty <= 0:
                symbol_queue.pop(0)
            else:
                symbol_queue[0] = (matched_buy, remaining_qty)

    metrics = _compute_metrics(pnls)
    metrics["window_days"] = days
    return metrics
