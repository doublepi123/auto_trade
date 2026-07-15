from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import LargeBinary, and_, cast, exists, func, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ExperimentResult, LLMInteraction
from app.schemas import LLMInteractionDetail


_CONTEXT_STORAGE_SCHEMA_VERSION = 1
_MIN_CONTEXT_MAX_BYTES = 2048
_RECENT_PRICE_POINTS = 24
_PRICE_POINT_KEYS = ("last_price", "bid", "ask", "timestamp", "observed_at")
_RECENT_ANALYSIS_KEYS = (
    "last_analysis_at",
    "buy_low",
    "sell_high",
    "confidence_score",
    "analysis",
    "applied_buy_low",
    "applied_sell_high",
    "reject_reason",
)
_ACCOUNT_CONTEXT_KEYS = (
    "cash_currency",
    "available_cash",
    "buying_power",
    "max_buy_quantity",
    "max_short_quantity",
    "pending_order",
    "errors",
)
_CORE_CONTEXT_KEYS = (
    "symbol",
    "market",
    "current_price",
    "current_buy_low",
    "current_sell_high",
    "short_selling",
    "current_position",
    "position_quantity",
    "position_avg_price",
    "unrealized_pnl_pct",
    "min_profit_amount",
)


@dataclass(frozen=True)
class LLMInteractionPruneResult:
    deleted: int = 0
    batches: int = 0


@dataclass(frozen=True)
class LLMContextCompactionResult:
    inspected: int = 0
    compacted: int = 0
    batches: int = 0


def _json_default(obj: Any) -> Any:
    """JSON serializer for types ``json.dumps`` cannot encode natively.

    Order matters: more specific types (Decimal, datetime) are handled
    first so we never fall through to ``str()``, which would render e.g.
    ``Decimal("123.45")`` as ``'Decimal("123.45")'`` — surprising and
    impossible to round-trip. Unknown objects are coerced via ``str()`` as
    a last resort; callers that need stricter behaviour should pre-serialize
    their data.
    """
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            return str(obj)
    return str(obj)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def _json_loads_dict(value: str | None) -> dict[str, Any]:
    """Parse a JSON text column to a dict; never raises (returns {} on failure)."""
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError, RecursionError):
        return {}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def _truncate_text(value: Any, limit: int) -> Any:
    if not isinstance(value, str) or _utf8_size(value) <= limit:
        return value
    suffix = b"..."
    if limit <= len(suffix):
        return suffix[: max(0, limit)].decode("ascii")
    prefix = value.encode("utf-8")[: limit - len(suffix)].decode(
        "utf-8",
        errors="ignore",
    )
    return prefix + suffix.decode("ascii")


def _bounded_scalar(value: Any, limit: int) -> tuple[bool, Any]:
    if value is None or isinstance(value, (bool, int, float)):
        return True, value
    if isinstance(value, str):
        return True, _truncate_text(value, limit)
    return False, None


def _validate_context_limits(max_bytes: int, recent_price_points: int) -> None:
    if max_bytes < _MIN_CONTEXT_MAX_BYTES:
        raise ValueError(
            f"max_bytes must be at least {_MIN_CONTEXT_MAX_BYTES}"
        )
    if recent_price_points <= 0:
        raise ValueError("recent_price_points must be positive")


def _sample_evenly(values: list[Any], limit: int) -> list[Any]:
    if len(values) <= limit:
        return list(values)
    if limit <= 1:
        return [values[-1]]
    last = len(values) - 1
    indexes = [round(index * last / (limit - 1)) for index in range(limit)]
    return [values[index] for index in indexes]


def _compact_price_points(values: list[Any], limit: int) -> list[Any]:
    sanitized: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        point: dict[str, Any] = {}
        for key in _PRICE_POINT_KEYS:
            if key not in item:
                continue
            keep, bounded = _bounded_scalar(item[key], 256)
            if keep:
                point[key] = bounded
        # observed_at is the local ingestion timestamp and is sufficient for
        # replay. Avoid retaining the broker timestamp twice when they match.
        if point.get("timestamp") == point.get("observed_at"):
            point.pop("timestamp", None)
        if point:
            sanitized.append(point)
    return _sample_evenly(sanitized, limit)


