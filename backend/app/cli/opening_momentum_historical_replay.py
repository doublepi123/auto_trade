"""Registered RETROSPECTIVE VALIDATION replay CLI for the frozen forward rule
``INDEX_CATALOG_STOCKS_IN_PLAY_ORB_TOP10_CHALLENGER`` (config 44e3c377...def9).

Governance contract: ``app/domain/OPENING_MOMENTUM_HISTORICAL_REPLAY.md``.
This is NOT forward certification: the window 2023-09-01..2026-04-30 was never
used to select the rule, results can never add trades to the forward cohort,
and the verdict vocabulary (CORROBORATES / DOES_NOT_CORROBORATE /
INCONCLUSIVE) is disjoint from the forward PASS vocabulary.

Safety rules baked into the code:
- ``fetch`` uses a QuoteContext ONLY (never TradeContext, never BrokerGateway,
  which also builds a TradeContext), one worker, default 0.5 req/s, and a hard
  pause window 13:00-22:00 UTC Mon-Fri.
- provider errors 301607 (quota) and 301604/permission cause an immediate
  GLOBAL stop with no retry; 301600/invalid-symbol is a persisted per-symbol
  permanent failure; transient errors get bounded retries.
- no DB access, no ``import_opening_activity``, no order path anywhere.

The replay reuses the frozen implementations by CALLING them: the domain
evaluator ``evaluate_stocks_in_play_opening_range_breakout``, the selector ADV
helper ``_dollar_volume``, and the static service helpers
``_opening_range_stop_loss_pct`` / ``_minute_path_complete`` / ``_exit_outcome``
/ ``_coerce_candles`` / ``_signal_turnover`` plus ``shadow_round_trip_return_bps``.
The glue below only supplies what the live ``_observe_variants`` provides for
this ONE variant: signal bars, activity ratios, and range high/low.  It never
calls ``tick``, ``_observe_variants`` or ``_close_if_due``.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from zoneinfo import ZoneInfo

from app.config import Settings, settings
from app.core import holiday_calendar
from app.core.holiday_calendar import is_half_day, is_market_closed
from app.core.market_calendar import get_session
from app.domain.opening_momentum import (
    OpeningMomentumConfig,
    OpeningMomentumObservation,
    evaluate_stocks_in_play_opening_range_breakout,
    shadow_round_trip_return_bps,
)
from app.domain.opening_momentum_policy import opening_execution_config
from app.domain.opening_momentum_universe import (
    opening_momentum_evidence_config_version,
)
from app.domain.universe_selection import (
    CATALOG_SOURCE_VERSION,
    UNIVERSE_ALGORITHM_VERSION,
)
from app.domain.universe_selection.catalog import (
    HISTORICAL_INDEX_CANDIDATE_CATALOG,
    INDEX_CANDIDATE_CATALOG,
)
from app.domain.universe_selection.membership_history import (
    INDEX_MEMBERSHIP_HISTORY,
)
from app.domain.universe_selection.selector import _dollar_volume
from app.services.opening_momentum_shadow_service import (
    OpeningMomentumShadowService,
    _EARLY_BROAD_MINIMUM_COVERAGE,
    _INDEX_CATALOG_STOCKS_IN_PLAY_ORB_VERSION_SUFFIX,
    _OPENING_RANGE_STOP_MAX_PCT,
)


# ---------------------------------------------------------------- frozen plan

ANALYSIS_ID = "opening-momentum-top10-pit-historical-v2"
REPLAY_CLI_VERSION = "opening-momentum-top10-pit-historical-replay-cli-v1"
WINDOW_START = date(2023, 9, 1)
WINDOW_END = date(2026, 4, 30)
WARMUP_SESSIONS = 21
ADV_LOOKBACK_BARS = 20
MIN_COMPLETED_BARS = 21
OPENING_ACTIVITY_TOP_N = 10
STOP_LOSS_CAP_PCT = float(_OPENING_RANGE_STOP_MAX_PCT)
HOLDING_MINUTES = 60
SIGNAL_MINUTES = 5
EXECUTION_DELAY_MINUTES = 1
ENTRY_OFFSET = SIGNAL_MINUTES + EXECUTION_DELAY_MINUTES  # 09:36 bar
EXIT_OFFSET = ENTRY_OFFSET + HOLDING_MINUTES  # 10:36 bar
MINUTE_RETAINED_LAST_OFFSET = 70  # retain 09:30..10:40 ET inclusive
STRESS_COST_BPS = 20.0
MIN_TRADES = 125
MIN_WEEKS = 26
GATE_SESSION_INPUT_COVERAGE = 0.95
GATE_MEMBER_DATA_MISSING_MAX_SHARE = 0.02
GATE_STILL_LISTED_MISSING_MAX_MEMBER_DAYS = 0
VERDICT_CORROBORATES = "CORROBORATES"
VERDICT_DOES_NOT_CORROBORATE = "DOES_NOT_CORROBORATE"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"

FROZEN_CONFIG_VERSION = (
    "44e3c377d37ca27f921eef983a985f8e45667c283d72ddb056aafbe0d026def9"
)

_GOOGLE_CLASS_A = "GOOGL.US"
_GOOGLE_CLASS_C = "GOOG.US"

# Decision 1: the provider trading_days API is infeasible for the
# registered window (interval must be under one month, only the most
# recent year supported; the first real fetch failed with
# ``code=301600 too many query days`` before any market data was
# fetched).  The session list is now derived returns-blind from the
# benchmark ETFs' daily bars.
BENCHMARK_ETFS: tuple[str, str] = ("QQQ.US", "DIA.US")

# Conventional NYSE early-close dates in 2023 (before the local half-day
# table's coverage starts 2024-01-01).  They are recorded as UNVERIFIED:
# the local calendar cannot confirm them.
_CONVENTIONAL_2023_EARLY_CLOSES: tuple[date, ...] = (
    date(2023, 7, 3),
    date(2023, 11, 24),
    date(2023, 12, 22),
)

# Connection / quota safety.
DEFAULT_REQUESTS_PER_SECOND = 0.5
DEFAULT_MAX_TRANSIENT_RETRIES = 3
PAGE_SIZE = 1000

#: Replay period names → LongPort SDK ``Period`` enum member names.
_SDK_PERIOD_NAMES: dict[str, str] = {"DAY": "Day", "MIN_1": "Min_1"}
QUOTA_ERROR_MARKERS = ("code=301607",)
PERMISSION_ERROR_MARKERS = ("code=301604", "no permission")
# A 301600 answer is a per-symbol permanent failure ONLY when it names the
# symbol; the same code with "too many query days" is a request-shape
# error about the query, never about the symbol.
INVALID_SYMBOL_MARKERS = ("invalid symbol", "unknown symbol")
REQUEST_SHAPE_MARKERS = (
    "code=301600",
    "too many query days",
)
PAUSE_WINDOW_START_UTC = time(13, 0)
PAUSE_WINDOW_END_UTC = time(22, 0)

_CACHE_DIR_NAME = Path("data/research/opening_momentum_historical_replay_v1")
_PLAN_DOC_RELATIVE_PATH = Path("app/domain/OPENING_MOMENTUM_HISTORICAL_REPLAY.md")
_MINUTE_RETAINED_MINUTES = MINUTE_RETAINED_LAST_OFFSET + 1
_MARKET_SESSION = get_session("US")
# The optional provider trading_days cross-check: the SDK supports only the
# most recent year and intervals under one month, so it is chunked to 28
# days and restricted to the recent year; its failure never aborts.
_TRADING_DAYS_CHUNK_DAYS = 28
_TRADING_DAYS_CROSS_CHECK_MAX_CHUNKS = 16
_BAR_DURATION = timedelta(minutes=1)


class HistoricalReplayError(RuntimeError):
    """Refusal or aborted replay operation (fail-closed)."""


class ErrorClass(str, Enum):
    GLOBAL_STOP_QUOTA = "GLOBAL_STOP_QUOTA"
    GLOBAL_STOP_PERMISSION = "GLOBAL_STOP_PERMISSION"
    PERMANENT_SYMBOL = "PERMANENT_SYMBOL"
    REQUEST_SHAPE = "REQUEST_SHAPE"
    TRANSIENT = "TRANSIENT"


# One-sided 95% Student-t critical values, df = 1..150 (index df-1).
# Pure Python (no SciPy dependency), mirroring the frozen table approach in
# ``app/domain/strategy_v2/clustered_returns.py``.  Anchors cross-validated
# against the standard one-sided t table in the behaviour tests.
ONE_SIDED_T95_BY_DF: tuple[float, ...] = (
    6.313751515, 2.919985580, 2.353363435, 2.131846786, 2.015048373,
    1.943180281, 1.894578605, 1.859548038, 1.833112933, 1.812461123,
    1.795884819, 1.782287556, 1.770933396, 1.761310136, 1.753050356,
    1.745883676, 1.739606726, 1.734063607, 1.729132812, 1.724718243,
    1.720742903, 1.717144374, 1.713871528, 1.710882080, 1.708140761,
    1.705617920, 1.703288446, 1.701130934, 1.699127027, 1.697260887,
    1.695518783, 1.693888748, 1.692360309, 1.690924255, 1.689572458,
    1.688297714, 1.687093620, 1.685954460, 1.684875122, 1.683851013,
    1.682878002, 1.681952357, 1.681070703, 1.680229977, 1.679427393,
    1.678660414, 1.677926722, 1.677224196, 1.676550893, 1.675905025,
    1.675284950, 1.674689154, 1.674116237, 1.673564906, 1.673033965,
    1.672522303, 1.672028888, 1.671552762, 1.671093032, 1.670648865,
    1.670219484, 1.669804163, 1.669402222, 1.669013025, 1.668635976,
    1.668270514, 1.667916114, 1.667572281, 1.667238549, 1.666914479,
    1.666599658, 1.666293696, 1.665996224, 1.665706893, 1.665425373,
    1.665151353, 1.664884537, 1.664624645, 1.664371409, 1.664124579,
    1.663883913, 1.663649184, 1.663420175, 1.663196679, 1.662978500,
    1.662765449, 1.662557349, 1.662354029, 1.662155326, 1.661961084,
    1.661771155, 1.661585397, 1.661403674, 1.661225855, 1.661051817,
    1.660881440, 1.660714610, 1.660551217, 1.660391156, 1.660234326,
    1.660080630, 1.659929976, 1.659782273, 1.659637437, 1.659495383,
    1.659356034, 1.659219312, 1.659085144, 1.658953458, 1.658824187,
    1.658697265, 1.658572629, 1.658450216, 1.658329969, 1.658211830,
    1.658095744, 1.657981659, 1.657869522, 1.657759285, 1.657650899,
    1.657544319, 1.657439499, 1.657336397, 1.657234970, 1.657135178,
    1.657036982, 1.656940344, 1.656845226, 1.656751594, 1.656659413,
    1.656568649, 1.656479270, 1.656391244, 1.656304542, 1.656219133,
    1.656134988, 1.656052080, 1.655970382, 1.655889868, 1.655810511,
    1.655732287, 1.655655173, 1.655579143, 1.655504177, 1.655430251,
    1.655357345, 1.655285437, 1.655214506, 1.655144534, 1.655075500,
)


def one_sided_t95(degrees_of_freedom: int) -> float:
    if not 1 <= degrees_of_freedom <= len(ONE_SIDED_T95_BY_DF):
        raise HistoricalReplayError(
            "week cluster count exceeds the fixed one-sided t table "
            f"(supported df: 1..{len(ONE_SIDED_T95_BY_DF)})"
        )
    return ONE_SIDED_T95_BY_DF[degrees_of_freedom - 1]


# ---------------------------------------------------------- frozen rule reuse


def frozen_decision_config() -> OpeningMomentumConfig:
    """The exact decision config of the registered TOP10 variant."""

    return replace(
        opening_execution_config(OpeningMomentumConfig()),
        signal_minutes=SIGNAL_MINUTES,
        holding_minutes=HOLDING_MINUTES,
        minimum_market_return_bps=-10_000.0,
        minimum_candidate_return_bps=0.0,
        minimum_excess_return_bps=0.0,
        one_side_fee_rate=0.0005,
        one_side_slippage_bps=10.0,
        stop_loss_pct=STOP_LOSS_CAP_PCT,
    )


def _spec_version(top_n: int) -> str:
    return (
        "forward-only-5m-orb-stocks-in-play-"
        f"top{top_n}-"
        f"{_INDEX_CATALOG_STOCKS_IN_PLAY_ORB_VERSION_SUFFIX}"
    )


def frozen_config_version() -> str:
    config = frozen_decision_config()
    inner = f"{config.version_hash()}:{_spec_version(OPENING_ACTIVITY_TOP_N)}:{OPENING_ACTIVITY_TOP_N}"
    return opening_momentum_evidence_config_version(
        inner,
        universe_algorithm_version=UNIVERSE_ALGORITHM_VERSION,
        catalog_source_version=CATALOG_SOURCE_VERSION,
    )


# ------------------------------------------------------------ PIT universe


def company_dedupe(symbols: Iterable[str]) -> tuple[str, ...]:
    """Company-level dedupe: keep GOOGL and drop GOOG (catalog.py L43-47)."""

    unique = tuple(dict.fromkeys(symbols))
    if _GOOGLE_CLASS_A in unique:
        return tuple(
            symbol for symbol in unique if symbol != _GOOGLE_CLASS_C
        )
    return unique


def pit_universe_for_session(session_date: date) -> tuple[str, ...]:
    """PIT members of NASDAQ_100 ∪ DJIA on ``session_date`` (half-open)."""

    members = tuple(
        candidate.symbol
        for candidate in (
            *INDEX_CANDIDATE_CATALOG,
            *HISTORICAL_INDEX_CANDIDATE_CATALOG,
        )
        if INDEX_MEMBERSHIP_HISTORY.is_active(candidate, session_date)
    )
    return company_dedupe(members)


def _weekday_sessions(begin: date, end: date) -> list[date]:
    sessions: list[date] = []
    current = begin
    while current <= end:
        if current.weekday() < 5:
            sessions.append(current)
        current += timedelta(days=1)
    return sessions


def _planning_sessions(begin: date, end: date) -> list[date]:
    """Offline approximation: weekdays minus locally-known US holidays."""

    return [
        session_date
        for session_date in _weekday_sessions(begin, end)
        if not is_market_closed("US", session_date)
    ]


def _warmup_start(sessions_before_first: Sequence[date]) -> date | None:
    if len(sessions_before_first) < WARMUP_SESSIONS:
        return None
    return sessions_before_first[-WARMUP_SESSIONS]


def _session_open_utc(session_date: date) -> datetime:
    return datetime.combine(
        session_date,
        _MARKET_SESSION.rth_open,
        tzinfo=_MARKET_SESSION.timezone,
    ).astimezone(timezone.utc)


def _minute_offset(timestamp: datetime, session_open: datetime) -> int | None:
    offset = (timestamp - session_open).total_seconds() // 60
    if offset % 1 != 0:
        return None
    return int(offset)


# ---------------------------------------------------------------- ADV rebuild


class DailyBarLike(Protocol):
    @property
    def timestamp(self) -> datetime: ...

    @property
    def open(self) -> float: ...

    @property
    def high(self) -> float: ...

    @property
    def low(self) -> float: ...

    @property
    def close(self) -> float: ...

    @property
    def volume(self) -> float: ...

    @property
    def turnover(self) -> float: ...


def _bar_is_valid(bar: DailyBarLike) -> bool:
    values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
    if any(not math.isfinite(float(value)) for value in values):
        return False
    if min(bar.open, bar.high, bar.low, bar.close) <= 0 or bar.volume < 0:
        return False
    if bar.high < max(bar.open, bar.close, bar.low):
        return False
    if bar.low > min(bar.open, bar.close, bar.high):
        return False
    return True


@dataclass(frozen=True)
class _DailyBarRow:
    """Concrete daily bar satisfying both ``DailyBarLike`` and the frozen
    selector's ``DailyBar`` protocols (non-optional numeric fields)."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


