from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.api.deps import extract_actor, get_audit_logger
from app.core.audit import AuditLogger
from app.database import get_db
from app.schemas import CredentialConfigSchema, CredentialResponse
from app.runner import CredentialSwitchBlockedError, get_runner
from app.services.credentials_service import CredentialsService

router = APIRouter(prefix="/api", tags=["credentials"])
logger = logging.getLogger("auto_trade.credentials")

CREDENTIALS_MASK_KEYS = {
    "longbridge_app_key",
    "longbridge_app_secret",
    "longbridge_access_token",
    "sct_key",
    "encrypted_app_key",
    "encrypted_app_secret",
    "encrypted_access_token",
}


def _mask_channel(channel: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": channel.get("type"),
        "severity_floor": channel.get("severity_floor"),
    }
    if channel.get("type") == "webhook":
        out["url"] = "***"
    return out


def _mask_credentials_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in CREDENTIALS_MASK_KEYS:
            out[key] = "***"
        elif key == "notification_channels":
            out[key] = [_mask_channel(channel) for channel in (value or [])]
        else:
            out[key] = value
    return out


@router.get("/credentials", response_model=CredentialResponse, dependencies=[Depends(require_api_key())])
def get_credentials(db: Session = Depends(get_db)) -> CredentialResponse:
    svc = CredentialsService(db)
    config = svc.get_config()
    return CredentialResponse.model_validate(svc.to_response(config))


