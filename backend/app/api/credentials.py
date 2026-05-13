from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import CredentialConfigSchema, CredentialResponse
from app.runner import get_runner
from app.services.credentials_service import CredentialsService
from app.api.auth import require_api_key

router = APIRouter(prefix="/api", tags=["credentials"])
logger = logging.getLogger("auto_trade.credentials")


@router.get("/credentials", response_model=CredentialResponse, dependencies=[Depends(require_api_key())])
def get_credentials(db: Session = Depends(get_db)) -> CredentialResponse:
    svc = CredentialsService(db)
    config = svc.get_config()
    return CredentialResponse.model_validate(svc.to_response(config))


@router.put("/credentials", response_model=CredentialResponse, dependencies=[Depends(require_api_key())])
def update_credentials(payload: CredentialConfigSchema, db: Session = Depends(get_db)) -> CredentialResponse:
    svc = CredentialsService(db)
    data = payload.model_dump(exclude_unset=True)
    config = svc.update_config(data)
    try:
        get_runner().reload_credentials()
    except Exception:
        logger.exception("credential reload failed after save")
    return CredentialResponse.model_validate(svc.to_response(config))
