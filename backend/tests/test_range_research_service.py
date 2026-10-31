from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.range_replay import RangeReplayConfig
from app.services.range_research_service import (
    evaluate_tapes, load_registration, register_forward, select_tape_files, session_days,
)

NOW = datetime(2026, 10, 31, 4, tzinfo=timezone.utc)


def test_registration_is_immutable_and_cannot_relabel_history(tmp_path: Path) -> None:
    path = tmp_path/"registration.json"
    registration = register_forward(path, RangeReplayConfig(), now=NOW)
    assert registration["registered_at"] == NOW.isoformat()
    assert registration["evidence_not_before"] == "2026-11-02T14:30:00+00:00"
    assert registration["automatic_promotion"] is False
    assert load_registration(path, RangeReplayConfig()) == registration
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        register_forward(path, RangeReplayConfig(), now=NOW)
    assert path.read_bytes() == original


def test_changed_config_or_tampered_registration_is_rejected(tmp_path: Path) -> None:
    path = tmp_path/"registration.json"
    register_forward(path, RangeReplayConfig(), now=NOW)
    with pytest.raises(ValueError, match="identity"):
        load_registration(path, replace(RangeReplayConfig(), max_notional=Decimal("50000")))
    document = json.loads(path.read_text())
    document["evidence_not_before"] = "2026-10-01T00:00:00+00:00"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        load_registration(path, RangeReplayConfig())


def test_late_registration_cannot_backdate_forward_clock(tmp_path: Path) -> None:
    late = datetime(2026, 11, 3, 15, tzinfo=timezone.utc)
    result = register_forward(tmp_path/"registration.json", RangeReplayConfig(), now=late)
    assert result["evidence_not_before"] == "2026-11-04T14:30:00+00:00"


def test_registration_rejects_changed_implementation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import range_research_service as service
    path = tmp_path/"registration.json"
    register_forward(path, RangeReplayConfig(), now=NOW)
    monkeypatch.setattr(service, "implementation_identity", lambda: {"changed.py": "0"*64})
    with pytest.raises(ValueError, match="identity"):
        load_registration(path, RangeReplayConfig())


def test_forward_without_market_data_waits_without_fake_zero_performance(tmp_path: Path) -> None:
    from datetime import date
    path = tmp_path/"registration.json"
    register_forward(path, RangeReplayConfig(), now=NOW)
    result = evaluate_tapes(
        tmp_path, date(2026, 11, 2), date(2026, 11, 2),
        config=RangeReplayConfig(), now=NOW, registration=path,
    )
    assert result["status"] == "WAITING_FOR_MARKET_DATA"
    assert result["evaluated_sessions"] == []
    assert "cost_totals" not in result
    with pytest.raises(ValueError, match="historical"):
        evaluate_tapes(
            tmp_path, date(2026, 10, 30), date(2026, 11, 2),
            config=RangeReplayConfig(), now=NOW, registration=path,
        )


def test_missing_historical_tapes_are_missing_sessions_not_healthy_evidence(tmp_path: Path) -> None:
    from datetime import date
    result = evaluate_tapes(
        tmp_path, date(2026, 10, 29), date(2026, 10, 30),
        config=RangeReplayConfig(), now=NOW,
    )
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["evidence_class"] == "RETROSPECTIVE_DIAGNOSTIC"
    assert result["arms"]["crossing_baseline"]["daily"]["2026-10-30"]["coverage"]["fraction"] == 0
    assert result["arms"]["reclaim_candidate"]["quality_complete"] is False


def test_hour_selection_obeys_dst_and_early_close_without_reading_live_tail(tmp_path: Path) -> None:
    from datetime import date
    for day in ("20261030", "20261102", "20261127"):
        folder = tmp_path/"TSLA.US"/day
        folder.mkdir(parents=True)
        for hour in range(12, 23):
            (folder/f"{hour:02}-run.jsonl.gz").touch()
    hours = lambda day: [p.name[:2] for p in select_tape_files(tmp_path, "TSLA.US", [day])]
    assert hours(date(2026, 10, 30)) == ["13", "14", "15", "16", "17", "18", "19"]
    assert hours(date(2026, 11, 2)) == ["14", "15", "16", "17", "18", "19", "20"]
    assert hours(date(2026, 11, 27)) == ["14", "15", "16", "17"]
    assert session_days(date(2026, 11, 26), date(2026, 11, 29)) == [date(2026, 11, 27)]


def test_corrupted_tape_fails_instead_of_emitting_a_partial_report(tmp_path: Path) -> None:
    from datetime import date
    folder = tmp_path/"TSLA.US"/"20261030"
    folder.mkdir(parents=True)
    (folder/"14-run.jsonl.gz").write_bytes(b"not a gzip")
    with pytest.raises(ValueError, match="gzip"):
        evaluate_tapes(
            tmp_path, date(2026, 10, 30), date(2026, 10, 30),
            config=RangeReplayConfig(), now=NOW,
        )


def test_research_phases_cannot_share_forward_identity(tmp_path: Path) -> None:
    path = tmp_path/"risk-registration.json"
    register_forward(path, RangeReplayConfig(), now=NOW, phase="crossday")
    assert load_registration(path, RangeReplayConfig(), phase="crossday")["algorithm_version"] == "range-crossday-loss-v1"
    with pytest.raises(ValueError, match="identity"):
        load_registration(path, RangeReplayConfig(), phase="entry")
