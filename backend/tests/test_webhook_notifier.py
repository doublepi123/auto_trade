from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.core.notifiers.webhook import WebhookNotifier


def test_webhook_sends_correct_payload() -> None:
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.post.return_value = mock_response

    with patch("app.core.notifiers.webhook.validated_httpx_client", return_value=mock_client):
        notifier = WebhookNotifier("https://93.184.216.34/hook")
        assert notifier.send("hello", "world", severity="CRITICAL") is True
        kwargs = mock_client.post.call_args.kwargs
        body = kwargs["json"]
        assert body["title"] == "hello"
        assert body["content"] == "world"
        assert body["severity"] == "CRITICAL"
        assert "timestamp" in body


def test_webhook_non_2xx_returns_false() -> None:
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_client.post.return_value = mock_response

    with patch("app.core.notifiers.webhook.validated_httpx_client", return_value=mock_client):
        notifier = WebhookNotifier("https://93.184.216.34/hook")
        assert notifier.send("t", "c") is False


def test_webhook_timeout_returns_false() -> None:
    mock_client = MagicMock()
    mock_client.post.side_effect = httpx.ReadTimeout("timeout")

    with patch("app.core.notifiers.webhook.validated_httpx_client", return_value=mock_client):
        notifier = WebhookNotifier("https://93.184.216.34/hook")
        assert notifier.send("t", "c") is False


def test_webhook_empty_url_returns_false() -> None:
    notifier = WebhookNotifier("")
    assert notifier.send("t", "c") is False


def test_webhook_log_omits_url_on_failure(caplog) -> None:
    """The full webhook URL (which may embed credentials) must not leak to logs."""
    import logging

    sensitive_url = "https://hooks.slack.com/services/T000/B000/SECRETTOKEN123"
    mock_client = MagicMock()
    mock_client.post.side_effect = httpx.ConnectError("connection refused")

    with patch("app.core.notifiers.webhook.validated_httpx_client", return_value=mock_client):
        notifier = WebhookNotifier(sensitive_url)
        with caplog.at_level(logging.WARNING, logger="auto_trade.notify.webhook"):
            assert notifier.send("t", "c") is False

    full_log = caplog.text
    assert "SECRETTOKEN123" not in full_log
    assert "hooks.slack.com" in full_log  # hostname is safe to log
