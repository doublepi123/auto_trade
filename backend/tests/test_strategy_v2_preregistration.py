"""Param-hash freeze for the Strategy v2 shadow (v5) parameter set.

Implements the mechanical half of
``backend/app/domain/strategy_v2/PREREGISTRATION.md``: the current v5
parameters are frozen as a NEGATIVE CONTROL, and ANY change to them resets
the forward-evidence clock to zero.  This module recomputes a canonical
SHA-256 over the effective v5 parameter set, derived from the authoritative
sources at test time (never from a hard-coded copy), and asserts it equals
the recorded constant below.  A hash mismatch fails CI and forces a
deliberate, written decision.

Hash scope (explicitly enumerated; do NOT extend implicitly):

* Identity / semantics: ``_ALGORITHM_VERSION``,
  ``CAUSAL_ENTRY_FILL_OFFSET_BARS``, evidence universe market context.
* Entry gates (model column defaults on ``StrategyV2ShadowConfig``):
  zscore windows, breach/reclaim/5m zscores, ADX period + max,
  realized-vol window + bounds, ``arm_ttl_bars``; plus ``residual_sigma_min``
  from the ``StrategyV2Config`` domain default (not DB-exposed).
* Bracket / session exits: ``stop_loss_pct`` / ``profit_target_pct`` from the
  service US seeding constants (``_US_DEFAULT_*``; the values the v5 window
  actually ran with), ``max_holding_minutes``, entry cutoff, flatten window,
  ``max_entries_per_day``, ``entry_cooldown_minutes``,
  ``settlement_grace_seconds`` and ``virtual_quantity`` (domain defaults).
* Cost model: ``slippage_bps``, ``estimated_fee_rate_us/hk``,
  ``DEFAULT_EDGE_SAFETY_BUFFER_BPS``, ``_MIN_NET_REWARD_RISK_RATIO``.

Deliberately out of scope: ``enabled`` / ``universe_managed`` /
``opening_momentum_execution_eligible`` (operational toggles that do not
change signal semantics), ``symbol`` (identity), ``updated_at`` (volatile),
challenger constants and review-threshold constants (separate hypotheses and
evaluation policy, not the frozen v5 signal).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields as dataclass_fields
from typing import Any

import pytest

from app.domain.strategy_v2.costs import DEFAULT_EDGE_SAFETY_BUFFER_BPS
from app.domain.strategy_v2.engine import (
    CAUSAL_ENTRY_FILL_OFFSET_BARS,
    StrategyV2Config,
)
from app.models import StrategyV2ShadowConfig
from app.services import strategy_v2_shadow_service as shadow_service

# Recorded at freeze time (2026-08-30).  See PREREGISTRATION.md.
_FROZEN_V5_PARAM_HASH = (
    "f6b76a03dea9ad4b2513bd6e171bc2070db18eee67bd895de0683edde19061db"
)

_DOMAIN_DEFAULTS: dict[str, Any] = {
    field.name: field.default for field in dataclass_fields(StrategyV2Config)
}


def _model_default(column_name: str) -> Any:
    default = StrategyV2ShadowConfig.__table__.columns[column_name].default
    if default is None:
        raise AssertionError(
            f"StrategyV2ShadowConfig.{column_name} lost its column default"
        )
    return default.arg


def _frozen_v5_params() -> dict[str, Any]:
    """Rebuild the effective v5 parameter set from its authoritative sources."""
    return {
        "algorithm_version": shadow_service._ALGORITHM_VERSION,
        "evidence_universe_market": "US",
        "causal_entry_fill_offset_bars": CAUSAL_ENTRY_FILL_OFFSET_BARS,
        "min_net_reward_risk_ratio": shadow_service._MIN_NET_REWARD_RISK_RATIO,
        "edge_safety_buffer_bps": DEFAULT_EDGE_SAFETY_BUFFER_BPS,
        "zscore_window_1m_bars": _model_default("zscore_window_1m_bars"),
        "zscore_window_5m_bars": _model_default("zscore_window_5m_bars"),
        "breach_zscore": _model_default("breach_zscore"),
        "reclaim_zscore": _model_default("reclaim_zscore"),
        "five_minute_zscore_max": _model_default("five_minute_zscore_max"),
        "adx_period": _model_default("adx_period"),
        "max_adx": _model_default("max_adx"),
        "realized_vol_window_bars": _model_default("realized_vol_window_bars"),
        "min_realized_vol": _model_default("min_realized_vol"),
        "max_realized_vol": _model_default("max_realized_vol"),
        "residual_sigma_min": _DOMAIN_DEFAULTS["residual_sigma_min"],
        "arm_ttl_bars": _model_default("arm_ttl_bars"),
        "stop_loss_pct": shadow_service._US_DEFAULT_STOP_LOSS_PCT,
        "profit_target_pct": shadow_service._US_DEFAULT_PROFIT_TARGET_PCT,
        "max_holding_minutes": _model_default("max_holding_minutes"),
        "entry_cutoff_minutes_before_close": _model_default(
            "entry_cutoff_minutes_before_close"
        ),
        "flatten_minutes_before_close": _model_default(
            "flatten_minutes_before_close"
        ),
        "max_entries_per_day": _model_default("max_entries_per_day"),
        "entry_cooldown_minutes": _model_default("entry_cooldown_minutes"),
        "virtual_quantity": _DOMAIN_DEFAULTS["virtual_quantity"],
        "slippage_bps": _model_default("slippage_bps"),
        "settlement_grace_seconds": _DOMAIN_DEFAULTS["settlement_grace_seconds"],
        "estimated_fee_rate_us": _model_default("estimated_fee_rate_us"),
        "estimated_fee_rate_hk": _model_default("estimated_fee_rate_hk"),
    }


def _param_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _freeze_failure_message(actual: str) -> str:
    return (
        "Strategy v2 shadow parameter freeze violated: the effective v5 "
        "parameter set no longer hashes to the recorded constant.\n"
        f"  recorded: {_FROZEN_V5_PARAM_HASH}\n"
        f"  actual:   {actual}\n"
        "WHY THIS FAILED: the forward-shadow evidence (day-clustered net CI, "
        "version-specific first-passage baseline, reach-rate) is only "
        "meaningful against ONE immutable parameter set.  Quietly tuning "
        "parameters until the evidence looks good is overfitting with extra "
        "steps; this test exists to make that impossible to do silently.\n"
        "WHAT THIS MEANS: per backend/app/domain/strategy_v2/"
        "PREREGISTRATION.md, ANY parameter change resets the evidence clock "
        "to zero.  All previously collected shadow days and closed trades "
        "belong to the OLD parameter set and cannot be counted toward the "
        "promotion gates of the NEW one.  The v5 parameters are the frozen "
        "negative control and must keep running untouched.\n"
        "WHAT TO DO: if this change was accidental, revert it.  If it is a "
        "deliberate new hypothesis, register it in writing FIRST (mechanism, "
        "universe, entry/exit barriers, cost model; see PREREGISTRATION.md), "
        "assign a new algorithm_version / config_version, and only then "
        "update the recorded hash and PREREGISTRATION.md in the same commit. "
        "Never update the hash to silence the test."
    )


def test_frozen_v5_parameters_match_recorded_hash() -> None:
    # Given the frozen v5 parameter set derived from its authoritative sources
    # When its canonical hash is recomputed
    actual = _param_hash(_frozen_v5_params())
    # Then it must equal the recorded freeze constant
    assert actual == _FROZEN_V5_PARAM_HASH, _freeze_failure_message(actual)


def test_any_parameter_mutation_changes_the_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the recorded freeze constant
    # When a bracket parameter is mutated at its authoritative source
    monkeypatch.setattr(shadow_service, "_US_DEFAULT_STOP_LOSS_PCT", 0.46)
    # Then the recomputed hash no longer matches the freeze
    assert _param_hash(_frozen_v5_params()) != _FROZEN_V5_PARAM_HASH
