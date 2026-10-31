"""Offline range shadow artifacts; never submits orders or changes live state."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from app.core.market_calendar import get_session, is_trading_hours, next_session_open
from app.domain.range_crossday import RangeCrossdayEngine
from app.domain.range_evaluation import summarize_comparison
from app.domain.range_replay import RangeReplayConfig, RangeReplayEngine
from app.services.range_tape_service import TapeReadStats, iter_tape_quotes

FORWARD_FLOOR = datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)
_APP = Path(__file__).resolve().parents[1]
# Include every transitive owner of semantics, including this driver and CLI.
_IDENTITY_FILES = (
    "domain/RANGE_RECLAIM_PREREGISTRATION.md",
    "domain/RANGE_CROSSDAY_PREREGISTRATION.md", "domain/range_crossday.py",
    "domain/range_replay.py", "domain/range_evaluation.py",
    "domain/strategy_v2/clustered_returns.py",
    "core/market_calendar.py", "core/holiday_calendar.py",
    "core/live_risk_day.py", "core/accounting_fees.py",
    "services/range_tape_service.py", "services/range_research_service.py",
    "cli/range_replay.py",
)
ResearchPhase = Literal["entry", "crossday"]


def _algorithm(phase: ResearchPhase) -> str:
    if phase not in ("entry", "crossday"):
        raise ValueError("unknown registered research phase")
    return "range-crossday-loss-v1" if phase == "crossday" else "range-reclaim-v1"


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("an aware clock is required")
    return value.astimezone(timezone.utc)


def session_days(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError("end day precedes start day")
    if (end-start).days > 366:
        raise ValueError("range research windows are limited to 366 days")
    session = get_session("US")
    days = []
    for offset in range((end-start).days+1):
        day = start+timedelta(days=offset)
        if is_trading_hours("US", datetime.combine(day, session.rth_open, tzinfo=session.timezone)):
            days.append(day)
    return days


def _bounds(day: date) -> tuple[datetime, datetime]:
    session = get_session("US")
    return (
        datetime.combine(day, session.rth_open, tzinfo=session.timezone).astimezone(timezone.utc),
        datetime.combine(day, session.close_time(day), tzinfo=session.timezone).astimezone(timezone.utc),
    )


def select_tape_files(root: Path, symbol: str, days: list[date]) -> list[Path]:
    """Read only UTC hour files intersecting RTH of explicitly requested days."""
    paths = []
    for day in days:
        start, end = _bounds(day)
        hour = start.replace(minute=0)
        while hour < end:
            paths.extend(sorted((root/symbol/hour.strftime("%Y%m%d")).glob(
                hour.strftime("%H")+"-*.jsonl.gz",
            )))
            hour += timedelta(hours=1)
    return paths


def implementation_identity() -> dict[str, str]:
    return {name: hashlib.sha256((_APP/name).read_bytes()).hexdigest() for name in _IDENTITY_FILES}


def _registration(
    config: RangeReplayConfig, registered_at: datetime, phase: ResearchPhase,
) -> dict[str, Any]:
    now = _utc(registered_at)
    # Use the next complete session if registration occurs after the opening;
    # this avoids a partial first day and all retrospective "forward" labels.
    earliest = max(now, FORWARD_FLOOR)
    session = get_session("US")
    day = session.local(earliest).date()
    open_at, _ = _bounds(day)
    if not is_trading_hours("US", open_at) or earliest > open_at:
        earliest = next_session_open("US", earliest)
    else:
        earliest = open_at
    payload = {
        "schema_version": 1, "algorithm_version": _algorithm(phase), "phase": phase,
        "registered_at": now.isoformat(), "evidence_not_before": earliest.isoformat(),
        "config_version": config.version_hash, "implementation": implementation_identity(),
        "automatic_promotion": False, "live_equivalent": False,
    }
    return {**payload, "registration_digest": hashlib.sha256(_canonical(payload)).hexdigest()}


def register_forward(
    path: Path, config: RangeReplayConfig, *, now: datetime, phase: ResearchPhase = "entry",
) -> dict[str, Any]:
    document = _registration(config, now, phase)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: prior evidence registration is never overwritten.
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(document, indent=2, sort_keys=True)+"\n")
    return document


def load_registration(
    path: Path, config: RangeReplayConfig, *, phase: ResearchPhase = "entry",
) -> dict[str, Any]:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate registration key")
            result[key] = value
        return result
    value = json.loads(path.read_text(), object_pairs_hook=unique_pairs)
    if not isinstance(value, dict) or not isinstance(value.get("registered_at"), str):
        raise ValueError("invalid registration")
    expected = _registration(config, datetime.fromisoformat(value["registered_at"]), phase)
    if _canonical(value) != _canonical(expected):
        raise ValueError("forward identity mismatch; re-register and reset evidence clock")
    return expected


def evaluate_tapes(
    root: Path, start: date, end: date, *, config: RangeReplayConfig,
    now: datetime, registration: Path | None = None, phase: ResearchPhase = "entry",
) -> dict[str, Any]:
    clock = _utc(now)
    producer = implementation_identity()
    days = session_days(start, end)
    algorithm = _algorithm(phase)
    evidence = load_registration(registration, config, phase=phase) if registration else None
    if evidence and datetime.fromisoformat(evidence["registered_at"]) > clock:
        raise ValueError("registration is in the future")
    eligible = days
    if evidence:
        floor = datetime.fromisoformat(evidence["evidence_not_before"])
        if any(_bounds(day)[0] < floor for day in days):
            raise ValueError("historical or partial-session input cannot be forward evidence")
        eligible = [day for day in days if _bounds(day)[1] < clock-timedelta(minutes=5)]
    elif any(_bounds(day)[1] >= clock-timedelta(minutes=5) for day in days):
        raise ValueError("only closed, settled tape sessions may be evaluated")
    if not eligible:
        return {
            "status": "WAITING_FOR_MARKET_DATA", "evidence": evidence, "algorithm_version": algorithm,
            "requested_start": start.isoformat(), "requested_end": end.isoformat(),
            "evaluated_sessions": [], "automatic_promotion": False,
            "live_equivalent": False, "adoption_authorized": False,
        }
    paths = select_tape_files(root, config.symbol, eligible)
    stats = TapeReadStats()
    engines: dict[str, RangeReplayEngine] = (
        {"crossday_baseline": RangeCrossdayEngine(config, candidate=False),
         "crossday_candidate": RangeCrossdayEngine(config, candidate=True)}
        if phase == "crossday" else {
            "crossing_baseline": RangeReplayEngine(config, "crossing_baseline"),
            "reclaim_candidate": RangeReplayEngine(config, "reclaim_candidate"),
        }
    )
    start_at = _bounds(eligible[0])[0]
    end_at = _bounds(eligible[-1])[1]
    events: Iterator[Any] = iter_tape_quotes(
        paths, symbol=config.symbol, start_at=start_at, end_at=end_at, stats=stats,
    )
    for quote in events:
        for engine in engines.values():
            engine.on_quote(quote)
    if implementation_identity() != producer:
        raise ValueError("evaluator implementation changed during replay")
    reports = {name: engine.report() for name, engine in engines.items()}
    return {
        "schema_version": 1, "generated_at": clock.isoformat(), "algorithm_version": algorithm,
        "evidence": evidence, "implementation": producer,
        "config_version": config.version_hash,
        "evaluated_sessions": [d.isoformat() for d in eligible],
        "requested_start": start.isoformat(), "requested_end": end.isoformat(),
        "tape": asdict(stats), "replays": reports,
        **summarize_comparison(
            reports, eligible, forward=evidence is not None,
            arms=("crossday_baseline", "crossday_candidate") if phase == "crossday"
            else ("crossing_baseline", "reclaim_candidate"),
        ),
    }
