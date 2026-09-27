"""Quote entitlement API — read-only, authenticated, process-local.

Exposes the cached result of the quote-entitlement cron (see
``app/services/quote_entitlement_service.py``). The endpoint performs no
I/O, database, or broker work: it returns the last assessment cached by the
6-hour observation cron, and never pauses, resumes, or alters any trading
decision.

When no assessment has run yet (fresh process before the startup tick) it
returns a validated ``QuoteEntitlementResponse`` payload with HTTP 503 and
``status="UNKNOWN"`` — it never synthesizes an OK verdict.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from app.api.auth import require_api_key
from app.schemas import QuoteEntitlementResponse
from app.services.quote_entitlement_service import (
    QuoteEntitlementService,
    get_quote_entitlement_service,
)

router = APIRouter(
    prefix="/api",
    tags=["system"],
    dependencies=[Depends(require_api_key())],
)


def _unassessed_payload() -> QuoteEntitlementResponse:
    """Build the typed UNKNOWN payload used before the first tick."""
    from datetime import datetime, timezone

    return QuoteEntitlementResponse(
        market="",
        status="UNKNOWN",
        package_key="",
        end_at=None,
        days_left=None,
        reason="quote entitlement has not been assessed yet",
    )


@router.get(
    "/quote-entitlement",
    response_model=QuoteEntitlementResponse,
    responses={
        503: {
            "description": (
                "No entitlement assessment has completed yet. The body is "
                "still a validated QuoteEntitlementResponse with "
                "status='UNKNOWN'."
            ),
            "model": QuoteEntitlementResponse,
        },
    },
)
def get_quote_entitlement(response: Response) -> QuoteEntitlementResponse:
    """Read-only cached quote-entitlement snapshot (authenticated).

    Returns HTTP 200 with the cron's cached assessment, or HTTP 503 with a
    validated ``UNKNOWN`` payload when no assessment has run yet. Never
    instantiates ``AppRunner``/``BrokerGateway``, contacts the broker, or
    changes any trading decision.
    """
    service: QuoteEntitlementService = get_quote_entitlement_service()
    result = service.last_result()
    if result is None:
        response.status_code = 503
        return _unassessed_payload()
    return QuoteEntitlementResponse(
        market=result.market,
        status=result.status,
        package_key=result.package_key,
        end_at=result.end_at,
        days_left=result.days_left,
        reason=result.reason,
    )
