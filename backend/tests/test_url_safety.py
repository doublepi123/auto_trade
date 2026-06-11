from __future__ import annotations

import pytest

from app.core.url_safety import validate_webhook_url


def test_validate_webhook_url_requires_https() -> None:
    with pytest.raises(ValueError, match="https"):
        validate_webhook_url("http://example.com/hook")


def test_validate_webhook_url_rejects_loopback_ip() -> None:
    with pytest.raises(ValueError, match="private"):
        validate_webhook_url("https://127.0.0.1/hook")