def _daily_bar_rows(
    candles: Sequence[_CandleView],
) -> list[_DailyBarRow]:
    return [
        _DailyBarRow(
            timestamp=candle.timestamp,
            open=float(candle.open),
            high=float(candle.high),
            low=float(candle.low),
            close=float(candle.close),
            volume=(
                float(candle.volume)
                if candle.volume is not None
                else 0.0
            ),
            turnover=(
                float(candle.turnover)
                if candle.turnover is not None
                else 0.0
            ),
        )
        for candle in candles
    ]


def rebuild_session_adv(
    daily_bars: Sequence[DailyBarLike],
    *,
    as_of: date,
) -> float | None:
    """Rebuild the selector's ``avg_dollar_volume`` from bars before ``as_of``.

    Mirrors ``selector._candidate_metrics``: requires >= 21 valid completed
    bars strictly before ``as_of`` and averages the frozen ``_dollar_volume``
    over the last 20 of them.
    """

    completed = sorted(
        (
            bar
            for bar in daily_bars
            if _MARKET_SESSION.local(bar.timestamp).date() < as_of
            and _bar_is_valid(bar)
        ),
        key=lambda bar: bar.timestamp,
    )
    if len(completed) < MIN_COMPLETED_BARS:
        return None
    dollar_volumes = [
        _dollar_volume(bar) for bar in completed[-ADV_LOOKBACK_BARS:]
    ]
    if any(
        not math.isfinite(value) or value <= 0 for value in dollar_volumes
    ):
        return None
    adv = math.fsum(dollar_volumes) / len(dollar_volumes)
    if not math.isfinite(adv) or adv <= 0:
        return None
    return adv


# ------------------------------------------------------ session replay glue


@dataclass(frozen=True)
class _CandleView:
    """Attribute carrier accepted by the frozen ``_coerce_candles``."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    turnover: float | None


@dataclass(frozen=True)
class MemberObservation:
    symbol: str
    observation: OpeningMomentumObservation
    opening_range_low: float
    opening_range_high: float
    activity_ratio: float | None


@dataclass(frozen=True)
class SessionDecision:
    """The parity surface against the live shadow-service run row."""

    status: Literal["OPEN", "SKIPPED"]
    reason: str
    candidate_symbol: str | None
    entry_price: float | None
    stop_loss_pct: float | None
    universe_size: int
    observed_symbols: int
    ratio_symbols: int
    excluded: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SettledExit:
    exit_price: float
    exit_reason: str
    gross_return_bps: float
    net_return_bps: float


def _coerced_candles(
    rows: Iterable[tuple[datetime, float, float, float, float, float | None, float | None]],
) -> dict[datetime, Any]:
    views = [
        _CandleView(*row)
        for row in rows
    ]
    return {
        candle.timestamp: candle
        for candle in OpeningMomentumShadowService._coerce_candles(views)
    }


def build_session_observation(
    bars_by_timestamp: dict[datetime, Any],
    *,
    session_open: datetime,
    symbol: str,
    adv: float | None,
) -> MemberObservation | None:
    """Mirror the one-variant observation glue of ``_observe_variants``."""

    expected = [
        session_open + timedelta(minutes=offset)
        for offset in range(SIGNAL_MINUTES)
    ]
    if any(timestamp not in bars_by_timestamp for timestamp in expected):
        return None
    signal_at = session_open + timedelta(minutes=SIGNAL_MINUTES)
    entry_at = session_open + timedelta(minutes=ENTRY_OFFSET)
    signal_bar = bars_by_timestamp.get(signal_at)
    if signal_bar is None:
        return None
    signal_candles = [bars_by_timestamp[timestamp] for timestamp in expected]
    entry_bar = bars_by_timestamp.get(entry_at)
    observation = OpeningMomentumObservation(
        symbol=symbol,
        session_open=bars_by_timestamp[session_open].open,
        signal_close=signal_bar.close,
        entry_open=entry_bar.open if entry_bar is not None else None,
    )
    activity_ratio: float | None = None
    opening_turnover = OpeningMomentumShadowService._signal_turnover(
        signal_candles
    )
    if (
        opening_turnover is not None
        and adv is not None
        and adv > 0
    ):
        activity_ratio = opening_turnover / adv
    return MemberObservation(
        symbol=symbol,
        observation=observation,
        opening_range_low=min(candle.low for candle in signal_candles),
        opening_range_high=max(candle.high for candle in signal_candles),
        activity_ratio=activity_ratio,
    )


def evaluate_session_decision(
    *,
    universe: Sequence[str],
    minute_bars_by_symbol: dict[str, dict[datetime, Any]],
    adv_by_symbol: dict[str, float],
    session_open: datetime,
) -> SessionDecision:
    """Reproduce the service's TOP10 ORB decision for one session."""

    config = frozen_decision_config()
    observations: list[OpeningMomentumObservation] = []
    members: dict[str, MemberObservation] = {}
    excluded: dict[str, str] = {}
    for symbol in universe:
        bars = minute_bars_by_symbol.get(symbol)
        if not bars:
            excluded[symbol] = "NO_BARS"
            continue
        member = build_session_observation(
            bars,
            session_open=session_open,
            symbol=symbol,
            adv=adv_by_symbol.get(symbol),
        )
        if member is None:
            excluded[symbol] = "SIGNAL_BARS_MISSING"
            continue
        members[symbol] = member
        observations.append(member.observation)

    ratio_by_symbol = {
        symbol: member.activity_ratio
        for symbol, member in members.items()
        if member.activity_ratio is not None
    }
    required_observations = max(
        config.minimum_universe_size,
        math.ceil(len(universe) * _EARLY_BROAD_MINIMUM_COVERAGE),
    )
    observation_symbols = {item.symbol for item in observations}
    opening_activity_data_complete = observation_symbols.issubset(
        ratio_by_symbol
    )
    data_complete = (
        bool(universe)
        and len(observations) >= required_observations
        and opening_activity_data_complete
    )
    for symbol in observation_symbols:
        if symbol not in ratio_by_symbol:
            excluded[symbol] = "OPENING_ACTIVITY_DATA_MISSING"

    decision = evaluate_stocks_in_play_opening_range_breakout(
        observations,
        opening_range_high_by_symbol={
            symbol: member.opening_range_high
            for symbol, member in members.items()
        },
        opening_activity_ratio_by_symbol=ratio_by_symbol,
        maximum_stocks_in_play=OPENING_ACTIVITY_TOP_N,
        minimum_opening_activity_ratio=None,
        candidate_ranking="BREAKOUT_DEPTH",
        minimum_breakout_depth_bps=None,
        config=config,
    )

    stop_loss_pct: float | None = None
    if (
        decision.action == "ENTER_LONG"
        and decision.candidate_symbol is not None
        and decision.entry_price is not None
    ):
        stop_loss_pct = OpeningMomentumShadowService._opening_range_stop_loss_pct(
            opening_range_low=(
                members[decision.candidate_symbol].opening_range_low
            ),
            entry_price=decision.entry_price,
            maximum_stop_loss_pct=STOP_LOSS_CAP_PCT,
        )
    opening_range_stop_invalid = (
        decision.action == "ENTER_LONG" and stop_loss_pct is None
    )
    status: Literal["OPEN", "SKIPPED"] = (
        "OPEN"
        if (
            decision.action == "ENTER_LONG"
            and data_complete
            and not opening_range_stop_invalid
        )
        else "SKIPPED"
    )
    if not data_complete:
        reason = "DATA_INCOMPLETE"
    elif opening_range_stop_invalid:
        reason = "OPENING_RANGE_STOP_INVALID"
    else:
        reason = decision.reason
    return SessionDecision(
        status=status,
        reason=reason,
        candidate_symbol=decision.candidate_symbol,
        entry_price=(
            decision.entry_price if status == "OPEN" else None
        ),
        stop_loss_pct=stop_loss_pct if status == "OPEN" else None,
        universe_size=len(universe),
        observed_symbols=len(observations),
        ratio_symbols=len(ratio_by_symbol),
        excluded=excluded,
    )


