"""Futility assessment (PREREGISTRATION §10.9), independent of v5."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.domain.guidance_continuation.config import (
    ALGORITHM_VERSION,
    config_digest,
)
from app.domain.guidance_continuation.evaluation import TradeRecord
from app.domain.guidance_continuation.evaluation import (
    INVALIDATED,
    TRIAL_FAMILY_UNKNOWN,
    VALID_SINGLE_CONFIRMATORY,
    TradeRecord,
    _record_problems,
)
from app.domain.guidance_continuation.futility import (
    ALIVE,
    BLOCKED_EXIT_GAP_FUTILITY,
    BLOCKED_INVALID_INPUT_FUTILITY,
    BUDGET_EXHAUSTED,
    FUTILE,
    INSUFFICIENT_DATA_FUTILITY,
    assess_guidance_futility,
    futility_checkpoint_due,
)

_ET = ZoneInfo("America/New_York")
_DIGEST = config_digest()

# Reference constants (config defaults).
Z_SUM = 1.6448536269514715 + 0.8416212335729144
SIGMA = 20.0
UPPER_MULT = 2.0


def _days(n: int, start: date = date(2026, 9, 1)) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _record(day: date, net: float, trigger: str = "PROFIT_TARGET") -> TradeRecord:
    """V4-consistent record: columns derived via costs.py from the
    notionals, with the exit notional chosen to land net_confirmatory
    ≈ ``net``."""
    from decimal import Decimal

    from app.domain.guidance_continuation.costs import cost_columns

    entry = Decimal("10000")
    _, confirmatory_usd = cost_columns(
        entry_notional=entry, exit_notional=Decimal("10000")
    )
    gross_bps = net + float(confirmatory_usd) / float(entry) * 10000
    exit_dec = (
        entry * (Decimal(1) + Decimal(repr(gross_bps)) / Decimal(10000))
    ).quantize(Decimal("0.01"))
    b, c = cost_columns(entry_notional=entry, exit_notional=exit_dec)
    g = float((exit_dec - entry) / entry * Decimal(10000))
    return TradeRecord(
        trade_day=day,
        entry_notional=entry,
        exit_notional=exit_dec,
        gross_return_bps=g,
        net_baseline_bps=g - float(b) / float(entry) * 10000,
        net_confirmatory_bps=g - float(c) / float(entry) * 10000,
        exit_trigger=trigger,
        algorithm_version=ALGORITHM_VERSION,
        config_digest=_DIGEST,
        resolved=True,
    )


def _cohort(days: int, net: float, per_day: int = 3) -> tuple[TradeRecord, ...]:
    """Cohort with mean ≈ ``net`` but day-varying structure: identical
    per-day patterns would give a zero day-cluster residual variance
    (SE=None), so vary the daily mean deterministically."""
    out = []
    for j, day in enumerate(_days(days)):
        day_shift = ((j * 7) % 5 - 2) * 0.1  # ±0.2 around net
        offs = [1.0, -1.0, 0.0][:per_day]
        if per_day > 3:
            offs = offs + [0.5] * (per_day - 3)
        for off in offs:
            out.append(_record(day, net + day_shift + off))
    return tuple(out)


def _neg_cohort(per_day: int = 6) -> tuple[TradeRecord, ...]:
    """The M2 repro cohort: 25 days × 6 ≈ −40 bps mixed-trigger trades."""
    out: list[TradeRecord] = []
    for j, day in enumerate(_days(25)):
        for i in range(per_day):
            trigger = "PROFIT_TARGET" if (j + i) % 2 == 0 else "PRICE_STOP"
            out.append(
                _record(day, -40.0 + ((j * 7 + i) % 5) * 0.1, trigger=trigger)
            )
    return tuple(out)


def _m1_cohort(stops_none: bool) -> tuple[TradeRecord, ...]:
    """The M1 denominator-contamination cohort (same as the evaluator
    test): 100 days, 600 trades, 180 PROFIT_TARGET / 420 PRICE_STOP;
    ``stops_none`` relabels the 420 stops' trigger to None."""
    out: list[TradeRecord] = []
    for j, day in enumerate(_days(100)):
        net = 8.0 + ((j * 37) % 23)
        for i in range(6):
            if (j * 6 + i) < 180:
                trigger: str | None = "PROFIT_TARGET"
            else:
                trigger = None if stops_none else "PRICE_STOP"
            out.append(_record(day, net, trigger=trigger))  # type: ignore[arg-type]
    return tuple(out)


