from __future__ import annotations

from types import SimpleNamespace

from app.cli import llm_storage_maintenance as cli


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
