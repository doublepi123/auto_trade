from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.cli.import_opening_activity import (
    import_opening_activity_records,
    load_opening_activity_cache,
)
from app.models import Base, OpeningActivityObservation


def _write_cache(path) -> None:
    session_open = datetime(2026, 7, 27, 13, 30, tzinfo=timezone.utc)
    rows = {
        (session_open + timedelta(minutes=offset)).isoformat(): [
            float(100 + offset),
            float(1_000 + offset),
        ]
        for offset in range(6)
    }
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump({
            "cache_version": "opening-activity-first6-v1",
            "start_date": "2026-07-27",
            "end_date": "2026-07-27",
            "window_minutes": 6,
            "symbols": {"aapl.us": rows},
        }, handle)


def test_load_opening_activity_cache_uses_only_first_five_minutes(
    tmp_path,
) -> None:
    path = tmp_path / "activity.json.gz"
    _write_cache(path)

    records, incomplete = load_opening_activity_cache(path)

    assert incomplete == 0
    assert len(records) == 1
    assert records[0].symbol == "AAPL.US"
    assert records[0].volume == 510.0
    assert records[0].turnover == 5_010.0
    assert records[0].observed_at == datetime(
        2026,
        7,
        27,
        13,
        35,
        tzinfo=timezone.utc,
    )


def test_import_opening_activity_records_is_idempotent(tmp_path) -> None:
    path = tmp_path / "activity.json.gz"
    _write_cache(path)
    records, incomplete = load_opening_activity_cache(path)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    db = Session(bind=engine)
    try:
        first = import_opening_activity_records(
            db,
            records,
            incomplete_symbol_sessions=incomplete,
        )
        second = import_opening_activity_records(
            db,
            records,
            incomplete_symbol_sessions=incomplete,
        )

        assert first.inserted_records == 1
        assert first.existing_records == 0
        assert second.inserted_records == 0
        assert second.existing_records == 1
        row = db.query(OpeningActivityObservation).one()
        assert row.source == "RESEARCH_CACHE_BACKFILL_V1"
        assert row.window_minutes == 5
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
