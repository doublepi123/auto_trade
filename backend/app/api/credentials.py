from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import extract_actor, get_audit_logger
from app.core.audit import AuditLogger
from app.database import get_db
from app.schemas import CredentialConfigSchema, CredentialResponse
from app.runner import get_runner
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


@router.get("/credentials", response_model=CredentialResponse)
def get_credentials(db: Session = Depends(get_db)) -> CredentialResponse:
    svc = CredentialsService(db)
    config = svc.get_config()
    return CredentialResponse.model_validate(svc.to_response(config))


@router.put("/credentials", response_model=CredentialResponse)
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
        config = svc.update_config(data)
        reload_warning = None
        try:
            get_runner().reload_credentials()
        except Exception:
            logger.exception("credential reload failed after save")
            reload_warning = (
                "Credentials saved but live reload failed. "
                "A restart may be required for changes to take effect."
            )
        response = svc.to_response(config)
        response["reload_warning"] = reload_warning
        return CredentialResponse.model_validate(response)
    except HTTPException as exc:
        result = "FAILED"
        masked = {"detail": str(exc.detail)}
        raise
    except Exception as exc:
        result = "FAILED"
        masked = {"detail": str(exc)}
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
