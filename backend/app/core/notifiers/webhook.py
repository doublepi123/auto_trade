from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.url_safety import validated_httpx_client
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


# Tokens allowed in a user-supplied webhook payload template. We deliberately
# do not pass the raw title/content through format_map — the template author
# may only reference the well-known fields below. Any unknown ``{token}``
# raises at template validation time and disables the template, falling
# back to the legacy fixed-schema payload.
_ALLOWED_TEMPLATE_TOKENS = frozenset(
    {"title", "content", "severity", "timestamp", "source"}
)


class _TemplateError(ValueError):
    pass


def _validate_template(template: str) -> str:
    """Validate and normalize a user-supplied payload template.

    The template is a JSON object string with optional ``{token}`` placeholders
    that get substituted from the message metadata. We do not parse it as
    JSON until substitution is done so the template author can write things
    like ``"text": "{content}"`` and we substitute before parsing.
    """
    if not template or not template.strip():
        raise _TemplateError("template is empty")
    stripped = template.strip()
    if not stripped.startswith("{"):
        raise _TemplateError(
            "template must be a JSON object (start with '{') so the rendered "
            "payload is a structured webhook body"
        )
    # Reject any control characters that could break HTTP framing.
    if any(ord(c) < 0x20 and c not in "\t\n" for c in template):
        raise _TemplateError("template contains control characters")
    # All placeholders must be in the allowlist.
    placeholders = set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", template))
    unknown = placeholders - _ALLOWED_TEMPLATE_TOKENS
    if unknown:
        raise _TemplateError(
            f"template references unknown tokens: {sorted(unknown)}"
        )
    return template


def _render_template(
    template: str, *, title: str, content: str, severity: str
) -> dict[str, Any]:
    """Substitute placeholders and parse the result as a JSON object.

    We first parse the template as a JSON object so we can identify the
    string-valued fields whose contents may contain ``{token}``
    placeholders. Only top-level string values are substituted, and only
    with tokens from the allowlist. This avoids the brace-escaping
    problem of ``str.format`` against a JSON literal.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    source = "auto_trade"
    values: dict[str, str] = {
        "title": title,
        "content": content,
        "severity": severity,
        "timestamp": timestamp,
        "source": source,
    }
    placeholder_pattern = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

    def _substitute(text: str) -> str:
        def _replace(match: re.Match[str]) -> str:
            token = match.group(1)
            if token not in _ALLOWED_TEMPLATE_TOKENS:
                raise _TemplateError(f"unknown template token: {token}")
            return values[token]
        # Iterate to surface a clear error if the template has an unterminated
        # brace (would otherwise be silently ignored by re.sub).
        return placeholder_pattern.sub(_replace, text)

    try:
        parsed_raw = json.loads(template)
    except json.JSONDecodeError as exc:
        raise _TemplateError(f"template does not parse as JSON: {exc}") from exc
    if not isinstance(parsed_raw, dict):
        raise _TemplateError("template must produce a JSON object")
    parsed: dict[str, Any] = {}
    for key, value in parsed_raw.items():
        if isinstance(value, str):
            parsed[key] = _substitute(value)
        else:
            parsed[key] = value
    return parsed


class WebhookNotifier:
    def __init__(
        self,
        url: str,
        *,
        timeout: float = 10.0,
        template: Optional[str] = None,
    ) -> None:
        self._url = (url or "").strip()
        self._client = None
        self._template: Optional[str] = None
        if template is not None:
            try:
                self._template = _validate_template(template)
            except _TemplateError as exc:
                logger.warning(
                    "webhook template validation failed, falling back to default: %s",
                    exc,
                )
                self._template = None
        if self._url:
            try:
                self._client = validated_httpx_client(self._url, timeout=timeout)
            except ValueError as exc:
                logger.error("webhook url validation failed: %s", exc)
                self._url = ""

    def _build_payload(
        self, title: str, content: str, severity: str
    ) -> dict[str, Any]:
        if self._template is None:
            return {
                "title": title,
                "content": content,
                "severity": severity,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        try:
            return _render_template(
                self._template, title=title, content=content, severity=severity
            )
        except _TemplateError as exc:
            logger.warning(
                "webhook template render failed, falling back to default: %s",
                exc,
            )
            return {
                "title": title,
                "content": content,
                "severity": severity,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    def send(self, title: str, content: str, severity: str = "INFO") -> bool:
        if not self._url or self._client is None:
            return False
        payload = self._build_payload(title, content, severity)
        try:
            resp = self._client.post(self._url, json=payload)
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

    def close(self) -> None:
        """Close the underlying httpx client to release connections."""
        if self._client is not None:
            self._client.close()


# Exposed for testing.
__all__ = [
    "WebhookNotifier",
    "_validate_template",
    "_render_template",
    "_ALLOWED_TEMPLATE_TOKENS",
    "_TemplateError",
]
