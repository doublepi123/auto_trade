from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.credential_crypto import decrypt_secret, encrypt_secret, is_encrypted
from app.core.url_safety import validate_webhook_url
from app.database import DEFAULT_NOTIFICATION_CHANNELS_JSON
from app.models import CredentialConfig

#: Process-wide guard so that the legacy-plaintext re-encryption pass
#: runs at most once per process. ``get_config`` is called on every
#: high-frequency read path (status, control, WS), and the migration is
#: idempotent — once the row has been re-encrypted a single time, every
#: subsequent invocation can skip the per-field ``is_encrypted`` check
#: and the associated write amplification.
_LEGACY_ENCRYPTION_DONE: bool = False

_VALID_CHANNEL_TYPES = {"serverchan", "webhook"}
_VALID_SEVERITY_FLOORS = {"INFO", "WARNING", "CRITICAL"}


@dataclass(frozen=True)
class PlainCredentials:
    longbridge_app_key: str = ""
    longbridge_app_secret: str = ""
    longbridge_access_token: str = ""
    sct_key: str = ""
    notification_channels: str = DEFAULT_NOTIFICATION_CHANNELS_JSON


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
            notification_channels=config.notification_channels or DEFAULT_NOTIFICATION_CHANNELS_JSON,
        )

    @staticmethod
    def _validate_channel(ch: Any, idx: int) -> None:
        """Validate a single notification channel entry. Raises ValueError on failure."""
        if not isinstance(ch, dict):
            raise ValueError(f"notification_channels[{idx}] must be a dict, got {type(ch).__name__}")
        ch_type = ch.get("type")
        if ch_type not in _VALID_CHANNEL_TYPES:
            raise ValueError(
                f"notification_channels[{idx}].type must be one of {_VALID_CHANNEL_TYPES}, got {ch_type!r}"
            )
        severity_floor = ch.get("severity_floor")
        if severity_floor not in _VALID_SEVERITY_FLOORS:
            raise ValueError(
                f"notification_channels[{idx}].severity_floor must be one of {_VALID_SEVERITY_FLOORS}, got {severity_floor!r}"
            )
        if ch_type == "webhook":
            url = ch.get("url")
            if not url or not isinstance(url, str) or not url.strip():
                raise ValueError(
                    f"notification_channels[{idx}].url is required and must be non-empty when type is 'webhook'"
                )
            validate_webhook_url(url)

    def _parse_notification_channels(self, config: CredentialConfig) -> list[dict[str, Any]]:
        try:
            raw = json.loads(config.notification_channels or DEFAULT_NOTIFICATION_CHANNELS_JSON)
        except json.JSONDecodeError:
            raw = json.loads(DEFAULT_NOTIFICATION_CHANNELS_JSON)
        if not isinstance(raw, list):
            return json.loads(DEFAULT_NOTIFICATION_CHANNELS_JSON)
        return raw

    def to_response(self, config: CredentialConfig) -> dict[str, Any]:
        return {
            "id": config.id,
            "longbridge_app_key": "",
            "longbridge_app_secret": "",
            "longbridge_access_token": "",
            "sct_key": "",
            "notification_channels": self._parse_notification_channels(config),
            "has_longbridge_app_key": bool(config.longbridge_app_key),
            "has_longbridge_app_secret": bool(config.longbridge_app_secret),
            "has_longbridge_access_token": bool(config.longbridge_access_token),
            "has_sct_key": bool(config.sct_key),
            "updated_at": config.updated_at,
        }

    def update_config(self, data: dict[str, Any]) -> CredentialConfig:
        """Apply a partial update to the credential row.

        Field update semantics for credential fields
        (``longbridge_app_key``, ``longbridge_app_secret``,
        ``longbridge_access_token``, ``sct_key``):

        * **key missing from ``data``** → field is left unchanged.
        * **key present with value ``None``** → field is left unchanged
          (``None`` means "no opinion from the caller"). Callers that want
          to *clear* a credential must explicitly pass an empty string.
        * **key present with value ``""``** → field is cleared (stored as
          an empty string, treated as "no credential configured").
        * **key present with any other string** → stored encrypted.

        This is the contract documented in :class:`Credentials`; new
        clients should follow ``None = no change, '' = clear`` rather
        than relying on Pydantic's default ``None`` skip behaviour.
        """
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
                    setattr(config, field, "")
                else:
                    setattr(config, field, encrypt_secret(value))

        if "notification_channels" in data and data["notification_channels"] is not None:
            channels = data["notification_channels"]
            if isinstance(channels, list):
                for idx, ch in enumerate(channels):
                    self._validate_channel(ch, idx)
                def _to_dict(ch: Any) -> dict[str, Any]:
                    if isinstance(ch, dict):
                        return ch
                    if hasattr(ch, "model_dump"):
                        return ch.model_dump()
                    raise ValueError(f"Unsupported notification channel type: {type(ch).__name__}")
                config.notification_channels = json.dumps(
                    [_to_dict(ch) for ch in channels],
                    ensure_ascii=False,
                )
            else:
                if isinstance(channels, str):
                    try:
                        parsed = json.loads(channels)
                    except json.JSONDecodeError:
                        raise ValueError("notification_channels string is not valid JSON")
                    if not isinstance(parsed, list):
                        raise ValueError("notification_channels must be a list, got JSON type: %s" % type(parsed).__name__)
                    for idx, ch in enumerate(parsed):
                        self._validate_channel(ch, idx)
                    config.notification_channels = json.dumps(parsed, ensure_ascii=False)
                else:
                    raise ValueError("notification_channels must be a list, got type: %s" % type(channels).__name__)

        config.updated_at = datetime.now(timezone.utc)
        self.db.add(config)
        self.db.commit()
        self.db.refresh(config)
        return config

    def _encrypt_plaintext_fields(self, config: CredentialConfig) -> None:
        global _LEGACY_ENCRYPTION_DONE
        # Once the migration has run for the process, all rows must already
        # hold ciphertext (the migration is idempotent and any subsequent
        # write goes through ``update_config`` → ``encrypt_secret``). Skipping
        # the per-field check avoids a write-amplification loop on every
        # high-frequency ``get_config`` call.
        if _LEGACY_ENCRYPTION_DONE:
            return
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
        _LEGACY_ENCRYPTION_DONE = True
