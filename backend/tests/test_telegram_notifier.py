from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import httpx

from app.core.notifiers.telegram import TelegramNotifier


def test_telegram_send_posts_expected_payload() -> None:
    response = MagicMock(status_code=200)
    response.json.return_value = {"ok": True}

    with patch("app.core.notifiers.telegram.httpx.post", return_value=response) as post:
        notifier = TelegramNotifier("123:ABC", "-100123")

        result = notifier.send("Order filled", "AAPL.US bought", severity="INFO")

    assert result is True
    post.assert_called_once_with(
        "https://api.telegram.org/bot123:ABC/sendMessage",
        json={
            "chat_id": "-100123",
            "text": "<b>Order filled</b>\n\nAAPL.US bought",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=10.0,
    )


def test_telegram_send_returns_false_when_api_rejects_message() -> None:
    response = MagicMock(status_code=200)
    response.json.return_value = {"ok": False}

    with patch("app.core.notifiers.telegram.httpx.post", return_value=response):
        notifier = TelegramNotifier("123:ABC", "42")

        result = notifier.send("title", "body")

    assert result is False


def test_telegram_send_returns_false_on_network_exception_without_leaking_token(
    caplog,
) -> None:
    token = "123:SECRET_TOKEN"

    with patch(
        "app.core.notifiers.telegram.httpx.post",
        side_effect=httpx.ConnectError(
            f"failed to connect to https://api.telegram.org/bot{token}/sendMessage"
        ),
    ):
        notifier = TelegramNotifier(token, "42")
        with caplog.at_level(logging.WARNING, logger="auto_trade.notify.telegram"):
            result = notifier.send("title", "body")

    assert result is False
    assert token not in caplog.text


def test_telegram_send_formats_severity_and_escapes_html() -> None:
    response = MagicMock(status_code=200)
    response.json.return_value = {"ok": True}

    with patch("app.core.notifiers.telegram.httpx.post", return_value=response) as post:
        notifier = TelegramNotifier("123:ABC", "42")

        notifier.send("Risk <limit>", "Loss > $500 & rising", severity="CRITICAL")

    payload = post.call_args.kwargs["json"]
    assert payload["text"] == (
        "<b>🚨 Risk &lt;limit&gt;</b>\n\nLoss &gt; $500 &amp; rising"
    )
