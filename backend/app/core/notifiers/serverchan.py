from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

from app.core.notifiers._messages import (
    render_fill_body,
    render_fill_title,
    render_order_body,
    render_order_title,
    render_risk_body,
    render_risk_title,
    resolve_risk_severity,
)

logger = logging.getLogger("auto_trade.notify.serverchan")


_SEVERITY_PREFIX = {"INFO": "", "WARNING": "⚠️ ", "CRITICAL": "🚨 "}


class ServerChanNotifier:
    BASE_URL: str = "https://sctapi.ftqq.com/"

    def __init__(self, sct_key: str) -> None:
        if sct_key and not re.match(r"^[A-Za-z0-9]+$", sct_key):
            raise ValueError(f"Invalid sct_key: must match ^[A-Za-z0-9]+$")
        self._sct_key = sct_key

    @property
    def sct_key(self) -> str:
        return self._sct_key

    def send(self, title: str, content: str, severity: str = "INFO") -> bool:
        if not self._sct_key:
            return False
        prefix = _SEVERITY_PREFIX.get(severity, "")
        try:
            url = f"{self.BASE_URL}{self._sct_key}.send"
            resp = httpx.post(
                url,
                data={"title": f"{prefix}{title}", "desp": content},
                timeout=10,
            )
            if resp.status_code != 200:
                return False
            try:
                data = resp.json()
                return data.get("code") == 0
            except Exception:
                return False
        except Exception:
            logger.warning("ServerChan notification failed: title=%s", title)
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
