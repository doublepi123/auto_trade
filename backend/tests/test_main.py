import pytest

from app import main as main_module


@pytest.mark.asyncio
async def test_lifespan_logs_runner_start_failure_without_crashing(monkeypatch) -> None:
    class FailingRunner:
        def start(self) -> bool:
            return False

        def stop(self) -> None:
            pass

    monkeypatch.setattr(main_module, "init_db", lambda: None)
    monkeypatch.setattr(main_module, "get_runner", lambda: FailingRunner())

    async with main_module.lifespan(main_module.app):
        pass
