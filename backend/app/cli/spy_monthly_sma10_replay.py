"""Registered research replay CLI for the frozen monthly-trend rule
``SPY_MONTHLY_SMA10_CASH_V1``.

Governance contract: ``app/domain/SPY_MONTHLY_SMA10_PREREGISTRATION.md``.

This is a NEW research lane, not an order path: nothing here touches the
DB, the runner, ``BrokerGateway`` or any trade service.  Safety rules
mirroring the hardened ORB replay (``opening_momentum_historical_replay.py``,
imported - never copy-edited):

- ``fetch`` uses a QuoteContext ONLY (never TradeContext, never
  BrokerGateway), one worker, default 0.5 req/s, hard pause window
  13:00-22:00 UTC Mon-Fri;
- provider errors 301607 (quota) / 301604 (permission) cause an
  immediate GLOBAL stop with no retry; 301600 request-shape refusals
  fail cleanly; explicit invalid-symbol answers are per-symbol permanent
  failures; transient errors get bounded retries;
- RAW (NoAdjust) daily bars only for SPY.US / QQQ.US: share counts,
  notionals and fixed fees must never see adjusted prices;
- corporate actions (dividends/splits) come from a SEALED, hashed
  ``corporate_actions.json`` imported via the separate
  ``import-corporate-actions`` subcommand (source URL + sha256).  seal
  refuses without it (DATA_BLOCKED);
- the trading calendar is SPY n QQQ sealed daily bars (as in the ORB
  replay's benchmark intersection), never the local holiday calendar
  (it does not cover 2012-2021);
- evaluate runs ONCE: exclusive attempt claim, ``--output`` required and
  outside the cache, clean worktree, rerun needs a reason, small receipt
  next to the output.

The rule itself lives in the PURE module
``app.domain.monthly_trend.sma10``; this CLI is glue only.
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
import zipfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from app.config import Settings, settings
from app.core.accounting_fees import (
    SEC98_FIXED_USD,
    SEC98_NOTIONAL_RATE,
)
from app.domain.monthly_trend.nyse_calendar import (
    expected_nyse_sessions,
)
from app.domain.monthly_trend.sma10 import (
    BASE_SLIPPAGE_BPS,
    BOOTSTRAP_CONFIG,
    CLAIM1_THRESHOLD,
    CLAIM2_THRESHOLD,
    CLAIM3_THRESHOLD,
    CLAIM4_THRESHOLD,
    INITIAL_CASH_USD,
    MAX_ENTRY_NOTIONAL_USD,
    MAX_SHARES_PER_ENTRY,
    MIN_CASH_MONTHS,
    MIN_INVESTED_MONTHS,
    REQUIRED_MONTHS,
    STRESS_SLIPPAGE_BPS,
    VERDICT_CORROBORATES_RISK_MANAGEMENT_VALUE,
    VERDICT_DATA_BLOCKED,
    VERDICT_DOES_NOT_CORROBORATE,
    VERDICT_INCONCLUSIVE,
    VERDICT_INSUFFICIENT_DATA,
    BootstrapConfig,
    ClaimEvaluations,
    CorporateAction,
    DailyPriceRow,
    DividendCredit,
    MonthRecord,
    MonthlyReplayBlockedError,
    MonthlyTrendError,
    SampleGates,
    SleeveResult,
    TradeFill,
    build_signal_index,
    circular_block_bootstrap_bounds,
    decide_verdict,
    descriptive_statistics,
    detect_ohlc_anomalies,
    downside_series,
    entry_share_cap,
    evaluate_claims,
    is_constant,
    month_end_index_levels,
    month_end_sessions,
    monthly_return_series,
    next_session_after,
    sample_gates_from_records,
    simulate_leg,
    sleeve_entry_exit_events,
    sma10_signal,
    screen_unexplained_split_anomalies,
    validate_window_data,
)

# Reuse the hardened ORB replay machinery by IMPORT (contract reuse
# clause): throttle, pause windows, error classification, atomic writes,
# file hashing, the clean-worktree check, the exclusive attempt claim
# and the plan digest.  The ORB-specific helpers (ADV, universe, minute
# retention) are NOT reused - they are tied to ORB semantics, so small
# new equivalents live in this module instead.
from app.cli.opening_momentum_historical_replay import (
    DEFAULT_MAX_TRANSIENT_RETRIES,
    DEFAULT_REQUESTS_PER_SECOND,
    HistoricalReplayError,
    _RetryableProvider,
    _Throttle,
    _atomic_write_json,
    _claim_next_attempt,
    _file_sha256,
    _load_attempt_receipt,
    _proc_cmdline,
    _read_gzip_json,
    _require_clean_worktree,
    _sealed_plan_digest,
    _verify_sealed_file,
    _write_attempt_receipt,
)


# ---------------------------------------------------------------- frozen plan

ANALYSIS_ID = "spy-monthly-sma10-cash-v3"
REPLAY_CLI_VERSION = "spy-monthly-sma10-cash-replay-cli-v1"
RULE_NAME = "SPY_MONTHLY_SMA10_CASH_V1"

INSTRUMENT = "SPY.US"
DISCLOSURE_BENCHMARK = "QQQ.US"
CALENDAR_SYMBOLS: tuple[str, str] = (INSTRUMENT, DISCLOSURE_BENCHMARK)

#: Sealed RAW data span (contract data contract): 2010-06-01..2022-01-31.
DATA_START = date(2010, 6, 1)
DATA_END = date(2022, 1, 31)
#: Primary scoring window: 2012-01..2021-12 (120 complete months).
WINDOW_START_MONTH = (2012, 1)
WINDOW_END_MONTH = (2021, 12)
WARMUP_MONTHS_START = (2010, 6)
WARMUP_MONTHS_END = (2011, 12)

#: Costs (pinned to the core accounting constants by test).
COMMISSION_FIXED_USD = SEC98_FIXED_USD
COMMISSION_NOTIONAL_RATE = SEC98_NOTIONAL_RATE

#: Dividend withholding sensitivity (disclosure only).
DIVIDEND_WITHHOLDING_RATE = 0.30

PAGE_SIZE = 1000
#: Decision 14.11: a NEW cache dir for the v3 attempt.  The v2 cache
#: (``spy_monthly_sma10_v1``, whose seal was REFUSED with DATA_BLOCKED)
#: stays untouched with its original inputs - a re-bind would have
#: required mutating the v2-bound plan.json ``analysis_id`` in place,
#: which is exactly the kind of historical rewrite this lane refuses.
#: v3 re-fetches (6 requests) into a fresh directory.
_CACHE_DIR_NAME = Path("data/research/spy_monthly_sma10_v3")
_PLAN_DOC_RELATIVE_PATH = Path("app/domain/SPY_MONTHLY_SMA10_PREREGISTRATION.md")

#: Decision 14.11: the sealed OHLC anomaly ledger - a PINNED, committed
#: data file carrying hashes and facts only (never prices).  Seal
#: validates it against the registered constants and hashes it into
#: the manifest; evaluate re-derives the violation set from the sealed
#: bars and requires it to EQUAL the ledger exactly before claiming
#: the attempt.
_ANOMALY_LEDGER_PATH = (
    Path(__file__).resolve().parents[1]
    / "domain"
    / "monthly_trend"
    / "data"
    / "ohlc_anomaly_ledger.json"
)
OHLC_ANOMALY_LEDGER_SYMBOL = "SPY.US"
OHLC_ANOMALY_LEDGER_SESSION = date(2020, 11, 18)
OHLC_ANOMALY_LEDGER_RELATION = "open > high"
OHLC_ANOMALY_LEDGER_CAP = 1

PAUSE_WINDOW_START_UTC = time(13, 0)
PAUSE_WINDOW_END_UTC = time(22, 0)

#: The provider speaks "DAY"; reuse of the ORB pagination needs no
#: period mapping here because this replay only ever requests DAY bars.


class MonthlySma10Error(RuntimeError):
    """Refusal or aborted replay operation (fail-closed)."""


def _fail(message: str) -> MonthlySma10Error:
    return MonthlySma10Error(message)


# ---------------------------------------------------------------- helpers


def _default_cache_dir() -> Path:
    return Path(__file__).resolve().parents[2] / _CACHE_DIR_NAME


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


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


def _month_range(
    start: tuple[int, int], end: tuple[int, int]
) -> list[tuple[int, int]]:
    months: list[tuple[int, int]] = []
    current = start
    while current <= end:
        months.append(current)
        year, month = current
        current = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


SCORING_MONTHS: tuple[tuple[int, int], ...] = tuple(
    _month_range(WINDOW_START_MONTH, WINDOW_END_MONTH)
)
WARMUP_MONTHS: tuple[tuple[int, int], ...] = tuple(
    _month_range(WARMUP_MONTHS_START, WARMUP_MONTHS_END)
)


# ------------------------------------------------------------------- plan


def build_plan_payload() -> dict[str, Any]:
    """Offline, returns-blind plan (no data touched)."""

    return {
        "analysis_id": ANALYSIS_ID,
        "cli_version": REPLAY_CLI_VERSION,
        "rule": RULE_NAME,
        "instrument": INSTRUMENT,
        "disclosure_benchmark": DISCLOSURE_BENCHMARK,
        "data_window": {
            "start": DATA_START.isoformat(),
            "end": DATA_END.isoformat(),
            "adjustment": "NoAdjust",
            "note": (
                "RAW daily OHLC only; ForwardAdjust prices are never an "
                "executable account; corporate actions come from the "
                "sealed corporate_actions.json"
            ),
        },
        "scoring_window": {
            "start_month": f"{WINDOW_START_MONTH[0]:04d}-{WINDOW_START_MONTH[1]:02d}",
            "end_month": f"{WINDOW_END_MONTH[0]:04d}-{WINDOW_END_MONTH[1]:02d}",
            "months": len(SCORING_MONTHS),
        },
        "warmup": {
            "start_month": f"{WARMUP_MONTHS_START[0]:04d}-{WARMUP_MONTHS_START[1]:02d}",
            "end_month": f"{WARMUP_MONTHS_END[0]:04d}-{WARMUP_MONTHS_END[1]:02d}",
            "note": "warm-up only; the SMA needs 10 month-ends",
        },
        "costs": {
            "commission_fixed_usd": str(COMMISSION_FIXED_USD),
            "commission_notional_rate": str(COMMISSION_NOTIONAL_RATE),
            "commission_source": (
                "app/core/accounting_fees.py SEC98_FIXED_USD / "
                "SEC98_NOTIONAL_RATE (imported)"
            ),
            "base_slippage_bps": BASE_SLIPPAGE_BPS,
            "stress_slippage_bps": STRESS_SLIPPAGE_BPS,
            "dividend_withholding_rate": DIVIDEND_WITHHOLDING_RATE,
        },
        "sleeve": {
            "initial_cash_usd": str(INITIAL_CASH_USD),
            "max_shares_per_entry": MAX_SHARES_PER_ENTRY,
            "max_entry_notional_usd": str(MAX_ENTRY_NOTIONAL_USD),
        },
        "statistics": {
            "bootstrap_block_length": BOOTSTRAP_CONFIG.block_length,
            "bootstrap_resamples": BOOTSTRAP_CONFIG.resamples,
            "bootstrap_seed": BOOTSTRAP_CONFIG.seed,
            "required_months": REQUIRED_MONTHS,
            "min_cash_months": MIN_CASH_MONTHS,
            "min_invested_months": MIN_INVESTED_MONTHS,
        },
        "verdicts": [
            VERDICT_CORROBORATES_RISK_MANAGEMENT_VALUE,
            VERDICT_DOES_NOT_CORROBORATE,
            VERDICT_INCONCLUSIVE,
            VERDICT_INSUFFICIENT_DATA,
            VERDICT_DATA_BLOCKED,
        ],
        "symbols": [
            {"symbol": symbol, "period": "DAY", "adjustment": "NoAdjust"}
            for symbol in CALENDAR_SYMBOLS
        ],
        "estimated_requests_total": 4,
        "planning_note": (
            "the trading calendar is derived from SPY n QQQ sealed daily "
            "bars (benchmark intersection as in the ORB replay); the "
            "local holiday calendar does not cover 2012-2021 and is "
            "never used here"
        ),
        "registration_honesty": (
            "no local research artefact or DB row covers SPY/QQQ in "
            "2012-2021 (audited 2026-09-28: 0 hits in data/research, 0 "
            "DB rows before 2022, no prior SMA10/Faber code or docs); "
            "this is NOT proof nobody has looked at those years - Faber "
            "(2007) published the rule on older data and 2012-2021 is "
            "post-publication OOS relative to that paper; LongPort US "
            "daily bars start 2010-06, so 2000/2008 cannot be tested"
        ),
    }


# ------------------------------------------------------------------- fetch


class _MonthlyCandleView:
    """Minimal attribute carrier shaped like the ORB ``_CandleView``."""

    def __init__(
        self, timestamp: datetime, open_: float, high: float, low: float,
        close: float, volume: float | None, turnover: float | None,
    ) -> None:
        self.timestamp = timestamp
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.turnover = turnover


class _LongportQuoteProvider:
    """QuoteContext-only adapter (env credentials; never TradeContext).

    A NEW small adapter rather than importing the ORB one: the ORB class
    pins ORB-period mapping behaviour and is part of that pinned module;
    instantiating it from here would still be legal, but its error
    surface references the ORB CLI.  This one only ever fetches DAY bars
    with NoAdjust.
    """

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
            raise _fail(
                "Longport credentials are unavailable: " + ", ".join(missing)
            )
        for name, value in credentials.items():
            os.environ[name] = value
        try:
            import longport.openapi as openapi
        except ImportError as exc:  # pragma: no cover - SDK present
            raise _fail("longport SDK is not installed") from exc
        config = openapi.Config.from_env()
        # QuoteContext ONLY: building a TradeContext is forbidden here.
        self._quote_ctx = openapi.QuoteContext(config)
        self._openapi = openapi

    def history_candlesticks_by_offset(
        self,
        symbol: str,
        period: str,
        *,
        count: int,
        after: datetime,
        forward: bool,
        adjustment: str,
    ) -> list[_MonthlyCandleView]:
        if period != "DAY":
            raise _fail(f"unsupported period for this replay: {period}")
        if adjustment != "NoAdjust":
            raise _fail(
                "this replay seals RAW bars only; refusing adjustment "
                f"{adjustment}"
            )
        adjust_type = getattr(self._openapi.AdjustType, adjustment)
        boundary = after
        if boundary.tzinfo is not None:
            boundary = boundary.astimezone(timezone.utc)
        response = self._quote_ctx.history_candlesticks_by_offset(
            symbol,
            self._openapi.Period.Day,
            adjust_type,
            forward,
            count,
            boundary,
        )
        views: list[_MonthlyCandleView] = []
        for item in response:
            timestamp = item.timestamp
            if not isinstance(timestamp, datetime):
                raise _fail("provider candle timestamp is not a datetime")
            views.append(
                _MonthlyCandleView(
                    timestamp=timestamp.astimezone(timezone.utc),
                    open_=float(item.open),
                    high=float(item.high),
                    low=float(item.low),
                    close=float(item.close),
                    volume=(
                        float(item.volume)
                        if item.volume is not None
                        else None
                    ),
                    turnover=(
                        float(item.turnover)
                        if item.turnover is not None
                        else None
                    ),
                )
            )
        return views


def _page_forward_daily_local(
    retryable: _RetryableProvider,
    *,
    symbol: str,
    first_boundary: datetime,
    stop_boundary: datetime,
    page_size: int = PAGE_SIZE,
) -> list[Any]:
    """Forward daily pagination for THIS replay (item 10/12 hygiene).

    Written locally instead of importing the ORB ``_page_forward_daily``
    because the ORB cursor advance (``latest + 1 day``) DROPS the first
    session of each subsequent page under this replay's strictly-after
    provider semantics (reproduced: 2014-05-21 and 2018-05-11 lost at
    the 1000-bar page boundaries).  This loop advances ``cursor =
    latest`` (strictly-after boundary), which loses nothing; ORB files
    stay untouched.
    """

    retained: dict[datetime, Any] = {}
    cursor = first_boundary
    empty_pages = 0
    while cursor < stop_boundary:
        boundary = cursor
        if boundary.tzinfo is not None:
            boundary = boundary.astimezone(timezone.utc)

        def _fetch() -> list[Any]:
            return retryable._provider.history_candlesticks_by_offset(
                symbol, "DAY",
                count=page_size, after=boundary, forward=True,
                adjustment="NoAdjust",
            )

        page = retryable.call(_fetch)
        if not page:
            empty_pages += 1
            if empty_pages >= 2:
                break
            cursor += timedelta(days=1)
            continue
        empty_pages = 0
        for bar in page:
            retained[bar.timestamp] = bar
        latest = max(bar.timestamp for bar in page)
        if latest <= cursor:
            cursor += timedelta(days=1)
        else:
            cursor = latest
        if latest >= stop_boundary:
            break
    return [retained[key] for key in sorted(retained)]


def _load_status(cache_dir: Path) -> dict[str, Any]:
    path = cache_dir / "status.json"
    if not path.exists():
        return {
            "version": REPLAY_CLI_VERSION,
            "global_stop": None,
            "symbols": {},
            "requests_total": 0,
        }
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise _fail("status.json is not an object")
    return raw


def _save_status(cache_dir: Path, status: dict[str, Any]) -> None:
    _atomic_write_json(cache_dir / "status.json", status)


def _symbol_state(status: dict[str, Any], symbol: str) -> dict[str, Any]:
    symbols = status.setdefault("symbols", {})
    if symbol not in symbols:
        symbols[symbol] = {"daily": {"state": "PENDING"}}
    return symbols[symbol]


def _write_bars_file(
    cache_dir: Path,
    symbol: str,
    *,
    bars: Sequence[tuple[str, float, float, float, float, float | None, float | None]],
) -> str:
    path = cache_dir / "daily" / f"{symbol}.json.gz"
    _atomic_write_gzip_json(
        path,
        {
            "symbol": symbol,
            "period": "DAY",
            "adjustment": "NoAdjust",
            "bars": [list(row) for row in bars],
        },
    )
    return _file_sha256(path)


def _session_date_of(timestamp: datetime) -> date:
    # US daily bars: the provider timestamp is the session open moment;
    # the New York calendar date IS the session date.
    from zoneinfo import ZoneInfo

    return timestamp.astimezone(ZoneInfo("America/New_York")).date()




def run_fetch(
    *,
    cache_dir: Path,
    plan_payload: dict[str, Any],
    provider: Any,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    rate_per_second: float = DEFAULT_REQUESTS_PER_SECOND,
    max_transient_retries: int = DEFAULT_MAX_TRANSIENT_RETRIES,
) -> dict[str, Any]:
    """Fetch RAW SPY/QQQ daily bars into the cache; prints no prices."""

    status = _load_status(cache_dir)
    if isinstance(status.get("global_stop"), dict):
        raise _fail(
            "fetch refused: GLOBAL STOP is active since "
            f"{status['global_stop'].get('at')} "
            f"({status['global_stop'].get('reason')}); manual review is "
            "required before any further provider call"
        )
    if plan_payload.get("analysis_id") != ANALYSIS_ID:
        raise _fail(
            "plan file analysis_id does not match the frozen replay"
        )
    # ---- Item 10: the fetch is BOUND to the frozen plan.  The full
    # canonical plan must equal the frozen ``build_plan_payload`` output
    # BEFORE the first provider request, and the bound plan is written
    # into the cache; a resume must present the SAME plan (digest
    # equality), otherwise the cache belongs to a different plan.
    frozen = build_plan_payload()
    if _sealed_plan_digest(plan_payload) != _sealed_plan_digest(frozen):
        raise _fail(
            "fetch refused: the plan is not the frozen registered plan "
            "(canonical digest mismatch); fetch only executes the "
            "registered plan"
        )
    bound_plan_path = cache_dir / "plan.json"
    if bound_plan_path.exists():
        bound = json.loads(
            bound_plan_path.read_text(encoding="utf-8")
        )
        if (
            _sealed_plan_digest(bound)
            != _sealed_plan_digest(plan_payload)
        ):
            raise _fail(
                "fetch refused: the cache holds a DIFFERENT bound plan "
                "(digest mismatch); a resume must present the same plan"
            )
    else:
        _atomic_write_json(bound_plan_path, plan_payload)
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
    symbols = [
        str(entry["symbol"])
        for entry in plan_payload["symbols"]
    ]
    failures: dict[str, str] = {}
    permanent: dict[str, str] = {}
    symbol_hint = "?"
    try:
        for symbol in symbols:
            symbol_hint = symbol
            state = _symbol_state(status, symbol)
            if state["daily"]["state"] in ("COMPLETE", "PERMANENT_FAILURE"):
                continue
            bars = _page_forward_daily_local(
                retryable,
                symbol=symbol,
                first_boundary=datetime(
                    DATA_START.year, DATA_START.month, DATA_START.day,
                    tzinfo=timezone.utc,
                ) - timedelta(days=5),
                stop_boundary=datetime(
                    DATA_END.year, DATA_END.month, DATA_END.day,
                    tzinfo=timezone.utc,
                ) + timedelta(days=5),
            )
            rows = [
                (
                    _session_date_of(bar.timestamp).isoformat(),
                    float(bar.open),
                    float(bar.high),
                    float(bar.low),
                    float(bar.close),
                    float(bar.volume) if bar.volume is not None else None,
                    float(bar.turnover) if bar.turnover is not None else None,
                )
                for bar in bars
                if DATA_START <= _session_date_of(bar.timestamp) <= DATA_END
            ]
            if not rows:
                raise _fail(
                    f"provider returned no RAW daily bars for {symbol} "
                    "inside the registered window"
                )
            digest = _write_bars_file(cache_dir, symbol, bars=rows)
            state["daily"] = {
                "state": "COMPLETE",
                "sha256": digest,
                "bars": len(rows),
            }
            status["requests_total"] = (
                status.get("requests_total", 0) + throttle.total_requests
            )
            throttle.total_requests = 0
            _save_status(cache_dir, status)
    except Exception as exc:
        error_class = getattr(exc, "error_class", None)
        if isinstance(error_class, str):
            detail = str(getattr(exc, "message", exc))[:400]
            symbol_name = str(getattr(exc, "symbol", symbol_hint))
            if error_class in (
                "GLOBAL_STOP_QUOTA",
                "GLOBAL_STOP_PERMISSION",
            ):
                status["global_stop"] = {
                    "reason": error_class,
                    "detail": detail,
                    "at": clock().isoformat(),
                }
                status.setdefault("errors", []).append({
                    "stage": "DAILY_FETCH",
                    "error_class": error_class,
                    "detail": detail,
                    "at": clock().isoformat(),
                })
                status["requests_total"] = (
                    status.get("requests_total", 0) + throttle.total_requests
                )
                _save_status(cache_dir, status)
                raise _fail(
                    f"GLOBAL STOP ({error_class}); no retry: {detail}"
                ) from exc
            if error_class == "PERMANENT_SYMBOL":
                permanent[symbol_name] = detail
                # Item 10: durable, resumable permanent failure.
                stored_state = _symbol_state(status, symbol_name)
                stored_state["daily"] = {
                    "state": "PERMANENT_FAILURE",
                    "error": detail,
                }
            elif error_class == "REQUEST_SHAPE":
                status.setdefault("errors", []).append({
                    "stage": "DAILY_FETCH",
                    "error_class": error_class,
                    "detail": detail,
                    "at": clock().isoformat(),
                })
                status["requests_total"] = (
                    status.get("requests_total", 0) + throttle.total_requests
                )
                _save_status(cache_dir, status)
                raise _fail(
                    "request-shape refusal while fetching "
                    f"{symbol_name} ({detail}); the request must be "
                    "corrected, not retried"
                ) from exc
            else:
                failures[symbol_name] = detail
        status["requests_total"] = (
            status.get("requests_total", 0) + throttle.total_requests
        )
        _save_status(cache_dir, status)
        if isinstance(exc, MonthlySma10Error):
            raise
        raise _fail(f"daily fetch failed: {exc}") from exc
    return {
        "coverage": {
            "symbols_total": len(symbols),
            "symbols_complete": sum(
                1
                for symbol in symbols
                if _symbol_state(status, symbol)["daily"]["state"]
                == "COMPLETE"
            ),
            "symbols_permanent_failure": len(permanent),
            "symbols_failed_transient": len(failures),
        },
        "errors": {"permanent": permanent, "transient": failures},
        "quota": {
            "requests_total": status.get("requests_total", 0),
            "rate_per_second": rate_per_second,
        },
    }




# ------------------------------------------------- corporate actions import


CORPORATE_ACTIONS_FILENAME = "corporate_actions.json"

#: Registered dividend source (contract §14.5/§14.6): SSGA spdr
#: historical distributions workbook.  SPY has exactly 48 events in
#: 2010-2021 (one per quarter, each with ex/record/payable date +
#: numeric amount).  It has NO QQQ rows (QQQ is Invesco) -> QQQ is a
#: PRICE-ONLY disclosure benchmark.  It has NO split column -> splits
#: are proven absent by the RAW-price screen
#: (``screen_unexplained_split_discontinuities``).
REGISTERED_ACTIONS_SOURCE_URL = (
    "https://www.ssga.com/library-content/products/fund-data/"
    "etfs/us/spdr-etf-historical-distributions.xlsx"
)
#: FULL sha256 of the verified workbook (577,780 bytes, HTTP 200,
#: verified 2026-09-28).  The xlsx import path REFUSES any file whose
#: hash differs: this is a LIVING file on SSGA's side, and a future
#: revision must go through a registered change decision (contract
#: §14.6), never silently substitute.
REGISTERED_ACTIONS_SOURCE_SHA256 = (
    "51a16a450298a663b3fa088883a17c75f4464e87c911b0e83902a0ad46877c54"
)
#: Registered content fact, enforced at import AND re-verified at seal:
#: exactly 48 SPY dividend events with ex-date in this window, one per
#: calendar quarter, each with a pay date and a numeric amount > 0.
REGISTERED_ACTIONS_WINDOW_START = date(2010, 1, 1)
REGISTERED_ACTIONS_WINDOW_END = date(2021, 12, 31)
REGISTERED_SPY_WINDOW_EVENTS = 48


def validate_corporate_actions_payload(
    payload: Any,
) -> list[CorporateAction]:
    """Validate the sealed corporate-actions file shape (fail-closed).

    Required per entry: ``symbol``, ``ex_date``, and at least one of
    ``cash_amount`` (per-share, > 0) or ``ratio`` (> 0, != 1 treated as
    a split); optional ``pay_date``.  Missing/uncertain data raises
    MonthlyTrendError (the caller maps it to DATA_BLOCKED at seal).
    """

    if not isinstance(payload, dict):
        raise MonthlyTrendError("corporate actions file is not an object")
    entries = payload.get("actions")
    if not isinstance(entries, list):
        raise MonthlyTrendError(
            "corporate actions file has no 'actions' array"
        )
    actions: list[CorporateAction] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise MonthlyTrendError(f"action #{index} is not an object")
        symbol = entry.get("symbol")
        ex_date_raw = entry.get("ex_date")
        if not isinstance(symbol, str) or not symbol:
            raise MonthlyTrendError(f"action #{index} has no symbol")
        try:
            ex_date = date.fromisoformat(str(ex_date_raw))
        except (TypeError, ValueError) as exc:
            raise MonthlyTrendError(
                f"action #{index} has an invalid ex_date"
            ) from exc
        cash = entry.get("cash_amount")
        ratio = entry.get("ratio")
        if cash is None and ratio is None:
            raise MonthlyTrendError(
                f"action #{index} ({symbol} {ex_date.isoformat()}) has "
                "neither cash_amount nor ratio (uncertain data)"
            )
        if cash is not None and (
            not isinstance(cash, int | float)
            or not math.isfinite(float(cash))
            or float(cash) <= 0
        ):
            raise MonthlyTrendError(
                f"action #{index} has an invalid cash_amount"
            )
        if ratio is not None and (
            not isinstance(ratio, int | float)
            or not math.isfinite(float(ratio))
            or float(ratio) <= 0
        ):
            raise MonthlyTrendError(
                f"action #{index} has an invalid ratio"
            )
        pay_date_raw = entry.get("pay_date")
        pay_date = (
            date.fromisoformat(str(pay_date_raw))
            if pay_date_raw is not None
            else None
        )
        actions.append(
            CorporateAction(
                symbol=symbol,
                ex_date=ex_date,
                cash_amount=float(cash) if cash is not None else None,
                ratio=float(ratio) if ratio is not None else None,
                pay_date=pay_date,
            )
        )
    return actions


def _col_index(
    cell_ref: str,
) -> int:
    """'BC12' -> 54 (0-based column index, spreadsheet convention)."""

    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index - 1


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    """Parse xl/sharedStrings.xml (inline <t> runs concatenated)."""

    values: list[str] = []
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return values
    root = ET.fromstring(raw)
    for si in root.iter(f"{{http://schemas.openxmlformats.org/spreadsheetml/2006/main}}si"):
        text = "".join(
            t.text or "" for t in si.iter(
                f"{{http://schemas.openxmlformats.org/spreadsheetml/2006/main}}t"
            )
        )
        values.append(text)
    return values


def _xlsx_cell_value(
    cell: Any, shared: list[str]
) -> str | float | None:
    """Value of one cell; SPARSE cells (no <v>, e.g. a blank date)
    return None and the row-level checks turn that into a refusal."""
    if cell is None:
        return None
    kind = cell.get("t")
    if kind == "s":
        index_raw = cell.findtext(
            f"{{http://schemas.openxmlformats.org/spreadsheetml/2006/main}}v"
        )
        if index_raw is None:
            return None
        index = int(index_raw)
        if not 0 <= index < len(shared):
            raise _fail(f"shared string index out of range: {index}")
        return shared[index]
    if kind == "inlineStr":
        return "".join(
            t.text or ""
            for t in cell.iter(
                f"{{http://schemas.openxmlformats.org/spreadsheetml/"
                f"2006/main}}t"
            )
        )
    raw = cell.findtext(
        f"{{http://schemas.openxmlformats.org/spreadsheetml/2006/main}}v"
    )
    if raw is None:
        return None
    if kind == "str":
        return raw
    try:
        return float(raw)
    except ValueError:
        return raw


_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _parse_ssga_xlsx(
    file_path: Path,
    *,
    symbol_filter: str = "SPY",
) -> list[CorporateAction]:
    """Parse the SSGA spdr distributions xlsx (stdlib zipfile + xml only).

    Layout of the registered workbook (verified 2026-09-28 against the
    sealed source, sha256 prefix 51a16a45 / suffix 7c54): sheet 1 with
    header row FUND | EX-DATE | RECORD DATE | PAYABLE DATE | AMOUNT per
    share | ... rows sorted newest-first.  Returns the dividend actions
    for ``symbol_filter`` in chronological order.  The workbook has no
    split column; splits are handled by the RAW-price screen, never by
    this parser.
    """

    try:
        archive = zipfile.ZipFile(file_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise _fail(f"not a readable xlsx: {exc}") from exc
    with archive:
        shared = _xlsx_shared_strings(archive)
        try:
            sheet_xml = archive.read("xl/worksheets/sheet1.xml")
        except KeyError as exc:
            raise _fail(
                "xlsx has no xl/worksheets/sheet1.xml"
            ) from exc
    root = ET.fromstring(sheet_xml)
    # ---- locate the header row and its columns by NAME, never position
    header_cells: dict[str, int] = {}
    header_row: Any = None
    for row in root.iter(f"{_NS}row"):
        cells = list(row.iter(f"{_NS}c"))
        texts = {}
        for cell in cells:
            ref = cell.get("r", "")
            letters = "".join(ch for ch in ref if ch.isalpha())
            if not letters:
                raise _fail(
                    "xlsx header cell without a column reference"
                )
            texts[_col_index(letters)] = str(
                _xlsx_cell_value(cell, shared) or ""
            )
        lowered = {
            index: text.strip().lower()
            for index, text in texts.items()
        }
        # A header row is any row carrying a FUND(-NAME) or TICKER
        # column (the real workbook uses TICKER).
        has_identity = any(
            text in ("fund", "fund name", "fund ticker", "ticker")
            for text in lowered.values()
        )
        if has_identity:
            header_row = row
            for index, text in lowered.items():
                compact = " ".join(text.split())
                if compact in ("fund", "fund name", "fund ticker"):
                    header_cells["fund"] = index
                elif compact == "ticker":
                    # Decision 14.9: the REAL workbook identifies the
                    # fund by TICKER, not by fund name.
                    header_cells["ticker"] = index
                elif compact in (
                    "ex-date", "ex date", "ex_date", "ex-dividend date",
                ):
                    header_cells["ex_date"] = index
                elif compact in ("record date",):
                    header_cells["record_date"] = index
                elif compact in (
                    "payable date", "payment date", "pay date",
                ):
                    header_cells["pay_date"] = index
                elif "amount" in compact and "share" in compact:
                    header_cells["amount"] = index
                elif compact.startswith("dividend"):
                    # e.g. "DIVIDEND ($)" in the real workbook.
                    header_cells.setdefault("amount", index)
                elif "distribution rate" in compact:
                    header_cells.setdefault("rate", index)
            if "fund" in header_cells or "ticker" in header_cells:
                break
    if header_row is None or not (
        "fund" in header_cells or "ticker" in header_cells
    ):
        raise _fail(
            "could not locate a header row with a FUND/TICKER column "
            "in the xlsx"
        )
    for required in ("ex_date", "amount"):
        if required not in header_cells:
            raise _fail(
                f"xlsx header is missing the {required} column; found: "
                + ", ".join(sorted(header_cells))
            )
    # ---- data rows after the header, in document order
    seen_header = False
    actions: list[CorporateAction] = []
    for row in root.iter(f"{_NS}row"):
        if not seen_header:
            if row is header_row:
                seen_header = True
            continue
        cells = list(row.iter(f"{_NS}c"))
        by_column: dict[int, Any] = {}
        for cell in cells:
            ref = cell.get("r", "")
            letters = "".join(ch for ch in ref if ch.isalpha())
            if not letters:
                # Item 6: data rows must carry cell references; a cell
                # without one cannot be placed reliably -> refuse.
                raise _fail(
                    "xlsx data cell without a column reference"
                )
            by_column[_col_index(letters)] = cell
        identity_col = header_cells.get("ticker") or header_cells.get(
            "fund"
        )
        if identity_col is None:
            raise _fail(
                "xlsx header carries neither a TICKER nor a FUND column"
            )
        fund = _xlsx_cell_value(by_column.get(identity_col), shared)
        if fund is None:
            continue
        if str(fund).strip().upper() != symbol_filter.upper():
            continue  # e.g. other spdr funds; QQQ is absent (Invesco)
        ex_raw = _xlsx_cell_value(
            by_column.get(header_cells["ex_date"]), shared
        )
        amount_raw = _xlsx_cell_value(
            by_column.get(header_cells["amount"]), shared
        )
        pay_raw = (
            _xlsx_cell_value(by_column.get(header_cells["pay_date"]), shared)
            if "pay_date" in header_cells
            else None
        )
        actions.append(
            _action_from_ssga_row(ex_raw, amount_raw, pay_raw)
        )
    if not actions:
        raise _fail(
            f"the xlsx contains no {symbol_filter} rows; the registered "
            "source has 48 SPY events in 2010-2021"
        )
    return sorted(actions, key=lambda action: action.ex_date)


def _action_from_ssga_row(
    ex_raw: str | float | None,
    amount_raw: str | float | None,
    pay_raw: str | float | None,
) -> CorporateAction:
    """One CorporateAction from raw cell values (dates as Excel serials
    or ISO strings; amount numeric-only)."""

    def _as_date(value: str | float | None) -> date | None:
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            # Excel serial date (1900 system): days since 1899-12-30.
            serial = int(value)
            if serial <= 59:
                # the Excel 1900 leap-year bug offset; refuse rather
                # than guess (uncertain data is DATA_BLOCKED).
                raise _fail(
                    f"excel serial date too early to disambiguate: {value}"
                )
            return date(1899, 12, 30) + timedelta(days=serial)
        text = str(value).strip()
        # Decision 14.9: the REAL workbook carries MM/DD/YYYY strings.
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        try:
            return date.fromisoformat(text[:10])
        except ValueError as exc:
            raise _fail(
                f"unparseable date in the xlsx: {value!r}"
            ) from exc

    # The real workbook stores amounts as TEXT with padding (" 0.5 ").
    amount_text: str | float
    if isinstance(amount_raw, (int, float)):
        amount_text = amount_raw
    else:
        cleaned = str(amount_raw).strip().replace(",", "")
        if not cleaned:
            raise _fail(
                "dividend amount is empty in the xlsx row "
                f"(ex-date {_as_date(ex_raw)})"
            )
        try:
            amount_text = float(cleaned)
        except ValueError as exc:
            raise _fail(
                f"dividend amount is not numeric in the xlsx: "
                f"{amount_raw!r}"
            ) from exc
    amount = float(amount_text)
    if not math.isfinite(amount) or amount <= 0:
        raise _fail(f"invalid dividend amount in the xlsx: {amount!r}")
    ex_date = _as_date(ex_raw)
    if ex_date is None:
        raise _fail("a dividend row is missing its ex-date")
    pay_date = _as_date(pay_raw)
    if pay_date is not None and pay_date < ex_date:
        raise _fail(
            "xlsx dividend row has pay date before ex date "
            f"(ex {ex_date.isoformat()}, pay {pay_date.isoformat()})"
        )
    return CorporateAction(
        symbol="SPY.US",
        ex_date=ex_date,
        cash_amount=amount,
        ratio=None,
        pay_date=pay_date,
    )


def _verify_registered_content_fact(
    actions: Sequence[CorporateAction],
) -> None:
    """Enforce the registered 48-quarter SPY content fact (contract
    §14.6) at import time.

    SPY must have EXACTLY 48 dividend events with ex-date in
    2010-01-01..2021-12-31: one per calendar quarter (48 quarters = 12
    years x 4, no duplicates, none missing), each with a pay date and a
    numeric amount > 0 (the parser already rejects non-positive or
    non-numeric amounts).  Any deviation refuses - which maps to
    DATA_BLOCKED for the run.  Amount VALUES are never printed, only
    counts and quarters.
    """

    # Decision 14.9 item 1: the REAL workbook carries SPY rows well
    # outside the window (the SSGA file spans decades).  The import
    # SELECTS the registered window instead of rejecting the file for
    # having extra events.  The selection INCLUDES the 2010-2011
    # warm-up dividends (48 quarters = 2010Q1..2021Q4), NOT only 2012+.
    first_year = REGISTERED_ACTIONS_WINDOW_START.year
    last_year = REGISTERED_ACTIONS_WINDOW_END.year
    expected_quarters = {
        (year, quarter)
        for year in range(first_year, last_year + 1)
        for quarter in (1, 2, 3, 4)
        if REGISTERED_ACTIONS_WINDOW_START
        <= date(year, quarter * 3 - 2, 1)
        <= REGISTERED_ACTIONS_WINDOW_END
        or REGISTERED_ACTIONS_WINDOW_START
        <= date(year, quarter * 3, 28)
        <= REGISTERED_ACTIONS_WINDOW_END
    }
    seen_quarters: set[tuple[int, int]] = set()
    duplicates: list[tuple[int, int]] = []
    missing_pay: list[date] = []
    for action in actions:
        if action.cash_amount is None:
            continue
        if not (
            REGISTERED_ACTIONS_WINDOW_START
            <= action.ex_date
            <= REGISTERED_ACTIONS_WINDOW_END
        ):
            continue  # out-of-window row: SELECTED OUT, never fatal
        quarter = (action.ex_date.month - 1) // 3 + 1
        key = (action.ex_date.year, quarter)
        if key in seen_quarters:
            duplicates.append(key)
        seen_quarters.add(key)
        if action.pay_date is None:
            missing_pay.append(action.ex_date)
    if duplicates:
        rendered = ", ".join(f"{y}Q{q}" for y, q in duplicates[:4])
        raise _fail(
            "import-corporate-actions refused (DATA_BLOCKED): duplicate "
            f"calendar quarter(s) in the SPY dividend events: {rendered}"
        )
    if missing_pay:
        rendered = ", ".join(d.isoformat() for d in missing_pay[:4])
        raise _fail(
            "import-corporate-actions refused (DATA_BLOCKED): SPY "
            f"dividend event(s) without a pay date: {rendered}"
        )
    in_window_count = len(seen_quarters)
    if (
        in_window_count != REGISTERED_SPY_WINDOW_EVENTS
        or seen_quarters != expected_quarters
    ):
        raise _fail(
            "import-corporate-actions refused (DATA_BLOCKED): the "
            "registered fact is exactly "
            f"{REGISTERED_SPY_WINDOW_EVENTS} SPY dividend events in "
            f"{first_year}-01-01..{last_year}-12-31, one per calendar "
            f"quarter; the selection covers {in_window_count} quarters"
        )


def run_import_corporate_actions(
    *,
    cache_dir: Path,
    file_path: Path,
    source_url: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Hash, validate and store the sealed corporate-actions input.

    Accepts EITHER a JSON file (``{"actions": [...]}``, the pluggable
    hand-built shape from the original registration) OR the REGISTERED
    SSGA spdr distributions .xlsx (stdlib zipfile+xml parser; openpyxl
    is NOT a dependency).  SPY rows are extracted (ex/record/payable
    date + numeric per-share amount); the workbook has no QQQ rows
    (QQQ is Invesco -> price-only disclosure benchmark) and no split
    column (splits are proven absent by the RAW-price screen at
    evaluate time).  The stored file is canonical JSON regardless of
    input format; the integrity anchor reported back is the hash of the
    bytes ON DISK.
    """

    if not file_path.exists():
        raise _fail(f"corporate actions file not found: {file_path}")
    if not source_url:
        raise _fail("--source-url is required (provenance)")
    target = cache_dir / CORPORATE_ACTIONS_FILENAME
    if target.exists() and not reason:
        raise _fail(
            "import-corporate-actions refused: "
            f"{CORPORATE_ACTIONS_FILENAME} already exists; pass "
            "--reason to replace it (the previous file is preserved "
            "under corporate_actions_archive/)"
        )
    payload_bytes = file_path.read_bytes()
    is_zip = payload_bytes[:2] == b"PK"
    raw_spy_row_count: int | None = None
    selection_range: list[str] | None = None
    if is_zip:
        # Decision 14.6: the registered workbook is enforced by its
        # FULL sha256.  This is a LIVING file on SSGA's side - any
        # other bytes (a future revision included) need a registered
        # change decision, never a silent substitution.
        actual_hash = hashlib.sha256(payload_bytes).hexdigest()
        if actual_hash != REGISTERED_ACTIONS_SOURCE_SHA256:
            raise _fail(
                "import-corporate-actions refused: the xlsx sha256 "
                f"({actual_hash[:16]}...) does not match the registered "
                "workbook ("
                f"{REGISTERED_ACTIONS_SOURCE_SHA256[:16]}...); a "
                "different workbook revision needs a registered change "
                "decision in SPY_MONTHLY_SMA10_PREREGISTRATION.md "
                "(contract 14.6)"
            )
        # Decision 14.9 item 1: hash FIRST, then parse, then SELECT the
        # registered window (the real workbook has events outside it).
        raw_actions = _parse_ssga_xlsx(file_path)
        raw_spy_row_count = len(raw_actions)
        actions = [
            action
            for action in raw_actions
            if REGISTERED_ACTIONS_WINDOW_START
            <= action.ex_date
            <= REGISTERED_ACTIONS_WINDOW_END
        ]
        _verify_registered_content_fact(raw_actions)
        selection_range = [
            REGISTERED_ACTIONS_WINDOW_START.isoformat(),
            REGISTERED_ACTIONS_WINDOW_END.isoformat(),
        ]
        source_format = "ssga-distributions-xlsx"
        payload = {
            "actions": [
                {
                    "symbol": action.symbol,
                    "ex_date": action.ex_date.isoformat(),
                    "cash_amount": action.cash_amount,
                    "ratio": action.ratio,
                    "pay_date": (
                        action.pay_date.isoformat()
                        if action.pay_date
                        else None
                    ),
                }
                for action in actions
            ]
        }
    else:
        # The JSON path is SYNTHETIC-TESTS-ONLY (contract 14.6): real
        # runs must go through the registered xlsx.  It stays available
        # so the sealed-record shape and the seal re-verification can be
        # exercised without the real workbook, but it can never carry
        # the registered source format, so seal/evaluate refuse it.
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _fail(
                f"corporate actions file is not valid JSON or xlsx: {exc}"
            ) from exc
        actions = validate_corporate_actions_payload(payload)
        source_format = "json-synthetic-test-only"
    if target.exists() and reason:
        archive_dir = cache_dir / "corporate_actions_archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        index = 1
        archived = archive_dir / f"corporate_actions-{index}.json"
        while archived.exists():
            index += 1
            archived = archive_dir / f"corporate_actions-{index}.json"
        archived.write_bytes(target.read_bytes())
    stored = {
        "source_url": source_url,
        "source_format": source_format,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "source_file_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "bytes": len(payload_bytes),
        "actions_count": len(actions),
        "dividends": sum(
            1 for action in actions if action.cash_amount is not None
        ),
        "splits": sum(1 for action in actions if action.ratio is not None),
        "raw_spy_row_count": raw_spy_row_count,
        "selection_range": selection_range,
        "reason": reason,
        "payload": payload,
    }
    _atomic_write_json(target, stored)
    return {
        "actions": len(actions),
        "source_format": source_format,
        "raw_spy_row_count": raw_spy_row_count,
        # Integrity anchor: the hash of the bytes actually on disk (the
        # stored file is canonical JSON, so this is reproducible).
        "sha256": _file_sha256(target),
        "source_url": source_url,
        "path": str(target),
    }




