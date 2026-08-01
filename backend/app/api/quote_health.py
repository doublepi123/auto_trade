"""Quote stream health API — read-only, authenticated, process-local.

Exposes a safe snapshot of the runner's primary-symbol quote stream health:
quotes received, last quote timestamp/age, maximum observed gap between
consecutive trusted primary quotes, disconnect/resubscribe/retry counts, and
a status verdict. The endpoint performs no I/O, database, or broker work and
never subscribes, reconnects, pauses, or alters the quote freshness threshold.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.auth import require_api_key
from app.schemas import QuoteStreamHealth
from app.services.quote_stream_health_service import QuoteStreamHealthTracker

router = APIRouter(
    prefix="/api",
    tags=["system"],
    dependencies=[Depends(require_api_key())],
)


def get_quote_stream_health_tracker() -> QuoteStreamHealthTracker:
    """Return the runner's bound quote-stream health tracker.

    Resolved lazily from the runner so the endpoint never constructs a tracker
    or touches the broker. If the runner is unavailable, a fresh empty tracker
    is returned (all-zero snapshot, status ``disconnected``).
    """
    from app.runner import get_runner

    try:
        runner = get_runner()
        tracker = getattr(runner, "quote_stream_health", None)
        if isinstance(tracker, QuoteStreamHealthTracker):
            return tracker
    except Exception:
        pass
    return QuoteStreamHealthTracker()


@router.get("/quote-health", response_model=QuoteStreamHealth)
def get_quote_health() -> QuoteStreamHealth:
    """Read-only process-local quote stream health snapshot (authenticated)."""
    tracker = get_quote_stream_health_tracker()
    snap = tracker.snapshot()
    return QuoteStreamHealth(
        symbol=snap.symbol,
        quotes_received=snap.quotes_received,
        last_quote_timestamp=snap.last_quote_timestamp,
        last_quote_age_seconds=snap.last_quote_age_seconds,
        max_gap_seconds=snap.max_gap_seconds,
        disconnect_count=snap.disconnect_count,
        resubscribe_count=snap.resubscribe_count,
        disconnect_retry_count=snap.disconnect_retry_count,
        quotes_subscribed=snap.quotes_subscribed,
        status=snap.status,
        as_of=snap.as_of,
    )