def settle_session_exit(
    bars_by_timestamp: dict[datetime, Any],
    *,
    session_open: datetime,
    entry_price: float,
    stop_loss_pct: float,
) -> SettledExit | None:
    """Enforce a PROVEN complete stop path before the frozen ``_exit_outcome``.

    Returns ``None`` (unresolved, never a guess) when the minute path between
    entry and the fixed-hold exit bar is incomplete.  The live service has a
    known defect here (backfill without re-check); this replay deliberately
    does NOT reproduce it - see the fidelity table in the replay doc.
    """

    entry_at = session_open + timedelta(minutes=ENTRY_OFFSET)
    exit_due_at = session_open + timedelta(minutes=EXIT_OFFSET)
    if exit_due_at not in bars_by_timestamp:
        return None
    if not OpeningMomentumShadowService._minute_path_complete(
        bars_by_timestamp,
        start_at=entry_at,
        end_at=exit_due_at,
    ):
        return None
    outcome = OpeningMomentumShadowService._exit_outcome(
        tuple(bars_by_timestamp.values()),
        entry_at=entry_at,
        exit_due_at=exit_due_at,
        entry_price=entry_price,
        stop_loss_pct=stop_loss_pct,
    )
    gross_return_bps, net_return_bps = shadow_round_trip_return_bps(
        entry_price=entry_price,
        exit_price=outcome.price,
        config=frozen_decision_config(),
    )
    return SettledExit(
        exit_price=outcome.price,
        exit_reason=outcome.reason,
        gross_return_bps=gross_return_bps,
        net_return_bps=net_return_bps,
    )


# --------------------------------------------------------------- statistics


@dataclass(frozen=True)
class WeekClusteredStat:
    n: int
    weeks: int
    mean_bps: float | None
    standard_error_bps: float | None
    t_critical: float | None
    lower_30: float | None
    upper_30: float | None
    lower_50: float | None
    upper_50: float | None


def week_clustered_statistic(
    observations: Sequence[tuple[date, float]],
) -> WeekClusteredStat:
    """Trade-weighted mean net bps with week-clustered SE (forward doc 5.4).

    ``S_w = sum_week(r_i - mean)``; ``SE = sqrt(W/(W-1) * sum S_w^2 / n^2)``;
    ``L = mean - t(0.95, W-1) * SE`` (one-sided 95%, df = W-1); the 50 bps
    stress column shifts both bounds by ``STRESS_COST_BPS``.
    """

    values = [float(value) for _, value in observations]
    if not values:
        return WeekClusteredStat(
            n=0,
            weeks=0,
            mean_bps=None,
            standard_error_bps=None,
            t_critical=None,
            lower_30=None,
            upper_30=None,
            lower_50=None,
            upper_50=None,
        )
    by_week: dict[tuple[int, int], list[float]] = {}
    for session_date, value in observations:
        iso = session_date.isocalendar()
        by_week.setdefault((iso[0], iso[1]), []).append(float(value))
    n = len(values)
    mean = math.fsum(values) / n
    weeks = len(by_week)
    if weeks < 2:
        return WeekClusteredStat(
            n=n,
            weeks=weeks,
            mean_bps=mean,
            standard_error_bps=None,
            t_critical=None,
            lower_30=None,
            upper_30=None,
            lower_50=None,
            upper_50=None,
        )
    cluster_score_squares = math.fsum(
        math.fsum(value - mean for value in week_values) ** 2
        for week_values in by_week.values()
    )
    variance = weeks / (weeks - 1) * cluster_score_squares / (n * n)
    if variance <= 0:
        return WeekClusteredStat(
            n=n,
            weeks=weeks,
            mean_bps=mean,
            standard_error_bps=None,
            t_critical=None,
            lower_30=None,
            upper_30=None,
            lower_50=None,
            upper_50=None,
        )
    standard_error = math.sqrt(variance)
    critical = one_sided_t95(weeks - 1)
    lower_30 = mean - critical * standard_error
    upper_30 = mean + critical * standard_error
    return WeekClusteredStat(
        n=n,
        weeks=weeks,
        mean_bps=mean,
        standard_error_bps=standard_error,
        t_critical=critical,
        lower_30=lower_30,
        upper_30=upper_30,
        lower_50=lower_30 - STRESS_COST_BPS,
        upper_50=upper_30 - STRESS_COST_BPS,
    )


# ------------------------------------------------------------------- gates