# ------------------------------------------------------------ exclusive lock


_RUN_LOCK_NAME = "run.lock"


def _acquire_run_lock(cache_dir: Path) -> Any:
    """Acquire the run-wide exclusive lock (item 9).

    An ``O_CREAT | O_EXCL`` lockfile held for the WHOLE evaluation (not
    just the attempt claim).  A stale lock (whose owning pid is dead)
    is removable ONLY with an explicit reason recorded in the lock
    audit; otherwise a non-terminal previous attempt blocks any rerun.
    """

    import fcntl

    lock_path = cache_dir / _RUN_LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(
            lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644
        )
    except FileExistsError:
        raw: dict[str, Any] = {}
        try:
            raw = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        pid = int(raw.get("pid", 0) or 0)
        alive = pid > 0 and Path(f"/proc/{pid}").exists()
        attempt_state = str(raw.get("attempt_state", "?"))
        if alive:
            raise _fail(
                f"evaluate refused: another evaluation run is active "
                f"(pid {pid}, attempt state {attempt_state})"
            )
        raise _fail(
            "evaluate refused: a previous evaluation attempt is "
            f"non-terminal (state {attempt_state}, pid {pid} dead); "
            "resolve or supersede it with a registered reason before "
            "any rerun"
        )
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with os.fdopen(handle, "w", encoding="utf-8") as file_handle:
        json.dump(
            {
                "pid": os.getpid(),
                "acquired_at": datetime.now(timezone.utc).isoformat(),
                "attempt_state": "STARTING",
            },
            file_handle,
        )
    return lock_path


