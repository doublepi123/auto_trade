from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    localcontext,
)
from functools import lru_cache
import re
from types import MappingProxyType

from app.core.holiday_calendar import (
    COVERAGE_END_YEAR,
    COVERAGE_START_YEAR,
    is_market_closed,
)
from app.core.market_calendar import get_session

from app.domain.watchlist_quant_v6.artifact import (
    MAX_QUANT_V6_ARTIFACT_COMPRESSED_BYTES,
    MAX_QUANT_V6_ARTIFACT_JSON_DEPTH,
    MAX_QUANT_V6_ARTIFACT_RAW_BYTES,
    MAX_QUANT_V6_DECIMAL_ADJUSTED_EXPONENT,
    MAX_QUANT_V6_DECIMAL_DIGITS,
    QUANT_V6_ARTIFACT_CODEC,
    QUANT_V6_ARTIFACT_COMPRESSION_LEVEL,
    QUANT_V6_ARTIFACT_SCHEMA_VERSION,
    QUANT_V6_ASSESSMENT_CONTRACT,
    QUANT_V6_ASSESSMENT_ARTIFACT_KIND,
    QUANT_V6_EVENT_CONTRACT,
    QUANT_V6_EVENT_ARTIFACT_KIND,
    QUANT_V6_PAYLOAD_SCHEMA_VERSION,
    QUANT_V6_SESSION_INPUT_CONTRACT,
    QUANT_V6_SESSION_INPUT_ARTIFACT_KIND,
    EncodedQuantV6Artifact,
    QuantV6ArtifactError,
    canonical_decimal,
    canonical_utc_timestamp,
    encode_quant_v6_artifact,
    quant_v6_payload_sha256,
)


QUANT_V6_ALGORITHM_VERSION = "quant-v6-bar-next-open-stressed-v1"
BAR_NEXT_OPEN_STRESSED = "BAR_NEXT_OPEN_STRESSED"
QUANT_V6_BAR_MINUTES = 5
QUANT_V6_ENTRY_OFFSET_BARS = 1
QUANT_V6_HOLDING_BARS = 6
QUANT_V6_EXIT_OFFSET_BARS = 7
QUANT_V6_HISTORICAL_STRESS_BPS_PER_SIDE = Decimal("8")
QUANT_V6_REFERENCE_QUANTITY = Decimal("1")
QUANT_V6_ASSESSMENT_SESSIONS = 30
QUANT_V6_MIN_COVERED_SESSIONS = 29
QUANT_V6_MIN_EVENTS = 60
QUANT_V6_MIN_EVENT_SESSIONS = 20
QUANT_V6_MIN_EDGE_COST_RATIO = Decimal("2")
QUANT_V6_THRESHOLD_TRAINING_SESSIONS = 10
QUANT_V6_MIN_THRESHOLD_RETURNS = 120
QUANT_V6_SHOCK_PERCENTILE = Decimal("0.75")
QUANT_V6_FEE_RATE_BY_MARKET: Mapping[str, Decimal] = MappingProxyType({
    "US": Decimal("0.0005"),
    "HK": Decimal("0.003"),
})
_SYMBOL_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9._-]*\.(US|HK)")

# One-sided p=0.90 Student-t critical values. The candidate contract requires
# at least 20 event sessions, but the complete table makes diagnostics stable
# for immature windows too and avoids a SciPy/runtime-version dependency.
SESSION_CLUSTER_T90_BY_DF: Mapping[int, Decimal] = MappingProxyType({
    1: Decimal("3.077683537"),
    2: Decimal("1.885618083"),
    3: Decimal("1.637744354"),
    4: Decimal("1.533206274"),
    5: Decimal("1.475884049"),
    6: Decimal("1.439755747"),
    7: Decimal("1.414923928"),
    8: Decimal("1.396815310"),
    9: Decimal("1.383028739"),
    10: Decimal("1.372183641"),
    11: Decimal("1.363430319"),
    12: Decimal("1.356217334"),
    13: Decimal("1.350171289"),
    14: Decimal("1.345030374"),
    15: Decimal("1.340605608"),
    16: Decimal("1.336757167"),
    17: Decimal("1.333379389"),
    18: Decimal("1.330390943"),
    19: Decimal("1.327728209"),
    20: Decimal("1.325340707"),
    21: Decimal("1.323187874"),
    22: Decimal("1.321236742"),
    23: Decimal("1.319460239"),
    24: Decimal("1.317835934"),
    25: Decimal("1.316345073"),
    26: Decimal("1.314971864"),
    27: Decimal("1.313702909"),
    28: Decimal("1.312526782"),
    29: Decimal("1.311433647"),
})


