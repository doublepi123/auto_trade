"""Confirmatory evaluation layer (PREREGISTRATION §10.8).

Pure statistics over a frozen cohort of resolved trade records:

1.  **Input validation** (G1): every record's bps fields must be finite,
    notionals finite and positive, ``trade_day`` a ``date``.  An invalid
    record is BLOCKING evidence — the verdict is the distinct
    ``BLOCKED_INVALID_INPUT`` carrying the count; records are never
    dropped and never counted toward the floors.
2.  **Cohort isolation** (L643-652): every record must carry the
    evaluated ``algorithm_version`` AND ``config_digest``; a mismatch
    raises (fail-closed, never silently filtered).  An unresolved exit
    gap blocks confirmatory certification entirely (§10.6 L599-600)
    with the distinct verdict ``BLOCKED_EXIT_GAP``.
3.  **Day budget** (L698-705, G6): distinct confirmatory days beyond
    ``final_traded_days_budget`` (100) raise — never truncate, never
    extend the t table.
4.  **Analysis floors** (L658-660): ≥ 20 distinct days, ≥ 30 resolved
    PRICE brackets, and gross/net each ≥ 30 observations over ≥ 20 days.
5.  **The four promotion ANDs** (L662-678), each reported separately,
    computed on the EXACT rational CR1 basis (G2) so AND #1 and the
    §10.9 futility upper bound cannot drift apart (L716).
6.  **Terminal timing** (L696-705): the 100th distinct confirmatory
    trading day or 24 calendar months after ``evidence_start_at``,
    whichever comes first; never extended.

``terminal=False`` computes NO promotion ANDs at all (L707: interim
allows only data-quality and futility checks): every AND is reported as
``NOT_EVALUATED`` and the verdict stays ``INTERIM_NO_CERTIFICATION``.
``trial_family_status`` gates AND #4 (L680-689): only
``VALID_SINGLE_CONFIRMATORY`` allows the N=1 DSR to pass; any other
status fails closed with the reason recorded.

All four ANDs holding (terminal, valid family) yields
``ELIGIBLE_FOR_HUMAN_REVIEW`` — never an automatic promotion (L691).
"""

from __future__ import annotations

import decimal
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from typing import Any, Final, Mapping, Sequence
from types import MappingProxyType
from zoneinfo import ZoneInfo

from app.domain.guidance_continuation.config import (
    ALGORITHM_VERSION,
    DEFAULT_GUIDANCE_CONFIG,
    GuidanceContinuationConfig,
)
from app.domain.guidance_continuation.config import (
    config_digest as config_digest_of,
)
from app.domain.guidance_continuation.costs import cost_columns
from app.domain.guidance_continuation.exit import (
    TRIGGER_EOD_FLATTEN,
    TRIGGER_MAX_HOLD,
    TRIGGER_PRICE_STOP,
    TRIGGER_PROFIT_TARGET,
)
from app.domain.strategy_v2.clustered_returns import (
    clustered_t_test,
    day_cluster_t_critical,
)

VERDICT_INSUFFICIENT_DATA: Final[str] = "INSUFFICIENT_DATA"
VERDICT_BLOCKED_EXIT_GAP: Final[str] = "BLOCKED_EXIT_GAP"
VERDICT_ELIGIBLE_FOR_HUMAN_REVIEW: Final[str] = "ELIGIBLE_FOR_HUMAN_REVIEW"
VERDICT_NOT_CERTIFIED: Final[str] = "NOT_CERTIFIED"
VERDICT_INTERIM_NO_CERTIFICATION: Final[str] = "INTERIM_NO_CERTIFICATION"
VERDICT_BLOCKED_INVALID_INPUT: Final[str] = "BLOCKED_INVALID_INPUT"

INSUFFICIENT_DATA = VERDICT_INSUFFICIENT_DATA
BLOCKED_EXIT_GAP = VERDICT_BLOCKED_EXIT_GAP
NOT_CERTIFIED = VERDICT_NOT_CERTIFIED
INTERIM_NO_CERTIFICATION = VERDICT_INTERIM_NO_CERTIFICATION
BLOCKED_INVALID_INPUT = VERDICT_BLOCKED_INVALID_INPUT

#: Trial-family status constants (§10.8 L680-689): the confirmatory
#: family must remain ONE frozen rule evaluated ONCE on data no candidate
#: scoring has touched — any violation loses N=1 and AND #4 fails closed.
VALID_SINGLE_CONFIRMATORY: Final[str] = "VALID_SINGLE_CONFIRMATORY"
INVALIDATED: Final[str] = "INVALIDATED"
TRIAL_FAMILY_UNKNOWN: Final[str] = "UNKNOWN"

#: Per-AND status values (distinct from the strictly-boolean and_*_pass
#: fields, which are False on every non-evaluated path).
AND_NOT_EVALUATED: Final[str] = "NOT_EVALUATED"
AND_PASS: Final[str] = "PASS"
AND_FAIL: Final[str] = "FAIL"

PRICE_BRACKET_TRIGGERS: Final[frozenset[str]] = frozenset(
    {"PRICE_STOP", "PROFIT_TARGET"}
)

TERMINAL_REASON_DAYS: Final[str] = "TRADED_DAYS_100"
TERMINAL_REASON_MONTHS: Final[str] = "CALENDAR_MONTHS_24"