def _atomic_publish_json(path: Path, payload: dict[str, Any]) -> None:
    """Strict no-clobber atomic publish (14.9 item 5; 14.10 item 3).

    Creates a UNIQUE temp file in the target directory with exclusive
    creation (``tempfile.mkstemp``) - a fixed temp name would let a
    concurrent writer truncate an inode already hard-linked to a
    published result.  Writes, fsyncs, ``os.link``s onto the target
    (fails with FileExistsError if the target exists - never
    overwrites), then unlinks the temp.  The published bytes are
    therefore complete on disk at link time and an existing target is
    never modified.
    """

    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(
        payload, ensure_ascii=True, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ) + "\n"
    data = rendered.encode("utf-8")
    handle, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as file_handle:
            file_handle.write(data)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.link(temporary, path)
    except FileExistsError:
        raise _fail(
            f"publish refused: the output path already exists: {path}"
        ) from None
    finally:
        temporary.unlink(missing_ok=True)


def _update_run_lock(cache_dir: Path, **fields: Any) -> None:
    lock_path = cache_dir / _RUN_LOCK_NAME
    try:
        raw = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    raw.update(fields)
    _atomic_write_json(lock_path, raw)


def _release_run_lock(cache_dir: Path) -> None:
    lock_path = cache_dir / _RUN_LOCK_NAME
    try:
        lock_path.unlink()
    except OSError:
        pass

