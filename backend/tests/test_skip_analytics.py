from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_skip_analytics_{os.getpid()}.db"
)

from app import database
from app.models import TradeEvent
from app.services.skip_analytics_service import (
    SkipAnalyticsService,
    _category_of,
)


database.init_db()


@pytest.fixture
def db() -> Iterator[Session]:
    session = database.SessionLocal()
    try:
        session.query(TradeEvent).delete()
        session.commit()
        yield session
    finally:
        session.close()


def test_regime_skip_event_has_complete_event_quality(db: Session) -> None:
    # Given: an ORDER_SKIPPED event emitted by the live regime gate.
    event = TradeEvent(
        event_type="ORDER_SKIPPED",
        symbol="AAPL.US",
        side="BUY",
        message="Regime gate blocked entry",
        payload_json='{"skip_category":"REGIME"}',
        created_at=datetime.now(timezone.utc),
    )
    db.add(event)
    db.commit()

    # When: category parsing and aggregate skip analytics consume the event.
    parsed = _category_of(event)
    result = SkipAnalyticsService(db).summary(days=30)

    # Then: REGIME is valid and does not degrade event quality.
    assert parsed.category == "REGIME"
    assert parsed.issue_code is None
    assert result["event_quality"]["status"] == "COMPLETE"