_DSR_CONFIDENCE: Final[float] = 0.95


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """One resolved (or exit-gapped) confirmatory trade.

    CONTRACT (V4): ``gross_return_bps`` is the price-return on the ENTRY
    notional, ``(exit_notional − entry_notional) / entry_notional ×
    10⁴``; ``net_baseline_bps`` / ``net_confirmatory_bps`` are that same
    gross minus the corresponding ``costs.cost_columns`` total converted
    to bps on the entry notional (``column_usd / entry_notional × 10⁴``).
    The evaluator recomputes ALL THREE columns from the notionals in
    exact Decimal arithmetic and rejects a record whose columns disagree
    by more than ``_COST_TOLERANCE_BPS``; a non-finite recomputation
    (Decimal or float) is invalid input, never a silently-accepted NaN.

    ``resolved=False`` marks an unresolved exit gap (§10.6 L599-600),
    which BLOCKS certification.  A gap record validates on the ENTRY side
    only: the return columns and ``exit_notional`` may be ``None`` or
    NaN, and ``exit_trigger`` may be ``None`` (no barrier observed) or a
    valid trigger (triggered but unfilled).  A RESOLVED record must carry
    one of the four §10.6 triggers — never None (M1).

    SIZING SCOPE (§10.5): this layer enforces only the NOTIONAL cap
    (``entry_notional <= config.notional_cap_usd``).  The 100-share cap
    and the 250 USD risk cap depend on quantity, which is NOT a field of
    this record — their enforcement belongs to the sizing layer and the
    P3b producer that mints records.
    """

    trade_day: date
    entry_notional: Decimal
    exit_notional: Decimal | None
    gross_return_bps: float | None
    net_baseline_bps: float | None
    net_confirmatory_bps: float | None
    exit_trigger: str | None
    algorithm_version: str
    config_digest: str
    resolved: bool


@dataclass(frozen=True, slots=True)
class CohortEvaluation:
    """The four ``and_*_pass`` fields are STRICTLY ``bool``: True only
    when that AND was actually evaluated AND passed.  Every
    non-evaluated path (interim, gap, invalid input, floors not met)
    gives False — never a truthy sentinel.  WHY each AND is False lives
    in ``and_status`` (PASS / FAIL / NOT_EVALUATED), and ``ands_evaluated``
    says whether the ANDs ran at all."""

    verdict: str
    distinct_days: int
    resolved_price_brackets: int
    and_net_pass: bool
    and_first_passage_pass: bool
    and_sample_size_pass: bool
    and_dsr_pass: bool
    net_ci: dict[str, Any]
    first_passage: dict[str, Any]
    dsr: dict[str, Any]
    exit_gap_records: int = 0
    floors: dict[str, Any] = field(default_factory=dict)
    failed_ands: tuple[str, ...] = ()
    invalid_input_records: int = 0
    and_status: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType(
            {
                "net": AND_NOT_EVALUATED,
                "first_passage": AND_NOT_EVALUATED,
                "sample_size": AND_NOT_EVALUATED,
                "dsr": AND_NOT_EVALUATED,
            }
        )
    )
    ands_evaluated: bool = False

    @property
    def eligible_for_human_review(self) -> bool:
        return self.verdict == VERDICT_ELIGIBLE_FOR_HUMAN_REVIEW


#: The only legal exit triggers, imported from the §10.6 state machine.
_VALID_TRIGGERS: Final[frozenset[str]] = frozenset(
    {
        TRIGGER_PRICE_STOP,
        TRIGGER_PROFIT_TARGET,
        TRIGGER_MAX_HOLD,
        TRIGGER_EOD_FLATTEN,
    }
)

#: V4 cross-check tolerance, absolute in bps.  The two net columns are
#: floats recomputed by the CALLER from Decimal ``cost_columns`` output;
#: the float round-trip (Decimal→float→bps) loses at most one ulp per
#: operation, which at typical notionals (~1e4 bps scale) is < 1e-9 bps.
#: 1e-9 therefore accepts every faithful round-trip and rejects any
#: genuine column error (≥ 1e-3 bps in practice).
_COST_TOLERANCE_BPS: Final[float] = 1e-9


def _within(a: float, b: float, tol: float) -> bool:
    """``|a − b| <= tol`` with NaN/inf IMPOSSIBLE to pass (M2).

    ``abs(x − NaN) > tol`` is False, so a bare tolerance comparison
    ACCEPTS a NaN — this helper returns False whenever either side is
    non-finite, so no NaN can ever reach a verdict.
    """
    if not (math.isfinite(a) and math.isfinite(b)):
        return False
    return abs(a - b) <= tol


