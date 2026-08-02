from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


_ROOT = Path(__file__).resolve().parents[2]


def test_durable_job_lease_defaults(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AUTO_TRADE_JOB_LEASE_TTL_SECONDS", raising=False)
    monkeypatch.delenv(
        "AUTO_TRADE_JOB_LEASE_HEARTBEAT_SECONDS",
        raising=False,
    )
    monkeypatch.chdir(tmp_path)

    configured = Settings()

    assert configured.job_lease_ttl_seconds == 120
    assert configured.job_lease_heartbeat_seconds == 30


def test_durable_job_lease_settings_read_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTO_TRADE_JOB_LEASE_TTL_SECONDS", "180")
    monkeypatch.setenv("AUTO_TRADE_JOB_LEASE_HEARTBEAT_SECONDS", "45")

    configured = Settings()

    assert configured.job_lease_ttl_seconds == 180
    assert configured.job_lease_heartbeat_seconds == 45


@pytest.mark.parametrize(("ttl", "heartbeat"), (("30", "30"), ("30", "31")))
def test_durable_job_lease_heartbeat_must_be_shorter_than_ttl(
    monkeypatch: pytest.MonkeyPatch,
    ttl: str,
    heartbeat: str,
) -> None:
    monkeypatch.setenv("AUTO_TRADE_JOB_LEASE_TTL_SECONDS", ttl)
    monkeypatch.setenv(
        "AUTO_TRADE_JOB_LEASE_HEARTBEAT_SECONDS",
        heartbeat,
    )

    with pytest.raises(ValidationError, match="shorter than its TTL"):
        Settings()


def test_compose_and_env_example_expose_durable_job_lease_settings() -> None:
    expected = {
        "AUTO_TRADE_JOB_LEASE_TTL_SECONDS": "120",
        "AUTO_TRADE_JOB_LEASE_HEARTBEAT_SECONDS": "30",
    }
    for filename in ("docker-compose.yaml", "docker-compose.dockerhub.yaml"):
        compose = (_ROOT / filename).read_text(encoding="utf-8")
        for name, default in expected.items():
            assert f"{name}=${{{name}:-{default}}}" in compose
    env_example = (_ROOT / ".env.example").read_text(encoding="utf-8")
    for name, default in expected.items():
        assert f"# {name}={default}" in env_example
