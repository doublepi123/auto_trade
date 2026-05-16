from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.credential_crypto import decrypt_secret, encrypt_secret, is_encrypted
from app.models import CredentialConfig


@dataclass(frozen=True)
class PlainCredentials:
    longbridge_app_key: str = ""
    longbridge_app_secret: str = ""
    longbridge_access_token: str = ""
    sct_key: str = ""


class CredentialsService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_config(self) -> CredentialConfig:
        config = self.db.query(CredentialConfig).order_by(CredentialConfig.id.desc()).first()
        if config is None:
            config = CredentialConfig()
            self.db.add(config)
            self.db.commit()
            self.db.refresh(config)
        self._encrypt_plaintext_fields(config)
        return config

    def get_plain_credentials(self) -> PlainCredentials:
        config = self.get_config()
        return PlainCredentials(
            longbridge_app_key=decrypt_secret(config.longbridge_app_key),
            longbridge_app_secret=decrypt_secret(config.longbridge_app_secret),
            longbridge_access_token=decrypt_secret(config.longbridge_access_token),
            sct_key=decrypt_secret(config.sct_key),
        )

    def to_response(self, config: CredentialConfig) -> dict:
        return {
            "id": config.id,
            "longbridge_app_key": "",
            "longbridge_app_secret": "",
            "longbridge_access_token": "",
            "sct_key": "",
            "has_longbridge_app_key": bool(config.longbridge_app_key),
            "has_longbridge_app_secret": bool(config.longbridge_app_secret),
            "has_longbridge_access_token": bool(config.longbridge_access_token),
            "has_sct_key": bool(config.sct_key),
            "updated_at": config.updated_at,
        }

    def update_config(self, data: dict) -> CredentialConfig:
        config = self.db.query(CredentialConfig).order_by(CredentialConfig.id.desc()).first()
        if config is None:
            config = CredentialConfig()

        updatable_fields = [
            "longbridge_app_key",
            "longbridge_app_secret",
            "longbridge_access_token",
            "sct_key",
        ]
        for field in updatable_fields:
            if field in data and data[field] is not None:
                value = data[field]
                if value == "":
                    continue
                setattr(config, field, encrypt_secret(value) if value else "")

        config.updated_at = datetime.now(timezone.utc)
        self.db.add(config)
        self.db.commit()
        self.db.refresh(config)
        return config

    def _encrypt_plaintext_fields(self, config: CredentialConfig) -> None:
        changed = False
        for field in ("longbridge_app_key", "longbridge_app_secret", "longbridge_access_token", "sct_key"):
            value = getattr(config, field)
            if value and not is_encrypted(value):
                setattr(config, field, encrypt_secret(value))
                changed = True
        if changed:
            self.db.add(config)
            self.db.commit()
            self.db.refresh(config)