@dataclass(frozen=True)
class IntegrityGates:
    expected_sessions: int
    auditable_sessions: int
    member_days_total: int
    member_days_missing_data: int
    still_listed_missing_member_days: int
    unresolved_exit_sessions: int

    @property
    def session_input_coverage(self) -> float:
        if self.member_days_total < 0:
            return 0.0
        if self.expected_sessions <= 0:
            return 0.0
        return self.auditable_sessions / self.expected_sessions

    @property
    def member_day_missing_share(self) -> float:
        if self.member_days_total <= 0:
            return 0.0
        return self.member_days_missing_data / self.member_days_total

    @property
    def passed(self) -> bool:
        return (
            self.session_input_coverage >= GATE_SESSION_INPUT_COVERAGE
            and (
                self.member_day_missing_share
                <= GATE_MEMBER_DATA_MISSING_MAX_SHARE
            )
            and (
                self.still_listed_missing_member_days
                <= GATE_STILL_LISTED_MISSING_MAX_MEMBER_DAYS
            )
            and self.unresolved_exit_sessions == 0
        )

    def failures(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.session_input_coverage < GATE_SESSION_INPUT_COVERAGE:
            reasons.append("SESSION_INPUT_COVERAGE_BELOW_0.95")
        if (
            self.member_day_missing_share
            > GATE_MEMBER_DATA_MISSING_MAX_SHARE
        ):
            reasons.append("MEMBER_DAY_MISSING_SHARE_ABOVE_0.02")
        if (
            self.still_listed_missing_member_days
            > GATE_STILL_LISTED_MISSING_MAX_MEMBER_DAYS
        ):
            reasons.append("STILL_LISTED_MEMBER_DAY_MISSING")
        if self.unresolved_exit_sessions != 0:
            reasons.append("UNRESOLVED_SELECTED_TRADE_EXIT_PATH")
        return tuple(reasons)


@dataclass(frozen=True)
class VerdictResult:
    verdict: str
    gates_passed: bool
    gates_failures: tuple[str, ...]
    sample_sufficient: bool
    upper_30_negative: bool | None
    reasons: tuple[str, ...]


def decide_verdict(
    *,
    gates: IntegrityGates,
    stat: WeekClusteredStat,
    trade_count: int,
) -> VerdictResult:
    """Frozen verdict mapping.

    CORROBORATES iff gates pass and L50 > 0.
    DOES_NOT_CORROBORATE iff gates pass and U50 < 0 (U30 < 0 also reported).
    INCONCLUSIVE otherwise (including insufficient sample: n >= 125 and
    W >= 26 are minimums).  This is NOT a forward PASS and the nominal p is
    not a strict independent error rate.
    """

    gates_passed = gates.passed
    failures = gates.failures()
    sample_sufficient = (
        trade_count >= MIN_TRADES and stat.weeks >= MIN_WEEKS
    )
    reasons: list[str] = list(failures)
    if not sample_sufficient:
        reasons.append(
            f"SAMPLE_BELOW_MINIMUM(n={trade_count}<{MIN_TRADES} "
            f"or W={stat.weeks}<{MIN_WEEKS})"
        )
    upper_30_negative: bool | None = None
    if gates_passed and sample_sufficient and stat.lower_50 is not None:
        if stat.lower_50 > 0:
            return VerdictResult(
                verdict=VERDICT_CORROBORATES,
                gates_passed=gates_passed,
                gates_failures=failures,
                sample_sufficient=sample_sufficient,
                upper_30_negative=None,
                reasons=(f"L50={stat.lower_50:.4f} > 0",),
            )
        if stat.upper_50 is not None and stat.upper_50 < 0:
            upper_30_negative = (
                stat.upper_30 is not None and stat.upper_30 < 0
            )
            return VerdictResult(
                verdict=VERDICT_DOES_NOT_CORROBORATE,
                gates_passed=gates_passed,
                gates_failures=failures,
                sample_sufficient=sample_sufficient,
                upper_30_negative=upper_30_negative,
                reasons=(
                    f"U50={stat.upper_50:.4f} < 0",
                    f"U30<0: {upper_30_negative}",
                ),
            )
    return VerdictResult(
        verdict=VERDICT_INCONCLUSIVE,
        gates_passed=gates_passed,
        gates_failures=failures,
        sample_sufficient=sample_sufficient,
        upper_30_negative=None,
        reasons=tuple(reasons) or ("BOUNDS_STRADDLE_ZERO",),
    )


# ------------------------------------------------------- descriptive only


def compute_descriptives(
    trades: Sequence[tuple[date, float]],
) -> dict[str, object]:
    """Descriptive-only section: never gates, never grounds to drop a period.

    A positive per-trade mean is NOT stable monthly profit; these numbers
    exist to characterise concentration and drawdown, not to select segments.
    """

    ordered = sorted(trades, key=lambda item: item[0])
    by_month: dict[tuple[int, int], list[float]] = {}
    by_half: dict[tuple[int, int], list[float]] = {}
    for session_date, value in ordered:
        by_month.setdefault(
            (session_date.year, session_date.month), []
        ).append(value)
        half = 1 if session_date.month <= 6 else 2
        by_half.setdefault((session_date.year, half), []).append(value)
    monthly = [
        {
            "month": f"{year:04d}-{month:02d}",
            "trades": len(values),
            "equal_notional_net_bps_sum": math.fsum(values),
        }
        for (year, month), values in sorted(by_month.items())
    ]
    half_yearly = [
        {
            "half": f"{year}-H{half}",
            "partial": (
                (year, half) == min(by_half) or (year, half) == max(by_half)
            ),
            "trades": len(values),
            "equal_notional_net_bps_sum": math.fsum(values),
        }
        for (year, half), values in sorted(by_half.items())
    ]
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for _, value in ordered:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    values_sorted = sorted((value for _, value in ordered), reverse=True)
    positive_total = math.fsum(
        value for value in values_sorted if value > 0
    )
    return {
        "descriptive_only": True,
        "never_gating": True,
        "calendar_half_years": half_yearly,
        "monthly_trade_counts": {
            item["month"]: item["trades"] for item in monthly
        },
        "monthly_equal_notional_sums": {
            item["month"]: item["equal_notional_net_bps_sum"]
            for item in monthly
        },
        "worst_month_bps": (
            min(item["equal_notional_net_bps_sum"] for item in monthly)
            if monthly
            else None
        ),
        "max_drawdown_equal_notional_bps": max_drawdown,
        "return_concentration": {
            "best_trade_share_of_positive_total": (
                values_sorted[0] / positive_total
                if positive_total > 0 and values_sorted
                else None
            ),
            "best_three_share_of_positive_total": (
                math.fsum(values_sorted[:3]) / positive_total
                if positive_total > 0 and values_sorted
                else None
            ),
        },
        "statement": (
            "a positive per-trade mean is not stable monthly profit; "
            "descriptive slices may never be used to drop a period"
        ),
    }


# ---------------------------------------------------------- calendar checks


def cross_check_trading_days(trading_days: Sequence[date]) -> dict[str, object]:
    """Cross-check the sealed API day list against the local holiday calendar.

    The local calendar covers 2024-01-01 onward only; 2023 sessions are
    reported as unchecked rather than assumed correct.
    """

    api_closed_locally_open: list[str] = []
    locally_closed_api_open: list[str] = []
    unchecked: list[str] = []
    for session_date in trading_days:
        if session_date < date(2024, 1, 1):
            unchecked.append(session_date.isoformat())
            continue
        local_closed = (
            session_date.weekday() >= 5
            or is_market_closed("US", session_date)
        )
        if local_closed:
            locally_closed_api_open.append(session_date.isoformat())
        else:
            api_closed_locally_open.append(session_date.isoformat())
    checked = len(trading_days) - len(unchecked)
    return {
        "checked_sessions": checked,
        "unchecked_sessions_2023": len(unchecked),
        "api_trading_local_trading": len(api_closed_locally_open),
        "api_trading_local_closed": locally_closed_api_open,
        "note": (
            "local holiday calendar has no 2023 data; mismatch lists are "
            "reported, never silently resolved"
        ),
    }


# ------------------------------------------------------------- fetch safety


def is_fetch_window_open(now: datetime) -> bool:
    """Fetching is forbidden 13:00-22:00 UTC Monday-Friday (US RTH + crons)."""

    current = now.astimezone(timezone.utc)
    if current.weekday() >= 5:
        return True
    moment = current.time()
    if PAUSE_WINDOW_START_UTC <= moment < PAUSE_WINDOW_END_UTC:
        return False
    return True


def _seconds_until_window_opens(now: datetime) -> float:
    current = now.astimezone(timezone.utc)
    if is_fetch_window_open(current):
        return 0.0
    opening = current.replace(
        hour=PAUSE_WINDOW_END_UTC.hour,
        minute=PAUSE_WINDOW_END_UTC.minute,
        second=0,
        microsecond=0,
    )
    if opening <= current:
        opening += timedelta(days=1)
    return (opening - current).total_seconds()


def classify_provider_error(message: str) -> str:
    """Classify a provider error string into the frozen safety policy.

    Order matters: quota and permission markers win first.  A ``301600``
    code alone is NOT a symbol verdict - the same code is used for
    request-shape refusals such as ``too many query days`` - so
    ``code=301600`` maps to REQUEST_SHAPE and only an explicit
    invalid/unknown-symbol message maps to PERMANENT_SYMBOL.
    """

    text = message.lower()
    if any(marker in text for marker in QUOTA_ERROR_MARKERS):
        return ErrorClass.GLOBAL_STOP_QUOTA.value
    if any(marker in text for marker in PERMISSION_ERROR_MARKERS):
        return ErrorClass.GLOBAL_STOP_PERMISSION.value
    if any(marker in text for marker in INVALID_SYMBOL_MARKERS):
        return ErrorClass.PERMANENT_SYMBOL.value
    if any(marker in text for marker in REQUEST_SHAPE_MARKERS):
        return ErrorClass.REQUEST_SHAPE.value
    return ErrorClass.TRANSIENT.value


class ReplayQuoteProvider(Protocol):
    """The ONLY provider surface the replay may touch (QuoteContext-based)."""

    def history_candlesticks_by_offset(
        self,
        symbol: str,
        period: str,
        *,
        count: int,
        after: datetime,
        forward: bool,
        adjustment: str,
    ) -> list[_CandleView]: ...

    def trading_days(
        self, *, begin: date, end: date
    ) -> tuple[tuple[date, ...], tuple[date, ...]]: ...


class _Throttle:
    """Single-worker rate limiter with pause windows and a monthly checkpoint."""

    def __init__(
        self,
        *,
        rate_per_second: float,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None],
    ) -> None:
        if rate_per_second <= 0:
            raise HistoricalReplayError("rate must be positive")
        self._min_interval = 1.0 / rate_per_second
        self._clock = clock
        self._sleep = sleep
        self._last_request: datetime | None = None
        self._month: tuple[int, int] | None = None
        self.requests_this_month = 0
        self.total_requests = 0
        self.quota_checkpoints: list[dict[str, object]] = []

    def _await_pause_window(self) -> None:
        while True:
            now = self._clock()
            if is_fetch_window_open(now):
                return
            self._sleep(_seconds_until_window_opens(now))

    def wait_for_slot(self) -> None:
        self._await_pause_window()
        now = self._clock()
        if self._last_request is not None:
            elapsed = (now - self._last_request).total_seconds()
            if elapsed < self._min_interval:
                self._sleep(self._min_interval - elapsed)
        now = self._clock()
        self._last_request = now
        self.total_requests += 1
        self.requests_this_month += 1
        current_month = (now.year, now.month)
        if self._month is None:
            self._month = current_month
        elif current_month != self._month:
            # Month boundary: recheck the quota bookkeeping before continuing.
            self.quota_checkpoints.append({
                "month": f"{self._month[0]:04d}-{self._month[1]:02d}",
                "requests": self.requests_this_month,
                "at": now.isoformat(),
            })
            self._month = current_month
            self.requests_this_month = 0


@dataclass(frozen=True)
class FetchedBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    turnover: float | None


class _ProviderCallFailure(Exception):
    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.message = message


class _RetryableProvider:
    """Bounded-retry wrapper; global-stop classes propagate immediately."""

    def __init__(
        self,
        provider: ReplayQuoteProvider,
        *,
        throttle: _Throttle,
        max_transient_retries: int,
    ) -> None:
        self._provider = provider
        self._throttle = throttle
        self._max_transient_retries = max_transient_retries

    def call(self, operation: Callable[[], list[_CandleView]]) -> list[_CandleView]:
        attempt = 0
        while True:
            self._throttle.wait_for_slot()
            try:
                return operation()
            except Exception as exc:  # noqa: BLE001 - classified below
                error_class = classify_provider_error(str(exc))
                if error_class in (
                    ErrorClass.GLOBAL_STOP_QUOTA.value,
                    ErrorClass.GLOBAL_STOP_PERMISSION.value,
                ):
                    raise _ProviderCallFailure(error_class, str(exc)) from exc
                if error_class in (
                    ErrorClass.PERMANENT_SYMBOL.value,
                    ErrorClass.REQUEST_SHAPE.value,
                ):
                    # Permanent answers about the symbol or the request
                    # shape are never retried: retrying an unchanged
                    # request cannot change the provider's answer.
                    raise _ProviderCallFailure(error_class, str(exc)) from exc
                if attempt >= self._max_transient_retries:
                    raise _ProviderCallFailure(
                        ErrorClass.TRANSIENT.value,
                        f"transient retries exhausted: {exc}",
                    ) from exc
                attempt += 1
                self._throttle._sleep(min(30.0, 2.0**attempt))


def _page_forward(
    retryable: _RetryableProvider,
    *,
    symbol: str,
    period: str,
    adjustment: str,
    first_boundary: datetime,
    stop_boundary: datetime,
) -> list[_CandleView]:
    """Forward pagination with one-bar overlap (deduped) and stall guards."""

    retained: dict[datetime, _CandleView] = {}
    cursor = first_boundary
    empty_pages = 0
    while cursor < stop_boundary:
        page = retryable.call(
            lambda: retryable._provider.history_candlesticks_by_offset(
                symbol,
                period,
                count=PAGE_SIZE,
                after=cursor,
                forward=True,
                adjustment=adjustment,
            )
        )
        if not page:
            empty_pages += 1
            if empty_pages >= 2:
                break
            cursor += timedelta(minutes=1)
            continue
        empty_pages = 0
        for bar in page:
            retained[bar.timestamp] = bar
        latest = max(bar.timestamp for bar in page)
        if latest <= cursor:
            cursor += timedelta(minutes=1)
        else:
            cursor = latest
        if latest >= stop_boundary:
            break
    return [retained[timestamp] for timestamp in sorted(retained)]


def _page_forward_daily(
    retryable: _RetryableProvider,
    *,
    symbol: str,
    adjustment: str,
    first_boundary: datetime,
    stop_boundary: datetime,
    chunk_days: int = 900,
) -> list[_CandleView]:
    """Forward daily pagination that tolerates empty spans between chunks.

    Unlike minute pagination, a daily cursor can sit far before the first
    available bar (warm-up padding) or cross delisting gaps; two empty
    pages there are NOT end-of-data.  The range is walked in fixed day
    chunks until the stop boundary; a completely empty walk yields no
    bars without aborting early.
    """

    retained: dict[datetime, _CandleView] = {}
    cursor = first_boundary
    while cursor < stop_boundary:
        page = retryable.call(
            lambda: retryable._provider.history_candlesticks_by_offset(
                symbol,
                "DAY",
                count=PAGE_SIZE,
                after=cursor,
                forward=True,
                adjustment=adjustment,
            )
        )
        if not page:
            cursor += timedelta(days=chunk_days)
            continue
        for bar in page:
            retained[bar.timestamp] = bar
        latest = max(bar.timestamp for bar in page)
        if latest >= stop_boundary:
            break
        if latest <= cursor:
            cursor += timedelta(days=1)
        else:
            cursor = latest + timedelta(days=1)
    return [retained[timestamp] for timestamp in sorted(retained)]


