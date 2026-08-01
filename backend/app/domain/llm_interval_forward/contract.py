from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Literal

from app.core.holiday_calendar import (
    COVERAGE_END_YEAR,
    COVERAGE_START_YEAR,
    is_market_closed,
)
from app.core.market_calendar import get_session, trade_day_for


REGISTRATION_SCHEMA_VERSION = "llm-interval-forward-registration-v1"
EXECUTION_POLICY_SCHEMA_VERSION = "llm-interval-forward-execution-policy-v1"
OBSERVATION_SCHEDULE_SCHEMA_VERSION = "llm-interval-forward-observation-schedule-v1"
SELECTION_RULE_VERSION = "first-auto-cron-primary-flat-low-confidence-v1"

PROPOSAL_ORIGIN = "AUTO_CRON"
REJECT_CODE = "LOW_CONFIDENCE"
TRADING_SESSION_MODE = "RTH_ONLY"
DATA_FIDELITY = "ONE_MINUTE_OHLCV"
BBO_COVERAGE = "NONE"
ENTRY_CROSSING_SEMANTICS = "BAR_LOCAL_CROSSING_APPROXIMATION"
FEE_MODEL_FIDELITY = "CONFIGURED_FEE_RATE_ESTIMATE"
TIMESTAMP_SEMANTICS = "START_STAMP_PLUS_ONE_MINUTE_OBSERVATION_TIME"

MAX_EXPECTED_SESSION_OBSERVATIONS = 1_000
MAX_CANONICAL_JSON_BYTES = 2 * 1024 * 1024
MAX_CANONICAL_JSON_DEPTH = 32
MAX_CANONICAL_JSON_NODES = 20_000
MAX_CANONICAL_CONTAINER_ITEMS = 2_000
MAX_CANONICAL_STRING_BYTES = 512 * 1024
MAX_CANONICAL_KEY_BYTES = 256
MAX_CANONICAL_INTEGER_ABS = 10**36
MAX_DECIMAL_DIGITS = 64
MAX_DECIMAL_ADJUSTED_EXPONENT = 128
MAX_INTERACTION_ID = 2**63 - 1
MAX_ANALYSIS_DURATION = timedelta(minutes=30)
MAX_REGISTRATION_DELAY = timedelta(minutes=5)
MAX_PRICE = Decimal("1000000000000")
MAX_VOLUME = Decimal("1000000000000000000")
MAX_QUANTITY = Decimal("1000000000000")
MAX_MONEY_AMOUNT = Decimal("1000000000000000000")
MAX_POLICY_RATIO = Decimal("1000000000000")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SYMBOL_RE = re.compile(r"[A-Z0-9][A-Z0-9.-]{0,31}\.(?:US|HK)")
_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_TEN_THOUSAND = Decimal("10000")
_FROZEN_POLICY_DECIMAL_PRECISION = 28


class IntervalForwardContractError(ValueError):
    """Raised when forward evidence violates the frozen v1 contract."""


def _require_decimal(value: object, *, field_name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal) or not value.is_finite():
        raise IntervalForwardContractError(
            f"{field_name} must be a finite Decimal"
        )
    _canonical_decimal(value)
    return value


