from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Protocol

from app.config import settings
from app.core.broker import BrokerCandle, BrokerGateway
from app.core.market_calendar import get_session
from app.database import SessionLocal
from app.domain.opening_momentum import (
    OpeningMomentumConfig,
    OpeningMomentumObservation,
)
from app.domain.opening_momentum_extension import (
    OPENING_EXTENSION_RESEARCH_VERSION,
    OpeningExtensionCandidateReport,
    OpeningExtensionExitPrice,
    OpeningExtensionResearchReport,
    OpeningExtensionSession,
    OpeningExtensionSlice,
    evaluate_opening_extension_candidates,
)
from app.models import StrategyV2ShadowConfig


OPENING_EXTENSION_CLI_VERSION = "opening-extension-research-cli-v4"
_CACHE_VERSION = "opening-extension-minute-cache-ohlc-v3"
_BAR_DURATION = timedelta(minutes=1)
_DEFAULT_SIGNAL_MINUTES = (2, 3, 5, 10)
_DEFAULT_HOLDING_MINUTES = (30, 60, 90, 120)
_DEFAULT_COST_STRESS_BPS = (14.0, 20.0, 30.0)
_FROZEN_SELECTION_SIGNAL_MINUTES = 3
_FROZEN_SELECTION_HOLDING_MINUTES = 60
_FROZEN_SELECTION_STOP_LOSS_PCT = 1.0
_EXECUTION_COHORT_MAX_SYMBOLS = 6
_EXECUTION_COHORT_MINIMUM_DISPLACEMENTS = 4
_EXECUTION_COHORT_SELECTION_VERSION = (
    "individual-discovery-top6-positive-delta-min4-stop1-shortlist-v2"
)
_JOINT_EXPLORATION_MAX_SYMBOLS = 6
_JOINT_EXPLORATION_MINIMUM_DISPLACEMENTS = 1
_JOINT_EXPLORATION_SELECTION_VERSION = (
    "individual-discovery-top6-positive-tail-risk-min1-"
    "joint-exploration-v1"
)


class HistoricalCandleProvider(Protocol):
    def get_history_candlesticks_by_offset(
        self,
        symbol: str,
        period: str,
        count: int,
        after: datetime,
    ) -> list[BrokerCandle]: ...


@dataclass(frozen=True)
class RawMinuteBar:
    timestamp: datetime
    open: float
    close: float
    high: float | None = None
    low: float | None = None

    def __post_init__(self) -> None:
        timestamp = _as_utc(self.timestamp)
        if timestamp.second or timestamp.microsecond:
            raise ValueError("minute bar timestamp must be minute-aligned")
        high = float(self.high) if self.high is not None else max(
            self.open,
            self.close,
        )
        low = float(self.low) if self.low is not None else min(
            self.open,
            self.close,
        )
        if any(
            not math.isfinite(value) or value <= 0
            for value in (self.open, self.close, high, low)
        ):
            raise ValueError("minute bar prices must be positive")
        if high < max(self.open, self.close) or low > min(
            self.open,
            self.close,
        ):
            raise ValueError("minute bar OHLC range is inconsistent")
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "high", high)
        object.__setattr__(self, "low", low)


@dataclass(frozen=True)
class GridEvaluation:
    signal_minutes: int
    holding_minutes: int
    report: OpeningExtensionResearchReport


@dataclass(frozen=True)
class DiscoverySelection:
    grid: GridEvaluation
    candidate: OpeningExtensionCandidateReport
    discovery: OpeningExtensionSlice
    discovery_blockers: tuple[str, ...]


def _configure_longport_environment() -> None:
    credentials = {
        "LONGPORT_APP_KEY": settings.longbridge_app_key,
        "LONGPORT_APP_SECRET": settings.longbridge_app_secret,
        "LONGPORT_ACCESS_TOKEN": settings.longbridge_access_token,
    }
    missing = [name for name, value in credentials.items() if not value]
    if missing:
        raise RuntimeError(
            "Longport credentials are unavailable: " + ", ".join(missing)
        )
    for name, value in credentials.items():
        os.environ[name] = value


def _load_current_baseline_symbols() -> tuple[str, ...]:
    """Load the symbols that can actually enter the opening strategy."""
    db = SessionLocal()
    try:
        rows = (
            db.query(StrategyV2ShadowConfig)
            .filter(
                StrategyV2ShadowConfig.enabled.is_(True),
                StrategyV2ShadowConfig.opening_momentum_execution_eligible.is_(
                    True
                ),
                StrategyV2ShadowConfig.symbol.like("%.US"),
            )
            .order_by(StrategyV2ShadowConfig.symbol.asc())
            .all()
        )
        symbols = tuple(row.symbol.strip().upper() for row in rows)
    finally:
        db.close()
    if not symbols:
        raise RuntimeError(
            "current opening-execution US baseline universe is empty"
        )
    return symbols