def _serialize_final_context(
    context: dict[str, Any],
    storage_metadata: dict[str, Any],
    *,
    max_bytes: int,
) -> str:
    candidate = dict(context)
    candidate["_storage"] = storage_metadata
    candidate_text = _json_dumps(candidate)
    if _utf8_size(candidate_text) <= max_bytes:
        return candidate_text

    truncated_fields = set(storage_metadata.get("truncated_fields", []))
    truncated_fields.add("hard_limit")
    storage_metadata["truncated_fields"] = sorted(truncated_fields)
    candidate.pop("recent_prices", None)
    recent_price_metadata = storage_metadata.get("recent_price_points")
    if isinstance(recent_price_metadata, dict):
        recent_price_metadata["stored"] = 0
    candidate["_storage"] = storage_metadata
    candidate_text = _json_dumps(candidate)
    if _utf8_size(candidate_text) <= max_bytes:
        return candidate_text

    for key in reversed(_CORE_CONTEXT_KEYS):
        candidate.pop(key, None)
        candidate["_storage"] = storage_metadata
        candidate_text = _json_dumps(candidate)
        if _utf8_size(candidate_text) <= max_bytes:
            return candidate_text

    metadata_only = _json_dumps({"_storage": storage_metadata})
    if _utf8_size(metadata_only) <= max_bytes:
        return metadata_only
    raise RuntimeError("context storage metadata exceeds configured hard limit")


def _invalid_context_snapshot(value: str, *, max_bytes: int) -> str:
    storage_metadata: dict[str, Any] = {
        "schema_version": _CONTEXT_STORAGE_SCHEMA_VERSION,
        "compacted": True,
        "original_bytes": _utf8_size(value),
        "recent_price_points": {"original": 0, "stored": 0},
        "truncated_fields": ["invalid_json"],
        "parse_error": "invalid_json",
    }
    return _serialize_final_context({}, storage_metadata, max_bytes=max_bytes)


