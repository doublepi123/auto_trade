from __future__ import annotations

import threading

from fastapi import Request

from app import database
from app.core.audit import AuditLogger

_audit_logger_singleton: AuditLogger | None = None
_audit_logger_lock = threading.Lock()


def init_audit_logger() -> AuditLogger:
    """Called once at app startup; returns the shared instance."""
    global _audit_logger_singleton
    if _audit_logger_singleton is not None:
        return _audit_logger_singleton
    with _audit_logger_lock:
        if _audit_logger_singleton is None:
            _audit_logger_singleton = AuditLogger(database.SessionLocal)
        return _audit_logger_singleton


def get_audit_logger() -> AuditLogger:
    return init_audit_logger()


def extract_actor(request: Request) -> tuple[str, str]:
    """Returns (actor_hash, source_ip) for use in API handlers."""
    api_key = request.headers.get("x-api-key")
    return AuditLogger.hash_actor(api_key), AuditLogger.extract_ip(request)