def _fetch_symbol_bars(
    provider: HistoricalCandleProvider,
    symbol: str,
    *,
    start_date: date,
    end_date: date,
    retained_minutes_after_open: int,
    page_size: int = 1000,
) -> tuple[RawMinuteBar, ...]:
    if end_date < start_date:
        raise ValueError("end_date must not precede start_date")
    if retained_minutes_after_open < 0:
        raise ValueError("retained minute range must be non-negative")
    if not 1 <= page_size <= 1000:
        raise ValueError("page_size must be in [1, 1000]")

    session = get_session("US")
    start = datetime.combine(
        start_date,
        time.min,
        tzinfo=session.timezone,
    ).astimezone(timezone.utc)
    stop = datetime.combine(
        end_date + timedelta(days=1),
        time.min,
        tzinfo=session.timezone,
    ).astimezone(timezone.utc)
    cursor = start
    non_advancing_pages = 0
    retained: dict[datetime, RawMinuteBar] = {}
    while cursor < stop:
        page = provider.get_history_candlesticks_by_offset(
            symbol,
            "MIN_1",
            page_size,
            cursor,
        )
        if not page:
            break
        timestamps = tuple(_as_utc(item.timestamp) for item in page)
        latest = max(timestamps)
        if latest < cursor:
            non_advancing_pages += 1
            if non_advancing_pages >= 2:
                break
            continue
        non_advancing_pages = 0
        for candle, timestamp in zip(page, timestamps, strict=True):
            if not start <= timestamp < stop:
                continue
            if not _within_opening_window(
                timestamp,
                retained_minutes_after_open=retained_minutes_after_open,
            ):
                continue
            retained[timestamp] = RawMinuteBar(
                timestamp=timestamp,
                open=float(candle.open),
                close=float(candle.close),
                high=float(candle.high),
                low=float(candle.low),
            )
        next_cursor = latest + _BAR_DURATION
        if next_cursor <= cursor:
            raise RuntimeError(
                f"historical candle cursor stalled for {symbol}"
            )
        cursor = next_cursor
        if latest >= stop:
            break
    return tuple(retained[value] for value in sorted(retained))


