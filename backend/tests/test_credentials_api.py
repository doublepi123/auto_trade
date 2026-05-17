import os

os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_credentials_api.db"


from fastapi.testclient import TestClient

from app.api import credentials as credentials_api
from app.core.credential_crypto import decrypt_secret, is_encrypted
from app.database import SessionLocal, engine as db_engine
from app.models import Base, CredentialConfig, StrategyConfig
from app.main import app


Base.metadata.create_all(bind=db_engine)

client = TestClient(app)


def _clean_credentials() -> None:
    db = SessionLocal()
    db.query(CredentialConfig).delete()
    db.commit()
    db.close()


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
