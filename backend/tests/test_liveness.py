from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.core import liveness as liveness_module
from app.core.liveness import LivenessWatchdog


_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clear_global_watchdog() -> Generator[None, None, None]:
    liveness_module.clear_liveness_watchdog()
    yield
    liveness_module.clear_liveness_watchdog()


class _FakeClock:
    def __init__(self) -> None:
        self._now = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _make_watchdog(
    clock: _FakeClock,
    *,
    stale_after: float = 50.0,
    hard_exit_after: float = 100.0,
    dump_enabled: bool = True,
    hard_exit_enabled: bool = True,
    exit_codes: list[int] | None = None,
) -> LivenessWatchdog:
    return LivenessWatchdog(
        stale_after_seconds=stale_after,
        hard_exit_after_seconds=hard_exit_after,
        beat_interval_seconds=0.05,
        dump_traceback_enabled=dump_enabled,
        hard_exit_enabled=hard_exit_enabled,
        monotonic=clock,
        exit_fn=(lambda code: exit_codes.append(code)) if exit_codes is not None else (lambda _code: None),
    )


def test_fresh_heartbeat_is_alive() -> None:
    clock = _FakeClock()
    watchdog = _make_watchdog(clock)
    assert watchdog.is_alive()
    clock.advance(60.0)
    assert not watchdog.is_alive()


def test_beat_resets_staleness() -> None:
    clock = _FakeClock()
    watchdog = _make_watchdog(clock)
    clock.advance(60.0)
    assert not watchdog.is_alive()
    watchdog.beat()
    assert watchdog.is_alive()


def test_invalid_thresholds_rejected() -> None:
    clock = _FakeClock()
    with pytest.raises(ValueError):
        LivenessWatchdog(
            stale_after_seconds=0.0,
            hard_exit_after_seconds=1.0,
            beat_interval_seconds=1.0,
            monotonic=clock,
        )
    with pytest.raises(ValueError):
        LivenessWatchdog(
            stale_after_seconds=10.0,
            hard_exit_after_seconds=5.0,
            beat_interval_seconds=1.0,
            monotonic=clock,
        )
    with pytest.raises(ValueError, match="shorter than stale_after_seconds"):
        LivenessWatchdog(
            stale_after_seconds=10.0,
            hard_exit_after_seconds=20.0,
            beat_interval_seconds=10.0,
            monotonic=clock,
        )


def test_watchdog_can_start_after_thread_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    watchdog = _make_watchdog(clock)
    original_start = threading.Thread.start

    def _fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(threading.Thread, "start", _fail_start)
    with pytest.raises(RuntimeError, match="thread unavailable"):
        watchdog.start()

    monkeypatch.setattr(threading.Thread, "start", original_start)
    watchdog.start()
    watchdog.stop()


def test_watchdog_dumps_then_hard_exits_on_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    dumps: list[bool] = []
    exit_codes: list[int] = []
    monkeypatch.setattr(
        liveness_module.faulthandler,
        "dump_traceback",
        lambda *, file=None, all_threads=True: dumps.append(all_threads),
    )
    watchdog = _make_watchdog(clock, exit_codes=exit_codes)
    watchdog.start()
    try:
        clock.advance(60.0)  # stale, below hard-exit threshold
        assert _wait_for(lambda: len(dumps) == 1)
        assert exit_codes == []
        clock.advance(60.0)  # beyond hard-exit threshold
        assert _wait_for(lambda: exit_codes == [1])
    finally:
        watchdog.stop()


def test_watchdog_respects_disabled_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    dumps: list[bool] = []
    exit_codes: list[int] = []
    monkeypatch.setattr(
        liveness_module.faulthandler,
        "dump_traceback",
        lambda *, file=None, all_threads=True: dumps.append(all_threads),
    )
    watchdog = _make_watchdog(
        clock,
        dump_enabled=False,
        hard_exit_enabled=False,
        exit_codes=exit_codes,
    )
    watchdog.start()
    try:
        clock.advance(500.0)
        time.sleep(0.5)
        assert dumps == []
        assert exit_codes == []
    finally:
        watchdog.stop()


def test_watchdog_does_not_wait_for_blocked_dump_before_hard_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    dump_started = threading.Event()
    release_dump = threading.Event()
    exit_codes: list[int] = []

    def _blocked_dump(*, file=None, all_threads=True) -> None:
        del file, all_threads
        dump_started.set()
        release_dump.wait()

    monkeypatch.setattr(
        liveness_module.faulthandler,
        "dump_traceback",
        _blocked_dump,
    )
    watchdog = _make_watchdog(
        clock,
        stale_after=1.0,
        hard_exit_after=2.0,
        exit_codes=exit_codes,
    )
    watchdog.start()
    try:
        clock.advance(1.5)
        assert dump_started.wait(1.0)
        clock.advance(1.0)
        assert _wait_for(lambda: exit_codes == [1], timeout=1.0)
    finally:
        release_dump.set()
        watchdog.stop()