# ------------------------------------------------------------------- seal


def _fetch_process_alive() -> int | None:
    """Scan /proc for a live fetch process of THIS replay module."""

    self_pid = os.getpid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == self_pid:
            continue
        argv = _proc_cmdline(entry / "cmdline")
        if not argv:
            continue
        runs_module = any(
            "spy_monthly_sma10_replay" in part for part in argv
        )
        if runs_module and "fetch" in argv:
            return pid
    return None


def _derive_trading_days(
    spy_dates: Sequence[date], qqq_dates: Sequence[date]
) -> dict[str, object]:
    """SPY n QQQ sealed daily-bar intersection (returns-blind)."""

    spy = set(spy_dates)
    qqq = set(qqq_dates)
    sessions = sorted(spy & qqq)
    one_sided = sorted(spy ^ qqq)
    return {
        "trading_days": [value.isoformat() for value in sessions],
        "one_sided_dates": [
            {
                "date": value.isoformat(),
                "in_spy": value in spy,
                "in_qqq": value in qqq,
            }
            for value in one_sided
        ],
        "source": (
            "SPY.US n QQQ.US sealed RAW daily bars (dates only); the "
            "local holiday calendar does not cover 2012-2021 and is "
            "never used"
        ),
    }


def _load_daily_rows(
    cache_dir: Path, symbol: str
) -> list[list[Any]]:
    """Raw sealed bar rows: [date_iso, o, h, l, c, volume, turnover]."""

    path = cache_dir / "daily" / f"{symbol}.json.gz"
    if not path.exists():
        raise _fail(f"sealed daily bars missing for {symbol}")
    raw = _read_gzip_json(path)
    return [list(row) for row in raw.get("bars", [])]


def _cache_preflight(cache_dir: Path) -> dict[str, Any]:
    """Shared seal/evaluate preflight: terminal fetch + sealed corporate
    actions + plan bound + no live fetch."""

    status = _load_status(cache_dir)
    if isinstance(status.get("global_stop"), dict):
        raise _fail("preflight refused: a GLOBAL STOP marker is active")
    plan_path = cache_dir / "plan.json"
    if not plan_path.exists():
        raise _fail(
            "preflight refused: plan.json is missing (run plan + fetch "
            "first)"
        )
    plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan_payload.get("analysis_id") != ANALYSIS_ID:
        raise _fail("preflight refused: plan.json analysis_id mismatch")
    plan_symbols = {
        str(entry["symbol"]) for entry in plan_payload["symbols"]
    }
    stored: dict[str, Any] = status.get("symbols", {})
    non_terminal: list[str] = []
    missing_files: list[str] = []
    for symbol in sorted(plan_symbols):
        entry = stored.get(symbol)
        if entry is None:
            non_terminal.append(f"{symbol}:MISSING_STATE")
            continue
        daily = entry.get("daily", {})
        state = daily.get("state")
        if state == "COMPLETE":
            if not (cache_dir / "daily" / f"{symbol}.json.gz").exists():
                missing_files.append(symbol)
        elif state == "PERMANENT_FAILURE":
            if not daily.get("error"):
                non_terminal.append(f"{symbol}:PERMANENT_NO_EVIDENCE")
        else:
            non_terminal.append(f"{symbol}:{state or 'PENDING'}")
    if non_terminal:
        raise _fail(
            "preflight refused: fetch is not terminal for: "
            + ", ".join(non_terminal[:10])
        )
    if missing_files:
        raise _fail(
            "preflight refused: COMPLETE state but file missing: "
            + ", ".join(missing_files[:10])
        )
    live_pid = _fetch_process_alive()
    if live_pid is not None:
        raise _fail(
            f"preflight refused: a fetch process is alive (pid {live_pid})"
        )
    actions_path = cache_dir / CORPORATE_ACTIONS_FILENAME
    if not actions_path.exists():
        raise _fail(
            "preflight refused (DATA_BLOCKED): "
            f"{CORPORATE_ACTIONS_FILENAME} is missing; run "
            "import-corporate-actions first - dividends/splits may not "
            "be guessed or skipped"
        )
    actions_doc = json.loads(actions_path.read_text(encoding="utf-8"))
    # Integrity anchor is the file ON DISK (canonical JSON written by the
    # importer); the recorded source-file hash stays for provenance.
    actions_doc["sha256"] = _file_sha256(actions_path)
    # Decision 14.6: seal re-verifies the REGISTERED source.  Only the
    # registered xlsx format with the registered full sha256 may seal a
    # real run; the JSON path is synthetic-tests-only and is refused
    # here, and a stored record whose source hash drifted (a future SSGA
    # revision) needs a registered change decision.
    if (
        actions_doc.get("source_format")
        != "ssga-distributions-xlsx"
    ):
        raise _fail(
            "preflight refused (DATA_BLOCKED): the stored corporate "
            f"actions record has source_format "
            f"{actions_doc.get('source_format')!r}; only the registered "
            "ssga-distributions-xlsx source may seal a real run (the "
            "JSON path is synthetic-tests-only, contract 14.6)"
        )
    if (
        actions_doc.get("source_file_sha256")
        != REGISTERED_ACTIONS_SOURCE_SHA256
    ):
        actual = str(actions_doc.get("source_file_sha256"))
        raise _fail(
            "preflight refused (DATA_BLOCKED): the stored corporate "
            f"actions record carries source sha256 {actual[:16]}... "
            "which is not the registered workbook ("
            f"{REGISTERED_ACTIONS_SOURCE_SHA256[:16]}...); a different "
            "workbook revision needs a registered change decision in "
            "SPY_MONTHLY_SMA10_PREREGISTRATION.md (contract 14.6)"
        )
    actions = validate_corporate_actions_payload(actions_doc.get("payload"))
    instrument_actions = [
        action
        for action in actions
        if action.symbol == INSTRUMENT
    ]
    if not instrument_actions:
        raise _fail(
            "preflight refused (DATA_BLOCKED): the sealed corporate "
            f"actions file contains no {INSTRUMENT} entries"
        )
    # Re-verify the 48-quarter content fact on the STORED record.
    _verify_registered_content_fact(instrument_actions)
    return {
        "status": status,
        "plan_payload": plan_payload,
        "actions": actions,
        "actions_doc": actions_doc,
    }


