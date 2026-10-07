"""Outbound request lines must never put credentials into the logs.

Notifier URLs carry credentials in the path (ServerChan SendKey, Telegram bot
token). httpx logs every request line at INFO with the full URL, so with the
root logger at INFO each notification wrote the key into the container log.
"""

from __future__ import annotations

import logging

import httpx
import pytest

import app.main  # noqa: F401  (configures logging at import time)


def test_httpx_request_line_does_not_log_url_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "SCTfakeSendKey0123456789"
    caplog.set_level(logging.INFO)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"code": 0}),
    )
    with httpx.Client(transport=transport) as client:
        response = client.post(
            f"https://sctapi.ftqq.com/{secret}.send", data={"title": "t"},
        )
    assert response.status_code == 200
    assert secret not in caplog.text


def test_httpx_warnings_still_reach_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    logging.getLogger("httpx").warning("httpx warning still visible")
    assert "httpx warning still visible" in caplog.text
