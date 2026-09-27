"""Futility assessment for the §10 cohort (PREREGISTRATION §10.9).

Independently named and implemented (:func:`assess_guidance_futility`);
v5's ``assess_futility``, its 10 bps floor and its t table are NOT
modified, overridden or reused (L737-739).

Definitions (L714-718), all in bps on ``net_confirmatory_bps``:

- ``μ_net``  — per-trade mean net (confirmatory cost basis), EXACT
  rational CR1 basis via ``_exact_cr1`` (G2);
- ``SE_net`` — the same exact day-clustered standard error — identical
  to AND #1's basis (L716 同口径);
- ``U_net = μ_net + 2.0 × SE_net``;
- ``required_effect = max(0, −μ_net)``;
- ``MDE = (z_0.95 + z_0.80) × 20.0 / sqrt(D)`` using the config constants.

Fail-closed ladder: invalid inputs (G1) → ``BLOCKED_INVALID_INPUT``;
exit gaps (§10.6 L598-600) → ``BLOCKED_EXIT_GAP``; beyond the terminal
day budget (G6) → raises; degenerate exact SE → ``INSUFFICIENT_DATA``
with ``fail_closed_reason`` (never a 0.0 substitution).  Mechanical
rules (L724-731) then decide ALIVE / FUTILE / INSUFFICIENT_DATA; the
three verdicts plus budget exhaustion stay DISTINCT (L742).

The machine digest (G7, L709) binds the FULL input: a canonical record
manifest (every field, deterministically sorted), its sha256, the
algorithm/config identity, the first/last trade days and the explicit
``as_of`` evaluation instant.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from typing import Any, Final, Sequence

from app.domain.guidance_continuation.config import (
    DEFAULT_GUIDANCE_CONFIG,
    GuidanceContinuationConfig,
    _dec,
)
from app.domain.guidance_continuation.evaluation import (
    PRICE_BRACKET_TRIGGERS,
    VALID_SINGLE_CONFIRMATORY,
    TradeRecord,
    _assert_cohort_isolation,
    _assert_day_budget,
    _assert_argument_contracts,
    _exact_cr1,
    count_invalid_inputs,
)
from app.domain.guidance_continuation.config import ALGORITHM_VERSION

FUTILE: Final[str] = "FUTILE"
ALIVE: Final[str] = "ALIVE"
INSUFFICIENT_DATA_FUTILITY: Final[str] = "INSUFFICIENT_DATA"
BUDGET_EXHAUSTED: Final[str] = "BUDGET_EXHAUSTED"
BLOCKED_EXIT_GAP_FUTILITY: Final[str] = "BLOCKED_EXIT_GAP"
BLOCKED_INVALID_INPUT_FUTILITY: Final[str] = "BLOCKED_INVALID_INPUT"


@dataclass(frozen=True, slots=True)
class FutilityAssessment:
    verdict: str
    mu_net_bps: float | None
    se_net_bps: float | None
    u_net_bps: float | None
    required_effect_bps: float | None
    mde_bps: float | None
    measured_day_dispersion_bps: float | None
    mde_at_measured_dispersion_bps: float | None
    requires_measured_dispersion_ratification: bool
    machine_inputs: dict[str, Any] = field(default_factory=dict)
    machine_outputs: dict[str, Any] = field(default_factory=dict)
    digest: str = ""


def _canonical(value: Any) -> Any:
    """JSON-safe canonicalisation using the config's ``_dec`` for Decimals."""
    if isinstance(value, Decimal):
        return _dec(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, float):
        return repr(value)
    return value