def _sealed_preflight(
    cache_dir: Path, manifest: dict[str, Any]
) -> dict[str, Any]:
    """Decision 14.9 item 2: evaluate-time preflight from the SEALED
    manifest only - no live status.json read.

    Verifies: the corporate-actions file is present, un-drifted vs the
    sealed hash, of the registered source format with the registered
    source hash, and its 48-quarter content fact holds; the sealed
    fetch snapshot shows every plan symbol terminal; no live fetch.
    """

    actions_path = cache_dir / CORPORATE_ACTIONS_FILENAME
    if not actions_path.exists():
        raise _fail(
            "evaluate refused (DATA_BLOCKED): the sealed corporate "
            "actions file is missing from the cache"
        )
    actions_doc = json.loads(actions_path.read_text(encoding="utf-8"))
    actions_doc["sha256"] = _file_sha256(actions_path)
    corporate = manifest.get("corporate_actions", {})
    if str(actions_doc.get("sha256")) != str(corporate.get("sha256")):
        raise _fail(
            "evaluate refused: the corporate actions file drifted "
            "after seal"
        )
    if (
        actions_doc.get("source_format")
        != "ssga-distributions-xlsx"
    ):
        raise _fail(
            "evaluate refused (DATA_BLOCKED): stored corporate actions "
            f"source_format {actions_doc.get('source_format')!r} is "
            "not the registered xlsx"
        )
    if (
        actions_doc.get("source_file_sha256")
        != REGISTERED_ACTIONS_SOURCE_SHA256
    ):
        raise _fail(
            "evaluate refused (DATA_BLOCKED): stored corporate actions "
            "source hash is not the registered workbook"
        )
    actions = validate_corporate_actions_payload(
        actions_doc.get("payload")
    )
    instrument_actions = [
        action for action in actions if action.symbol == INSTRUMENT
    ]
    _verify_registered_content_fact(actions)
    snapshot = manifest.get("fetch_status_snapshot", {})
    plan_path = cache_dir / "plan.json"
    if not plan_path.exists():
        raise _fail("evaluate refused: the bound plan.json is missing")
    plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_symbols = {
        str(entry["symbol"]) for entry in plan_payload["symbols"]
    }
    non_terminal: list[str] = []
    for symbol in sorted(plan_symbols):
        entry = snapshot.get("symbols", {}).get(symbol)
        state = (
            entry.get("daily", {}).get("state")
            if isinstance(entry, dict)
            else None
        )
        if state not in ("COMPLETE", "PERMANENT_FAILURE"):
            non_terminal.append(f"{symbol}:{state or 'MISSING'}")
    if non_terminal:
        raise _fail(
            "evaluate refused: the SEALED fetch snapshot is not "
            "terminal for: " + ", ".join(non_terminal[:10])
        )
    if isinstance(snapshot.get("global_stop"), dict):
        raise _fail(
            "evaluate refused: the SEALED snapshot carries a global stop"
        )
    live_pid = _fetch_process_alive()
    if live_pid is not None:
        raise _fail(
            "evaluate refused: a fetch process is alive "
            f"(pid {live_pid})"
        )
    # ---- 14.10 item 2: structure/OHLC/duplicate/action-date checks
    # for BOTH SPY and QQQ run HERE - BEFORE the attempt is claimed.
    # A structural error is an explicit refusal with no attempt
    # claimed (seal validated the same shapes; this re-proves them
    # against the bytes that exist NOW, independent of seal).
    try:
        spy_raw_rows = _load_daily_rows(cache_dir, INSTRUMENT)
        qqq_raw_rows = _load_daily_rows(cache_dir, DISCLOSURE_BENCHMARK)
    except MonthlySma10Error as exc:
        raise _fail(f"evaluate refused: {exc}") from exc
    sealed_sessions = sorted(
        date.fromisoformat(str(value))
        for value in manifest.get("trading_days", {}).get(
            "trading_days", []
        )
    )
    if not sealed_sessions:
        raise _fail(
            "evaluate refused: the sealed manifest has no trading days"
        )
    independent_sessions = expected_nyse_sessions(DATA_START, DATA_END)
    protected_opens = _potential_execution_opens(sealed_sessions)
    for rows, symbol, acts in (
        (spy_raw_rows, INSTRUMENT, actions),
        (qqq_raw_rows, DISCLOSURE_BENCHMARK, []),
    ):
        try:
            validate_window_data(
                sessions=sealed_sessions,
                scoring_months=SCORING_MONTHS,
                daily_rows=_month_end_market_rows(rows),
                actions=acts,
                calendar_sessions=independent_sessions,
                symbol=symbol,
                protected_opens=protected_opens,
            )
        except (MonthlyReplayBlockedError, MonthlyTrendError) as exc:
            raise _fail(
                "evaluate refused (DATA_BLOCKED) pre-attempt for "
                f"{symbol}: {exc}"
            ) from exc
        except (TypeError, ValueError, IndexError, KeyError) as exc:
            raise _fail(
                "evaluate refused: malformed bar row in the sealed "
                f"{symbol} data: {exc!r}"
            ) from exc
    # ---- Decision 14.11: re-derive the OHLC anomaly set from the
    # sealed bars and require it to EQUAL the sealed ledger exactly -
    # BEFORE the attempt is claimed, and independent of the manifest
    # hashes (a consistently-rewritten outer hash set cannot pass this:
    # the derivation reads the bars).
    anomaly_ledger = _validate_anomaly_ledger()
    _verify_ledger_against_bars(
        _month_end_market_rows(spy_raw_rows),
        _month_end_market_rows(qqq_raw_rows),
        anomaly_ledger,
    )
    sealed_ledger = manifest.get("ohlc_anomaly_ledger", {})
    if (
        not isinstance(sealed_ledger, dict)
        or str(sealed_ledger.get("sha256"))
        != _file_sha256(_ANOMALY_LEDGER_PATH)
    ):
        raise _fail(
            "evaluate refused: the sealed anomaly ledger hash does not "
            "match the committed ledger file (missing or drifted - "
            "re-seal with a registered change decision)"
        )
    return {
        "status": snapshot,
        "plan_payload": plan_payload,
        "actions": actions,
        "actions_doc": actions_doc,
        "instrument_actions": instrument_actions,
        "spy_rows": _month_end_market_rows(spy_raw_rows),
        "qqq_rows": _month_end_market_rows(qqq_raw_rows),
        "sealed_sessions": sealed_sessions,
        "anomaly_ledger": anomaly_ledger,
    }


def _source_provenance_hashes_monthly() -> dict[str, str]:
    """sha256 of this CLI, the pure module and the governance doc."""

    repo = _repo_root()
    relpaths = (
        "backend/app/cli/spy_monthly_sma10_replay.py",
        "backend/app/domain/monthly_trend/__init__.py",
        "backend/app/domain/monthly_trend/sma10.py",
        "backend/app/domain/monthly_trend/nyse_calendar.py",
        "backend/app/domain/monthly_trend/data/splits.json",
        "backend/app/domain/monthly_trend/data/ohlc_anomaly_ledger.json",
        "backend/app/domain/SPY_MONTHLY_SMA10_PREREGISTRATION.md",
        "backend/app/core/accounting_fees.py",
        # Item 8: the REUSED ORB helper module is a runtime dependency
        # of this CLI (throttle, pagination, attempt claim, ...) - its
        # hash must be bound too.
        "backend/app/cli/opening_momentum_historical_replay.py",
    )
    hashes: dict[str, str] = {}
    for relative in relpaths:
        # Decision 14.11 follow-up: the ledger's BYTES are read through
        # the module-level ``_ANOMALY_LEDGER_PATH`` (monkeypatchable in
        # tests via a tmp_path copy) while the manifest KEY stays the
        # repo-relative name, so every hash of the ledger - the
        # dedicated manifest block, the source_hashes entry and the
        # drift comparison - is computed over the SAME bytes.
        if relative.endswith("ohlc_anomaly_ledger.json"):
            if not _ANOMALY_LEDGER_PATH.exists():
                raise _fail(
                    "evaluate refused: source file missing: "
                    f"{relative}"
                )
            hashes[relative] = _file_sha256(_ANOMALY_LEDGER_PATH)
            continue
        path = repo / relative
        if not path.exists():
            raise _fail(f"evaluate refused: source file missing: {relative}")
        hashes[relative] = _file_sha256(path)
    return hashes


def sealed_set_for(calendar: dict[str, object]) -> set[date]:
    return {
        date.fromisoformat(value)
        for value in cast(list[str], calendar["trading_days"])
    }


def _validate_anomaly_ledger() -> dict[str, Any]:
    """Decision 14.11: load the sealed anomaly ledger and verify it
    equals the registered constants EXACTLY (symbol, session,
    relation, cap, and the evidence hashes).  Returns the parsed
    ledger.  Refuses when the file is missing, malformed or tampered -
    at seal AND at evaluate (before any attempt is claimed)."""

    if not _ANOMALY_LEDGER_PATH.exists():
        raise _fail(
            "seal refused (DATA_BLOCKED): the sealed ohlc_anomaly "
            "ledger is missing (decision 14.11 requires the pinned, "
            "committed ledger file)"
        )
    try:
        ledger = json.loads(
            _ANOMALY_LEDGER_PATH.read_text(encoding="utf-8")
        )
    except ValueError as exc:
        raise _fail(
            "seal refused (DATA_BLOCKED): the ohlc_anomaly ledger is "
            f"not valid JSON: {exc!r}"
        ) from exc
    entries = ledger.get("anomalies")
    if not isinstance(entries, list):
        raise _fail(
            "seal refused (DATA_BLOCKED): the ohlc_anomaly ledger has "
            "no anomalies list"
        )
    if len(entries) != OHLC_ANOMALY_LEDGER_CAP:
        raise _fail(
            "seal refused (DATA_BLOCKED): the ohlc_anomaly ledger "
            f"carries {len(entries)} entries; the registered cap is "
            f"{OHLC_ANOMALY_LEDGER_CAP}"
        )
    entry = entries[0]
    expected = {
        "symbol": OHLC_ANOMALY_LEDGER_SYMBOL,
        "session": OHLC_ANOMALY_LEDGER_SESSION.isoformat(),
    }
    for key, want in expected.items():
        if entry.get(key) != want:
            raise _fail(
                "seal refused (DATA_BLOCKED): the ohlc_anomaly ledger "
                f"entry {key} is {entry.get(key)!r}, the registered "
                f"value is {want!r}"
            )
    relations = entry.get("violated_relations")
    if relations != [OHLC_ANOMALY_LEDGER_RELATION]:
        raise _fail(
            "seal refused (DATA_BLOCKED): the ohlc_anomaly ledger "
            f"relations are {relations!r}; the registered relation is "
            f"[{OHLC_ANOMALY_LEDGER_RELATION!r}]"
        )
    evidence = entry.get("evidence", {})
    receipt = evidence.get("provider_reproduction_receipt", {})
    if (
        evidence.get("sealed_cache_file_sha256")
        != "eef5adc1d68d7020563ba90e729cbcc5a0c6dba29c183765c7907477947e25ca"
    ):
        raise _fail(
            "seal refused (DATA_BLOCKED): the ohlc_anomaly ledger "
            "sealed-cache hash is not the registered SPY.US.json.gz "
            "sha256"
        )
    if (
        receipt.get("sha256")
        != "edb6a218a92ffe2fd571821a2259b3b5376fed8c6603a1eaceba7d7542ccb4fd"
    ):
        raise _fail(
            "seal refused (DATA_BLOCKED): the ohlc_anomaly ledger "
            "provider-reproduction receipt hash is not the registered "
            "value"
        )
    return ledger


def _ledger_derived_entries(ledger: dict[str, Any]) -> list[str]:
    """The (symbol, session, relation) keys the LEDGER registers."""

    return sorted(
        f"{entry.get('symbol')}|{entry.get('session')}|{relation}"
        for entry in ledger.get("anomalies", [])
        for relation in entry.get("violated_relations", [])
    )


def _verify_ledger_against_bars(
    spy_rows: Sequence[DailyPriceRow],
    qqq_rows: Sequence[DailyPriceRow],
    ledger: dict[str, Any],
) -> None:
    """Decision 14.11: re-detect every OHLC violation from the sealed
    bars of BOTH symbols and require the derived set to EQUAL the
    ledger exactly.  Refuses on a missing, tampered or mismatched
    ledger EVEN IF the outer hashes were rewritten consistently (the
    derivation reads the bars, not the manifest)."""

    derived = [
        f"{item['symbol']}|{item['session']}|{item['relation']}"
        for item in (
            *detect_ohlc_anomalies(spy_rows, symbol=INSTRUMENT),
            *detect_ohlc_anomalies(
                qqq_rows, symbol=DISCLOSURE_BENCHMARK
            ),
        )
    ]
    registered = _ledger_derived_entries(ledger)
    if derived != registered:
        def _render(items: list[str]) -> str:
            return (
                ", ".join(items[:5]) if items else "(none)"
            )

        raise _fail(
            "evaluate refused (DATA_BLOCKED): the sealed bars' OHLC "
            "anomaly set does not equal the anomaly ledger (derived: "
            f"{_render(derived)}; ledger: {_render(registered)}); the "
            "ledger must be re-registered by a written decision before "
            "any attempt is claimed"
        )


def _potential_execution_opens(sessions: Sequence[date]) -> list[date]:
    """Decision 14.11 read set: the next sealed session after each
    month-end of 2011-12..2021-12 - EVERY potential execution day
    (boundary entry 2012-01-03 through final liquidation 2022-01-03),
    whether or not a trade actually happens there."""

    sealed = sorted(set(sessions))
    targets: list[date] = []
    for key in (*WARMUP_MONTHS[-1:], *SCORING_MONTHS):
        month_end = month_end_sessions(sealed).get(key)
        if month_end is None:
            continue
        following = next_session_after(sealed, month_end)
        if following is not None:
            targets.append(following)
    return targets