def _compact_context_snapshot(
    context_snapshot: dict[str, Any],
    *,
    max_bytes: int,
    recent_price_points: int,
) -> str:
    """Serialize an audit-useful snapshot without retaining the full quote deque.

    ``recent_prices`` is already represented in the prompt and historically
    contained hundreds of nearly identical quote objects. A uniform sample
    preserves the first/last points used by recommendation evaluation and the
    trajectory in between, while removing the repeated per-point symbol.
    """
    _validate_context_limits(max_bytes, recent_price_points)
    original_text = _json_dumps(context_snapshot)
    original_prices = context_snapshot.get("recent_prices")
    original_price_count = len(original_prices) if isinstance(original_prices, list) else 0
    if _utf8_size(original_text) <= max_bytes and original_price_count <= recent_price_points:
        return original_text

    # Normalize custom objects before applying the bounded audit schema.
    normalized = _json_loads_dict(original_text)
    compacted = dict(normalized)
    truncated_fields: list[str] = []

    prices = normalized.get("recent_prices")
    if isinstance(prices, list):
        compacted["recent_prices"] = _compact_price_points(prices, recent_price_points)
        if len(prices) > recent_price_points:
            truncated_fields.append("recent_prices.sampled")
        if any(isinstance(item, dict) and "symbol" in item for item in prices):
            truncated_fields.append("recent_prices.symbol_deduplicated")

    recent_analysis = normalized.get("recent_analysis")
    if isinstance(recent_analysis, dict):
        bounded_analysis = {
            key: _truncate_text(recent_analysis[key], 2048)
            for key in _RECENT_ANALYSIS_KEYS
            if key in recent_analysis
        }
        if bounded_analysis != recent_analysis:
            truncated_fields.append("recent_analysis.bounded")
        compacted["recent_analysis"] = bounded_analysis

    account_context = normalized.get("account_context")
    if isinstance(account_context, dict):
        bounded_account = {
            key: account_context[key]
            for key in _ACCOUNT_CONTEXT_KEYS
            if key in account_context
        }
        errors = bounded_account.get("errors")
        if isinstance(errors, list):
            bounded_account["errors"] = [
                _truncate_text(error, 512) for error in errors[:8]
            ]
        if bounded_account != account_context:
            truncated_fields.append("account_context.bounded")
        compacted["account_context"] = bounded_account

    storage_metadata: dict[str, Any] = {
        "schema_version": _CONTEXT_STORAGE_SCHEMA_VERSION,
        "compacted": True,
        "original_bytes": _utf8_size(original_text),
        "recent_price_points": {
            "original": original_price_count,
            "stored": len(compacted.get("recent_prices", []))
            if isinstance(compacted.get("recent_prices"), list)
            else 0,
        },
        "truncated_fields": truncated_fields,
    }
    compacted["_storage"] = storage_metadata
    compacted_text = _json_dumps(compacted)
    if _utf8_size(compacted_text) <= max_bytes:
        return compacted_text

    # Unknown future fields may be large. Fall back to the stable context used
    # by audit/replay and retain a smaller trajectory sample.
    fallback = {
        key: _truncate_text(normalized[key], 512)
        for key in _CORE_CONTEXT_KEYS
        if key in normalized
    }
    if isinstance(prices, list):
        fallback["recent_prices"] = _compact_price_points(prices, min(8, recent_price_points))
        storage_metadata["recent_price_points"]["stored"] = len(fallback["recent_prices"])
    if isinstance(recent_analysis, dict):
        fallback["recent_analysis"] = {
            key: _truncate_text(recent_analysis[key], 512)
            for key in _RECENT_ANALYSIS_KEYS
            if key in recent_analysis
        }
    if isinstance(account_context, dict):
        fallback["account_context"] = {
            key: account_context[key]
            for key in _ACCOUNT_CONTEXT_KEYS
            if key in account_context and key != "errors"
        }
        errors = account_context.get("errors")
        if isinstance(errors, list):
            fallback["account_context"]["errors"] = [
                _truncate_text(error, 256) for error in errors[:4]
            ]
    storage_metadata["truncated_fields"] = sorted(
        {*truncated_fields, "unknown_fields.omitted"}
    )
    fallback["_storage"] = storage_metadata
    fallback_text = _json_dumps(fallback)
    if _utf8_size(fallback_text) <= max_bytes:
        return fallback_text

    final: dict[str, Any] = {}
    for key in _CORE_CONTEXT_KEYS:
        if key not in normalized:
            continue
        keep, bounded = _bounded_scalar(normalized[key], 128)
        if keep:
            final[key] = bounded
    if isinstance(prices, list) and prices:
        final_points = _compact_price_points(prices, 2)
        final["recent_prices"] = [
            {
                key: point[key]
                for key in ("last_price", "bid", "ask")
                if key in point
            }
            for point in final_points
        ]
        storage_metadata["recent_price_points"]["stored"] = len(final["recent_prices"])
    return _serialize_final_context(
        final,
        storage_metadata,
        max_bytes=max_bytes,
    )


def build_order_policy_outcome(
    analysis_result: dict[str, Any],
    order_result: dict[str, Any],
) -> dict[str, Any]:
    """Build the stable audit shape for an LLM order-policy decision."""
    action = str(analysis_result.get("order_action") or "NONE").upper()
    status = str(order_result.get("status") or "NO_ACTION").upper()

    raw_disposition = order_result.get("policy_disposition") or order_result.get("disposition")
    normalized_disposition = str(raw_disposition or "").upper()
    if normalized_disposition not in {"ALLOW", "REJECT", "SHADOW"}:
        if status == "SHADOW_ONLY":
            normalized_disposition = "SHADOW"
        elif action == "NONE" or status in {
            "POLICY_REJECTED",
            "CONFIDENCE_REJECTED",
            "RUNNER_STOPPED",
            "WATCHLIST_READ_ONLY",
            "UNKNOWN_SYMBOL",
            "ERROR",
        }:
            normalized_disposition = "REJECT"
        else:
            normalized_disposition = "ALLOW"

    raw_code = order_result.get("policy_code")
    if raw_code:
        code = str(raw_code)
    elif action == "NONE":
        code = "NO_ACTION"
    elif normalized_disposition == "SHADOW":
        code = "SHADOW_MODE"
    elif normalized_disposition == "REJECT":
        code = status or "POLICY_REJECTED"
    else:
        code = "ALLOW"

    candidate_source = order_result.get("candidate_price")
    if candidate_source is None:
        candidate_source = (
            analysis_result.get("replacement_price") or analysis_result.get("order_price")
            if action == "CANCEL_REPLACE"
            else analysis_result.get("order_price")
        )

    return {
        "code": code,
        "reference_price": _finite_float_or_none(order_result.get("reference_price")),
        "candidate_price": _finite_float_or_none(candidate_source),
        "deviation_pct": _finite_float_or_none(order_result.get("deviation_pct")),
        "confidence": _finite_float_or_none(
            order_result.get("confidence")
            if order_result.get("confidence") is not None
            else analysis_result.get("confidence_score")
        ),
        "disposition": normalized_disposition,
    }


