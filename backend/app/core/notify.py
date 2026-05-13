from __future__ import annotations

import httpx
import logging

logger = logging.getLogger("auto_trade.notify")


class ServerChanNotifier:
    def __init__(self, sct_key: str) -> None:
        self._sct_key = sct_key
        self._url = f"https://sctapi.ftqq.com/{sct_key}.send"

    def send(self, title: str, content: str = "") -> bool:
        if not self._sct_key:
            return False
        try:
            resp = httpx.post(self._url, data={"title": title, "desp": content}, timeout=10)
            return resp.status_code == 200
        except Exception:
            logger.warning("ServerChan notification failed: title=%s", title)
            return False

    def notify_order(self, side: str, symbol: str, quantity: str, price: str, order_id: str) -> bool:
        title = f"[Auto Trade] {side} Order Submitted"
        content = f"Symbol: {symbol}\nSide: {side}\nQuantity: {quantity}\nPrice: {price}\nOrder ID: {order_id}"
        return self.send(title, content)

    def notify_risk_event(self, event_type: str, reason: str) -> bool:
        title = f"[Auto Trade] Risk Event: {event_type}"
        content = f"Type: {event_type}\nReason: {reason}"
        return self.send(title, content)

    def notify_fill(self, symbol: str, side: str, quantity: str, price: str) -> bool:
        title = "[Auto Trade] Order Filled"
        content = f"Symbol: {symbol}\nSide: {side}\nQuantity: {quantity}\nPrice: {price}"
        return self.send(title, content)
