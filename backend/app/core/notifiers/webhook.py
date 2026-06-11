from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.core.url_safety import validate_webhook_url
from app.core.notifiers._messages import (
    render_fill_body,
    render_fill_title,
    render_order_body,
    render_order_title,
    render_risk_body,
    render_risk_title,
    resolve_risk_severity,
)

logger = logging.getLogger("auto_trade.notify.webhook")


class WebhookNotifier:
    def __init__(self, url: str, *, timeout: float = 10.0) -> None:
        self._url = validate_webhook_url(url) if (url or "").strip() else ""
        self._timeout = timeout

    def send(self, title: str, content: str, severity: str = "INFO") -> bool:
        if not self._url:
            return False
        payload = {
            "title": title,
            "content": content,
            "severity": severity,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            resp = httpx.post(self._url, json=payload, timeout=self._timeout, follow_redirects=False)
            return 200 <= resp.status_code < 300
        except Exception as exc:
            logger.warning("webhook send failed (%s): %s", self._url, exc)
            return False

    def notify_order(self, side: str, symbol: str, quantity: str, price: str, order_id: str) -> bool:
        return self.send(
            render_order_title(side),
            render_order_body(side, symbol, quantity, price, order_id),
            severity="INFO",
        )

    def notify_fill(self, symbol: str, side: str, quantity: str, price: str) -> bool:
        return self.send(
            render_fill_title(),
            render_fill_body(symbol, side, quantity, price),
            severity="INFO",
        )

    def notify_risk_event(self, event_type: str, reason: str, *, severity: Optional[str] = None) -> bool:
        return self.send(
            render_risk_title(event_type),
            render_risk_body(event_type, reason),
            severity=resolve_risk_severity(event_type, severity),
        )