def _retain_minute_bars(
    bars: Sequence[_CandleView],
    *,
    sessions: frozenset[date],
) -> list[FetchedBar]:
    retained: list[FetchedBar] = []
    for bar in bars:
        local = _MARKET_SESSION.local(bar.timestamp)
        if local.date() not in sessions:
            continue
        offset = _minute_offset(
            bar.timestamp, _session_open_utc(local.date())
        )
        if offset is None or not 0 <= offset <= MINUTE_RETAINED_LAST_OFFSET:
            continue
        retained.append(
            FetchedBar(
                timestamp=bar.timestamp,
                open=float(bar.open),
                high=float(bar.high),
                low=float(bar.low),
                close=float(bar.close),
                volume=float(bar.volume) if bar.volume is not None else None,
                turnover=(
                    float(bar.turnover) if bar.turnover is not None else None
                ),
            )
        )
    return retained


def _retain_daily_bars(
    bars: Sequence[_CandleView],
    *,
    start_date: date,
    end_date: date,
) -> list[FetchedBar]:
    retained: list[FetchedBar] = []
    for bar in bars:
        session_date = _MARKET_SESSION.local(bar.timestamp).date()
        if not start_date <= session_date <= end_date:
            continue
        retained.append(
            FetchedBar(
                timestamp=bar.timestamp,
                open=float(bar.open),
                high=float(bar.high),
                low=float(bar.low),
                close=float(bar.close),
                volume=float(bar.volume) if bar.volume is not None else None,
                turnover=(
                    float(bar.turnover) if bar.turnover is not None else None
                ),
            )
        )
    return retained


# ------------------------------------------------------------ cache / status


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    rendered = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    _atomic_write_bytes(path, (rendered + "\n").encode("utf-8"))


def _atomic_write_gzip_json(
    path: Path, payload: dict[str, object]
) -> None:
    rendered = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with gzip.open(temporary, "wt", encoding="utf-8") as handle:
            handle.write(rendered)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_gzip_json(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise HistoricalReplayError(f"cache file is not an object: {path}")
    return raw


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_status(cache_dir: Path) -> dict[str, Any]:
    path = cache_dir / "status.json"
    if not path.exists():
        return {
            "version": REPLAY_CLI_VERSION,
            "global_stop": None,
            "symbols": {},
            "monthly_requests": {},
            "requests_total": 0,
        }
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise HistoricalReplayError("status.json is not an object")
    return raw


def _save_status(cache_dir: Path, status: dict[str, Any]) -> None:
    _atomic_write_json(cache_dir / "status.json", status)


def _symbol_state(
    status: dict[str, Any], symbol: str
) -> dict[str, Any]:
    symbols = status.setdefault("symbols", {})
    if symbol not in symbols:
        symbols[symbol] = {
            "daily": {"state": "PENDING"},
            "minute": {"state": "PENDING"},
        }
    return symbols[symbol]


def _write_bars_file(
    cache_dir: Path,
    kind: str,
    symbol: str,
    *,
    period: str,
    adjustment: str,
    bars: Sequence[FetchedBar],
) -> str:
    if kind == "minute":
        rows: list[list[object]] = [
            [
                bar.timestamp.isoformat(),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.turnover,
            ]
            for bar in bars
        ]
    else:
        rows = [
            [
                _MARKET_SESSION.local(bar.timestamp)
                .date()
                .isoformat(),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.turnover,
            ]
            for bar in bars
        ]
    path = cache_dir / kind / f"{symbol}.json.gz"
    _atomic_write_gzip_json(
        path,
        {
            "symbol": symbol,
            "period": period,
            "adjustment": adjustment,
            "bars": rows,
        },
    )
    return _file_sha256(path)


def _load_minute_bars(
    cache_dir: Path, symbol: str
) -> dict[datetime, Any]:
    path = cache_dir / "minute" / f"{symbol}.json.gz"
    if not path.exists():
        return {}
    raw = _read_gzip_json(path)
    rows: list[
        tuple[datetime, float, float, float, float, float | None, float | None]
    ] = []
    for row in raw.get("bars", []):
        rows.append(
            (
                datetime.fromisoformat(row[0]),
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]) if row[5] is not None else None,
                float(row[6]) if row[6] is not None else None,
            )
        )
    return _coerced_candles(rows)


def _load_daily_bars(
    cache_dir: Path, symbol: str
) -> list[_CandleView]:
    path = cache_dir / "daily" / f"{symbol}.json.gz"
    if not path.exists():
        return []
    raw = _read_gzip_json(path)
    views: list[_CandleView] = []
    for row in raw.get("bars", []):
        session_date = date.fromisoformat(row[0])
        views.append(
            _CandleView(
                timestamp=_session_open_utc(session_date),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]) if row[5] is not None else None,
                turnover=float(row[6]) if row[6] is not None else None,
            )
        )
    return views


# --------------------------------------------------------------- fetch flow


def _default_cache_dir() -> Path:
    return (
        Path(__file__).resolve().parents[2] / _CACHE_DIR_NAME
    )


def _load_plan(plan_path: Path) -> dict[str, Any]:
    raw = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise HistoricalReplayError("plan file is not an object")
    if raw.get("analysis_id") != ANALYSIS_ID:
        raise HistoricalReplayError(
            "plan file analysis_id does not match the frozen replay"
        )
    return raw


def _plan_warmup_start(plan_payload: dict[str, Any]) -> date:
    """Earliest warm-up start declared by the pre-declared fetch plan."""

    starts = [
        date.fromisoformat(str(entry["daily_start"]))
        for entry in plan_payload["symbols"]
    ]
    if not starts:
        raise HistoricalReplayError(
            "fetch plan declares no symbols; warm-up start is undefined"
        )
    return min(starts)


def derive_trading_days_from_benchmarks(
    *,
    qqq_dates: Sequence[date],
    dia_dates: Sequence[date],
    start: date,
    end: date,
) -> dict[str, object]:
    """Derive the expected US session list returns-blind from QQQ/DIA bars.

    Decision 1 (2026-09-27, before any outcome) replaced the infeasible
    provider ``trading_days`` source (interval < 1 month, most recent year
    only).  A date is an expected session iff BOTH benchmark ETFs have a
    regular-session daily bar that day inside [start, end].  One-sided
    dates are excluded and reported.  Only dates matter, so the daily bars
    are fetched unadjusted.

    The local-calendar cross-check reports every mismatch (both-ETF-bar on
    a locally-closed day, and vice versa where covered) but never gates.
    Half days are recorded where knowable; 2023 half-days are marked
    unverified because the local calendar starts 2024-01-01.  The 09:36
    entry and the 60-minute hold both end before 13:00, so half days do
    not change the rule.
    """

    qqq = {
        value
        for value in qqq_dates
        if start <= value <= end
    }
    dia = {
        value
        for value in dia_dates
        if start <= value <= end
    }
    sessions = sorted(qqq & dia)
    one_sided = sorted((qqq ^ dia))
    mismatches = [
        {
            "date": value.isoformat(),
            "in_qqq": value in qqq,
            "in_dia": value in dia,
            "reason": "benchmark daily bar present in only one ETF",
        }
        for value in one_sided
    ]
    both_bar_local_closed: list[str] = []
    local_coverage_starts = date(2024, 1, 1)
    for value in sessions:
        if value < local_coverage_starts:
            continue
        if value.weekday() >= 5 or is_market_closed("US", value):
            both_bar_local_closed.append(value.isoformat())
    half_days: dict[str, dict[str, object]] = {}
    for value in sessions:
        if value in _CONVENTIONAL_2023_EARLY_CLOSES:
            half_days[value.isoformat()] = {
                "label": "conventional NYSE early close (2023)",
                "verified": False,
            }
            continue
        if not is_half_day("US", value):
            continue
        # Best-effort label from the same static table the boolean comes
        # from (sibling app.core module, read-only index access).
        half_day_index = holiday_calendar._HALF_DAY_INDEX  # noqa: SLF001
        label = (
            half_day_index.get((value, "US"))
            if half_day_index is not None
            else None
        )
        half_days[value.isoformat()] = {
            "label": label,
            "verified": value >= local_coverage_starts,
        }
    return {
        "trading_days": [value.isoformat() for value in sessions],
        "one_sided_dates": mismatches,
        "local_calendar_cross_check": {
            "local_coverage_starts": local_coverage_starts.isoformat(),
            "both_etf_bar_local_closed": both_bar_local_closed,
            "note": (
                "the cross-check lists mismatches and never gates; "
                "sessions before local coverage are unchecked, not assumed"
            ),
        },
        "half_trading_days": half_days,
        "half_day_note": (
            "2023 half-days are unverified (local calendar starts "
            "2024-01-01); the 09:36 entry and 60-minute hold both end "
            "before 13:00, so half days do not change the rule"
        ),
    }


def _optional_provider_trading_days_cross_check(
    throttle: _Throttle,
    provider: ReplayQuoteProvider,
    *,
    benchmark_days: Sequence[date],
    clock: Callable[[], datetime],
) -> dict[str, object]:
    """Extra-only provider cross-check; its failure never aborts the fetch.

    The SDK supports only the most recent year and intervals under one
    month, so the check is chunked to 28 days over at most the recent year
    and every failure (including ``301600 too many query days``) is
    recorded as SKIPPED_ERROR and swallowed.
    """

    now = clock()
    recent_start = date(now.year - 1, now.month, now.day)
    span_start = max(recent_start, benchmark_days[0])
    span_end = min(
        date(now.year, now.month, now.day), benchmark_days[-1]
    )
    if span_end < span_start:
        return {
            "status": "SKIPPED_OUT_OF_RANGE",
            "provider_days": [],
        }
    days: list[date] = []
    half: list[date] = []
    cursor = span_start
    chunks = 0
    while cursor <= span_end and chunks < _TRADING_DAYS_CROSS_CHECK_MAX_CHUNKS:
        chunk_end = min(
            span_end,
            cursor + timedelta(days=_TRADING_DAYS_CHUNK_DAYS - 1),
        )
        throttle.wait_for_slot()
        try:
            chunk_days, chunk_half = provider.trading_days(
                begin=cursor, end=chunk_end
            )
        except Exception as exc:  # noqa: BLE001 - extra cross-check only
            return {
                "status": "SKIPPED_ERROR",
                "error": str(exc)[:400],
                "chunks_completed": chunks,
            }
        days.extend(chunk_days)
        half.extend(chunk_half)
        chunks += 1
        cursor = chunk_end + timedelta(days=1)
    benchmark_set = set(benchmark_days)
    provider_set = {
        value for value in days if benchmark_days[0] <= value <= benchmark_days[-1]
    }
    return {
        "status": "RECORDED",
        "chunks": chunks,
        "provider_days": [value.isoformat() for value in sorted(provider_set)],
        "provider_half_days": [
            value.isoformat() for value in sorted(set(half))
        ],
        "provider_only_dates": [
            value.isoformat() for value in sorted(provider_set - benchmark_set)
        ],
        "benchmark_only_dates": [
            value.isoformat() for value in sorted(benchmark_set - provider_set)
        ],
    }


