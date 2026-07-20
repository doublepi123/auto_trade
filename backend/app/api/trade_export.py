from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import Final

from fastapi import Response

from app.schemas import ClosedTrade
from app.services.daily_pnl_service import ClosedRoundTrip


CSV_COLUMNS: Final = tuple(ClosedTrade.model_fields)


def build_closed_trade_items(
    trips: list[ClosedRoundTrip],
    limit: int,
) -> list[ClosedTrade]:
    return [
        ClosedTrade(
            symbol=trip.symbol,
            side=trip.side,
            entry_order_id=trip.entry_order_id,
            exit_order_id=trip.exit_order_id,
            entry_at=trip.entry_at,
            exit_at=trip.exit_at,
            entry_price=trip.entry_price,
            exit_price=trip.exit_price,
            quantity=trip.quantity,
            gross_pnl=round(trip.gross_pnl, 2),
            est_fees=round(trip.est_fees, 2),
            net_pnl=round(trip.net_pnl, 2),
            holding_seconds=trip.holding_seconds,
            fee_source=trip.fee_source,
            actual_fees=trip.actual_fees,
            slippage_amount=trip.slippage_amount,
            slippage_bps=trip.slippage_bps,
            ack_latency_ms=trip.ack_latency_ms,
            fill_latency_ms=trip.fill_latency_ms,
            exit_cause=trip.exit_cause,
            exit_reason=trip.exit_reason,
            mfe_amount=trip.mfe_amount,
            mae_amount=trip.mae_amount,
            mfe_pct=trip.mfe_pct,
            mae_pct=trip.mae_pct,
        )
        for trip in sorted(trips, key=lambda item: item.exit_at, reverse=True)[:limit]
    ]


def closed_trade_export_response(
    items: list[ClosedTrade],
    format: str,
) -> Response:
    filename = f"trades_{datetime.now(timezone.utc).strftime('%Y%m%d')}.{format}"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if format == "json":
        return Response(
            content=json.dumps(
                [item.model_dump(mode="json") for item in items],
                ensure_ascii=False,
            ),
            media_type="application/json",
            headers=headers,
        )

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for item in items:
        writer.writerow(item.model_dump(mode="json"))
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )
