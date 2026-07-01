"""URL safety helpers for user-supplied outbound HTTP targets."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx


_CLOUD_METADATA_IP = ipaddress.ip_address("169.254.169.254")
_CLOUD_METADATA_IP_V6 = ipaddress.ip_address("::ffff:169.254.169.254")


def _reject_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    # IPv4-mapped IPv6 addresses (e.g. ::ffff:169.254.169.254) can bypass
    # the IPv4 _CLOUD_METADATA_IP comparison.  Extract the mapped IPv4 for
    # comparison when the address has an ipv4_mapped attribute.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        v4_part: ipaddress.IPv4Address | None = addr.ipv4_mapped
    else:
        v4_part = None
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or addr == _CLOUD_METADATA_IP
        or addr == _CLOUD_METADATA_IP_V6
        or (v4_part is not None and v4_part == _CLOUD_METADATA_IP)
    ):
        raise ValueError("webhook url must not target private or link-local addresses")


def validate_webhook_url(url: str) -> str:
    """Validate a webhook URL for server-side POST use.

    Requires https and rejects private, loopback, link-local, unspecified,
    and cloud-metadata targets.
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
        validated_any = False
        try:
            for family in (socket.AF_INET, socket.AF_INET6):
                for resolved in socket.getaddrinfo(host, None, family, socket.SOCK_STREAM):
                    addr_str = resolved[4][0]
                    try:
                        parsed_addr = ipaddress.ip_address(addr_str)
                    except ValueError:
                        continue
                    _reject_ip(parsed_addr)
                    validated_any = True
        except OSError as exc:
            raise ValueError(f"webhook url host could not be resolved: {host}") from exc
        if not validated_any:
            raise ValueError(f"webhook url host resolved to no valid addresses: {host}")
        return url
    _reject_ip(addr)
    return url


class _PinnedTransport(httpx.BaseTransport):
    """Custom httpx transport that connects to a pre-validated IP address.

    Pins the resolved IP at client-creation time so that a DNS rebinding
    attack cannot redirect the actual HTTP request to a different (e.g.
    internal) host after URL validation has passed.
    """

    def __init__(self, resolved_ip: str, original_host: str) -> None:
        self._resolved_ip = resolved_ip
        self._original_host = original_host
        self._transport = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # Rewrite the URL to use the pinned IP, preserving the original
        # Host header so SNI / virtual-host routing still works.
        pinned_url = request.url.copy_with(host=self._resolved_ip)
        request.url = pinned_url
        # Ensure the Host header reflects the original hostname for SNI.
        if "host" not in request.headers:
            request.headers["host"] = self._original_host
        # Set SNI hostname so SSL certificate verification succeeds against
        # the original hostname rather than the pinned IP.
        request.extensions["sni_hostname"] = self._original_host
        return self._transport.handle_request(request)


def validated_httpx_client(url: str, *, timeout: float = 10.0) -> httpx.Client:
    """Create an httpx.Client whose transport pins the validated DNS resolution.

    Resolves the hostname, validates the IP against private/link-local ranges,
    then builds a client with a custom transport that connects to the pinned IP
    directly, preventing TOCTOU DNS rebinding between validation and request.
    """
    validate_webhook_url(url)

    parsed = urlparse(url)
    host = parsed.hostname
    assert host is not None  # validated above

    # Try to resolve the host to an IP we can pin.
    resolved_ip: str | None = None
    try:
        ipaddress.ip_address(host)
        resolved_ip = host  # already an IP literal
    except ValueError:
        for family in (socket.AF_INET, socket.AF_INET6):
            for resolved in socket.getaddrinfo(host, None, family, socket.SOCK_STREAM):
                addr_str = str(resolved[4][0])
                try:
                    parsed_addr = ipaddress.ip_address(addr_str)
                except ValueError:
                    continue
                # Re-validate the resolved IP (defense-in-depth).
                _reject_ip(parsed_addr)
                resolved_ip = addr_str
                break
            if resolved_ip is not None:
                break

    transport = _PinnedTransport(resolved_ip or host, host)
    return httpx.Client(transport=transport, timeout=timeout, follow_redirects=False)
