"""Rotation forward-experiment preregistration: parameters and gate are frozen.

Mirrors ``test_strategy_v2_preregistration.py`` for the monthly momentum
rotation family. The forward scorecard accumulates one observation per
month per variant; that evidence is only meaningful against ONE immutable
parameter set and ONE immutable promotion gate. Two failure modes this
test exists to make impossible to do silently:

* Tuning a variant's lookback / skip / SMA / selection count after an
  adverse month, so the next cohort is scored under different rules from
  the ones the evidence clock started under.
* Loosening the scorecard gate (fewer required cohorts, lower excess win
  rate) once three cohorts have landed and the numbers look thin.

The v2 preregistration also records that a single-exit evidence machine
runs forever, because every unsignificant reading is translated into "collect
a little more". The rotation gate today has only the promotion exit; the
decision contract in ROTATION_PREREGISTRATION.md supplies the other one, and
this test pins that the contract file exists and names the same constants
the code enforces, so the document and the machine cannot drift apart.

If this test fails: do NOT update the recorded hash. Restore the parameters,
or write the change into ROTATION_PREREGISTRATION.md as a new version with a
reset evidence clock, and only then re-record.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path

from app.domain.universe_selection import rotation_forward_scorecard as scorecard
from app.domain.universe_selection import rotation_walk_forward as wf

# Recorded at freeze time (2026-09-11). See ROTATION_PREREGISTRATION.md.
_FROZEN_ROTATION_HASH = "f5a882792a2c8fce64758094336a86fd17e4c68ac17053df61127222bf852977"

_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "domain"
    / "universe_selection"
    / "ROTATION_PREREGISTRATION.md"
)


def _frozen_payload() -> dict[str, object]:
    """Everything that, if changed, would reset the evidence clock."""
    tracked = (
        wf.DIVERSIFIED_ROTATION_VARIANT,
        wf.CONCENTRATED_ROTATION_VARIANT,
        wf.DIVERSIFIED_INVERSE_VOLATILITY_VARIANT,
        wf.DIVERSIFIED_SHRINKAGE_ROTATION_VARIANT,
        wf.RETURN_TO_VARIANCE_ROTATION_VARIANT,
    )
    return {
        "forward_tracked_variants": [asdict(v) for v in tracked],
        "walk_forward_variants": [asdict(v) for v in wf.DEFAULT_ROTATION_VARIANTS],
        "benchmarks": list(wf.ROTATION_BENCHMARK_SYMBOLS),
        "gate": {
            "minimum_completed_forward_cohorts": (
                scorecard.MINIMUM_COMPLETED_FORWARD_COHORTS
            ),
            "minimum_excess_win_rate_pct": scorecard.MINIMUM_EXCESS_WIN_RATE_PCT,
        },
    }


def _hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_rotation_parameters_and_gate_are_frozen() -> None:
    actual = _hash(_frozen_payload())
    assert actual == _FROZEN_ROTATION_HASH, (
        "Rotation forward-experiment freeze violated: the tracked variant "
        "parameters or the scorecard gate no longer hash to the recorded "
        "constant.\n"
        f"  recorded: {_FROZEN_ROTATION_HASH}\n"
        f"  actual:   {actual}\n"
        "WHY THIS FAILED: one forward observation per month per variant is "
        "only evidence against ONE immutable parameter set and ONE immutable "
        "gate. Changing either after an adverse month, or loosening the gate "
        "once cohorts have landed, is overfitting with extra steps. Restore "
        "the values, or record a new version in ROTATION_PREREGISTRATION.md "
        "with a reset evidence clock, then re-record the hash."
    )


def test_hash_is_sensitive_to_a_single_parameter() -> None:
    payload = _frozen_payload()
    variants = payload["forward_tracked_variants"]
    assert isinstance(variants, list)
    variants[0]["lookback_bars"] = 251
    assert _hash(payload) != _FROZEN_ROTATION_HASH


def test_hash_is_sensitive_to_the_gate() -> None:
    payload = _frozen_payload()
    gate = payload["gate"]
    assert isinstance(gate, dict)
    gate["minimum_completed_forward_cohorts"] = 2
    assert _hash(payload) != _FROZEN_ROTATION_HASH


def test_contract_document_exists_and_names_the_enforced_constants() -> None:
    assert _CONTRACT.is_file(), (
        f"missing {_CONTRACT.name}: the rotation gate has a promotion exit but "
        "no written decision contract; without one the evidence machine has "
        "a single exit and runs forever"
    )
    text = _CONTRACT.read_text(encoding="utf-8")
    assert str(scorecard.MINIMUM_COMPLETED_FORWARD_COHORTS) in text
    assert re.search(
        rf"\b{int(scorecard.MINIMUM_EXCESS_WIN_RATE_PCT)}\s*%", text
    ), "contract must state the excess win-rate floor the code enforces"
    assert _FROZEN_ROTATION_HASH in text, (
        "contract must record the same frozen hash the test enforces"
    )


def test_contract_defines_a_kill_exit_not_only_a_promotion_exit() -> None:
    text = _CONTRACT.read_text(encoding="utf-8")
    assert re.search(r"弃置|kill|abandon", text, re.IGNORECASE)
    assert "READY_FOR_MANUAL_REVIEW" in text
    assert re.search(r"不等于|does not (mean|imply)|is not proof", text, re.IGNORECASE), (
        "contract must state that READY_FOR_MANUAL_REVIEW is a review "
        "trigger, not a proof of edge"
    )