def _finite_float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return None
    return normalized if math.isfinite(normalized) else None


class LLMInteractionService:
    def __init__(
        self,
        db: Session,
        *,
        context_max_bytes: int | None = None,
        recent_price_points: int = _RECENT_PRICE_POINTS,
    ) -> None:
        self.db = db
        self.context_max_bytes = (
            settings.llm_context_snapshot_max_bytes
            if context_max_bytes is None
            else context_max_bytes
        )
        self.recent_price_points = recent_price_points
        _validate_context_limits(
            self.context_max_bytes,
            self.recent_price_points,
        )

    def create(
        self,
        *,
        interaction_type: str,
        symbol: str,
        market: str,
        prompt: str,
        raw_response: str = "",
        parsed_response: dict[str, Any] | None = None,
        context_snapshot: dict[str, Any] | None = None,
        success: bool,
        error: str = "",
        order_action: str = "NONE",
        prompt_variant: str | None = None,
    ) -> LLMInteraction:
        record = LLMInteraction(
            interaction_type=interaction_type,
            symbol=symbol,
            market=market,
            prompt=prompt,
            raw_response=raw_response,
            parsed_response=_json_dumps(parsed_response or {}),
            context_snapshot=_compact_context_snapshot(
                context_snapshot or {},
                max_bytes=self.context_max_bytes,
                recent_price_points=self.recent_price_points,
            ),
            success=success,
            error=error,
            order_action=order_action or "NONE",
            prompt_variant=prompt_variant,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def prune_expired(
        self,
        *,
        retention_days: int,
        no_action_retention_days: int,
        batch_size: int,
        max_batches: int | None = 8,
        now: datetime | None = None,
    ) -> LLMInteractionPruneResult:
        """Delete expired interactions in short transactions.

        Successful, unapplied ``NONE`` decisions with no order linkage are
        routine observations and use the shorter retention window. Prompt
        variants, failures, actionable decisions, applied suggestions and
        order-linked rows keep the full retention window. Interactions linked
        from experiment results are never deleted automatically.
        """
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if max_batches is not None and max_batches <= 0:
            return LLMInteractionPruneResult()

        reference = now or datetime.now(timezone.utc)
        expiration_clauses: list[Any] = []
        if retention_days > 0:
            expiration_clauses.append(
                LLMInteraction.created_at < reference - timedelta(days=retention_days)
            )
        if no_action_retention_days > 0:
            expiration_clauses.append(
                and_(
                    LLMInteraction.created_at
                    < reference - timedelta(days=no_action_retention_days),
                    LLMInteraction.success.is_(True),
                    LLMInteraction.order_action == "NONE",
                    LLMInteraction.applied.is_(False),
                    or_(LLMInteraction.order_id.is_(None), LLMInteraction.order_id == ""),
                    or_(
                        LLMInteraction.order_status.is_(None),
                        LLMInteraction.order_status == "",
                        LLMInteraction.order_status == "NO_ACTION",
                    ),
                    or_(
                        LLMInteraction.prompt_variant.is_(None),
                        LLMInteraction.prompt_variant == "",
                    ),
                )
            )
        if not expiration_clauses:
            return LLMInteractionPruneResult()

        experiment_reference = exists().where(
            ExperimentResult.interaction_id == LLMInteraction.id
        )
        expired = and_(
            or_(*expiration_clauses),
            ~experiment_reference,
        )
        deleted = 0
        batches = 0
        while max_batches is None or batches < max_batches:
            ids = [
                row[0]
                for row in (
                    self.db.query(LLMInteraction.id)
                    .filter(expired)
                    .order_by(LLMInteraction.created_at.asc(), LLMInteraction.id.asc())
                    .limit(batch_size)
                    .all()
                )
            ]
            if not ids:
                break
            try:
                deleted += int(
                    self.db.query(LLMInteraction)
                    .filter(
                        LLMInteraction.id.in_(ids),
                        expired,
                    )
                    .delete(synchronize_session=False)
                )
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            batches += 1
        return LLMInteractionPruneResult(deleted=deleted, batches=batches)

    def compact_oversized_contexts(
        self,
        *,
        max_bytes: int,
        recent_price_points: int = _RECENT_PRICE_POINTS,
        batch_size: int = 25,
        max_rows: int | None = None,
    ) -> LLMContextCompactionResult:
        """Rewrite legacy oversized snapshots in bounded transactions."""
        _validate_context_limits(max_bytes, recent_price_points)
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if max_rows is not None and max_rows <= 0:
            return LLMContextCompactionResult()

        inspected = 0
        compacted_count = 0
        batches = 0
        last_id = 0
        while max_rows is None or inspected < max_rows:
            limit = batch_size
            if max_rows is not None:
                limit = min(limit, max_rows - inspected)
            rows = (
                self.db.query(LLMInteraction)
                .filter(
                    LLMInteraction.id > last_id,
                    func.length(
                        cast(LLMInteraction.context_snapshot, LargeBinary)
                    ) > max_bytes,
                )
                .order_by(LLMInteraction.id.asc())
                .limit(limit)
                .all()
            )
            if not rows:
                break
            inspected += len(rows)
            last_id = rows[-1].id
            for record in rows:
                original_text = record.context_snapshot or ""
                try:
                    parsed = json.loads(original_text)
                except (ValueError, TypeError, RecursionError):
                    compacted_text = _invalid_context_snapshot(
                        original_text,
                        max_bytes=max_bytes,
                    )
                else:
                    context = parsed if isinstance(parsed, dict) else {"value": parsed}
                    compacted_text = _compact_context_snapshot(
                        context,
                        max_bytes=max_bytes,
                        recent_price_points=recent_price_points,
                    )
                if _utf8_size(compacted_text) > max_bytes:
                    raise RuntimeError(
                        f"compacted context for interaction {record.id} exceeds "
                        f"{max_bytes} bytes"
                    )
                if compacted_text == original_text:
                    raise RuntimeError(
                        f"oversized context for interaction {record.id} was not rewritten"
                    )
                record.context_snapshot = compacted_text
                compacted_count += 1
            try:
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            batches += 1
        return LLMContextCompactionResult(
            inspected=inspected,
            compacted=compacted_count,
            batches=batches,
        )

    def update_outcome(
        self,
        interaction_id: int | None,
        *,
        applied: bool | None = None,
        order_status: str | None = None,
        order_id: str | None = None,
        policy_outcome: dict[str, Any] | None = None,
    ) -> None:
        if interaction_id is None:
            return
        record = self.db.get(LLMInteraction, interaction_id)
        if record is None:
            return
        if applied is not None:
            record.applied = applied
        if order_status is not None:
            record.order_status = order_status
        if order_id is not None:
            record.order_id = order_id
        if policy_outcome is not None:
            parsed_response = _json_loads_dict(record.parsed_response)
            parsed_response["policy_outcome"] = dict(policy_outcome)
            record.parsed_response = _json_dumps(parsed_response)
        self.db.commit()

    def list_recent(self, limit: int = 50) -> list[LLMInteraction]:
        return (
            self.db.query(LLMInteraction)
            .order_by(LLMInteraction.created_at.desc(), LLMInteraction.id.desc())
            .limit(limit)
            .all()
        )

    def get_detail(self, interaction_id: int) -> LLMInteractionDetail | None:
        record = self.db.get(LLMInteraction, interaction_id)
        if record is None:
            return None
        return LLMInteractionDetail(
            id=record.id,
            interaction_type=record.interaction_type,
            symbol=record.symbol,
            market=record.market,
            prompt=record.prompt,
            raw_response=record.raw_response,
            parsed_response=_json_loads_dict(record.parsed_response),
            context_snapshot=_json_loads_dict(record.context_snapshot),
            success=bool(record.success),
            error=record.error,
            order_action=record.order_action,
            order_status=record.order_status,
            order_id=record.order_id,
            applied=bool(record.applied),
            prompt_variant=record.prompt_variant,
            created_at=record.created_at,
        )
