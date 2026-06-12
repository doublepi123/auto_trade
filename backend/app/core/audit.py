from __future__ import annotations

import hashlib
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
        # Account for the suffix length so the final result stays within the
        # byte limit.  Trim character-by-character from the end until the
        # encoded length fits within (limit - suffix_len) bytes.  This avoids
        # slicing raw bytes which can split a multi-byte UTF-8 sequence.
        target = limit - suffix_len
        while text and len(text.encode("utf-8")) > target:
            text = text[:-1]
        return text + suffix

    @staticmethod
    def hash_actor(api_key: str | None) -> str:
        if not api_key:
            return "anonymous"
        digest = hashlib.sha256(api_key.encode("utf-8")).digest()
        return digest[:16].hex()

    @staticmethod
    def extract_ip(request: Request) -> str:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
        if request.client:
            return request.client.host
        return ""
