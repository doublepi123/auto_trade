"""URL safety helpers for user-supplied outbound HTTP targets."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


def _reject_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
    ):
        raise ValueError("webhook url must not target private or link-local addresses")


def validate_webhook_url(url: str) -> str:
    """Validate a webhook URL for server-side POST use.

    Requires https and rejects private, loopback, and link-local targets.
    """
    parsed = urlparse((url or "").strip())
    if parsed.scheme != "https":
        raise ValueError("webhook url must use https")
    host = parsed.hostname
    if not host:
        raise ValueError("webhook url must include a host")
    if host.lower() in {"localhost", "metadata.google.internal"}:
        raise ValueError("webhook url host is not allowed")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        try:
            for family in (socket.AF_INET, socket.AF_INET6):
                for resolved in socket.getaddrinfo(host, None, family, socket.SOCK_STREAM):
                    addr_str = resolved[4][0]
                    try:
                        parsed_addr = ipaddress.ip_address(addr_str)
                    except ValueError:
                        continue
                    _reject_ip(parsed_addr)
        except OSError as exc:
            raise ValueError(f"webhook url host could not be resolved: {host}") from exc
        return url
    _reject_ip(addr)
    return url
