from datetime import datetime, timezone
from decimal import Decimal

from app.database import engine
from app.models import Base
from app.platform.events import BarEvent, EventSource
from app.platform.store import EventStore


def test_store_persists_and_loads_event():
    Base.metadata.create_all(bind=engine)
    store = EventStore()
    store.clear()

    bar = BarEvent(
        timestamp=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="AAPL.US",
        open=Decimal("150"),
        high=Decimal("151"),
        low=Decimal("149"),
        close=Decimal("150.5"),
        volume=100,
    )
    store.append(bar)

    events = store.load(since=datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc))
    assert len(events) == 1
    loaded = events[0]
    assert isinstance(loaded, BarEvent)
    assert loaded.close == Decimal("150.5")
