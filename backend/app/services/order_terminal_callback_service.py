from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Final

from app.database import SessionLocal
from app.models import OrderTerminalCallback

_PROCESSING: Final = "PROCESSING"
_COMPLETED: Final = "COMPLETED"
_CLAIM_LOCK = threading.Lock()


class OrderTerminalCallbackService:
    def claim(self, broker_order_id: str, terminal_status: str) -> bool:
        normalized_status = terminal_status.strip().upper()
        now = datetime.now(timezone.utc)
        with _CLAIM_LOCK, SessionLocal() as db:
            receipt = db.get(
                OrderTerminalCallback,
                (broker_order_id, normalized_status),
            )
            if receipt is None:
                db.add(
                    OrderTerminalCallback(
                        broker_order_id=broker_order_id,
                        terminal_status=normalized_status,
                        state=_PROCESSING,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
                db.commit()
                return True
            receipt.last_seen_at = now
            receipt.attempt_count += 1
            db.commit()
            return receipt.state != _COMPLETED

    def complete(self, broker_order_id: str, terminal_status: str) -> None:
        normalized_status = terminal_status.strip().upper()
        now = datetime.now(timezone.utc)
        with _CLAIM_LOCK, SessionLocal() as db:
            receipt = db.get(
                OrderTerminalCallback,
                (broker_order_id, normalized_status),
            )
            if receipt is None:
                receipt = OrderTerminalCallback(
                    broker_order_id=broker_order_id,
                    terminal_status=normalized_status,
                    state=_COMPLETED,
                    first_seen_at=now,
                    last_seen_at=now,
                    completed_at=now,
                )
                db.add(receipt)
            else:
                receipt.state = _COMPLETED
                receipt.last_seen_at = now
                receipt.completed_at = now
            db.commit()

    def release(self, broker_order_id: str, terminal_status: str) -> None:
        normalized_status = terminal_status.strip().upper()
        with _CLAIM_LOCK, SessionLocal() as db:
            db.query(OrderTerminalCallback).filter_by(
                broker_order_id=broker_order_id,
                terminal_status=normalized_status,
                state=_PROCESSING,
            ).delete()
            db.commit()