def _fetch_trading_days(
    throttle: _Throttle,
    retryable: _RetryableProvider,
    provider: ReplayQuoteProvider,
    *,
    window_start: date,
    window_end: date,
    warmup_start: date,
    clock: Callable[[], datetime],
) -> dict[str, object]:
    """Fetch QQQ/DIA daily bars and derive the expected session list."""

    benchmark_dates: dict[str, set[date]] = {}
    for symbol in BENCHMARK_ETFS:
        bars = _page_forward_daily(
            retryable,
            symbol=symbol,
            # Only dates matter, so no adjustment is needed.
            adjustment="NoAdjust",
            first_boundary=_session_open_utc(warmup_start) - timedelta(days=2),
            stop_boundary=(
                _session_open_utc(window_end) + timedelta(days=2)
            ),
        )
        benchmark_dates[symbol] = {
            _MARKET_SESSION.local(bar.timestamp).date() for bar in bars
        }
    qqq_dates, dia_dates = (
        benchmark_dates[BENCHMARK_ETFS[0]],
        benchmark_dates[BENCHMARK_ETFS[1]],
    )
    derived = derive_trading_days_from_benchmarks(
        qqq_dates=sorted(qqq_dates),
        dia_dates=sorted(dia_dates),
        start=window_start,
        end=window_end,
    )
    benchmark_days = [
        date.fromisoformat(value)
        for value in cast(list[str], derived["trading_days"])
    ]
    cross_check = _optional_provider_trading_days_cross_check(
        throttle,
        provider,
        benchmark_days=benchmark_days,
        clock=clock,
    )
    return {
        "source": (
            "benchmark daily-bar intersection (QQQ.US ∩ DIA.US, DAY bars, "
            "NoAdjust; dates only) - decision 1, 2026-09-27"
        ),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "warmup_start": warmup_start.isoformat(),
        "provider_trading_days_cross_check": cross_check,
        **derived,
    }


def run_fetch(
    *,
    cache_dir: Path,
    plan_payload: dict[str, Any],
    provider: ReplayQuoteProvider,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    rate_per_second: float = DEFAULT_REQUESTS_PER_SECOND,
    max_transient_retries: int = DEFAULT_MAX_TRANSIENT_RETRIES,
) -> dict[str, Any]:
    """Fetch the pre-declared plan into the cache; prints nothing itself.

    Refuses to run while a global stop marker is present.  Writes only
    coverage / error / quota / ETA information upward - never PnL (none is
    computed at this stage).
    """

    status = _load_status(cache_dir)
    global_stop = status.get("global_stop")
    if isinstance(global_stop, dict):
        raise HistoricalReplayError(
            "fetch refused: GLOBAL STOP is active since "
            f"{global_stop.get('at')} ({global_stop.get('reason')}); "
            "manual review is required before any further provider call"
        )
    throttle = _Throttle(
        rate_per_second=rate_per_second,
        clock=clock,
        sleep=sleep,
    )
    retryable = _RetryableProvider(
        provider,
        throttle=throttle,
        max_transient_retries=max_transient_retries,
    )

    trading_days_path = cache_dir / "trading_days.json"
    if not trading_days_path.exists():
        try:
            payload = _fetch_trading_days(
                throttle,
                retryable,
                provider,
                window_start=date.fromisoformat(
                    plan_payload["window_start"]
                ),
                window_end=date.fromisoformat(
                    plan_payload["window_end"]
                ),
                warmup_start=_plan_warmup_start(plan_payload),
                clock=clock,
            )
        except _ProviderCallFailure as exc:
            status["requests_total"] = (
                status.get("requests_total", 0)
                + throttle.total_requests
            )
            if exc.error_class in (
                ErrorClass.GLOBAL_STOP_QUOTA.value,
                ErrorClass.GLOBAL_STOP_PERMISSION.value,
            ):
                status["global_stop"] = {
                    "reason": exc.error_class,
                    "detail": exc.message[:400],
                    "at": clock().isoformat(),
                }
                status.setdefault("errors", []).append({
                    "stage": "TRADING_DAYS",
                    "error_class": exc.error_class,
                    "detail": exc.message[:400],
                    "at": clock().isoformat(),
                })
                _save_status(cache_dir, status)
                raise HistoricalReplayError(
                    f"GLOBAL STOP ({exc.error_class}); no retry: "
                    f"{exc.message}"
                ) from exc
            # Any other trading-day failure (benchmark bars exhausted their
            # bounded retries, request-shape refusal, ...) fails CLEANLY:
            # a status.json error entry, never a raw traceback, never a
            # per-symbol permanent failure, and no global stop.
            status.setdefault("errors", []).append({
                "stage": "TRADING_DAYS",
                "error_class": exc.error_class,
                "detail": exc.message[:400],
                "at": clock().isoformat(),
            })
            _save_status(cache_dir, status)
            raise HistoricalReplayError(
                "benchmark trading-day derivation failed ("
                f"{exc.error_class}): {exc.message}"
            ) from exc
        _atomic_write_json(trading_days_path, payload)
    sealed_days = [
        date.fromisoformat(value)
        for value in json.loads(
            trading_days_path.read_text(encoding="utf-8")
        )["trading_days"]
    ]
    sealed_set = frozenset(sealed_days)

    symbols: list[dict[str, Any]] = list(plan_payload["symbols"])
    estimated_requests_total = int(
        plan_payload.get("estimated_requests_total", 0)
    )
    failures: dict[str, str] = {}
    permanent: dict[str, str] = {}
    for entry in symbols:
        symbol = str(entry["symbol"])
        state = _symbol_state(status, symbol)
        first_session = date.fromisoformat(entry["first_session"])
        last_session = date.fromisoformat(entry["last_session"])
        daily_start = date.fromisoformat(entry["daily_start"])
        member_sessions = frozenset(
            value
            for value in sealed_days
            if first_session <= value <= last_session
        )

        try:
            if state["daily"]["state"] != "COMPLETE":
                if state["daily"]["state"] != "PERMANENT_FAILURE":
                    bars = _page_forward_daily(
                        retryable,
                        symbol=symbol,
                        adjustment="ForwardAdjust",
                        first_boundary=(
                            _session_open_utc(daily_start) - timedelta(days=2)
                        ),
                        stop_boundary=(
                            _session_open_utc(last_session) + timedelta(days=2)
                        ),
                    )
                    retained = _retain_daily_bars(
                        bars, start_date=daily_start, end_date=last_session
                    )
                    digest = _write_bars_file(
                        cache_dir,
                        "daily",
                        symbol,
                        period="DAY",
                        adjustment="ForwardAdjust",
                        bars=retained,
                    )
                    state["daily"] = {
                        "state": "COMPLETE",
                        "sha256": digest,
                        "bars": len(retained),
                    }
            if state["minute"]["state"] != "COMPLETE":
                if state["minute"]["state"] != "PERMANENT_FAILURE":
                    bars = _page_forward(
                        retryable,
                        symbol=symbol,
                        period="MIN_1",
                        adjustment="NoAdjust",
                        first_boundary=(
                            _session_open_utc(first_session) - _BAR_DURATION
                        ),
                        stop_boundary=(
                            _session_open_utc(last_session)
                            + timedelta(
                                minutes=MINUTE_RETAINED_LAST_OFFSET + 2
                            )
                        ),
                    )
                    retained = _retain_minute_bars(
                        bars, sessions=member_sessions
                    )
                    digest = _write_bars_file(
                        cache_dir,
                        "minute",
                        symbol,
                        period="MIN_1",
                        adjustment="NoAdjust",
                        bars=retained,
                    )
                    covered = len(
                        {
                            _MARKET_SESSION.local(bar.timestamp).date()
                            for bar in retained
                        }
                    )
                    state["minute"] = {
                        "state": "COMPLETE",
                        "sha256": digest,
                        "bars": len(retained),
                        "sessions_with_bars": covered,
                        "sessions_expected": len(member_sessions),
                    }
        except _ProviderCallFailure as exc:
            if exc.error_class in (
                ErrorClass.GLOBAL_STOP_QUOTA.value,
                ErrorClass.GLOBAL_STOP_PERMISSION.value,
            ):
                status["global_stop"] = {
                    "reason": exc.error_class,
                    "detail": exc.message[:400],
                    "at": clock().isoformat(),
                }
                status["requests_total"] = (
                    status.get("requests_total", 0) + throttle.total_requests
                )
                _save_status(cache_dir, status)
                raise HistoricalReplayError(
                    f"GLOBAL STOP ({exc.error_class}); no retry: {exc.message}"
                ) from exc
            if exc.error_class == ErrorClass.PERMANENT_SYMBOL.value:
                permanent[symbol] = exc.message[:200]
                state["daily"] = {
                    "state": "PERMANENT_FAILURE",
                    "error": exc.message[:200],
                }
                state["minute"] = {
                    "state": "PERMANENT_FAILURE",
                    "error": exc.message[:200],
                }
            elif exc.error_class == ErrorClass.REQUEST_SHAPE.value:
                # A request-shape refusal is about the QUERY, never about
                # the symbol: it must not be persisted as a symbol
                # permanent failure.  It fails the fetch cleanly for
                # review instead.
                status.setdefault("errors", []).append({
                    "stage": "SYMBOL_FETCH",
                    "symbol": symbol,
                    "error_class": exc.error_class,
                    "detail": exc.message[:400],
                    "at": clock().isoformat(),
                })
                status["requests_total"] = (
                    status.get("requests_total", 0)
                    + throttle.total_requests
                )
                throttle.total_requests = 0
                _save_status(cache_dir, status)
                raise HistoricalReplayError(
                    "request-shape refusal while fetching "
                    f"{symbol} ({exc.message}); the request must be "
                    "corrected, not retried or persisted"
                ) from exc
            else:
                failures[symbol] = exc.message[:200]
                state["daily"]["state"] = "FAILED_TRANSIENT"
                state["minute"]["state"] = "FAILED_TRANSIENT"
        status["requests_total"] = (
            status.get("requests_total", 0) + throttle.total_requests
        )
        throttle.total_requests = 0
        _save_status(cache_dir, status)

    symbol_states = {
        symbol: (
            status["symbols"][symbol]["minute"]["state"]
            if symbol in status.get("symbols", {})
            else "PENDING"
        )
        for symbol in (str(entry["symbol"]) for entry in symbols)
    }
    complete = sum(1 for value in symbol_states.values() if value == "COMPLETE")
    remaining_requests = max(0, estimated_requests_total - int(status.get("requests_total", 0)))
    return {
        "coverage": {
            "symbols_total": len(symbols),
            "symbols_complete": complete,
            "symbols_permanent_failure": len(permanent),
            "symbols_failed_transient": len(failures),
        },
        "errors": {
            "permanent": permanent,
            "transient": failures,
        },
        "quota": {
            "requests_total": status.get("requests_total", 0),
            "monthly_checkpoints": throttle.quota_checkpoints,
            "rate_per_second": rate_per_second,
        },
        "eta_seconds_remaining": (
            remaining_requests / rate_per_second if rate_per_second else None
        ),
    }


