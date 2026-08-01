from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import main as main_module
from app.domain.opening_research_quiet_window import (
    is_opening_research_quiet_window,
)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (
            datetime(2026, 7, 27, 13, 27, 59, tzinfo=timezone.utc),
            False,
        ),
        (datetime(2026, 7, 27, 13, 28, tzinfo=timezone.utc), True),
        (datetime(2026, 7, 27, 13, 30, tzinfo=timezone.utc), True),
        (
            datetime(2026, 7, 27, 13, 34, 59, tzinfo=timezone.utc),
            True,
        ),
        (datetime(2026, 7, 27, 13, 35, tzinfo=timezone.utc), False),
        (datetime(2026, 7, 25, 13, 30, tzinfo=timezone.utc), False),
        (datetime(2026, 7, 3, 13, 30, tzinfo=timezone.utc), False),
        (datetime(2026, 1, 26, 14, 28, tzinfo=timezone.utc), True),
        (datetime(2026, 1, 26, 14, 35, tzinfo=timezone.utc), False),
    ],
)
def test_opening_research_quiet_window_boundaries(
    now: datetime,
    expected: bool,
) -> None:
    assert is_opening_research_quiet_window(now) is expected


def test_research_window_is_independent_from_live_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 27, 13, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(
        main_module.settings,
        "opening_momentum_execution_enabled",
        False,
    )

    assert main_module._opening_research_quiet_window(now) is True
    assert main_module._opening_execution_priority_window(now) is False
    assert main_module._opening_momentum_poll_seconds(now) == 15


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 7, 27, 13, 28),
        datetime(
            2026,
            7,
            27,
            9,
            28,
            tzinfo=ZoneInfo("America/New_York"),
        ),
    ],
)
def test_research_window_normalizes_supported_datetime_inputs(
    now: datetime,
) -> None:
    assert is_opening_research_quiet_window(now) is True