@router.put("/credentials", response_model=CredentialResponse, dependencies=[Depends(require_api_key())])
def update_credentials(
    request: Request,
    payload: CredentialConfigSchema,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> CredentialResponse:
    actor_hash, source_ip = extract_actor(request)
    result = "SUCCESS"
    masked: dict[str, Any] = {}
    try:
        svc = CredentialsService(db)
        data = payload.model_dump(exclude_unset=True)
        masked = _mask_credentials_payload(data)
        current = svc.get_config()
        previous = {
            field: getattr(current, field)
            for field in (
                "longbridge_app_key",
                "longbridge_app_secret",
                "longbridge_access_token",
                "sct_key",
                "notification_channels",
            )
        }
        config = svc.update_config(data)
        broker_identity_change = any(
            field in data and data[field] is not None
            for field in (
                "longbridge_app_key",
                "longbridge_app_secret",
                "longbridge_access_token",
            )
        )
        try:
            get_runner().reload_credentials(
                broker_identity_change=broker_identity_change
            )
        except Exception as exc:
            logger.exception("credential reload failed after save; rolling back")
            for field, value in previous.items():
                setattr(config, field, value)
            db.add(config)
            db.commit()
            status_code = (
                409 if isinstance(exc, CredentialSwitchBlockedError) else 503
            )
            raise HTTPException(
                status_code=status_code,
                detail="credentials were not changed because live verification failed",
            ) from exc
        response = svc.to_response(config)
        response["reload_warning"] = None
        return CredentialResponse.model_validate(response)
    except HTTPException as exc:
        result = "FAILED"
        # Do not echo raw user input (e.g. webhook URLs that may carry tokens)
        # into the audit log. Persist a stable error code instead.
        masked = {"error": "invalid_request"}
        raise
    except Exception as exc:
        result = "FAILED"
        # Truncate to avoid unbounded user-controlled text landing in audit_logs.
        masked = {"error": type(exc).__name__, "detail": str(exc)[:256]}
        logger.exception("unexpected credential update failure")
        raise HTTPException(status_code=500, detail="credential update failed") from exc
    finally:
        audit.record(
            "CREDENTIALS_UPDATE",
            severity="INFO",
            actor_hash=actor_hash,
            source_ip=source_ip,
            request_summary={"changed": masked},
            result=result,
        )


@router.post("/credentials/test", dependencies=[Depends(require_api_key())])
def test_credentials(
    request: Request,
    audit: AuditLogger = Depends(get_audit_logger),
) -> dict[str, Any]:
    """Send a smoke-test notification to verify the saved channels are reachable.

    Resolves the live ``MultiChannelNotifier`` from the runner and dispatches
    a single test message with ``severity=INFO``. The endpoint reports
    per-channel success so the UI can pinpoint which channel is failing.
    No state is mutated; the broadcast is purely diagnostic.
    """
    actor_hash, source_ip = extract_actor(request)
    runner = get_runner()
    notifier = getattr(runner, "notifier", None)
    if notifier is None:
        audit.record(
            "CREDENTIALS_TEST",
            severity="WARNING",
            actor_hash=actor_hash,
            source_ip=source_ip,
            request_summary={"error": "no notifier configured"},
            result="FAILED",
        )
        return {"ok": False, "error": "notifier not configured"}

    title = "Auto Trade: 凭证测试"
    content = "这是一条凭证连通性测试消息。如果您看到它，说明通知渠道配置正确。"
    try:
        ok = notifier.send(title, content, severity="INFO")
    except Exception as exc:  # noqa: BLE001
        ok = False
        logger.error("notification send failed", exc_info=exc)
        audit.record(
            "CREDENTIALS_TEST",
            severity="WARNING",
            actor_hash=actor_hash,
            source_ip=source_ip,
            request_summary={"error": str(exc)[:200]},
            result="FAILED",
        )
        return {"ok": False, "error": "notification send failed — see server logs for details"}

    audit.record(
        "CREDENTIALS_TEST",
        severity="INFO",
        actor_hash=actor_hash,
        source_ip=source_ip,
        request_summary={"delivered": ok},
        result="SUCCESS" if ok else "FAILED",
    )
    return {"ok": ok, "error": None if ok else "all channels failed"}


class NotificationChannelTestSchema(BaseModel):
    type: Literal["serverchan", "webhook"]
    severity_floor: Literal["INFO", "WARNING", "CRITICAL"] = "INFO"
    url: Optional[str] = Field(default=None, max_length=4096)
    template: Optional[str] = Field(default=None, max_length=8192)


@router.post("/credentials/notification-channels/test", dependencies=[Depends(require_api_key())])
def test_notification_channel(
    request: Request,
    payload: NotificationChannelTestSchema,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> dict[str, Any]:
    """Send a test notification through a single notification channel.

    Uses the saved Server酱 key for ``serverchan`` channels and the provided
    URL/template for ``webhook`` channels. The channel config does not need
    to be saved first, so users can verify a new channel before committing it.
    """
    actor_hash, source_ip = extract_actor(request)
    svc = CredentialsService(db)
    config = svc.get_config()
    from app.core.credential_crypto import decrypt_secret
    from app.core.notifiers.multi_channel import MultiChannelNotifier
    from app.core.notifiers.serverchan import ServerChanNotifier
    from app.core.notifiers.webhook import WebhookNotifier

    built: list[tuple[Any, str]] = []
    if payload.type == "serverchan":
        built.append((ServerChanNotifier(decrypt_secret(config.sct_key or "")), payload.severity_floor))
    elif payload.type == "webhook" and payload.url:
        built.append((WebhookNotifier(payload.url, template=payload.template), payload.severity_floor))

    if not built:
        return {"ok": False, "error": "invalid channel configuration"}

    notifier = MultiChannelNotifier(built)
    try:
        ok = notifier.send("Auto Trade: 渠道测试", "这是一条单渠道连通性测试消息。", severity="INFO")
    except Exception as exc:  # noqa: BLE001
        ok = False
        logger.error("single channel test failed", exc_info=exc)
    finally:
        notifier.close()

    audit.record(
        "CREDENTIALS_CHANNEL_TEST",
        severity="INFO",
        actor_hash=actor_hash,
        source_ip=source_ip,
        request_summary={"type": payload.type, "delivered": ok},
        result="SUCCESS" if ok else "FAILED",
    )
    return {"ok": ok, "error": None if ok else "channel test failed"}
