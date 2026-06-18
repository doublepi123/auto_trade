from fastapi.testclient import TestClient

import json

import pytest

from app.api import credentials as credentials_api
from app.core.credential_crypto import decrypt_secret, is_encrypted
from app.database import SessionLocal
from app.models import AuditLog, CredentialConfig, StrategyConfig
from app.main import app
from app import database

database.init_db()

client = TestClient(app)


def _clean_credentials() -> None:
    with SessionLocal() as db:
        db.query(CredentialConfig).delete()
        db.commit()


def _clean_audit_logs() -> None:
    with SessionLocal() as db:
        db.query(AuditLog).delete()
        db.commit()


class TestCredentialsAPI:
    def test_init_db_migrates_legacy_sct_key(self) -> None:
        _clean_credentials()
        db = SessionLocal()
        try:
            db.query(StrategyConfig).delete()
            db.commit()
            db.add(StrategyConfig(sct_key="legacy-sct"))
            db.commit()
        finally:
            db.close()

        from app.database import init_db

        init_db()

        db = SessionLocal()
        try:
            credential = db.query(CredentialConfig).order_by(CredentialConfig.id.desc()).first()
            assert credential is not None
            assert is_encrypted(credential.sct_key)
            assert decrypt_secret(credential.sct_key) == "legacy-sct"
        finally:
            db.close()

    def test_init_db_preserves_existing_sct_key(self) -> None:
        _clean_credentials()
        db = SessionLocal()
        try:
            db.query(StrategyConfig).delete()
            db.commit()
            db.add(StrategyConfig(sct_key="legacy-sct"))
            db.commit()
            db.add(CredentialConfig(sct_key="current-sct"))
            db.commit()
        finally:
            db.close()

        from app.database import init_db

        init_db()

        db = SessionLocal()
        try:
            credential = db.query(CredentialConfig).order_by(CredentialConfig.id.desc()).first()
            assert credential is not None
            assert decrypt_secret(credential.sct_key) == "current-sct"
        finally:
            db.close()

    def test_get_credentials_default(self) -> None:
        _clean_credentials()
        resp = client.get("/api/credentials")
        assert resp.status_code == 200
        data = resp.json()
        assert data["longbridge_app_key"] == ""
        assert data["sct_key"] == ""
        assert data["has_longbridge_app_key"] is False
        assert data["has_sct_key"] is False

    def test_update_credentials(self) -> None:
        _clean_credentials()
        resp = client.put("/api/credentials", json={
            "longbridge_app_key": "key",
            "longbridge_app_secret": "secret",
            "longbridge_access_token": "token",
            "sct_key": "sct",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["longbridge_app_key"] == ""
        assert data["longbridge_app_secret"] == ""
        assert data["longbridge_access_token"] == ""
        assert data["sct_key"] == ""
        assert data["has_longbridge_app_key"] is True
        assert data["has_longbridge_app_secret"] is True
        assert data["has_longbridge_access_token"] is True
        assert data["has_sct_key"] is True

        db = SessionLocal()
        try:
            credential = db.query(CredentialConfig).order_by(CredentialConfig.id.desc()).first()
            assert credential is not None
            assert credential.longbridge_app_secret != "secret"
            assert is_encrypted(credential.longbridge_app_secret)
            assert decrypt_secret(credential.longbridge_app_secret) == "secret"
        finally:
            db.close()

    def test_update_credentials_accepts_long_access_token(self) -> None:
        _clean_credentials()
        long_token = "t" * 1200

        resp = client.put("/api/credentials", json={
            "longbridge_app_key": "key",
            "longbridge_app_secret": "secret",
            "longbridge_access_token": long_token,
            "sct_key": "sct",
        })

        assert resp.status_code == 200
        db = SessionLocal()
        try:
            credential = db.query(CredentialConfig).order_by(CredentialConfig.id.desc()).first()
            assert credential is not None
            assert decrypt_secret(credential.longbridge_access_token) == long_token
        finally:
            db.close()

    def test_update_credentials_clears_when_blank(self) -> None:
        _clean_credentials()
        initial = client.put("/api/credentials", json={
            "longbridge_app_key": "key",
            "longbridge_app_secret": "secret",
            "longbridge_access_token": "token",
            "sct_key": "sct",
        })
        assert initial.status_code == 200

        resp = client.put("/api/credentials", json={
            "longbridge_app_key": "",
            "longbridge_app_secret": "",
            "longbridge_access_token": "",
            "sct_key": "",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["has_longbridge_app_key"] is False
        assert data["has_longbridge_app_secret"] is False
        assert data["has_longbridge_access_token"] is False
        assert data["has_sct_key"] is False

        db = SessionLocal()
        try:
            credential = db.query(CredentialConfig).order_by(CredentialConfig.id.desc()).first()
            assert credential is not None
            assert decrypt_secret(credential.longbridge_app_key) == ""
            assert decrypt_secret(credential.longbridge_app_secret) == ""
            assert decrypt_secret(credential.longbridge_access_token) == ""
            assert decrypt_secret(credential.sct_key) == ""
        finally:
            db.close()

    def test_update_credentials_ignores_reload_failure(self, monkeypatch) -> None:
        class FailingRunner:
            def reload_credentials(self) -> None:
                raise RuntimeError("reload failed")

        monkeypatch.setattr(credentials_api, "get_runner", lambda: FailingRunner())
        _clean_credentials()

        resp = client.put("/api/credentials", json={
            "longbridge_app_key": "key",
            "longbridge_app_secret": "secret",
            "longbridge_access_token": "token",
            "sct_key": "sct",
        })

        assert resp.status_code == 200


def test_update_persists_notification_channels() -> None:
    _clean_credentials()
    payload = {
        "notification_channels": [
            {"type": "serverchan", "severity_floor": "INFO"},
            {
                "type": "webhook",
                "url": "https://93.184.216.34/hook",
                "severity_floor": "WARNING",
            },
        ]
    }
    resp = client.put("/api/credentials", json=payload)
    assert resp.status_code == 200
    body = client.get("/api/credentials").json()
    assert body["notification_channels"][1]["url"] == "https://93.184.216.34/hook"


def test_credentials_update_audits_with_masked_payload() -> None:
    _clean_audit_logs()
    _clean_credentials()
    import json

    payload = {
        "longbridge_app_key": "newkey",
        "longbridge_app_secret": "newsecret",
        "longbridge_access_token": "newtoken",
        "sct_key": "newsct",
    }
    resp = client.put("/api/credentials", json=payload)
    assert resp.status_code == 200
    db = SessionLocal()
    try:
        row = (
            db.query(AuditLog)
            .filter_by(action="CREDENTIALS_UPDATE")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row is not None
        summary = json.loads(row.request_summary)
        changed = summary["changed"]
        for key in (
            "longbridge_app_key",
            "longbridge_app_secret",
            "longbridge_access_token",
            "sct_key",
        ):
            assert changed.get(key) == "***"
    finally:
        db.close()


def test_test_credentials_sanitizes_exception(monkeypatch):
    """Issue I2-2: The /api/credentials/test endpoint must not leak
    the raw exception message (e.g. webhook URL / token) to the API consumer.
    """
    _clean_credentials()
    from app.api import credentials as credentials_api

    class FailingNotifier:
        def send(self, title, content, severity="INFO"):
            raise RuntimeError("secret-token-abc123 leaked in exception")

    class FakeRunner:
        notifier = FailingNotifier()

    monkeypatch.setattr(credentials_api, "get_runner", lambda: FakeRunner())

    resp = client.post("/api/credentials/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "secret-token-abc123" not in data["error"]
    assert data["error"] == "notification send failed — see server logs for details"


def test_test_notification_channel_serverchan_ok(monkeypatch):
    """The per-channel test endpoint should exercise a single channel without
    requiring the channel to be saved first.
    """
    _clean_credentials()
    client.put("/api/credentials", json={"sct_key": "testsctkey"})
    from app.core.notifiers import serverchan as sc

    def fake_send(self, title, content, severity="INFO"):
        return True

    monkeypatch.setattr(sc.ServerChanNotifier, "send", fake_send)

    resp = client.post(
        "/api/credentials/notification-channels/test",
        json={"type": "serverchan", "severity_floor": "INFO"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["error"] is None


def test_test_notification_channel_webhook_ok(monkeypatch):
    _clean_credentials()
    from app.core.notifiers import webhook as wh

    def fake_send(self, title, content, severity="INFO"):
        return True

    monkeypatch.setattr(wh.WebhookNotifier, "send", fake_send)

    resp = client.post(
        "/api/credentials/notification-channels/test",
        json={"type": "webhook", "severity_floor": "INFO", "url": "https://93.184.216.34/hook"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True


def test_test_notification_channel_invalid_config():
    _clean_credentials()
    resp = client.post(
        "/api/credentials/notification-channels/test",
        json={"type": "webhook", "severity_floor": "INFO", "url": ""},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["error"] == "invalid channel configuration"


def test_decrypt_tampered_ciphertext_raises_credential_integrity_error(tmp_path, monkeypatch):
    """Tampered ciphertext must surface as CredentialIntegrityError, not raw InvalidTag.

    AESGCM raises ``cryptography.exceptions.InvalidTag`` on any tamper. Before
    the fix, that exception leaked to callers; the API layer would render it
    as a 500 with a cryptography-internal stack trace. After the fix, the
    caller observes a single named exception that can be mapped to a 4xx/5xx
    response with a sanitized message.
    """
    from app.core import credential_crypto
    from app.core.credential_crypto import CredentialIntegrityError, encrypt_secret

    monkeypatch.setenv("AUTO_TRADE_CREDENTIAL_KEY_PATH", str(tmp_path / "k.pem"))

    ciphertext = encrypt_secret("super-secret-value")
    assert credential_crypto.is_encrypted(ciphertext)

    # Replace the entire encrypted blob with a valid-shape but tampered
    # payload. We rebuild a known-good encryption structure, then mutate
    # the ciphertext ("c") field so AES-GCM's tag check fails on decrypt.
    body = ciphertext[len("enc:"):]
    decoded = credential_crypto._decode(body)
    payload = json.loads(decoded.decode("utf-8"))
    # Mutate a single byte of the ciphertext — base64 round-trip preserves
    # the JSON shape but invalidates the AES-GCM authentication tag.
    raw_ciphertext = credential_crypto._decode(payload["c"])
    tampered = bytes([raw_ciphertext[0] ^ 0x01]) + raw_ciphertext[1:]
    payload["c"] = credential_crypto._encode(tampered)
    tampered_body = credential_crypto._encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )

    with pytest.raises(CredentialIntegrityError):
        decrypt_secret("enc:" + tampered_body)


def test_update_notification_channels_str_rejects_malicious_webhook() -> None:
    """G2-1: str branch must validate each channel (incl. webhook URL safety)."""
    _clean_credentials()
    from app.services.credentials_service import CredentialsService

    db = SessionLocal()
    try:
        svc = CredentialsService(db)
        malicious = json.dumps([
            {"type": "webhook", "url": "http://127.0.0.1:8080/hack", "severity_floor": "INFO"},
        ])
        with pytest.raises(ValueError, match="webhook url must use https"):
            svc.update_config({"notification_channels": malicious})
    finally:
        db.close()


def test_update_notification_channels_str_rejects_invalid_type() -> None:
    """G2-1: str branch must validate channel type."""
    _clean_credentials()
    from app.services.credentials_service import CredentialsService

    db = SessionLocal()
    try:
        svc = CredentialsService(db)
        payload = json.dumps([
            {"type": "sms", "severity_floor": "INFO"},
        ])
        with pytest.raises(ValueError, match="notification_channels\\[0\\]\\.type"):
            svc.update_config({"notification_channels": payload})
    finally:
        db.close()


def test_update_notification_channels_str_rejects_invalid_severity_floor() -> None:
    """G2-1: str branch must validate severity_floor."""
    _clean_credentials()
    from app.services.credentials_service import CredentialsService

    db = SessionLocal()
    try:
        svc = CredentialsService(db)
        payload = json.dumps([
            {"type": "serverchan", "severity_floor": "DEBUG"},
        ])
        with pytest.raises(ValueError, match="notification_channels\\[0\\]\\.severity_floor"):
            svc.update_config({"notification_channels": payload})
    finally:
        db.close()


def test_update_notification_channels_str_valid() -> None:
    """G2-1: str branch accepts valid channels."""
    _clean_credentials()
    from app.services.credentials_service import CredentialsService

    db = SessionLocal()
    try:
        svc = CredentialsService(db)
        valid = json.dumps([
            {"type": "serverchan", "severity_floor": "INFO"},
        ])
        result = svc.update_config({"notification_channels": valid})
        assert result is not None
        channels = json.loads(result.notification_channels)
        assert channels[0]["type"] == "serverchan"
    finally:
        db.close()


def test_update_notification_channels_str_rejects_non_list_json() -> None:
    """G2-1: str branch rejects non-list JSON."""
    _clean_credentials()
    from app.services.credentials_service import CredentialsService

    db = SessionLocal()
    try:
        svc = CredentialsService(db)
        with pytest.raises(ValueError, match="must be a list"):
            svc.update_config({"notification_channels": json.dumps({"type": "serverchan"})})
    finally:
        db.close()


def test_update_notification_channels_str_rejects_invalid_json() -> None:
    """G2-1: str branch rejects invalid JSON."""
    _clean_credentials()
    from app.services.credentials_service import CredentialsService

    db = SessionLocal()
    try:
        svc = CredentialsService(db)
        with pytest.raises(ValueError, match="not valid JSON"):
            svc.update_config({"notification_channels": "not-json"})
    finally:
        db.close()
