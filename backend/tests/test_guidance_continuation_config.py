"""Guidance-continuation config freeze (PREREGISTRATION §10).

``config_digest()`` is §10's ``config_version``: the SHA-256 of the
canonical JSON of ``config_payload()``.  The hash below pins the frozen
rule set of ``earnings-revenue-guidance-continuation-v1``.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, localcontext

from app.domain.guidance_continuation.config import (
    ALGORITHM_VERSION,
    DEFAULT_GUIDANCE_CONFIG,
    GuidanceContinuationConfig,
    config_digest,
    config_payload,
    first_passage_driftless_baseline,
)

# Never update this hash to silence the test; a rule change needs a new
# algorithm_version per PREREGISTRATION §4/§10.8.
# P1a draft digest.  History: b17a4b8c…9e3a2b (initial P1a) → badfec43…
# c4ae98 (Gate-1 B1-B8) → 444e9c2d…5d9d31 (B3 residual: per-index
# coverage) → current (R1-R3: order-independent conflicts, coverage
# ANY-rule, precision-independent canonical decimals).  P2/P3 must
# extend the payload before REGISTRATION.
_FROZEN_DIGEST = "514f679e940b2ea88715f67a87b3b0a989f86351060f64651f91d827a65d8928"


class TestDigestSensitivity:
    def test_b8_digest_distinguishes_trailing_decimal_precision(self) -> None:
        # 0.02 vs 0.0200000000000000000001 must hash DIFFERENTLY: the
        # canonical serialization is lossless (Decimal → canonical
        # string), never float.
        base = GuidanceContinuationConfig()
        tweaked = GuidanceContinuationConfig(
            min_midpoint_raise=Decimal("0.020000000000000000001")
        )
        assert config_digest(base) != config_digest(tweaked)

    def test_r3_digest_beyond_28_digits_changes(self) -> None:
        # normalize() rounds at the default context precision of 28; a
        # difference in the 31st digit must still change the digest.
        base = GuidanceContinuationConfig()
        tweaked = GuidanceContinuationConfig(
            min_midpoint_raise=Decimal("0.020000000000000000000000000000001")
        )
        assert config_digest(base) != config_digest(tweaked)

    def test_r3_digest_identical_across_context_precisions(self) -> None:
        # The canonical form must involve NO Decimal arithmetic, so the
        # digest is identical under prec=10/28/40.
        base = GuidanceContinuationConfig()
        tweaked = GuidanceContinuationConfig(
            min_midpoint_raise=Decimal("0.020000000000000000000000000000001")
        )
        d28 = config_digest(base), config_digest(tweaked)
        with localcontext() as ctx:
            ctx.prec = 10
            d10 = config_digest(base), config_digest(tweaked)
        with localcontext() as ctx:
            ctx.prec = 40
            d40 = config_digest(base), config_digest(tweaked)
        assert d28 == d10 == d40

    def test_r3_canonical_decimal_forms(self) -> None:
        from app.domain.guidance_continuation.config import _dec

        assert _dec(Decimal("0.02")) == "0.02"
        assert _dec(Decimal("0.020")) == "0.02"
        assert _dec(Decimal("0.0200")) == "0.02"
        assert _dec(Decimal("2")) == "2"
        assert _dec(Decimal("2.0")) == "2"
        assert _dec(Decimal("2E+1")) == "20"
        assert _dec(Decimal("-0")) == "0"
        assert _dec(Decimal("100000000")) == "100000000"
        assert _dec(
            Decimal("0.020000000000000000000000000000001")
        ) == "0.020000000000000000000000000000001"
        # NaN / Infinity are rejected outright.
        for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            try:
                _dec(bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{bad} must be rejected")

    def test_b8_digest_ignores_insignificant_trailing_zeros(self) -> None:
        # 0.02 == 0.020 numerically → identical canonical form.
        base = GuidanceContinuationConfig()
        padded = GuidanceContinuationConfig(
            min_midpoint_raise=Decimal("0.020")
        )
        assert config_digest(base) == config_digest(padded)

    def test_b8_decimal_serialization_is_canonical_string(self) -> None:
        payload = config_payload()
        # Spot-check that rule constants serialize losslessly.
        assert payload["announcement"]["min_midpoint_raise"] == "0.02"

    def test_b8_negative_age_tolerance_removed(self) -> None:
        # REJECTED interpretation: the 50 ms skew tolerance is gone; the
        # config field must not exist.
        assert not hasattr(
            GuidanceContinuationConfig(), "quote_negative_age_tolerance_seconds"
        )

    def test_b8_tick_min_price_enforced(self) -> None:
        # us_tick_size_min_price is now USED: a price below it is
        # unsupported by this version and must be rejected.
        from app.domain.guidance_continuation.sizing import ceil_to_tick

        try:
            ceil_to_tick(Decimal("0.99"))
        except ValueError:
            pass
        else:
            raise AssertionError("sub-min-price tick rounding must fail")


class TestConfig:
    def test_algorithm_version(self) -> None:
        assert ALGORITHM_VERSION == "earnings-revenue-guidance-continuation-v1"

    def test_digest_matches_frozen_sha256(self) -> None:
        assert config_digest() == _FROZEN_DIGEST

    def test_digest_is_canonical_json_sha256(self) -> None:
        payload = config_payload()
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        assert config_digest() == hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()

    def test_payload_is_json_roundtrippable_and_sorted_stable(self) -> None:
        payload = config_payload()
        again = json.loads(json.dumps(payload))
        assert again == payload
        assert json.dumps(payload) == json.dumps(again)

    def test_default_config_is_singleton_frozen(self) -> None:
        config = GuidanceContinuationConfig()
        assert config == DEFAULT_GUIDANCE_CONFIG

    def test_first_passage_baseline_is_36pct(self) -> None:
        assert first_passage_driftless_baseline() == Decimal("0.36")
        assert (
            first_passage_driftless_baseline()
            == DEFAULT_GUIDANCE_CONFIG.first_passage_stop_pct
            / (
                DEFAULT_GUIDANCE_CONFIG.first_passage_stop_pct
                + DEFAULT_GUIDANCE_CONFIG.first_passage_target_pct
            )
        )

    def test_every_s10_constant_present(self) -> None:
        c = DEFAULT_GUIDANCE_CONFIG
        assert c.universe_indices == ("NASDAQ_100", "DJIA")
        assert c.universe_market == "US"
        assert str(c.tminus1_close_min_usd) == "20"
        assert str(c.tminus1_close_max_usd) == "500"
        assert c.adv_lookback_days == 20
        assert str(c.adv_min_avg_daily_turnover_usd) == "100000000"
        assert c.guidance_metric == "TOTAL_REVENUE"
        assert c.prior_guidance_lookback_days == 120
        assert str(c.min_midpoint_raise) == "0.02"
        assert c.entry_bar_count == 15
        assert str(c.gap_min) == "0.01"
        assert str(c.gap_max) == "0.05"
        assert str(c.min_c15_over_o_gain) == "0.002"
        assert str(c.rvol15_min) == "2.0"
        assert c.rvol_history_days == 20
        assert str(c.quantity_cap_shares) == "100"
        assert str(c.notional_cap_usd) == "25000"
        assert str(c.risk_cap_usd) == "250"
        assert str(c.stop_loss_pct) == "0.0045"
        assert str(c.profit_target_pct) == "0.0080"
        assert c.max_hold_minutes == 60
        assert c.entry_cutoff_minutes_before_close == 45
        assert c.flatten_minutes_before_close == 15
        assert str(c.commission_fixed_usd) == "1.568"
        assert str(c.commission_rate) == "0.0000641"
        assert str(c.execution_deduction_bps_baseline) == "0.031"
        assert str(c.execution_deduction_bps_confirmatory) == "2.031"
        assert c.analysis_min_distinct_days == 20
        assert c.analysis_min_resolved_brackets == 30
        assert c.min_gross_net_observations == 30
        assert c.min_gross_net_distinct_days == 20
        assert c.promotion_min_distinct_days == 60
        assert c.promotion_min_resolved_brackets == 180
        assert c.futility_checkpoint_days == (20, 40, 60, 80)
        assert c.final_traded_days_budget == 100
        assert c.final_calendar_months_budget == 24
        assert str(c.futility_upper_bound_multiplier) == "2.0"
        assert str(c.mde_sigma_day_bps) == "20.0"
