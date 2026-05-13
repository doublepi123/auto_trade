from __future__ import annotations

import logging
import secrets
from typing import Callable

from fastapi import HTTPException, Request

from app.config import settings

logger = logging.getLogger("auto_trade.auth")

_auth_disabled_warned = False


def require_api_key() -> Callable:
    def dependency(request: Request) -> None:
        global _auth_disabled_warned
        if not settings.api_key:
            if not _auth_disabled_warned:
                logger.warning("AUTO_TRADE_API_KEY not configured - auth disabled")
                _auth_disabled_warned = True
            return
        provided = request.headers.get("X-API-Key", "")
        if not provided or not secrets.compare_digest(provided, settings.api_key):
            logger.warning("invalid or missing API key from %s", request.client)
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return dependency