def _record_problems(
    record: TradeRecord,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> str | None:
    """G1 validation: None when the record is usable, else the reason.

    Blocking evidence: a record failing any check is never dropped, never
    counted toward floors — it poisons the whole evaluation.

    A RESOLVED record validates fully: plain-date day, finite positive
    entry/exit notionals, finite numeric returns, a valid trigger (M1:
    None is legal ONLY on a gap — a resolved record must name its
    trigger), bool ``resolved``, and gross plus both net columns
    matching an all-Decimal ``costs.py`` recomputation within
    ``_COST_TOLERANCE_BPS`` (V4; H1 adds the gross cross-check; M2 makes
    the recomputation Decimal-only with explicit finite checks, so a
    finite-but-huge Decimal notionals overflow is invalid input, never a
    NaN that compares False).

    An UNRESOLVED record (exit gap) validates on the ENTRY side only
    (V3): plain-date day, finite positive entry_notional, valid
    version/digest strings, and ``exit_trigger`` either None or a valid
    trigger.  Return columns and exit_notional may be None or NaN.
    """
    # R1: datetime is a date SUBCLASS; only exact dates are valid days.
    if type(record.trade_day) is not date:
        return "trade_day is not a plain datetime.date"
    if not isinstance(record.entry_notional, Decimal) or not record.entry_notional.is_finite() or record.entry_notional <= 0:
        return "entry_notional is not a finite positive Decimal"
    # W4 (§10.5): a record above the frozen notional cap is not a valid
    # confirmatory observation.  Share/risk caps belong to the sizing
    # layer and P3b (quantity is not on the record); the exit notional is
    # NOT capped because price can move.
    if record.entry_notional > config.notional_cap_usd:
        return (
            f"entry_notional {record.entry_notional} exceeds the §10.5 "
            f"cap {config.notional_cap_usd}"
        )
    # V2: resolved must be a real bool (truthy strings/ints are invalid).
    if type(record.resolved) is not bool:
        return "resolved is not a bool"
    # V1: the trigger vocabulary is the four §10.6 constants.  None is
    # legal ONLY on an unresolved gap (M1): a RESOLVED record must name
    # its trigger — a resolved None previously masqueraded as a time
    # exit and silently left the first-passage denominator.
    if record.resolved:
        if record.exit_trigger is None:
            return (
                "exit_trigger is None on a resolved record (None is legal "
                "only on an unresolved exit gap)"
            )
        if record.exit_trigger not in _VALID_TRIGGERS:
            return f"exit_trigger {record.exit_trigger!r} is not one of the four §10.6 triggers"
    elif record.exit_trigger is not None and record.exit_trigger not in _VALID_TRIGGERS:
        return f"exit_trigger {record.exit_trigger!r} is not one of the four §10.6 triggers"

    if not record.resolved:
        # V3: entry-side validation only for an exit gap.
        return None

    # Resolved: full exit-side and cost validation.
    if not isinstance(record.exit_notional, Decimal) or not record.exit_notional.is_finite() or record.exit_notional <= 0:
        return "exit_notional is not a finite positive Decimal"
    for name in (
        "gross_return_bps",
        "net_baseline_bps",
        "net_confirmatory_bps",
    ):
        value = getattr(record, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{name} is not a number"
        if not math.isfinite(value):
            return f"{name} is not finite"

    # V4 (M2/H1): cross-check gross AND both net columns against a
    # costs.py recomputation done ENTIRELY in Decimal, then converted to
    # float.  A finite-but-huge Decimal (e.g. Decimal("1e400")) floats
    # to inf and the old float arithmetic produced NaN (inf − inf),
    # whose comparison against anything is False — the record failed
    # open.  Decimal arithmetic keeps every intermediate exact; a
    # non-finite Decimal result, a non-finite float conversion, or any
    # arithmetic exception (InvalidOperation, Overflow, ZeroDivision)
    # is invalid input naming the offending field.
    try:
        baseline_usd, confirmatory_usd = cost_columns(
            entry_notional=record.entry_notional,
            exit_notional=record.exit_notional,
            config=config,
        )
        gross_dec = (
            (record.exit_notional - record.entry_notional)
            / record.entry_notional
            * Decimal(10000)
        )
        baseline_dec = gross_dec - baseline_usd / record.entry_notional * Decimal(10000)
        confirmatory_dec = (
            gross_dec - confirmatory_usd / record.entry_notional * Decimal(10000)
        )
        if not (
            gross_dec.is_finite()
            and baseline_dec.is_finite()
            and confirmatory_dec.is_finite()
        ):
            return (
                "entry_notional/exit_notional produce a non-finite Decimal "
                "cost recomputation (overflow/underflow)"
            )
        gross = float(gross_dec)
        expected_baseline = float(baseline_dec)
        expected_confirmatory = float(confirmatory_dec)
        if not (
            math.isfinite(gross)
            and math.isfinite(expected_baseline)
            and math.isfinite(expected_confirmatory)
        ):
            return (
                "entry_notional/exit_notional produce a non-finite float "
                "cost recomputation (overflow/underflow)"
            )
    except (decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow, ZeroDivisionError, OverflowError) as exc:
        return (
            f"entry_notional/exit_notional cost recomputation failed for "
            f"entry_notional {record.entry_notional!r}/exit_notional "
            f"{record.exit_notional!r}: {type(exc).__name__}"
        )
    baseline_value = record.net_baseline_bps
    confirmatory_value = record.net_confirmatory_bps
    assert isinstance(baseline_value, (int, float))
    assert isinstance(confirmatory_value, (int, float))
    # _within is False whenever either side is non-finite, so a NaN can
    # never slip through a comparison (M2).
    if not _within(float(record.gross_return_bps), gross, _COST_TOLERANCE_BPS):  # type: ignore[arg-type]
        return (
            f"gross_return_bps {record.gross_return_bps!r} disagrees with "
            f"the Decimal recomputation {gross!r}"
        )
    if not _within(float(baseline_value), expected_baseline, _COST_TOLERANCE_BPS):
        return (
            f"net_baseline_bps {record.net_baseline_bps!r} disagrees with "
            f"the costs.py recomputation {expected_baseline!r}"
        )
    if not _within(float(confirmatory_value), expected_confirmatory, _COST_TOLERANCE_BPS):
        return (
            f"net_confirmatory_bps {record.net_confirmatory_bps!r} disagrees "
            f"with the costs.py recomputation {expected_confirmatory!r}"
        )
    return None


def count_invalid_inputs(
    records: Sequence[TradeRecord],
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> int:
    """Number of records failing the G1 validation contract."""
    return sum(1 for r in records if _record_problems(r, config) is not None)


def _assert_cohort_isolation(
    records: tuple[TradeRecord, ...],
    *,
    algorithm_version: str,
    config_digest: str,
) -> None:
    """Shared cohort isolation (§10.8 L643-652): every record must match
    the evaluated ``algorithm_version`` AND ``config_digest`` — a
    mismatch raises (fail-closed, never silently filtered; 不得把旧成交
    改标为新版本)."""
    for r in records:
        if r.algorithm_version != algorithm_version:
            raise ValueError(
                f"algorithm_version mismatch: record {r.algorithm_version!r} "
                f"!= evaluated {algorithm_version!r} (cohort isolation, "
                f"PREREGISTRATION §10.8 L645)"
            )
        if r.config_digest != config_digest:
            raise ValueError(
                f"config_digest mismatch: record {r.config_digest!r} != "
                f"evaluated {config_digest!r} (cohort isolation, "
                f"PREREGISTRATION §10.8 L646)"
            )


def _assert_day_budget(
    distinct_days: int, config: GuidanceContinuationConfig
) -> None:
    """G6 (§10.9 L698-705): a cohort beyond the terminal day budget is a
    caller error — never truncated, never extended."""
    if distinct_days > config.final_traded_days_budget:
        raise ValueError(
            f"distinct confirmatory days {distinct_days} exceed the "
            f"terminal budget of {config.final_traded_days_budget} "
            f"(§10.9 L698-705): the terminal check must have run at or "
            f"before day {config.final_traded_days_budget}"
        )


def _exact_cr1(
    observations: Sequence[tuple[date, float]],
) -> tuple[float, float | None]:
    """EXACT trade-weighted CR1 mean and day-clustered SE (G2).

    Every finite float converts to ``fractions.Fraction`` exactly; the
    trade mean, per-day residual sums and
    ``variance = G/(G−1) · Σ_day(Σ residual)² / n²`` are computed in
    exact rationals.  ``variance == 0`` exactly is degenerate → SE
    ``None``; otherwise ``SE = sqrt(float(variance))``.  Matches
    ``strategy_v2.clustered_returns.clustered_t_test`` on non-degenerate
    data (same mean, same CR1 SE) but sees through float residue.
    """
    values = [(day, Fraction(v)) for day, v in observations]
    n = len(values)
    if n == 0:
        raise ValueError("no observations")
    total = sum((v for _, v in values), Fraction(0))
    mean = total / n

    by_day: dict[date, list[Fraction]] = {}
    for day, value in values:
        by_day.setdefault(day, []).append(value)
    g = len(by_day)
    if g < 2:
        return float(mean), None

    cluster_score_squares = sum(
        (sum((v - mean for v in day_values), Fraction(0))) ** 2
        for day_values in by_day.values()
    )
    variance = Fraction(g) / Fraction(g - 1) * cluster_score_squares / (n * n)
    if variance == 0:
        return float(mean), None
    return float(mean), math.sqrt(float(variance))


def clopper_pearson_lower(k: int, n: int, alpha: float = 0.05) -> float:
    """Exact one-sided (1−α) Clopper-Pearson lower bound for k/n.

    ``p_L`` is the p where P(X >= k | p) = α; solved by bisection on a
    NUMERICALLY STABLE log-space binomial upper tail (G3): the pmf is
    summed via ``math.lgamma`` log-terms with a ``math.fsum`` log-sum-exp,
    so large n (thousands) never overflows — unlike direct
    ``comb·p^i`` products.  v5's ``binomial_p_upper`` is untouched.
    Edge cases: k = 0 → 0; k = n → α^(1/n).
    """
    if n < 0 or k < 0:
        raise ValueError("counts must be non-negative")
    if k > n:
        raise ValueError("k cannot exceed n")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    if k == 0:
        return 0.0
    log_alpha = math.log(alpha)

    def log_tail(p: float) -> float:
        """log P(X >= k | p), stable log-sum-exp over log-pmf terms."""
        log_p = math.log(p)
        log_1mp = math.log1p(-p)
        terms = [
            math.lgamma(n + 1)
            - math.lgamma(i + 1)
            - math.lgamma(n - i + 1)
            + i * log_p
            + (n - i) * log_1mp
            for i in range(k, n + 1)
        ]
        m = max(terms)
        return m + math.log(math.fsum(math.exp(t - m) for t in terms))

    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        # tail > alpha  ⇔  the true p_L is below mid.
        if log_tail(mid) > log_alpha:
            hi = mid
        else:
            lo = mid
    return lo


def _and_net(
    records: tuple[TradeRecord, ...]
) -> tuple[bool, dict[str, Any]]:
    """AND #1: net CI lower > 0 under confirmatory costs (L664-666).

    Exact-rational CR1 basis (``_exact_cr1``) with the frozen t critical
    at df = D−1 — the SAME basis as the §10.9 futility SE (L716).
    """
    observations = [
        (r.trade_day, float(r.net_confirmatory_bps))  # type: ignore[arg-type]
        for r in records
    ]
    mean, se = _exact_cr1(observations)
    distinct_days = len({d for d, _ in observations})
    report: dict[str, Any] = {
        "estimator": "exact-rational trade-weighted day-clustered CR1, df=D-1",
        "confirmatory_net_mean_bps": mean,
        "confirmatory_net_clustered_se_bps": se,
        "degrees_of_freedom": distinct_days - 1,
    }
    if se is None:
        report["confirmatory_net_ci_lower_bps"] = None
        report["confirmatory_net_ci_upper_bps"] = None
        report["gross_ci_lower_bps"] = None
        report["gross_ci_upper_bps"] = None
        report["baseline_net_ci_lower_bps"] = None
        report["baseline_net_ci_upper_bps"] = None
        report["t_critical"] = None
        report["fail_closed_reason"] = (
            "exact day-clustered variance is zero (degenerate cohort)"
        )
        return False, report

    critical = day_cluster_t_critical(distinct_days)
    lower = mean - critical * se
    upper = mean + critical * se
    report["confirmatory_net_ci_lower_bps"] = lower
    report["confirmatory_net_ci_upper_bps"] = upper
    report["t_critical"] = critical
    # Disclosure-only companions (L666 同时披露), same exact basis.
    for label, getter in (
        ("gross", "gross_return_bps"),
        ("baseline_net", "net_baseline_bps"),
    ):
        _, comp_se = _exact_cr1(
            [(r.trade_day, getattr(r, getter)) for r in records]
        )
        comp_mean = sum(getattr(r, getter) for r in records) / len(records)
        if comp_se is None:
            report[f"{label}_ci_lower_bps"] = None
            report[f"{label}_ci_upper_bps"] = None
        else:
            report[f"{label}_ci_lower_bps"] = comp_mean - critical * comp_se
            report[f"{label}_ci_upper_bps"] = comp_mean + critical * comp_se
    return lower > 0.0, report


def _and_first_passage(
    records: tuple[TradeRecord, ...],
    config: GuidanceContinuationConfig,
) -> tuple[bool, dict[str, Any]]:
    """AND #2: version-specific target-first vs the driftless 36% (L667-671)."""
    price = [r for r in records if r.exit_trigger in PRICE_BRACKET_TRIGGERS]
    targets = sum(1 for r in price if r.exit_trigger == "PROFIT_TARGET")
    stops = len(price) - targets
    n = len(price)
    baseline = (
        config.first_passage_stop_pct
        / (config.first_passage_stop_pct + config.first_passage_target_pct)
    )
    lower = clopper_pearson_lower(targets, n) if n > 0 else None
    time_exits = sum(
        1 for r in records if r.exit_trigger not in PRICE_BRACKET_TRIGGERS
    )
    all_target = (
        clopper_pearson_lower(targets + time_exits, n + time_exits)
        if n + time_exits
        else None
    )
    all_stop = (
        clopper_pearson_lower(targets, n + time_exits) if n + time_exits else None
    )
    report = {
        "targets": targets,
        "stops": stops,
        "n_price_brackets": n,
        "observed_share": targets / n if n else None,
        "lower_bound": lower,
        "method": (
            "exact one-sided 95% Clopper-Pearson, stable log-space tail "
            "(lgamma + fsum log-sum-exp), bisection"
        ),
        "driftless_baseline": float(baseline),
        "time_exit_count": time_exits,
        "time_exit_share": time_exits / len(records) if records else None,
        "lower_if_time_all_target": all_target,
        "lower_if_time_all_stop": all_stop,
    }
    passed = lower is not None and lower > float(baseline)
    return passed, report


def _and_sample_size(
    records: tuple[TradeRecord, ...],
    config: GuidanceContinuationConfig,
) -> tuple[bool, dict[str, Any]]:
    """AND #3: ≥ 60 distinct days and ≥ 180 resolved price brackets (L672-674)."""
    days = len({r.trade_day for r in records})
    brackets = sum(
        1 for r in records if r.exit_trigger in PRICE_BRACKET_TRIGGERS
    )
    report = {
        "distinct_days": days,
        "resolved_price_brackets": brackets,
        "min_distinct_days": config.promotion_min_distinct_days,
        "min_resolved_brackets": config.promotion_min_resolved_brackets,
    }
    return (
        days >= config.promotion_min_distinct_days
        and brackets >= config.promotion_min_resolved_brackets
    ), report


def _exact_day_means(
    observations: Sequence[tuple[date, float]],
) -> dict[date, Fraction] | None:
    """EXACT per-day equal-weight means (R3): each value → Fraction, each
    day's mean in exact rationals.  None if any day key is not a plain
    ``date`` (R1: datetime must never reach the moments)."""
    by_day: dict[date, list[Fraction]] = {}
    for day, value in observations:
        if type(day) is not date:
            return None
        by_day.setdefault(day, []).append(Fraction(float(value)))
    means: dict[date, Fraction] = {}
    for d, values in by_day.items():
        total = Fraction(0)
        for v in values:
            total += v
        means[d] = total / len(values)
    return means


def _dsr_stats(day_means: Sequence[float]) -> dict[str, Any]:
    """Day-level DSR with N=1: benchmark exactly zero (L675-678, L680-689).

    Frozen moment estimator (G4), computed EXACTLY (R3): the means and
    the central moments m2/m3/m4 are rationals — float residue (e.g.
    ``[1.1]*60`` giving m2 ≈ 1e-32) can never masquerade as variance.
    ``m2 == 0`` exactly fails closed.  Conversion to float happens only
    for the final SR / g3 / g4 / z.

    ``m_r = (1/T) Σ (x − x̄)^r`` for r = 2, 3, 4;
    ``g3 = m3 / m2^1.5``; ``g4 = m4 / m2²`` (RAW, Normal = 3);
    ``SR = mean / sample SD`` (the T−1 denominator).
    """
    t = len(day_means)
    if t < 2:
        return {
            "T": t,
            "sharpe": None,
            "sharpe_std": None,
            "z": None,
            "dsr_probability": None,
            "skewness_g3": None,
            "kurtosis_g4": None,
            "distinguishable_from_luck": False,
            "n_trials": 1,
            "fail_closed_reason": "T < 2",
        }
    exact = [Fraction(v) for v in day_means]
    total = sum(exact, Fraction(0))
    mean = total / t
    central = [(x - mean) for x in exact]
    m2 = sum((c**2 for c in central), Fraction(0)) / t
    if m2 == 0:
        return {
            "T": t,
            "sharpe": None,
            "sharpe_std": None,
            "z": None,
            "dsr_probability": None,
            "skewness_g3": None,
            "kurtosis_g4": None,
            "distinguishable_from_luck": False,
            "n_trials": 1,
            "fail_closed_reason": "zero variance across day means (exact)",
        }
    m3 = sum((c**3 for c in central), Fraction(0)) / t
    m4 = sum((c**4 for c in central), Fraction(0)) / t
    g3 = float(m3 / m2**Fraction(3, 2))
    g4 = float(m4 / (m2**2))
    sample_var = sum((c**2 for c in central), Fraction(0)) / (t - 1)
    sr = float(mean) / math.sqrt(float(sample_var))
    var_sr = (1 - g3 * sr + (g4 - 1) / 4 * sr**2) / (t - 1)
    if var_sr <= 0:
        return {
            "T": t,
            "sharpe": sr,
            "sharpe_std": None,
            "z": None,
            "dsr_probability": None,
            "skewness_g3": g3,
            "kurtosis_g4": g4,
            "distinguishable_from_luck": False,
            "n_trials": 1,
            "fail_closed_reason": "non-positive DSR variance bracket",
        }
    sharpe_std = math.sqrt(var_sr)
    z = sr / sharpe_std
    dsr_probability = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    return {
        "T": t,
        "sharpe": sr,
        "sharpe_std": sharpe_std,
        "z": z,
        "dsr_probability": dsr_probability,
        "skewness_g3": g3,
        "kurtosis_g4": g4,
        "distinguishable_from_luck": dsr_probability >= _DSR_CONFIDENCE,
        "n_trials": 1,
        "fail_closed_reason": None,
        "moment_estimator": (
            "exact-rational m_r=(1/T)Σ(x−x̄)^r; g3=m3/m2^1.5; g4=m4/m2^2 "
            "(raw); SR=mean/sample SD (T−1)"
        ),
    }


def _and_dsr(
    records: tuple[TradeRecord, ...],
    trial_family_status: str,
) -> tuple[bool, dict[str, Any]]:
    """AND #4: day-level DSR ≥ 0.95 with N=1 (L675-689).

    The trial-family gate comes FIRST: any status other than
    ``VALID_SINGLE_CONFIRMATORY`` fails closed with the reason recorded
    (L685-689: 违反任一条件 … 未完成前 AND #4 为 false).
    """
    by_day: dict[date, list[float]] = {}
    for r in records:
        by_day.setdefault(r.trade_day, []).append(
            float(r.net_confirmatory_bps)  # type: ignore[arg-type]
        )
    day_means_exact = _exact_day_means(
        [
            (r.trade_day, float(r.net_confirmatory_bps))  # type: ignore[arg-type]
            for r in records
        ]
    )
    if day_means_exact is None:  # pragma: no cover - validation upstream
        stats: dict[str, Any] = {
            "fail_closed_reason": "invalid day key",
            "dsr_probability": None,
            "distinguishable_from_luck": False,
        }
        return False, stats
    day_means = [float(m) for m in day_means_exact.values()]
    _ = by_day
    stats = _dsr_stats(day_means)
    stats["day_level_aggregation"] = (
        "equal-weight per-day mean of net_confirmatory_bps"
    )
    stats["trial_family_status"] = trial_family_status
    if trial_family_status != VALID_SINGLE_CONFIRMATORY:
        stats["fail_closed_reason"] = (
            f"trial family status {trial_family_status!r} is not "
            f"{VALID_SINGLE_CONFIRMATORY!r}: the N=1 single-confirmatory "
            f"premise does not hold, so AND #4 is false (§10.8 L685-689)"
        )
        stats["dsr_probability"] = None
        stats["distinguishable_from_luck"] = False
        return False, stats
    passed = (
        stats["dsr_probability"] is not None
        and stats["dsr_probability"] >= _DSR_CONFIDENCE
    )
    return passed, stats


def _assert_argument_contracts(
    *,
    terminal: bool,
    trial_family_status: str,
    algorithm_version: str,
    config_digest: str,
    config: GuidanceContinuationConfig,
) -> None:
    """W1/W2/W3 caller-contract checks (fail-closed, raise ValueError).

    - ``terminal`` must be a real bool (a truthy "False" string must not
      certify);
    - ``trial_family_status`` must be one of the three §10.8 constants;
    - the evaluated ``algorithm_version`` must be THIS package's constant
      (§10.8 L645: the cohort matches 本节 algorithm_version — a v5 label
      is a different study);
    - the evaluated ``config`` must hash to the claimed ``config_digest``
      (a loosened config must not ride the frozen label).
    """
    if type(terminal) is not bool:
        raise ValueError(
            f"terminal must be a bool, got {type(terminal).__name__}"
        )
    if trial_family_status not in (
        VALID_SINGLE_CONFIRMATORY,
        INVALIDATED,
        TRIAL_FAMILY_UNKNOWN,
    ):
        raise ValueError(
            f"trial_family_status {trial_family_status!r} is not one of "
            f"{VALID_SINGLE_CONFIRMATORY!r}/{INVALIDATED!r}/"
            f"{TRIAL_FAMILY_UNKNOWN!r}"
        )
    if algorithm_version != ALGORITHM_VERSION:
        raise ValueError(
            f"algorithm_version {algorithm_version!r} is not this "
            f"package's {ALGORITHM_VERSION!r} (§10.8 L645)"
        )
    computed = config_digest_of(config)
    if computed != config_digest:
        raise ValueError(
            f"the evaluated config hashes to {computed!r}, not the "
            f"claimed config_digest {config_digest!r}: a loosened config "
            f"must not ride the frozen label"
        )


def evaluate_cohort(
    records: tuple[TradeRecord, ...],
    *,
    algorithm_version: str,
    config_digest: str,
    terminal: bool,
    trial_family_status: str,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> CohortEvaluation:
    """Evaluate the four §10.8 promotion ANDs over one frozen cohort.

    Input validation and cohort isolation run first (fail-closed); an
    unresolved exit gap yields ``BLOCKED_EXIT_GAP`` without computing a
    pass over the remaining records (L656: 不得静默删除后认证); invalid
    inputs yield ``BLOCKED_INVALID_INPUT``.  ``terminal`` is REQUIRED
    (§10.9 L707): with ``terminal=False`` NO promotion AND is computed —
    every AND is ``NOT_EVALUATED`` and the verdict stays
    ``INTERIM_NO_CERTIFICATION`` with only quality/coverage info.

    Verdict semantics (§10.9 L703-704, L742): ``INSUFFICIENT_DATA`` is
    reserved for unmet evidence floors; floors met but any AND failing is
    ``NOT_CERTIFIED`` — "not certified this round", NOT a claim of
    negativity or futility.
    """
    _assert_cohort_isolation(
        records, algorithm_version=algorithm_version, config_digest=config_digest
    )
    _assert_argument_contracts(
        terminal=terminal,
        trial_family_status=trial_family_status,
        algorithm_version=algorithm_version,
        config_digest=config_digest,
        config=config,
    )

    invalid = count_invalid_inputs(records, config)
    distinct_days = len(
        {r.trade_day for r in records if type(r.trade_day) is date}
    )
    _assert_day_budget(distinct_days, config)

    brackets = sum(
        1
        for r in records
        if r.resolved and r.exit_trigger in PRICE_BRACKET_TRIGGERS
    )

    def _blocked(verdict: str, extra: dict[str, Any]) -> CohortEvaluation:
        return CohortEvaluation(
            verdict=verdict,
            distinct_days=distinct_days,
            resolved_price_brackets=brackets,
            and_net_pass=False,
            and_first_passage_pass=False,
            and_sample_size_pass=False,
            and_dsr_pass=False,
            net_ci={},
            first_passage={},
            dsr={},
            exit_gap_records=extra.get("exit_gap_records", 0),
            floors={
                "distinct_days": distinct_days,
                "resolved_price_brackets": brackets,
                "records_total": len(records),
                **{k: v for k, v in extra.items() if k != "exit_gap_records"},
            },
            failed_ands=(),
            invalid_input_records=invalid,
        )

    gaps = sum(1 for r in records if type(r.resolved) is bool and r.resolved is False)

    # V3 precedence: an INVALID record blocks first; a GAP blocks next.
    # BOTH counts are reported on BOTH paths.
    if invalid:
        return _blocked(
            VERDICT_BLOCKED_INVALID_INPUT,
            {"invalid_input_records": invalid, "exit_gap_records": gaps},
        )

    if gaps:
        return _blocked(
            VERDICT_BLOCKED_EXIT_GAP,
            {"exit_gap_records": gaps, "invalid_input_records": invalid},
        )

    gross_ok = len(records) >= config.min_gross_net_observations and (
        distinct_days >= config.min_gross_net_distinct_days
    )
    floors = {
        "distinct_days": distinct_days,
        "resolved_price_brackets": brackets,
        "min_distinct_days": config.analysis_min_distinct_days,
        "min_resolved_brackets": config.analysis_min_resolved_brackets,
        "gross_net_observations": len(records),
        "min_gross_net_observations": config.min_gross_net_observations,
        "gross_net_distinct_days": distinct_days,
        "min_gross_net_distinct_days": config.min_gross_net_distinct_days,
        "floors_met": (
            distinct_days >= config.analysis_min_distinct_days
            and brackets >= config.analysis_min_resolved_brackets
            and gross_ok
        ),
        "terminal": terminal,
        "trial_family_status": trial_family_status,
    }

    if not terminal:
        # §10.9 L707: interim allows ONLY data-quality and futility
        # checks — no promotion AND is computed at all.
        return CohortEvaluation(
            verdict=VERDICT_INTERIM_NO_CERTIFICATION,
            distinct_days=distinct_days,
            resolved_price_brackets=brackets,
            and_net_pass=False,
            and_first_passage_pass=False,
            and_sample_size_pass=False,
            and_dsr_pass=False,
            net_ci={},
            first_passage={},
            dsr={},
            exit_gap_records=0,
            floors=floors,
            failed_ands=(),
            invalid_input_records=0,
        )

    if not floors["floors_met"]:
        return CohortEvaluation(
            verdict=VERDICT_INSUFFICIENT_DATA,
            distinct_days=distinct_days,
            resolved_price_brackets=brackets,
            and_net_pass=False,
            and_first_passage_pass=False,
            and_sample_size_pass=False,
            and_dsr_pass=False,
            net_ci={},
            first_passage={},
            dsr={},
            exit_gap_records=0,
            floors=floors,
            failed_ands=(),
            invalid_input_records=0,
        )

    net_pass, net_report = _and_net(records)
    fp_pass, fp_report = _and_first_passage(records, config)
    ss_pass, ss_report = _and_sample_size(records, config)
    dsr_pass, dsr_report = _and_dsr(records, trial_family_status)

    failed_ands: tuple[str, ...] = tuple(
        name
        for name, ok in (
            ("net", net_pass),
            ("first_passage", fp_pass),
            ("sample_size", ss_pass),
            ("dsr", dsr_pass),
        )
        if not ok
    )

    if failed_ands:
        verdict = VERDICT_NOT_CERTIFIED
    else:
        verdict = VERDICT_ELIGIBLE_FOR_HUMAN_REVIEW
    return CohortEvaluation(
        verdict=verdict,
        distinct_days=distinct_days,
        resolved_price_brackets=brackets,
        and_net_pass=net_pass,
        and_first_passage_pass=fp_pass,
        and_sample_size_pass=ss_pass,
        and_dsr_pass=dsr_pass,
        net_ci=net_report,
        first_passage=fp_report,
        dsr=dsr_report,
        exit_gap_records=0,
        floors=floors | {"sample_size": ss_report},
        failed_ands=failed_ands,
        invalid_input_records=0,
        and_status=MappingProxyType(
            {
                "net": AND_PASS if net_pass else AND_FAIL,
                "first_passage": AND_PASS if fp_pass else AND_FAIL,
                "sample_size": AND_PASS if ss_pass else AND_FAIL,
                "dsr": AND_PASS if dsr_pass else AND_FAIL,
            }
        ),
        ands_evaluated=True,
    )


def _add_months_clamped(start: date, months: int) -> date:
    """Same day-of-month, clamped to the target month's end (L699)."""
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    last_day = next_month_first.toordinal() - date(year, month, 1).toordinal()
    return date(year, month, min(start.day, last_day))


def terminal_due(
    traded_days: int,
    *,
    evidence_start_at_et: datetime,
    as_of: date,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> tuple[bool, str]:
    """The §10.9 L696-705 one-shot terminal-timing rule.

    Due at the 100th distinct confirmatory trading day, or 24 calendar
    months after ``evidence_start_at`` (same day-of-month, clamped at
    month end, America/New_York), whichever comes FIRST.  Once due it
    never extends (L702: 不得因为届时未达到 180 个价格 bracket 而延长).
    """
    if evidence_start_at_et.tzinfo is None:
        raise ValueError("evidence_start_at_et must be timezone-aware")
    if type(as_of) is not date:
        raise ValueError(
            f"as_of must be a plain datetime.date, got {type(as_of).__name__}"
        )
    start_local = evidence_start_at_et.astimezone(ZoneInfo("America/New_York")).date()
    months_deadline = _add_months_clamped(
        start_local, config.final_calendar_months_budget
    )
    days_budget = config.final_traded_days_budget
    if as_of >= months_deadline:
        return True, TERMINAL_REASON_MONTHS
    if traded_days >= days_budget:
        return True, TERMINAL_REASON_DAYS
    return False, ""