def _within_opening_window(
    timestamp: datetime,
    *,
    retained_minutes_after_open: int,
) -> bool:
    session = get_session("US")
    local = session.local(timestamp)
    session_open = datetime.combine(
        local.date(),
        session.rth_open,
        tzinfo=session.timezone,
    )
    offset = int((local - session_open).total_seconds() // 60)
    return (
        local.second == 0
        and local.microsecond == 0
        and 0 <= offset <= retained_minutes_after_open
    )


def _baseline_session_dates(
    bars_by_symbol: dict[str, tuple[RawMinuteBar, ...]],
    *,
    baseline_symbols: Sequence[str],
    minimum_universe_size: int,
    minimum_data_coverage: float,
) -> tuple[date, ...]:
    required = max(
        minimum_universe_size,
        math.ceil(len(baseline_symbols) * minimum_data_coverage),
    )
    counts: dict[date, int] = {}
    session = get_session("US")
    for symbol in baseline_symbols:
        seen_dates: set[date] = set()
        for bar in bars_by_symbol.get(symbol, ()):
            local = session.local(bar.timestamp)
            if local.time() != session.rth_open:
                continue
            seen_dates.add(local.date())
        for session_date in seen_dates:
            counts[session_date] = counts.get(session_date, 0) + 1
    return tuple(sorted(
        session_date
        for session_date, count in counts.items()
        if count >= required
    ))


def _build_sessions(
    bars_by_symbol: dict[str, tuple[RawMinuteBar, ...]],
    *,
    symbols: Sequence[str],
    session_dates: Sequence[date],
    signal_minutes: int,
    execution_delay_minutes: int,
    holding_minutes: int,
    stop_loss_pct: float | None = None,
) -> tuple[OpeningExtensionSession, ...]:
    if signal_minutes <= 0:
        raise ValueError("signal_minutes must be positive")
    if execution_delay_minutes <= 0:
        raise ValueError("execution_delay_minutes must be positive")
    if holding_minutes <= 0:
        raise ValueError("holding_minutes must be positive")
    if stop_loss_pct is not None and (
        not math.isfinite(stop_loss_pct)
        or not 0 < stop_loss_pct <= 20
    ):
        raise ValueError("stop_loss_pct must be in (0, 20] when set")

    normalized_symbols = tuple(
        dict.fromkeys(symbol.strip().upper() for symbol in symbols)
    )
    bars_index = {
        symbol: {bar.timestamp: bar for bar in bars_by_symbol.get(symbol, ())}
        for symbol in normalized_symbols
    }
    market_session = get_session("US")
    result: list[OpeningExtensionSession] = []
    for session_date in sorted(set(session_dates)):
        session_open = datetime.combine(
            session_date,
            market_session.rth_open,
            tzinfo=market_session.timezone,
        ).astimezone(timezone.utc)
        expected_signal_bars = tuple(
            session_open + timedelta(minutes=index)
            for index in range(signal_minutes)
        )
        signal_at = expected_signal_bars[-1]
        entry_at = session_open + timedelta(
            minutes=signal_minutes + execution_delay_minutes
        )
        exit_at = entry_at + timedelta(minutes=holding_minutes)
        observations: list[OpeningMomentumObservation] = []
        exit_prices: list[OpeningExtensionExitPrice] = []
        for symbol in normalized_symbols:
            indexed = bars_index[symbol]
            if any(value not in indexed for value in expected_signal_bars):
                continue
            opening_bar = indexed[session_open]
            signal_bar = indexed[signal_at]
            entry_bar = indexed.get(entry_at)
            observations.append(OpeningMomentumObservation(
                symbol=symbol,
                session_open=opening_bar.open,
                signal_close=signal_bar.close,
                entry_open=entry_bar.open if entry_bar is not None else None,
            ))
            exit_outcome = _session_exit_outcome(
                indexed,
                entry_at=entry_at,
                exit_at=exit_at,
                entry_price=(
                    entry_bar.open if entry_bar is not None else None
                ),
                stop_loss_pct=stop_loss_pct,
            )
            if exit_outcome is not None:
                exit_prices.append(OpeningExtensionExitPrice(
                    symbol=symbol,
                    price=exit_outcome[0],
                    stop_triggered=exit_outcome[1],
                ))
        result.append(OpeningExtensionSession(
            session_date=session_date,
            observations=tuple(observations),
            exit_prices=tuple(exit_prices),
        ))
    return tuple(result)


def _session_exit_outcome(
    indexed: dict[datetime, RawMinuteBar],
    *,
    entry_at: datetime,
    exit_at: datetime,
    entry_price: float | None,
    stop_loss_pct: float | None,
) -> tuple[float, bool] | None:
    exit_bar = indexed.get(exit_at)
    if exit_bar is None or entry_price is None:
        return None
    if stop_loss_pct is None:
        return exit_bar.open, False

    stop_price = entry_price * (1 - stop_loss_pct / 100)
    for timestamp in sorted(
        value for value in indexed if entry_at <= value < exit_at
    ):
        bar = indexed[timestamp]
        if bar.open <= stop_price:
            return bar.open, True
        if bar.low is not None and bar.low <= stop_price:
            return stop_price, True
    return exit_bar.open, False


def _load_cache(
    path: Path,
    *,
    start_date: date,
    end_date: date,
    retained_minutes_after_open: int,
) -> dict[str, tuple[RawMinuteBar, ...]]:
    if not path.exists():
        return {}
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("opening research cache root must be an object")
    expected_metadata = {
        "cache_version": _CACHE_VERSION,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }
    if any(raw.get(key) != value for key, value in expected_metadata.items()):
        raise ValueError(
            "opening research cache metadata does not match the requested "
            "scope; choose a different cache path"
        )
    cached_retained_minutes = raw.get("retained_minutes_after_open")
    if not isinstance(cached_retained_minutes, int):
        raise ValueError(
            "opening research cache retained-minute metadata is invalid"
        )
    if cached_retained_minutes < retained_minutes_after_open:
        raise ValueError(
            "opening research cache covers only "
            f"{cached_retained_minutes} minutes after open but "
            f"{retained_minutes_after_open} are required; choose a "
            "different cache path"
        )
    raw_symbols = raw.get("bars_by_symbol")
    if not isinstance(raw_symbols, dict):
        raise ValueError("opening research cache has no symbol map")
    result: dict[str, tuple[RawMinuteBar, ...]] = {}
    for raw_symbol, raw_bars in raw_symbols.items():
        if not isinstance(raw_symbol, str) or not isinstance(raw_bars, list):
            raise ValueError("opening research cache symbol entry is invalid")
        parsed: list[RawMinuteBar] = []
        for raw_bar in raw_bars:
            if not isinstance(raw_bar, list) or len(raw_bar) != 5:
                raise ValueError("opening research cache bar is invalid")
            timestamp_raw, open_raw, high_raw, low_raw, close_raw = raw_bar
            if not isinstance(timestamp_raw, str):
                raise ValueError("opening research cache timestamp is invalid")
            parsed.append(RawMinuteBar(
                timestamp=datetime.fromisoformat(timestamp_raw),
                open=float(open_raw),
                high=float(high_raw),
                low=float(low_raw),
                close=float(close_raw),
            ))
        result[raw_symbol.strip().upper()] = tuple(parsed)
    return result


def _save_cache(
    path: Path,
    bars_by_symbol: dict[str, tuple[RawMinuteBar, ...]],
    *,
    start_date: date,
    end_date: date,
    retained_minutes_after_open: int,
) -> None:
    payload = {
        "cache_version": _CACHE_VERSION,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "retained_minutes_after_open": retained_minutes_after_open,
        "bars_by_symbol": {
            symbol: [
                [
                    bar.timestamp.isoformat(),
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                ]
                for bar in values
            ]
            for symbol, values in sorted(bars_by_symbol.items())
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump(
            payload,
            handle,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    os.replace(temporary, path)


def _slice(
    candidate: OpeningExtensionCandidateReport,
    name: str,
) -> OpeningExtensionSlice:
    return next(value for value in candidate.slices if value.name == name)


def _discovery_blockers(
    value: OpeningExtensionSlice,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if value.resolved_sessions < 20:
        blockers.append("DISCOVERY_SESSIONS_BELOW_20")
    if value.extension_signal_sessions < 3:
        blockers.append("DISCOVERY_EXTENSION_SIGNALS_BELOW_3")
    if value.comparison.cumulative_delta_bps <= 0:
        blockers.append("DISCOVERY_DELTA_NOT_POSITIVE")
    if (
        value.challenger.cumulative_without_best_3_bps
        <= value.baseline.cumulative_without_best_3_bps
    ):
        blockers.append("DISCOVERY_TAIL_ROBUSTNESS_FAILED")
    if not value.comparison.risk_guard_passed:
        blockers.append("DISCOVERY_DRAWDOWN_GUARD_FAILED")
    return tuple(blockers)


def _select_discovery_winner(
    grids: Sequence[GridEvaluation],
) -> DiscoverySelection:
    options: list[DiscoverySelection] = []
    for grid in grids:
        for candidate in grid.report.candidates:
            discovery = _slice(candidate, "DISCOVERY")
            options.append(DiscoverySelection(
                grid=grid,
                candidate=candidate,
                discovery=discovery,
                discovery_blockers=_discovery_blockers(discovery),
            ))
    if not options:
        raise ValueError("at least one grid candidate is required")
    return max(
        options,
        key=lambda value: (
            not value.discovery_blockers,
            value.discovery.comparison.mean_delta_bps,
            value.discovery.comparison.cumulative_delta_bps,
            (
                value.discovery.challenger.cumulative_without_best_3_bps
                - value.discovery.baseline.cumulative_without_best_3_bps
            ),
            value.discovery.extension_signal_sessions,
            -value.grid.signal_minutes,
            -value.grid.holding_minutes,
            value.candidate.symbol,
        ),
    )


def _frozen_selection_grid(
    grids: Sequence[GridEvaluation],
) -> GridEvaluation:
    matches = tuple(
        value
        for value in grids
        if (
            value.signal_minutes == _FROZEN_SELECTION_SIGNAL_MINUTES
            and value.holding_minutes
            == _FROZEN_SELECTION_HOLDING_MINUTES
            and value.report.stop_loss_pct
            == _FROZEN_SELECTION_STOP_LOSS_PCT
        )
    )
    if len(matches) != 1:
        raise ValueError(
            "research grid must contain exactly one frozen 3m/60m "
            "execution configuration"
        )
    return matches[0]


def _selected_status(
    selection: DiscoverySelection,
) -> tuple[str, tuple[str, ...]]:
    blockers = list(selection.discovery_blockers)
    holdout = _slice(selection.candidate, "HOLDOUT")
    if holdout.resolved_sessions < 20:
        blockers.append("HOLDOUT_SESSIONS_BELOW_20")
    if holdout.extension_signal_sessions < 3:
        blockers.append("HOLDOUT_EXTENSION_SIGNALS_BELOW_3")
    if holdout.comparison.cumulative_delta_bps <= 0:
        blockers.append("HOLDOUT_DELTA_NOT_POSITIVE")
    if (
        holdout.challenger.cumulative_without_best_3_bps
        <= holdout.baseline.cumulative_without_best_3_bps
    ):
        blockers.append("HOLDOUT_TAIL_ROBUSTNESS_FAILED")
    if not holdout.comparison.risk_guard_passed:
        blockers.append("HOLDOUT_DRAWDOWN_GUARD_FAILED")
    most_conservative = max(
        selection.candidate.cost_stress,
        key=lambda value: value.round_trip_cost_bps,
    )
    if most_conservative.cumulative_delta_bps <= 0:
        blockers.append("HOLDOUT_30BP_COST_STRESS_FAILED")
    if blockers:
        return "REJECTED", tuple(blockers)
    if (
        holdout.comparison.confidence_lower_bps is not None
        and holdout.comparison.confidence_lower_bps > 0
    ):
        return "HISTORICALLY_ROBUST", ()
    return "SHADOW_CANDIDATE", ()


def _slice_payload(value: OpeningExtensionSlice) -> dict[str, object]:
    return {
        "name": value.name,
        "start_date": (
            value.start_date.isoformat() if value.start_date else None
        ),
        "end_date": value.end_date.isoformat() if value.end_date else None,
        "resolved_sessions": value.resolved_sessions,
        "displaced_baseline_sessions": value.displaced_baseline_sessions,
        "extension_signal_sessions": value.extension_signal_sessions,
        "baseline": asdict(value.baseline),
        "challenger": asdict(value.challenger),
        "comparison": asdict(value.comparison),
    }


def _grid_summary(value: GridEvaluation) -> dict[str, object]:
    selection = _select_discovery_winner((value,))
    status, blockers = _selected_status(selection)
    return {
        "signal_minutes": value.signal_minutes,
        "holding_minutes": value.holding_minutes,
        "stop_loss_pct": value.report.stop_loss_pct,
        "opening_config_version": value.report.opening_config_version,
        "discovery_winner": selection.candidate.symbol,
        "diagnostic_status": status,
        "promotion_blockers": list(blockers),
        "discovery": _slice_payload(selection.discovery),
        "holdout": _slice_payload(
            _slice(selection.candidate, "HOLDOUT")
        ),
    }


def _selected_payload(
    selection: DiscoverySelection,
    *,
    status: str,
    blockers: Sequence[str],
) -> dict[str, object]:
    return {
        "status": status,
        "promotion_blockers": list(blockers),
        "symbol": selection.candidate.symbol,
        "signal_minutes": selection.grid.signal_minutes,
        "holding_minutes": selection.grid.holding_minutes,
        "stop_loss_pct": selection.grid.report.stop_loss_pct,
        "selected_using": "DISCOVERY_ONLY_FROZEN_EXECUTION_GRID",
        "discovery": _slice_payload(selection.discovery),
        "holdout": _slice_payload(_slice(selection.candidate, "HOLDOUT")),
        "cost_stress": [
            asdict(item) for item in selection.candidate.cost_stress
        ],
    }


def _execution_cohort_payload(
    grid: GridEvaluation,
) -> dict[str, object]:
    eligible: list[
        tuple[float, str, OpeningExtensionSlice]
    ] = []
    for candidate in grid.report.candidates:
        discovery = _slice(candidate, "DISCOVERY")
        delta = discovery.comparison.cumulative_delta_bps
        if (
            discovery.displaced_baseline_sessions
            < _EXECUTION_COHORT_MINIMUM_DISPLACEMENTS
            or delta <= 0
        ):
            continue
        eligible.append((delta, candidate.symbol, discovery))
    eligible.sort(key=lambda item: (-item[0], item[1]))
    selected = eligible[:_EXECUTION_COHORT_MAX_SYMBOLS]
    return {
        "selection_version": _EXECUTION_COHORT_SELECTION_VERSION,
        "selection_stage": "INDIVIDUAL_CANDIDATE_SHORTLIST",
        "selection_uses_holdout": False,
        "joint_subset_selection_required": True,
        "automatic_execution_cohort_allowed": False,
        "maximum_symbols": _EXECUTION_COHORT_MAX_SYMBOLS,
        "minimum_displacement_sessions": (
            _EXECUTION_COHORT_MINIMUM_DISPLACEMENTS
        ),
        "symbols": [symbol for _, symbol, _ in selected],
        "candidates": [
            {
                "symbol": symbol,
                "displaced_baseline_sessions": (
                    discovery.displaced_baseline_sessions
                ),
                "extension_signal_sessions": (
                    discovery.extension_signal_sessions
                ),
                "cumulative_delta_bps": delta,
                "mean_delta_bps": (
                    discovery.comparison.mean_delta_bps
                ),
            }
            for delta, symbol, discovery in selected
        ],
    }


def _joint_exploration_shortlist_payload(
    grid: GridEvaluation,
) -> dict[str, object]:
    """Keep sparse positive candidates for a later joint-subset search."""

    eligible: list[
        tuple[float, float, str, OpeningExtensionSlice]
    ] = []
    for candidate in grid.report.candidates:
        discovery = _slice(candidate, "DISCOVERY")
        delta = discovery.comparison.cumulative_delta_bps
        tail_delta = (
            discovery.challenger.cumulative_without_best_3_bps
            - discovery.baseline.cumulative_without_best_3_bps
        )
        if (
            discovery.resolved_sessions < 20
            or discovery.displaced_baseline_sessions
            < _JOINT_EXPLORATION_MINIMUM_DISPLACEMENTS
            or delta <= 0
            or tail_delta <= 0
            or not discovery.comparison.risk_guard_passed
        ):
            continue
        eligible.append((
            delta,
            tail_delta,
            candidate.symbol,
            discovery,
        ))
    eligible.sort(key=lambda item: (-item[0], -item[1], item[2]))
    selected = eligible[:_JOINT_EXPLORATION_MAX_SYMBOLS]
    return {
        "selection_version": _JOINT_EXPLORATION_SELECTION_VERSION,
        "selection_stage": "JOINT_EXPLORATION_CANDIDATE_SHORTLIST",
        "selection_uses_holdout": False,
        "joint_subset_selection_required": True,
        "diagnostic_only": True,
        "automatic_execution_cohort_allowed": False,
        "maximum_symbols": _JOINT_EXPLORATION_MAX_SYMBOLS,
        "minimum_displacement_sessions": (
            _JOINT_EXPLORATION_MINIMUM_DISPLACEMENTS
        ),
        "symbols": [symbol for _, _, symbol, _ in selected],
        "candidates": [
            {
                "symbol": symbol,
                "displaced_baseline_sessions": (
                    discovery.displaced_baseline_sessions
                ),
                "extension_signal_sessions": (
                    discovery.extension_signal_sessions
                ),
                "cumulative_delta_bps": delta,
                "tail_delta_bps": tail_delta,
                "mean_delta_bps": (
                    discovery.comparison.mean_delta_bps
                ),
                "risk_guard_passed": (
                    discovery.comparison.risk_guard_passed
                ),
            }
            for delta, tail_delta, symbol, discovery in selected
        ],
    }


def _parse_symbols(value: str, *, field_name: str) -> tuple[str, ...]:
    symbols = tuple(
        part.strip().upper()
        for part in value.split(",")
        if part.strip()
    )
    if not symbols:
        raise ValueError(f"{field_name} must contain at least one symbol")
    if len(symbols) != len(set(symbols)):
        raise ValueError(f"{field_name} must contain unique symbols")
    if any(not symbol.endswith(".US") for symbol in symbols):
        raise ValueError(f"{field_name} only supports .US symbols")
    return symbols


def _parse_integer_grid(
    value: str,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> tuple[int, ...]:
    try:
        values = tuple(
            int(part.strip())
            for part in value.split(",")
            if part.strip()
        )
    except ValueError as exc:
        raise ValueError(f"{field_name} must contain integers") from exc
    if not values:
        raise ValueError(f"{field_name} must contain at least one value")
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must contain unique values")
    if any(not minimum <= item <= maximum for item in values):
        raise ValueError(
            f"{field_name} values must be in [{minimum}, {maximum}]"
        )
    return values


def _parse_date(value: str, *, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Causally evaluate single-symbol opening-momentum universe "
            "extensions with a discovery/holdout split and cost stress."
        )
    )
    parser.add_argument("--extension-symbols", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument(
        "--baseline-symbols",
        help=(
            "optional frozen comma-separated baseline; defaults to current "
            "opening-execution eligible DB rows"
        ),
    )
    parser.add_argument(
        "--signal-minutes",
        default=",".join(str(value) for value in _DEFAULT_SIGNAL_MINUTES),
    )
    parser.add_argument(
        "--holding-minutes",
        default=",".join(str(value) for value in _DEFAULT_HOLDING_MINUTES),
    )
    parser.add_argument("--discovery-ratio", type=float, default=0.60)
    parser.add_argument("--minimum-data-coverage", type=float, default=0.95)
    parser.add_argument(
        "--stop-loss-pct",
        type=float,
        default=_FROZEN_SELECTION_STOP_LOSS_PCT,
        help="fixed intraday stop used by every research grid",
    )
    parser.add_argument("--cache-path")
    parser.add_argument("--output")
    parser.add_argument(
        "--full",
        action="store_true",
        help="print the full grid payload instead of the selected summary",
    )
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        start_date = _parse_date(args.start_date, field_name="start_date")
        end_date = _parse_date(args.end_date, field_name="end_date")
        if end_date < start_date:
            raise ValueError("end_date must not precede start_date")
        if not 0 < args.discovery_ratio < 1:
            raise ValueError("discovery_ratio must be in (0, 1)")
        if not 0 < args.minimum_data_coverage <= 1:
            raise ValueError("minimum_data_coverage must be in (0, 1]")
        if args.stop_loss_pct != _FROZEN_SELECTION_STOP_LOSS_PCT:
            raise ValueError(
                "formal selection requires the frozen 1% production stop"
            )
        extension_symbols = _parse_symbols(
            args.extension_symbols,
            field_name="extension_symbols",
        )
        signal_grid = _parse_integer_grid(
            args.signal_minutes,
            field_name="signal_minutes",
            minimum=1,
            maximum=120,
        )
        holding_grid = _parse_integer_grid(
            args.holding_minutes,
            field_name="holding_minutes",
            minimum=1,
            maximum=120,
        )
        if (
            _FROZEN_SELECTION_SIGNAL_MINUTES not in signal_grid
            or _FROZEN_SELECTION_HOLDING_MINUTES not in holding_grid
        ):
            raise ValueError(
                "parameter grids must include the frozen 3m/60m "
                "execution configuration"
            )
        baseline_symbols = (
            _parse_symbols(
                args.baseline_symbols,
                field_name="baseline_symbols",
            )
            if args.baseline_symbols
            else _load_current_baseline_symbols()
        )
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))

    overlap = set(baseline_symbols).intersection(extension_symbols)
    if overlap:
        parser.error(
            "extension symbols already exist in baseline: "
            + ", ".join(sorted(overlap))
        )
    execution_delay_minutes = 1
    maximum_offset = max(signal_grid) + execution_delay_minutes + max(
        holding_grid
    )
    cache_path = Path(args.cache_path) if args.cache_path else Path(
        "data/research/"
        f"opening-extension-{start_date.isoformat()}-{end_date.isoformat()}"
        "-ohlc-v3.json.gz"
    )
    try:
        bars_by_symbol = _load_cache(
            cache_path,
            start_date=start_date,
            end_date=end_date,
            retained_minutes_after_open=maximum_offset,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"failed to load research cache: {exc}")

    all_symbols = tuple(dict.fromkeys(
        (*baseline_symbols, *extension_symbols)
    ))
    missing_symbols = tuple(
        symbol
        for symbol in all_symbols
        if not bars_by_symbol.get(symbol)
    )
    broker: BrokerGateway | None = None
    if missing_symbols:
        try:
            _configure_longport_environment()
            broker = BrokerGateway()
            for index, symbol in enumerate(missing_symbols, start=1):
                print(
                    f"minute history {index}/{len(missing_symbols)} {symbol}",
                    file=sys.stderr,
                    flush=True,
                )
                bars_by_symbol[symbol] = _fetch_symbol_bars(
                    broker,
                    symbol,
                    start_date=start_date,
                    end_date=end_date,
                    retained_minutes_after_open=maximum_offset,
                )
                if index % 5 == 0 or index == len(missing_symbols):
                    _save_cache(
                        cache_path,
                        bars_by_symbol,
                        start_date=start_date,
                        end_date=end_date,
                        retained_minutes_after_open=maximum_offset,
                    )
        finally:
            if broker is not None:
                broker.close()

    config_template = OpeningMomentumConfig()
    session_dates = _baseline_session_dates(
        bars_by_symbol,
        baseline_symbols=baseline_symbols,
        minimum_universe_size=config_template.minimum_universe_size,
        minimum_data_coverage=args.minimum_data_coverage,
    )
    if len(session_dates) < 2:
        parser.error("fewer than two baseline-complete sessions were found")

    grids: list[GridEvaluation] = []
    for signal_minutes in signal_grid:
        for holding_minutes in holding_grid:
            print(
                f"evaluate signal={signal_minutes} hold={holding_minutes}",
                file=sys.stderr,
                flush=True,
            )
            config = OpeningMomentumConfig(
                signal_minutes=signal_minutes,
                execution_delay_minutes=execution_delay_minutes,
                holding_minutes=holding_minutes,
                minimum_universe_size=8,
                minimum_market_return_bps=-50.0,
                minimum_candidate_return_bps=50.0,
                minimum_excess_return_bps=25.0,
                one_side_fee_rate=0.0005,
                one_side_slippage_bps=2.0,
                stop_loss_pct=args.stop_loss_pct,
            )
            sessions = _build_sessions(
                bars_by_symbol,
                symbols=all_symbols,
                session_dates=session_dates,
                signal_minutes=signal_minutes,
                execution_delay_minutes=execution_delay_minutes,
                holding_minutes=holding_minutes,
                stop_loss_pct=args.stop_loss_pct,
            )
            report = evaluate_opening_extension_candidates(
                sessions,
                baseline_symbols=baseline_symbols,
                extension_symbols=extension_symbols,
                config=config,
                discovery_ratio=args.discovery_ratio,
                minimum_data_coverage=args.minimum_data_coverage,
                round_trip_cost_scenarios_bps=_DEFAULT_COST_STRESS_BPS,
            )
            grids.append(GridEvaluation(
                signal_minutes=signal_minutes,
                holding_minutes=holding_minutes,
                report=report,
            ))

    try:
        selection_grid = _frozen_selection_grid(grids)
    except ValueError as exc:
        parser.error(str(exc))
    selection = _select_discovery_winner((selection_grid,))
    status, blockers = _selected_status(selection)
    generated = datetime.now(timezone.utc)
    generated_at = generated.isoformat()
    selected_payload = _selected_payload(
        selection,
        status=status,
        blockers=blockers,
    )
    execution_cohort_payload = _execution_cohort_payload(
        selection_grid
    )
    joint_exploration_shortlist_payload = (
        _joint_exploration_shortlist_payload(selection_grid)
    )
    full_payload: dict[str, object] = {
        "cli_version": OPENING_EXTENSION_CLI_VERSION,
        "algorithm_version": OPENING_EXTENSION_RESEARCH_VERSION,
        "generated_at": generated_at,
        "data_scope": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "period": "MIN_1",
            "adjustment": "NO_ADJUST",
            "session_count": len(session_dates),
            "cache_path": str(cache_path),
            "bar_counts": {
                symbol: len(bars_by_symbol.get(symbol, ()))
                for symbol in all_symbols
            },
        },
        "research_design": {
            "baseline_source": (
                "CLI_FROZEN_SYMBOLS"
                if args.baseline_symbols
                else "CURRENT_OPENING_EXECUTION_ELIGIBLE_STRATEGY_V2_CONFIG"
            ),
            "baseline_symbols": list(baseline_symbols),
            "extension_symbols": list(extension_symbols),
            "signal_minutes_grid": list(signal_grid),
            "holding_minutes_grid": list(holding_grid),
            "execution_delay_minutes": execution_delay_minutes,
            "stop_loss_pct": args.stop_loss_pct,
            "discovery_ratio": args.discovery_ratio,
            "minimum_data_coverage": args.minimum_data_coverage,
            "round_trip_cost_scenarios_bps": list(
                _DEFAULT_COST_STRESS_BPS
            ),
            "selection_uses_holdout": False,
            "selection_grid": {
                "signal_minutes": _FROZEN_SELECTION_SIGNAL_MINUTES,
                "holding_minutes": _FROZEN_SELECTION_HOLDING_MINUTES,
                "stop_loss_pct": _FROZEN_SELECTION_STOP_LOSS_PCT,
            },
            "sensitivity_grid_selection_allowed": False,
            "grid_search_bias": True,
            "survivorship_bias": "CURRENT_BASELINE_SYMBOLS",
        },
        "selected": selected_payload,
        "execution_cohort": execution_cohort_payload,
        "joint_exploration_shortlist": (
            joint_exploration_shortlist_payload
        ),
        "automatic_promotion_allowed": False,
        "grid": [
            {
                "signal_minutes": value.signal_minutes,
                "holding_minutes": value.holding_minutes,
                "stop_loss_pct": value.report.stop_loss_pct,
                "report": value.report.to_dict(),
            }
            for value in grids
        ],
    }
    output_path = Path(args.output) if args.output else Path(
        "data/research/"
        "opening-extension-report-"
        f"{generated.strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output_path.with_name(f".{output_path.name}.tmp")
    temporary_output.write_text(
        json.dumps(
            full_payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_output, output_path)

    compact_payload = {
        "cli_version": OPENING_EXTENSION_CLI_VERSION,
        "generated_at": generated_at,
        "data_scope": full_payload["data_scope"],
        "selected": selected_payload,
        "execution_cohort": execution_cohort_payload,
        "joint_exploration_shortlist": (
            joint_exploration_shortlist_payload
        ),
        "automatic_promotion_allowed": False,
        "full_report_path": str(output_path),
        "grid": [_grid_summary(value) for value in grids],
    }
    print(json.dumps(
        full_payload if args.full else compact_payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
