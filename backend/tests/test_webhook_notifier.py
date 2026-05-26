from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from app.core.notifiers.webhook import WebhookNotifier


def test_webhook_sends_correct_payload() -> None:
    with patch("app.core.notifiers.webhook.httpx.post") as mock_post:
        mock_post.return_value.status_code = 200
        notifier = WebhookNotifier("https://example.com/hook")
        assert notifier.send("hello", "world", severity="CRITICAL") is True
        kwargs = mock_post.call_args.kwargs
        body = kwargs["json"]
        assert body["title"] == "hello"
        assert body["content"] == "world"
        assert body["severity"] == "CRITICAL"
        assert "timestamp" in body


def test_webhook_non_2xx_returns_false() -> None:
    with patch("app.core.notifiers.webhook.httpx.post") as mock_post:
        mock_post.return_value.status_code = 500
        notifier = WebhookNotifier("https://example.com/hook")
        assert notifier.send("t", "c") is False


def test_webhook_timeout_returns_false() -> None:
    with patch("app.core.notifiers.webhook.httpx.post", side_effect=httpx.ReadTimeout("timeout")):
        notifier = WebhookNotifier("https://example.com/hook")
        assert notifier.send("t", "c") is False


def test_webhook_empty_url_returns_false() -> None:
    notifier = WebhookNotifier("")
    assert notifier.send("t", "c") is False
