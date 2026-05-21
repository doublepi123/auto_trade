from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import CredentialConfigSchema, CredentialResponse
from app.runner import get_runner
from app.services.credentials_service import CredentialsService

router = APIRouter(prefix="/api", tags=["credentials"])
logger = logging.getLogger("auto_trade.credentials")
RELOAD_WARNING = "Credentials saved but live reload failed. A restart may be required for changes to take effect."


def _reload_runner_credentials() -> None:
    try:
        get_runner().reload_credentials()
    except Exception:
        logger.exception("credential reload failed after save")


def schedule_runner_credential_reload() -> None:
    thread = threading.Thread(target=_reload_runner_credentials, name="credential-reload", daemon=True)
    thread.start()


@router.get("/credentials", response_model=CredentialResponse)
def get_credentials(db: Session = Depends(get_db)) -> CredentialResponse:
    svc = CredentialsService(db)
    config = svc.get_config()
    return CredentialResponse.model_validate(svc.to_response(config))


@router.put("/credentials", response_model=CredentialResponse)
def update_credentials(payload: CredentialConfigSchema, db: Session = Depends(get_db)) -> CredentialResponse:
    svc = CredentialsService(db)
    data = payload.model_dump(exclude_unset=True)
    config = svc.update_config(data)
    reload_warning = None
    try:
        schedule_runner_credential_reload()
    except Exception:
        logger.exception("credential reload scheduling failed after save")
        reload_warning = RELOAD_WARNING
    response = svc.to_response(config)
    response["reload_warning"] = reload_warning
    return CredentialResponse.model_validate(response)
