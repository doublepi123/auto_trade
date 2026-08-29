"""Audit tracked_entries for cost-basis writes made by today-order sync.

Read-only. Opens no broker connection, submits nothing, writes nothing.

Between 0fd2dcc and its revert, ``_upsert_broker_order`` applied broker
fills onto ``tracked_entries``. Its ownership guard could never fire, so any
fill the sync observed was booked -- including fills the runner had already
flagged as unattributable. This finds rows that may carry such a write.

The forensic signature, in order of strength:

1. A broker order with fills and NO ``ORDER_SUBMITTED`` event. The sync path
   uses the same absence to declare a fill unattributable, so an entry whose
   cost basis moved alongside one was booked from a fill the system itself
   said it could not attribute.
2. A ``tracked_entries.updated_at`` close in time to that order's
   ``ORDER_SYNCED`` / ``ORDER_STATUS_CHANGED`` event. Proximity is
   circumstantial: the legitimate snapshot path can also touch a row near a
   sync. It narrows attention, it does not convict.

A clean report is not proof of an untouched ledger -- a wrongly booked fill
that was later overwritten by a confirmed position snapshot leaves no
residue here. Treat findings as leads for reconciliation against the broker,
not as a balance correction.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.database import SessionLocal
from app.models import OrderRecord, TradeEvent, TrackedEntry

_SYNC_EVENT_TYPES = ("ORDER_SYNCED", "ORDER_STATUS_CHANGED")


def _orders_without_submission_provenance(db) -> dict[str, list[OrderRecord]]:
    submitted = {
        str(row[0])
        for row in db.execute(
            select(TradeEvent.broker_order_id).where(
                TradeEvent.event_type == "ORDER_SUBMITTED",
                TradeEvent.broker_order_id != "",
            )
        ).all()
    }
    out: dict[str, list[OrderRecord]] = {}
    for order in db.execute(select(OrderRecord)).scalars().all():
        executed = float(order.executed_quantity or 0)
        if executed <= 0:
            continue
        if str(order.broker_order_id) in submitted:
            continue
        out.setdefault(str(order.symbol or ""), []).append(order)
    return out


def _sync_events_for(db, broker_order_id: str) -> list[TradeEvent]:
    return list(
        db.execute(
            select(TradeEvent)
            .where(
                TradeEvent.broker_order_id == broker_order_id,
                TradeEvent.event_type.in_(_SYNC_EVENT_TYPES),
            )
            .order_by(TradeEvent.created_at)
        )
        .scalars()
        .all()
    )


def audit(window_seconds: int) -> dict[str, object]:
    db = SessionLocal()
    try:
        entries = {
            str(row.symbol): row
            for row in db.execute(select(TrackedEntry)).scalars().all()
        }
        suspects = _orders_without_submission_provenance(db)

        findings: list[dict[str, object]] = []
        for symbol, orders in sorted(suspects.items()):
            entry = entries.get(symbol)
            for order in orders:
                events = _sync_events_for(db, str(order.broker_order_id))
                proximate = []
                if entry is not None and entry.updated_at is not None:
                    updated = entry.updated_at
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    for event in events:
                        created = event.created_at
                        if created is None:
                            continue
                        if created.tzinfo is None:
                            created = created.replace(tzinfo=timezone.utc)
                        gap = abs((updated - created).total_seconds())
                        if gap <= window_seconds:
                            proximate.append(
                                {"event_type": event.event_type, "gap_seconds": round(gap, 1)}
                            )
                findings.append(
                    {
                        "symbol": symbol,
                        "broker_order_id": str(order.broker_order_id),
                        "side": str(order.side or ""),
                        "status": str(order.status or ""),
                        "executed_quantity": float(order.executed_quantity or 0),
                        "executed_price": float(order.executed_price or 0),
                        "has_tracked_entry": entry is not None,
                        "tracked_side": str(entry.side) if entry is not None else None,
                        "tracked_quantity": float(entry.quantity) if entry is not None else None,
                        "tracked_cost": float(entry.cost) if entry is not None else None,
                        "sync_events": len(events),
                        "proximate_sync_events": proximate,
                        # A fill opposing the tracked side is the strongest lead:
                        # booking it would have added on top of or flipped the
                        # position, which P0 forbids.
                        "direction_conflict": bool(
                            entry is not None
                            and str(order.side or "").upper() == "BUY"
                            and str(entry.side).upper() == "SHORT"
                        ),
                    }
                )

        return {
            "tracked_entries": len(entries),
            "unattributable_filled_orders": sum(len(v) for v in suspects.values()),
            "findings": findings,
            "window_seconds": window_seconds,
        }
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit tracked_entries for cost-basis writes made by today-order sync."
    )
    parser.add_argument(
        "--window-seconds",
        type=int,
        default=120,
        help="how close a tracked_entries.updated_at must be to a sync event to be flagged",
    )
    parser.add_argument("--json-out", type=str, default="")
    args = parser.parse_args()

    report = audit(args.window_seconds)
    findings = report["findings"]
    assert isinstance(findings, list)

    print(f"tracked_entries rows            : {report['tracked_entries']}")
    print(f"filled orders w/o ORDER_SUBMITTED: {report['unattributable_filled_orders']}")
    if not findings:
        print("\nNo unattributable filled orders found.")
        print("This does not prove the ledger is clean; see the module docstring.")
    else:
        print(f"\n{len(findings)} lead(s):\n")
        for item in findings:
            assert isinstance(item, dict)
            flag = " [DIRECTION CONFLICT]" if item["direction_conflict"] else ""
            print(
                f"  {item['symbol']} order={item['broker_order_id']} "
                f"{item['side']} {item['executed_quantity']}@{item['executed_price']} "
                f"status={item['status']}{flag}"
            )
            print(
                f"    tracked: side={item['tracked_side']} qty={item['tracked_quantity']} "
                f"cost={item['tracked_cost']}"
            )
            print(
                f"    sync events={item['sync_events']} "
                f"proximate={item['proximate_sync_events']}"
            )

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2, default=str))
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