def _digest_of(inputs: dict[str, Any], outputs: dict[str, Any]) -> str:
    """sha256 over the canonical JSON of {inputs, outputs} (L709)."""
    return hashlib.sha256(
        json.dumps(
            _canonical({"inputs": inputs, "outputs": outputs}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def record_manifest(records: Sequence[TradeRecord]) -> tuple[list[Any], str]:
    """Canonical, deterministically sorted manifest of every record.

    R2: ONE comparable sort key — a tuple of STRINGS, where every bps
    value is canonicalised as ``repr(float(v))`` (so the int 5 and the
    float 5.0 agree), Decimals via ``_dec`` and dates via ``isoformat``.
    Sorted by (trade_day, then all other fields) so input ORDER never
    changes the manifest.

    Assumes VALIDATED records (the caller validates first); a None or
    non-numeric bps would raise here, which is why the invalid-input path
    uses the diagnostic encoder instead.
    """
    fields = (
        "trade_day",
        "entry_notional",
        "exit_notional",
        "gross_return_bps",
        "net_baseline_bps",
        "net_confirmatory_bps",
        "exit_trigger",
        "algorithm_version",
        "config_digest",
        "resolved",
    )

    def enc(r: TradeRecord, name: str) -> str:
        value = getattr(r, name)
        if isinstance(value, Decimal):
            # A gap record may legally carry a NaN exit notional (no
            # realised exit); encode it as a plain repr instead of the
            # canonical _dec, which rejects non-finite values.
            if not value.is_finite():
                return f"nan[{type(value).__name__}]"
            return _dec(value)
        if value is None:
            return "None"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            as_float = float(value)
            if not math.isfinite(as_float):
                return f"{as_float!r}[{type(value).__name__}]"
            return repr(as_float)
        return str(value)

    def sort_key(r: TradeRecord) -> tuple[str, ...]:
        return tuple(enc(r, f) for f in fields)

    entries = [
        {f: enc(r, f) for f in fields}
        for r in sorted(records, key=sort_key)
    ]
    manifest_json = json.dumps(
        entries, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    sha = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
    return entries, sha


def _diagnostic_manifest_sha(records: Sequence[TradeRecord]) -> str:
    """Never-raising encoding for INVALID records (R2).

    ``repr()`` of every field with its type name, in a stable field
    order; record order is the arrival order (the cohort is invalid —
    the diagnostic only has to be deterministic, not canonical).
    """
    fields = (
        "trade_day",
        "entry_notional",
        "exit_notional",
        "gross_return_bps",
        "net_baseline_bps",
        "net_confirmatory_bps",
        "exit_trigger",
        "algorithm_version",
        "config_digest",
        "resolved",
    )
    parts = [
        [
            f"{f}={getattr(r, f)!r}[{type(getattr(r, f)).__name__}]"
            for f in fields
        ]
        for r in records
    ]
    payload = json.dumps(parts, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def futility_checkpoint_due(
    traded_days: int,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> bool:
    """True exactly at the 20/40/60/80 confirmatory traded-day
    checkpoints (L708); the terminal point is :func:`terminal_due`."""
    return traded_days in config.futility_checkpoint_days


def assess_guidance_futility(
    records: tuple[TradeRecord, ...],
    *,
    algorithm_version: str,
    config_digest: str,
    as_of: datetime,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> FutilityAssessment:
    """Assess §10.9 futility over one cohort of resolved trades.

    ``as_of`` (tz-aware) is the evaluation instant, bound into the digest.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    _assert_cohort_isolation(
        records, algorithm_version=algorithm_version, config_digest=config_digest
    )
    _assert_argument_contracts(
        terminal=True,  # futility runs at checkpoints AND at terminal
        trial_family_status=VALID_SINGLE_CONFIRMATORY,
        algorithm_version=algorithm_version,
        config_digest=config_digest,
        config=config,
    )

    # R1/R2: VALIDATE FIRST — an invalid record (including a datetime
    # trade_day) is rejected before any day counting, budget check,
    # clustering or canonical encoding.
    invalid = count_invalid_inputs(records, config)
    valid_days = [r.trade_day for r in records if type(r.trade_day) is date]
    distinct_days = len(set(valid_days))
    gaps = sum(
        1 for r in records if type(r.resolved) is bool and r.resolved is False
    )

    # R2: the invalid path uses the never-raising DIAGNOSTIC encoding.
    # V3 precedence: invalid blocks first; both counts are reported.
    if invalid:
        inputs = {
            "observations": len(records),
            "distinct_days": distinct_days,
            "record_manifest_sha256": _diagnostic_manifest_sha(records),
            "manifest_encoding": "diagnostic",
            "algorithm_version": algorithm_version,
            "config_digest": config_digest,
            "first_trade_day": None,
            "last_trade_day": None,
            "as_of": as_of.isoformat(),
            "invalid_input_records": invalid,
            "exit_gap_records": gaps,
        }
        outputs = {"verdict": BLOCKED_INVALID_INPUT_FUTILITY}
        return FutilityAssessment(
            verdict=BLOCKED_INVALID_INPUT_FUTILITY,
            mu_net_bps=None,
            se_net_bps=None,
            u_net_bps=None,
            required_effect_bps=None,
            mde_bps=None,
            measured_day_dispersion_bps=None,
            mde_at_measured_dispersion_bps=None,
            requires_measured_dispersion_ratification=False,
            machine_inputs=inputs,
            machine_outputs=outputs,
            digest=_digest_of(inputs, outputs),
        )

    _assert_day_budget(distinct_days, config)

    # Valid records only from here: the canonical manifest cannot raise.
    _, manifest_sha = record_manifest(records)
    first_day = min(valid_days).isoformat() if valid_days else None
    last_day = max(valid_days).isoformat() if valid_days else None

    def _blocked(
        verdict: str, extra: dict[str, Any]
    ) -> FutilityAssessment:
        inputs = {
            "observations": len(records),
            "distinct_days": distinct_days,
            "record_manifest_sha256": manifest_sha,
            "algorithm_version": algorithm_version,
            "config_digest": config_digest,
            "first_trade_day": first_day,
            "last_trade_day": last_day,
            "as_of": as_of.isoformat(),
            **extra,
        }
        outputs = {"verdict": verdict}
        return FutilityAssessment(
            verdict=verdict,
            mu_net_bps=None,
            se_net_bps=None,
            u_net_bps=None,
            required_effect_bps=None,
            mde_bps=None,
            measured_day_dispersion_bps=None,
            mde_at_measured_dispersion_bps=None,
            requires_measured_dispersion_ratification=False,
            machine_inputs=inputs,
            machine_outputs=outputs,
            digest=_digest_of(inputs, outputs),
        )

    if gaps:
        return _blocked(
            BLOCKED_EXIT_GAP_FUTILITY,
            {"exit_gap_records": gaps, "invalid_input_records": invalid},
        )

    resolved = list(records)
    brackets = sum(
        1 for r in resolved if r.exit_trigger in PRICE_BRACKET_TRIGGERS
    )
    observations = len(resolved)
    floors_met = (
        distinct_days >= config.analysis_min_distinct_days
        and brackets >= config.analysis_min_resolved_brackets
        and observations >= config.min_gross_net_observations
        and distinct_days >= config.min_gross_net_distinct_days
    )

    z_alpha = config.mde_z_alpha
    z_power = config.mde_z_power
    sigma = float(config.mde_sigma_day_bps)

    def mde(dispersion: float, days: int) -> float:
        return (
            (z_alpha + z_power) * dispersion / math.sqrt(days)
            if days
            else float("nan")
        )

    # G7: a checkpoint label only when distinct_days EQUALS a checkpoint
    # day — 25 is not "past the 20 checkpoint".
    checkpoint = next(
        (c for c in config.futility_checkpoint_days if c == distinct_days),
        None,
    )

    by_day: dict[date, list[float]] = {}
    for r in resolved:
        by_day.setdefault(r.trade_day, []).append(
            float(r.net_confirmatory_bps)  # type: ignore[arg-type]
        )
    # R4: EXACT per-day means and dispersion — the stdev of day means in
    # exact rationals, converted to float at the end, so the digest never
    # depends on input order through float-summation residue.
    exact_means = {
        d: sum((Fraction(v) for v in values), Fraction(0)) / len(values)
        for d, values in by_day.items()
    }
    day_means = [float(m) for m in sorted(exact_means.values())]
    if len(day_means) >= 2:
        exact_dm = sum(exact_means.values()) / len(exact_means)
        exact_var = sum(
            ((m - exact_dm) ** 2 for m in exact_means.values()), Fraction(0)
        ) / (len(exact_means) - 1)
        measured = math.sqrt(float(exact_var)) if exact_var > 0 else 0.0
    else:
        measured = 0.0

    mu: float | None = None
    se: float | None = None
    upper: float | None = None
    required: float | None = None
    planned_mde: float | None = None
    measured_mde: float | None = None
    fail_closed_reason: str | None = None
    verdict: str

    if not floors_met:
        verdict = INSUFFICIENT_DATA_FUTILITY
    else:
        # G2: EXACT rational CR1 — float residue can never masquerade as
        # a tight bound; variance == 0 exactly is degenerate.
        mu, se = _exact_cr1(
            [
                (r.trade_day, float(r.net_confirmatory_bps))  # type: ignore[arg-type]
                for r in resolved
            ]
        )
        if se is None or se <= 0.0:
            fail_closed_reason = (
                "exact day-clustered variance is zero (degenerate cohort): "
                "no tight upper bound can be claimed"
            )
            verdict = INSUFFICIENT_DATA_FUTILITY
            mu = None if se is None else mu
            se = None
        else:
            upper = mu + float(config.futility_upper_bound_multiplier) * se
            required = max(0.0, -mu)
            planned_mde = mde(sigma, distinct_days)
            if upper >= 0:
                verdict = ALIVE
            elif planned_mde <= required:
                verdict = FUTILE
            else:
                verdict = INSUFFICIENT_DATA_FUTILITY

    requires_ratification = (
        floors_met and fail_closed_reason is None and measured > sigma
    )
    if requires_ratification:
        measured_mde = mde(measured, distinct_days)

    inputs = {
        "observations": observations,
        "distinct_days": distinct_days,
        "resolved_price_brackets": brackets,
        "floors_met": floors_met,
        "checkpoint": checkpoint,
        "record_manifest_sha256": manifest_sha,
        "algorithm_version": algorithm_version,
        "config_digest": config_digest,
        "first_trade_day": first_day,
        "last_trade_day": last_day,
        "as_of": as_of.isoformat(),
        "mu_net_bps": mu,
        "se_net_bps": se,
        "upper_multiplier": float(config.futility_upper_bound_multiplier),
        "u_net_bps": upper,
        "required_effect_bps": required,
        "mde_planning_sigma_bps": sigma,
        "mde_bps": planned_mde,
        "measured_day_dispersion_bps": measured,
        "mde_at_measured_dispersion_bps": measured_mde,
        "requires_measured_dispersion_ratification": requires_ratification,
        "fail_closed_reason": fail_closed_reason,
        "z_alpha": z_alpha,
        "z_power": z_power,
    }
    outputs = {
        "verdict": verdict,
        "distinct_verdicts": [
            ALIVE,
            FUTILE,
            INSUFFICIENT_DATA_FUTILITY,
            BUDGET_EXHAUSTED,
            BLOCKED_EXIT_GAP_FUTILITY,
            BLOCKED_INVALID_INPUT_FUTILITY,
        ],
    }
    digest = _digest_of(inputs, outputs)

    return FutilityAssessment(
        verdict=verdict,
        mu_net_bps=mu,
        se_net_bps=se,
        u_net_bps=upper,
        required_effect_bps=required,
        mde_bps=planned_mde,
        measured_day_dispersion_bps=measured,
        mde_at_measured_dispersion_bps=measured_mde,
        requires_measured_dispersion_ratification=requires_ratification,
        machine_inputs=inputs,
        machine_outputs=outputs,
        digest=digest,
    )
