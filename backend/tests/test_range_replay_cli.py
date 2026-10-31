from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.cli import range_replay as cli


class _Clock:
    @staticmethod
    def now(_zone: object) -> datetime:
        return datetime(2026, 10, 31, 4, tzinfo=timezone.utc)


def test_offline_cli_registers_and_waits_without_database_or_order_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "datetime", _Clock)
    before = set(sys.modules)
    registration = tmp_path/"forward.json"
    report = tmp_path/"report.json"
    assert cli.main(["register", "--phase", "crossday", "--output", str(registration)]) == 0
    assert cli.main([
        "evaluate", "--phase", "crossday", "--tape-root", str(tmp_path),
        "--start", "2026-11-02", "--end", "2026-11-02",
        "--registration", str(registration), "--output", str(report),
    ]) == 0
    result = json.loads(report.read_text())
    assert result["status"] == "WAITING_FOR_MARKET_DATA"
    assert result["algorithm_version"] == "range-crossday-loss-v1"
    assert result["adoption_authorized"] is False
    forbidden = ("app.database", "app.runner", "app.config", "app.core.broker",
                 "app.services.trade_execution_service")
    assert not any(name in forbidden for name in set(sys.modules)-before)


def test_cli_never_overwrites_prior_artifacts_or_labels_history_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "datetime", _Clock)
    registration = tmp_path/"forward.json"
    report = tmp_path/"report.json"
    assert cli.main(["register", "--output", str(registration)]) == 0
    with pytest.raises(SystemExit) as exc:
        cli.main([
            "evaluate", "--tape-root", str(tmp_path), "--start", "2026-10-30",
            "--end", "2026-10-30", "--registration", str(registration),
            "--output", str(report),
        ])
    assert exc.value.code == 2
    assert not report.exists()
    with pytest.raises(SystemExit) as exc:
        cli.main(["register", "--output", str(registration)])
    assert exc.value.code == 2