def _deep_freeze_semantic_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({
            key: _deep_freeze_semantic_value(item)
            for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze_semantic_value(item) for item in value)
    return value


def _deep_freeze_semantic_mapping(
    value: Mapping[str, object],
) -> Mapping[str, object]:
    return MappingProxyType({
        key: _deep_freeze_semantic_value(item)
        for key, item in value.items()
    })

QUANT_V6_SEMANTIC_SPEC: Mapping[str, object] = _deep_freeze_semantic_mapping({
    "algorithm_version": QUANT_V6_ALGORITHM_VERSION,
    "artifact_envelope": {
        "codec": QUANT_V6_ARTIFACT_CODEC,
        "compression_level": QUANT_V6_ARTIFACT_COMPRESSION_LEVEL,
        "kinds": {
            "assessment": QUANT_V6_ASSESSMENT_ARTIFACT_KIND,
            "event": QUANT_V6_EVENT_ARTIFACT_KIND,
            "session_input": QUANT_V6_SESSION_INPUT_ARTIFACT_KIND,
        },
        "limits": {
            "compressed_bytes": MAX_QUANT_V6_ARTIFACT_COMPRESSED_BYTES,
            "decimal_adjusted_exponent": MAX_QUANT_V6_DECIMAL_ADJUSTED_EXPONENT,
            "decimal_digits": MAX_QUANT_V6_DECIMAL_DIGITS,
            "json_depth": MAX_QUANT_V6_ARTIFACT_JSON_DEPTH,
            "raw_bytes": MAX_QUANT_V6_ARTIFACT_RAW_BYTES,
        },
        "schema_version": QUANT_V6_ARTIFACT_SCHEMA_VERSION,
    },
    "assessment": {
        "common_session_denominator": QUANT_V6_ASSESSMENT_SESSIONS,
        "current_capture_mode": BAR_NEXT_OPEN_STRESSED,
        "current_capture_promotion_eligible": False,
        "future_candidate_required_capture_mode": "FULL_EVENT_BBO_VERIFIED",
        "minimum_covered_sessions": QUANT_V6_MIN_COVERED_SESSIONS,
        "minimum_edge_cost_ratio": QUANT_V6_MIN_EDGE_COST_RATIO,
        "minimum_event_sessions": QUANT_V6_MIN_EVENT_SESSIONS,
        "minimum_events": QUANT_V6_MIN_EVENTS,
        "minimum_median_net_bps": "STRICTLY_POSITIVE",
        "minimum_session_cluster_lcb_bps": "STRICTLY_POSITIVE",
        "session_cluster_lcb": "EQUAL_WEIGHT_SESSION_RETURN_STUDENT_T_ONE_SIDED_90",
        "session_cluster_missing_session_treatment": "EXCLUDED_NOT_ZERO",
        "session_cluster_sample": "ALL_COVERED_SESSIONS",
        "student_t_critical_by_df": {
            str(key): value for key, value in SESSION_CLUSTER_T90_BY_DF.items()
        },
        "zero_event_covered_session_return_bps": Decimal("0"),
    },
    "event": {
        "bar_evidence": "PREVIOUS_SIGNAL_HOLDING_EXIT_FULL_OHLCV",
        "bar_minutes": QUANT_V6_BAR_MINUTES,
        "capture_mode": BAR_NEXT_OPEN_STRESSED,
        "entry_offset_bars": QUANT_V6_ENTRY_OFFSET_BARS,
        "entry_reference": "NEXT_BAR_OPEN",
        "exit_offset_bars": QUANT_V6_EXIT_OFFSET_BARS,
        "exit_reference": "BAR_I_PLUS_7_OPEN",
        "historical_stress_bps_per_side": (
            QUANT_V6_HISTORICAL_STRESS_BPS_PER_SIDE
        ),
        "holding_bars": QUANT_V6_HOLDING_BARS,
        "fee_rate_by_market": dict(QUANT_V6_FEE_RATE_BY_MARKET),
        "fee_snapshot": "ENTRY_AND_EXIT_COMPONENTS",
        "position_add_on_allowed": False,
        "reference_quantity": QUANT_V6_REFERENCE_QUANTITY,
        "side": "LONG",
        "signal": "NEGATIVE_CLOSE_TO_CLOSE_LOG_RETURN_AT_OR_BELOW_THRESHOLD",
        "signal_overlap_allowed": False,
        "threshold": {
            "minimum_returns": QUANT_V6_MIN_THRESHOLD_RETURNS,
            "percentile": QUANT_V6_SHOCK_PERCENTILE,
            "source": "EXACT_PRIOR_COMPLETE_RTH_SESSIONS",
            "training_sessions": QUANT_V6_THRESHOLD_TRAINING_SESSIONS,
        },
    },
    "payload_contracts": {
        "assessment": QUANT_V6_ASSESSMENT_CONTRACT,
        "event": QUANT_V6_EVENT_CONTRACT,
        "schema_version": QUANT_V6_PAYLOAD_SCHEMA_VERSION,
        "session_input": QUANT_V6_SESSION_INPUT_CONTRACT,
    },
    "p0": {
        "automatic_promotion_allowed": False,
        "order_submission_allowed": False,
        "short_entry_allowed": False,
    },
    "schema_version": QUANT_V6_PAYLOAD_SCHEMA_VERSION,
})
QUANT_V6_SEMANTIC_DIGEST = quant_v6_payload_sha256(QUANT_V6_SEMANTIC_SPEC)


class QuantV6SemanticError(ValueError):
    """Raised when inputs cannot satisfy the frozen quant-v6 event contract."""


def _decimal(value: Decimal | int | str, *, label: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise QuantV6SemanticError(f"{label} must be a decimal")
    try:
        candidate = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise QuantV6SemanticError(f"{label} must be a decimal") from exc
    if not candidate.is_finite():
        raise QuantV6SemanticError(f"{label} must be finite")
    try:
        canonical_decimal(candidate)
    except QuantV6ArtifactError as exc:
        raise QuantV6SemanticError(str(exc)) from exc
    return candidate


def _canonical_symbol(value: str) -> str:
    if not isinstance(value, str):
        raise QuantV6SemanticError("symbol must be a string")
    if (
        not value
        or value != value.strip()
        or value != value.upper()
        or len(value) > 50
        or _SYMBOL_PATTERN.fullmatch(value) is None
    ):
        raise QuantV6SemanticError("symbol must be canonical uppercase text")
    return value


def _canonical_market(value: str) -> str:
    if value not in {"US", "HK"}:
        raise QuantV6SemanticError("market must be US or HK")
    return value


def _validate_symbol_market_pair(symbol: str, market: str) -> None:
    if not symbol.endswith(f".{market}"):
        raise QuantV6SemanticError("symbol suffix must match its market")


def validate_quant_v6_symbol_market(symbol: str, market: str) -> tuple[str, str]:
    normalized_symbol = _canonical_symbol(symbol)
    normalized_market = _canonical_market(market)
    _validate_symbol_market_pair(normalized_symbol, normalized_market)
    return normalized_symbol, normalized_market


def quant_v6_fee_rate(market: str) -> Decimal:
    return QUANT_V6_FEE_RATE_BY_MARKET[_canonical_market(market)]


@dataclass(frozen=True)
class QuantV6Bar:
    start_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        if (
            not isinstance(self.start_at, datetime)
            or self.start_at.tzinfo is None
            or self.start_at.utcoffset() is None
        ):
            raise QuantV6SemanticError("bar start_at must be timezone-aware")
        normalized_at = self.start_at.astimezone(timezone.utc)
        if (
            normalized_at.second != 0
            or normalized_at.microsecond != 0
            or normalized_at.minute % QUANT_V6_BAR_MINUTES != 0
        ):
            raise QuantV6SemanticError("bar start_at must align to a 5-minute boundary")
        object.__setattr__(self, "start_at", normalized_at)
        for field_name in ("open", "high", "low", "close", "volume"):
            object.__setattr__(
                self,
                field_name,
                _decimal(getattr(self, field_name), label=f"bar {field_name}"),
            )
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise QuantV6SemanticError("bar prices must be positive")
        if self.volume < 0:
            raise QuantV6SemanticError("bar volume must be non-negative")
        if not (
            self.low <= self.open <= self.high
            and self.low <= self.close <= self.high
        ):
            raise QuantV6SemanticError("bar OHLC values are inconsistent")

    @property
    def end_at(self) -> datetime:
        return self.start_at + timedelta(minutes=QUANT_V6_BAR_MINUTES)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "close": canonical_decimal(self.close),
            "high": canonical_decimal(self.high),
            "low": canonical_decimal(self.low),
            "open": canonical_decimal(self.open),
            "start_at": canonical_utc_timestamp(self.start_at),
            "volume": canonical_decimal(self.volume),
        }


@dataclass(frozen=True)
class QuantV6TrainingSession:
    session_date: date
    bars: tuple[QuantV6Bar, ...]

    def __post_init__(self) -> None:
        if type(self.session_date) is not date:
            raise QuantV6SemanticError("training session_date must be a date")
        object.__setattr__(self, "bars", tuple(self.bars))


@dataclass(frozen=True)
class QuantV6ThresholdEvidence:
    symbol: str
    market: str
    target_session_date: date
    training_sessions: tuple[QuantV6TrainingSession, ...]
    shock_threshold_bps: Decimal
    preimage_digest_sha256: str

    def canonical_preimage(self) -> dict[str, object]:
        return {
            "market": self.market,
            "minimum_returns": QUANT_V6_MIN_THRESHOLD_RETURNS,
            "percentile": canonical_decimal(QUANT_V6_SHOCK_PERCENTILE),
            "symbol": self.symbol,
            "target_session_date": self.target_session_date.isoformat(),
            "training_sessions": [
                _training_session_preimage(
                    symbol=self.symbol,
                    market=self.market,
                    training_session=item,
                )
                for item in self.training_sessions
            ],
            "training_sessions_required": QUANT_V6_THRESHOLD_TRAINING_SESSIONS,
        }

    def canonical_payload(self) -> dict[str, object]:
        return {
            **self.canonical_preimage(),
            "preimage_digest_sha256": self.preimage_digest_sha256,
            "shock_threshold_bps": canonical_decimal(self.shock_threshold_bps),
        }


def quant_v6_expected_rth_bar_starts(
    market: str,
    session_date: date,
) -> tuple[datetime, ...]:
    """Return the exact exchange-local 5-minute grid for one covered day."""
    normalized_market = _canonical_market(market)
    _validate_calendar_year(session_date)
    session = get_session(normalized_market)
    if session_date.weekday() >= 5 or is_market_closed(normalized_market, session_date):
        return ()
    cursor = datetime.combine(
        session_date,
        session.rth_open,
        tzinfo=session.timezone,
    )
    close_at = datetime.combine(
        session_date,
        session.close_time(session_date),
        tzinfo=session.timezone,
    )
    starts: list[datetime] = []
    while cursor < close_at:
        local_time = cursor.time()
        in_lunch = (
            session.lunch_start is not None
            and session.lunch_end is not None
            and session.lunch_start <= local_time < session.lunch_end
        )
        if not in_lunch:
            starts.append(cursor.astimezone(timezone.utc))
        cursor += timedelta(minutes=QUANT_V6_BAR_MINUTES)
    return tuple(starts)


def quant_v6_previous_trading_session_dates(
    market: str,
    target_session_date: date,
    *,
    count: int,
) -> tuple[date, ...]:
    normalized_market = _canonical_market(market)
    _validate_calendar_year(target_session_date)
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise QuantV6SemanticError("trading-session count must be positive")
    values: list[date] = []
    cursor = target_session_date - timedelta(days=1)
    while len(values) < count:
        _validate_calendar_year(cursor)
        if cursor.weekday() < 5 and not is_market_closed(normalized_market, cursor):
            values.append(cursor)
        cursor -= timedelta(days=1)
    values.reverse()
    return tuple(values)


def quant_v6_consecutive_trading_session_dates(
    market: str,
    first_session_date: date,
    *,
    count: int,
) -> tuple[date, ...]:
    normalized_market = _canonical_market(market)
    _validate_calendar_year(first_session_date)
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise QuantV6SemanticError("trading-session count must be positive")
    values: list[date] = []
    cursor = first_session_date
    while len(values) < count:
        _validate_calendar_year(cursor)
        if cursor.weekday() < 5 and not is_market_closed(normalized_market, cursor):
            values.append(cursor)
        cursor += timedelta(days=1)
    if values[0] != first_session_date:
        raise QuantV6SemanticError("first session date must be an open trading day")
    return tuple(values)


def build_quant_v6_threshold_evidence(
    *,
    symbol: str,
    market: str,
    target_session_date: date,
    training_sessions: Sequence[QuantV6TrainingSession],
) -> QuantV6ThresholdEvidence:
    normalized_symbol = _canonical_symbol(symbol)
    normalized_market = _canonical_market(market)
    _validate_symbol_market_pair(normalized_symbol, normalized_market)
    if not quant_v6_expected_rth_bar_starts(normalized_market, target_session_date):
        raise QuantV6SemanticError("target session date must be an open trading day")
    sessions = tuple(training_sessions)
    threshold, preimage_digest = _threshold_calculation(
        normalized_symbol,
        normalized_market,
        target_session_date,
        sessions,
    )
    return QuantV6ThresholdEvidence(
        symbol=normalized_symbol,
        market=normalized_market,
        target_session_date=target_session_date,
        training_sessions=sessions,
        shock_threshold_bps=threshold,
        preimage_digest_sha256=preimage_digest,
    )


@lru_cache(maxsize=128)
def _threshold_calculation(
    normalized_symbol: str,
    normalized_market: str,
    target_session_date: date,
    sessions: tuple[QuantV6TrainingSession, ...],
) -> tuple[Decimal, str]:
    """Cache only immutable derived values, never a validation-success result."""
    if len(sessions) != QUANT_V6_THRESHOLD_TRAINING_SESSIONS:
        raise QuantV6SemanticError("threshold requires exactly 10 training sessions")
    expected_dates = quant_v6_previous_trading_session_dates(
        normalized_market,
        target_session_date,
        count=QUANT_V6_THRESHOLD_TRAINING_SESSIONS,
    )
    if tuple(item.session_date for item in sessions) != expected_dates:
        raise QuantV6SemanticError(
            "threshold training sessions must be the exact prior 10 trading days"
        )
    absolute_returns: list[Decimal] = []
    for item in sessions:
        _validate_complete_session_bars(
            item.bars,
            market=normalized_market,
            session_date=item.session_date,
        )
        absolute_returns.extend(_training_session_absolute_returns(item))
    if len(absolute_returns) < QUANT_V6_MIN_THRESHOLD_RETURNS:
        raise QuantV6SemanticError("threshold preimage has insufficient returns")
    threshold = _decimal_percentile(
        absolute_returns,
        QUANT_V6_SHOCK_PERCENTILE,
    )
    if threshold <= 0:
        raise QuantV6SemanticError("shock threshold must be positive")
    provisional = QuantV6ThresholdEvidence(
        symbol=normalized_symbol,
        market=normalized_market,
        target_session_date=target_session_date,
        training_sessions=sessions,
        shock_threshold_bps=threshold,
        preimage_digest_sha256="",
    )
    return (
        threshold,
        quant_v6_payload_sha256(provisional.canonical_preimage()),
    )


def validate_quant_v6_threshold_evidence(
    evidence: QuantV6ThresholdEvidence,
) -> None:
    if not isinstance(evidence, QuantV6ThresholdEvidence):
        raise QuantV6SemanticError("threshold evidence has an unsupported type")
    rebuilt = build_quant_v6_threshold_evidence(
        symbol=evidence.symbol,
        market=evidence.market,
        target_session_date=evidence.target_session_date,
        training_sessions=evidence.training_sessions,
    )
    if rebuilt != evidence:
        raise QuantV6SemanticError("threshold evidence failed canonical replay")


def quant_v6_session_bars_sha256(
    *,
    symbol: str,
    market: str,
    session_date: date,
    bars: Sequence[QuantV6Bar],
) -> str:
    normalized_symbol = _canonical_symbol(symbol)
    normalized_market = _canonical_market(market)
    _validate_symbol_market_pair(normalized_symbol, normalized_market)
    normalized_bars = tuple(bars)
    _validate_complete_session_bars(
        normalized_bars,
        market=normalized_market,
        session_date=session_date,
    )
    return quant_v6_payload_sha256({
        "bar_minutes": QUANT_V6_BAR_MINUTES,
        "bars": [item.canonical_payload() for item in normalized_bars],
        "market": normalized_market,
        "session_date": session_date.isoformat(),
        "symbol": normalized_symbol,
    })


def build_bar_next_open_stressed_session_events(
    *,
    symbol: str,
    market: str,
    session_date: date,
    bars: Sequence[QuantV6Bar],
    threshold_evidence: QuantV6ThresholdEvidence,
    fee_rate: Decimal | int | str,
) -> tuple[BarNextOpenStressedEvent, ...]:
    """Replay a complete RTH day, splitting exchange lunch boundaries."""
    normalized_bars = tuple(bars)
    _validate_complete_session_bars(
        normalized_bars,
        market=market,
        session_date=session_date,
    )
    events: list[BarNextOpenStressedEvent] = []
    for segment in _contiguous_bar_segments(normalized_bars):
        events.extend(build_bar_next_open_stressed_events(
            symbol=symbol,
            market=market,
            session_date=session_date,
            bars=segment,
            threshold_evidence=threshold_evidence,
            fee_rate=fee_rate,
        ))
    return tuple(events)


def _training_session_preimage(
    *,
    symbol: str,
    market: str,
    training_session: QuantV6TrainingSession,
) -> dict[str, object]:
    returns = _training_session_absolute_returns(training_session)
    return {
        "absolute_log_returns_bps": [
            canonical_decimal(value) for value in returns
        ],
        "bar_input_sha256": quant_v6_session_bars_sha256(
            symbol=symbol,
            market=market,
            session_date=training_session.session_date,
            bars=training_session.bars,
        ),
        "bars": [item.canonical_payload() for item in training_session.bars],
        "return_count": len(returns),
        "session_date": training_session.session_date.isoformat(),
    }


@lru_cache(maxsize=512)
def _training_session_absolute_returns(
    training_session: QuantV6TrainingSession,
) -> tuple[Decimal, ...]:
    return _absolute_log_returns_bps(training_session.bars)


def _absolute_log_returns_bps(
    bars: Sequence[QuantV6Bar],
) -> tuple[Decimal, ...]:
    values: list[Decimal] = []
    for segment in _contiguous_bar_segments(tuple(bars)):
        for previous, current in zip(segment, segment[1:]):
            with localcontext() as context:
                context.prec = 50
                context.rounding = ROUND_HALF_EVEN
                values.append(abs(context.multiply(
                    context.ln(context.divide(current.close, previous.close)),
                    Decimal("10000"),
                )))
    return tuple(values)


def _decimal_percentile(
    values: Sequence[Decimal],
    percentile: Decimal,
) -> Decimal:
    if not values:
        raise QuantV6SemanticError("percentile preimage must not be empty")
    ordered = sorted(values)
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        position = Decimal(len(ordered) - 1) * percentile
        lower = int(position.to_integral_value(rounding=ROUND_FLOOR))
        upper = min(lower + 1, len(ordered) - 1)
        fraction = position - Decimal(lower)
        return ordered[lower] * (Decimal("1") - fraction) + ordered[upper] * fraction


def _validate_complete_session_bars(
    bars: tuple[QuantV6Bar, ...],
    *,
    market: str,
    session_date: date,
) -> None:
    expected = quant_v6_expected_rth_bar_starts(market, session_date)
    if not expected:
        raise QuantV6SemanticError("session date must be an open trading day")
    if any(not isinstance(item, QuantV6Bar) for item in bars):
        raise QuantV6SemanticError("bars must contain QuantV6Bar values")
    if tuple(item.start_at for item in bars) != expected:
        raise QuantV6SemanticError(
            "covered session bars must match the complete canonical RTH grid"
        )


def _contiguous_bar_segments(
    bars: tuple[QuantV6Bar, ...],
) -> tuple[tuple[QuantV6Bar, ...], ...]:
    if not bars:
        return ()
    step = timedelta(minutes=QUANT_V6_BAR_MINUTES)
    segments: list[list[QuantV6Bar]] = [[bars[0]]]
    for previous, current in zip(bars, bars[1:]):
        if current.start_at - previous.start_at != step:
            segments.append([])
        segments[-1].append(current)
    return tuple(tuple(segment) for segment in segments)


def _validate_calendar_year(value: date) -> None:
    if type(value) is not date:
        raise QuantV6SemanticError("session date must be a date, not datetime")
    if not COVERAGE_START_YEAR <= value.year <= COVERAGE_END_YEAR:
        raise QuantV6SemanticError("session date is outside calendar coverage")


@dataclass(frozen=True)
class BarNextOpenStressedEvent:
    event_key_sha256: str
    symbol: str
    market: str
    session_date: date
    previous_bar: QuantV6Bar
    signal_bar: QuantV6Bar
    entry_bar: QuantV6Bar
    holding_bars: tuple[QuantV6Bar, ...]
    exit_bar: QuantV6Bar
    shock_return_bps: Decimal
    threshold_evidence: QuantV6ThresholdEvidence
    fee_rate: Decimal
    entry_reference_price: Decimal
    exit_reference_price: Decimal
    entry_fill_price: Decimal
    exit_fill_price: Decimal
    entry_reference_notional: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    gross_reference_pnl: Decimal
    gross_fill_pnl: Decimal
    total_fees: Decimal
    net_pnl: Decimal
    gross_edge_bps: Decimal
    cost_bps: Decimal
    net_return_bps: Decimal

    @property
    def entry_at(self) -> datetime:
        return self.entry_bar.start_at

    @property
    def exit_at(self) -> datetime:
        return self.exit_bar.start_at

    def canonical_payload(self) -> dict[str, object]:
        return {
            "algorithm_version": QUANT_V6_ALGORITHM_VERSION,
            "capture": {
                "historical_only": True,
                "mode": BAR_NEXT_OPEN_STRESSED,
                "promotion_eligible": False,
            },
            "contract": QUANT_V6_EVENT_CONTRACT,
            "costs": {
                "cost_bps": canonical_decimal(self.cost_bps),
                "entry_fee": canonical_decimal(self.entry_fee),
                "entry_reference_notional": canonical_decimal(
                    self.entry_reference_notional
                ),
                "fee_rate": canonical_decimal(self.fee_rate),
                "exit_fee": canonical_decimal(self.exit_fee),
                "gross_fill_pnl": canonical_decimal(self.gross_fill_pnl),
                "gross_reference_pnl": canonical_decimal(
                    self.gross_reference_pnl
                ),
                "net_pnl": canonical_decimal(self.net_pnl),
                "net_return_bps": canonical_decimal(self.net_return_bps),
                "reference_gross_edge_bps": canonical_decimal(
                    self.gross_edge_bps
                ),
                "total_fees": canonical_decimal(self.total_fees),
            },
            "execution": {
                "entry_at": canonical_utc_timestamp(self.entry_at),
                "entry_fill_price": canonical_decimal(self.entry_fill_price),
                "entry_offset_bars": QUANT_V6_ENTRY_OFFSET_BARS,
                "entry_reference_price": canonical_decimal(
                    self.entry_reference_price
                ),
                "exit_at": canonical_utc_timestamp(self.exit_at),
                "exit_bar": self.exit_bar.canonical_payload(),
                "exit_fill_price": canonical_decimal(self.exit_fill_price),
                "exit_offset_bars": QUANT_V6_EXIT_OFFSET_BARS,
                "exit_reference_price": canonical_decimal(
                    self.exit_reference_price
                ),
                "historical_stress_bps_per_side": canonical_decimal(
                    QUANT_V6_HISTORICAL_STRESS_BPS_PER_SIDE
                ),
                "holding_bars": [
                    item.canonical_payload() for item in self.holding_bars
                ],
                "holding_bars_count": QUANT_V6_HOLDING_BARS,
                "overlap_allowed": False,
                "position_add_on_allowed": False,
                "quantity": canonical_decimal(QUANT_V6_REFERENCE_QUANTITY),
                "side": "LONG",
            },
            "identity": {
                "event_key_sha256": self.event_key_sha256,
                "market": self.market,
                "session_date": self.session_date.isoformat(),
                "signal_bar_start_at": canonical_utc_timestamp(
                    self.signal_bar.start_at
                ),
                "symbol": self.symbol,
            },
            "p0": {
                "automatic_promotion_allowed": False,
                "order_submission_allowed": False,
                "short_entry_allowed": False,
            },
            "schema_version": QUANT_V6_PAYLOAD_SCHEMA_VERSION,
            "semantic_digest": QUANT_V6_SEMANTIC_DIGEST,
            "signal": {
                "direction": "DOWN",
                "previous_bar": self.previous_bar.canonical_payload(),
                "shock_return_bps": canonical_decimal(self.shock_return_bps),
                "shock_threshold_bps": canonical_decimal(
                    self.threshold_evidence.shock_threshold_bps
                ),
                "signal_bar": self.signal_bar.canonical_payload(),
                "threshold_evidence": self.threshold_evidence.canonical_payload(),
                "trigger": "RETURN_LTE_NEGATIVE_THRESHOLD",
            },
        }

    def encoded_artifact(self) -> EncodedQuantV6Artifact:
        validate_bar_next_open_stressed_event(self)
        return encode_quant_v6_artifact(
            self.canonical_payload(),
            kind=QUANT_V6_EVENT_ARTIFACT_KIND,
        )

    @property
    def artifact_digest_sha256(self) -> str:
        validate_bar_next_open_stressed_event(self)
        return quant_v6_payload_sha256(self.canonical_payload())


def build_bar_next_open_stressed_events(
    *,
    symbol: str,
    market: str,
    session_date: date,
    bars: Sequence[QuantV6Bar],
    threshold_evidence: QuantV6ThresholdEvidence,
    fee_rate: Decimal | int | str,
) -> tuple[BarNextOpenStressedEvent, ...]:
    """Build non-overlapping historical long events from one RTH segment.

    This function never accepts a side, quantity, spread, quote, or fill-time
    override. Those omissions are part of the P0 contract.
    """
    normalized_symbol = _canonical_symbol(symbol)
    normalized_market = _canonical_market(market)
    _validate_symbol_market_pair(normalized_symbol, normalized_market)
    if type(session_date) is not date:
        raise QuantV6SemanticError("session_date must be a date")
    validate_quant_v6_threshold_evidence(threshold_evidence)
    if (
        threshold_evidence.symbol != normalized_symbol
        or threshold_evidence.market != normalized_market
        or threshold_evidence.target_session_date != session_date
    ):
        raise QuantV6SemanticError(
            "threshold evidence identity must match the target event session"
        )
    threshold = threshold_evidence.shock_threshold_bps
    normalized_fee_rate = _decimal(fee_rate, label="fee rate")
    if normalized_fee_rate != quant_v6_fee_rate(normalized_market):
        raise QuantV6SemanticError(
            "fee rate must equal the frozen market fee authority"
        )
    normalized_bars = tuple(bars)
    _validate_contiguous_bars(
        normalized_bars,
        market=normalized_market,
        session_date=session_date,
    )

    events: list[BarNextOpenStressedEvent] = []
    index = 1
    while index + QUANT_V6_EXIT_OFFSET_BARS < len(normalized_bars):
        previous = normalized_bars[index - 1]
        signal = normalized_bars[index]
        with localcontext() as context:
            context.prec = 50
            context.rounding = ROUND_HALF_EVEN
            shock_return_bps = context.multiply(
                context.ln(context.divide(signal.close, previous.close)),
                Decimal("10000"),
            )
        if shock_return_bps > -threshold:
            index += 1
            continue
        event = _build_event(
            symbol=normalized_symbol,
            market=normalized_market,
            session_date=session_date,
            bars=normalized_bars,
            signal_index=index,
            shock_return_bps=shock_return_bps,
            threshold_evidence=threshold_evidence,
            fee_rate=normalized_fee_rate,
        )
        events.append(event)
        # The next signal may begin on the exit bar, after this event has
        # flattened at that bar's open. No held bar can become another signal.
        index += QUANT_V6_EXIT_OFFSET_BARS
    return tuple(events)


def validate_bar_next_open_stressed_event(
    event: BarNextOpenStressedEvent,
) -> None:
    """Replay one self-contained event and reject any altered preimage field."""
    if not isinstance(event, BarNextOpenStressedEvent):
        raise QuantV6SemanticError("event has an unsupported type")
    local_bars = (
        event.previous_bar,
        event.signal_bar,
        *event.holding_bars,
        event.exit_bar,
    )
    rebuilt = build_bar_next_open_stressed_events(
        symbol=event.symbol,
        market=event.market,
        session_date=event.session_date,
        bars=local_bars,
        threshold_evidence=event.threshold_evidence,
        fee_rate=event.fee_rate,
    )
    if len(rebuilt) != 1 or rebuilt[0] != event:
        raise QuantV6SemanticError("event does not match its canonical replay")


def _validate_contiguous_bars(
    bars: tuple[QuantV6Bar, ...],
    *,
    market: str,
    session_date: date,
) -> None:
    session = get_session(market)
    for item in bars:
        if not isinstance(item, QuantV6Bar):
            raise QuantV6SemanticError("bars must contain QuantV6Bar values")
        if not session.is_rth(item.start_at):
            raise QuantV6SemanticError("event bars must be inside market RTH")
        if session.local(item.start_at).date() != session_date:
            raise QuantV6SemanticError(
                "event bars must belong to the declared market session"
            )
    expected_step = timedelta(minutes=QUANT_V6_BAR_MINUTES)
    for previous, current in zip(bars, bars[1:]):
        if current.start_at - previous.start_at != expected_step:
            raise QuantV6SemanticError(
                "bars must be ordered and contiguous within one RTH segment"
            )


def _build_event(
    *,
    symbol: str,
    market: str,
    session_date: date,
    bars: tuple[QuantV6Bar, ...],
    signal_index: int,
    shock_return_bps: Decimal,
    threshold_evidence: QuantV6ThresholdEvidence,
    fee_rate: Decimal,
) -> BarNextOpenStressedEvent:
    previous = bars[signal_index - 1]
    signal = bars[signal_index]
    entry = bars[signal_index + QUANT_V6_ENTRY_OFFSET_BARS]
    holding = tuple(
        bars[
            signal_index + QUANT_V6_ENTRY_OFFSET_BARS:
            signal_index + QUANT_V6_ENTRY_OFFSET_BARS + QUANT_V6_HOLDING_BARS
        ]
    )
    exit_bar = bars[signal_index + QUANT_V6_EXIT_OFFSET_BARS]
    if len(holding) != QUANT_V6_HOLDING_BARS or holding[0] != entry:
        raise QuantV6SemanticError("event does not contain exactly six holding bars")

    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        stress_rate = context.divide(
            QUANT_V6_HISTORICAL_STRESS_BPS_PER_SIDE,
            Decimal("10000"),
        )
        entry_reference = entry.open
        exit_reference = exit_bar.open
        entry_fill = context.multiply(
            entry_reference,
            Decimal("1") + stress_rate,
        )
        exit_fill = context.multiply(
            exit_reference,
            Decimal("1") - stress_rate,
        )
        quantity = QUANT_V6_REFERENCE_QUANTITY
        entry_reference_notional = context.multiply(entry_reference, quantity)
        entry_fee = context.multiply(
            context.multiply(entry_fill, quantity),
            fee_rate,
        )
        exit_fee = context.multiply(
            context.multiply(exit_fill, quantity),
            fee_rate,
        )
        total_fees = entry_fee + exit_fee
        gross_reference_pnl = context.multiply(
            exit_reference - entry_reference,
            quantity,
        )
        gross_fill_pnl = context.multiply(exit_fill - entry_fill, quantity)
        net_pnl = gross_fill_pnl - total_fees
        gross_edge_bps = context.multiply(
            context.divide(gross_reference_pnl, entry_reference_notional),
            Decimal("10000"),
        )
        cost_bps = context.multiply(
            context.divide(gross_reference_pnl - net_pnl, entry_reference_notional),
            Decimal("10000"),
        )
        net_return_bps = context.multiply(
            context.divide(net_pnl, entry_reference_notional),
            Decimal("10000"),
        )

    identity_preimage = {
        "algorithm_version": QUANT_V6_ALGORITHM_VERSION,
        "capture_mode": BAR_NEXT_OPEN_STRESSED,
        "market": market,
        "semantic_digest": QUANT_V6_SEMANTIC_DIGEST,
        "session_date": session_date.isoformat(),
        "signal_bar_start_at": canonical_utc_timestamp(signal.start_at),
        "symbol": symbol,
    }
    return BarNextOpenStressedEvent(
        event_key_sha256=quant_v6_payload_sha256(identity_preimage),
        symbol=symbol,
        market=market,
        session_date=session_date,
        previous_bar=previous,
        signal_bar=signal,
        entry_bar=entry,
        holding_bars=holding,
        exit_bar=exit_bar,
        shock_return_bps=shock_return_bps,
        threshold_evidence=threshold_evidence,
        fee_rate=fee_rate,
        entry_reference_price=entry_reference,
        exit_reference_price=exit_reference,
        entry_fill_price=entry_fill,
        exit_fill_price=exit_fill,
        entry_reference_notional=entry_reference_notional,
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        gross_reference_pnl=gross_reference_pnl,
        gross_fill_pnl=gross_fill_pnl,
        total_fees=total_fees,
        net_pnl=net_pnl,
        gross_edge_bps=gross_edge_bps,
        cost_bps=cost_bps,
        net_return_bps=net_return_bps,
    )
