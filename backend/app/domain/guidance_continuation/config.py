"""Frozen configuration for the earnings revenue-guidance continuation study.

Encodes ``PREREGISTRATION.md`` §10 (lines 388-802) literally.  Every rule
constant of ``earnings-revenue-guidance-continuation-v1`` lives in exactly
one immutable object; behaviour modules in this package may only *read* this
config and must not define their own thresholds.

``config_digest()`` is the **P1a draft digest** (§10's ``config_version``):
the SHA-256 of the canonical JSON of :func:`config_payload`.  It covers the
rules implemented in the P1a package; P2/P3 MUST extend it before
REGISTRATION.  Any rule change requires a new ``algorithm_version`` per
PREREGISTRATION §4/§10.8.

Decimal rule constants serialize LOSSLESSLY as canonical decimal strings
built from ``as_tuple()`` with NO Decimal arithmetic (see :func:`_dec`):
``0.020`` and ``0.02`` hash identically, ``0.020000000000000000000000000
000001`` differs, NaN/Infinity are rejected, and the digest is identical
under any ``decimal.localcontext`` precision.

Purity: stdlib only (see ``app/domain/AGENTS.md``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from statistics import NormalDist
from typing import Any, Final

ALGORITHM_VERSION: Final[str] = "earnings-revenue-guidance-continuation-v1"

#: z_0.95 for the §10.9 MDE planning formula (NormalDist().inv_cdf(0.95)).
_Z_ALPHA_0_95: Final[float] = NormalDist().inv_cdf(0.95)
#: z_0.80 for the §10.9 MDE planning formula (NormalDist().inv_cdf(0.80)).
_Z_POWER_0_80: Final[float] = NormalDist().inv_cdf(0.80)


@dataclass(frozen=True, slots=True)
class GuidanceContinuationConfig:
    """Every frozen constant of PREREGISTRATION §10 in one place.

    Money, prices and dimensionless rule thresholds are ``Decimal`` so the
    inclusive boundaries of §10 compare exactly.  ``*_et`` fields are
    America/New_York wall-clock times (no tzinfo by design; the behaviour
    modules attach the US session timezone from ``market_calendar``).
    """

    # ---- §10.2 universe ----
    #: Base pool: point-in-time union of these US indices, deduplicated.
    universe_indices: tuple[str, ...] = ("NASDAQ_100", "DJIA")
    universe_market: str = "US"
    universe_currency: str = "USD"
    #: T−1 close band, inclusive at both endpoints.
    tminus1_close_min_usd: Decimal = Decimal("20")
    tminus1_close_max_usd: Decimal = Decimal("500")
    #: Number of complete US trading days of history required (§10.2/§10.4).
    adv_lookback_days: int = 20
    #: 20-day mean of daily ``close × volume`` must be at least this.
    adv_min_avg_daily_turnover_usd: Decimal = Decimal("100000000")

    # ---- §10.3 announcement event ----
    guidance_metric: str = "TOTAL_REVENUE"
    guidance_currency: str = "USD"
    #: Announcement window right endpoint (inclusive), ET.
    announcement_window_end_et: time = time(9, 0)
    #: Immutable registration deadline for announcements and index data, ET.
    registration_deadline_et: time = time(9, 25)
    #: Prior-guidance lookback in calendar days (§10.3).
    prior_guidance_lookback_days: int = 120
    #: Minimum midpoint raise, inclusive.
    min_midpoint_raise: Decimal = Decimal("0.02")

    # ---- §10.4 entry conditions ----
    entry_bar_count: int = 15
    entry_window_first_bar_start_et: time = time(9, 30)
    entry_window_last_bar_start_et: time = time(9, 44)
    #: Gap band ``0.01 <= O/Pprev - 1 <= 0.05``, inclusive both ends.
    gap_min: Decimal = Decimal("0.01")
    gap_max: Decimal = Decimal("0.05")
    #: ``C15/O - 1 >= 0.002``.
    min_c15_over_o_gain: Decimal = Decimal("0.002")
    #: ``RVOL15 >= 2.0`` over this many prior complete trading days.
    rvol15_min: Decimal = Decimal("2.0")
    rvol_history_days: int = 20
    #: All inputs must have been obtained by this ET time.
    inputs_deadline_et: time = time(9, 45, 59)
    #: The single virtual entry attempt window, ET.
    entry_attempt_window_start_et: time = time(9, 46, 0)
    entry_attempt_window_end_et: time = time(9, 46, 5)

    # ---- §10.4 quote validity ----
    #: ``received_at - quote_ts <= 1`` second.  A NEGATIVE age
    #: (``quote_ts`` after ``received_at``) fails closed as STALE —
    #: no clock-skew tolerance is accepted in this version.
    quote_max_age_seconds: Decimal = Decimal("1")
    #: ``(ask-bid)/((ask+bid)/2) * 10000 <= 5`` bps, inclusive.
    max_spread_bps: Decimal = Decimal("5")
    #: Wait at least 1 second after the reference quote before a fill counts.
    entry_wait_seconds: Decimal = Decimal("1")
    #: Fill confirmation window after the wait.
    entry_fill_window_seconds: Decimal = Decimal("5")

    # ---- §10.5 sizing / hard bounds ----
    quantity_cap_shares: int = 100
    notional_cap_usd: Decimal = Decimal("25000")
    risk_cap_usd: Decimal = Decimal("250")
    #: US equity tick for prices >= $1.00 (SEC Rule 612).  Assumption: the
    #: sub-$1.00 sub-penny tier is unreachable because the §10.2 universe
    #: floors the T−1 close at $20.
    us_tick_size: Decimal = Decimal("0.01")
    us_tick_size_min_price: Decimal = Decimal("1.00")

    # ---- §10.6 exits ----
    stop_loss_pct: Decimal = Decimal("0.0045")
    profit_target_pct: Decimal = Decimal("0.0080")
    max_hold_minutes: int = 60
    entry_cutoff_minutes_before_close: int = 45
    flatten_minutes_before_close: int = 15
    exit_wait_seconds: Decimal = Decimal("1")

    # ---- §10.7 cost model ----
    commission_fixed_usd: Decimal = Decimal("1.568")
    commission_rate: Decimal = Decimal("0.0000641")
    execution_deduction_bps_baseline: Decimal = Decimal("0.031")
    execution_deduction_bps_confirmatory: Decimal = Decimal("2.031")

    # ---- §10.8 evaluation floors / promotion ----
    analysis_min_distinct_days: int = 20
    analysis_min_resolved_brackets: int = 30
    min_gross_net_observations: int = 30
    min_gross_net_distinct_days: int = 20
    promotion_min_distinct_days: int = 60
    promotion_min_resolved_brackets: int = 180
    #: Driftless first-passage baseline 0.45/(0.45+0.80) = 36%.
    first_passage_stop_pct: Decimal = Decimal("0.45")
    first_passage_target_pct: Decimal = Decimal("0.80")

    # ---- §10.9 stopping / futility ----
    futility_checkpoint_days: tuple[int, ...] = (20, 40, 60, 80)
    final_traded_days_budget: int = 100
    final_calendar_months_budget: int = 24
    #: ``U = mean + 2.0 * SE``.
    futility_upper_bound_multiplier: Decimal = Decimal("2.0")
    #: Frozen MDE planning sigma (bps/day).
    mde_sigma_day_bps: Decimal = Decimal("20.0")
    mde_z_alpha: float = _Z_ALPHA_0_95
    mde_z_power: float = _Z_POWER_0_80


DEFAULT_GUIDANCE_CONFIG: Final[GuidanceContinuationConfig] = (
    GuidanceContinuationConfig()
)


def _dec(value: Decimal) -> str:
    """Lossless canonical string for a Decimal rule constant.

    Built with NO Decimal arithmetic (so the result is independent of the
    active context precision — ``Decimal.normalize()`` rounds at prec=28
    and is therefore NOT lossless).  Construction from
    ``value.as_tuple()`` (sign, digits, exponent):

    - NaN / Infinity are rejected (``ValueError``);
    - the sign of zero is dropped (``-0`` → ``"0"``);
    - a positive exponent expands integrally (``2E+1`` → ``"20"``);
    - trailing zeros AFTER the decimal point are stripped (``0.0200`` →
      ``"0.02"``, ``2.0`` → ``"2"``), and a trailing ``.`` is removed;
    - every other digit is preserved verbatim: ``0.02`` == ``0.020`` →
      ``"0.02"``, while ``0.020000000000000000000000000000001`` keeps all
      31 digits and hashes differently.
    """
    if not value.is_finite():
        raise ValueError(f"non-finite rule constant: {value}")
    sign, digits, exponent = value.as_tuple()
    digit_str = "".join(str(d) for d in digits)
    if not isinstance(exponent, int):  # pragma: no cover - NaN/Inf filtered
        raise ValueError(f"non-finite rule constant: {value}")
    if exponent >= 0:
        expanded = digit_str + "0" * exponent
        body = expanded.lstrip("0") or "0"
        return f"-{body}" if sign and body != "0" else body
    # Negative exponent: place the decimal point.
    point = len(digit_str) + exponent
    if point > 0:
        body = digit_str[:point] + "." + digit_str[point:]
    else:
        body = "0." + "0" * (-point) + digit_str
    # Strip insignificant trailing zeros after the point, then a lone '.'.
    if "." in body:
        body = body.rstrip("0").rstrip(".")
    if body in ("", "-"):
        body = "0"
    # Drop the sign of a zero result (Decimal("-0") == Decimal("0")).
    if sign and body != "0":
        return f"-{body}"
    return body

def config_payload(
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> dict[str, Any]:
    """Canonical, JSON-serializable view of every implemented §10 rule.

    Decimal constants appear as canonical decimal STRINGS (see :func:`_dec`)
    so the digest is lossless.  ``rule_versions`` pins the semantics of each
    behaviour module as implemented in P1a; P2/P3 must extend this payload
    before REGISTRATION.
    """
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "digest_scope": "P1a-draft",
        "rule_versions": {
            # Named semantics pinned by tests; bump on any rule change.
            "universe": "p1a-1: exact-20-complete-trading-days-by-calendar",
            "membership": "p1a-3: single-baseline+per-index-coverage-any-rule-tri-state",
            "eligibility": "p1a-2: latest-prior-no-fallback+3-timestamps+order-independent-conflicts",
            "entry_conditions": "p1a-1: v15-pairs-history+ohlc-consistency",
            "quotes": "p1a-1: qualify-then-fill+result-semantics",
            "sizing": "p1a-1: floor-caps+single-tick-min-price",
            "exit": "p1a-1: qualify+deadline-instant+same-instant-priority",
            "costs": "p1a-1: two-columns+spread-not-double-counted",
            "config_serialization": (
                "p1a-2: canonical decimal strings from as_tuple() with no "
                "Decimal arithmetic; precision-independent"
            ),
            "evaluator": (
                "p3a-8: argument-contracts+config-digest-binding"
                "+version-pin+notional-cap+trigger+resolved"
                "+gap-entry-side-validation+cost-column-cross-check"
                "+resolved-trigger-required+decimal-recompute-finite"
                "+gross-cross-check+date-only-trade-day+exact-dsr-moments"
                "+strict-bool-ands+and-status-mapping+validated-inputs"
                "+exact-cr1+stable-cp+not-evaluated-interim+trial-family"
                "+day-budget"
            ),
            "futility": (
                "p3a-7: argument-contracts+config-digest-binding"
                "+version-pin+notional-cap+shared-full-validation"
                "+validate-then-encode+order-independent-digest"
                "+exact-dispersion+exact-cr1-upper-bound"
                "+gap-and-degenerate-fail-closed+day-budget"
                "+resolved-trigger-required+decimal-recompute-finite"
            ),
        },
        "universe": {
            "indices": sorted(config.universe_indices),
            "market": config.universe_market,
            "currency": config.universe_currency,
            "tminus1_close_min_usd": _dec(config.tminus1_close_min_usd),
            "tminus1_close_max_usd": _dec(config.tminus1_close_max_usd),
            "adv_lookback_days": config.adv_lookback_days,
            "adv_min_avg_daily_turnover_usd": _dec(
                config.adv_min_avg_daily_turnover_usd
            ),
            "history_days_definition": (
                "the 20 most recent complete US trading days strictly before "
                "the target day, unique and contiguous per "
                "market_calendar/holiday_calendar; half days count as "
                "complete trading days (§10.2 L448)"
            ),
            "membership_semantics": (
                "tri-state MEMBER/NOT_MEMBER/UNKNOWN, order-independent at "
                "every step; per index exactly one applicable baseline (the "
                "latest baseline_effective_date <= target_day among "
                "snapshots obtained < target 09:25 ET; conflicting "
                "same-date snapshots give UNKNOWN, exact duplicates "
                "deduplicate); coverage per index: covered if ANY proof "
                "attests changes complete through the target day AND was "
                "obtained < 09:25 ET (late or short proofs are ignored, "
                "never overriding a valid one); coverage is required for "
                "BOTH MEMBER and NOT_MEMBER — an old snapshot is never "
                "valid forever; changes applied after the baseline date "
                "and <= target day, ordered by (effective_date, "
                "first_observed_at); late-learned or same-date conflicting "
                "changes give UNKNOWN; overall MEMBER if any index resolves "
                "MEMBER, NOT_MEMBER only if every index resolves NOT_MEMBER "
                "with valid coverage; missing sources never prove MEMBER "
                "or NOT_MEMBER (§10.2 L437-446)"
            ),
        },
        "announcement": {
            "metric": config.guidance_metric,
            "currency": config.guidance_currency,
            "window_end_et": config.announcement_window_end_et.isoformat(),
            "registration_deadline_et": config.registration_deadline_et.isoformat(),
            "prior_guidance_lookback_days": config.prior_guidance_lookback_days,
            "min_midpoint_raise": _dec(config.min_midpoint_raise),
            "lookback_definition": (
                "120 natural days on the America/New_York LOCAL calendar, "
                "boundary inclusive (§10.3 L476)"
            ),
            "prior_selection": (
                "the most recent statement for (symbol, fiscal_year) "
                "published before the new one, INCLUDING incomparable or "
                "not-yet-visible ones; never fall back to an older "
                "statement (§10.3 L487)"
            ),
            "visibility_rule": (
                "source_obtained(first_observed_at), transcription_reviewed_at "
                "and registered_at must ALL be strictly before 09:25 ET on "
                "the target day, for both the new statement and the prior "
                "(§10.3 L470-471)"
            ),
        },
        "entry": {
            "bar_count": config.entry_bar_count,
            "first_bar_start_et": config.entry_window_first_bar_start_et.isoformat(),
            "last_bar_start_et": config.entry_window_last_bar_start_et.isoformat(),
            "gap_min": _dec(config.gap_min),
            "gap_max": _dec(config.gap_max),
            "min_c15_over_o_gain": _dec(config.min_c15_over_o_gain),
            "rvol15_min": _dec(config.rvol15_min),
            "rvol_history_days": config.rvol_history_days,
            "inputs_deadline_et": config.inputs_deadline_et.isoformat(),
            "attempt_window_start_et": config.entry_attempt_window_start_et.isoformat(),
            "attempt_window_end_et": config.entry_attempt_window_end_et.isoformat(),
            "endpoint_inclusivity": (
                "bars are [first_bar_start, last_bar_start] starts; inputs "
                "deadline right-inclusive; attempt window inclusive both "
                "ends; entry cutoff 45 min before real close (half days "
                "included)"
            ),
            "bar_validity": (
                "low <= min(open, close) and max(open, close) <= high, all "
                "OHLC finite and positive, volume > 0 (§10.4 L496-509)"
            ),
            "v15_history_definition": (
                "(trading_day, V15) pairs over the SAME 20-day set as the "
                "universe window, every value finite and > 0; median over "
                "the 20 values (§10.4 L506)"
            ),
            "vwap15_definition": (
                "sum(((H+L+C)/3) * V) / sum(V) over the 15 bars, Decimal"
            ),
        },
        "quotes": {
            "max_age_seconds": _dec(config.quote_max_age_seconds),
            "max_spread_bps": _dec(config.max_spread_bps),
            "entry_wait_seconds": _dec(config.entry_wait_seconds),
            "entry_fill_window_seconds": _dec(config.entry_fill_window_seconds),
            "qualification": (
                "0 <= received_at - quote_ts <= 1 s (negative age fails "
                "closed as STALE); bid > 0; ask >= bid; mid spread <= 5 bps; "
                "ages computed on UTC-converted instants so DST folds are "
                "handled (§10.4 L525-529)"
            ),
            "result_semantics": (
                "FILLED / UNFILLED / NO_ATTEMPT (quotes present, none "
                "qualifying) / MISSED_WINDOW (no quote received in the "
                "frozen 09:46:00-09:46:05 ET window); quantity is always "
                "the package's position_quantity — no external quantity_fn"
            ),
        },
        "sizing": {
            "quantity_cap_shares": config.quantity_cap_shares,
            "notional_cap_usd": _dec(config.notional_cap_usd),
            "risk_cap_usd": _dec(config.risk_cap_usd),
            "us_tick_size": _dec(config.us_tick_size),
            "us_tick_size_min_price": _dec(config.us_tick_size_min_price),
            "tick_rule": (
                "single $0.01 tick for prices >= us_tick_size_min_price "
                "($1.00); prices below it are REJECTED by this version "
                "(SEC Rule 612 sub-dollar tier unsupported)"
            ),
        },
        "exit": {
            "stop_loss_pct": _dec(config.stop_loss_pct),
            "profit_target_pct": _dec(config.profit_target_pct),
            "max_hold_minutes": config.max_hold_minutes,
            "entry_cutoff_minutes_before_close": (
                config.entry_cutoff_minutes_before_close
            ),
            "flatten_minutes_before_close": config.flatten_minutes_before_close,
            "exit_wait_seconds": _dec(config.exit_wait_seconds),
            "trigger_semantics": (
                "only §10.4-qualifying quotes trigger or fill; price "
                "barriers evaluated on qualifying quotes at instants <= the "
                "earlier deadline; time exits trigger AT the deadline "
                "instant; same-instant priority stop > flatten > holding > "
                "target (§10.6 L586-591)"
            ),
            "fill_semantics": (
                "first qualifying quote with bid_size >= qty at >= "
                "trigger_at + 1 s, never the trigger quote itself; target "
                "fills at min(target, bid); others at the bid; unresolved "
                "exits stay EXIT_GAP, delays preserved (§10.6 L593-600)"
            ),
        },
        "costs": {
            "commission_fixed_usd": _dec(config.commission_fixed_usd),
            "commission_rate": _dec(config.commission_rate),
            "execution_deduction_bps_baseline": _dec(
                config.execution_deduction_bps_baseline
            ),
            "execution_deduction_bps_confirmatory": _dec(
                config.execution_deduction_bps_confirmatory
            ),
            "column_identifiers": ["baseline", "confirmatory"],
            "spread_handling": (
                "spread is already inside the simulated fill prices (ask "
                "buys, bid sells) and is NOT deducted again (§10.7 L621-622)"
            ),
        },
        "evaluation": {
            "analysis_min_distinct_days": config.analysis_min_distinct_days,
            "analysis_min_resolved_brackets": config.analysis_min_resolved_brackets,
            "min_gross_net_observations": config.min_gross_net_observations,
            "min_gross_net_distinct_days": config.min_gross_net_distinct_days,
            "promotion_min_distinct_days": config.promotion_min_distinct_days,
            "promotion_min_resolved_brackets": config.promotion_min_resolved_brackets,
            "first_passage_stop_pct": _dec(config.first_passage_stop_pct),
            "first_passage_target_pct": _dec(config.first_passage_target_pct),
            "first_passage_driftless_baseline": _dec(
                first_passage_driftless_baseline(config)
            ),
            "futility_checkpoint_traded_days": list(config.futility_checkpoint_days),
            "final_traded_days_budget": config.final_traded_days_budget,
            "final_calendar_months_budget": config.final_calendar_months_budget,
            "futility_upper_bound_multiplier": _dec(
                config.futility_upper_bound_multiplier
            ),
            "mde_sigma_day_bps": _dec(config.mde_sigma_day_bps),
            "mde_z_alpha": config.mde_z_alpha,
            "mde_z_power": config.mde_z_power,
            "evaluator_semantics": {
                "cohort_isolation": (
                    "every record must match the evaluated algorithm_version "
                    "AND config_digest; a mismatch raises ValueError "
                    "(fail-closed, never silently filtered) in BOTH "
                    "evaluate_cohort and assess_guidance_futility via one "
                    "shared helper; unresolved exit gaps BLOCK with the "
                    "distinct verdict BLOCKED_EXIT_GAP in both evaluators "
                    "and records are never dropped (§10.8 L643-656, "
                    "§10.6 L598-600)"
                ),
                "input_validation": (
                    "RESOLVED records validate fully: gross_return_bps, "
                    "net_baseline_bps and net_confirmatory_bps all real "
                    "finite numbers (bool rejected, int and float both "
                    "legal); entry_notional and exit_notional finite "
                    "positive Decimals; trade_day a PLAIN datetime.date (a "
                    "datetime is a date subclass and is invalid, rejected "
                    "before any day counting, budget check or clustering, "
                    "never coerced); exit_trigger one of exactly "
                    "PRICE_STOP/PROFIT_TARGET/MAX_HOLD/EOD_FLATTEN "
                    "(imported from the exit state machine) and NEVER None "
                    "on a resolved record (None is legal only on an "
                    "unresolved gap; a resolved None previously vanished "
                    "into the first-passage denominator as a time exit); "
                    "resolved a real bool; and ALL THREE precomputed "
                    "columns (gross and both nets) cross-checked against "
                    "a costs.cost_columns recomputation performed ENTIRELY "
                    "in exact Decimal arithmetic "
                    "(gross=(exit-entry)/entry*1e4; net = gross - "
                    "column_usd/entry*1e4) within 1e-9 bps absolute — a "
                    "float round-trip bound — naming the offending column. "
                    "The Decimal recomputation is finite-checked at BOTH "
                    "stages (Decimal is_finite, then math.isfinite on the "
                    "float conversion); any non-finite result or "
                    "arithmetic exception (InvalidOperation, Overflow, "
                    "DivisionByZero) is invalid input naming the notional, "
                    "and the column comparison itself returns False on any "
                    "non-finite operand so a NaN can never pass. "
                    "UNRESOLVED (exit-gap) records validate on the ENTRY "
                    "side only: plain date, finite positive entry_notional, "
                    "valid version/digest, exit_trigger None or a valid "
                    "trigger; return fields and exit_notional may be None "
                    "or NaN.  Precedence in both evaluators: any invalid "
                    "record gives BLOCKED_INVALID_INPUT first, else any gap "
                    "gives BLOCKED_EXIT_GAP, with BOTH counts reported on "
                    "both paths; records never dropped, never counted "
                    "toward floors.  Validation runs BEFORE any encoding: "
                    "valid records are encoded canonically (single "
                    "comparable string-tuple sort key; bps as float repr "
                    "so int 5 and float 5.0 agree; Decimals via _dec with "
                    "non-finite gap-side values as typed reprs; dates "
                    "isoformat); invalid cohorts use a separate "
                    "never-raising DIAGNOSTIC encoding (typed repr per "
                    "field) for their digest"
                ),
                "estimator_basis": (
                    "EXACT rational CR1: floats converted to Fraction, "
                    "trade mean, per-day residual sums and variance = "
                    "G/(G-1)*sum_day(sum residual)^2/n^2 computed exactly; "
                    "variance == 0 exactly is degenerate and fails closed; "
                    "SE = sqrt(float(variance)); used by AND #1 AND the "
                    "futility SE so the two cannot drift (§10.9 L716); "
                    "frozen t critical from day_cluster_t_critical at "
                    "df = D-1"
                ),
                "clopper_pearson_tail": (
                    "numerically stable log-space binomial upper tail "
                    "(math.lgamma terms with an fsum log-sum-exp) so large "
                    "n never overflows; bisection to the alpha=0.05 "
                    "defining equation; k=n equals alpha^(1/n)"
                ),
                "dsr_moments": (
                    "frozen estimator, computed EXACTLY: per-day means and "
                    "central moments m_r=(1/T)*sum(x-mean)^r as rationals "
                    "(fractions.Fraction), so float residue can never "
                    "masquerade as variance; m2 == 0 exactly fails closed; "
                    "g3=m3/m2^1.5; g4=m4/m2^2 (RAW, Normal=3); "
                    "SR=mean/sample SD (T-1 denominator); conversion to "
                    "float only for the final SR/g3/g4/z; N=1 benchmark 0"
                ),
                "interim_semantics": (
                    "terminal=False computes NO promotion AND at all: the "
                    "four and_*_pass fields are strictly bool and are "
                    "False on every non-evaluated path (interim, gap, "
                    "invalid input, floors not met) — never a truthy "
                    "sentinel; the reason lives in the immutable and_status "
                    "mapping (PASS/FAIL/NOT_EVALUATED keyed by net/"
                    "first_passage/sample_size/dsr), ands_evaluated is "
                    "False, reports are empty and failed_ands=(); only "
                    "floors, day/bracket counts, exit-gap and "
                    "invalid-input status are returned; the verdict stays "
                    "INTERIM_NO_CERTIFICATION (§10.9 L707)"
                ),
                "argument_contracts": (
                    "evaluate_cohort and assess_guidance_futility raise "
                    "ValueError unless: terminal is a real bool (a truthy "
                    "'False' string must not certify); trial_family_status "
                    "is one of the three §10.8 constants; the evaluated "
                    "algorithm_version equals this package's "
                    "ALGORITHM_VERSION constant (§10.8 L645 — a v5 label "
                    "is a different study); and config_digest(config) "
                    "equals the claimed config_digest argument, so a "
                    "loosened config cannot ride the frozen label.  "
                    "terminal_due requires as_of to be a plain "
                    "datetime.date (type(x) is date) and "
                    "evidence_start_at_et to be tz-aware"
                ),
                "notional_cap": (
                    "entry_notional above config.notional_cap_usd "
                    "(25,000 USD) is invalid input for resolved AND gap "
                    "records (§10.5); share-cap and risk-cap enforcement "
                    "belongs to the sizing layer and P3b (quantity is not "
                    "on the record); the exit notional is NOT capped "
                    "because price can move"
                ),
                "trial_family_gate": (
                    "required keyword trial_family_status with values "
                    "VALID_SINGLE_CONFIRMATORY / INVALIDATED / UNKNOWN; "
                    "AND #4 passes only on VALID_SINGLE_CONFIRMATORY with "
                    "DSR >= 0.95; any other status fails closed with the "
                    "reason recorded (§10.8 L680-689)"
                ),
                "day_budget": (
                    "distinct confirmatory days above "
                    "final_traded_days_budget (100) raise ValueError in "
                    "both evaluators: the terminal check must have run at "
                    "or before day 100; never truncate, never extend the "
                    "t table (§10.9 L698-705)"
                ),
                "and_1_net": (
                    "exact-rational CR1 on net_confirmatory_bps "
                    "(_exact_cr1): trade-weighted per-trade mean, exact "
                    "day-clustered SE, two-sided 95% CI with the frozen "
                    "Student-t critical at df=D-1; degenerate exact "
                    "variance (zero) fails closed; pass requires CI lower "
                    "> 0; gross and baseline-net CIs disclosed only "
                    "(§10.8 L664-666)"
                ),
                "and_2_first_passage": (
                    "over PRICE_STOP/PROFIT_TARGET only; k=targets, "
                    "n=resolved price brackets (time exits excluded from the "
                    "denominator); one-sided 95% exact Clopper-Pearson lower "
                    "bound by bisection on the exact binomial tail; pass "
                    "requires lower > 0.45/1.25 = 0.36; count, share and "
                    "extreme-classification sensitivity of time exits "
                    "disclosed (§10.8 L667-671)"
                ),
                "and_3_sample": (
                    "at least 60 distinct trading days and 180 resolved "
                    "price brackets; same-day cross-symbol trades are not "
                    "distinct days (§10.8 L672-674)"
                ),
                "and_4_dsr": (
                    "per-day equal-weight means of net_confirmatory_bps; "
                    "T=distinct days; frozen moments m_r=(1/T)sum(x-mean)^r "
                    "with g3=m3/m2^1.5 and RAW g4=m4/m2^2; SR=mean/sample "
                    "SD (T-1); sharpe_std=sqrt((1-g3*SR+(g4-1)/4*SR^2)/"
                    "(T-1)); z=SR/sharpe_std; dsr_probability=Phi(z) via "
                    "math.erf; distinguishable_from_luck disclosed; N=1 so "
                    "the benchmark is exactly 0; gated by "
                    "trial_family_status; pass requires >= 0.95; missing "
                    "data, zero variance, T<2, non-positive variance "
                    "bracket or an invalid family FAIL closed "
                    "(§10.8 L675-689)"
                ),
                "overall_verdict": (
                    "INSUFFICIENT_DATA is reserved for unmet evidence "
                    "floors (§10.9 L703); floors met but any AND failing "
                    "gives NOT_CERTIFIED with a failed_ands tuple naming "
                    "which ANDs failed — not a claim of negativity or "
                    "futility (§10.9 L704); the required keyword terminal "
                    "gates certification: terminal=False can never yield "
                    "ELIGIBLE_FOR_HUMAN_REVIEW and returns "
                    "INTERIM_NO_CERTIFICATION with the ANDs still reported "
                    "for data-quality purposes (§10.9 L707); PASS (all four "
                    "ANDs, terminal) is named ELIGIBLE_FOR_HUMAN_REVIEW — "
                    "never automatic promotion (§10.8 L691)"
                ),
                "terminal_rule": (
                    "due at the 100th distinct confirmatory trading day or "
                    "24 calendar months after evidence_start_at (same "
                    "day-of-month clamped at month end, America/New_York), "
                    "whichever comes first; never extended (§10.9 L696-705)"
                ),
                "futility_rule": (
                    "input validation and cohort isolation identical to "
                    "evaluate_cohort (shared helpers): invalid inputs give "
                    "BLOCKED_INVALID_INPUT, version/digest mismatch "
                    "raises, distinct days above the 100 budget raise; any "
                    "unresolved exit gap gives BLOCKED_EXIT_GAP with the "
                    "gap count in machine_inputs — never FUTILE or ALIVE; "
                    "the machine digest binds a canonical record manifest "
                    "(every field, deterministically sorted, one "
                    "comparable string-tuple sort key) plus "
                    "algorithm_version, config_digest, first/last trade "
                    "day and the explicit tz-aware as_of instant; EVERY "
                    "quantity bound into the digest is order-independent "
                    "(day means and the measured dispersion computed in "
                    "exact rationals, converted to float at the end); the "
                    "checkpoint label is set only when distinct_days "
                    "EQUALS 20/40/60/80; the SE is the EXACT rational CR1 "
                    "(same basis as AND #1, §10.9 L716) so float residue "
                    "can never masquerade as a tight bound — degenerate "
                    "exact variance fails closed; "
                    "mu_net=mean net_confirmatory_bps; SE_net=same-basis "
                    "day-clustered SE; U=mu+2.0*SE; required=max(0,-mu); "
                    "MDE=(z0.95+z0.80)*20.0/sqrt(D); a degenerate estimate "
                    "(no naive_mean, or clustered SE undefined or <= 0) "
                    "fails closed to INSUFFICIENT_DATA with "
                    "fail_closed_reason recorded — no zero substitution, "
                    "abandonment never reachable; floors unmet→"
                    "INSUFFICIENT_DATA; U>=0→ALIVE; U<0 and MDE<=required→"
                    "FUTILE; otherwise INSUFFICIENT_DATA; measured day "
                    "dispersion disclosed and above 20 bps also reports MDE "
                    "at the measured dispersion with "
                    "requires_measured_dispersion_ratification, which may "
                    "only block abandonment; checkpoints at 20/40/60/80 "
                    "traded days; INSUFFICIENT_DATA, BUDGET_EXHAUSTED and "
                    "FUTILE stay distinct (§10.9 L707-742)"
                ),
            },
        },
    }


def config_digest(
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> str:
    """P1a draft digest: SHA-256 over the canonical payload JSON.

    This is §10's ``config_version`` as implemented in P1a.  P2/P3 MUST
    extend the payload before REGISTRATION.
    """
    canonical = json.dumps(
        config_payload(config),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def first_passage_driftless_baseline(
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> Decimal:
    """Driftless P(target first) = stop/(stop+target) = 0.45/1.25 = 0.36."""
    stop = config.first_passage_stop_pct
    target = config.first_passage_target_pct
    return stop / (stop + target)
