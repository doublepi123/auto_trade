from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import EventLog
from app.platform.events import Event, event_from_dict


class EventStore:
    def __init__(self, db: Session | None = None) -> None:
        self._db = db

    def _session(self) -> Session:
        if self._db is not None:
            return self._db
        return SessionLocal()

    def append(self, event: Event) -> None:
        data = event.to_dict()
        record = EventLog(
            event_id=str(event.event_id),
            event_type=event.event_type,
            source=event.source.value,
            symbol=event.symbol,
            timestamp=event.timestamp,
            payload_json=json.dumps(data, default=str),
        )
        session = self._session()
        try:
            session.add(record)
            session.commit()
        finally:
            if self._db is None:
                session.close()

    def load(self, since: datetime | None = None, symbol: str | None = None, limit: int = 1000) -> list[Event]:
        session = self._session()
        try:
            query = session.query(EventLog)
            if since:
                query = query.filter(EventLog.timestamp >= since)
            if symbol:
                query = query.filter(EventLog.symbol == symbol)
            rows = query.order_by(EventLog.timestamp, EventLog.id).limit(limit).all()
            return [event_from_dict(json.loads(row.payload_json)) for row in rows]
        finally:
            if self._db is None:
                session.close()

    def clear(self) -> None:
        session = self._session()
        try:
            session.query(EventLog).delete()
            session.commit()
        finally:
            if self._db is None:
                session.close()
