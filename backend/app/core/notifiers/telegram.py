from __future__ import annotations

import html
import logging
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

logger = logging.getLogger("auto_trade.notify.telegram")

_SEVERITY_PREFIX = {"INFO": "", "WARNING": "⚠️ ", "CRITICAL": "🚨 "}


class TelegramNotifier:
    BASE_URL = "https://api.telegram.org"

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._bot_token = (bot_token or "").strip()
        self._chat_id = (chat_id or "").strip()

    @property
    def bot_token(self) -> str:
        return self._bot_token

    @property
    def chat_id(self) -> str:
        return self._chat_id

    def send(self, title: str, content: str, severity: str = "INFO") -> bool:
        if not self._bot_token or not self._chat_id:
            return False
        prefix = _SEVERITY_PREFIX.get(severity, "")
        text = f"<b>{html.escape(prefix + title)}</b>\n\n{html.escape(content)}"
        try:
            response = httpx.post(
                f"{self.BASE_URL}/bot{self._bot_token}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10.0,
            )
            if response.status_code != 200:
                logger.warning("Telegram notification API returned non-200 status")
                return False
            try:
                return response.json().get("ok") is True
            except Exception:
                logger.warning("Telegram notification API returned an invalid response")
                return False
        except Exception:
            logger.warning("Telegram notification request failed")
            return False

    def notify_order(
        self,
        side: str,
        symbol: str,
        quantity: str,
        price: str,
        order_id: str,
    ) -> bool:
        return self.send(
            render_order_title(side),
            render_order_body(side, symbol, quantity, price, order_id),
            severity="INFO",
        )

    def notify_fill(
        self,
        symbol: str,
        side: str,
        quantity: str,
        price: str,
    ) -> bool:
        return self.send(
            render_fill_title(),
            render_fill_body(symbol, side, quantity, price),
            severity="INFO",
        )

    def notify_risk_event(
        self,
        event_type: str,
        reason: str,
        *,
        severity: Optional[str] = None,
    ) -> bool:
        return self.send(
            render_risk_title(event_type),
            render_risk_body(event_type, reason),
            severity=resolve_risk_severity(event_type, severity),
        )
