from __future__ import annotations

import logging

from app.core.log_throttle import HealthcheckAccessFilter, RepeatedLogThrottle


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _make_record(path: str, status_code: int = 200) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/1.1" %d',
        args=("127.0.0.1:1", "GET", path, status_code),
        exc_info=None,
    )


class TestRepeatedLogThrottle:
    def test_first_call_emits(self) -> None:
        clock = _FakeClock(100.0)
        throttle = RepeatedLogThrottle(window_seconds=3600.0, clock=clock)

        assert throttle.should_log("gate") is True
        assert throttle.suppressed_count == 0

    def test_repeats_within_window_are_suppressed_and_counted(self) -> None:
        clock = _FakeClock(100.0)
        throttle = RepeatedLogThrottle(window_seconds=3600.0, clock=clock)

        assert throttle.should_log("gate") is True
        clock.now = 200.0
        assert throttle.should_log("gate") is False
        clock.now = 300.0
        assert throttle.should_log("gate") is False

        assert throttle.suppressed_count == 2

    def test_emission_after_window_reports_suppressed_count(self) -> None:
        clock = _FakeClock(100.0)
        throttle = RepeatedLogThrottle(window_seconds=3600.0, clock=clock)

        throttle.should_log("gate")
        clock.now = 200.0
        throttle.should_log("gate")
        clock.now = 300.0
        throttle.should_log("gate")
        clock.now = 100.0 + 3601.0

        suppressed = throttle.take_suppressed_count()
        assert suppressed == 2
        assert throttle.should_log("gate") is True
        assert throttle.take_suppressed_count() == 0

    def test_take_suppressed_count_resets_counter(self) -> None:
        clock = _FakeClock(100.0)
        throttle = RepeatedLogThrottle(window_seconds=3600.0, clock=clock)

        throttle.should_log("gate")
        clock.now = 150.0
        throttle.should_log("gate")

        assert throttle.take_suppressed_count() == 1
        assert throttle.take_suppressed_count() == 0

    def test_distinct_keys_do_not_interfere(self) -> None:
        clock = _FakeClock(100.0)
        throttle = RepeatedLogThrottle(window_seconds=3600.0, clock=clock)

        assert throttle.should_log("gate") is True
        assert throttle.should_log("other") is True
        clock.now = 150.0
        assert throttle.should_log("gate") is False
        assert throttle.should_log("other") is False

        assert throttle.suppressed_count == 2

    def test_zero_window_never_suppresses(self) -> None:
        clock = _FakeClock(100.0)
        throttle = RepeatedLogThrottle(window_seconds=0.0, clock=clock)

        assert throttle.should_log("gate") is True
        clock.now = 100.1
        assert throttle.should_log("gate") is True
        assert throttle.suppressed_count == 0

    def test_thread_safety_under_concurrent_calls(self) -> None:
        import threading

        clock = _FakeClock(100.0)
        throttle = RepeatedLogThrottle(window_seconds=3600.0, clock=clock)
        emitted = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(200):
                if throttle.should_log("gate"):
                    with lock:
                        emitted.append(1)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(emitted) == 1
        assert throttle.suppressed_count == 799


class TestHealthcheckAccessFilter:
    def test_ready_and_live_requests_are_dropped(self) -> None:
        f = HealthcheckAccessFilter()
        assert f.filter(_make_record("/api/ready")) is False
        assert f.filter(_make_record("/api/live")) is False

    def test_normal_requests_pass(self) -> None:
        f = HealthcheckAccessFilter()
        assert f.filter(_make_record("/api/orders")) is True
        assert f.filter(_make_record("/api/readyz")) is True
        assert f.filter(_make_record("/api/livez")) is True

    def test_non_access_records_pass(self) -> None:
        f = HealthcheckAccessFilter()
        record = logging.LogRecord(
            name="auto_trade.main",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="runner started",
            args=None,
            exc_info=None,
        )
        assert f.filter(record) is True

    def test_malformed_args_pass_through_untouched(self) -> None:
        f = HealthcheckAccessFilter()
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="%s",
            args=("weird",),
            exc_info=None,
        )
        assert f.filter(record) is True