def _canonical_decimal(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise IntervalForwardContractError("decimal value must be finite")
    decimal_tuple = value.as_tuple()
    if len(decimal_tuple.digits) > MAX_DECIMAL_DIGITS:
        raise IntervalForwardContractError("decimal value exceeds the digit limit")
    if value == _ZERO:
        return "0"
    if abs(value.adjusted()) > MAX_DECIMAL_ADJUSTED_EXPONENT:
        raise IntervalForwardContractError("decimal value exceeds the exponent limit")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def canonical_decimal_text(value: Decimal) -> str:
    """Render a bounded Decimal without consulting the ambient context."""
    return _canonical_decimal(value)


def _require_bounded_decimal(
    value: object,
    *,
    field_name: str,
    minimum: Decimal | None = None,
    maximum: Decimal,
) -> Decimal:
    result = _require_decimal(value, field_name=field_name)
    if minimum is not None and result < minimum:
        raise IntervalForwardContractError(
            f"{field_name} must be at least {_canonical_decimal(minimum)}"
        )
    if result > maximum:
        raise IntervalForwardContractError(
            f"{field_name} exceeds the frozen maximum"
        )
    return result


def _require_non_negative_decimal(value: object, *, field_name: str) -> Decimal:
    result = _require_decimal(value, field_name=field_name)
    if result < _ZERO:
        raise IntervalForwardContractError(f"{field_name} must be non-negative")
    return result


def _require_utc(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise IntervalForwardContractError(
            f"{field_name} must be timezone-aware UTC"
        )
    if value.utcoffset() != timedelta(0):
        raise IntervalForwardContractError(f"{field_name} must use UTC")
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return _require_utc(value, field_name="timestamp").isoformat().replace(
        "+00:00",
        "Z",
    )


def _require_sha256(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise IntervalForwardContractError(
            f"{field_name} must be lowercase SHA-256 hex"
        )
    return value


def _require_market(value: object) -> Literal["US", "HK"]:
    if value == "US":
        return "US"
    if value == "HK":
        return "HK"
    raise IntervalForwardContractError("market must be US or HK")


def _require_symbol(value: object, *, field_name: str = "symbol") -> str:
    if not isinstance(value, str) or _SYMBOL_RE.fullmatch(value) is None:
        raise IntervalForwardContractError(f"{field_name} is invalid")
    return value


def validate_symbol(value: object) -> str:
    """Validate and return one canonical US/HK security symbol."""
    return _require_symbol(value)


def _require_positive_int(value: object, *, field_name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > MAX_CANONICAL_INTEGER_ABS
    ):
        raise IntervalForwardContractError(f"{field_name} must be a positive integer")
    return value


def _require_non_negative_int(value: object, *, field_name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_CANONICAL_INTEGER_ABS
    ):
        raise IntervalForwardContractError(
            f"{field_name} must be a non-negative integer"
        )
    return value


@dataclass
class _CanonicalJsonBudget:
    nodes: int = 0
    utf8_bytes: int = 0

    def consume(self) -> None:
        self.nodes += 1
        if self.nodes > MAX_CANONICAL_JSON_NODES:
            raise IntervalForwardContractError(
                "payload exceeds the canonical JSON node limit"
            )
        self.consume_utf8(1)

    def consume_utf8(self, size: int) -> None:
        self.utf8_bytes += size
        if self.utf8_bytes > MAX_CANONICAL_JSON_BYTES:
            raise IntervalForwardContractError(
                "payload exceeds the canonical JSON byte limit"
            )


def _validated_json(
    value: object,
    *,
    path: str,
    depth: int,
    budget: _CanonicalJsonBudget,
) -> object:
    if depth > MAX_CANONICAL_JSON_DEPTH:
        raise IntervalForwardContractError(
            "payload exceeds the canonical JSON nesting limit"
        )
    budget.consume()
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        encoded_size = len(value.encode("utf-8"))
        if encoded_size > MAX_CANONICAL_STRING_BYTES:
            raise IntervalForwardContractError(
                f"{path} exceeds the canonical string size limit"
            )
        budget.consume_utf8(encoded_size)
        return value
    if isinstance(value, int):
        if abs(value) > MAX_CANONICAL_INTEGER_ABS:
            raise IntervalForwardContractError(
                f"{path} exceeds the canonical integer limit"
            )
        return value
    if isinstance(value, list):
        if len(value) > MAX_CANONICAL_CONTAINER_ITEMS:
            raise IntervalForwardContractError(
                f"{path} exceeds the canonical container item limit"
            )
        normalized_items: list[object] = []
        for index, item in enumerate(value):
            if len(normalized_items) >= MAX_CANONICAL_CONTAINER_ITEMS:
                raise IntervalForwardContractError(
                    f"{path} exceeds the canonical container item limit"
                )
            normalized_items.append(
                _validated_json(
                    item,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                    budget=budget,
                )
            )
        return normalized_items
    if isinstance(value, Mapping):
        if len(value) > MAX_CANONICAL_CONTAINER_ITEMS:
            raise IntervalForwardContractError(
                f"{path} exceeds the canonical container item limit"
            )
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if len(normalized) >= MAX_CANONICAL_CONTAINER_ITEMS:
                raise IntervalForwardContractError(
                    f"{path} exceeds the canonical container item limit"
                )
            if not isinstance(key, str):
                raise IntervalForwardContractError(
                    f"{path} contains a non-string key"
                )
            if key in normalized:
                raise IntervalForwardContractError(
                    f"{path} contains a duplicate key"
                )
            encoded_key_size = len(key.encode("utf-8"))
            if encoded_key_size > MAX_CANONICAL_KEY_BYTES:
                raise IntervalForwardContractError(
                    f"{path} contains an oversized key"
                )
            budget.consume_utf8(encoded_key_size)
            normalized[key] = _validated_json(
                item,
                path=f"{path}.{key}",
                depth=depth + 1,
                budget=budget,
            )
        return normalized
    raise IntervalForwardContractError(
        f"{path} contains unsupported canonical value {type(value).__name__}"
    )


def canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    if not isinstance(value, Mapping):
        raise IntervalForwardContractError("payload root must be a mapping")
    try:
        normalized = _validated_json(
            value,
            path="$",
            depth=0,
            budget=_CanonicalJsonBudget(),
        )
        encoder = json.JSONEncoder(
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        output = bytearray()
        for chunk in encoder.iterencode(normalized):
            encoded = chunk.encode("utf-8")
            if len(output) + len(encoded) > MAX_CANONICAL_JSON_BYTES:
                raise IntervalForwardContractError(
                    "payload exceeds the canonical JSON byte limit"
                )
            output.extend(encoded)
        raw = bytes(output)
    except IntervalForwardContractError:
        raise
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
        raise IntervalForwardContractError(
            "payload is not canonical JSON"
        ) from exc
    if not raw:
        raise IntervalForwardContractError(
            "payload exceeds the canonical JSON byte limit"
        )
    return raw


def canonical_sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def content_sha256(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    if not isinstance(raw, bytes):
        raise IntervalForwardContractError("content must be str or bytes")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class FrozenIntervalBand:
    buy_low: Decimal
    sell_high: Decimal

    def __post_init__(self) -> None:
        buy_low = _require_bounded_decimal(
            self.buy_low,
            field_name="buy_low",
            minimum=Decimal("0.00000001"),
            maximum=MAX_PRICE,
        )
        sell_high = _require_bounded_decimal(
            self.sell_high,
            field_name="sell_high",
            minimum=Decimal("0.00000001"),
            maximum=MAX_PRICE,
        )
        if sell_high <= buy_low:
            raise IntervalForwardContractError(
                "sell_high must be greater than buy_low"
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "buy_low": _canonical_decimal(self.buy_low),
            "sell_high": _canonical_decimal(self.sell_high),
        }

    @property
    def digest_sha256(self) -> str:
        return canonical_sha256({
            "schema_version": "llm-interval-forward-band-v1",
            **self.to_payload(),
        })


@dataclass(frozen=True, kw_only=True)
class FrozenExecutionPolicy:
    symbol: str
    market: Literal["US", "HK"]
    reference_quantity: Decimal
    one_side_fee_rate: Decimal
    fixed_fee_per_order: Decimal
    entry_round_trip_slippage_bps: Decimal
    minimum_profit_amount: Decimal
    minimum_profit_pct: Decimal
    minimum_edge_cost_ratio: Decimal
    max_interval_width_pct: Decimal
    max_bound_deviation_pct: Decimal
    stop_loss_pct: Decimal
    trailing_stop_pct: Decimal
    max_daily_loss_amount: Decimal
    max_drawdown_amount: Decimal
    max_consecutive_losses: int
    max_entries_per_symbol_per_day: int
    max_holding_minutes: int
    opening_warmup_minutes: int
    entry_cutoff_minutes_before_close: int
    flatten_minutes_before_close: int
    trading_session_mode: Literal["RTH_ONLY"] = TRADING_SESSION_MODE
    data_fidelity: Literal["ONE_MINUTE_OHLCV"] = DATA_FIDELITY
    bbo_coverage: Literal["NONE"] = BBO_COVERAGE
    entry_crossing_semantics: Literal[
        "BAR_LOCAL_CROSSING_APPROXIMATION"
    ] = ENTRY_CROSSING_SEMANTICS
    fee_model_fidelity: Literal[
        "CONFIGURED_FEE_RATE_ESTIMATE"
    ] = FEE_MODEL_FIDELITY
    timestamp_semantics: Literal[
        "START_STAMP_PLUS_ONE_MINUTE_OBSERVATION_TIME"
    ] = TIMESTAMP_SEMANTICS
    virtual_quantity: Literal[1] = 1
    entry_crossing_required: Literal[True] = True
    short_entries_allowed: Literal[False] = False
    position_addons_allowed: Literal[False] = False
    llm_order_execution_allowed: Literal[False] = False
    order_submission_allowed: Literal[False] = False
    live_config_mutation_allowed: Literal[False] = False
    automatic_promotion_allowed: Literal[False] = False
    manual_review_required: Literal[True] = True

    def __post_init__(self) -> None:
        _require_symbol(self.symbol)
        expected_market = "HK" if self.symbol.endswith(".HK") else "US"
        _require_market(self.market)
        if self.market != expected_market:
            raise IntervalForwardContractError("market does not match symbol")
        _require_bounded_decimal(
            self.reference_quantity,
            field_name="reference_quantity",
            minimum=Decimal("0.00000001"),
            maximum=MAX_QUANTITY,
        )
        _require_bounded_decimal(
            self.one_side_fee_rate,
            field_name="one_side_fee_rate",
            minimum=_ZERO,
            maximum=_ONE,
        )
        for field_name in (
            "fixed_fee_per_order",
            "minimum_profit_amount",
            "max_daily_loss_amount",
            "max_drawdown_amount",
        ):
            _require_bounded_decimal(
                getattr(self, field_name),
                field_name=field_name,
                minimum=_ZERO,
                maximum=MAX_MONEY_AMOUNT,
            )
        _require_bounded_decimal(
            self.entry_round_trip_slippage_bps,
            field_name="entry_round_trip_slippage_bps",
            minimum=_ZERO,
            maximum=Decimal("100"),
        )
        _require_bounded_decimal(
            self.minimum_edge_cost_ratio,
            field_name="minimum_edge_cost_ratio",
            minimum=_ZERO,
            maximum=MAX_POLICY_RATIO,
        )
        for field_name in (
            "minimum_profit_pct",
            "max_interval_width_pct",
            "max_bound_deviation_pct",
            "stop_loss_pct",
            "trailing_stop_pct",
        ):
            _require_bounded_decimal(
                getattr(self, field_name),
                field_name=field_name,
                minimum=_ZERO,
                maximum=_HUNDRED,
            )
        if self.max_interval_width_pct <= _ZERO:
            raise IntervalForwardContractError(
                "max_interval_width_pct must be positive"
            )
        if self.max_bound_deviation_pct <= _ZERO:
            raise IntervalForwardContractError(
                "max_bound_deviation_pct must be positive"
            )
        consecutive_losses = _require_positive_int(
            self.max_consecutive_losses,
            field_name="max_consecutive_losses",
        )
        if consecutive_losses > 1_000:
            raise IntervalForwardContractError(
                "max_consecutive_losses exceeds the frozen maximum"
            )
        if (
            type(self.max_entries_per_symbol_per_day) is not int
            or self.max_entries_per_symbol_per_day != 1
        ):
            raise IntervalForwardContractError(
                "v1 requires exactly one entry per symbol per day"
            )
        for field_name, maximum in (
            ("max_holding_minutes", 10_080),
            ("opening_warmup_minutes", 390),
            ("entry_cutoff_minutes_before_close", 180),
            ("flatten_minutes_before_close", 180),
        ):
            value = _require_non_negative_int(
                getattr(self, field_name),
                field_name=field_name,
            )
            if value > maximum:
                raise IntervalForwardContractError(
                    f"{field_name} exceeds the frozen maximum"
                )
        if (
            self.entry_cutoff_minutes_before_close > 0
            and self.flatten_minutes_before_close
            > self.entry_cutoff_minutes_before_close
        ):
            raise IntervalForwardContractError(
                "flatten window must not exceed entry cutoff window"
            )
        if self.trading_session_mode != TRADING_SESSION_MODE:
            raise IntervalForwardContractError("v1 requires RTH_ONLY")
        if self.data_fidelity != DATA_FIDELITY:
            raise IntervalForwardContractError("unsupported data fidelity")
        if self.bbo_coverage != BBO_COVERAGE:
            raise IntervalForwardContractError("v1 must remain BBO_NONE")
        if self.entry_crossing_semantics != ENTRY_CROSSING_SEMANTICS:
            raise IntervalForwardContractError("unsupported crossing semantics")
        if self.fee_model_fidelity != FEE_MODEL_FIDELITY:
            raise IntervalForwardContractError("unsupported fee model fidelity")
        if self.timestamp_semantics != TIMESTAMP_SEMANTICS:
            raise IntervalForwardContractError("unsupported timestamp semantics")
        if (
            type(self.virtual_quantity) is not int
            or self.virtual_quantity != 1
            or self.entry_crossing_required is not True
        ):
            raise IntervalForwardContractError(
                "v1 requires virtual quantity one and fresh crossing"
            )
        for field_name in (
            "short_entries_allowed",
            "position_addons_allowed",
            "llm_order_execution_allowed",
            "order_submission_allowed",
            "live_config_mutation_allowed",
            "automatic_promotion_allowed",
        ):
            if getattr(self, field_name) is not False:
                raise IntervalForwardContractError(f"{field_name} must remain false")
        if self.manual_review_required is not True:
            raise IntervalForwardContractError("manual review must remain required")

    @property
    def per_side_slippage_bps(self) -> Decimal:
        with localcontext() as context:
            context.prec = _FROZEN_POLICY_DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            return self.entry_round_trip_slippage_bps / Decimal("2")

    @property
    def minimum_profit_per_share(self) -> Decimal:
        with localcontext() as context:
            context.prec = _FROZEN_POLICY_DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            return self.minimum_profit_amount / self.reference_quantity

    @property
    def fixed_fee_per_share_per_order(self) -> Decimal:
        with localcontext() as context:
            context.prec = _FROZEN_POLICY_DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            return self.fixed_fee_per_order / self.reference_quantity

    @property
    def max_daily_loss_per_share(self) -> Decimal:
        with localcontext() as context:
            context.prec = _FROZEN_POLICY_DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            return self.max_daily_loss_amount / self.reference_quantity

    @property
    def max_drawdown_per_share(self) -> Decimal:
        with localcontext() as context:
            context.prec = _FROZEN_POLICY_DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            return self.max_drawdown_amount / self.reference_quantity

    def to_payload(self) -> dict[str, object]:
        decimal_fields = {
            name: _canonical_decimal(getattr(self, name))
            for name in (
                "reference_quantity",
                "one_side_fee_rate",
                "fixed_fee_per_order",
                "entry_round_trip_slippage_bps",
                "minimum_profit_amount",
                "minimum_profit_pct",
                "minimum_edge_cost_ratio",
                "max_interval_width_pct",
                "max_bound_deviation_pct",
                "stop_loss_pct",
                "trailing_stop_pct",
                "max_daily_loss_amount",
                "max_drawdown_amount",
            )
        }
        return {
            "schema_version": EXECUTION_POLICY_SCHEMA_VERSION,
            "symbol": self.symbol,
            "market": self.market,
            **decimal_fields,
            "max_consecutive_losses": self.max_consecutive_losses,
            "max_entries_per_symbol_per_day": self.max_entries_per_symbol_per_day,
            "max_holding_minutes": self.max_holding_minutes,
            "opening_warmup_minutes": self.opening_warmup_minutes,
            "entry_cutoff_minutes_before_close": (
                self.entry_cutoff_minutes_before_close
            ),
            "flatten_minutes_before_close": self.flatten_minutes_before_close,
            "trading_session_mode": self.trading_session_mode,
            "data_fidelity": self.data_fidelity,
            "bbo_coverage": self.bbo_coverage,
            "entry_crossing_semantics": self.entry_crossing_semantics,
            "fee_model_fidelity": self.fee_model_fidelity,
            "timestamp_semantics": self.timestamp_semantics,
            "virtual_quantity": self.virtual_quantity,
            "entry_crossing_required": self.entry_crossing_required,
            "short_entries_allowed": self.short_entries_allowed,
            "position_addons_allowed": self.position_addons_allowed,
            "llm_order_execution_allowed": self.llm_order_execution_allowed,
            "order_submission_allowed": self.order_submission_allowed,
            "live_config_mutation_allowed": self.live_config_mutation_allowed,
            "automatic_promotion_allowed": self.automatic_promotion_allowed,
            "manual_review_required": self.manual_review_required,
            "normalized_per_share": {
                "per_side_slippage_bps": _canonical_decimal(
                    self.per_side_slippage_bps
                ),
                "minimum_profit": _canonical_decimal(
                    self.minimum_profit_per_share
                ),
                "fixed_fee_per_order": _canonical_decimal(
                    self.fixed_fee_per_share_per_order
                ),
                "max_daily_loss": _canonical_decimal(
                    self.max_daily_loss_per_share
                ),
                "max_drawdown": _canonical_decimal(
                    self.max_drawdown_per_share
                ),
            },
        }

    @property
    def digest_sha256(self) -> str:
        return canonical_sha256(self.to_payload())


@dataclass(frozen=True)
class CounterfactualPolicyDecision:
    allowed: bool
    code: str
    gross_profit: Decimal
    estimated_costs: Decimal
    net_profit: Decimal
    required_profit: Decimal
    edge_cost_ratio: Decimal | None
    interval_width_pct: Decimal
    max_bound_deviation_pct: Decimal

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool:
            raise IntervalForwardContractError("counterfactual allowed flag is invalid")
        if self.code not in {
            "ALLOW_WITHOUT_CONFIDENCE",
            "INTERVAL_TOO_NARROW",
            "INTERVAL_TOO_WIDE",
            "INTERVAL_BOUND_DEVIATION",
        }:
            raise IntervalForwardContractError("counterfactual policy code is invalid")
        for field_name in (
            "gross_profit",
            "estimated_costs",
            "net_profit",
            "required_profit",
            "interval_width_pct",
            "max_bound_deviation_pct",
        ):
            _require_decimal(getattr(self, field_name), field_name=field_name)
        if self.gross_profit <= _ZERO:
            raise IntervalForwardContractError("gross profit must be positive")
        if self.estimated_costs < _ZERO or self.required_profit < _ZERO:
            raise IntervalForwardContractError(
                "counterfactual costs and required profit must be non-negative"
            )
        if self.edge_cost_ratio is not None:
            _require_decimal(self.edge_cost_ratio, field_name="edge_cost_ratio")
        with localcontext() as context:
            context.prec = _FROZEN_POLICY_DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            expected_net = self.gross_profit - self.estimated_costs
            expected_ratio = (
                self.gross_profit / self.estimated_costs
                if self.estimated_costs > _ZERO
                else None
            )
        if self.net_profit != expected_net:
            raise IntervalForwardContractError("counterfactual net profit is inconsistent")
        if self.edge_cost_ratio != expected_ratio:
            raise IntervalForwardContractError("counterfactual ratio is inconsistent")
        if self.interval_width_pct <= _ZERO or self.max_bound_deviation_pct < _ZERO:
            raise IntervalForwardContractError(
                "counterfactual width and deviation are invalid"
            )
        if self.allowed != (self.code == "ALLOW_WITHOUT_CONFIDENCE"):
            raise IntervalForwardContractError(
                "counterfactual disposition and code are inconsistent"
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "gross_profit": _canonical_decimal(self.gross_profit),
            "estimated_costs": _canonical_decimal(self.estimated_costs),
            "net_profit": _canonical_decimal(self.net_profit),
            "required_profit": _canonical_decimal(self.required_profit),
            "edge_cost_ratio": (
                None
                if self.edge_cost_ratio is None
                else _canonical_decimal(self.edge_cost_ratio)
            ),
            "interval_width_pct": _canonical_decimal(self.interval_width_pct),
            "max_bound_deviation_pct": _canonical_decimal(
                self.max_bound_deviation_pct
            ),
        }


def counterfactual_policy_without_confidence(
    *,
    reference_price: Decimal,
    band: FrozenIntervalBand,
    policy: FrozenExecutionPolicy,
) -> CounterfactualPolicyDecision:
    """Re-run every interval gate except the confidence threshold."""
    price = _require_bounded_decimal(
        reference_price,
        field_name="reference_price",
        minimum=Decimal("0.00000001"),
        maximum=MAX_PRICE,
    )
    with localcontext() as context:
        context.prec = _FROZEN_POLICY_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        quantity = policy.reference_quantity
        gross = (band.sell_high - band.buy_low) * quantity
        estimated_fees = (
            (band.buy_low + band.sell_high)
            * quantity
            * policy.one_side_fee_rate
        )
        modeled_slippage = (
            band.buy_low
            * quantity
            * policy.entry_round_trip_slippage_bps
            / _TEN_THOUSAND
        )
        costs = estimated_fees + modeled_slippage
        net = gross - costs
        required = max(
            policy.minimum_profit_amount,
            band.buy_low
            * quantity
            * policy.minimum_profit_pct
            / _HUNDRED,
        )
        ratio = gross / costs if costs > _ZERO else None
        width_pct = (band.sell_high - band.buy_low) / price * _HUNDRED
        deviation_pct = max(
            abs(band.buy_low - price),
            abs(band.sell_high - price),
        ) / price * _HUNDRED

    code = "ALLOW_WITHOUT_CONFIDENCE"
    allowed = True
    if net < required or (
        ratio is not None and ratio < policy.minimum_edge_cost_ratio
    ):
        code = "INTERVAL_TOO_NARROW"
        allowed = False
    elif width_pct > policy.max_interval_width_pct:
        code = "INTERVAL_TOO_WIDE"
        allowed = False
    elif deviation_pct > policy.max_bound_deviation_pct:
        code = "INTERVAL_BOUND_DEVIATION"
        allowed = False
    return CounterfactualPolicyDecision(
        allowed=allowed,
        code=code,
        gross_profit=gross,
        estimated_costs=costs,
        net_profit=net,
        required_profit=required,
        edge_cost_ratio=ratio,
        interval_width_pct=width_pct,
        max_bound_deviation_pct=deviation_pct,
    )


def _validate_calendar_year(day: date) -> None:
    if not COVERAGE_START_YEAR <= day.year <= COVERAGE_END_YEAR:
        raise IntervalForwardContractError(
            "target session is outside frozen holiday-calendar coverage"
        )


def strict_next_full_session_date(market: Literal["US", "HK"], after: datetime) -> date:
    """Return the next exchange date, never the current or a lunch restart."""
    normalized_market = _require_market(market)
    instant = _require_utc(after, field_name="after")
    session = get_session(normalized_market)
    local_day = instant.astimezone(session.timezone).date()
    candidate = local_day + timedelta(days=1)
    for _ in range(14):
        _validate_calendar_year(candidate)
        if candidate.weekday() < 5 and not is_market_closed(
            normalized_market,
            candidate,
        ):
            return candidate
        candidate += timedelta(days=1)
    raise IntervalForwardContractError("next full session could not be resolved")


def full_session_observation_schedule(
    market: Literal["US", "HK"],
    target_session_date: date,
) -> tuple[datetime, ...]:
    """Build the complete start-stamped +1 minute observation schedule."""
    normalized_market = _require_market(market)
    _validate_calendar_year(target_session_date)
    if target_session_date.weekday() >= 5 or is_market_closed(
        normalized_market,
        target_session_date,
    ):
        raise IntervalForwardContractError("target date is not an open session")
    session = get_session(normalized_market)
    local_start = datetime.combine(
        target_session_date,
        session.rth_open,
        tzinfo=session.timezone,
    )
    local_close = datetime.combine(
        target_session_date,
        session.close_time(target_session_date),
        tzinfo=session.timezone,
    )
    starts: list[datetime] = []
    cursor = local_start
    while cursor < local_close:
        cursor_utc = cursor.astimezone(timezone.utc)
        if session.is_rth(cursor_utc):
            starts.append(cursor_utc + timedelta(minutes=1))
        cursor += timedelta(minutes=1)
    if not starts or len(starts) > MAX_EXPECTED_SESSION_OBSERVATIONS:
        raise IntervalForwardContractError("session observation count is invalid")
    return tuple(starts)


def observation_schedule_sha256(timestamps: Sequence[datetime]) -> str:
    if not timestamps:
        raise IntervalForwardContractError("observation schedule must not be empty")
    canonical: list[str] = []
    previous: datetime | None = None
    for timestamp in timestamps:
        if len(canonical) >= MAX_EXPECTED_SESSION_OBSERVATIONS:
            raise IntervalForwardContractError("observation schedule is too large")
        item = _require_utc(timestamp, field_name="observation timestamp")
        if previous is not None and item <= previous:
            raise IntervalForwardContractError(
                "observation schedule must be strictly increasing"
            )
        canonical.append(_iso_utc(item))
        previous = item
    return canonical_sha256({
        "schema_version": OBSERVATION_SCHEDULE_SCHEMA_VERSION,
        "timestamps": canonical,
    })


@dataclass(frozen=True, kw_only=True)
class ProposalObservation:
    interaction_id: int
    origin: Literal["AUTO_CRON"]
    analysis_started_at: datetime
    analysis_completed_at: datetime
    registered_at: datetime
    source_session_date: date
    target_session_date: date
    target_open_at: datetime
    expected_observation_count: int
    observation_schedule_sha256: str
    confidence: Decimal
    minimum_confidence: Decimal
    reject_code: Literal["LOW_CONFIDENCE"]
    reference_price: Decimal
    baseline_band: FrozenIntervalBand
    raw_proposed_band: FrozenIntervalBand
    effective_candidate_band: FrozenIntervalBand
    execution_policy: FrozenExecutionPolicy
    counterfactual_decision: CounterfactualPolicyDecision
    prompt_sha256: str
    raw_response_sha256: str
    parsed_response_sha256: str
    context_sha256: str
    quote_source_sha256: str
    config_sha256: str
    eligibility_snapshot_sha256: str
    evaluator_digest_sha256: str
    is_primary: Literal[True]
    analysis_started_flat: Literal[True]
    registration_flat: Literal[True]
    broker_position_zero: Literal[True]
    tracked_entry_absent: Literal[True]
    pending_order_absent: Literal[True]
    order_submission_allowed: Literal[False] = False
    live_config_mutation_allowed: Literal[False] = False
    automatic_promotion_allowed: Literal[False] = False
    registration_digest_sha256: str = field(default="")

    def __post_init__(self) -> None:
        interaction_id = _require_positive_int(
            self.interaction_id,
            field_name="interaction_id",
        )
        if interaction_id > MAX_INTERACTION_ID:
            raise IntervalForwardContractError(
                "interaction_id exceeds the frozen database range"
            )
        if self.origin != PROPOSAL_ORIGIN:
            raise IntervalForwardContractError("only AUTO_CRON proposals are eligible")
        started = _require_utc(
            self.analysis_started_at,
            field_name="analysis_started_at",
        )
        completed = _require_utc(
            self.analysis_completed_at,
            field_name="analysis_completed_at",
        )
        registered = _require_utc(self.registered_at, field_name="registered_at")
        target_open = _require_utc(self.target_open_at, field_name="target_open_at")
        if not started <= completed <= registered < target_open:
            raise IntervalForwardContractError(
                "proposal timestamps violate forward-only ordering"
            )
        if completed - started > MAX_ANALYSIS_DURATION:
            raise IntervalForwardContractError(
                "proposal analysis exceeds the frozen duration"
            )
        if registered - completed > MAX_REGISTRATION_DELAY:
            raise IntervalForwardContractError(
                "proposal registration exceeds the frozen delay"
            )
        if type(self.source_session_date) is not date or type(
            self.target_session_date
        ) is not date:
            raise IntervalForwardContractError("session dates must be date values")
        session = get_session(self.execution_policy.market)
        started_day = trade_day_for(self.execution_policy.market, started)
        completed_day = trade_day_for(self.execution_policy.market, completed)
        if (
            not session.is_rth(started)
            or not session.is_rth(completed)
            or started_day != completed_day
        ):
            raise IntervalForwardContractError(
                "analysis must start and complete in one open RTH session"
            )
        if started_day != self.source_session_date:
            raise IntervalForwardContractError("source_session_date is inconsistent")
        expected_target = strict_next_full_session_date(
            self.execution_policy.market,
            completed,
        )
        if self.target_session_date != expected_target:
            raise IntervalForwardContractError(
                "target must be the strict next full exchange session"
            )
        schedule = full_session_observation_schedule(
            self.execution_policy.market,
            self.target_session_date,
        )
        if target_open >= schedule[0]:
            raise IntervalForwardContractError(
                "target open must precede the first observation timestamp"
            )
        expected_open = datetime.combine(
            self.target_session_date,
            get_session(self.execution_policy.market).rth_open,
            tzinfo=get_session(self.execution_policy.market).timezone,
        ).astimezone(timezone.utc)
        if target_open != expected_open:
            raise IntervalForwardContractError("target_open_at is inconsistent")
        if self.expected_observation_count != len(schedule):
            raise IntervalForwardContractError(
                "expected observation count is inconsistent"
            )
        if self.observation_schedule_sha256 != observation_schedule_sha256(schedule):
            raise IntervalForwardContractError(
                "observation schedule digest is inconsistent"
            )
        confidence = _require_decimal(self.confidence, field_name="confidence")
        minimum_confidence = _require_decimal(
            self.minimum_confidence,
            field_name="minimum_confidence",
        )
        if not _ZERO <= confidence <= _ONE:
            raise IntervalForwardContractError("confidence must be in [0, 1]")
        if not _ZERO <= minimum_confidence <= _ONE:
            raise IntervalForwardContractError(
                "minimum_confidence must be in [0, 1]"
            )
        if confidence >= minimum_confidence:
            raise IntervalForwardContractError(
                "confidence-only challenger requires confidence below threshold"
            )
        if self.reject_code != REJECT_CODE:
            raise IntervalForwardContractError(
                "only LOW_CONFIDENCE proposals are eligible"
            )
        reference_price = _require_bounded_decimal(
            self.reference_price,
            field_name="reference_price",
            minimum=Decimal("0.00000001"),
            maximum=MAX_PRICE,
        )
        if self.raw_proposed_band != self.effective_candidate_band:
            raise IntervalForwardContractError(
                "FLAT counterfactual must use the raw proposed band unchanged"
            )
        if self.baseline_band == self.effective_candidate_band:
            raise IntervalForwardContractError(
                "candidate band must differ from baseline"
            )
        recomputed = counterfactual_policy_without_confidence(
            reference_price=reference_price,
            band=self.effective_candidate_band,
            policy=self.execution_policy,
        )
        if not recomputed.allowed or recomputed != self.counterfactual_decision:
            raise IntervalForwardContractError(
                "candidate does not pass all non-confidence interval gates"
            )
        for field_name in (
            "prompt_sha256",
            "raw_response_sha256",
            "parsed_response_sha256",
            "context_sha256",
            "quote_source_sha256",
            "config_sha256",
            "eligibility_snapshot_sha256",
            "evaluator_digest_sha256",
        ):
            _require_sha256(getattr(self, field_name), field_name=field_name)
        for field_name in (
            "is_primary",
            "analysis_started_flat",
            "registration_flat",
            "broker_position_zero",
            "tracked_entry_absent",
            "pending_order_absent",
        ):
            if getattr(self, field_name) is not True:
                raise IntervalForwardContractError(f"{field_name} must remain true")
        for field_name in (
            "order_submission_allowed",
            "live_config_mutation_allowed",
            "automatic_promotion_allowed",
        ):
            if getattr(self, field_name) is not False:
                raise IntervalForwardContractError(f"{field_name} must remain false")
        expected_digest = canonical_sha256(self._preimage_payload())
        if not self.registration_digest_sha256:
            object.__setattr__(
                self,
                "registration_digest_sha256",
                expected_digest,
            )
        elif self.registration_digest_sha256 != expected_digest:
            raise IntervalForwardContractError("registration digest mismatch")

    def _preimage_payload(self) -> dict[str, object]:
        return {
            "schema_version": REGISTRATION_SCHEMA_VERSION,
            "selection_rule_version": SELECTION_RULE_VERSION,
            "interaction_id": self.interaction_id,
            "origin": self.origin,
            "analysis_started_at": _iso_utc(self.analysis_started_at),
            "analysis_completed_at": _iso_utc(self.analysis_completed_at),
            "registered_at": _iso_utc(self.registered_at),
            "source_session_date": self.source_session_date.isoformat(),
            "target_session_date": self.target_session_date.isoformat(),
            "target_open_at": _iso_utc(self.target_open_at),
            "expected_observation_count": self.expected_observation_count,
            "observation_schedule_sha256": self.observation_schedule_sha256,
            "confidence": _canonical_decimal(self.confidence),
            "minimum_confidence": _canonical_decimal(self.minimum_confidence),
            "reject_code": self.reject_code,
            "reference_price": _canonical_decimal(self.reference_price),
            "baseline_band": self.baseline_band.to_payload(),
            "raw_proposed_band": self.raw_proposed_band.to_payload(),
            "effective_candidate_band": self.effective_candidate_band.to_payload(),
            "execution_policy": self.execution_policy.to_payload(),
            "execution_policy_sha256": self.execution_policy.digest_sha256,
            "counterfactual_decision": self.counterfactual_decision.to_payload(),
            "source_digests": {
                "prompt_sha256": self.prompt_sha256,
                "raw_response_sha256": self.raw_response_sha256,
                "parsed_response_sha256": self.parsed_response_sha256,
                "context_sha256": self.context_sha256,
                "quote_source_sha256": self.quote_source_sha256,
                "config_sha256": self.config_sha256,
                "eligibility_snapshot_sha256": self.eligibility_snapshot_sha256,
            },
            "evaluator_digest_sha256": self.evaluator_digest_sha256,
            "eligibility_proofs": {
                "is_primary": self.is_primary,
                "analysis_started_flat": self.analysis_started_flat,
                "registration_flat": self.registration_flat,
                "broker_position_zero": self.broker_position_zero,
                "tracked_entry_absent": self.tracked_entry_absent,
                "pending_order_absent": self.pending_order_absent,
            },
            "p0_safety": {
                "order_submission_allowed": self.order_submission_allowed,
                "live_config_mutation_allowed": self.live_config_mutation_allowed,
                "automatic_promotion_allowed": self.automatic_promotion_allowed,
            },
        }

    def to_payload(self) -> dict[str, object]:
        return {
            **self._preimage_payload(),
            "registration_digest_sha256": self.registration_digest_sha256,
        }


def freeze_proposal_observation(
    *,
    interaction_id: int,
    analysis_started_at: datetime,
    analysis_completed_at: datetime,
    registered_at: datetime,
    confidence: Decimal,
    minimum_confidence: Decimal,
    reference_price: Decimal,
    baseline_band: FrozenIntervalBand,
    raw_proposed_band: FrozenIntervalBand,
    execution_policy: FrozenExecutionPolicy,
    prompt_sha256: str,
    raw_response_sha256: str,
    parsed_response_sha256: str,
    context_sha256: str,
    quote_source_sha256: str,
    config_sha256: str,
    eligibility_snapshot_sha256: str,
    evaluator_digest_sha256: str,
    is_primary: Literal[True],
    analysis_started_flat: Literal[True],
    registration_flat: Literal[True],
    broker_position_zero: Literal[True],
    tracked_entry_absent: Literal[True],
    pending_order_absent: Literal[True],
) -> ProposalObservation:
    completed = _require_utc(
        analysis_completed_at,
        field_name="analysis_completed_at",
    )
    source_day = trade_day_for(execution_policy.market, analysis_started_at)
    target_day = strict_next_full_session_date(execution_policy.market, completed)
    schedule = full_session_observation_schedule(execution_policy.market, target_day)
    session = get_session(execution_policy.market)
    target_open = datetime.combine(
        target_day,
        session.rth_open,
        tzinfo=session.timezone,
    ).astimezone(timezone.utc)
    decision = counterfactual_policy_without_confidence(
        reference_price=reference_price,
        band=raw_proposed_band,
        policy=execution_policy,
    )
    return ProposalObservation(
        interaction_id=interaction_id,
        origin=PROPOSAL_ORIGIN,
        analysis_started_at=analysis_started_at,
        analysis_completed_at=analysis_completed_at,
        registered_at=registered_at,
        source_session_date=source_day,
        target_session_date=target_day,
        target_open_at=target_open,
        expected_observation_count=len(schedule),
        observation_schedule_sha256=observation_schedule_sha256(schedule),
        confidence=confidence,
        minimum_confidence=minimum_confidence,
        reject_code=REJECT_CODE,
        reference_price=reference_price,
        baseline_band=baseline_band,
        raw_proposed_band=raw_proposed_band,
        effective_candidate_band=raw_proposed_band,
        execution_policy=execution_policy,
        counterfactual_decision=decision,
        prompt_sha256=prompt_sha256,
        raw_response_sha256=raw_response_sha256,
        parsed_response_sha256=parsed_response_sha256,
        context_sha256=context_sha256,
        quote_source_sha256=quote_source_sha256,
        config_sha256=config_sha256,
        eligibility_snapshot_sha256=eligibility_snapshot_sha256,
        evaluator_digest_sha256=evaluator_digest_sha256,
        is_primary=is_primary,
        analysis_started_flat=analysis_started_flat,
        registration_flat=registration_flat,
        broker_position_zero=broker_position_zero,
        tracked_entry_absent=tracked_entry_absent,
        pending_order_absent=pending_order_absent,
    )


def select_first_session_proposal(
    proposals: Sequence[ProposalObservation],
    *,
    symbol: str,
    target_session_date: date,
) -> ProposalObservation | None:
    """Select the pre-registered first observation without performance inputs."""
    _require_symbol(symbol)
    if type(target_session_date) is not date:
        raise IntervalForwardContractError("target_session_date must be a date")
    eligible = [
        proposal
        for proposal in proposals
        if proposal.execution_policy.symbol == symbol
        and proposal.target_session_date == target_session_date
    ]
    if not eligible:
        return None
    interaction_ids: dict[int, str] = {}
    for proposal in eligible:
        previous = interaction_ids.get(proposal.interaction_id)
        if previous is not None and previous != proposal.registration_digest_sha256:
            raise IntervalForwardContractError(
                "one interaction id has conflicting immutable registrations"
            )
        interaction_ids[proposal.interaction_id] = proposal.registration_digest_sha256
    return min(
        eligible,
        key=lambda item: (
            item.analysis_completed_at,
            item.interaction_id,
            item.registration_digest_sha256,
        ),
    )


@dataclass(frozen=True, kw_only=True)
class FrozenSessionSlot:
    symbol: str
    target_session_date: date
    selected_interaction_id: int
    selected_registration_digest_sha256: str
    causal_registration_set_sha256: str
    causal_registration_count: int
    occupied_at: datetime
    selection_rule_version: Literal[
        "first-auto-cron-primary-flat-low-confidence-v1"
    ] = SELECTION_RULE_VERSION
    replacement_allowed: Literal[False] = False
    slot_digest_sha256: str = field(default="")

    def __post_init__(self) -> None:
        _require_symbol(self.symbol, field_name="slot symbol")
        if type(self.target_session_date) is not date:
            raise IntervalForwardContractError("slot target date must be a date")
        _require_positive_int(
            self.selected_interaction_id,
            field_name="selected_interaction_id",
        )
        _require_sha256(
            self.selected_registration_digest_sha256,
            field_name="selected_registration_digest_sha256",
        )
        _require_sha256(
            self.causal_registration_set_sha256,
            field_name="causal_registration_set_sha256",
        )
        count = _require_positive_int(
            self.causal_registration_count,
            field_name="causal_registration_count",
        )
        if count > MAX_CANONICAL_CONTAINER_ITEMS:
            raise IntervalForwardContractError(
                "causal registration count exceeds the frozen maximum"
            )
        occupied = _require_utc(self.occupied_at, field_name="occupied_at")
        market: Literal["US", "HK"] = (
            "HK" if self.symbol.endswith(".HK") else "US"
        )
        schedule = full_session_observation_schedule(
            market,
            self.target_session_date,
        )
        target_open = schedule[0] - timedelta(minutes=1)
        if occupied >= target_open:
            raise IntervalForwardContractError(
                "session slot must be occupied before the target open"
            )
        if self.selection_rule_version != SELECTION_RULE_VERSION:
            raise IntervalForwardContractError("slot selection rule drift")
        if self.replacement_allowed is not False:
            raise IntervalForwardContractError("slot replacement must remain disabled")
        expected = canonical_sha256(self._preimage_payload())
        if not self.slot_digest_sha256:
            object.__setattr__(self, "slot_digest_sha256", expected)
        elif self.slot_digest_sha256 != expected:
            raise IntervalForwardContractError("session slot digest mismatch")

    def _preimage_payload(self) -> dict[str, object]:
        return {
            "schema_version": "llm-interval-forward-session-slot-v1",
            "symbol": self.symbol,
            "target_session_date": self.target_session_date.isoformat(),
            "selected_interaction_id": self.selected_interaction_id,
            "selected_registration_digest_sha256": (
                self.selected_registration_digest_sha256
            ),
            "causal_registration_set_sha256": (
                self.causal_registration_set_sha256
            ),
            "causal_registration_count": self.causal_registration_count,
            "occupied_at": _iso_utc(self.occupied_at),
            "selection_rule_version": self.selection_rule_version,
            "replacement_allowed": self.replacement_allowed,
        }

    def to_payload(self) -> dict[str, object]:
        return {
            **self._preimage_payload(),
            "slot_digest_sha256": self.slot_digest_sha256,
        }


def freeze_session_slot(
    proposals: Sequence[ProposalObservation],
    *,
    symbol: str,
    target_session_date: date,
    occupied_at: datetime,
) -> FrozenSessionSlot | None:
    occupied = _require_utc(occupied_at, field_name="occupied_at")
    causal_proposals = [
        proposal
        for proposal in proposals
        if proposal.execution_policy.symbol == symbol
        and proposal.target_session_date == target_session_date
        and proposal.registered_at <= occupied
    ]
    selected = select_first_session_proposal(
        causal_proposals,
        symbol=symbol,
        target_session_date=target_session_date,
    )
    if selected is None:
        return None
    if occupied >= selected.target_open_at:
        raise IntervalForwardContractError(
            "session slot must be occupied before the target open"
        )
    causal_digests = sorted({
        proposal.registration_digest_sha256
        for proposal in causal_proposals
    })
    if not causal_digests:
        raise IntervalForwardContractError("session slot has no causal registrations")
    causal_root = canonical_sha256({
        "schema_version": "llm-interval-forward-causal-registration-set-v1",
        "symbol": symbol,
        "target_session_date": target_session_date.isoformat(),
        "selection_cutoff_at": _iso_utc(occupied),
        "registration_digests": causal_digests,
    })
    return FrozenSessionSlot(
        symbol=symbol,
        target_session_date=target_session_date,
        selected_interaction_id=selected.interaction_id,
        selected_registration_digest_sha256=(
            selected.registration_digest_sha256
        ),
        causal_registration_set_sha256=causal_root,
        causal_registration_count=len(causal_digests),
        occupied_at=occupied,
    )


def resolve_session_slot(
    slot: FrozenSessionSlot,
    proposals: Sequence[ProposalObservation],
) -> ProposalObservation | None:
    """Resolve only the frozen winner; deletion never promotes a runner-up."""
    for proposal in proposals:
        if (
            proposal.interaction_id == slot.selected_interaction_id
            and proposal.registration_digest_sha256
            == slot.selected_registration_digest_sha256
            and proposal.execution_policy.symbol == slot.symbol
            and proposal.target_session_date == slot.target_session_date
        ):
            return proposal
    return None


__all__ = [
    "BBO_COVERAGE",
    "DATA_FIDELITY",
    "ENTRY_CROSSING_SEMANTICS",
    "EXECUTION_POLICY_SCHEMA_VERSION",
    "FEE_MODEL_FIDELITY",
    "FrozenExecutionPolicy",
    "FrozenIntervalBand",
    "FrozenSessionSlot",
    "IntervalForwardContractError",
    "ProposalObservation",
    "CounterfactualPolicyDecision",
    "REGISTRATION_SCHEMA_VERSION",
    "SELECTION_RULE_VERSION",
    "TIMESTAMP_SEMANTICS",
    "canonical_json_bytes",
    "canonical_decimal_text",
    "canonical_sha256",
    "content_sha256",
    "counterfactual_policy_without_confidence",
    "freeze_proposal_observation",
    "freeze_session_slot",
    "full_session_observation_schedule",
    "observation_schedule_sha256",
    "resolve_session_slot",
    "select_first_session_proposal",
    "strict_next_full_session_date",
    "validate_symbol",
]
