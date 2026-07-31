from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal, cast


FROZEN_DISPROOF_INPUT_SCHEMA_VERSION = (
    "strategy-v2-frozen-disproof-input-v1"
)
FROZEN_DISPROOF_OUTPUT_SCHEMA_VERSION = (
    "strategy-v2-frozen-disproof-report-v1"
)
FROZEN_DISPROOF_ALGORITHM_VERSION = (
    "strategy-v2-frozen-forward-disproof-queue-v1"
)
FORWARD_CANDIDATE_ALGORITHM_VERSION = (
    "strategy-v2-causal-trend-prewarm-v1"
)
CONTROL_SYMBOL = "NVDA.US"
MINIMUM_FUTURE_TRADE_DAYS = 20
MINIMUM_CLOSED_TRADES = 50
FROZEN_QUEUE_AS_OF_DATE = date(2026, 7, 31)
FROZEN_QUEUE_NAME = "nasdaq-djia-disproof-2026-07-31"
FROZEN_EVALUATOR_DIGEST = (
    "e5ae9ea3e68dcc47d5131c21d8ba223824aecabf59da1f4b592df72cb9aa0294"
)
FROZEN_QUEUE_ENTRIES = (
    (
        "AAPL.US",
        "EXPLORATION",
        (
            "Highest usable quant edge-to-cost and positive small-sample "
            "shadow evidence require prospective disproof."
        ),
        "11583728616a7d5f17328d38f024fa10644c29e8b5691106ea961ba75d160a32",
    ),
    (
        "AVGO.US",
        "EXPLORATION",
        (
            "One-session challenger improvement conflicts with near-flat "
            "historical shadow evidence and negative quant edge."
        ),
        "f2e0b1fcfb832c1887a093abda8a146fc03741b2deb47bb29c3132542a4393e2",
    ),
    (
        "GOOGL.US",
        "SELECTED",
        (
            "Latest universe and observation priority rank one has "
            "insufficient forward trade evidence."
        ),
        "5afa45fb95bda68262818c43bbc4f01f62cb81a5e6457d68b5c64dbd11be5bdc",
    ),
    (
        "META.US",
        "EXPLORATION",
        (
            "Small positive shadow evidence conflicts with strongly "
            "negative cost-adjusted quant edge."
        ),
        "cf97ba201b97e8040e0ff113ebb4197268f7709ef6c801979f27eea0a6e9fcd3",
    ),
    (
        "NVDA.US",
        "CONTROL",
        "Deployed symbol is the mandatory same-window control.",
        "9afed570d67d2394f01d40d6706ad7b5eefea5627c7813b4ae762d46a4eeddd9",
    ),
    (
        "TER.US",
        "WATCHLIST",
        (
            "Positive small-sample shadow evidence conflicts with AVOID "
            "quant evidence and negative backfilled rotation performance."
        ),
        "e9f094c87d6342ea6a8663b04584c0aae16455060e0ad3a903a18620c5411435",
    ),
)
FROZEN_QUEUE_ROLES = {
    symbol: role for symbol, role, _, _ in FROZEN_QUEUE_ENTRIES
}
FROZEN_OHLC_SOURCE = "strategy-v2 forward-validation API JSON export"
FROZEN_COST_MODEL = "frozen source evaluator fees and slippage"
FROZEN_EVIDENCE_NOTES = (
    "Only source forward-validation INCLUDED candidate_metrics are consumed.",
    "The evaluator performs no broker or database access.",
)
FROZEN_QUEUE_DIGEST = (
    "d2005f023cc9e1874609008a55c2b0d21d1d30647175ca607e60225e4f7ea69f"
)

