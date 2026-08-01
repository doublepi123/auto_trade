"""Quote stream health API — read-only, authenticated, process-local.

Exposes a safe snapshot of the runner's primary-symbol quote stream health:
quotes received, last quote timestamp/age, maximum observed gap between
consecutive trusted primary push quotes, disconnect/resubscribe/retry counts,
and a status verdict. The endpoint performs no I/O, database, or broker work
and never subscribes, reconnects, pauses, or alters the quote freshness
threshold.

This endpoint never constructs an ``AppRunner``/``BrokerGateway`` or contacts
the broker. It uses :func:`app.runner.peek_runner` (non-constructing) to look
up the current runner's bound tracker. If no runner/tracker exists, it returns
an explicit ``unavailable`` response with HTTP 503 — it never synthesizes an
all-zero disconnected tracker.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api.auth import require_api_key
from app.runner import peek_runner
from app.schemas import QuoteStreamHealth
from app.services.quote_stream_health_service import QuoteStreamHealthTracker

router = APIRouter(
    prefix="/api",
    tags=["system"],
    dependencies=[Depends(require_api_key())],
)


def _peek_quote_stream_health_tracker() -> QuoteStreamHealthTracker | None:
    """Return the runner's bound quote-stream health tracker without
    constructing a runner.

    Uses :func:`peek_runner` (non-constructing). Returns ``None`` when no
    runner exists or the runner has no bound tracker, so the endpoint can
    return an explicit unavailable response instead of synthesizing state.
    """
    try:
        runner = peek_runner()
        if runner is None:
            return None
        tracker = getattr(runner, "quote_stream_health", None)
        if isinstance(tracker, QuoteStreamHealthTracker):
            return tracker
    except Exception:
        pass
    return None


@router.get("/quote-health", response_model=None)
def get_quote_health() -> JSONResponse | QuoteStreamHealth:
    """Read-only process-local quote stream health snapshot (authenticated).

    Returns HTTP 503 with an ``unavailable`` status when no runner/tracker
    exists, so callers can distinguish "no runtime yet" from a known
    disconnected stream. Never instantiates ``AppRunner``/``BrokerGateway``
    or contacts the broker.
    """
    tracker = _peek_quote_stream_health_tracker()
    if tracker is None:
        return JSONResponse(
            status_code=503,
            content={
                "symbol": "",
                "quotes_received": 0,
                "last_quote_timestamp": None,
                "last_quote_age_seconds": None,
                "max_gap_seconds": 0.0,
                "disconnect_count": 0,
                "resubscribe_count": 0,
                "disconnect_retry_count": 0,
                "quotes_subscribed": False,
                "status": "unavailable",
                "as_of": datetime.now(timezone.utc).isoformat(),
            },
        )
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