import asyncio

import pytest

from app import main as main_module


@pytest.mark.asyncio
async def test_lifespan_logs_runner_start_failure_without_crashing(monkeypatch) -> None:
    class FailingRunner:
        def start(self, *, loop=None) -> bool:
            return False

        def stop(self) -> None:
            pass

    monkeypatch.setattr(main_module, "init_db", lambda: None)
    monkeypatch.setattr(main_module, "get_runner", lambda: FailingRunner())

    async with main_module.lifespan(main_module.app):
        pass


@pytest.mark.asyncio
async def test_lifespan_passes_application_loop_to_runner(monkeypatch) -> None:
    started_with = []

    class RecordingRunner:
        def start(self, *, loop=None) -> bool:
            started_with.append(loop)
            return True

        def stop(self) -> None:
            pass

    monkeypatch.setattr(main_module, "init_db", lambda: None)
    monkeypatch.setattr(main_module, "get_runner", lambda: RecordingRunner())

    current_loop = asyncio.get_running_loop()
    async with main_module.lifespan(main_module.app):
        pass

    assert started_with == [current_loop]