def _validate_bars_at_seal(
    rows: Sequence[Sequence[Any]],
    *,
    symbol: str,
    actions: Sequence[CorporateAction],
    calendar: dict[str, object],
) -> None:
    """Item 4 (14.9): seal-time OHLC/duplicate/structure validation for
    ONE symbol's RAW bars against the FULL independent calendar."""

    sessions = sorted(sealed_set_for(calendar))
    validate_window_data(
        sessions=sessions,
        scoring_months=SCORING_MONTHS,
        daily_rows=_month_end_market_rows(list(rows)),
        actions=actions,
        calendar_sessions=expected_nyse_sessions(DATA_START, DATA_END),
        symbol=symbol,
        protected_opens=_potential_execution_opens(sessions),
    )


def run_seal(
    cache_dir: Path,
    *,
    reseal_reason: str | None = None,
) -> str:
    """Seal the CLOSED input set: per-file sha256 at seal time, the
    corporate-actions file, the plan, the derived trading calendar and
    the source hashes.  A re-seal needs a reason and preserves the old
    receipt."""

    receipt_path = cache_dir / "seal_receipt.json"
    if receipt_path.exists() and not reseal_reason:
        raise _fail(
            "seal refused: a seal receipt already exists; pass "
            "--reseal-reason (the previous receipt is preserved)"
        )
    if receipt_path.exists() and reseal_reason:
        receipts_dir = cache_dir / "seal_receipts"
        receipts_dir.mkdir(parents=True, exist_ok=True)
        previous = json.loads(receipt_path.read_text(encoding="utf-8"))
        index = 1
        while (receipts_dir / f"seal-{index}.json").exists():
            index += 1
        _atomic_write_json(receipts_dir / f"seal-{index}.json", previous)
        # Item 5 (14.9): archive the old MANIFEST too (the receipt
        # alone loses the sealed inputs).
        manifest_path = cache_dir / "manifest.json"
        if manifest_path.exists():
            archive_dir = cache_dir / "manifest_archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            midx = 1
            while (archive_dir / f"manifest-{midx}.json").exists():
                midx += 1
            archived = archive_dir / f"manifest-{midx}.json"
            archived.write_bytes(manifest_path.read_bytes())

    preflight = _cache_preflight(cache_dir)
    plan_payload: dict[str, Any] = preflight["plan_payload"]
    status: dict[str, Any] = preflight["status"]

    spy_rows = _load_daily_rows(cache_dir, INSTRUMENT)
    qqq_rows = _load_daily_rows(cache_dir, DISCLOSURE_BENCHMARK)
    spy_dates = [date.fromisoformat(row[0]) for row in spy_rows]
    qqq_dates = [date.fromisoformat(row[0]) for row in qqq_rows]
    calendar = _derive_trading_days(spy_dates, qqq_dates)

    # ---- Item 5: seal-time data completeness checks (DATA_BLOCKED) ----
    # (a) ONE-SIDED dates BLOCK inside the required range: a session
    #     with a bar on only one symbol is a data gap, not a holiday.
    required_start = DATA_START
    required_end = DATA_END
    one_sided = calendar["one_sided_dates"]
    assert isinstance(one_sided, list)
    in_range_one_sided = [
        entry
        for entry in one_sided
        if required_start
        <= date.fromisoformat(str(entry["date"]))
        <= required_end
    ]
    if in_range_one_sided:
        rendered = ", ".join(
            str(entry["date"]) for entry in in_range_one_sided[:5]
        )
        raise _fail(
            "seal refused (DATA_BLOCKED): one-sided SPY/QQQ bar dates "
            f"inside the required range: {rendered}"
        )
    # (b) Decision 14.9 item 4: the fetch receipt's per-symbol bar
    #     hash and count are REQUIRED and must match the files on disk
    #     exactly (missing values refuse - no is-not-None guards).
    for symbol in (INSTRUMENT, DISCLOSURE_BENCHMARK):
        recorded = (
            preflight["status"]
            .get("symbols", {})
            .get(symbol, {})
            .get("daily", {})
        )
        path = cache_dir / "daily" / f"{symbol}.json.gz"
        actual_bars = len(_read_gzip_json(path).get("bars", []))
        if "bars" not in recorded or "sha256" not in recorded:
            raise _fail(
                "seal refused: the fetch receipt for "
                f"{symbol} lacks its bar hash/count (incomplete fetch)"
            )
        if int(recorded["bars"]) != actual_bars:
            raise _fail(
                "seal refused: bar count drift for "
                f"{symbol} (status.json says {recorded['bars']}, file "
                f"has {actual_bars})"
            )
        if str(recorded["sha256"]) != _file_sha256(path):
            raise _fail(
                f"seal refused: bar file hash drift for {symbol}"
            )
    # (b') Item 4: validate RAW OHLC, duplicates and structure for
    #      BOTH SPY and QQQ at SEAL time (again at evaluate).  A
    #      blocked condition is a SEAL refusal (DATA_BLOCKED), not a
    #      raw domain exception.
    for bars, sym, acts in (
        (spy_rows, INSTRUMENT, preflight["actions"]),
        (qqq_rows, DISCLOSURE_BENCHMARK, []),
    ):
        try:
            _validate_bars_at_seal(
                bars, symbol=sym, actions=acts, calendar=calendar
            )
        except MonthlyReplayBlockedError as exc:
            raise _fail(
                f"seal refused (DATA_BLOCKED) for {sym}: {exc}"
            ) from exc
    # (c) Item 4: compare the FULL registered range DATA_START..DATA_END
    #     EXACTLY - a truncated head or tail blocks; no cropping to the
    #     cache's own min/max.
    trading_day_strings = cast(list[str], calendar["trading_days"])
    sealed_set = {
        date.fromisoformat(value) for value in trading_day_strings
    }
    independent = set(expected_nyse_sessions(DATA_START, DATA_END))
    missing_both = sorted(independent - sealed_set)
    extra_sealed = sorted(sealed_set - independent)
    if missing_both:
        rendered = ", ".join(d.isoformat() for d in missing_both[:5])
        head_truncated = bool(spy_dates) and min(spy_dates) > DATA_START
        tail_truncated = bool(spy_dates) and max(spy_dates) < DATA_END
        detail = (
            " (head truncated - the sealed data starts "
            f"{min(spy_dates).isoformat() if spy_dates else '?'}, the "
            f"registered range starts {DATA_START.isoformat()})"
            if head_truncated and not missing_both[0] > (max(spy_dates) if spy_dates else DATA_END)
            else (
                " (tail truncated - the sealed data ends "
                f"{max(spy_dates).isoformat() if spy_dates else '?'}, "
                f"the registered range ends {DATA_END.isoformat()})"
                if tail_truncated
                else ""
            )
        )
        raise _fail(
            "seal refused (DATA_BLOCKED): NYSE-rule sessions with NO "
            f"SPY/QQQ bar (missing on BOTH symbols): {rendered}"
            f"{detail}"
        )
    if extra_sealed:
        rendered = ", ".join(d.isoformat() for d in extra_sealed[:5])
        raise _fail(
            "seal refused (DATA_BLOCKED): sealed sessions that the "
            f"NYSE-rule calendar says were closed: {rendered}"
        )

    files: list[dict[str, object]] = []
    for symbol in (INSTRUMENT, DISCLOSURE_BENCHMARK):
        path = cache_dir / "daily" / f"{symbol}.json.gz"
        raw = _read_gzip_json(path)
        if (
            raw.get("symbol") != symbol
            or raw.get("period") != "DAY"
            or raw.get("adjustment") != "NoAdjust"
        ):
            raise _fail(f"seal refused: file metadata mismatch for {symbol}")
        files.append(
            {
                "path": f"daily/{symbol}.json.gz",
                "symbol": symbol,
                "sha256": _file_sha256(path),
                "bytes": path.stat().st_size,
                "bars": len(raw.get("bars", [])),
            }
        )

    # ---- Decision 14.11: the sealed anomaly ledger.  It is validated
    # against the registered constants here and hashed into the
    # manifest; evaluate re-derives the violation set from the sealed
    # bars and requires it to EQUAL the ledger before claiming.
    anomaly_ledger = _validate_anomaly_ledger()
    anomaly_ledger_sha256 = _file_sha256(_ANOMALY_LEDGER_PATH)

    # Item 7: the sealed independent split evidence is hashed into
    # the manifest; its absence is DATA_BLOCKED (the price screen is
    # only a tripwire and cannot prove "no split").
    splits_path = (
        _repo_root()
        / "backend"
        / "app"
        / "domain"
        / "monthly_trend"
        / "data"
        / "splits.json"
    )
    if not splits_path.exists():
        raise _fail(
            "seal refused (DATA_BLOCKED): the sealed splits.json "
            "evidence file is missing; the price anomaly screen cannot "
            "prove the absence of splits (decision 14.7 item 7)"
        )
    splits_sha256 = _file_sha256(splits_path)
    splits_payload = json.loads(splits_path.read_text(encoding="utf-8"))
    for symbol_key in ("SPY.US", "QQQ.US"):
        entry = splits_payload.get("symbols", {}).get(symbol_key)
        if not isinstance(entry, dict) or entry.get(
            "splits_in_window"
        ) != 0:
            raise _fail(
                "seal refused (DATA_BLOCKED): splits.json does not "
                f"attest zero in-window splits for {symbol_key}"
            )

    # Item 5: the INDEPENDENT sealed calendar (NYSE rules) is derived
    # and stored; month-ends and next opens are validated against it so
    # dates missing on BOTH symbols are caught.
    expected_sessions = expected_nyse_sessions(DATA_START, DATA_END)
    manifest: dict[str, object] = {
        "analysis_id": ANALYSIS_ID,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reseal_reason": reseal_reason,
        "window": {
            "data_start": DATA_START.isoformat(),
            "data_end": DATA_END.isoformat(),
            "scoring_start_month": (
                f"{WINDOW_START_MONTH[0]:04d}-{WINDOW_START_MONTH[1]:02d}"
            ),
            "scoring_end_month": (
                f"{WINDOW_END_MONTH[0]:04d}-{WINDOW_END_MONTH[1]:02d}"
            ),
        },
        "plan": {
            "sha256": _sealed_plan_digest(plan_payload),
        },
        "plan_doc_sha256": _file_sha256(
            _repo_root() / "backend" / _PLAN_DOC_RELATIVE_PATH
        ),
        # Decision 14.9 item 2: a REQUIRED dict (never spread into the
        # top level) so evaluate can compare key SETS as well as values.
        "source_hashes": _source_provenance_hashes_monthly(),
        "corporate_actions": {
            "sha256": preflight["actions_doc"]["sha256"],
            "source_url": preflight["actions_doc"]["source_url"],
            "imported_at": preflight["actions_doc"]["imported_at"],
            "actions_count": len(preflight["actions"]),
        },
        "splits_evidence": {
            "sha256": splits_sha256,
            "path": "backend/app/domain/monthly_trend/data/splits.json",
        },
        # Decision 14.11: the sealed OHLC anomaly ledger.
        "ohlc_anomaly_ledger": {
            "sha256": anomaly_ledger_sha256,
            "path": (
                "backend/app/domain/monthly_trend/data/"
                "ohlc_anomaly_ledger.json"
            ),
            "entries": len(anomaly_ledger.get("anomalies", [])),
            "sessions": [
                str(entry.get("session"))
                for entry in anomaly_ledger.get("anomalies", [])
            ],
        },
        "independent_calendar": {
            "source": "NYSE rules derivation (nyse_calendar.py, item 5)",
            "expected_sessions": len(expected_sessions),
            "sessions": [d.isoformat() for d in expected_sessions],
        },
        "trading_days": calendar,
        "files": files,
        "fetch_status_snapshot": status,
    }
    manifest_path = cache_dir / "manifest.json"
    _atomic_write_json(manifest_path, manifest)
    manifest_sha256 = _file_sha256(manifest_path)
    receipt = {
        "sealed_at": datetime.now(timezone.utc).isoformat(),
        "manifest_sha256": manifest_sha256,
        "files": len(files),
        "reseal_reason": reseal_reason,
    }
    _atomic_write_json(receipt_path, receipt)
    return manifest_sha256


# ---------------------------------------------------------------- evaluate


def _month_end_market_rows(
    rows: Sequence[Sequence[Any]],
) -> list[DailyPriceRow]:
    return [
        DailyPriceRow(
            session=date.fromisoformat(str(row[0])),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
        )
        for row in rows
    ]



def run_evaluate(
    *,
    cache_dir: Path,
    output_path: Path,
    rerun_reason: str | None = None,
) -> dict[str, Any]:
    """One-pass evaluation: prechecks (output outside cache, clean
    worktree, sealed manifest, source hashes, corporate actions, no live
    fetch, exclusive attempt claim), then the single computation."""

    resolved_output = output_path.resolve()
    resolved_cache = cache_dir.resolve()
    if (
        resolved_cache in resolved_output.parents
        or resolved_output == resolved_cache
    ):
        raise _fail(
            "evaluate refused: the output path is inside the cache; pass "
            "--output pointing outside the sealed input set"
        )
    # ---- Item 9: the run-wide exclusive lock (held until the end).
    _acquire_run_lock(cache_dir)
    try:
        return _run_evaluate_locked(
            cache_dir=cache_dir,
            output_path=output_path,
            rerun_reason=rerun_reason,
        )
    finally:
        _release_run_lock(cache_dir)