class TestVerdicts:
    def test_alive_when_upper_bound_nonnegative(self) -> None:
        # mean ≈ 12 (the quantized exit notional shifts it a fraction of
        # a bp), SE ~0 → U = μ + 2·SE ≥ 0 → ALIVE.
        result = assess_guidance_futility(
            _cohort(25, 12.0), algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == ALIVE
        assert result.mu_net_bps == pytest.approx(12.0, abs=0.1)

    def test_futile_when_upper_bound_negative_and_power_sufficient(self) -> None:
        # Strongly negative mean with tiny SE: U < 0, and
        # MDE = 2.4864·20/sqrt(75) ≈ 5.74 bps ≤ required = 40 → FUTILE.
        result = assess_guidance_futility(
            _cohort(25, -40.0), algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == FUTILE
        assert result.u_net_bps is not None and result.u_net_bps < 0
        assert result.mde_bps is not None
        assert result.required_effect_bps is not None
        assert result.mde_bps <= result.required_effect_bps

    def test_insufficient_data_below_floors(self) -> None:
        result = assess_guidance_futility(
            _cohort(19, 12.0), algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == INSUFFICIENT_DATA_FUTILITY
        assert result.verdict != BUDGET_EXHAUSTED

    def test_negative_upper_but_power_insufficient(self) -> None:
        # μ ≈ −5, small clustered SE (≈0.28) → U < 0, but the planning
        # MDE at D=25 is ≈ 9.95 bps > required 5 → power condition NOT
        # met → INSUFFICIENT_DATA (cannot abandon), NOT FUTILE.
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(25)):
            shift = ((j * 7) % 5 - 2) * 0.2  # day means ≈ −5 ± 0.4
            records.append(_record(day, -5.0 + shift + 0.5))
            records.append(_record(day, -5.0 + shift - 0.5))
        result = assess_guidance_futility(
            tuple(records), algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.u_net_bps is not None and result.u_net_bps < 0
        assert result.mde_bps is not None
        assert result.required_effect_bps is not None
        assert result.mde_bps > result.required_effect_bps
        assert result.verdict == INSUFFICIENT_DATA_FUTILITY


class TestInputsAndOutputs:
    def test_machine_inputs_reported(self) -> None:
        result = assess_guidance_futility(
            _cohort(25, 12.0), algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.machine_inputs is not None
        assert result.machine_inputs["distinct_days"] == 25
        assert result.machine_inputs["observations"] == 75
        # G7: a checkpoint label only on the EXACT day — D=25 is not
        # "past the 20 checkpoint".
        assert result.machine_inputs["checkpoint"] is None
        assert result.machine_outputs["verdict"] == ALIVE

    def test_digest_present_and_deterministic(self) -> None:
        cohort = _cohort(25, 12.0)
        d1 = assess_guidance_futility(
            cohort, algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        ).digest
        d2 = assess_guidance_futility(
            cohort, algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        ).digest
        assert d1 == d2
        assert len(d1) == 64 and all(c in "0123456789abcdef" for c in d1)
        # A different cohort hashes differently.
        d3 = assess_guidance_futility(
            _cohort(25, 13.0), algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        ).digest
        assert d3 != d1

    def test_measured_dispersion_reported(self) -> None:
        result = assess_guidance_futility(
            _cohort(25, 12.0), algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.measured_day_dispersion_bps is not None
        assert result.measured_day_dispersion_bps >= 0

    def test_measured_dispersion_above_20_flags_ratification(self) -> None:
        # Per-day nets alternating ±60 → stdev ≈ 66 > 20 → flag set, and
        # MDE at the measured dispersion is also reported.  Floors met
        # via two trades per day (50 observations over 25 days).
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(25)):
            big = 60.0 if j % 2 == 0 else -60.0
            records.append(_record(day, big))
            records.append(_record(day, big * 0.9))
        result = assess_guidance_futility(
            tuple(records), algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        disp = result.measured_day_dispersion_bps
        assert disp is not None and disp > 20.0
        assert result.requires_measured_dispersion_ratification
        measured_mde = result.mde_at_measured_dispersion_bps
        planned = result.mde_bps
        assert measured_mde is not None and planned is not None
        assert measured_mde > planned

    def test_flag_never_creates_pass(self) -> None:
        # Even ALIVE with the flag: the flag only blocks abandonment.
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(25)):
            big = 80.0 if j % 2 == 0 else -60.0
            records.append(_record(day, big))
            records.append(_record(day, big * 0.9))
        result = assess_guidance_futility(
            tuple(records), algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.requires_measured_dispersion_ratification
        assert result.verdict in {ALIVE, INSUFFICIENT_DATA_FUTILITY, FUTILE}
        # Mean > 0 with modest SE → U ≥ 0 → only ALIVE is possible.
        u = result.u_net_bps
        assert u is not None
        assert u > 0 or result.verdict == ALIVE
        assert result.verdict == ALIVE

    def test_budget_exhausted_distinct(self) -> None:
        assert BUDGET_EXHAUSTED != FUTILE
        assert BUDGET_EXHAUSTED != INSUFFICIENT_DATA_FUTILITY
        assert FUTILE != INSUFFICIENT_DATA_FUTILITY


class TestCheckpoints:
    @pytest.mark.parametrize("day", [20, 40, 60, 80])
    def test_checkpoint_days(self, day: int) -> None:
        assert futility_checkpoint_due(day)

    @pytest.mark.parametrize("day", [0, 1, 19, 21, 39, 41, 59, 61, 79, 81, 99, 101])
    def test_non_checkpoint_days(self, day: int) -> None:
        assert not futility_checkpoint_due(day)


class TestD2CohortIsolation:
    """D2: futility needs the same cohort isolation as evaluate_cohort."""

    def test_d2_mixed_algorithm_version_raises(self) -> None:
        cohort = list(_cohort(25, -40.0))
        cohort[-1] = _record(cohort[-1].trade_day, -40.0)
        # Rebuild with a bad version via the evaluation dataclass.
        from decimal import Decimal

        bad = TradeRecord(
            trade_day=cohort[-1].trade_day,
            entry_notional=Decimal("10000"),
            exit_notional=Decimal("9960"),
            gross_return_bps=-10.0,
            net_baseline_bps=-37.0,
            net_confirmatory_bps=-40.0,
            exit_trigger="PROFIT_TARGET",
            algorithm_version="other-v",
            config_digest=_DIGEST,
            resolved=True,
        )
        with pytest.raises(ValueError) as excinfo:
            assess_guidance_futility(
                tuple(cohort[:-1]) + (bad,),
                algorithm_version=ALGORITHM_VERSION,
                config_digest=_DIGEST,
                as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
            )
        assert "algorithm_version" in str(excinfo.value)

    def test_d2_mixed_config_digest_raises(self) -> None:
        from decimal import Decimal

        cohort = _cohort(25, -40.0)
        bad = TradeRecord(
            trade_day=cohort[-1].trade_day,
            entry_notional=Decimal("10000"),
            exit_notional=Decimal("9960"),
            gross_return_bps=-10.0,
            net_baseline_bps=-37.0,
            net_confirmatory_bps=-40.0,
            exit_trigger="PROFIT_TARGET",
            algorithm_version=ALGORITHM_VERSION,
            config_digest="0" * 64,
            resolved=True,
        )
        with pytest.raises(ValueError) as excinfo:
            assess_guidance_futility(
                cohort[:-1] + (bad,),
                algorithm_version=ALGORITHM_VERSION,
                config_digest=_DIGEST,
                as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
            )
        assert "config_digest" in str(excinfo.value)

    def test_d2_version_and_digest_are_required_keywords(self) -> None:
        with pytest.raises(TypeError):
            assess_guidance_futility(_cohort(25, 12.0))  # type: ignore[call-arg]


class TestD3ExitGap:
    """D3: unresolved exit gaps block futility — never FUTILE or ALIVE."""

    def test_d3_gap_gives_blocked_exit_gap(self) -> None:
        cohort = list(_cohort(25, -40.0))
        last = cohort[-1]
        cohort[-1] = TradeRecord(
            trade_day=last.trade_day,
            entry_notional=last.entry_notional,
            exit_notional=last.exit_notional,
            gross_return_bps=last.gross_return_bps,
            net_baseline_bps=last.net_baseline_bps,
            net_confirmatory_bps=last.net_confirmatory_bps,
            exit_trigger="MAX_HOLD",
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=False,
        )
        result = assess_guidance_futility(
            tuple(cohort), algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == BLOCKED_EXIT_GAP_FUTILITY
        assert result.verdict != FUTILE
        assert result.verdict != ALIVE
        assert result.machine_inputs["exit_gap_records"] == 1

    def test_d3_gap_recorded_in_machine_inputs_and_digest(self) -> None:
        cohort = list(_cohort(25, 12.0))
        last = cohort[-1]
        cohort[-1] = TradeRecord(
            trade_day=last.trade_day,
            entry_notional=last.entry_notional,
            exit_notional=last.exit_notional,
            gross_return_bps=last.gross_return_bps,
            net_baseline_bps=last.net_baseline_bps,
            net_confirmatory_bps=last.net_confirmatory_bps,
            exit_trigger="MAX_HOLD",
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=False,
        )
        result = assess_guidance_futility(
            tuple(cohort), algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.machine_inputs["exit_gap_records"] == 1
        assert len(result.digest) == 64


class TestD4DegenerateSE:
    """D4: a zero/undefined clustered SE is not a tight upper bound."""

    def test_d4_constant_series_fails_closed(self) -> None:
        # Every trade net = −10 → naive SE collapse → clustered SE None/0.
        records = tuple(
            _record(day, -10.0) for day in _days(25) for _ in range(2)
        )
        result = assess_guidance_futility(
            records, algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == INSUFFICIENT_DATA_FUTILITY
        assert result.verdict != FUTILE
        assert "fail_closed_reason" in result.machine_inputs
        assert result.machine_inputs["fail_closed_reason"]

    def test_d4_no_zero_substitution_in_outputs(self) -> None:
        records = tuple(
            _record(day, -10.0) for day in _days(25) for _ in range(2)
        )
        result = assess_guidance_futility(
            records, algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        # U must not be fabricated from a substituted SE.
        assert result.u_net_bps is None or result.se_net_bps not in (None, 0.0)
        # Abandonment unreachable.
        assert result.verdict != FUTILE


class TestG1FutilityValidation:
    """G1: invalid inputs block the futility verdict too."""

    def test_g1_nan_gross_negative_cohort_blocks(self) -> None:
        nan = float("nan")
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(25)):
            net = -40.0 + ((j * 7) % 5) * 0.1
            records.append(_record(day, net))
            bad = records[-1]
            records[-1] = TradeRecord(
                trade_day=bad.trade_day,
                entry_notional=bad.entry_notional,
                exit_notional=bad.exit_notional,
                gross_return_bps=nan,
                net_baseline_bps=bad.net_baseline_bps,
                net_confirmatory_bps=bad.net_confirmatory_bps,
                exit_trigger=bad.exit_trigger,
                algorithm_version=ALGORITHM_VERSION,
                config_digest=_DIGEST,
                resolved=True,
            )
        result = assess_guidance_futility(
            tuple(records),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == BLOCKED_INVALID_INPUT_FUTILITY
        assert result.verdict != FUTILE


class TestM1FutilityResolvedTriggerRequired:
    """M1 through the futility evaluator: a RESOLVED record with
    ``exit_trigger=None`` must be invalid input, not a time exit."""

    def test_m1_denominator_contamination_blocked(self) -> None:
        # The reviewer repro, exactly: 100 days, 600 trades, 180
        # PROFIT_TARGET and 420 PRICE_STOP whose trigger is set to None.
        # Previously the None records were counted as time exits and
        # dropped out of the first-passage denominator — through the
        # shared validator they must now be invalid input.
        cohort = _m1_cohort(stops_none=True)
        result = assess_guidance_futility(
            cohort,
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == BLOCKED_INVALID_INPUT_FUTILITY
        assert result.machine_inputs["invalid_input_records"] == 420

    def test_m1_none_trigger_on_resolved_is_invalid(self) -> None:
        cohort = list(_neg_cohort())
        cohort[7] = replace(cohort[7], exit_trigger=None)
        result = assess_guidance_futility(
            tuple(cohort),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == BLOCKED_INVALID_INPUT_FUTILITY
        assert result.machine_inputs["invalid_input_records"] == 1


class TestM2FutilityDecimalRecomputation:
    """M2 through the futility evaluator: the Decimal recomputation must
    catch overflow/underflow notionals.  The reviewer repro: a 150-record
    negative cohort with ONLY one exit_notional = Decimal("1e400")
    previously gave FUTILE with 0 invalid records (float(1e400) = inf →
    inf − inf = NaN → comparison False → accepted)."""

    def test_m2_exit_notional_1e400_invalid(self) -> None:
        cohort = list(_neg_cohort())
        cohort[7] = replace(cohort[7], exit_notional=Decimal("1e400"))
        result = assess_guidance_futility(
            tuple(cohort),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == BLOCKED_INVALID_INPUT_FUTILITY
        assert result.machine_inputs["invalid_input_records"] == 1

    def test_m2_entry_notional_1e_minus_400_invalid(self) -> None:
        cohort = list(_neg_cohort())
        cohort[3] = replace(cohort[3], entry_notional=Decimal("1e-400"))
        result = assess_guidance_futility(
            tuple(cohort),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == BLOCKED_INVALID_INPUT_FUTILITY
        assert result.machine_inputs["invalid_input_records"] == 1
        # The problem message names the offending field.
        problem = _record_problems(cohort[3])
        assert problem is not None
        assert "entry_notional" in problem

    def test_m2_negative_cohort_without_blowup_is_futile(self) -> None:
        # Guard the legitimate verdict: the same cohort WITHOUT the
        # blown-up notional is still FUTILE (μ ≈ −40, SE tiny).
        result = assess_guidance_futility(
            _neg_cohort(),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == FUTILE
        # The clean path reports no invalid inputs (key absent on the
        # non-blocked path — absent means zero).
        assert not result.machine_inputs.get("invalid_input_records")


class TestH1FutilityGrossCrossCheck:
    def test_h1_gross_shifted_by_5bps_invalid(self) -> None:
        cohort = list(_neg_cohort())
        r = cohort[7]
        assert r.gross_return_bps is not None
        cohort[7] = replace(cohort[7], gross_return_bps=r.gross_return_bps + 5.0)
        result = assess_guidance_futility(
            tuple(cohort),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == BLOCKED_INVALID_INPUT_FUTILITY
        assert result.machine_inputs["invalid_input_records"] == 1
        problem = _record_problems(cohort[7])
        assert problem is not None
        assert "gross_return_bps" in problem


class TestG2ExactCR1:
    def test_g2_constant_401_fails_closed(self) -> None:
        # All net = −40.1: float residue SE ≈ 1.16e-14 previously gave
        # FUTILE; exact-rational CR1 sees variance == 0 → fail closed.
        records = tuple(
            _record(day, -40.1) for day in _days(25) for _ in range(3)
        )
        result = assess_guidance_futility(
            records,
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == INSUFFICIENT_DATA_FUTILITY
        assert result.machine_inputs["fail_closed_reason"]

    def test_g2_exact_cr1_parity_with_strategy_v2(self) -> None:
        # On non-degenerate data, _exact_cr1 matches clustered_t_test.
        from app.domain.guidance_continuation.evaluation import _exact_cr1
        from app.domain.strategy_v2.clustered_returns import (
            clustered_t_test,
            day_cluster_t_critical,
        )

        obs = []
        for j, day in enumerate(_days(30)):
            for i in range(1 + j % 3):  # uneven trades per day
                obs.append((day, 5.0 + ((j * 13 + i * 7) % 11) * 0.7 - 3.0))
        mean_e, se_e = _exact_cr1(obs)
        ref = clustered_t_test(obs)
        assert mean_e == pytest.approx(ref.naive_mean, rel=1e-12)
        assert se_e is not None and ref.clustered_standard_error is not None
        assert se_e == pytest.approx(ref.clustered_standard_error, rel=1e-12)
        # Same CI basis with the frozen t critical.
        crit = day_cluster_t_critical(ref.distinct_days)
        assert mean_e - crit * se_e == pytest.approx(ref.ci_lower, rel=1e-12)

    def test_g2_hand_computed_uneven_cr1(self) -> None:
        """Hand-computed CR1 with uneven trades per day.

        Day A: one trade, value 1. Day B: four trades, values 2, 3, 4, 5.
        n = 5, trade mean = (1+2+3+4+5)/5 = 3.
        Residuals: day A: (1−3) = −2, sum = −2, squared = 4.
        Day B residuals: (−1, 0, 1, 2), sum = 2, squared = 4.
        G = 2 clusters → variance = G/(G−1) · (4 + 4) / n² = 2·8/25 = 0.64.
        SE = sqrt(0.64) = 0.8 EXACTLY.
        """
        from app.domain.guidance_continuation.evaluation import _exact_cr1

        d1, d2 = date(2026, 6, 1), date(2026, 6, 2)
        obs = [(d1, 1.0)] + [(d2, float(v)) for v in (2, 3, 4, 5)]
        mean_e, se_e = _exact_cr1(obs)
        assert mean_e == pytest.approx(3.0, abs=1e-12)
        assert se_e is not None
        assert se_e == pytest.approx(0.8, abs=1e-12)

    def test_g2_exact_zero_variance_detects_degenerate(self) -> None:
        from app.domain.guidance_continuation.evaluation import _exact_cr1

        d1, d2 = date(2026, 6, 1), date(2026, 6, 2)
        obs = [(d1, 7.0), (d2, 7.0), (d1, 7.0)]
        mean_e, se_e = _exact_cr1(obs)
        assert se_e is None  # variance == 0 exactly


class TestG6FutilityDayBudget:
    def test_g6_101_days_raises(self) -> None:
        records: list[TradeRecord] = []
        for j, day in enumerate(_days(101)):
            records.append(_record(day, -5.0 + (j % 5) * 0.1))
        with pytest.raises(ValueError):
            assess_guidance_futility(
                tuple(records),
                algorithm_version=ALGORITHM_VERSION,
                config_digest=_DIGEST,
                as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
            )


class TestG7ManifestDigest:
    def _cohort(self, shift_days: int = 0) -> tuple[TradeRecord, ...]:
        return tuple(
            _record(day + timedelta(days=shift_days), -5.0 + ((j % 5) * 0.3))
            for j, day in enumerate(_days(25))
        )

    def test_g7_shifted_dates_change_digest(self) -> None:
        a = assess_guidance_futility(
            self._cohort(0),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        b = assess_guidance_futility(
            self._cohort(7),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert a.digest != b.digest

    def test_g7_version_and_digest_bind_inputs(self) -> None:
        base = assess_guidance_futility(
            self._cohort(0),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        # The identity fields are bound INTO machine_inputs, which feed
        # the digest: a cohort carrying a different version/digest must
        # have a different manifest (it is part of every record).
        assert base.machine_inputs["algorithm_version"] == ALGORITHM_VERSION
        assert base.machine_inputs["config_digest"] == _DIGEST
        from app.domain.guidance_continuation.futility import record_manifest

        cohort_b = list(self._cohort(0))
        last = cohort_b[-1]
        cohort_b[-1] = TradeRecord(
            trade_day=last.trade_day,
            entry_notional=last.entry_notional,
            exit_notional=last.exit_notional,
            gross_return_bps=last.gross_return_bps,
            net_baseline_bps=last.net_baseline_bps,
            net_confirmatory_bps=last.net_confirmatory_bps,
            exit_trigger=last.exit_trigger,
            algorithm_version="another-version",
            config_digest="9" * 64,
            resolved=True,
        )
        _, sha_b = record_manifest(tuple(cohort_b))
        assert base.machine_inputs["record_manifest_sha256"] != sha_b

    def test_g7_input_order_does_not_change_digest(self) -> None:
        cohort = list(self._cohort(0))
        reversed_cohort = tuple(reversed(cohort))
        a = assess_guidance_futility(
            tuple(cohort),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        b = assess_guidance_futility(
            reversed_cohort,
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert a.digest == b.digest

    def test_g7_as_of_binds_digest(self) -> None:
        cohort = self._cohort(0)
        a = assess_guidance_futility(
            cohort,
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        b = assess_guidance_futility(
            cohort,
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 2, 12, 0, tzinfo=_ET),
        )
        assert a.digest != b.digest

    def test_g7_checkpoint_only_on_exact_day(self) -> None:
        result = assess_guidance_futility(
            self._cohort(0),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.machine_inputs["checkpoint"] is None  # D = 25
        # Exactly 40 distinct days → checkpoint == 40.
        cohort40 = tuple(
            _record(day, -5.0 + ((j % 5) * 0.3)) for j, day in enumerate(_days(40))
        )
        result40 = assess_guidance_futility(
            cohort40,
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result40.machine_inputs["distinct_days"] == 40
        assert result40.machine_inputs["checkpoint"] == 40


class TestR2ValidateThenEncode:
    """R2: validation runs BEFORE any encoding; invalid inputs give
    BLOCKED_INVALID_INPUT with a never-raising DIAGNOSTIC digest."""

    def _bad(self, **overrides) -> TradeRecord:
        base = _record(date(2026, 6, 1), -40.0)
        return TradeRecord(
            trade_day=overrides.get("trade_day", base.trade_day),
            entry_notional=overrides.get("entry_notional", base.entry_notional),
            exit_notional=overrides.get("exit_notional", base.exit_notional),
            gross_return_bps=overrides.get("gross_return_bps", base.gross_return_bps),
            net_baseline_bps=overrides.get("net_baseline_bps", base.net_baseline_bps),
            net_confirmatory_bps=overrides.get("net_confirmatory_bps", base.net_confirmatory_bps),
            exit_trigger=overrides.get("exit_trigger", base.exit_trigger),
            algorithm_version=overrides.get("algorithm_version", base.algorithm_version),
            config_digest=overrides.get("config_digest", base.config_digest),
            resolved=overrides.get("resolved", base.resolved),
        )

    def _cohort_with(self, bad: TradeRecord) -> tuple[TradeRecord, ...]:
        cohort = [_record(day, -40.0 + ((j % 5) * 0.2)) for j, day in enumerate(_days(25))]
        return tuple(cohort[:-1]) + (bad,)

    def test_r2_nan_notional_blocks_without_exception(self) -> None:
        result = assess_guidance_futility(
            self._cohort_with(self._bad(entry_notional=Decimal("NaN"))),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == BLOCKED_INVALID_INPUT_FUTILITY
        assert result.machine_inputs["invalid_input_records"] == 1
        assert len(result.digest) == 64

    def test_r2_none_gross_blocks_without_exception(self) -> None:
        result = assess_guidance_futility(
            self._cohort_with(self._bad(gross_return_bps=None)),  # type: ignore[arg-type]
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == BLOCKED_INVALID_INPUT_FUTILITY

    def test_r2_string_gross_blocks_without_exception(self) -> None:
        result = assess_guidance_futility(
            self._cohort_with(self._bad(gross_return_bps="5.0")),  # type: ignore[arg-type]
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == BLOCKED_INVALID_INPUT_FUTILITY

    def test_r2_infinities_block_without_exception(self) -> None:
        for bad_value in (float("inf"), float("-inf")):
            result = assess_guidance_futility(
                self._cohort_with(self._bad(gross_return_bps=bad_value)),
                algorithm_version=ALGORITHM_VERSION,
                config_digest=_DIGEST,
                as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
            )
            assert result.verdict == BLOCKED_INVALID_INPUT_FUTILITY

    def test_r2_datetime_trade_day_blocks(self) -> None:
        result = assess_guidance_futility(
            self._cohort_with(
                self._bad(trade_day=datetime(2026, 6, 1, 10, 0, tzinfo=_ET))  # type: ignore[arg-type]
            ),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == BLOCKED_INVALID_INPUT_FUTILITY

    def test_r2_mixed_int_float_bps_work(self) -> None:
        cohort = [_record(day, -40.0) for day in _days(25)]
        m0 = cohort[0]
        # int gross that is numerically IDENTICAL to the derived float:
        # V4-consistent (the column check compares numerically) while
        # exercising the int/float mixed-encoding path.
        cohort[0] = TradeRecord(
            trade_day=m0.trade_day,
            entry_notional=m0.entry_notional,
            exit_notional=m0.exit_notional,
            gross_return_bps=5,  # int, not float — but == derived 5.0?
            net_baseline_bps=m0.net_baseline_bps,
            net_confirmatory_bps=m0.net_confirmatory_bps,
            exit_trigger="PROFIT_TARGET",
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=True,
        )
        # Build a notional pair whose gross IS exactly 5 bps so the int
        # equals the recomputation.
        from decimal import Decimal

        entry = Decimal("10000")
        exit_dec = (entry * (Decimal(1) + Decimal(5) / Decimal(10000))).quantize(Decimal("0.01"))
        from app.domain.guidance_continuation.costs import cost_columns

        b, c = cost_columns(entry_notional=entry, exit_notional=exit_dec)
        g = float((exit_dec - entry) / entry * Decimal(10000))
        cohort[0] = TradeRecord(
            trade_day=m0.trade_day,
            entry_notional=entry,
            exit_notional=exit_dec,
            gross_return_bps=5,
            net_baseline_bps=g - float(b) / float(entry) * 10000,
            net_confirmatory_bps=g - float(c) / float(entry) * 10000,
            exit_trigger="PROFIT_TARGET",
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=True,
        )
        result = assess_guidance_futility(
            tuple(cohort),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict != BLOCKED_INVALID_INPUT_FUTILITY

    def test_r2_int_5_and_float_5_same_manifest(self) -> None:
        from app.domain.guidance_continuation.futility import record_manifest

        def make(gross: object) -> TradeRecord:
            base = _record(date(2026, 6, 1), -40.0)
            return TradeRecord(
                trade_day=base.trade_day,
                entry_notional=base.entry_notional,
                exit_notional=base.exit_notional,
                gross_return_bps=gross,  # type: ignore[arg-type]
                net_baseline_bps=base.net_baseline_bps,
                net_confirmatory_bps=base.net_confirmatory_bps,
                exit_trigger=base.exit_trigger,
                algorithm_version=base.algorithm_version,
                config_digest=base.config_digest,
                resolved=base.resolved,
            )

        _, sha_int = record_manifest((make(5),))
        _, sha_float = record_manifest((make(5.0),))
        assert sha_int == sha_float


class TestR4OrderIndependentDigest:
    def _cohort(self) -> list[TradeRecord]:
        import random

        rng = random.Random(2)
        return [
            _record(day, rng.gauss(-3, 30)) for day in _days(25) for _ in range(3)
        ]

    def test_r4_reversed_cohort_same_digest(self) -> None:
        cohort = self._cohort()
        a = assess_guidance_futility(
            tuple(cohort), algorithm_version=ALGORITHM_VERSION, config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        b = assess_guidance_futility(
            tuple(reversed(cohort)), algorithm_version=ALGORITHM_VERSION, config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert a.digest == b.digest
        assert a.machine_inputs == b.machine_inputs

    def test_r4_five_permutations_same_digest(self) -> None:
        import random

        cohort = self._cohort()
        rng = random.Random(99)
        base = assess_guidance_futility(
            tuple(cohort), algorithm_version=ALGORITHM_VERSION, config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        for _ in range(5):
            shuffled = cohort[:]
            rng.shuffle(shuffled)
            other = assess_guidance_futility(
                tuple(shuffled), algorithm_version=ALGORITHM_VERSION, config_digest=_DIGEST,
                as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
            )
            assert other.digest == base.digest
            assert other.machine_inputs == base.machine_inputs


class TestVValidationFutility:
    """V1-V4 applied through the futility evaluator (shared helper)."""

    def _cohort(self) -> list[TradeRecord]:
        return [
            _record(day, -40.0 + ((j % 5) * 0.2)) for j, day in enumerate(_days(25))
        ]

    def test_v1_bad_trigger_invalid(self) -> None:
        cohort = self._cohort()
        bad = cohort[3]
        cohort[3] = TradeRecord(
            trade_day=bad.trade_day,
            entry_notional=bad.entry_notional,
            exit_notional=bad.exit_notional,
            gross_return_bps=bad.gross_return_bps,
            net_baseline_bps=bad.net_baseline_bps,
            net_confirmatory_bps=bad.net_confirmatory_bps,
            exit_trigger="price_stop",
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=True,
        )
        result = assess_guidance_futility(
            tuple(cohort),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == BLOCKED_INVALID_INPUT_FUTILITY
        assert result.machine_inputs["invalid_input_records"] == 1

    def test_v3_gap_nan_returns_exit_gap_not_invalid(self) -> None:
        cohort = self._cohort()
        g = cohort[5]
        cohort[5] = TradeRecord(
            trade_day=g.trade_day,
            entry_notional=g.entry_notional,
            exit_notional=Decimal("NaN"),
            gross_return_bps=float("nan"),
            net_baseline_bps=float("nan"),
            net_confirmatory_bps=float("nan"),
            exit_trigger="MAX_HOLD",
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            resolved=False,
        )
        result = assess_guidance_futility(
            tuple(cohort),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == BLOCKED_EXIT_GAP_FUTILITY
        assert result.machine_inputs["exit_gap_records"] == 1

    def test_v4_cost_columns_checked(self) -> None:
        from app.domain.guidance_continuation.costs import cost_columns

        cohort = self._cohort()
        swapped = []
        for r in cohort:
            b, c = cost_columns(
                entry_notional=Decimal("10000"), exit_notional=Decimal("10030")
            )
            gross = float((Decimal("10030") - Decimal("10000")) / Decimal("10000") * Decimal(10000))
            swapped.append(
                TradeRecord(
                    trade_day=r.trade_day,
                    entry_notional=Decimal("10000"),
                    exit_notional=Decimal("10030"),
                    gross_return_bps=gross,
                    net_baseline_bps=gross - float(c) / 10000 * 10000,
                    net_confirmatory_bps=gross - float(b) / 10000 * 10000,
                    exit_trigger=r.exit_trigger,
                    algorithm_version=r.algorithm_version,
                    config_digest=r.config_digest,
                    resolved=True,
                )
            )
        result = assess_guidance_futility(
            tuple(swapped),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == BLOCKED_INVALID_INPUT_FUTILITY


class TestW4NotionalCapFutility:
    def test_w4_over_cap_invalid_in_futility(self) -> None:
        cohort = []
        for j, day in enumerate(_days(25)):
            entry = Decimal("30000")  # above the 25,000 cap
            from app.domain.guidance_continuation.costs import cost_columns

            _, conf = cost_columns(entry_notional=entry, exit_notional=Decimal("30000"))
            b, c = cost_columns(entry_notional=entry, exit_notional=Decimal("30000"))
            g = 0.0
            cohort.append(
                TradeRecord(
                    trade_day=day,
                    entry_notional=entry,
                    exit_notional=Decimal("30000"),
                    gross_return_bps=g,
                    net_baseline_bps=g - float(b) / float(entry) * 10000,
                    net_confirmatory_bps=g - float(c) / float(entry) * 10000,
                    exit_trigger="PROFIT_TARGET",
                    algorithm_version=ALGORITHM_VERSION,
                    config_digest=_DIGEST,
                    resolved=True,
                )
            )
        result = assess_guidance_futility(
            tuple(cohort),
            algorithm_version=ALGORITHM_VERSION,
            config_digest=_DIGEST,
            as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
        )
        assert result.verdict == BLOCKED_INVALID_INPUT_FUTILITY
        assert result.machine_inputs["invalid_input_records"] == 25


class TestW2W3FutilityContracts:
    """W2/W3 through the futility evaluator."""

    def test_w2_loosened_config_under_frozen_label_raises(self) -> None:
        import dataclasses

        from app.domain.guidance_continuation.config import DEFAULT_GUIDANCE_CONFIG

        loose = dataclasses.replace(DEFAULT_GUIDANCE_CONFIG, mde_sigma_day_bps=Decimal("1"))
        cohort = tuple(_record(day, -5.0) for day in _days(25))
        with pytest.raises(ValueError):
            assess_guidance_futility(
                cohort,
                algorithm_version=ALGORITHM_VERSION,
                config_digest=_DIGEST,
                as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
                config=loose,
            )

    def test_w3_foreign_version_raises(self) -> None:
        cohort = tuple(_record(day, -5.0) for day in _days(25))
        # Rebuild every record with the foreign version so isolation
        # passes and ONLY the version pin can reject it.
        foreign = tuple(
            TradeRecord(
                trade_day=r.trade_day,
                entry_notional=r.entry_notional,
                exit_notional=r.exit_notional,
                gross_return_bps=r.gross_return_bps,
                net_baseline_bps=r.net_baseline_bps,
                net_confirmatory_bps=r.net_confirmatory_bps,
                exit_trigger=r.exit_trigger,
                algorithm_version="v5-anything",
                config_digest=r.config_digest,
                resolved=True,
            )
            for r in cohort
        )
        with pytest.raises(ValueError) as excinfo:
            assess_guidance_futility(
                foreign,
                algorithm_version="v5-anything",
                config_digest=_DIGEST,
                as_of=datetime(2026, 12, 1, 12, 0, tzinfo=_ET),
            )
        assert "algorithm_version" in str(excinfo.value)