class _LongportQuoteProvider:
    """QuoteContext-only adapter (env credentials; never TradeContext)."""

    def __init__(self, config_values: Settings) -> None:
        credentials = {
            "LONGPORT_APP_KEY": config_values.longbridge_app_key,
            "LONGPORT_APP_SECRET": config_values.longbridge_app_secret,
            "LONGPORT_ACCESS_TOKEN": config_values.longbridge_access_token,
        }
        missing = [
            name for name, value in credentials.items() if not value
        ]
        if missing:
            raise HistoricalReplayError(
                "Longport credentials are unavailable: " + ", ".join(missing)
            )
        for name, value in credentials.items():
            os.environ[name] = value
        try:
            import longport.openapi as openapi
        except ImportError as exc:  # pragma: no cover - SDK always present
            raise HistoricalReplayError(
                "longport SDK is not installed"
            ) from exc
        config = openapi.Config.from_env()
        # QuoteContext ONLY: building a TradeContext is forbidden here.
        self._quote_ctx = openapi.QuoteContext(config)
        self._openapi = openapi

    def _period(self, period: str) -> Any:
        # The replay speaks "DAY" / "MIN_1"; the SDK enum members are
        # ``Period.Day`` / ``Period.Min_1`` (v2's first fetch failed on
        # ``Period.DAY``).  Anything else is refused before any request.
        name = _SDK_PERIOD_NAMES.get(period)
        if name is None:
            raise HistoricalReplayError(
                f"unsupported candlestick period for the replay: {period}"
            )
        return getattr(self._openapi.Period, name)

    def history_candlesticks_by_offset(
        self,
        symbol: str,
        period: str,
        *,
        count: int,
        after: datetime,
        forward: bool,
        adjustment: str,
    ) -> list[_CandleView]:
        adjust_type = getattr(self._openapi.AdjustType, adjustment)
        boundary = after
        if boundary.tzinfo is not None:
            market = symbol.rsplit(".", 1)[-1].upper()
            zone = ZoneInfo("America/New_York") if market == "US" else boundary.tzinfo
            boundary = boundary.astimezone(zone)
        response = self._quote_ctx.history_candlesticks_by_offset(
            symbol,
            self._period(period),
            adjust_type,
            forward,
            count,
            boundary,
        )
        return [self._view(item) for item in response]

    def trading_days(
        self, *, begin: date, end: date
    ) -> tuple[tuple[date, ...], tuple[date, ...]]:
        response = self._quote_ctx.trading_days(
            self._openapi.Market.US, begin, end
        )
        return (
            tuple(response.trading_days),
            tuple(response.half_trading_days),
        )

    def _view(self, item: Any) -> _CandleView:
        timestamp = item.timestamp
        if not isinstance(timestamp, datetime):
            raise HistoricalReplayError(
                "provider candle timestamp is not a datetime"
            )
        if timestamp.tzinfo is None:
            timestamp = timestamp.astimezone(timezone.utc)
        else:
            timestamp = timestamp.astimezone(timezone.utc)
        return _CandleView(
            timestamp=timestamp,
            open=float(item.open),
            high=float(item.high),
            low=float(item.low),
            close=float(item.close),
            volume=float(item.volume) if item.volume is not None else None,
            turnover=(
                float(item.turnover) if item.turnover is not None else None
            ),
        )


# ------------------------------------------------------------------- plan


