"""Confirmatory evaluation layer (PREREGISTRATION §10.8)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.domain.guidance_continuation.config import (
    ALGORITHM_VERSION,
    DEFAULT_GUIDANCE_CONFIG,
    config_digest,
)
from app.domain.guidance_continuation.evaluation import (
    AND_FAIL,
    TRIAL_FAMILY_UNKNOWN,
    AND_NOT_EVALUATED,
    AND_PASS,
    BLOCKED_EXIT_GAP,
    BLOCKED_INVALID_INPUT,
    INSUFFICIENT_DATA,
    INTERIM_NO_CERTIFICATION,
    INVALIDATED,
    NOT_CERTIFIED,
    PRICE_BRACKET_TRIGGERS,
    VALID_SINGLE_CONFIRMATORY,
    TradeRecord,
    _record_problems,
    clopper_pearson_lower,
    evaluate_cohort,
    terminal_due,
)

_ET = ZoneInfo("America/New_York")
_DIGEST = config_digest()


def _record(
    day: date,
    *,
    gross: float | None = None,
    net_base: float | None = None,
    net_conf: float | None = None,
    trigger: str = "PROFIT_TARGET",
    resolved: bool = True,
    version: str = ALGORITHM_VERSION,
    digest: str = _DIGEST,
    entry_notional: str = "10000",
    exit_notional: str | None = None,
) -> TradeRecord:
    """Build a CONSISTENT TradeRecord via costs.py (V4 contract).

    Either pass ``net_conf`` (the net confirmatory bps; the exit notional
    is then derived so the cost columns check out exactly) or pass
    ``exit_notional``/``gross`` explicitly.  ``net_base`` may be
    overridden to an inconsistent value by tests that WANT an invalid
    record.
    """
    from app.domain.guidance_continuation.costs import cost_columns

    entry = Decimal(entry_notional)
    if net_conf is not None:
        # net_conf is authoritative: derive the exit notional so the cost
        # columns check out exactly (V4-consistent helper).
        _, confirmatory_usd = cost_columns(
            entry_notional=entry, exit_notional=Decimal("10000")
        )
        gross_bps = net_conf + float(confirmatory_usd) / float(entry) * 10000
        exit_dec = (
            entry * (Decimal(1) + Decimal(repr(gross_bps)) / Decimal(10000))
        ).quantize(Decimal("0.01"))
        b2, c2 = cost_columns(entry_notional=entry, exit_notional=exit_dec)
        g2 = float((exit_dec - entry) / entry * Decimal(10000))
        final_conf = g2 - float(c2) / float(entry) * 10000
        final_base = g2 - float(b2) / float(entry) * 10000
        return TradeRecord(
            trade_day=day,
            entry_notional=entry,
            exit_notional=exit_dec,
            gross_return_bps=g2,
            net_baseline_bps=net_base if net_base is not None else final_base,
            net_confirmatory_bps=final_conf,
            exit_trigger=trigger,
            algorithm_version=version,
            config_digest=digest,
            resolved=resolved,
        )
    if exit_notional is not None:
        exit_dec = Decimal(exit_notional)
        b, c = cost_columns(entry_notional=entry, exit_notional=exit_dec)
        gross_bps = float((exit_dec - entry) / entry * Decimal(10000))
        return TradeRecord(
            trade_day=day,
            entry_notional=entry,
            exit_notional=exit_dec,
            gross_return_bps=gross if gross is not None else gross_bps,
            net_baseline_bps=net_base
            if net_base is not None
            else gross_bps - float(b) / float(entry) * 10000,
            net_confirmatory_bps=net_conf
            if net_conf is not None
            else gross_bps - float(c) / float(entry) * 10000,
            exit_trigger=trigger,
            algorithm_version=version,
            config_digest=digest,
            resolved=resolved,
        )
    # Neither given: default notional pair.
    exit_dec = Decimal("10020")
    b, c = cost_columns(entry_notional=entry, exit_notional=exit_dec)
    gross_bps = float((exit_dec - entry) / entry * Decimal(10000))
    return TradeRecord(
        trade_day=day,
        entry_notional=entry,
        exit_notional=exit_dec,
        gross_return_bps=gross if gross is not None else gross_bps,
        net_baseline_bps=net_base
        if net_base is not None
        else gross_bps - float(b) / float(entry) * 10000,
        net_confirmatory_bps=net_conf
        if net_conf is not None
        else gross_bps - float(c) / float(entry) * 10000,
        exit_trigger=trigger,
        algorithm_version=version,
        config_digest=digest,
        resolved=resolved,
    )


def _days(n: int, start: date = date(2026, 9, 1)) -> list[date]:
    """n distinct weekdays starting at ``start``."""
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _cohort_records(
    days: int,
    *,
    per_day: int = 3,
    targets_per_day: int = 2,
    stops_per_day: int = 1,
    net: float = 12.0,
) -> tuple[TradeRecord, ...]:
    """A synthetic cohort: ``per_day`` trades/day, mixed triggers."""
    records: list[TradeRecord] = []
    for day in _days(days):
        for i in range(per_day):
            trigger = (
                "PROFIT_TARGET"
                if i < targets_per_day
                else ("PRICE_STOP" if i < targets_per_day + stops_per_day else "MAX_HOLD")
            )
            records.append(_record(day, gross=20.0, net_conf=net, trigger=trigger))
    return tuple(records)


def _m1_cohort(stops_none: bool) -> tuple[TradeRecord, ...]:
    """The M1 repro cohort: 100 days, 600 trades, 180 targets / 420 stops.

    All other ANDs pass; the first-passage AND alone decides.  With
    correct labels the CP lower is 180/600 = 0.2692 < 0.36 → FAIL.
    """
    records: list[TradeRecord] = []
    for j, day in enumerate(_days(100)):
        net = 8.0 + ((j * 37) % 23)
        for i in range(6):
            if (j * 6 + i) < 180:
                trigger: str | None = "PROFIT_TARGET"
            else:
                # 420 PRICE_STOP records; the defect variant sets None.
                trigger = None if stops_none else "PRICE_STOP"
            records.append(_record(day, net_conf=net, trigger=trigger))  # type: ignore[arg-type]
    return tuple(records)


class TestM1ResolvedTriggerRequired:
    """M1: a RESOLVED record must carry one of the four §10.6 triggers.

    ``exit_trigger=None`` is legal ONLY on an unresolved exit-gap record.
    A resolved record without a trigger was previously silently counted
    as a TIME exit and dropped out of the first-passage denominator,
    certifying a cohort whose correct labels give NOT_CERTIFIED.
    """

    def test_m1_correct_labels_not_certified(self) -> None:
        # Baseline: 180 targets / 600 brackets → CP lower ≈ 0.2692 < 0.36.
        e = evaluate_cohort(
            _m1_cohort(stops_none=False),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == NOT_CERTIFIED
        assert e.failed_ands == ("first_passage",)
        assert e.first_passage["n_price_brackets"] == 600
        assert e.first_passage["lower_bound"] == pytest.approx(
            0.26917481118602754, abs=1e-9
        )

    def test_m1_none_trigger_on_resolved_is_invalid(self) -> None:
        # Setting the 420 stops' trigger to None must be INVALID input,
        # never a silent denominator shrink: BLOCKED_INVALID_INPUT with
        # every None record counted.
        cohort = _m1_cohort(stops_none=True)
        e = evaluate_cohort(
            cohort,
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == BLOCKED_INVALID_INPUT
        assert e.invalid_input_records == 420
        # The None records never reach the first-passage denominator.
        assert e.first_passage == {}

    def test_m1_problem_message_names_exit_trigger(self) -> None:
        bad = replace(_m1_cohort(stops_none=False)[0], exit_trigger=None)
        problem = _record_problems(bad)
        assert problem is not None
        assert "exit_trigger" in problem

    def test_m1_single_resolved_none_trigger_blocks_whole_cohort(self) -> None:
        # Even ONE resolved record with exit_trigger=None poisons the
        # cohort (1 invalid record), while a gap record keeps None legal.
        cohort = list(_m1_cohort(stops_none=False))
        cohort[5] = replace(cohort[5], exit_trigger=None)  # type: ignore[arg-type]
        e = evaluate_cohort(
            tuple(cohort),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == BLOCKED_INVALID_INPUT
        assert e.invalid_input_records == 1


class TestM2DecimalRecomputation:
    """M2: the V4 recomputation must be all-Decimal and finite-checked.

    ``Decimal("1e400")`` is a finite positive Decimal whose ``float()``
    is ``inf``: the old float recomputation made gross = inf and the
    expected nets inf − inf = NaN, so ``abs(x − NaN) > tol`` was False
    and the record was accepted.  The same path certified forged
    positive columns.  ``Decimal("1e-400")`` is positive but its float
    is 0, crashing the division (and producing extreme bps otherwise).
    """

    def _strong(self) -> list[TradeRecord]:
        return [
            _record(day, net_conf=8.0 + ((j * 37) % 23))
            for j, day in enumerate(_days(65))
            for _ in range(3)
        ]

    def test_m2_exit_notional_1e400_invalid(self) -> None:
        # The reviewer repro: a consistent positive cohort whose one
        # record carries exit_notional = Decimal("1e400") with the
        # donor's (finite, consistent-for-the-donor) columns must be
        # BLOCKED_INVALID_INPUT — previously it certified.
        cohort = self._strong()
        donor = cohort[0]
        cohort[7] = replace(cohort[7], exit_notional=Decimal("1e400"),
                            gross_return_bps=donor.gross_return_bps,
                            net_baseline_bps=donor.net_baseline_bps,
                            net_confirmatory_bps=donor.net_confirmatory_bps)
        e = evaluate_cohort(
            tuple(cohort),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == BLOCKED_INVALID_INPUT
        assert e.invalid_input_records == 1

    def test_m2_entry_notional_1e_minus_400_invalid(self) -> None:
        # Positive but underflowing entry notional: the recomputation
        # must reject it as invalid input naming entry_notional —
        # previously ZeroDivisionError (or, for less extreme values,
        # fabricated extreme bps columns were accepted).
        cohort = self._strong()
        cohort[3] = replace(cohort[3], entry_notional=Decimal("1e-400"))
        e = evaluate_cohort(
            tuple(cohort),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == BLOCKED_INVALID_INPUT
        assert e.invalid_input_records == 1
        problem = _record_problems(cohort[3])
        assert problem is not None
        assert "entry_notional" in problem

    def test_m2_forged_positive_columns_invalid(self) -> None:
        # Forged positive variant: the record keeps its own consistent
        # notionals but its exit notional is blown up to 1e400 while the
        # columns come from a normal-profit record — the cross-check must
        # reject it, not certify.
        cohort = self._strong()
        donor = cohort[0]
        target = cohort[7]
        assert target.exit_notional is not None
        # Build the forged record with columns copied from the donor.
        forged = replace(
            target,
            exit_notional=Decimal("1e400"),
            gross_return_bps=donor.gross_return_bps,
            net_baseline_bps=donor.net_baseline_bps,
            net_confirmatory_bps=donor.net_confirmatory_bps,
        )
        assert _record_problems(forged) is not None

    def test_m2_realistic_consistent_records_still_accepted(self) -> None:
        # Guard against over-rejection: a realistic fully consistent
        # cohort (helper derives columns via costs.py from the
        # notionals) still validates clean.
        e = evaluate_cohort(
            tuple(self._strong()),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == "ELIGIBLE_FOR_HUMAN_REVIEW"
        assert e.invalid_input_records == 0


class TestH1GrossCrossCheck:
    """H1: gross_return_bps must match (exit−entry)/entry×1e4 too.

    The V4 cross-check previously verified only the two net columns; a
    shifted gross rode the frozen label into AND #1's gross CI
    disclosure.  Nets consistent with the notionals, gross +5 bps →
    invalid, naming gross_return_bps.
    """

    def _strong(self) -> list[TradeRecord]:
        return [
            _record(day, net_conf=8.0 + ((j * 37) % 23))
            for j, day in enumerate(_days(65))
            for _ in range(3)
        ]

    def test_h1_gross_shifted_by_5bps_invalid(self) -> None:
        cohort = self._strong()
        cohort[7] = replace(cohort[7], gross_return_bps=cohort[7].gross_return_bps + 5.0)  # type: ignore[operator]
        e = evaluate_cohort(
            tuple(cohort),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == BLOCKED_INVALID_INPUT
        assert e.invalid_input_records == 1
        problem = _record_problems(cohort[7])
        assert problem is not None
        assert "gross_return_bps" in problem

    def test_h1_consistent_gross_accepted(self) -> None:
        e = evaluate_cohort(
            tuple(self._strong()),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == "ELIGIBLE_FOR_HUMAN_REVIEW"
        assert e.invalid_input_records == 0


class TestCohortIsolation:
    def test_mixed_algorithm_version_fails_closed(self) -> None:
        records = _cohort_records(25)
        bad = _record(_days(1, date(2026, 12, 1))[0], version="other-v1")
        with pytest.raises(ValueError) as excinfo:
            evaluate_cohort(
                records + (bad,),
                algorithm_version=ALGORITHM_VERSION,
                config_digest=_DIGEST,
                terminal=True,
                trial_family_status=VALID_SINGLE_CONFIRMATORY,
            )
        assert "algorithm_version" in str(excinfo.value)

    def test_mixed_config_digest_fails_closed(self) -> None:
        records = _cohort_records(25)
        # INJECT exactly one bad record: the evaluated digest matches the
        # good records, so ONLY the trailing bad record triggers the
        # failure — and the message must name the mismatch.
        bad = _record(_days(1, date(2026, 12, 1))[0], digest="0" * 64)
        with pytest.raises(ValueError) as excinfo:
            evaluate_cohort(
                records + (bad,),
                algorithm_version=ALGORITHM_VERSION,
                config_digest=_DIGEST,
                terminal=True,
                trial_family_status=VALID_SINGLE_CONFIRMATORY,
            )
        assert "config_digest" in str(excinfo.value)
        assert repr("0" * 64) in str(excinfo.value)

    def test_exit_gap_blocks_certification(self) -> None:
        # Strong returns everywhere, but ONE unresolved exit: the verdict is
        # BLOCKED_EXIT_GAP — never a PASS computed over the rest (§10.6
        # L599-600, §10.8 L656).
        records = list(_cohort_records(25, net=25.0))
        records[7] = _record(
            records[7].trade_day, net_conf=25.0, resolved=False, trigger="MAX_HOLD"
        )
        result = evaluate_cohort(
            tuple(records), algorithm_version=ALGORITHM_VERSION, config_digest=_DIGEST, terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert result.verdict == BLOCKED_EXIT_GAP
        assert result.exit_gap_records == 1

    def test_exit_gap_verdict_distinct_from_insufficient(self) -> None:
        records = list(_cohort_records(5))
        records[0] = _record(records[0].trade_day, resolved=False)
        result = evaluate_cohort(
            tuple(records), algorithm_version=ALGORITHM_VERSION, config_digest=_DIGEST, terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert result.verdict == BLOCKED_EXIT_GAP
        assert result.verdict != INSUFFICIENT_DATA


class TestAnalysisFloors:
    def test_19_days_insufficient(self) -> None:
        result = evaluate_cohort(
            _cohort_records(19),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert result.verdict == INSUFFICIENT_DATA
        assert result.distinct_days == 19

    def test_29_price_brackets_insufficient(self) -> None:
        # ≥ 20 full days, ≥ 30 observations, EXACTLY 29 price brackets
        # (day 1: 2 brackets + 1 MAX_HOLD; days 2-20: 1 bracket + 2
        # MAX_HOLD → 2 + 19 = 21? no: 2 + 19×1 = 21.  Use day1: 5
        # brackets, days 2+: 24 more → 29 total, 60 observations/20 days).
        records: list[TradeRecord] = []
        day_list = _days(20)
        for j, day in enumerate(day_list):
            # Day 0: 3 (all trades are brackets); days 1-7: 2 each
            # (14); days 8-19: 1 each (12).  Total = 3 + 14 + 12 = 29
            # price brackets over 20 days × 3 = 60 observations.
            brackets_today = 3 if j == 0 else (2 if j <= 7 else 1)
            for i in range(3):
                is_bracket = i < brackets_today
                trigger = (
                    "PROFIT_TARGET"
                    if is_bracket and i % 2 == 0
                    else ("PRICE_STOP" if is_bracket else "MAX_HOLD")
                )
                records.append(_record(day, trigger=trigger))
        total_brackets = sum(
            1 for r in records if r.exit_trigger in {"PRICE_STOP", "PROFIT_TARGET"}
        )
        assert total_brackets == 29
        assert len(records) == 60
        assert len({r.trade_day for r in records}) == 20
        result = evaluate_cohort(
            tuple(records), algorithm_version=ALGORITHM_VERSION, config_digest=_DIGEST, terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert result.verdict == INSUFFICIENT_DATA
        assert result.floors["resolved_price_brackets"] == 29

    def test_gross_net_30_observations_20_days_floor(self) -> None:
        # Net observations must each be ≥ 30 over ≥ 20 days: 10 days × 4
        # trades = 40 observations on only 10 distinct days.
        records = [
            _record(day, trigger="PROFIT_TARGET")
            for day in _days(10)
            for _ in range(4)
        ]
        result = evaluate_cohort(
            tuple(records), algorithm_version=ALGORITHM_VERSION, config_digest=_DIGEST, terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert result.verdict == INSUFFICIENT_DATA
        assert result.distinct_days == 10


class TestFirstPassage:
    def test_time_exits_excluded_from_denominator(self) -> None:
        result = evaluate_cohort(
            _cohort_records(25, per_day=4, targets_per_day=3, stops_per_day=1),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        fp = result.first_passage
        assert fp["n_price_brackets"] == 25 * 4  # 3 targets + 1 stop per day
        assert fp["time_exit_count"] == 0

    def test_extreme_classification_sensitivity_disclosed(self) -> None:
        result = evaluate_cohort(
            _cohort_records(25, per_day=4, targets_per_day=2, stops_per_day=1),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        fp = result.first_passage
        # 1 MAX_HOLD per day: all-as-target must raise the share, all-as-stop
        # must lower it.
        assert fp["time_exit_count"] == 25
        assert fp["lower_if_time_all_target"] >= fp["lower_bound"]
        assert fp["lower_if_time_all_stop"] <= fp["lower_bound"]

    def test_clopper_pearson_known_value(self) -> None:
        # Independent reference: exact rational bisection on the binomial
        # tail P(X >= k | p) = 0.05 with fractions.Fraction (see report):
        #   k=80,  n=180 → 0.381782268499
        #   k=110, n=180 → 0.547510000338
        #   k=70,  n=180 → 0.328061358355
        assert clopper_pearson_lower(80, 180) == pytest.approx(0.381782268499, abs=1e-9)
        assert clopper_pearson_lower(110, 180) == pytest.approx(0.547510000338, abs=1e-9)
        assert clopper_pearson_lower(70, 180) == pytest.approx(0.328061358355, abs=1e-9)
        # Boundaries.
        assert clopper_pearson_lower(0, 180) == 0.0
        assert clopper_pearson_lower(180, 180) == pytest.approx(0.983494771804, abs=1e-9)

    def test_first_passage_pass_needs_bound_above_036(self) -> None:
        # 65/180 targets → CP lower 0.3015 < 0.36 → AND #2 fails.
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(60)):
            for i in range(3):
                trigger = (
                    "PROFIT_TARGET" if (j * 3 + i) < 65 else "PRICE_STOP"
                )
                records.append(_record(day, trigger=trigger))
        result = evaluate_cohort(
            tuple(records), algorithm_version=ALGORITHM_VERSION, config_digest=_DIGEST, terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert not result.and_first_passage_pass
        assert result.first_passage["lower_bound"] == pytest.approx(
            0.301501644909, abs=1e-9
        )


class TestSampleSizeGate:
    def test_59_days_fails_and3(self) -> None:
        records: list[TradeRecord] = []
        for day in _days(59):
            for i in range(4):
                trigger = "PROFIT_TARGET" if i < 3 else "PRICE_STOP"
                records.append(_record(day, trigger=trigger))
        result = evaluate_cohort(
            tuple(records), algorithm_version=ALGORITHM_VERSION, config_digest=_DIGEST, terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert result.distinct_days == 59
        assert not result.and_sample_size_pass

    def test_179_brackets_fails_and3(self) -> None:
        # 60 days but only 179 price brackets.
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(60)):
            for i in range(3):
                trigger = "PROFIT_TARGET" if (j * 3 + i) != 0 else "PRICE_STOP"
                records.append(_record(day, trigger=trigger))
        records = records[:179]
        result = evaluate_cohort(
            tuple(records), algorithm_version=ALGORITHM_VERSION, config_digest=_DIGEST, terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert result.resolved_price_brackets == 179
        assert not result.and_sample_size_pass


class TestDayLevelDSR:
    def test_dsr_fails_closed_on_zero_variance(self) -> None:
        # Every day has the SAME mean net → zero variance → AND #4 false.
        records = _cohort_records(30, net=12.0)
        result = evaluate_cohort(
            records, algorithm_version=ALGORITHM_VERSION, config_digest=_DIGEST, terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert not result.and_dsr_pass
        assert result.dsr["dsr_probability"] is None

    def test_dsr_n1_benchmark_zero_pass(self) -> None:
        # Varied positive daily means → a real Sharpe, N=1 benchmark 0.
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(70)):
            net = 8.0 + ((j * 37) % 23)  # deterministic, varied, positive
            for i in range(3):
                trigger = "PROFIT_TARGET" if i < 2 else "PRICE_STOP"
                records.append(_record(day, net_conf=net, trigger=trigger))
        result = evaluate_cohort(
            tuple(records), algorithm_version=ALGORITHM_VERSION, config_digest=_DIGEST, terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert result.and_dsr_pass
        assert result.dsr["dsr_probability"] is not None
        assert result.dsr["dsr_probability"] >= 0.95

    def test_dsr_parity_with_platform_n_trials_1(self) -> None:
        # Parity with app.platform.overfitting.deflated_sharpe_ratio at
        # n_trials=1 (benchmark exactly 0) for several series shapes.
        from app.platform.overfitting import deflated_sharpe_ratio

        series = [
            [8.0 + ((j * 37) % 23) for j in range(70)],
            [12.0] * 30,                      # zero variance
            [((j % 5) - 2) * 6.0 for j in range(40)],  # symmetric, mean 0
            [1.0 if j % 3 == 0 else -0.5 for j in range(50)],
        ]
        for values in series:
            m = sum(values) / len(values)
            var = sum((v - m) ** 2 for v in values) / (len(values) - 1)
            if var <= 0:
                continue
            sd = var**0.5
            g3 = sum(((v - m) / sd) ** 3 for v in values) / len(values)
            g4 = sum(((v - m) / sd) ** 4 for v in values) / len(values)
            sr = m / sd
            import math

            sharpe_std = math.sqrt(
                (1 - g3 * sr + (g4 - 1) / 4 * sr**2) / (len(values) - 1)
            )
            z = sr / sharpe_std
            expected = 0.5 * (1 + math.erf(z / math.sqrt(2)))
            got = deflated_sharpe_ratio(
                observed_sharpe=sr,
                n_trials=1,
                sample_size=len(values),
                skewness=g3,
                kurtosis=g4,
            )
            assert expected == pytest.approx(got["dsr_probability"], abs=1e-12)

    def test_dsr_negative_mean_fails(self) -> None:
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(70)):
            net = -8.0 + ((j * 13) % 7)
            for i in range(3):
                trigger = "PROFIT_TARGET" if i < 2 else "PRICE_STOP"
                records.append(_record(day, net_conf=net, trigger=trigger))
        result = evaluate_cohort(
            tuple(records), algorithm_version=ALGORITHM_VERSION, config_digest=_DIGEST, terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert not result.and_dsr_pass


class TestOverallVerdict:
    def test_all_four_ands_pass_is_eligible_for_human_review(self) -> None:
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(65)):
            net = 8.0 + ((j * 37) % 23)
            for i in range(3):
                trigger = "PROFIT_TARGET" if (j * 3 + i) < 180 else "PRICE_STOP"
                records.append(_record(day, net_conf=net, trigger=trigger))
        result = evaluate_cohort(
            tuple(records),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert result.verdict == "ELIGIBLE_FOR_HUMAN_REVIEW"
        assert result.failed_ands == ()

    def test_verdict_never_says_promoted(self) -> None:
        records = _cohort_records(65, per_day=4, targets_per_day=3)
        result = evaluate_cohort(
            records,
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert "PROMOT" not in result.verdict.upper()


class TestD1VerdictSemantics:
    """D1: INSUFFICIENT_DATA is reserved for unmet evidence floors
    (§10.9 L703); floors met but an AND failing is NOT_CERTIFIED."""

    def _negative_cohort(self) -> tuple[TradeRecord, ...]:
        # The reviewer repro: 70 days, 140 brackets, net ≈ −30 bps,
        # alternating triggers, floors met, net CI lower ≈ −29.9.
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(70)):
            for i in range(2):
                net = -30.0 + ((i + j) % 3) * 0.1
                trigger = (
                    "PROFIT_TARGET" if (j + i) % 2 == 0 else "PRICE_STOP"
                )
                records.append(_record(day, net_conf=net, trigger=trigger))
        return tuple(records)

    def test_d1_failed_cohort_is_not_certified_not_insufficient(self) -> None:
        result = evaluate_cohort(
            self._negative_cohort(),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert result.verdict == NOT_CERTIFIED
        assert result.verdict != INSUFFICIENT_DATA
        assert "net" in result.failed_ands

    def test_d1_failed_ands_tuple_reports_each_failure(self) -> None:
        result = evaluate_cohort(
            self._negative_cohort(),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        # Net fails (CI lower < 0); sample size and DSR also fail here.
        assert isinstance(result.failed_ands, tuple)
        assert "net" in result.failed_ands
        # 19 days: floors NOT met → INSUFFICIENT_DATA, no failed_ands claim.
        low = evaluate_cohort(
            _cohort_records(19),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert low.verdict == INSUFFICIENT_DATA
        assert low.failed_ands == ()

    def test_d1_only_sample_size_failing_still_not_certified(self) -> None:
        # Floors met, all statistics pass, but only AND #3 (60/180) fails
        # → NOT_CERTIFIED with failed_ands == ("sample_size",).
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(40)):
            net = 8.0 + ((j * 37) % 23)
            for i in range(3):
                trigger = "PROFIT_TARGET" if i < 2 else "PRICE_STOP"
                records.append(_record(day, net_conf=net, trigger=trigger))
        result = evaluate_cohort(
            tuple(records),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert result.verdict == NOT_CERTIFIED
        assert result.failed_ands == ("sample_size",)

    def test_d1_interim_never_certifies(self) -> None:
        # L707: interim looks allow only data-quality and futility checks.
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(65)):
            net = 8.0 + ((j * 37) % 23)
            for i in range(3):
                trigger = "PROFIT_TARGET" if (j * 3 + i) < 180 else "PRICE_STOP"
                records.append(_record(day, net_conf=net, trigger=trigger))
        interim = evaluate_cohort(
            tuple(records),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=False,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert interim.verdict == INTERIM_NO_CERTIFICATION
        # Interim never computes the ANDs (G5): all strictly False and
        # NOT_EVALUATED — never a truthy read that could look like a pass.
        assert interim.and_net_pass is False
        assert interim.and_first_passage_pass is False
        assert interim.and_sample_size_pass is False
        assert interim.and_dsr_pass is False
        assert set(interim.and_status.values()) == {AND_NOT_EVALUATED}
        assert interim.ands_evaluated is False

    def test_d1_terminal_required_keyword(self) -> None:
        # terminal is a REQUIRED keyword: omitting it must not default to
        # True silently.
        records = _cohort_records(65, per_day=4, targets_per_day=3)
        with pytest.raises(TypeError):
            evaluate_cohort(  # type: ignore[call-arg]
                records, algorithm_version=ALGORITHM_VERSION, config_digest=_DIGEST
            )

    def test_d1_interim_with_failing_ands_is_interim(self) -> None:
        # Interim never certifies and (post-G5) never even computes the
        # ANDs: every AND is NOT_EVALUATED and failed_ands stays empty —
        # the sample-size shortfall is visible in floors, not a failed AND.
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(40)):
            net = 8.0 + ((j * 37) % 23)
            for i in range(3):
                trigger = "PROFIT_TARGET" if i < 2 else "PRICE_STOP"
                records.append(_record(day, net_conf=net, trigger=trigger))
        interim = evaluate_cohort(
            tuple(records),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=False,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert interim.verdict == INTERIM_NO_CERTIFICATION
        assert interim.and_sample_size_pass is False
        assert interim.failed_ands == ()
        assert interim.floors["distinct_days"] == 40


class TestTerminalTiming:
    def test_terminal_at_100th_day(self) -> None:
        days = _days(100)
        due, reason = terminal_due(
            len(days),
            evidence_start_at_et=datetime(2026, 6, 1, 9, 30, tzinfo=_ET),
            as_of=date(2026, 9, 30),
        )
        assert due
        assert reason == "TRADED_DAYS_100"

    def test_terminal_at_99_days_not_due(self) -> None:
        due, reason = terminal_due(
            99,
            evidence_start_at_et=datetime(2026, 6, 1, 9, 30, tzinfo=_ET),
            as_of=date(2026, 9, 30),
        )
        assert not due

    def test_terminal_at_24_months(self) -> None:
        # Start 2026-01-31 → +24 months = 2028-01-31 (clamped from the
        # 31st in Feb-free months; 2028-01 has 31 days).
        due, reason = terminal_due(
            40,
            evidence_start_at_et=datetime(2026, 1, 31, 9, 30, tzinfo=_ET),
            as_of=date(2028, 1, 31),
        )
        assert due
        assert reason == "CALENDAR_MONTHS_24"

    def test_month_end_clamp(self) -> None:
        # Start 2026-08-31 → 24 months later is 2028-08-31 (Aug has 31
        # days); but start 2026-01-31 → 2028-02-29 does NOT exist as +24m
        # from Jan... rather start 2026-02-29 does not exist.  Use
        # 2026-08-31 vs a hypothetical +24m = 2028-08-31.
        due, _ = terminal_due(
            40,
            evidence_start_at_et=datetime(2026, 8, 31, 9, 30, tzinfo=_ET),
            as_of=date(2028, 8, 31),
        )
        assert due
        # One day before the clamped date is not due.
        due_early, _ = terminal_due(
            40,
            evidence_start_at_et=datetime(2026, 8, 31, 9, 30, tzinfo=_ET),
            as_of=date(2028, 8, 30),
        )
        assert not due_early

    def test_month_end_clamp_leap_day(self) -> None:
        # The genuine clamp: evidence_start 2024-02-29 (leap) + 24 months
        # lands in non-leap 2026-02 → clamped to 2026-02-28.
        due_on_28, _ = terminal_due(
            40,
            evidence_start_at_et=datetime(2024, 2, 29, 9, 30, tzinfo=_ET),
            as_of=date(2026, 2, 28),
        )
        assert due_on_28
        due_before, _ = terminal_due(
            40,
            evidence_start_at_et=datetime(2024, 2, 29, 9, 30, tzinfo=_ET),
            as_of=date(2026, 2, 27),
        )
        assert not due_before

    def test_never_extends_past_due(self) -> None:
        # Once due, a later as_of stays due and the reason is stable.
        d1, r1 = terminal_due(
            100,
            evidence_start_at_et=datetime(2026, 1, 1, 9, 30, tzinfo=_ET),
            as_of=date(2027, 1, 1),
        )
        d2, r2 = terminal_due(
            100,
            evidence_start_at_et=datetime(2026, 1, 1, 9, 30, tzinfo=_ET),
            as_of=date(2027, 6, 1),
        )
        assert d1 and d2
        assert r1 == r2 == "TRADED_DAYS_100"


class TestG1InputValidation:
    """G1: every record's bps fields must be finite, notionals finite and
    positive, trade_day a date.  Invalid records are BLOCKING evidence."""

    def _strong(
        self, n_days: int = 65, gross: float | None = None
    ) -> tuple[TradeRecord, ...]:
        recs = []
        for j, day in enumerate(_days(n_days)):
            for _ in range(3):
                recs.append(
                    _record(day, net_conf=8.0 + ((j * 37) % 23), gross=gross)
                )
        return tuple(recs)

    def test_g1_nan_gross_blocks_certification(self) -> None:
        # NaN must be INJECTED into a built record (the helper derives
        # consistent columns from the notionals by default).
        cohort = list(self._strong())
        bad = cohort[0]
        cohort[0] = TradeRecord(
            trade_day=bad.trade_day,
            entry_notional=bad.entry_notional,
            exit_notional=bad.exit_notional,
            gross_return_bps=float("nan"),
            net_baseline_bps=bad.net_baseline_bps,
            net_confirmatory_bps=bad.net_confirmatory_bps,
            exit_trigger=bad.exit_trigger,
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=True,
        )
        result = evaluate_cohort(
            tuple(cohort),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert result.verdict == BLOCKED_INVALID_INPUT
        assert result.invalid_input_records > 0

    def test_g1_nan_baseline_blocks(self) -> None:
        nan = float("nan")
        recs = []
        for j, day in enumerate(_days(65)):
            for _ in range(3):
                recs.append(
                    _record(day, net_conf=8.0 + ((j * 37) % 23), net_base=nan)
                )
        result = evaluate_cohort(
            tuple(recs),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert result.verdict == BLOCKED_INVALID_INPUT

    def test_g1_infinite_confirmatory_blocks(self) -> None:
        recs = list(self._strong())
        bad = recs[3]
        recs[3] = TradeRecord(
            trade_day=bad.trade_day,
            entry_notional=bad.entry_notional,
            exit_notional=bad.exit_notional,
            gross_return_bps=bad.gross_return_bps,
            net_baseline_bps=bad.net_baseline_bps,
            net_confirmatory_bps=float("inf"),
            exit_trigger=bad.exit_trigger,
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=True,
        )
        result = evaluate_cohort(
            tuple(recs),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert result.verdict == BLOCKED_INVALID_INPUT

    def test_g1_non_positive_notional_blocks(self) -> None:
        recs = list(self._strong())
        bad = recs[5]
        recs[5] = TradeRecord(
            trade_day=bad.trade_day,
            entry_notional=Decimal("0"),
            exit_notional=bad.exit_notional,
            gross_return_bps=bad.gross_return_bps,
            net_baseline_bps=bad.net_baseline_bps,
            net_confirmatory_bps=bad.net_confirmatory_bps,
            exit_trigger=bad.exit_trigger,
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=True,
        )
        result = evaluate_cohort(
            tuple(recs),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert result.verdict == BLOCKED_INVALID_INPUT

    def test_g1_non_date_trade_day_blocks(self) -> None:
        recs = list(self._strong())
        bad = recs[2]
        recs[2] = TradeRecord(
            trade_day="2026-06-01",  # type: ignore[arg-type]
            entry_notional=bad.entry_notional,
            exit_notional=bad.exit_notional,
            gross_return_bps=bad.gross_return_bps,
            net_baseline_bps=bad.net_baseline_bps,
            net_confirmatory_bps=bad.net_confirmatory_bps,
            exit_trigger=bad.exit_trigger,
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=True,
        )
        result = evaluate_cohort(
            tuple(recs),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert result.verdict == BLOCKED_INVALID_INPUT

    def test_g1_valid_cohort_unaffected(self) -> None:
        result = evaluate_cohort(
            self._strong(),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert result.verdict == "ELIGIBLE_FOR_HUMAN_REVIEW"
        assert result.invalid_input_records == 0


class TestG3StableClopperPearson:
    def test_g3_large_n_no_overflow(self) -> None:
        for n in (1100, 5000):
            for k in (0, 1, n // 2, n - 1, n):
                value = clopper_pearson_lower(k, n)
                assert 0.0 <= value <= 1.0

    def test_g3_k_equals_n_is_alpha_pow_1_over_n(self) -> None:
        for n in (180, 1100, 5000):
            assert clopper_pearson_lower(n, n) == pytest.approx(
                0.05 ** (1.0 / n), rel=1e-9
            )

    def test_g3_monotonic_in_k(self) -> None:
        prev = clopper_pearson_lower(0, 200)
        for k in range(1, 201):
            cur = clopper_pearson_lower(k, 200)
            assert cur >= prev - 1e-15
            prev = cur

    def test_g3_fraction_exact_references_still_match(self) -> None:
        # Fraction-exact references (independently computed).
        assert clopper_pearson_lower(80, 180) == pytest.approx(0.381782268499, abs=1e-9)
        assert clopper_pearson_lower(110, 180) == pytest.approx(0.547510000338, abs=1e-9)
        assert clopper_pearson_lower(70, 180) == pytest.approx(0.328061358355, abs=1e-9)

    def test_g3_large_n_independent_reference(self) -> None:
        # Independent references computed with NO floating point: 64-step
        # bisection over dyadic rationals on the exact Fraction binomial
        # tail P(X >= k | n, p) = sum comb(n, j) p^j (1-p)^(n-j), solving
        # tail = 1/20 (bracket width 2^-64). Same method as
        # _fraction_cp_lower below, run offline because n=1100 is slow.
        assert clopper_pearson_lower(550, 1100) == pytest.approx(
            0.47477011107626205, abs=1e-12
        )
        assert clopper_pearson_lower(1, 1100) == pytest.approx(
            4.6629180451015e-05, rel=1e-9
        )


def _fraction_cp_lower(k: int, n: int, iterations: int = 120) -> float:
    """Exact-Fraction CP lower bound (slow; test-only reference)."""
    from fractions import Fraction
    from math import comb

    def tail(k: int, n: int, p: "Fraction") -> "Fraction":
        total: Fraction = Fraction(0)
        for i in range(k, n + 1):
            total += Fraction(comb(n, i)) * p**i * (1 - p) ** (n - i)
        return total

    if k == 0:
        return 0.0
    lo, hi = Fraction(0), Fraction(1)
    for _ in range(iterations):
        mid = (lo + hi) / 2
        if tail(k, n, mid) > Fraction(5, 100):
            hi = mid
        else:
            lo = mid
    return float(lo)


class TestG4Moments:
    def test_g4_two_point_series_is_exactly_one(self) -> None:
        # 60 day-means alternating [19, 21] × 30: with the frozen
        # estimator m_r = (1/T)Σ(x−x̄)^r, g4 = m4/m2² = 1 EXACTLY.
        from app.domain.guidance_continuation.evaluation import _dsr_stats

        means = [19.0, 21.0] * 30
        stats = _dsr_stats(means)
        assert stats["kurtosis_g4"] == pytest.approx(1.0, abs=1e-12)
        assert stats["skewness_g3"] == pytest.approx(0.0, abs=1e-12)
        # The bracket must be positive → DSR computed, not fail-closed.
        assert stats["dsr_probability"] is not None

    def test_g4_parity_with_platform_production_path(self) -> None:
        # MUST call the production path (_dsr_stats) and compare with the
        # platform DSR at n_trials=1 for three series shapes.
        from app.platform.overfitting import deflated_sharpe_ratio
        from app.domain.guidance_continuation.evaluation import _dsr_stats

        series = [
            [8.0 + ((j * 37) % 23) for j in range(70)],      # asymmetric
            [19.0, 21.0] * 30,                                # two-point
            [1.0 + 0.28 * j for j in range(60)],              # near 0.95
        ]
        for values in series:
            stats = _dsr_stats(values)
            assert stats["dsr_probability"] is not None
            got = deflated_sharpe_ratio(
                observed_sharpe=stats["sharpe"],
                n_trials=1,
                sample_size=len(values),
                skewness=stats["skewness_g3"],
                kurtosis=stats["kurtosis_g4"],
            )
            assert stats["dsr_probability"] == pytest.approx(
                got["dsr_probability"], abs=1e-12
            )

    def test_g4_dsr_report_discloses_distinguishable(self) -> None:
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(65)):
            net = 8.0 + ((j * 37) % 23)
            for i in range(3):
                trigger = "PROFIT_TARGET" if (j * 3 + i) < 180 else "PRICE_STOP"
                records.append(_record(day, net_conf=net, trigger=trigger))
        result = evaluate_cohort(
            tuple(records),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert "distinguishable_from_luck" in result.dsr
        assert result.dsr["distinguishable_from_luck"] is True


class TestG5InterimAndTrialFamily:
    def _strong(self) -> tuple[TradeRecord, ...]:
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(65)):
            net = 8.0 + ((j * 37) % 23)
            for i in range(3):
                trigger = "PROFIT_TARGET" if (j * 3 + i) < 180 else "PRICE_STOP"
                records.append(_record(day, net_conf=net, trigger=trigger))
        return tuple(records)

    def test_g5_interim_does_not_evaluate_ands(self) -> None:
        interim = evaluate_cohort(
            self._strong(),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=False,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert interim.verdict == INTERIM_NO_CERTIFICATION
        # Every AND strictly False and marked NOT_EVALUATED; reports empty.
        assert interim.and_net_pass is False
        assert interim.and_first_passage_pass is False
        assert interim.and_sample_size_pass is False
        assert interim.and_dsr_pass is False
        assert set(interim.and_status.values()) == {AND_NOT_EVALUATED}
        assert interim.ands_evaluated is False
        assert interim.net_ci == {}
        assert interim.first_passage == {}
        assert interim.dsr == {}
        # Quality info still present.
        assert interim.distinct_days == 65
        assert interim.floors["floors_met"] is True

    def test_g5_invalidated_family_fails_and4(self) -> None:
        result = evaluate_cohort(
            self._strong(),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=INVALIDATED,
        )
        assert result.verdict == NOT_CERTIFIED
        assert "dsr" in result.failed_ands
        assert result.dsr["fail_closed_reason"]

    def test_g5_unknown_family_fails_and4(self) -> None:
        result = evaluate_cohort(
            self._strong(),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=TRIAL_FAMILY_UNKNOWN,
        )
        assert result.verdict == NOT_CERTIFIED
        assert "dsr" in result.failed_ands

    def test_g5_valid_family_allows_and4(self) -> None:
        result = evaluate_cohort(
            self._strong(),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert result.verdict == "ELIGIBLE_FOR_HUMAN_REVIEW"

    def test_g5_trial_family_status_required(self) -> None:
        with pytest.raises(TypeError):
            evaluate_cohort(  # type: ignore[call-arg]
                self._strong(),
                algorithm_version=ALGORITHM_VERSION,
                config_digest=_DIGEST,
                terminal=True,
            )


class TestG6DayBudget:
    def test_g6_101_days_raises(self) -> None:
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(101)):
            for _ in range(2):
                records.append(_record(day, net_conf=8.0 + ((j * 37) % 23)))
        with pytest.raises(ValueError) as excinfo:
            evaluate_cohort(
                tuple(records),
                algorithm_version=ALGORITHM_VERSION,
                config_digest=_DIGEST,
                terminal=True,
                trial_family_status=VALID_SINGLE_CONFIRMATORY,
            )
        assert "100" in str(excinfo.value) or "budget" in str(excinfo.value)

    def test_g6_100_days_accepted(self) -> None:
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(100)):
            for i in range(2):
                trigger = "PROFIT_TARGET" if (j * 2 + i) < 150 else "PRICE_STOP"
                records.append(_record(day, net_conf=8.0 + ((j * 37) % 23), trigger=trigger))
        result = evaluate_cohort(
            tuple(records),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert result.distinct_days == 100


class TestAndStatusContract:
    """The four and_*_pass fields are STRICTLY bool: True only when that
    AND was actually evaluated and passed.  Non-evaluated paths give
    False, and the reason lives in the separate and_status mapping."""

    def _strong(self) -> tuple[TradeRecord, ...]:
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(65)):
            net = 8.0 + ((j * 37) % 23)
            for i in range(3):
                trigger = "PROFIT_TARGET" if (j * 3 + i) < 180 else "PRICE_STOP"
                records.append(_record(day, net_conf=net, trigger=trigger))
        return tuple(records)

    def test_interim_all_false_not_evaluated(self) -> None:
        e = evaluate_cohort(
            self._strong(),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=False,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == INTERIM_NO_CERTIFICATION
        assert e.and_net_pass is False
        assert e.and_first_passage_pass is False
        assert e.and_sample_size_pass is False
        assert e.and_dsr_pass is False
        assert set(e.and_status.values()) == {AND_NOT_EVALUATED}
        assert e.ands_evaluated is False

    def test_interim_booleans_not_truthy(self) -> None:
        # The original defect: bool(and_net_pass) was True on interim.
        e = evaluate_cohort(
            self._strong(),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=False,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert not any(
            (
                e.and_net_pass,
                e.and_first_passage_pass,
                e.and_sample_size_pass,
                e.and_dsr_pass,
            )
        )

    def test_exit_gap_all_false_not_evaluated(self) -> None:
        records = list(self._strong())
        bad = records[7]
        records[7] = TradeRecord(
            trade_day=bad.trade_day,
            entry_notional=bad.entry_notional,
            exit_notional=bad.exit_notional,
            gross_return_bps=bad.gross_return_bps,
            net_baseline_bps=bad.net_baseline_bps,
            net_confirmatory_bps=bad.net_confirmatory_bps,
            exit_trigger="MAX_HOLD",
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=False,
        )
        e = evaluate_cohort(
            tuple(records),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == BLOCKED_EXIT_GAP
        assert e.and_net_pass is False
        assert e.and_dsr_pass is False
        assert set(e.and_status.values()) == {AND_NOT_EVALUATED}
        assert e.ands_evaluated is False

    def test_invalid_input_all_false_not_evaluated(self) -> None:
        records = list(self._strong())
        bad = records[3]
        records[3] = TradeRecord(
            trade_day=bad.trade_day,
            entry_notional=bad.entry_notional,
            exit_notional=bad.exit_notional,
            gross_return_bps=float("nan"),
            net_baseline_bps=bad.net_baseline_bps,
            net_confirmatory_bps=bad.net_confirmatory_bps,
            exit_trigger=bad.exit_trigger,
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=True,
        )
        e = evaluate_cohort(
            tuple(records),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == BLOCKED_INVALID_INPUT
        assert all(
            x is False
            for x in (
                e.and_net_pass,
                e.and_first_passage_pass,
                e.and_sample_size_pass,
                e.and_dsr_pass,
            )
        )
        assert set(e.and_status.values()) == {AND_NOT_EVALUATED}
        assert e.ands_evaluated is False

    def test_floors_not_met_all_false_not_evaluated(self) -> None:
        e = evaluate_cohort(
            _cohort_records(19),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == INSUFFICIENT_DATA
        assert e.and_net_pass is False
        assert e.and_sample_size_pass is False
        assert set(e.and_status.values()) == {AND_NOT_EVALUATED}
        assert e.ands_evaluated is False

    def test_eligible_all_true_and_pass(self) -> None:
        e = evaluate_cohort(
            self._strong(),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == "ELIGIBLE_FOR_HUMAN_REVIEW"
        assert e.and_net_pass is True
        assert e.and_first_passage_pass is True
        assert e.and_sample_size_pass is True
        assert e.and_dsr_pass is True
        assert set(e.and_status.values()) == {AND_PASS}
        assert e.ands_evaluated is True

    def test_not_certified_status_agrees_with_booleans(self) -> None:
        # The negative cohort: net/FP/DSR statistics computed, sample
        # size passes at 70 days × 2 = 140 brackets?  Ensure mixed.
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(70)):
            for i in range(2):
                net = -30.0 + ((i + j) % 3) * 0.1
                trigger = (
                    "PROFIT_TARGET" if (j + i) % 2 == 0 else "PRICE_STOP"
                )
                records.append(_record(day, net_conf=net, trigger=trigger))
        e = evaluate_cohort(
            tuple(records),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == NOT_CERTIFIED
        assert e.ands_evaluated is True
        # and_status agrees with the booleans AND with failed_ands.
        assert e.and_status["net"] == (
            AND_PASS if e.and_net_pass else AND_FAIL
        )
        assert e.and_status["first_passage"] == (
            AND_PASS if e.and_first_passage_pass else AND_FAIL
        )
        assert e.and_status["sample_size"] == (
            AND_PASS if e.and_sample_size_pass else AND_FAIL
        )
        assert e.and_status["dsr"] == (
            AND_PASS if e.and_dsr_pass else AND_FAIL
        )
        expected_failed = tuple(
            k for k, v in e.and_status.items() if v == AND_FAIL
        )
        assert set(expected_failed) == set(e.failed_ands)

    def test_and_status_is_immutable(self) -> None:
        from types import MappingProxyType

        e = evaluate_cohort(
            self._strong(),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert isinstance(e.and_status, MappingProxyType)
        try:
            e.and_status["net"] = AND_FAIL  # type: ignore[index]
        except TypeError:
            pass
        else:
            raise AssertionError("and_status must be immutable")


class TestR1TradeDayDateOnly:
    """R1: a datetime IS a date subclass — it must be rejected as invalid
    input (BLOCKED_INVALID_INPUT), never coerced, never day-counted."""

    def test_r1_datetime_trade_days_block(self) -> None:
        # 60 hour-offset datetimes would count as 60 "distinct days".
        records: list[TradeRecord] = []
        for j in range(60):
            day = datetime(2026, 6, 1, 10, j % 60, tzinfo=_ET)
            for _ in range(3):
                records.append(
                    TradeRecord(
                        trade_day=day,  # type: ignore[arg-type]
                        entry_notional=Decimal("10000"),
                        exit_notional=Decimal("10030"),
                        gross_return_bps=30.0,
                        net_baseline_bps=23.4,
                        net_confirmatory_bps=23.4,
                        exit_trigger="PROFIT_TARGET",
                        algorithm_version=ALGORITHM_VERSION,
                        config_digest=_DIGEST,
                        resolved=True,
                    )
                )
        e = evaluate_cohort(
            tuple(records),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == BLOCKED_INVALID_INPUT
        assert e.invalid_input_records == 180
        # Never coerced into day counting.
        assert e.floors["distinct_days"] == 0 or "distinct_days" not in e.floors

    def test_r1_mixed_datetime_blocks_whole_cohort(self) -> None:
        # One datetime among valid dates poisons the cohort.
        records = [
            _record(day, net_conf=8.0 + ((j * 37) % 23))
            for j, day in enumerate(_days(65))
            for _ in range(3)
        ]
        bad = records[10]
        records[10] = TradeRecord(
            trade_day=datetime(2026, 6, 5, 15, 0, tzinfo=_ET),  # type: ignore[arg-type]
            entry_notional=bad.entry_notional,
            exit_notional=bad.exit_notional,
            gross_return_bps=bad.gross_return_bps,
            net_baseline_bps=bad.net_baseline_bps,
            net_confirmatory_bps=bad.net_confirmatory_bps,
            exit_trigger=bad.exit_trigger,
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=True,
        )
        e = evaluate_cohort(
            tuple(records),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == BLOCKED_INVALID_INPUT
        assert e.invalid_input_records == 1

    def test_r1_dsr_rejects_datetime_days(self) -> None:
        from app.domain.guidance_continuation.evaluation import _exact_day_means

        d = datetime(2026, 6, 1, 10, 0, tzinfo=_ET)  # type: ignore[arg-type]
        means = _exact_day_means([(d, 1.0), (d, 2.0)])  # type: ignore[arg-type]
        assert means is None  # invalid day key never reaches the moments


class TestR3ExactDSRMoments:
    def test_r3_non_integer_constant_series_fail_closed(self) -> None:
        from app.domain.guidance_continuation.evaluation import _dsr_stats

        for series in ([1.1] * 60, [-40.1] * 30, [0.3] * 25):
            stats = _dsr_stats(series)
            assert stats["dsr_probability"] is None, series[:1]
            assert stats["distinguishable_from_luck"] is False
            assert stats["fail_closed_reason"]

    def test_r3_platform_parity_still_holds(self) -> None:
        from app.platform.overfitting import deflated_sharpe_ratio
        from app.domain.guidance_continuation.evaluation import _dsr_stats

        for values in (
            [8.0 + ((j * 37) % 23) for j in range(70)],
            [19.0, 21.0] * 30,
        ):
            stats = _dsr_stats(values)
            got = deflated_sharpe_ratio(
                observed_sharpe=stats["sharpe"],
                n_trials=1,
                sample_size=len(values),
                skewness=stats["skewness_g3"],
                kurtosis=stats["kurtosis_g4"],
            )
            assert stats["dsr_probability"] == pytest.approx(
                got["dsr_probability"], abs=1e-12
            )


class TestDSRStraddle:
    """The claimed near-0.95 straddle: two real series bracketing 0.95."""

    def test_dsr_straddle_across_095(self) -> None:
        from app.platform.overfitting import deflated_sharpe_ratio
        from app.domain.guidance_continuation.evaluation import _dsr_stats

        # T=60 two-point series {m-d, m+d}×30: SR = mean/sample SD is a
        # pure function of (m, d) — solve for the 0.95 boundary.
        # sample SD of [m-d, m+d]×30 = d*sqrt(60/59); SR = m/(d*sqrt(60/59)).
        # z = SR / sqrt((1 + (g4-1)/4*SR^2)/(T-1)) with g4=1 → bracket 0.95.
        below = None
        above = None
        for m in [x * 0.01 for x in range(10, 60)]:
            values = [1.0 - 0.5, 1.0 + 0.5]
            values = [1.0 - 0.5 if j % 2 == 0 else 1.0 + 0.5 for j in range(60)]
            values = [m - 0.5 if j % 2 == 0 else m + 0.5 for j in range(60)]
            stats = _dsr_stats(values)
            p = stats["dsr_probability"]
            assert p is not None
            if p < 0.95 and (below is None or p > below[1]):
                below = (values, p, stats)
            if p >= 0.95 and (above is None or p < above[1]):
                above = (values, p, stats)
        assert below is not None and above is not None
        assert 0.90 < below[1] < 0.95, below[1]
        assert 0.95 <= above[1] < 0.99, above[1]
        # Platform parity for BOTH.
        for values, p, stats in (below, above):
            got = deflated_sharpe_ratio(
                observed_sharpe=stats["sharpe"],
                n_trials=1,
                sample_size=60,
                skewness=stats["skewness_g3"],
                kurtosis=stats["kurtosis_g4"],
            )
            assert p == pytest.approx(got["dsr_probability"], abs=1e-12)
        # The pass/fail flip across 0.95.
        assert below[2]["distinguishable_from_luck"] is False
        assert above[2]["distinguishable_from_luck"] is True


class TestV1TriggerValidation:
    """V1: exit_trigger must be one of the four §10.6 constants."""

    def test_v1_mislabelled_stop_is_invalid(self) -> None:
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(65)):
            for i in range(3):
                # One mislabelled stop per day (the last daily trade).
                trigger = "price_stop" if i == 2 else "PROFIT_TARGET"
                records.append(_record(day, net_conf=8.0 + ((j * 37) % 23), trigger=trigger))
        e = evaluate_cohort(
            tuple(records),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == BLOCKED_INVALID_INPUT
        assert e.invalid_input_records == 65  # one per day
        # The mislabelled stops never vanish into the denominator.
        assert e.first_passage == {}

    def test_v1_all_four_valid_triggers_accepted(self) -> None:
        from app.domain.guidance_continuation.exit import (
            TRIGGER_EOD_FLATTEN,
            TRIGGER_MAX_HOLD,
            TRIGGER_PRICE_STOP,
            TRIGGER_PROFIT_TARGET,
        )

        for trig in (
            TRIGGER_PRICE_STOP,
            TRIGGER_PROFIT_TARGET,
            TRIGGER_MAX_HOLD,
            TRIGGER_EOD_FLATTEN,
        ):
            assert _record_problems(_record(_days(1)[0], trigger=trig)) is None


class TestV2ResolvedValidation:
    def test_v2_truthy_resolved_string_is_invalid(self) -> None:
        records = [
            _record(day, net_conf=8.0 + ((j * 37) % 23))
            for j, day in enumerate(_days(65))
            for _ in range(3)
        ]
        r0 = records[0]
        records[0] = TradeRecord(
            trade_day=r0.trade_day,
            entry_notional=r0.entry_notional,
            exit_notional=r0.exit_notional,
            gross_return_bps=r0.gross_return_bps,
            net_baseline_bps=r0.net_baseline_bps,
            net_confirmatory_bps=r0.net_confirmatory_bps,
            exit_trigger=r0.exit_trigger,
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved="no",  # type: ignore[arg-type]
        )
        e = evaluate_cohort(
            tuple(records),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == BLOCKED_INVALID_INPUT
        assert e.invalid_input_records == 1

    def test_v2_int_resolved_is_invalid(self) -> None:
        bad = _record(_days(1)[0])
        bad = TradeRecord(
            trade_day=bad.trade_day,
            entry_notional=bad.entry_notional,
            exit_notional=bad.exit_notional,
            gross_return_bps=bad.gross_return_bps,
            net_baseline_bps=bad.net_baseline_bps,
            net_confirmatory_bps=bad.net_confirmatory_bps,
            exit_trigger=bad.exit_trigger,
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=1,  # type: ignore[arg-type]
        )
        assert _record_problems(bad) is not None


class TestV3GapRecords:
    """V3: a gap record validates on the ENTRY side only."""

    def test_v3_gap_with_nan_returns_is_exit_gap(self) -> None:
        records = [
            _record(day, net_conf=8.0 + ((j * 37) % 23))
            for j, day in enumerate(_days(65))
            for _ in range(3)
        ]
        g = records[7]
        records[7] = TradeRecord(
            trade_day=g.trade_day,
            entry_notional=g.entry_notional,
            exit_notional=Decimal("NaN"),
            gross_return_bps=float("nan"),
            net_baseline_bps=None,  # type: ignore[arg-type]
            net_confirmatory_bps=float("nan"),
            exit_trigger="MAX_HOLD",
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=False,
        )
        e = evaluate_cohort(
            tuple(records),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == BLOCKED_EXIT_GAP
        assert e.exit_gap_records == 1
        assert e.invalid_input_records == 0

    def test_v3_gap_with_none_returns_is_exit_gap(self) -> None:
        records = [
            _record(day, net_conf=8.0 + ((j * 37) % 23))
            for j, day in enumerate(_days(65))
            for _ in range(3)
        ]
        g = records[5]
        records[5] = TradeRecord(
            trade_day=g.trade_day,
            entry_notional=g.entry_notional,
            exit_notional=None,  # type: ignore[arg-type]
            gross_return_bps=None,  # type: ignore[arg-type]
            net_baseline_bps=None,  # type: ignore[arg-type]
            net_confirmatory_bps=None,  # type: ignore[arg-type]
            exit_trigger="PRICE_STOP",
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=False,
        )
        e = evaluate_cohort(
            tuple(records),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == BLOCKED_EXIT_GAP
        assert e.exit_gap_records == 1

    def test_v3_gap_with_datetime_day_is_invalid(self) -> None:
        records = [
            _record(day, net_conf=8.0 + ((j * 37) % 23))
            for j, day in enumerate(_days(65))
            for _ in range(3)
        ]
        g = records[7]
        records[7] = TradeRecord(
            trade_day=datetime(2026, 6, 5, 15, 0, tzinfo=_ET),  # type: ignore[arg-type]
            entry_notional=g.entry_notional,
            exit_notional=None,  # type: ignore[arg-type]
            gross_return_bps=None,  # type: ignore[arg-type]
            net_baseline_bps=None,  # type: ignore[arg-type]
            net_confirmatory_bps=None,  # type: ignore[arg-type]
            exit_trigger="MAX_HOLD",
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=False,
        )
        e = evaluate_cohort(
            tuple(records),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == BLOCKED_INVALID_INPUT
        assert e.invalid_input_records == 1

    def test_v3_gap_trigger_none_is_valid(self) -> None:
        g = TradeRecord(
            trade_day=_days(1)[0],
            entry_notional=Decimal("10000"),
            exit_notional=None,  # type: ignore[arg-type]
            gross_return_bps=None,  # type: ignore[arg-type]
            net_baseline_bps=None,  # type: ignore[arg-type]
            net_confirmatory_bps=None,  # type: ignore[arg-type]
            exit_trigger=None,  # type: ignore[arg-type]
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=False,
        )
        assert _record_problems(g) is None

    def test_v3_gap_bad_trigger_is_invalid(self) -> None:
        g = TradeRecord(
            trade_day=_days(1)[0],
            entry_notional=Decimal("10000"),
            exit_notional=None,  # type: ignore[arg-type]
            gross_return_bps=None,  # type: ignore[arg-type]
            net_baseline_bps=None,  # type: ignore[arg-type]
            net_confirmatory_bps=None,  # type: ignore[arg-type]
            exit_trigger="expired",  # type: ignore[arg-type]
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=False,
        )
        assert _record_problems(g) is not None

    def test_v3_invalid_takes_precedence_over_gap(self) -> None:
        # Both an invalid record AND a gap: BLOCKED_INVALID_INPUT wins,
        # and BOTH counts are reported.
        records = [
            _record(day, net_conf=8.0 + ((j * 37) % 23))
            for j, day in enumerate(_days(65))
            for _ in range(3)
        ]
        g = records[3]
        records[3] = TradeRecord(
            trade_day=g.trade_day,
            entry_notional=g.entry_notional,
            exit_notional=None,  # type: ignore[arg-type]
            gross_return_bps=None,  # type: ignore[arg-type]
            net_baseline_bps=None,  # type: ignore[arg-type]
            net_confirmatory_bps=None,  # type: ignore[arg-type]
            exit_trigger="MAX_HOLD",
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=False,
        )
        b = records[9]
        records[9] = TradeRecord(
            trade_day=b.trade_day,
            entry_notional=Decimal("0"),
            exit_notional=b.exit_notional,
            gross_return_bps=b.gross_return_bps,
            net_baseline_bps=b.net_baseline_bps,
            net_confirmatory_bps=b.net_confirmatory_bps,
            exit_trigger=b.exit_trigger,
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=True,
        )
        e = evaluate_cohort(
            tuple(records),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == BLOCKED_INVALID_INPUT
        assert e.invalid_input_records == 1
        assert e.exit_gap_records == 1  # both counts visible


class TestV4CostColumnCrossCheck:
    """V4: the precomputed net columns must match costs.py recomputation."""

    def _cohort(self, exit_n: str = "10030") -> list[TradeRecord]:
        from app.domain.guidance_continuation.costs import cost_columns

        exit_decimal = Decimal(exit_n)
        out: list[TradeRecord] = []
        for j, day in enumerate(_days(65)):
            b, c = cost_columns(
                entry_notional=Decimal("10000"), exit_notional=exit_decimal
            )
            gross = float(
                (exit_decimal - Decimal("10000"))
                / Decimal("10000")
                * Decimal(10000)
            )
            nb = gross - float(b) / 10000 * 10000
            nc = gross - float(c) / 10000 * 10000
            for _ in range(3):
                out.append(
                    TradeRecord(
                        trade_day=day,
                        entry_notional=Decimal("10000"),
                        exit_notional=exit_decimal,
                        gross_return_bps=gross,
                        net_baseline_bps=nb,
                        net_confirmatory_bps=nc,
                        exit_trigger="PROFIT_TARGET",
                        algorithm_version=ALGORITHM_VERSION,
                        config_digest=_DIGEST,
                        resolved=True,
                    )
                )
        return out

    def test_v4_correct_columns_accepted(self) -> None:
        e = evaluate_cohort(
            tuple(self._cohort("1030")),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict != BLOCKED_INVALID_INPUT
        assert e.invalid_input_records == 0

    def test_v4_swapped_columns_invalid(self) -> None:
        cohort = self._cohort("1030")
        swapped = []
        for r in cohort:
            swapped.append(
                TradeRecord(
                    trade_day=r.trade_day,
                    entry_notional=r.entry_notional,
                    exit_notional=r.exit_notional,
                    gross_return_bps=r.gross_return_bps,
                    net_baseline_bps=r.net_confirmatory_bps,
                    net_confirmatory_bps=r.net_baseline_bps,
                    exit_trigger=r.exit_trigger,
                    algorithm_version=r.algorithm_version,
                    config_digest=r.config_digest,
                    resolved=True,
                )
            )
        e = evaluate_cohort(
            tuple(swapped),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == BLOCKED_INVALID_INPUT
        assert e.invalid_input_records > 0

    def test_v4_off_by_one_bps_confirmatory_invalid(self) -> None:
        cohort = self._cohort("1030")
        nudged = []
        for i, r in enumerate(cohort):
            assert r.net_confirmatory_bps is not None
            conf = r.net_confirmatory_bps - 1.0 if i == 0 else r.net_confirmatory_bps
            nudged.append(
                TradeRecord(
                    trade_day=r.trade_day,
                    entry_notional=r.entry_notional,
                    exit_notional=r.exit_notional,
                    gross_return_bps=r.gross_return_bps,
                    net_baseline_bps=r.net_baseline_bps,
                    net_confirmatory_bps=conf,
                    exit_trigger=r.exit_trigger,
                    algorithm_version=r.algorithm_version,
                    config_digest=r.config_digest,
                    resolved=True,
                )
            )
        e = evaluate_cohort(
            tuple(nudged),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == BLOCKED_INVALID_INPUT
        assert e.invalid_input_records == 1


class TestW1ArgumentContracts:
    """W1: keyword arguments are type-checked."""

    def _strong(self) -> tuple[TradeRecord, ...]:
        return tuple(
            _record(day, net_conf=8.0 + ((j * 37) % 23))
            for j, day in enumerate(_days(65))
            for _ in range(3)
        )

    def test_w1_terminal_string_raises(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            evaluate_cohort(
                self._strong(),
                algorithm_version=ALGORITHM_VERSION,
                config_digest=_DIGEST,
                terminal="False",  # type: ignore[arg-type]
                trial_family_status=VALID_SINGLE_CONFIRMATORY,
            )
        assert "terminal" in str(excinfo.value)

    def test_w1_terminal_int_raises(self) -> None:
        with pytest.raises(ValueError):
            evaluate_cohort(
                self._strong(),
                algorithm_version=ALGORITHM_VERSION,
                config_digest=_DIGEST,
                terminal=1,  # type: ignore[arg-type]
                trial_family_status=VALID_SINGLE_CONFIRMATORY,
            )

    def test_w1_garbage_trial_family_raises(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            evaluate_cohort(
                self._strong(),
                algorithm_version=ALGORITHM_VERSION,
                config_digest=_DIGEST,
                terminal=True,
                trial_family_status="garbage",
            )
        assert "trial_family_status" in str(excinfo.value)

    def test_w1_all_three_family_constants_accepted(self) -> None:
        for status in (VALID_SINGLE_CONFIRMATORY, INVALIDATED, TRIAL_FAMILY_UNKNOWN):
            e = evaluate_cohort(
                self._strong(),
                algorithm_version=ALGORITHM_VERSION,
                config_digest=_DIGEST,
                terminal=True,
                trial_family_status=status,
            )
            assert e.verdict in {NOT_CERTIFIED, "ELIGIBLE_FOR_HUMAN_REVIEW"}

    def test_w1_as_of_datetime_raises_valueerror(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            terminal_due(
                50,
                evidence_start_at_et=datetime(2026, 1, 1, 9, 30, tzinfo=_ET),
                as_of=datetime(2026, 6, 1, 12, 0, tzinfo=_ET),  # type: ignore[arg-type]
            )
        assert "as_of" in str(excinfo.value)

    def test_w1_as_of_plain_date_accepted(self) -> None:
        due, _ = terminal_due(
            50,
            evidence_start_at_et=datetime(2026, 1, 1, 9, 30, tzinfo=_ET),
            as_of=date(2026, 6, 1),
        )
        assert isinstance(due, bool)

    def test_w1_futility_as_of_checks(self) -> None:
        # tz-aware datetime required (existing) and manifest binding intact.
        with pytest.raises(ValueError):
            terminal_due(
                50,
                evidence_start_at_et=datetime(2026, 1, 1, 9, 30),  # naive
                as_of=date(2026, 6, 1),
            )


class TestW2ConfigDigestBinding:
    def test_w2_loosened_config_under_frozen_label_raises(self) -> None:
        import dataclasses

        loose = dataclasses.replace(
            DEFAULT_GUIDANCE_CONFIG,
            promotion_min_distinct_days=5,
            promotion_min_resolved_brackets=10,
            analysis_min_distinct_days=5,
            analysis_min_resolved_brackets=10,
            min_gross_net_observations=10,
            min_gross_net_distinct_days=5,
        )
        records = tuple(
            _record(day, net_conf=8.0) for day in _days(6) for _ in range(2)
        )
        with pytest.raises(ValueError) as excinfo:
            evaluate_cohort(
                records,
                algorithm_version=ALGORITHM_VERSION,
                config_digest=_DIGEST,
                terminal=True,
                trial_family_status=VALID_SINGLE_CONFIRMATORY,
                config=loose,
            )
        assert "config_digest" in str(excinfo.value) or "config" in str(excinfo.value)

    def test_w2_matching_digest_accepted(self) -> None:
        records = tuple(
            _record(day, net_conf=8.0 + ((j * 37) % 23))
            for j, day in enumerate(_days(65))
            for _ in range(3)
        )
        e = evaluate_cohort(
            records,
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
            config=DEFAULT_GUIDANCE_CONFIG,
        )
        assert e.verdict in {NOT_CERTIFIED, "ELIGIBLE_FOR_HUMAN_REVIEW"}



class TestW3VersionPin:
    def test_w3_foreign_version_raises(self) -> None:
        records = tuple(
            _record(day, net_conf=8.0 + ((j * 37) % 23), version="v5-anything")
            for j, day in enumerate(_days(65))
            for _ in range(3)
        )
        with pytest.raises(ValueError) as excinfo:
            evaluate_cohort(
                records,
                algorithm_version="v5-anything",
                config_digest=_DIGEST,
                terminal=True,
                trial_family_status=VALID_SINGLE_CONFIRMATORY,
            )
        assert "algorithm_version" in str(excinfo.value)

    def test_w3_package_constant_accepted(self) -> None:
        records = tuple(
            _record(day, net_conf=8.0 + ((j * 37) % 23))
            for j, day in enumerate(_days(65))
            for _ in range(3)
        )
        e = evaluate_cohort(
            records,
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict in {NOT_CERTIFIED, "ELIGIBLE_FOR_HUMAN_REVIEW"}



class TestW4NotionalCap:
    def _cohort(self, entry: str) -> tuple[TradeRecord, ...]:
        return tuple(
            _record(day, net_conf=8.0 + ((j * 37) % 23) * 0.1, entry_notional=entry)
            for j, day in enumerate(_days(25))
            for _ in range(3)
        )

    def test_w4_cap_exactly_25000_accepted(self) -> None:
        e = evaluate_cohort(
            self._cohort("25000"),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.invalid_input_records == 0

    def test_w4_one_cent_over_cap_invalid(self) -> None:
        e = evaluate_cohort(
            self._cohort("25000.01"),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == BLOCKED_INVALID_INPUT
        assert e.invalid_input_records == 75

    def test_w4_ten_million_invalid(self) -> None:
        e = evaluate_cohort(
            self._cohort("10000000"),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            terminal=True,
            trial_family_status=VALID_SINGLE_CONFIRMATORY,
        )
        assert e.verdict == BLOCKED_INVALID_INPUT

    def test_w4_gap_record_over_cap_invalid(self) -> None:
        g = TradeRecord(
            trade_day=_days(1)[0],
            entry_notional=Decimal("1000000"),
            exit_notional=None,  # type: ignore[arg-type]
            gross_return_bps=None,  # type: ignore[arg-type]
            net_baseline_bps=None,  # type: ignore[arg-type]
            net_confirmatory_bps=None,  # type: ignore[arg-type]
            exit_trigger="MAX_HOLD",
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=False,
        )
        assert _record_problems(g) is not None
