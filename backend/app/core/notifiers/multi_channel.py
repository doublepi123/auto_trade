from __future__ import annotations

import json
import logging
from typing import Optional, Protocol

from app.core.notifiers._messages import (
    render_fill_body,
    render_fill_title,
    render_order_body,
    render_order_title,
    render_risk_body,
    render_risk_title,
    resolve_risk_severity,
    severity_for_risk_event,
)

logger = logging.getLogger("auto_trade.notify")

_SEVERITY_RANK = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}
_VALID_SEVERITY_FLOORS = set(_SEVERITY_RANK)


class NotifierInterface(Protocol):
    def send(self, title: str, content: str, severity: str = "INFO") -> bool: ...
    def notify_order(self, side: str, symbol: str, quantity: str, price: str, order_id: str) -> bool: ...
    def notify_fill(self, symbol: str, side: str, quantity: str, price: str) -> bool: ...
    def notify_risk_event(self, event_type: str, reason: str, *, severity: Optional[str] = None) -> bool: ...


class MultiChannelNotifier:
    def __init__(self, channels: list[tuple[NotifierInterface, str]]) -> None:
        self._channels = channels

    @property
    def sct_key(self) -> str:
        """First ServerChan channel key (tests and legacy callers)."""
        from app.core.notifiers.serverchan import ServerChanNotifier

        for notifier, _floor in self._channels:
            if isinstance(notifier, ServerChanNotifier):
                return notifier.sct_key
        return ""

    def send(self, title: str, content: str, severity: str = "INFO") -> bool:
        target_rank = _SEVERITY_RANK.get(severity, 0)
        success_any = False
        for notifier, floor in self._channels:
            if _SEVERITY_RANK.get(floor, 0) > target_rank:
                continue
            try:
                if notifier.send(title, content, severity):
                    success_any = True
            except Exception as exc:
                logger.warning("notifier %s send raised: %s", type(notifier).__name__, exc)
        if not success_any:
            logger.warning("all notifier channels failed: title=%s severity=%s", title, severity)
        return success_any

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

    def close(self) -> None:
        """Close all channel notifiers to release resources (e.g. httpx clients)."""
        for notifier, _floor in self._channels:
            close_fn = getattr(notifier, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception as exc:
                    logger.warning("notifier %s close raised: %s", type(notifier).__name__, exc)

    @classmethod
    def from_credential_config(cls, cred) -> MultiChannelNotifier:
        from app.core.notifiers.serverchan import ServerChanNotifier
        from app.core.notifiers.webhook import WebhookNotifier

        try:
            raw = json.loads(cred.notification_channels or "[]")
        except Exception as exc:
            logger.warning("notification_channels invalid JSON, falling back: %s", exc)
            return cls([(ServerChanNotifier(cred.sct_key or ""), "INFO")])
        if not isinstance(raw, list):
            logger.warning("notification_channels must be a JSON array, falling back")
            return cls([(ServerChanNotifier(cred.sct_key or ""), "INFO")])
        built: list[tuple[NotifierInterface, str]] = []
        for channel in raw:
            if not isinstance(channel, dict):
                logger.warning("notification_channels entry is not an object, skipping")
                continue
            channel_type = channel.get("type")
            floor = channel.get("severity_floor", "INFO")
            if floor not in _VALID_SEVERITY_FLOORS:
                logger.warning("notification_channels entry has invalid severity_floor=%r, defaulting to INFO", floor)
                floor = "INFO"
            if channel_type == "serverchan":
                built.append((ServerChanNotifier(cred.sct_key or ""), floor))
            elif channel_type == "webhook":
                url = channel.get("url", "")
                if url:
                    built.append((WebhookNotifier(url), floor))
        if not built:
            built = [(ServerChanNotifier(cred.sct_key or ""), "INFO")]
        return cls(built)


_severity_for_risk_event = severity_for_risk_event