def _run_evaluate_locked(
    *,
    cache_dir: Path,
    output_path: Path,
    rerun_reason: str | None,
) -> dict[str, Any]:
    _require_clean_worktree(_repo_root())
    source_hashes = _source_provenance_hashes_monthly()

    manifest_path = cache_dir / "manifest.json"
    if not manifest_path.exists():
        raise _fail(
            "evaluate refused: no sealed input manifest (run seal first)"
        )
    manifest_sha256 = _file_sha256(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("analysis_id") != ANALYSIS_ID:
        raise _fail("evaluate refused: manifest analysis_id mismatch")
    receipt_path = cache_dir / "seal_receipt.json"
    if not receipt_path.exists():
        raise _fail("evaluate refused: the seal receipt is missing")
    seal_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if str(seal_receipt.get("manifest_sha256")) != manifest_sha256:
        raise _fail(
            "evaluate refused: the seal receipt's manifest hash does not "
            "match the manifest on disk"
        )
    for entry in manifest["files"]:
        _verify_sealed_file(cache_dir, entry)
    unsealed: list[str] = []
    sealed_paths = {str(entry["path"]) for entry in manifest["files"]}
    for path in sorted(cache_dir.rglob("*.json.gz")):
        relative = path.relative_to(cache_dir).as_posix()
        if relative not in sealed_paths:
            unsealed.append(relative)
    if unsealed:
        raise _fail(
            "evaluate refused: unsealed input files present: "
            + ", ".join(unsealed[:10])
        )
    actions_path = cache_dir / CORPORATE_ACTIONS_FILENAME
    if _file_sha256(actions_path) != str(
        manifest["corporate_actions"]["sha256"]
    ):
        raise _fail(
            "evaluate refused: the corporate actions file drifted after "
            "seal"
        )
    plan_path = cache_dir / "plan.json"
    stored_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if _sealed_plan_digest(stored_plan) != str(manifest["plan"]["sha256"]):
        raise _fail(
            "evaluate refused: the stored plan digest does not match the "
            "sealed plan hash"
        )

    # ---- Item 2 (14.9): the sealed source hashes must match the
    # CURRENT files EXACTLY - identical key set AND identical values -
    # before any attempt is claimed.  A missing dict is itself a
    # refusal (the seal was made by an older, non-conforming version).
    sealed_hashes = manifest.get("source_hashes")
    if not isinstance(sealed_hashes, dict) or not sealed_hashes:
        raise _fail(
            "evaluate refused: the sealed manifest has no "
            "source_hashes dict (required by decision 14.9 item 2); "
            "re-seal with the current CLI"
        )
    if set(sealed_hashes) != set(source_hashes):
        missing = sorted(set(sealed_hashes) - set(source_hashes))
        extra = sorted(set(source_hashes) - set(sealed_hashes))
        raise _fail(
            "evaluate refused: sealed source_hashes key set differs "
            f"from the current files (missing: {missing}, extra: {extra})"
        )
    for key, sealed_value in sealed_hashes.items():
        if source_hashes[key] != sealed_value:
            raise _fail(
                "evaluate refused: source hash drift for "
                f"{key} (sealed {str(sealed_value)[:16]}..., current "
                f"{str(source_hashes[key])[:16]}...); "
                "re-seal with a registered change decision first"
            )
    # The sealed split-evidence hash must match too (item 7).
    sealed_splits = manifest.get("splits_evidence", {}).get("sha256")
    splits_rel = (
        "backend/app/domain/monthly_trend/data/splits.json"
    )
    if (
        sealed_splits is not None
        and source_hashes.get(splits_rel) != sealed_splits
    ):
        raise _fail(
            "evaluate refused: sealed splits.json evidence drifted "
            "after seal"
        )

    # ---- Item 2 (14.9): validate from the SEALED snapshot ONLY - no
    # re-read of the live status.json (the cache may have moved on).
    # The corporate-actions payload is re-verified from the sealed
    # bytes; the fetch terminality is proven by the manifest's
    # snapshot.
    preflight = _sealed_preflight(cache_dir, manifest)

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        probe = output_path.with_name(f".{output_path.name}.probe")
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise _fail(f"evaluate refused: output path is not writable: {exc}") from exc

    # ---- Item 5 (14.9): a PERSISTED non-terminal attempt blocks any
    # rerun (a crash during publish/receipt can leave STARTED behind;
    # that state must be resolved with a registered reason, never
    # silently retried over).
    persisted = _load_attempt_receipt(cache_dir)
    if persisted is not None and persisted.get("state") not in (
        "COMPLETED",
        "FAILED",
        None,
    ):
        raise _fail(
            "evaluate refused: the previous evaluation attempt is "
            f"non-terminal (state {persisted.get('state')!r}, started "
            f"{persisted.get('started_at')}); resolve or supersede it "
            "with a registered reason first"
        )
    attempt_index, _claim_path = _claim_next_attempt(
        cache_dir, rerun_reason=rerun_reason
    )
    _update_run_lock(
        cache_dir,
        attempt_state="STARTED",
        attempt_index=attempt_index,
    )
    previous_attempt = persisted
    attempt: dict[str, Any] = {
        "analysis_id": ANALYSIS_ID,
        "state": "STARTED",
        "attempt_index": attempt_index,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "output_path": str(output_path),
        "rerun_reason": rerun_reason,
    }
    _write_attempt_receipt(cache_dir, attempt)
    try:
        payload = _evaluate_computation(
            cache_dir,
            manifest,
            manifest_sha256,
            source_hashes,
            preflight,
            rerun_reason,
        )
    except Exception:
        attempt["state"] = "FAILED"
        attempt["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_attempt_receipt(cache_dir, attempt)
        _update_run_lock(cache_dir, attempt_state="FAILED")
        raise

    # ---- Item 5 (14.9): publishing is a strict no-clobber ATOMIC
    # operation - an existing target REFUSES (no versioned fallback
    # hiding the collision), and the supersedes chain links the SAVED
    # previous attempt (not a fresh receipt read).
    superseded_link = (
        {
            "path": previous_attempt.get("output_path"),
            "sha256": previous_attempt.get("output_sha256"),
        }
        if previous_attempt is not None
        and previous_attempt.get("output_sha256") is not None
        else None
    )
    payload.setdefault("provenance", {})["supersedes"] = superseded_link

    # ---- 14.10 item 1: the WHOLE publish/receipt stage is protected.
    # A failure at ANY point - result publish, hashing, receipt
    # publish, bookkeeping - records the attempt FAILED with whatever
    # partial provenance exists (a published result path/hash included)
    # before propagating.  The run lock is released by the outer
    # finally in run_evaluate.
    published_output: Path | None = None
    published_sha256: str | None = None
    try:
        _atomic_publish_json(output_path, payload)
        published_output = output_path
        published_sha256 = _file_sha256(output_path)
        receipt_out = {
            "analysis_id": ANALYSIS_ID,
            "attempt_index": attempt["attempt_index"],
            "seal_manifest_sha256": manifest_sha256,
            "source_hashes": source_hashes,
            "output_sha256": published_sha256,
            "verdict": payload["verdict"],
            "verdict_reasons": payload["verdict_reasons"],
            "months": payload["sample_gates"]["months"],
            "cash_months": payload["sample_gates"]["cash_months"],
            "invested_months": payload["sample_gates"]["invested_months"],
        }
        receipt_out_path = output_path.with_name(
            f"{output_path.stem}.receipt{output_path.suffix or '.json'}"
        )
        _atomic_publish_json(receipt_out_path, receipt_out)

        attempt["state"] = "COMPLETED"
        attempt["finished_at"] = datetime.now(timezone.utc).isoformat()
        attempt["output_path"] = str(output_path)
        attempt["output_sha256"] = published_sha256
        _write_attempt_receipt(cache_dir, attempt)
        _update_run_lock(cache_dir, attempt_state="COMPLETED")
    except Exception:
        attempt["state"] = "FAILED"
        attempt["finished_at"] = datetime.now(timezone.utc).isoformat()
        if published_output is not None:
            attempt["output_path"] = str(published_output)
        if published_sha256 is not None:
            attempt["output_sha256"] = published_sha256
        _write_attempt_receipt(cache_dir, attempt)
        _update_run_lock(cache_dir, attempt_state="FAILED")
        raise
    payload["receipt_path"] = str(receipt_out_path)
    return payload


def _evaluate_computation(
    cache_dir: Path,
    manifest: dict[str, Any],
    manifest_sha256: str,
    source_hashes: dict[str, str],
    preflight: dict[str, Any],
    rerun_reason: str | None,
) -> dict[str, Any]:
    """The one-pass computation over verified sealed inputs only."""

    spy_rows = _month_end_market_rows(
        _load_daily_rows(cache_dir, INSTRUMENT)
    )
    qqq_rows = _month_end_market_rows(
        _load_daily_rows(cache_dir, DISCLOSURE_BENCHMARK)
    )
    actions = [
        action
        for action in preflight["actions"]
        if action.symbol == INSTRUMENT
    ]
    sessions = sorted(
        date.fromisoformat(str(value))
        for value in manifest["trading_days"]["trading_days"]
    )

    # ---------- DATA_BLOCKED screening (never fill, never drop a month)
    independent_sessions = [
        date.fromisoformat(str(value))
        for value in manifest.get("independent_calendar", {}).get(
            "sessions", []
        )
    ]
    try:
        validate_window_data(
            sessions=sessions,
            scoring_months=SCORING_MONTHS,
            daily_rows=spy_rows,
            actions=actions,
            calendar_sessions=independent_sessions,
            protected_opens=_potential_execution_opens(sessions),
        )
    except MonthlyReplayBlockedError as exc:
        return _blocked_payload(manifest_sha256, str(exc), source_hashes)

    # ---------- split-completeness screen (contract §14.5)
    # The registered dividend source has NO split column, so the absence
    # of split rows is not evidence of the absence of splits: any RAW
    # adjacent-close discontinuity without a registered split action is
    # DATA_BLOCKED, never filled in.
    split_offenders = screen_unexplained_split_anomalies(
        spy_rows, actions, sessions=sessions
    )
    if split_offenders:
        rendered = ", ".join(
            f"{session.isoformat()}(factor {factor:.4f})"
            for session, factor in split_offenders[:5]
        )
        return _blocked_payload(
            manifest_sha256,
            "unexplained split discontinuity in RAW SPY prices "
            f"(no registered split action): {rendered}",
            source_hashes,
        )

    spy_by_session = {row.session: row for row in spy_rows}
    qqq_by_session = {row.session: row for row in qqq_rows}

    # ---------- signal (warm-up months included, never scoring)
    signal_index = build_signal_index(spy_rows, actions)
    month_bars = month_end_index_levels(signal_index, sessions)
    signals: dict[tuple[int, int], int | None] = {}
    for key in (*WARMUP_MONTHS, *SCORING_MONTHS):
        signals[key] = sma10_signal(
            month_bars, year=key[0], month=key[1]
        )

    # ---------- execution events (next-open rule)
    try:
        events = sleeve_entry_exit_events(
            signals=signals,
            scoring_months=SCORING_MONTHS,
            sessions=sessions,
        )
    except MonthlyReplayBlockedError as exc:
        return _blocked_payload(manifest_sha256, str(exc), source_hashes)

    # month-end of the last scoring month -> liquidation next open
    last_month_end = month_end_sessions(sessions)[WINDOW_END_MONTH]
    liquidation_session = next_session_after(sessions, last_month_end)
    if liquidation_session is None:
        return _blocked_payload(
            manifest_sha256,
            "missing next open after the final scoring month-end "
            f"({last_month_end.isoformat()})",
            source_hashes,
        )

    # ---------- three legs: sleeve base, sleeve stress, SPY buy&hold
    sleeve_base = simulate_leg(
        sessions=sessions,
        daily_rows=spy_rows,
        actions=actions,
        scoring_months=SCORING_MONTHS,
        entry_events=events,
        slippage_bps=BASE_SLIPPAGE_BPS,
        liquidation_session=liquidation_session,
    )
    # Item 12: the 30% withholding sensitivity is COMPUTED (not just
    # promised): the same sleeve under a 30% dividend withholding -
    # disclosure only, never gating.
    sleeve_withholding = simulate_leg(
        sessions=sessions,
        daily_rows=spy_rows,
        actions=actions,
        scoring_months=SCORING_MONTHS,
        entry_events=events,
        slippage_bps=BASE_SLIPPAGE_BPS,
        liquidation_session=liquidation_session,
        dividends_withholding_rate=DIVIDEND_WITHHOLDING_RATE,
    )
    sleeve_stress = simulate_leg(
        sessions=sessions,
        daily_rows=spy_rows,
        actions=actions,
        scoring_months=SCORING_MONTHS,
        entry_events=events,
        slippage_bps=STRESS_SLIPPAGE_BPS,
        liquidation_session=liquidation_session,
    )

    # SPY buy-and-hold: bought at the OPEN of the first session of the
    # first scoring month (2012-01) — "the open of the first eligible
    # month" (contract §4.4).  The sleeve's January return is therefore
    # 0 by construction (it cannot trade before the first month-end
    # close); that give-up is exactly what claim 2 measures.  Dividends
    # as cash, identical caps, liquidation cost charged at the end.
    window_sessions = [
        session
        for session in sessions
        if WINDOW_START_MONTH
        <= (session.year, session.month)
        <= WINDOW_END_MONTH
    ]
    if not window_sessions:
        return _blocked_payload(
            manifest_sha256,
            "no sealed sessions inside the scoring window",
            source_hashes,
        )
    buy_hold_entry_session = window_sessions[0]
    spy_buy_hold = simulate_leg(
        sessions=sessions,
        daily_rows=spy_rows,
        actions=actions,
        scoring_months=SCORING_MONTHS,
        entry_events=[(buy_hold_entry_session, "BUY")],
        slippage_bps=BASE_SLIPPAGE_BPS,
        liquidation_session=liquidation_session,
    )
    # QQQ is a PRICE-ONLY disclosure benchmark (contract §14.5): the
    # registered dividend source is SSGA (SPDR funds) and carries no
    # Invesco QQQ rows, so the QQQ leg deliberately receives NO
    # corporate actions and its dividends are NOT credited.  Its return
    # series feeds only the descriptive qqq_differences slice - never
    # any claim.
    qqq_buy_hold = simulate_leg(
        sessions=sessions,
        daily_rows=qqq_rows,
        actions=[],
        scoring_months=SCORING_MONTHS,
        entry_events=[(buy_hold_entry_session, "BUY")],
        slippage_bps=BASE_SLIPPAGE_BPS,
        liquidation_session=liquidation_session,
    )

    r_sleeve = monthly_return_series(
        sleeve_base.records, initial_equity=INITIAL_CASH_USD
    )
    r_stress = monthly_return_series(
        sleeve_stress.records, initial_equity=INITIAL_CASH_USD
    )
    r_benchmark = monthly_return_series(
        spy_buy_hold.records, initial_equity=INITIAL_CASH_USD
    )
    r_qqq = monthly_return_series(
        qqq_buy_hold.records, initial_equity=INITIAL_CASH_USD
    )

    gates = sample_gates_from_records(sleeve_base.records)
    if (
        len(r_sleeve) != len(SCORING_MONTHS)
        or len(r_benchmark) != len(SCORING_MONTHS)
    ):
        return _blocked_payload(
            manifest_sha256,
            "month records do not cover every scoring month "
            f"(sleeve {len(r_sleeve)}, benchmark {len(r_benchmark)}, "
            f"expected {len(SCORING_MONTHS)})",
            source_hashes,
        )

    # ---------- Item 2/3 (14.9): the closing identity.  The last
    # scored month includes the registered next-open liquidation AND
    # any receivable outstanding at the sealed end, so the product of
    # monthly returns must equal cash + outstanding receivables for
    # EVERY leg - base, stress, benchmark AND the withholding leg.
    # Any mismatch is a bookkeeping defect -> DATA_BLOCKED, never a
    # silent number.
    for leg_name, leg, returns in (
        ("sleeve_base", sleeve_base, r_sleeve),
        ("sleeve_stress", sleeve_stress, r_stress),
        ("spy_buy_hold", spy_buy_hold, r_benchmark),
        ("qqq_buy_hold", qqq_buy_hold, r_qqq),
        (
            "sleeve_withholding",
            sleeve_withholding,
            monthly_return_series(
                sleeve_withholding.records,
                initial_equity=INITIAL_CASH_USD,
            ),
        ),
    ):
        compounded = float(INITIAL_CASH_USD)
        for value in returns:
            compounded *= 1.0 + value
        outstanding = sum(
            (r.amount_usd for r in leg.outstanding_receivables),
            Decimal("0"),
        )
        terminal = float(leg.terminal_cash_after_liquidation + outstanding)
        if not math.isclose(
            compounded, terminal, rel_tol=1e-9, abs_tol=1e-6
        ):
            return _blocked_payload(
                manifest_sha256,
                f"closing identity failed for {leg_name}: "
                f"initial x prod(1+r) = {compounded:.6f} but terminal "
                f"equity (cash + receivables) = {terminal:.6f}",
                source_hashes,
            )

    claims = evaluate_claims(
        sleeve_monthly_returns=r_sleeve,
        benchmark_monthly_returns=r_benchmark,
        stress_sleeve_monthly_returns=r_stress,
    )
    if not gates.sufficient:
        # Distinct verdict (contract §6.3): a too-small sample is never
        # INCONCLUSIVE and never judged on its bounds.
        verdict = VERDICT_INSUFFICIENT_DATA
    else:
        verdict = decide_verdict(gates=gates, claims=claims)

    withholding_disclosure = descriptive_statistics(
        sleeve_monthly_returns=r_sleeve,
        qqq_monthly_returns=r_qqq,
    )
    payload: dict[str, Any] = {
        "analysis_id": ANALYSIS_ID,
        "rule": RULE_NAME,
        "verdict": verdict,
        "verdict_reasons": list(gates.failures())
        or [
            (
                "all four claims hold at their one-sided 95% lower "
                "bounds"
                if verdict == VERDICT_CORROBORATES_RISK_MANAGEMENT_VALUE
                else "see claim bounds"
            )
        ],
        "verdict_statement": _VERDICT_STATEMENT,
        "sample_gates": {
            "months": gates.months,
            "cash_months": gates.cash_months,
            "invested_months": gates.invested_months,
            "sufficient": gates.sufficient,
            "failures": list(gates.failures()),
        },
        "claims": {
            "claim1_positive_expectancy": _bounds_json(
                claims.claim1_positive_expectancy
            ),
            "claim2_monthly_giveup": _bounds_json(claims.claim2_giveup),
            "claim3_downside_second_moment": _bounds_json(
                claims.claim3_downside
            ),
            "claim4_stress_positive": _bounds_json(
                claims.claim4_stress_positive
            ),
            "thresholds": {
                "claim1": CLAIM1_THRESHOLD,
                "claim2": CLAIM2_THRESHOLD,
                "claim3": CLAIM3_THRESHOLD,
                "claim4": CLAIM4_THRESHOLD,
            },
            "degenerate": claims.degenerate,
        },
        "statistics": {
            "bootstrap": {
                "method": "circular moving-block bootstrap",
                "block_length": BOOTSTRAP_CONFIG.block_length,
                "resamples": BOOTSTRAP_CONFIG.resamples,
                "seed": BOOTSTRAP_CONFIG.seed,
                "bounds": "one-sided 95% percentile (5th / 95th)",
            },
            "n_months": len(r_sleeve),
        },
        "execution": {
            "entry_exit_events": [
                {
                    "session": session.isoformat(),
                    "side": side,
                }
                for session, side in events
            ],
            "liquidation_session": liquidation_session.isoformat(),
            "buy_hold_entry_session": buy_hold_entry_session.isoformat(),
            "sleeve_fills": [
                {
                    "session": fill.session.isoformat(),
                    "side": fill.side,
                    "shares": fill.shares,
                    "fill_price": fill.fill_price,
                    "commission_usd": str(fill.commission_usd),
                }
                for fill in sleeve_base.fills
            ],
            "sleeve_terminal_cash_after_liquidation": str(
                sleeve_base.terminal_cash_after_liquidation
            ),
            "benchmark_terminal_cash_after_liquidation": str(
                spy_buy_hold.terminal_cash_after_liquidation
            ),
            "final_outstanding_receivable_usd": (
                str(
                    sum(
                        (
                            r.amount_usd
                            for r in sleeve_base.outstanding_receivables
                        ),
                        Decimal("0"),
                    )
                )
                or "0"
            ),
            "final_outstanding_receivable_count": len(
                sleeve_base.outstanding_receivables
            ),
        },
        "dividend_withholding_disclosure": {
            "rate": DIVIDEND_WITHHOLDING_RATE,
            "computed": True,
            "terminal_cash_after_liquidation": str(
                sleeve_withholding.terminal_cash_after_liquidation
            ),
            "monthly_returns": monthly_return_series(
                sleeve_withholding.records,
                initial_equity=INITIAL_CASH_USD,
            ),
            "note": (
                "disclosure only, never gating: the identical sleeve "
                "under a 30% dividend withholding; the registered "
                "claims run pre-personal-tax"
            ),
        },
        "descriptive": withholding_disclosure,
        "ohlc_anomaly_exemptions": {
            "decision": "SPY_MONTHLY_SMA10_PREREGISTRATION.md 14.11",
            "entries": len(
                preflight["anomaly_ledger"].get("anomalies", [])
            ),
            "sessions": [
                str(entry.get("session"))
                for entry in preflight["anomaly_ledger"].get(
                    "anomalies", []
                )
            ],
            "relations": [
                relation
                for entry in preflight["anomaly_ledger"].get(
                    "anomalies", []
                )
                for relation in entry.get("violated_relations", [])
            ],
            "ledger_sha256": _file_sha256(_ANOMALY_LEDGER_PATH),
            "note": (
                "the single registered open>high relation on SPY.US "
                "2020-11-18 is exempt (a non-execution day whose open "
                "is outside the read set); the bar is never skipped, "
                "its close stays checked, and the exemption never "
                "asserts any field is correct"
            ),
        },
        "monthly_returns": {
            "sleeve_base": r_sleeve,
            "sleeve_stress": r_stress,
            "spy_buy_hold": r_benchmark,
            "qqq_buy_hold": r_qqq,
            "months": [
                f"{year:04d}-{month:02d}" for year, month in SCORING_MONTHS
            ],
        },
        "provenance": {
            "git_head": _git_head(),
            "source_hashes": source_hashes,
            "input_manifest_sha256": manifest_sha256,
            "corporate_actions_sha256": manifest["corporate_actions"][
                "sha256"
            ],
            "corporate_actions_source_url": manifest["corporate_actions"][
                "source_url"
            ],
            "cli_version": REPLAY_CLI_VERSION,
        },
        "rerun_reason": rerun_reason,
    }
    return payload


_VERDICT_STATEMENT = (
    "CORROBORATES_RISK_MANAGEMENT_VALUE authorises research priority "
    "only: it is not a forward PASS, not stable monthly profit, and "
    "authorises no orders, no live switching and no promotion; "
    "DOES_NOT_CORROBORATE is material for a written abandonment "
    "decision, nothing more"
)


def _bounds_json(bounds: Any) -> dict[str, float | None]:
    return {
        "mean": bounds.mean,
        "lower_95_one_sided": bounds.lower,
        "upper_95_one_sided": bounds.upper,
    }


def _blocked_payload(
    manifest_sha256: str,
    reason: str,
    source_hashes: dict[str, str],
) -> dict[str, Any]:
    return {
        "analysis_id": ANALYSIS_ID,
        "rule": RULE_NAME,
        "verdict": VERDICT_DATA_BLOCKED,
        "verdict_reasons": [reason],
        "verdict_statement": _VERDICT_STATEMENT,
        "sample_gates": {
            "months": 0,
            "cash_months": 0,
            "invested_months": 0,
            "sufficient": False,
            "failures": [reason],
        },
        "claims": None,
        "provenance": {
            "input_manifest_sha256": manifest_sha256,
            "source_hashes": source_hashes,
            "cli_version": REPLAY_CLI_VERSION,
        },
        "data_blocked_reason": reason,
    }


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


# --------------------------------------------------------------------- CLI


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spy_monthly_sma10_replay",
        description=(
            "Registered research replay of the frozen monthly-trend rule "
            "SPY_MONTHLY_SMA10_CASH_V1 on 2012-01..2021-12 "
            "(record-only; never orders)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser(
        "plan", help="offline returns-blind plan (no data touched)"
    )
    plan_parser.add_argument("--output", type=Path, required=True)

    fetch_parser = subparsers.add_parser(
        "fetch", help="QuoteContext-only RAW daily fetch into the cache"
    )
    fetch_parser.add_argument("--cache-dir", type=Path, default=None)
    fetch_parser.add_argument("--plan", type=Path, required=True)
    fetch_parser.add_argument("--rate", type=float, default=None)

    import_parser = subparsers.add_parser(
        "import-corporate-actions",
        help="hash, validate and seal the corporate-actions input file",
    )
    import_parser.add_argument("--cache-dir", type=Path, default=None)
    import_parser.add_argument("--file", type=Path, required=True)
    import_parser.add_argument("--source-url", required=True)
    import_parser.add_argument(
        "--reason", default=None, help="required to replace an existing file"
    )

    seal_parser = subparsers.add_parser(
        "seal", help="write the sealed input manifest and print its hash"
    )
    seal_parser.add_argument("--cache-dir", type=Path, default=None)
    seal_parser.add_argument(
        "--reseal-reason",
        default=None,
        help="required to re-seal; the previous receipt is preserved",
    )

    evaluate_parser = subparsers.add_parser(
        "evaluate", help="one-pass evaluation of the sealed input set"
    )
    evaluate_parser.add_argument("--cache-dir", type=Path, default=None)
    evaluate_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="REQUIRED result path OUTSIDE the cache directory",
    )
    evaluate_parser.add_argument("--rerun-reason", default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            payload = build_plan_payload()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(args.output, payload)
            print(
                json.dumps(
                    {
                        "analysis_id": payload["analysis_id"],
                        "symbols": payload["symbols"],
                        "estimated_requests_total": payload[
                            "estimated_requests_total"
                        ],
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
            plan_payload = json.loads(
                args.plan.read_text(encoding="utf-8")
            )
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
        if args.command == "import-corporate-actions":
            cache_dir = (
                args.cache_dir
                if args.cache_dir is not None
                else _default_cache_dir()
            )
            report = run_import_corporate_actions(
                cache_dir=cache_dir,
                file_path=args.file,
                source_url=args.source_url,
                reason=args.reason,
            )
            print(json.dumps(report, ensure_ascii=True, sort_keys=True))
            return 0
        if args.command == "seal":
            cache_dir = (
                args.cache_dir
                if args.cache_dir is not None
                else _default_cache_dir()
            )
            manifest_hash = run_seal(
                cache_dir, reseal_reason=args.reseal_reason
            )
            print(manifest_hash)
            return 0
        if args.command == "evaluate":
            cache_dir = (
                args.cache_dir
                if args.cache_dir is not None
                else _default_cache_dir()
            )
            payload = run_evaluate(
                cache_dir=cache_dir,
                output_path=args.output,
                rerun_reason=args.rerun_reason,
            )
            print(
                json.dumps(
                    {
                        "analysis_id": payload["analysis_id"],
                        "verdict": payload["verdict"],
                        "months": payload["sample_gates"]["months"],
                        "git_head": (
                            payload["provenance"].get("git_head")
                        ),
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                )
            )
            return 0
        parser.error(f"unknown command: {args.command}")
        return 2
    except (
        MonthlySma10Error,
        HistoricalReplayError,
        MonthlyTrendError,
        OSError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
