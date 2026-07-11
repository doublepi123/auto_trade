from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
from typing import Any, Callable

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.config import settings
from app.models import AuditLog

logger = logging.getLogger("auto_trade.audit")


class AuditLogger:
    """Persist audit rows for sensitive ops. Failures are swallowed."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def record(
        self,
        action: str,
        *,
        severity: str = "INFO",
        actor_hash: str = "anonymous",
        source_ip: str = "",
        request_summary: dict[str, Any] | str = "",
        result: str = "SUCCESS",
    ) -> None:
        try:
            summary_str = self._normalize_summary(request_summary)
            with self._session_factory() as db:
                db.add(
                    AuditLog(
                        action=action,
                        severity=severity,
                        actor_hash=actor_hash,
                        source_ip=source_ip,
                        request_summary=summary_str,
                        result=result,
                    )
                )
                db.commit()
        except Exception as exc:
            logger.warning("audit write failed: action=%s err=%s", action, exc)

    def _normalize_summary(self, summary: dict[str, Any] | str) -> str:
        if isinstance(summary, dict):
            text = json.dumps(summary, ensure_ascii=False, default=str)
        else:
            text = str(summary)
        limit = settings.audit_request_summary_limit
        suffix = "...truncated"
        suffix_len = len(suffix.encode("utf-8"))
        text_bytes = text.encode("utf-8")
        if len(text_bytes) <= limit:
            return text
        # Single-shot UTF-8-safe truncation. Slicing raw bytes by the target
        # length can split a multi-byte sequence, so we decode with
        # errors="ignore" to drop the truncated tail bytes. Avoids the
        # O(n^2) re-encode cost of trimming character-by-character.
        target = limit - suffix_len
        if target <= 0:
            return suffix[:limit]
        truncated = text_bytes[:target].decode("utf-8", errors="ignore")
        return truncated + suffix

    @staticmethod
    def hash_actor(api_key: str | None) -> str:
        if not api_key:
            return "anonymous"
        digest = hashlib.sha256(api_key.encode("utf-8")).digest()
        return digest[:16].hex()

    @staticmethod
    def extract_ip(request: Request) -> str:
        """Resolve the request source IP for the audit log.

        Forwarded identity is accepted only when the socket peer belongs to an
        explicitly configured trusted proxy network. This keeps direct and
        development requests from spoofing audit attribution.
        """
        peer = request.client.host if request.client else ""
        if not peer:
            return ""
        try:
            peer_ip = ipaddress.ip_address(peer)
            trusted = any(
                peer_ip in ipaddress.ip_network(item.strip(), strict=False)
                for item in settings.audit_trusted_proxy_cidrs.split(",")
                if item.strip()
            )
        except ValueError:
            trusted = False
        if trusted:
            forwarded = request.headers.get("x-real-ip", "").strip()
            try:
                if forwarded:
                    return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        return peer
