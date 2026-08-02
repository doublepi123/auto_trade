from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.cli import llm_storage_maintenance as cli


def test_cli_and_runtime_share_storage_maintenance_lease_key() -> None:
    from app import main as main_module

    assert cli._STORAGE_MAINTENANCE_LEASE_KEY == (
        main_module._LLM_STORAGE_MAINTENANCE_LEASE_KEY
    )


@pytest.fixture(autouse=True)
def _successful_storage_lease(monkeypatch) -> None:
    handle = object()

    class FakeGuard:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> bool:
            return False

        @staticmethod
        def checkpoint() -> None:
            return None

        @staticmethod
        def fence_in_transaction(_session) -> None:
            return None

    class FakeLeaseService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        @staticmethod
        def try_acquire(lease_key: str):
            assert lease_key == cli._STORAGE_MAINTENANCE_LEASE_KEY
            return handle

        @staticmethod
        def keepalive(acquired, **_kwargs):
            assert acquired is handle
            return FakeGuard()

    monkeypatch.setattr(cli, "DurableJobLeaseService", FakeLeaseService)


def test_vacuum_returns_nonzero_when_wal_checkpoint_is_busy(
    monkeypatch,
    capsys,
) -> None:
    commands: list[str] = []

    class FakeParser:
        @staticmethod
        def parse_args():
            return SimpleNamespace(
                retention_days=90,
                no_action_retention_days=14,
                context_max_bytes=2048,
                batch_size=25,
                vacuum=True,
                confirm_service_stopped=True,
            )

        @staticmethod
        def error(message: str) -> None:
            raise AssertionError(message)

    class FakeSession:
        @staticmethod
        def close() -> None:
            return None

    class FakeService:
        def __init__(self, _db) -> None:
            pass

        @staticmethod
        def prune_expired(**_kwargs):
            return SimpleNamespace(deleted=0, batches=0)

        @staticmethod
        def compact_oversized_contexts(**_kwargs):
            return SimpleNamespace(compacted=0, batches=0)

    class FakeResult:
        @staticmethod
        def one() -> tuple[int, int, int]:
            return (1, 12, 3)

    class FakeConnection:
        def execution_options(self, **_kwargs):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        @staticmethod
        def exec_driver_sql(statement: str):
            commands.append(statement)
            return FakeResult()

    class FakeEngine:
        dialect = SimpleNamespace(name="sqlite")

        @staticmethod
        def connect() -> FakeConnection:
            return FakeConnection()

    monkeypatch.setattr(cli, "_parser", lambda: FakeParser())
    monkeypatch.setattr(cli, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(cli, "LLMInteractionService", FakeService)
    monkeypatch.setattr(cli, "engine", FakeEngine())

    exit_code = cli.main()

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "WAL checkpoint is busy" in captured.err
    assert "VACUUM was not run" in captured.err
    assert commands == ["PRAGMA wal_checkpoint(TRUNCATE)"]


def test_cli_returns_three_when_storage_lease_is_busy(
    monkeypatch,
    capsys,
) -> None:
    class FakeParser:
        @staticmethod
        def parse_args():
            return SimpleNamespace(
                retention_days=90,
                no_action_retention_days=14,
                context_max_bytes=2048,
                batch_size=25,
                vacuum=False,
                confirm_service_stopped=False,
            )

        @staticmethod
        def error(message: str) -> None:
            raise AssertionError(message)

    class BusyLeaseService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        @staticmethod
        def try_acquire(lease_key: str):
            assert lease_key == cli._STORAGE_MAINTENANCE_LEASE_KEY
            return None

    monkeypatch.setattr(cli, "_parser", lambda: FakeParser())
    monkeypatch.setattr(cli, "DurableJobLeaseService", BusyLeaseService)
    monkeypatch.setattr(
        cli,
        "SessionLocal",
        lambda: pytest.fail("busy CLI opened a business session"),
    )

    assert cli.main() == 3
    assert "already running" in capsys.readouterr().err
