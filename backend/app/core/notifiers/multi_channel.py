from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Callable, Optional, Protocol

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
    def __init__(
        self,
        channels: list[tuple[NotifierInterface, str]],
        *,
        retry_queue: Optional["_RetryQueueProtocol"] = None,
        sink: Optional[Callable[[str, str, str, bool, str], None]] = None,
        dedup_window_seconds: float = 0.0,
    ) -> None:
        self._channels = channels
        # Optional retry queue; when present, failed sends are rescheduled
        # with exponential backoff. See retry_queue.NotificationRetryQueue.
        self._retry_queue = retry_queue
        # Optional dispatch-log sink: (title, content, severity, success, error).
        # The runner attaches a NotificationLogSink so every notification is
        # auditable. Best-effort — the sink itself swallows errors.
        self._sink = sink
        self._dedup_window_seconds = dedup_window_seconds
        self._dedup_success_at: dict[str, float] = {}
        self._dedup_suppressed_total = 0
        self._dedup_lock = threading.Lock()

    @property
    def dedup_suppressed_total(self) -> int:
        with self._dedup_lock:
            return self._dedup_suppressed_total

    @property
    def dedup_window_seconds(self) -> float:
        return self._dedup_window_seconds

    @property
    def sct_key(self) -> str:
        """First ServerChan channel key (tests and legacy callers)."""
        from app.core.notifiers.serverchan import ServerChanNotifier

        for notifier, _floor in self._channels:
            if isinstance(notifier, ServerChanNotifier):
                return notifier.sct_key
        return ""

    def send(self, title: str, content: str, severity: str = "INFO") -> bool:
        if self._dedup_window_seconds <= 0 or severity not in {"INFO", "WARNING"}:
            return self._dispatch(title, content, severity)

        with self._dedup_lock:
            now = time.monotonic()
            expired = [
                key
                for key, sent_at in self._dedup_success_at.items()
                if now - sent_at >= self._dedup_window_seconds
            ]
            for key in expired:
                del self._dedup_success_at[key]
            fingerprint = hashlib.sha256(
                f"{title}\x1f{content}".encode("utf-8")
            ).hexdigest()
            if fingerprint in self._dedup_success_at:
                self._dedup_suppressed_total += 1
                return True

            success_any = self._dispatch(title, content, severity)
            if success_any:
                self._dedup_success_at[fingerprint] = time.monotonic()
            return success_any

    def _dispatch(self, title: str, content: str, severity: str) -> bool:
        target_rank = _SEVERITY_RANK.get(severity, 0)
        success_any = False
        last_error = ""
        for notifier, floor in self._channels:
            if _SEVERITY_RANK.get(floor, 0) > target_rank:
                continue
            try:
                if notifier.send(title, content, severity):
                    success_any = True
            except Exception as exc:
                last_error = f"{type(notifier).__name__}: {exc}"
                logger.warning("notifier %s send raised: %s", type(notifier).__name__, exc)
        if self._sink is not None:
            try:
                self._sink(title, content, severity, success_any, last_error)
            except Exception:
                logger.debug("notification sink raised", exc_info=True)
        if not success_any:
            logger.warning("all notifier channels failed: title=%s severity=%s", title, severity)
            if self._retry_queue is not None:
                self._retry_queue.enqueue(title, content, severity, error=last_error)
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
    def from_credential_config(
        cls,
        cred,
        *,
        retry_queue: Optional["_RetryQueueProtocol"] = None,
        sink: Optional[Callable[[str, str, str, bool, str], None]] = None,
        dedup_window_seconds: float = 0.0,
    ) -> "MultiChannelNotifier":
        from app.core.notifiers.serverchan import ServerChanNotifier
        from app.core.notifiers.telegram import TelegramNotifier
        from app.core.notifiers.webhook import WebhookNotifier

        try:
            raw = json.loads(cred.notification_channels or "[]")
        except Exception as exc:
            logger.warning("notification_channels invalid JSON, falling back: %s", exc)
            return cls(
                [(ServerChanNotifier(cred.sct_key or ""), "INFO")],
                retry_queue=retry_queue,
                sink=sink,
                dedup_window_seconds=dedup_window_seconds,
            )
        if not isinstance(raw, list):
            logger.warning("notification_channels must be a JSON array, falling back")
            return cls(
                [(ServerChanNotifier(cred.sct_key or ""), "INFO")],
                retry_queue=retry_queue,
                sink=sink,
                dedup_window_seconds=dedup_window_seconds,
            )
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
                    template = channel.get("template")
                    built.append((WebhookNotifier(url, template=template), floor))
            elif channel_type == "telegram":
                bot_token = channel.get("bot_token", "")
                chat_id = channel.get("chat_id", "")
                if bot_token and chat_id:
                    built.append((TelegramNotifier(bot_token, chat_id), floor))
                else:
                    logger.warning(
                        "notification_channels telegram entry is missing bot_token or chat_id, skipping"
                    )
        if not built:
            built = [(ServerChanNotifier(cred.sct_key or ""), "INFO")]
        return cls(
            built,
            retry_queue=retry_queue,
            sink=sink,
            dedup_window_seconds=dedup_window_seconds,
        )


# Module-level (not nested inside MultiChannelNotifier) protocol declared
# at import time so it can be referenced in type annotations and runtime
# isinstance checks. The retry queue module imports this protocol to
# break the import cycle.
class _RetryQueueProtocol(Protocol):
    def enqueue(self, title: str, content: str, severity: str, error: str = "") -> bool: ...


_severity_for_risk_event = severity_for_risk_event
