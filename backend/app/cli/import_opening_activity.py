from __future__ import annotations

import argparse
import gzip
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from sqlalchemy.orm import Session

from app.core.market_calendar import get_session
from app.database import SessionLocal, init_db
from app.models import OpeningActivityObservation


_CACHE_VERSION = "opening-activity-first6-v1"
_WINDOW_MINUTES = 5
_SOURCE = "RESEARCH_CACHE_BACKFILL_V1"


@dataclass(frozen=True)
class OpeningActivityRecord:
    session_date: date
    symbol: str
    volume: float
    turnover: float | None
    observed_at: datetime


@dataclass(frozen=True)
class OpeningActivityImportResult:
    cache_records: int
    inserted_records: int
    existing_records: int
    incomplete_symbol_sessions: int


def load_opening_activity_cache(
    path: Path,
    *,
    through_date: date | None = None,
) -> tuple[list[OpeningActivityRecord], int]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict) or raw.get("cache_version") != _CACHE_VERSION:
        raise ValueError("opening activity cache version is incompatible")
    if raw.get("window_minutes") != 6:
        raise ValueError("opening activity cache must contain six minutes")
    raw_symbols = raw.get("symbols")
    if not isinstance(raw_symbols, dict):
        raise ValueError("opening activity cache has no symbol map")

    market_session = get_session("US")
    records: list[OpeningActivityRecord] = []
    incomplete = 0
    normalized_symbols: set[str] = set()
    for raw_symbol, raw_rows in raw_symbols.items():
        if not isinstance(raw_symbol, str) or not isinstance(raw_rows, dict):
            raise ValueError("opening activity symbol entry is invalid")
        symbol = raw_symbol.strip().upper()
        if not symbol or symbol in normalized_symbols:
            raise ValueError("opening activity symbols must be unique")
        normalized_symbols.add(symbol)
        by_date: dict[date, dict[int, tuple[float, float]]] = {}
        for raw_timestamp, raw_values in raw_rows.items():
            if (
                not isinstance(raw_timestamp, str)
                or not isinstance(raw_values, list)
                or len(raw_values) != 2
            ):
                raise ValueError("opening activity row is invalid")
            timestamp = datetime.fromisoformat(raw_timestamp)
            if timestamp.tzinfo is None:
                raise ValueError("opening activity timestamp must be aware")
            local = market_session.local(timestamp)
            session_date = local.date()
            if through_date is not None and session_date > through_date:
                continue
            session_open = datetime.combine(
                session_date,
                market_session.rth_open,
                tzinfo=market_session.timezone,
            )
            offset_seconds = (local - session_open).total_seconds()
            if offset_seconds % 60 != 0:
                continue
            offset = int(offset_seconds // 60)
            if not 0 <= offset < _WINDOW_MINUTES:
                continue
            volume, turnover = (float(value) for value in raw_values)
            if (
                not math.isfinite(volume)
                or volume < 0
                or not math.isfinite(turnover)
                or turnover < 0
            ):
                raise ValueError("opening activity values must be nonnegative")
            session_rows = by_date.setdefault(session_date, {})
            if offset in session_rows:
                raise ValueError("duplicate opening activity minute")
            session_rows[offset] = (volume, turnover)

        for session_date, session_rows in by_date.items():
            if any(offset not in session_rows for offset in range(_WINDOW_MINUTES)):
                incomplete += 1
                continue
            volume = sum(session_rows[offset][0] for offset in range(_WINDOW_MINUTES))
            turnover = sum(
                session_rows[offset][1] for offset in range(_WINDOW_MINUTES)
            )
            if volume <= 0 or not math.isfinite(volume):
                incomplete += 1
                continue
            session_open = datetime.combine(
                session_date,
                market_session.rth_open,
                tzinfo=market_session.timezone,
            ).astimezone(timezone.utc)
            records.append(OpeningActivityRecord(
                session_date=session_date,
                symbol=symbol,
                volume=volume,
                turnover=(
                    turnover
                    if turnover > 0 and math.isfinite(turnover)
                    else None
                ),
                observed_at=(
                    session_open + timedelta(minutes=_WINDOW_MINUTES)
                ),
            ))
    records.sort(key=lambda item: (item.session_date, item.symbol))
    return records, incomplete


def import_opening_activity_records(
    db: Session,
    records: list[OpeningActivityRecord],
    *,
    incomplete_symbol_sessions: int = 0,
) -> OpeningActivityImportResult:
    if not records:
        return OpeningActivityImportResult(
            cache_records=0,
            inserted_records=0,
            existing_records=0,
            incomplete_symbol_sessions=incomplete_symbol_sessions,
        )
    first_date = min(item.session_date for item in records)
    last_date = max(item.session_date for item in records)
    symbols = {item.symbol for item in records}
    existing = {
        (row.session_date, row.symbol)
        for row in db.query(OpeningActivityObservation)
        .filter(
            OpeningActivityObservation.window_minutes == _WINDOW_MINUTES,
            OpeningActivityObservation.session_date >= first_date,
            OpeningActivityObservation.session_date <= last_date,
            OpeningActivityObservation.symbol.in_(symbols),
        )
        .all()
    }
    inserted = 0
    for item in records:
        key = (item.session_date, item.symbol)
        if key in existing:
            continue
        db.add(OpeningActivityObservation(
            session_date=item.session_date,
            symbol=item.symbol,
            window_minutes=_WINDOW_MINUTES,
            volume=item.volume,
            turnover=item.turnover,
            source=_SOURCE,
            observed_at=item.observed_at,
        ))
        existing.add(key)
        inserted += 1
    db.commit()
    return OpeningActivityImportResult(
        cache_records=len(records),
        inserted_records=inserted,
        existing_records=len(records) - inserted,
        incomplete_symbol_sessions=incomplete_symbol_sessions,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import causal first-five-minute activity observations",
    )
    parser.add_argument("cache_path", type=Path)
    parser.add_argument("--through-date", type=date.fromisoformat)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    records, incomplete = load_opening_activity_cache(
        cast(Path, args.cache_path),
        through_date=cast(date | None, args.through_date),
    )
    init_db()
    db = SessionLocal()
    try:
        result = import_opening_activity_records(
            db,
            records,
            incomplete_symbol_sessions=incomplete,
        )
    finally:
        db.close()
    print(json.dumps({
        "cache_records": result.cache_records,
        "inserted_records": result.inserted_records,
        "existing_records": result.existing_records,
        "incomplete_symbol_sessions": (
            result.incomplete_symbol_sessions
        ),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
