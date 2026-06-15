from __future__ import annotations

import ipaddress

import pytest

from app.core.url_safety import _reject_ip, _CLOUD_METADATA_IP, _CLOUD_METADATA_IP_V6
from app.core.url_safety import validate_webhook_url


def test_validate_webhook_url_requires_https() -> None:
    with pytest.raises(ValueError, match="https"):
        validate_webhook_url("http://example.com/hook")


def test_validate_webhook_url_rejects_loopback_ip() -> None:
    with pytest.raises(ValueError, match="private"):
        validate_webhook_url("https://127.0.0.1/hook")


class TestRejectIp:
    """B7: _reject_ip must catch IPv4-mapped IPv6 cloud metadata addresses."""

    def test_ipv4_cloud_metadata_rejected(self) -> None:
        with pytest.raises(ValueError, match="private|link-local"):
            _reject_ip(_CLOUD_METADATA_IP)

    def test_ipv6_mapped_cloud_metadata_rejected(self) -> None:
        """IPv4-mapped IPv6 (::ffff:169.254.169.254) must be rejected."""
        with pytest.raises(ValueError, match="private|link-local"):
            _reject_ip(_CLOUD_METADATA_IP_V6)

    def test_ipv6_mapped_private_ip_rejected(self) -> None:
        """IPv4-mapped IPv6 of a private address must also be rejected."""
        addr = ipaddress.ip_address("::ffff:10.0.0.1")
        with pytest.raises(ValueError, match="private|link-local"):
            _reject_ip(addr)

    def test_public_ipv6_allowed(self) -> None:
        """Public IPv6 addresses must pass validation."""
        addr = ipaddress.ip_address("2001:470::")
        # Should not raise
        _reject_ip(addr)