FrozenQueueStatus = Literal[
    "INSUFFICIENT_EVIDENCE",
    "READY_FOR_MANUAL_DISPROOF_REVIEW",
]

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SYMBOL_RE = re.compile(r"[A-Z0-9][A-Z0-9.-]{0,31}\.(?:US|HK)")
_ROLES = {"CONTROL", "SELECTED", "EXPLORATION", "WATCHLIST"}
_BBO_COVERAGE = {"NONE", "PARTIAL", "COMPLETE"}
_REPORT_STATUSES = {
    "NOT_REGISTERED",
    "FROZEN",
    "COLLECTING",
    "READY_FOR_REVIEW",
    "MATURE_EVIDENCE",
    "BLOCKED",
}


def _mapping(value: object, *, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise ValueError(f"{field_name} must be an object")
    return cast(Mapping[str, object], value)


def _sequence(value: object, *, field_name: str) -> Sequence[object]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        raise ValueError(f"{field_name} must be an array")
    return cast(Sequence[object], value)


def _required_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _required_sha256(value: object, *, field_name: str) -> str:
    result = _required_string(value, field_name=field_name).lower()
    if _SHA256_RE.fullmatch(result) is None:
        raise ValueError(f"{field_name} must be a SHA-256 hex digest")
    return result


def _required_date(value: object, *, field_name: str) -> date:
    raw = _required_string(value, field_name=field_name)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date") from exc


def _non_negative_integer(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _finite_number(value: object, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite number")
    try:
        result = float(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be a finite number")
    return result


def _required_false(value: object, *, field_name: str) -> None:
    if value is not False:
        raise ValueError(f"{field_name} must remain false")


def _required_true(value: object, *, field_name: str) -> None:
    if value is not True:
        raise ValueError(f"{field_name} must be true")


def _string_list(value: object, *, field_name: str) -> tuple[str, ...]:
    raw = _sequence(value, field_name=field_name)
    result = tuple(
        _required_string(item, field_name=f"{field_name}[]")
        for item in raw
    )
    return result


def _canonical_digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FrozenQueueEntry:
    symbol: str
    role: str
    reason: str
    config_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "role": self.role,
            "reason": self.reason,
            "config_hash": self.config_hash,
        }


@dataclass(frozen=True)
class FrozenEvidenceContext:
    ohlc_source: str
    ohlc_fidelity: Literal["ONE_MINUTE_OHLCV_BAR_CLOSE"]
    bbo_coverage: Literal["NONE", "PARTIAL", "COMPLETE"]
    cost_model: str
    costs_included_in_net_pnl: Literal[True]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "ohlc_source": self.ohlc_source,
            "ohlc_fidelity": self.ohlc_fidelity,
            "bbo_coverage": self.bbo_coverage,
            "cost_model": self.cost_model,
            "costs_included_in_net_pnl": True,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class FrozenQueueSpec:
    name: str
    as_of_date: date
    benchmark_symbol: str
    candidate_algorithm_version: str
    evaluator_digest: str
    entries: tuple[FrozenQueueEntry, ...]
    evidence_context: FrozenEvidenceContext

    def canonical_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "as_of_date": self.as_of_date.isoformat(),
            "benchmark_symbol": self.benchmark_symbol,
            "candidate_algorithm_version": (
                self.candidate_algorithm_version
            ),
            "evaluator_digest": self.evaluator_digest,
            "metric_track": "candidate_metrics",
            "entries": [
                item.to_dict()
                for item in sorted(self.entries, key=lambda row: row.symbol)
            ],
            "evidence_context": self.evidence_context.to_dict(),
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self.canonical_payload())


@dataclass(frozen=True)
class _DailyMetric:
    session_date: date
    bars: int
    closed_trades: int
    gross_pnl: float
    fees: float
    net_pnl: float


@dataclass(frozen=True)
class _SourceEvidence:
    metrics_by_date: Mapping[date, _DailyMetric]
    blockers: tuple[str, ...]
    excluded_targets: int
    pre_freeze_rows_excluded: int


def canonical_frozen_queue_manifest() -> dict[str, object]:
    """Return the repository-owned precommitment as a fresh JSON object."""
    return {
        "name": FROZEN_QUEUE_NAME,
        "as_of_date": FROZEN_QUEUE_AS_OF_DATE.isoformat(),
        "benchmark_symbol": CONTROL_SYMBOL,
        "candidate_algorithm_version": (
            FORWARD_CANDIDATE_ALGORITHM_VERSION
        ),
        "evaluator_digest": FROZEN_EVALUATOR_DIGEST,
        "entries": [
            {
                "symbol": symbol,
                "role": role,
                "reason": reason,
                "config_hash": config_hash,
            }
            for symbol, role, reason, config_hash in FROZEN_QUEUE_ENTRIES
        ],
        "evidence_context": {
            "ohlc_source": FROZEN_OHLC_SOURCE,
            "ohlc_fidelity": "ONE_MINUTE_OHLCV_BAR_CLOSE",
            "bbo_coverage": "NONE",
            "cost_model": FROZEN_COST_MODEL,
            "costs_included_in_net_pnl": True,
            "notes": list(FROZEN_EVIDENCE_NOTES),
        },
    }


def parse_frozen_queue_spec(value: object) -> FrozenQueueSpec:
    payload = _mapping(value, field_name="frozen_queue")
    name = _required_string(payload.get("name"), field_name="name")
    if name != FROZEN_QUEUE_NAME:
        raise ValueError("name does not match the canonical precommitment")
    as_of_date = _required_date(
        payload.get("as_of_date"),
        field_name="as_of_date",
    )
    if as_of_date != FROZEN_QUEUE_AS_OF_DATE:
        raise ValueError(
            "as_of_date must remain frozen at "
            f"{FROZEN_QUEUE_AS_OF_DATE.isoformat()}"
        )
    benchmark_symbol = _required_string(
        payload.get("benchmark_symbol"),
        field_name="benchmark_symbol",
    ).upper()
    if benchmark_symbol != CONTROL_SYMBOL:
        raise ValueError(f"benchmark_symbol must be {CONTROL_SYMBOL}")
    candidate_algorithm_version = _required_string(
        payload.get("candidate_algorithm_version"),
        field_name="candidate_algorithm_version",
    )
    if candidate_algorithm_version != FORWARD_CANDIDATE_ALGORITHM_VERSION:
        raise ValueError("candidate_algorithm_version is unsupported")
    evaluator_digest = _required_sha256(
        payload.get("evaluator_digest"),
        field_name="evaluator_digest",
    )
    if evaluator_digest != FROZEN_EVALUATOR_DIGEST:
        raise ValueError(
            "evaluator_digest does not match the canonical precommitment"
        )

    raw_entries = _sequence(payload.get("entries"), field_name="entries")
    entries: list[FrozenQueueEntry] = []
    for index, raw_entry in enumerate(raw_entries):
        entry = _mapping(raw_entry, field_name=f"entries[{index}]")
        symbol = _required_string(
            entry.get("symbol"),
            field_name=f"entries[{index}].symbol",
        ).upper()
        if _SYMBOL_RE.fullmatch(symbol) is None:
            raise ValueError(f"entries[{index}].symbol is invalid")
        role = _required_string(
            entry.get("role"),
            field_name=f"entries[{index}].role",
        ).upper()
        if role not in _ROLES:
            raise ValueError(f"entries[{index}].role is invalid")
        entries.append(FrozenQueueEntry(
            symbol=symbol,
            role=role,
            reason=_required_string(
                entry.get("reason"),
                field_name=f"entries[{index}].reason",
            ),
            config_hash=_required_sha256(
                entry.get("config_hash"),
                field_name=f"entries[{index}].config_hash",
            ),
        ))
    if not entries:
        raise ValueError("entries must not be empty")
    symbols = [entry.symbol for entry in entries]
    if len(symbols) != len(set(symbols)):
        raise ValueError("entries must not contain duplicate symbols")
    control_entries = [entry for entry in entries if entry.role == "CONTROL"]
    if len(control_entries) != 1 or control_entries[0].symbol != CONTROL_SYMBOL:
        raise ValueError("entries must contain exactly one NVDA.US CONTROL")
    roles_by_symbol = {entry.symbol: entry.role for entry in entries}
    if roles_by_symbol != FROZEN_QUEUE_ROLES:
        raise ValueError(
            "entries must match the frozen AAPL/AVGO/GOOGL/TER/META "
            "queue and NVDA control roles"
        )
    entry_identity = {
        (
            entry.symbol,
            entry.role,
            entry.reason,
            entry.config_hash,
        )
        for entry in entries
    }
    if entry_identity != set(FROZEN_QUEUE_ENTRIES):
        raise ValueError(
            "entry reason or config hash does not match the canonical "
            "precommitment"
        )

    raw_context = _mapping(
        payload.get("evidence_context"),
        field_name="evidence_context",
    )
    ohlc_fidelity = _required_string(
        raw_context.get("ohlc_fidelity"),
        field_name="evidence_context.ohlc_fidelity",
    )
    if ohlc_fidelity != "ONE_MINUTE_OHLCV_BAR_CLOSE":
        raise ValueError("evidence_context.ohlc_fidelity is unsupported")
    bbo_coverage = _required_string(
        raw_context.get("bbo_coverage"),
        field_name="evidence_context.bbo_coverage",
    ).upper()
    if bbo_coverage not in _BBO_COVERAGE:
        raise ValueError("evidence_context.bbo_coverage is invalid")
    _required_true(
        raw_context.get("costs_included_in_net_pnl"),
        field_name="evidence_context.costs_included_in_net_pnl",
    )
    evidence_context = FrozenEvidenceContext(
        ohlc_source=_required_string(
            raw_context.get("ohlc_source"),
            field_name="evidence_context.ohlc_source",
        ),
        ohlc_fidelity="ONE_MINUTE_OHLCV_BAR_CLOSE",
        bbo_coverage=cast(
            Literal["NONE", "PARTIAL", "COMPLETE"],
            bbo_coverage,
        ),
        cost_model=_required_string(
            raw_context.get("cost_model"),
            field_name="evidence_context.cost_model",
        ),
        costs_included_in_net_pnl=True,
        notes=_string_list(
            raw_context.get("notes", []),
            field_name="evidence_context.notes",
        ),
    )
    expected_context = cast(
        dict[str, object],
        canonical_frozen_queue_manifest()["evidence_context"],
    )
    if evidence_context.to_dict() != expected_context:
        raise ValueError(
            "evidence_context does not match the canonical precommitment"
        )
    spec = FrozenQueueSpec(
        name=name,
        as_of_date=as_of_date,
        benchmark_symbol=benchmark_symbol,
        candidate_algorithm_version=candidate_algorithm_version,
        evaluator_digest=evaluator_digest,
        entries=tuple(entries),
        evidence_context=evidence_context,
    )
    if spec.digest != FROZEN_QUEUE_DIGEST:
        raise RuntimeError("repository canonical queue digest is inconsistent")
    return spec


def frozen_queue_digest(value: object) -> str:
    return parse_frozen_queue_spec(value).digest


def _daily_metric(
    payload: Mapping[str, object],
    *,
    session_date: date,
    target_bars: int,
) -> _DailyMetric:
    bars = _non_negative_integer(
        payload.get("bars"),
        field_name="candidate_metrics.bars",
    )
    if bars != target_bars or bars == 0:
        raise ValueError("candidate metrics must cover all target bars")
    closed_trades = _non_negative_integer(
        payload.get("closed_trades"),
        field_name="candidate_metrics.closed_trades",
    )
    gross_pnl = _finite_number(
        payload.get("gross_pnl"),
        field_name="candidate_metrics.gross_pnl",
    )
    fees = _finite_number(
        payload.get("fees"),
        field_name="candidate_metrics.fees",
    )
    if fees < 0:
        raise ValueError("candidate_metrics.fees must not be negative")
    net_pnl = _finite_number(
        payload.get("net_pnl"),
        field_name="candidate_metrics.net_pnl",
    )
    if not math.isclose(
        net_pnl,
        gross_pnl - fees,
        rel_tol=1e-8,
        abs_tol=1e-6,
    ):
        raise ValueError("candidate metrics net PnL is not cost adjusted")
    return _DailyMetric(
        session_date=session_date,
        bars=bars,
        closed_trades=closed_trades,
        gross_pnl=gross_pnl,
        fees=fees,
        net_pnl=net_pnl,
    )


def _source_evidence(
    raw_report: object,
    *,
    entry: FrozenQueueEntry,
    spec: FrozenQueueSpec,
) -> _SourceEvidence:
    blockers: list[str] = []
    excluded_targets = 0
    pre_freeze_rows_excluded = 0
    try:
        report = _mapping(raw_report, field_name=f"report[{entry.symbol}]")
        _required_false(
            report.get("order_submission_allowed"),
            field_name="order_submission_allowed",
        )
        _required_false(
            report.get("automatic_promotion_allowed"),
            field_name="automatic_promotion_allowed",
        )
        _required_false(
            report.get("historical_target_backfill_allowed"),
            field_name="historical_target_backfill_allowed",
        )
        if report.get("mode") != "SHADOW":
            raise ValueError("source report mode must be SHADOW")
        if report.get("evaluation_scope") != "FORWARD_OUT_OF_SAMPLE":
            raise ValueError(
                "source report evaluation_scope must be FORWARD_OUT_OF_SAMPLE"
            )
        status = _required_string(
            report.get("status"),
            field_name="status",
        )
        if status not in _REPORT_STATUSES:
            raise ValueError("source report status is invalid")
        source_blockers = _string_list(
            report.get("blockers", []),
            field_name="blockers",
        )
        if source_blockers:
            blockers.extend(
                f"SOURCE_FORWARD_BLOCKER:{value}"
                for value in source_blockers
            )
        registration = _mapping(
            report.get("registration"),
            field_name="registration",
        )
        if _required_string(
            registration.get("symbol"),
            field_name="registration.symbol",
        ).upper() != entry.symbol:
            raise ValueError("registration symbol does not match frozen queue")
        if _required_sha256(
            registration.get("source_config_version"),
            field_name="registration.source_config_version",
        ) != entry.config_hash:
            blockers.append("FROZEN_CONFIG_HASH_MISMATCH")
        if _required_sha256(
            registration.get("evaluator_digest"),
            field_name="registration.evaluator_digest",
        ) != spec.evaluator_digest:
            blockers.append("FROZEN_EVALUATOR_DIGEST_MISMATCH")
        if _required_string(
            registration.get("candidate_algorithm_version"),
            field_name="registration.candidate_algorithm_version",
        ) != spec.candidate_algorithm_version:
            blockers.append("FROZEN_CANDIDATE_ALGORITHM_MISMATCH")
        raw_daily = _sequence(report.get("daily"), field_name="daily")
    except ValueError:
        return _SourceEvidence(
            metrics_by_date={},
            blockers=("SOURCE_FORWARD_REPORT_INVALID",),
            excluded_targets=0,
            pre_freeze_rows_excluded=0,
        )

    metrics_by_date: dict[date, _DailyMetric] = {}
    observed_dates: set[date] = set()
    for raw_row in raw_daily:
        try:
            row = _mapping(raw_row, field_name="daily[]")
            session_date = _required_date(
                row.get("target_session_date"),
                field_name="target_session_date",
            )
            if session_date in observed_dates:
                raise ValueError("duplicate target session")
            observed_dates.add(session_date)
            disposition = _required_string(
                row.get("disposition"),
                field_name="disposition",
            )
            if disposition == "EXCLUDED":
                excluded_targets += 1
                continue
            if disposition != "INCLUDED":
                raise ValueError("daily disposition is invalid")
            if session_date <= spec.as_of_date:
                pre_freeze_rows_excluded += 1
                continue
            _required_false(
                row.get("structural_failure"),
                field_name="structural_failure",
            )
            if row.get("exclusion_reason") not in {"", None}:
                raise ValueError("included row has an exclusion reason")
            _required_true(
                row.get("same_target_bars"),
                field_name="same_target_bars",
            )
            _required_true(
                row.get("baseline_replay_match"),
                field_name="baseline_replay_match",
            )
            _required_true(
                row.get("session_local_invariant"),
                field_name="session_local_invariant",
            )
            target_bars = _non_negative_integer(
                row.get("target_bars"),
                field_name="target_bars",
            )
            if target_bars == 0:
                raise ValueError("target_bars must be positive")
            baseline_input_hash = _required_sha256(
                row.get("baseline_input_sha256"),
                field_name="baseline_input_sha256",
            )
            candidate_input_hash = _required_sha256(
                row.get("candidate_input_sha256"),
                field_name="candidate_input_sha256",
            )
            if baseline_input_hash != candidate_input_hash:
                raise ValueError("baseline and candidate inputs differ")
            for field_name in (
                "seed_bars_sha256",
                "target_bars_sha256",
                "baseline_result_sha256",
                "candidate_result_sha256",
                "evidence_digest_sha256",
            ):
                _required_sha256(
                    row.get(field_name),
                    field_name=field_name,
                )
            metric = _daily_metric(
                _mapping(
                    row.get("candidate_metrics"),
                    field_name="candidate_metrics",
                ),
                session_date=session_date,
                target_bars=target_bars,
            )
        except ValueError:
            blockers.append("SOURCE_INCLUDED_EVIDENCE_INVALID")
            continue
        metrics_by_date[session_date] = metric

    return _SourceEvidence(
        metrics_by_date=metrics_by_date,
        blockers=tuple(dict.fromkeys(blockers)),
        excluded_targets=excluded_targets,
        pre_freeze_rows_excluded=pre_freeze_rows_excluded,
    )


def _round_metric(value: float) -> float:
    return round(value, 6)


def _metric_summary(
    metrics: Sequence[_DailyMetric],
) -> dict[str, object]:
    trades = sum(item.closed_trades for item in metrics)
    net_pnl = sum(item.net_pnl for item in metrics)
    return {
        "trade_days": len(metrics),
        "bars": sum(item.bars for item in metrics),
        "closed_trades": trades,
        "gross_pnl": _round_metric(sum(item.gross_pnl for item in metrics)),
        "estimated_fees": _round_metric(sum(item.fees for item in metrics)),
        "net_pnl": _round_metric(net_pnl),
        "average_net_pnl_per_trade": (
            _round_metric(net_pnl / trades) if trades else None
        ),
    }


def _window_digest(dates: Sequence[date]) -> str:
    return hashlib.sha256(
        "\n".join(value.isoformat() for value in dates).encode("ascii")
    ).hexdigest()


def _limitations(context: FrozenEvidenceContext) -> list[str]:
    bbo_limitation = {
        "NONE": (
            "Historical BBO is unavailable; spread, queue position, and "
            "touch availability cannot be replayed."
        ),
        "PARTIAL": (
            "Historical BBO coverage is partial and cannot provide a "
            "uniform executable-price comparison."
        ),
        "COMPLETE": (
            "BBO snapshots do not reproduce queue priority, partial fills, "
            "order rejection, or market impact."
        ),
    }[context.bbo_coverage]
    return [
        (
            "One-minute OHLCV bar-close evidence cannot recover intrabar "
            "quote paths or exact trigger/fill ordering."
        ),
        bbo_limitation,
        (
            "Gross PnL uses the frozen source evaluator's configured "
            "slippage; fees are estimates under the declared cost model, "
            "not broker-confirmed execution costs."
        ),
        (
            "Cross-symbol PnL is descriptive and inherits the source "
            "evaluator's sizing assumptions."
        ),
        (
            "Only sessions strictly after the frozen as-of date count; the "
            "shortlist's pre-freeze history is excluded."
        ),
        (
            "Twenty future trade days and fifty closed trades are evidence "
            "sufficiency gates, not promotion thresholds."
        ),
        (
            "The repository-owned canonical manifest freezes queue identity; "
            "the caller-supplied digest verifies identity but is not itself "
            "a trusted timestamp."
        ),
        (
            "Forward-validation JSON is caller-supplied and unsigned; digest "
            "format checks cannot prove that the exported content was not "
            "altered before evaluation."
        ),
    ]


def evaluate_frozen_forward_disproof_queue(
    value: object,
) -> dict[str, object]:
    payload = _mapping(value, field_name="input")
    if payload.get("schema_version") != FROZEN_DISPROOF_INPUT_SCHEMA_VERSION:
        raise ValueError("input schema_version is unsupported")
    spec = parse_frozen_queue_spec(payload.get("frozen_queue"))
    expected_digest_raw = payload.get("precommitted_freeze_digest")
    precommit_verified = False
    if expected_digest_raw is not None:
        expected_digest = _required_sha256(
            expected_digest_raw,
            field_name="precommitted_freeze_digest",
        )
        if expected_digest != spec.digest:
            raise ValueError("precommitted_freeze_digest does not match queue")
        precommit_verified = True
    reports = _mapping(
        payload.get("forward_reports", {}),
        field_name="forward_reports",
    )

    source_by_symbol: dict[str, _SourceEvidence] = {}
    for entry in spec.entries:
        raw_report = reports.get(entry.symbol)
        if raw_report is None:
            source_by_symbol[entry.symbol] = _SourceEvidence(
                metrics_by_date={},
                blockers=("SOURCE_FORWARD_REPORT_MISSING",),
                excluded_targets=0,
                pre_freeze_rows_excluded=0,
            )
            continue
        source_by_symbol[entry.symbol] = _source_evidence(
            raw_report,
            entry=entry,
            spec=spec,
        )

    control = source_by_symbol[spec.benchmark_symbol]
    candidate_reports: list[dict[str, object]] = []
    for entry in sorted(
        (row for row in spec.entries if row.role != "CONTROL"),
        key=lambda row: row.symbol,
    ):
        source = source_by_symbol[entry.symbol]
        common_dates = tuple(sorted(
            set(source.metrics_by_date).intersection(control.metrics_by_date)
        ))
        candidate_metrics = [
            source.metrics_by_date[value] for value in common_dates
        ]
        control_metrics = [
            control.metrics_by_date[value] for value in common_dates
        ]
        candidate_summary = _metric_summary(candidate_metrics)
        control_summary = _metric_summary(control_metrics)
        blockers = [*control.blockers, *source.blockers]
        if not precommit_verified:
            blockers.append("FREEZE_PRECOMMITMENT_MISSING")
        if len(common_dates) < MINIMUM_FUTURE_TRADE_DAYS:
            blockers.append("FUTURE_TRADE_DAYS_INSUFFICIENT")
        candidate_trades = cast(int, candidate_summary["closed_trades"])
        control_trades = cast(int, control_summary["closed_trades"])
        if candidate_trades < MINIMUM_CLOSED_TRADES:
            blockers.append("CANDIDATE_CLOSED_TRADES_INSUFFICIENT")
        if control_trades < MINIMUM_CLOSED_TRADES:
            blockers.append("NVDA_CONTROL_CLOSED_TRADES_INSUFFICIENT")
        blockers = list(dict.fromkeys(blockers))
        ready = not blockers
        candidate_net = cast(float, candidate_summary["net_pnl"])
        control_net = cast(float, control_summary["net_pnl"])
        candidate_reports.append({
            "symbol": entry.symbol,
            "role": entry.role,
            "reason": entry.reason,
            "config_hash": entry.config_hash,
            "status": (
                "READY_FOR_MANUAL_DISPROOF_REVIEW"
                if ready
                else "INSUFFICIENT_EVIDENCE"
            ),
            "manual_disproof_review_ready": ready,
            "automatic_disproof_decision_allowed": False,
            "common_future_trade_days": len(common_dates),
            "remaining_future_trade_days": max(
                0,
                MINIMUM_FUTURE_TRADE_DAYS - len(common_dates),
            ),
            "first_common_session_date": (
                common_dates[0].isoformat() if common_dates else None
            ),
            "last_common_session_date": (
                common_dates[-1].isoformat() if common_dates else None
            ),
            "same_window_digest": _window_digest(common_dates),
            "candidate_source_future_days": len(source.metrics_by_date),
            "nvda_source_future_days": len(control.metrics_by_date),
            "candidate_excluded_targets": source.excluded_targets,
            "nvda_excluded_targets": control.excluded_targets,
            "candidate_pre_freeze_rows_excluded": (
                source.pre_freeze_rows_excluded
            ),
            "nvda_pre_freeze_rows_excluded": (
                control.pre_freeze_rows_excluded
            ),
            "candidate": candidate_summary,
            "nvda_same_window_control": control_summary,
            "observed_net_pnl_delta_vs_nvda": _round_metric(
                candidate_net - control_net
            ),
            "remaining_candidate_closed_trades": max(
                0,
                MINIMUM_CLOSED_TRADES - candidate_trades,
            ),
            "remaining_nvda_closed_trades": max(
                0,
                MINIMUM_CLOSED_TRADES - control_trades,
            ),
            "blockers": blockers,
        })

    all_ready = bool(candidate_reports) and all(
        item["manual_disproof_review_ready"] is True
        for item in candidate_reports
    )
    status: FrozenQueueStatus = (
        "READY_FOR_MANUAL_DISPROOF_REVIEW"
        if all_ready
        else "INSUFFICIENT_EVIDENCE"
    )
    return {
        "schema_version": FROZEN_DISPROOF_OUTPUT_SCHEMA_VERSION,
        "algorithm_version": FROZEN_DISPROOF_ALGORITHM_VERSION,
        "status": status,
        "research_only": True,
        "live_equivalent": False,
        "order_submission_allowed": False,
        "automatic_promotion_allowed": False,
        "automatic_disproof_decision_allowed": False,
        "parameter_tuning_performed": False,
        "promotion_recommendation": "NONE",
        "freeze": {
            **spec.canonical_payload(),
            "freeze_digest": spec.digest,
            "precommit_verified": precommit_verified,
        },
        "evidence_thresholds": {
            "minimum_future_trade_days": MINIMUM_FUTURE_TRADE_DAYS,
            "minimum_closed_trades_per_candidate": MINIMUM_CLOSED_TRADES,
            "minimum_closed_trades_for_nvda_control": MINIMUM_CLOSED_TRADES,
            "same_window_nvda_comparison_required": True,
            "thresholds_tunable": False,
        },
        "fidelity_mode": "ONE_MINUTE_OHLCV_FORWARD_APPROXIMATION",
        "limitations": _limitations(spec.evidence_context),
        "candidates": candidate_reports,
    }


__all__ = [
    "CONTROL_SYMBOL",
    "FORWARD_CANDIDATE_ALGORITHM_VERSION",
    "FROZEN_DISPROOF_ALGORITHM_VERSION",
    "FROZEN_DISPROOF_INPUT_SCHEMA_VERSION",
    "FROZEN_DISPROOF_OUTPUT_SCHEMA_VERSION",
    "FROZEN_QUEUE_AS_OF_DATE",
    "FROZEN_QUEUE_DIGEST",
    "FROZEN_QUEUE_ROLES",
    "MINIMUM_CLOSED_TRADES",
    "MINIMUM_FUTURE_TRADE_DAYS",
    "canonical_frozen_queue_manifest",
    "evaluate_frozen_forward_disproof_queue",
    "frozen_queue_digest",
    "parse_frozen_queue_spec",
]