def test_recovered_watchdog_dumps_again_on_next_freeze(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    dumps: list[bool] = []
    monkeypatch.setattr(
        liveness_module.faulthandler,
        "dump_traceback",
        lambda *, file=None, all_threads=True: dumps.append(all_threads),
    )
    watchdog = _make_watchdog(clock, hard_exit_enabled=False)
    watchdog.start()
    try:
        clock.advance(60.0)
        assert _wait_for(lambda: len(dumps) == 1)
        watchdog.beat()
        clock.advance(60.0)
        assert _wait_for(lambda: len(dumps) == 2)
    finally:
        watchdog.stop()


def test_global_watchdog_clear_does_not_remove_newer_instance() -> None:
    clock = _FakeClock()
    first = _make_watchdog(clock)
    second = _make_watchdog(clock)
    liveness_module.init_liveness_watchdog(first)
    liveness_module.init_liveness_watchdog(second)

    liveness_module.clear_liveness_watchdog(first)
    assert liveness_module.get_liveness_watchdog() is second

    liveness_module.clear_liveness_watchdog(second)
    assert liveness_module.get_liveness_watchdog() is None


@pytest.mark.parametrize(
    ("beat_interval", "stale_after", "hard_exit_after", "message"),
    (
        (10.0, 10.0, 20.0, "heartbeat interval must be shorter"),
        (5.0, 20.0, 20.0, "hard-exit threshold must exceed"),
        (5.0, 20.0, 10.0, "hard-exit threshold must exceed"),
    ),
)
def test_settings_reject_invalid_liveness_threshold_relationships(
    beat_interval: float,
    stale_after: float,
    hard_exit_after: float,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(
            liveness_beat_interval_seconds=beat_interval,
            liveness_stale_after_seconds=stale_after,
            liveness_hard_exit_after_seconds=hard_exit_after,
        )


def test_deploy_configs_expose_liveness_settings() -> None:
    expected = {
        "AUTO_TRADE_LIVENESS_ENABLED": "true",
        "AUTO_TRADE_LIVENESS_BEAT_INTERVAL_SECONDS": "5",
        "AUTO_TRADE_LIVENESS_STALE_AFTER_SECONDS": "120",
        "AUTO_TRADE_LIVENESS_DUMP_TRACEBACK_ENABLED": "true",
        "AUTO_TRADE_LIVENESS_HARD_EXIT_ENABLED": "true",
        "AUTO_TRADE_LIVENESS_HARD_EXIT_AFTER_SECONDS": "300",
    }
    env_example = (_ROOT / ".env.example").read_text(encoding="utf-8")
    for filename in ("docker-compose.yaml", "docker-compose.dockerhub.yaml"):
        compose = (_ROOT / filename).read_text(encoding="utf-8")
        for name, default in expected.items():
            assert f"{name}=${{{name}:-{default}}}" in compose
            assert f"# {name}={default}" in env_example


@pytest.mark.asyncio
async def test_live_endpoint_disabled_without_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as main_module

    monkeypatch.setattr(main_module, "get_liveness_watchdog", lambda: None)
    response = await main_module.live()
    assert response.status_code == 200
    assert json.loads(bytes(response.body)) == {
        "alive": True,
        "watchdog": "disabled",
    }


@pytest.mark.asyncio
async def test_live_endpoint_reports_fresh_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as main_module

    clock = _FakeClock()
    watchdog = _make_watchdog(clock)
    monkeypatch.setattr(main_module, "get_liveness_watchdog", lambda: watchdog)

    response = await main_module.live()
    assert response.status_code == 200
    body = json.loads(bytes(response.body))
    assert body["alive"] is True
    assert body["heartbeat_age_seconds"] == 0.0
    assert body["stale_after_seconds"] == 50.0


@pytest.mark.asyncio
async def test_live_endpoint_reports_stale_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as main_module

    clock = _FakeClock()
    watchdog = _make_watchdog(clock)
    clock.advance(500.0)
    monkeypatch.setattr(main_module, "get_liveness_watchdog", lambda: watchdog)

    response = await main_module.live()
    assert response.status_code == 503
    body = json.loads(bytes(response.body))
    assert body["alive"] is False
    assert body["heartbeat_age_seconds"] == 500.0
