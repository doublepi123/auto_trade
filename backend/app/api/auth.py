from __future__ import annotations

import logging
import secrets
from typing import Any, Callable

from fastapi import HTTPException, Request

from app.config import settings

logger = logging.getLogger("auto_trade.auth")

_auth_disabled_warned = False


def require_api_key() -> Callable[..., Any]:
    def dependency(request: Request) -> None:
        """Validate X-API-Key header against configured AUTO_TRADE_API_KEY.

        Behavior when api_key is empty:
          - dev/test: auth is disabled (requests pass through).
          - non-dev/test: requests are rejected with 401 ("API key not configured").
            This means intranet users in prod MUST set AUTO_TRADE_API_KEY,
            even though CLAUDE.md notes 'API_KEY can be left empty for intranet'.
            To run without auth in production, set env=AUTO_TRADE_ENV=dev.
        """
        global _auth_disabled_warned
        provided = request.headers.get("X-API-Key", "")
        if not settings.api_key:
            if settings.env not in ("dev", "test"):
                logger.error("AUTO_TRADE_API_KEY not configured in %s environment — rejecting request", settings.env)
                raise HTTPException(status_code=401, detail="API key not configured")
            if not _auth_disabled_warned:
                logger.warning("AUTO_TRADE_API_KEY not configured - auth disabled (dev/test mode only)")
                _auth_disabled_warned = True
            return
        if not provided or not secrets.compare_digest(provided, settings.api_key):
            logger.warning("invalid or missing API key from %s", request.client)
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return dependency