def build_plan_payload(*, window_start: date, window_end: date) -> dict[str, Any]:
    """Offline plan: PIT universe, per-symbol intervals, request estimates."""

    planning = _planning_sessions(window_start, window_end)
    sessions_by_symbol: dict[str, list[date]] = {}
    for session_date in planning:
        for symbol in pit_universe_for_session(session_date):
            sessions_by_symbol.setdefault(symbol, []).append(session_date)

    weekdays_before = _weekday_sessions(date(2022, 1, 1), window_start)
    rth_minutes_per_session = 390
    entries: list[dict[str, Any]] = []
    forward_requests = 0
    per_day_requests = 0
    for symbol in sorted(sessions_by_symbol):
        member_sessions = sessions_by_symbol[symbol]
        first_session = member_sessions[0]
        last_session = member_sessions[-1]
        prior = [
            value for value in weekdays_before if value < first_session
        ]
        warmup = _warmup_start(prior)
        if warmup is None:
            raise HistoricalReplayError(
                f"not enough warm-up sessions before {first_session}"
            )
        minute_sessions = len(member_sessions)
        daily_sessions = len(member_sessions) + (
            len(prior) - max(0, len(prior) - WARMUP_SESSIONS)
        )
        daily_span = (last_session - warmup).days * 5 // 7 + WARMUP_SESSIONS
        minute_request_count = (
            -(-minute_sessions * rth_minutes_per_session // PAGE_SIZE)
        )
        daily_request_count = max(1, -(-daily_span // PAGE_SIZE))
        forward_requests += minute_request_count + daily_request_count
        per_day_requests += minute_sessions + daily_request_count
        entries.append(
            {
                "symbol": symbol,
                "first_session": first_session.isoformat(),
                "last_session": last_session.isoformat(),
                "daily_start": warmup.isoformat(),
                "minute_sessions": minute_sessions,
                "estimated_minute_requests": minute_request_count,
                "estimated_daily_requests": daily_request_count,
            }
        )
    member_days = sum(entry["minute_sessions"] for entry in entries)
    return {
        "analysis_id": ANALYSIS_ID,
        "cli_version": REPLAY_CLI_VERSION,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "planning_day_source": (
            "offline weekday approximation minus local holiday calendar; "
            "the sealed trading-day list comes from the provider"
        ),
        "distinct_symbols": len(entries),
        "member_days": member_days,
        "symbols": entries,
        "request_estimates": {
            "forward_pagination_1000": forward_requests + 3,
            "one_small_request_per_session": per_day_requests + 3,
            "note": (
                "minute data needs 09:30-10:40 ET (71 bars) per session but "
                "forward pagination receives full RTH pages (390 min est.) "
                "and trims retention; +3 trading-day chunk requests"
            ),
        },
        "estimated_requests_total": forward_requests + 3,
        "universe_preview": {
            session_date.isoformat(): sorted(
                pit_universe_for_session(session_date)
            )
            for session_date in (planning[0], planning[-1])
        },
    }


# ------------------------------------------------------------------- seal


def run_seal(cache_dir: Path) -> str:
    """Seal the input manifest (files, trading days, universe) and hash it."""

    trading_days_path = cache_dir / "trading_days.json"
    if not trading_days_path.exists():
        raise HistoricalReplayError(
            "seal refused: trading_days.json is missing (run fetch first)"
        )
    status = _load_status(cache_dir)
    if isinstance(status.get("global_stop"), dict):
        raise HistoricalReplayError(
            "seal refused: a GLOBAL STOP marker is active"
        )
    files: list[dict[str, object]] = []
    for path in sorted(cache_dir.rglob("*.json.gz")):
        relative = path.relative_to(cache_dir).as_posix()
        files.append(
            {
                "path": relative,
                "sha256": _file_sha256(path),
                "bytes": path.stat().st_size,
            }
        )
    trading_payload = json.loads(
        trading_days_path.read_text(encoding="utf-8")
    )
    sealed_days = sorted(
        date.fromisoformat(value)
        for value in trading_payload["trading_days"]
        if WINDOW_START
        <= date.fromisoformat(value)
        <= WINDOW_END
    )
    universe: dict[str, list[str]] = {}
    member_days_total = 0
    for session_date in sealed_days:
        members = sorted(pit_universe_for_session(session_date))
        universe[session_date.isoformat()] = members
        member_days_total += len(members)
    manifest: dict[str, object] = {
        "analysis_id": ANALYSIS_ID,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "window": {
            "start": WINDOW_START.isoformat(),
            "end": WINDOW_END.isoformat(),
        },
        "files": files,
        "trading_days": trading_payload,
        "universe": universe,
        "member_days_total": member_days_total,
        "fetch_status_snapshot": status,
        "calendar_cross_check": cross_check_trading_days(sealed_days),
    }
    manifest_path = cache_dir / "manifest.json"
    _atomic_write_json(manifest_path, manifest)
    return _file_sha256(manifest_path)


# --------------------------------------------------------------- evaluate


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _git_head() -> str | None:
    try:
        completed = subprocess.run(
            ("git", "-C", str(_repo_root()), "rev-parse", "HEAD"),
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip()


def _member_has_decision_inputs(
    bars: dict[datetime, Any],
    *,
    session_open: datetime,
) -> bool:
    for offset in range(ENTRY_OFFSET + 1):
        if session_open + timedelta(minutes=offset) not in bars:
            return False
    return True


def run_evaluate(
    *,
    cache_dir: Path,
    output_path: Path,
    rerun_reason: str | None = None,
) -> dict[str, Any]:
    """One-pass evaluation; refuses unsealed inputs and silent overwrites."""

    manifest_path = cache_dir / "manifest.json"
    if not manifest_path.exists():
        raise HistoricalReplayError(
            "evaluate refused: no sealed input manifest (run seal first)"
        )
    manifest_sha256 = _file_sha256(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("analysis_id") != ANALYSIS_ID:
        raise HistoricalReplayError(
            "evaluate refused: manifest analysis_id mismatch"
        )
    if frozen_config_version() != FROZEN_CONFIG_VERSION:
        raise HistoricalReplayError(
            "evaluate refused: the frozen rule hash no longer reproduces "
            f"({frozen_config_version()}); the replay may only run against "
            "the registered rule"
        )
    drift: list[str] = []
    for entry in manifest["files"]:
        path = cache_dir / str(entry["path"])
        if not path.exists() or _file_sha256(path) != entry["sha256"]:
            drift.append(str(entry["path"]))
    if drift:
        raise HistoricalReplayError(
            "evaluate refused: sealed inputs drifted after seal: "
            + ", ".join(drift[:10])
        )
    if output_path.exists() and not rerun_reason:
        raise HistoricalReplayError(
            "evaluate refused: output already exists; pass --rerun-reason "
            "to supersede it (the original is kept)"
        )
    if output_path.exists():
        supersede_index = 1
        while True:
            preserved = output_path.with_name(
                f"{output_path.stem}.superseded-{supersede_index}.json"
            )
            if not preserved.exists():
                break
            supersede_index += 1
        output_path.rename(preserved)

    fetch_snapshot = manifest.get("fetch_status_snapshot", {})
    symbol_states = fetch_snapshot.get("symbols", {})

    sealed_days = sorted(
        date.fromisoformat(value)
        for value in manifest["trading_days"]["trading_days"]
        if WINDOW_START <= date.fromisoformat(value) <= WINDOW_END
    )
    universe_by_session: dict[date, list[str]] = {
        date.fromisoformat(key): list(value)
        for key, value in manifest["universe"].items()
    }

    still_listed = {
        candidate.symbol
        for candidate in (
            *INDEX_CANDIDATE_CATALOG,
            *HISTORICAL_INDEX_CANDIDATE_CATALOG,
        )
        if INDEX_MEMBERSHIP_HISTORY.is_active(candidate, WINDOW_END)
    }

    minute_cache: dict[str, dict[datetime, Any]] = {}
    daily_cache: dict[str, list[_CandleView]] = {}
    trades: list[dict[str, Any]] = []
    session_rows: list[dict[str, Any]] = []
    unresolved_sessions: list[str] = []
    auditable_sessions = 0
    member_days_missing = 0
    still_listed_missing = 0
    member_days_total = 0

    for session_date in sealed_days:
        session_open = _session_open_utc(session_date)
        universe = universe_by_session.get(session_date, [])
        minute_by_symbol: dict[str, dict[datetime, Any]] = {}
        adv_by_symbol: dict[str, float] = {}
        member_day_missing_this_session = 0
        auditable = True
        for symbol in universe:
            member_days_total += 1
            if symbol not in minute_cache:
                minute_cache[symbol] = _load_minute_bars(cache_dir, symbol)
            if symbol not in daily_cache:
                daily_cache[symbol] = _load_daily_bars(cache_dir, symbol)
            bars = minute_cache[symbol]
            adv = rebuild_session_adv(
                _daily_bar_rows(daily_cache[symbol]),
                as_of=session_date,
            )
            minute_by_symbol[symbol] = bars
            if adv is not None:
                adv_by_symbol[symbol] = adv
            has_bars = bool(bars)
            has_inputs = _member_has_decision_inputs(
                bars, session_open=session_open
            )
            permanent_absence = (
                symbol_states.get(symbol, {})
                .get("minute", {})
                .get("state")
                == "PERMANENT_FAILURE"
            )
            if not has_bars:
                member_days_missing += 1
                member_day_missing_this_session += 1
                if symbol in still_listed:
                    still_listed_missing += 1
                if not permanent_absence:
                    auditable = False
            elif not has_inputs or adv is None:
                auditable = False
        if auditable:
            auditable_sessions += 1

        decision = evaluate_session_decision(
            universe=universe,
            minute_bars_by_symbol=minute_by_symbol,
            adv_by_symbol=adv_by_symbol,
            session_open=session_open,
        )
        if decision.status != "OPEN":
            session_rows.append(
                {
                    "session_date": session_date.isoformat(),
                    "status": "SKIPPED",
                    "reason": decision.reason,
                    "candidate_symbol": decision.candidate_symbol,
                }
            )
            continue
        candidate = decision.candidate_symbol
        entry_price = decision.entry_price
        stop_loss_pct = decision.stop_loss_pct
        if (
            candidate is None
            or entry_price is None
            or stop_loss_pct is None
        ):
            raise HistoricalReplayError(
                "open decision is missing entry evidence: "
                f"{session_date.isoformat()}"
            )
        settled = settle_session_exit(
            minute_by_symbol[candidate],
            session_open=session_open,
            entry_price=entry_price,
            stop_loss_pct=stop_loss_pct,
        )
        if settled is None:
            unresolved_sessions.append(session_date.isoformat())
            session_rows.append(
                {
                    "session_date": session_date.isoformat(),
                    "status": "UNRESOLVED_EXIT",
                    "reason": "EXIT_PATH_INCOMPLETE",
                    "candidate_symbol": candidate,
                }
            )
            continue
        trades.append(
            {
                "session_date": session_date.isoformat(),
                "symbol": candidate,
                "entry_price": entry_price,
                "stop_loss_pct": stop_loss_pct,
                "exit_price": settled.exit_price,
                "exit_reason": settled.exit_reason,
                "gross_return_bps": settled.gross_return_bps,
                "net_return_bps": settled.net_return_bps,
            }
        )
        session_rows.append(
            {
                "session_date": session_date.isoformat(),
                "status": "CLOSED",
                "reason": settled.exit_reason,
                "candidate_symbol": candidate,
            }
        )

    observations = [
        (
            date.fromisoformat(trade["session_date"]),
            float(trade["net_return_bps"]),
        )
        for trade in trades
    ]
    stat = week_clustered_statistic(observations)
    gates = IntegrityGates(
        expected_sessions=len(sealed_days),
        auditable_sessions=auditable_sessions,
        member_days_total=member_days_total,
        member_days_missing_data=member_days_missing,
        still_listed_missing_member_days=still_listed_missing,
        unresolved_exit_sessions=len(unresolved_sessions),
    )
    verdict = decide_verdict(
        gates=gates, stat=stat, trade_count=len(trades)
    )
    doc_path = _repo_root() / "backend" / _PLAN_DOC_RELATIVE_PATH
    payload: dict[str, Any] = {
        "analysis_id": ANALYSIS_ID,
        "verdict": verdict.verdict,
        "verdict_reasons": list(verdict.reasons),
        "verdict_statement": (
            "this is NOT a forward PASS; a nominal p here is not a strict "
            "independent error rate; no trades are added to the forward "
            "cohort"
        ),
        "upper_30_negative": verdict.upper_30_negative,
        "gates": {
            "passed": gates.passed,
            "failures": list(gates.failures()),
            "session_input_coverage": gates.session_input_coverage,
            "member_day_missing_share": gates.member_day_missing_share,
            "still_listed_missing_member_days": (
                gates.still_listed_missing_member_days
            ),
            "unresolved_exit_sessions": gates.unresolved_exit_sessions,
        },
        "statistics": {
            "n": stat.n,
            "weeks": stat.weeks,
            "mean_net_bps_30": stat.mean_bps,
            "clustered_standard_error_bps": stat.standard_error_bps,
            "t_critical_one_sided_95": stat.t_critical,
            "L30": stat.lower_30,
            "U30": stat.upper_30,
            "L50": stat.lower_50,
            "U50": stat.upper_50,
            "stress_bps": STRESS_COST_BPS,
        },
        "sample_minimums": {"trades": MIN_TRADES, "weeks": MIN_WEEKS},
        "sessions": {
            "expected": len(sealed_days),
            "rows": session_rows,
        },
        "trades": trades,
        "descriptive": compute_descriptives(observations),
        "calendar_cross_check": manifest.get("calendar_cross_check"),
        "provenance": {
            "plan_doc_sha256": (
                _file_sha256(doc_path) if doc_path.exists() else None
            ),
            "git_head": _git_head(),
            "cli_source_sha256": _file_sha256(Path(__file__).resolve()),
            "input_manifest_sha256": manifest_sha256,
            "input_files": len(manifest["files"]),
            "frozen_config_version": frozen_config_version(),
            "cli_version": REPLAY_CLI_VERSION,
        },
        "rerun_reason": rerun_reason,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(output_path, payload)
    return payload


# --------------------------------------------------------------------- CLI


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "date must be YYYY-MM-DD"
        ) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opening_momentum_historical_replay",
        description=(
            "Registered retrospective validation of the frozen "
            "INDEX_CATALOG_STOCKS_IN_PLAY_ORB_TOP10 rule on "
            "2023-09-01..2026-04-30 (record-only; never orders)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser(
        "plan",
        help="offline: PIT universe, fetch intervals, request estimates",
    )
    plan_parser.add_argument("--output", type=Path, required=True)

    fetch_parser = subparsers.add_parser(
        "fetch", help="QuoteContext-only fetch into the cache"
    )
    fetch_parser.add_argument("--cache-dir", type=Path, default=None)
    fetch_parser.add_argument("--plan", type=Path, required=True)
    fetch_parser.add_argument("--rate", type=float, default=None)

    seal_parser = subparsers.add_parser(
        "seal", help="write the sealed input manifest and print its hash"
    )
    seal_parser.add_argument("--cache-dir", type=Path, default=None)

    evaluate_parser = subparsers.add_parser(
        "evaluate", help="one-pass evaluation requiring the sealed manifest"
    )
    evaluate_parser.add_argument("--cache-dir", type=Path, default=None)
    evaluate_parser.add_argument("--output", type=Path, default=None)
    evaluate_parser.add_argument("--rerun-reason", default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            payload = build_plan_payload(
                window_start=WINDOW_START, window_end=WINDOW_END
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(args.output, payload)
            print(
                json.dumps(
                    {
                        "distinct_symbols": payload["distinct_symbols"],
                        "member_days": payload["member_days"],
                        "request_estimates": payload["request_estimates"],
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                    indent=2,
                )
            )
            return 0
        if args.command == "fetch":
            cache_dir = (
                args.cache_dir
                if args.cache_dir is not None
                else _default_cache_dir()
            )
            plan_payload = _load_plan(args.plan)
            provider = _LongportQuoteProvider(settings)

            def _sleep(seconds: float) -> None:
                if seconds > 0:
                    import time as _time

                    _time.sleep(seconds)

            report = run_fetch(
                cache_dir=cache_dir,
                plan_payload=plan_payload,
                provider=provider,
                clock=lambda: datetime.now(timezone.utc),
                sleep=_sleep,
                rate_per_second=(
                    args.rate
                    if args.rate is not None
                    else DEFAULT_REQUESTS_PER_SECOND
                ),
            )
            print(json.dumps(report, ensure_ascii=True, sort_keys=True))
            if report["errors"]["transient"]:
                return 1
            return 0
        if args.command == "seal":
            cache_dir = (
                args.cache_dir
                if args.cache_dir is not None
                else _default_cache_dir()
            )
            manifest_hash = run_seal(cache_dir)
            print(manifest_hash)
            return 0
        if args.command == "evaluate":
            cache_dir = (
                args.cache_dir
                if args.cache_dir is not None
                else _default_cache_dir()
            )
            output_path = (
                args.output
                if args.output is not None
                else cache_dir / "output" / "result.json"
            )
            payload = run_evaluate(
                cache_dir=cache_dir,
                output_path=output_path,
                rerun_reason=args.rerun_reason,
            )
            print(
                json.dumps(
                    {
                        "analysis_id": payload["analysis_id"],
                        "verdict": payload["verdict"],
                        "n": payload["statistics"]["n"],
                        "weeks": payload["statistics"]["weeks"],
                        "git_head": payload["provenance"]["git_head"],
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                )
            )
            return 0
        parser.error(f"unknown command: {args.command}")
        return 2
    except (HistoricalReplayError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
