"""Governance pin for the opening-momentum confirmatory preregistration.

Implements the mechanical half of
``backend/app/domain/OPENING_MOMENTUM_PREREGISTRATION.md``: exactly ONE
opening-momentum shadow variant (``INDEX_CATALOG_STOCKS_IN_PLAY_ORB_TOP10_
CHALLENGER``) is registered as a confirmatory (N=1) forward hypothesis.
This module rebuilds the variant descriptor from its authoritative sources
at test time (never from a hard-coded copy) and pins three things:

1. the full ``config_version`` SHA-256 (rebuilt through the service's own
   variant table AND independently recomputed from the pure domain
   functions), plus the non-hashed rule fields that matter;
2. the canonical-JSON SHA-256 of ``INDEX_CANDIDATE_CATALOG`` content;
3. an AST-normalised per-function source pin (``ast.dump`` without
   attributes, so comments and formatting do not matter) over an explicit
   "code manifest" of the functions that implement universe selection,
   ADV, activity ranking, breakout selection, entry, stop/exit and cost
   for this variant, plus the broker candle-conversion path.

Any change to a pinned semantic trips CI and forces a deliberate, written
decision: a new version, a new E, and a documented decision in the doc,
all in the same commit.  Never update a hash to silence this test.

Record-only research: this registration authorises no orders.
``opening_momentum_execution_enabled`` is forced False
(``config.py`` L1133-1143).
"""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.domain.opening_momentum import OpeningMomentumConfig
from app.domain.opening_momentum_policy import opening_execution_config
from app.domain.opening_momentum_universe import (
    opening_momentum_evidence_config_version,
)
from app.domain.universe_selection import (
    CATALOG_SOURCE_VERSION,
    UNIVERSE_ALGORITHM_VERSION,
)
from app.domain.universe_selection.catalog import INDEX_CANDIDATE_CATALOG
from app.models import Base
from app.services import opening_momentum_shadow_service as om_service
from app.services.opening_momentum_shadow_service import (
    OpeningMomentumShadowService,
)

_VARIANT = "INDEX_CATALOG_STOCKS_IN_PLAY_ORB_TOP10_CHALLENGER"
_ALGORITHM_VERSION = (
    "cross-sectional-opening-momentum-v3-preopen-frozen-universe"
    "+forward-only-5m-orb-stocks-in-play-top10-"
    "index-catalog-valid-adv-opening5-turnover-to-prior20d-adv-proxy-"
    "next-minute-open-range-low-stop-cap4-hold60-cost30-"
    "precommitted-20260728-v1"
)
_UNIVERSE_SOURCE = (
    "OPENING_INDEX_CATALOG_FIVE_MINUTE_ORB_STOCKS_IN_PLAY_TOP10"
)

# Recorded at freeze time (2026-09-27).  See OPENING_MOMENTUM_PREREGISTRATION.md.
_FROZEN_CONFIG_VERSION = (
    "44e3c377d37ca27f921eef983a985f8e45667c283d72ddb056aafbe0d026def9"
)
_FROZEN_CATALOG_SHA256 = (
    "3678a732e5e1d527633912424cacfeadf8bc6339899c1b6fec1f8f1c3353b921"
)
_FROZEN_CATALOG_SOURCE_VERSION = (
    "nasdaq-100-2026-07-24_djia-2026-06-29_historical-pit-v9"
)
_FROZEN_SOURCE_PIN = (
    "1a9e1515ba5f4070e3023d7218f0286ccd4f37d68adf565c10c67a94a1c6cecb"
)
_FROZEN_FORWARD_EVIDENCE_START_DATE = date(2026, 7, 28)
_CONFIRMATORY_E = "2026-09-28"

_DOC_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "domain"
    / "OPENING_MOMENTUM_PREREGISTRATION.md"
)

# The code manifest: (display name, path relative to backend/, kind,
# owning class for methods, definition name).  This list is duplicated in
# the doc's "code manifest" section; the doc-agreement test asserts the
# two stay in sync.
_MANIFEST: list[tuple[str, str, str, str | None, str]] = [
    (
        "app.services.opening_momentum_shadow_service"
        ":_INDEX_CATALOG_STOCKS_IN_PLAY_ORB_SPECS",
        "app/services/opening_momentum_shadow_service.py",
        "const",
        None,
        "_INDEX_CATALOG_STOCKS_IN_PLAY_ORB_SPECS",
    ),
    (
        "app.services.opening_momentum_shadow_service"
        ":_INDEX_CATALOG_STOCKS_IN_PLAY_ORB_SOURCE",
        "app/services/opening_momentum_shadow_service.py",
        "const",
        None,
        "_INDEX_CATALOG_STOCKS_IN_PLAY_ORB_SOURCE",
    ),
    (
        "app.services.opening_momentum_shadow_service"
        ":_INDEX_CATALOG_STOCKS_IN_PLAY_ORB_VERSION_SUFFIX",
        "app/services/opening_momentum_shadow_service.py",
        "const",
        None,
        "_INDEX_CATALOG_STOCKS_IN_PLAY_ORB_VERSION_SUFFIX",
    ),
    (
        "app.services.opening_momentum_shadow_service"
        ":_OPENING_RANGE_STOP_MAX_PCT",
        "app/services/opening_momentum_shadow_service.py",
        "const",
        None,
        "_OPENING_RANGE_STOP_MAX_PCT",
    ),
    (
        "app.services.opening_momentum_shadow_service"
        ":_EARLY_BROAD_MINIMUM_COVERAGE",
        "app/services/opening_momentum_shadow_service.py",
        "const",
        None,
        "_EARLY_BROAD_MINIMUM_COVERAGE",
    ),
    (
        "app.services.opening_momentum_shadow_service"
        ":_POST_20260727_FORWARD_EVIDENCE_START_DATE",
        "app/services/opening_momentum_shadow_service.py",
        "const",
        None,
        "_POST_20260727_FORWARD_EVIDENCE_START_DATE",
    ),
    (
        "app.services.opening_momentum_shadow_service"
        "._IndexCatalogStocksInPlayOrbSpec",
        "app/services/opening_momentum_shadow_service.py",
        "class",
        None,
        "_IndexCatalogStocksInPlayOrbSpec",
    ),
    (
        "app.services.opening_momentum_shadow_service"
        "._evidence_config_version",
        "app/services/opening_momentum_shadow_service.py",
        "method",
        "OpeningMomentumShadowService",
        "_evidence_config_version",
    ),
    (
        "app.services.opening_momentum_shadow_service"
        "._variant_identities",
        "app/services/opening_momentum_shadow_service.py",
        "method",
        "OpeningMomentumShadowService",
        "_variant_identities",
    ),
    (
        "app.services.opening_momentum_shadow_service"
        "._latest_universe_selection_run",
        "app/services/opening_momentum_shadow_service.py",
        "method",
        "OpeningMomentumShadowService",
        "_latest_universe_selection_run",
    ),
    (
        "app.services.opening_momentum_shadow_service._universe_variants",
        "app/services/opening_momentum_shadow_service.py",
        "method",
        "OpeningMomentumShadowService",
        "_universe_variants",
    ),
    (
        "app.services.opening_momentum_shadow_service"
        "._candidate_avg_dollar_volume_by_run",
        "app/services/opening_momentum_shadow_service.py",
        "method",
        "OpeningMomentumShadowService",
        "_candidate_avg_dollar_volume_by_run",
    ),
    (
        "app.services.opening_momentum_shadow_service._optional_metric",
        "app/services/opening_momentum_shadow_service.py",
        "func",
        None,
        "_optional_metric",
    ),
    (
        "app.services.opening_momentum_shadow_service"
        "._evaluate_variant_decision",
        "app/services/opening_momentum_shadow_service.py",
        "method",
        "OpeningMomentumShadowService",
        "_evaluate_variant_decision",
    ),
    (
        "app.services.opening_momentum_shadow_service._variant_signal_at",
        "app/services/opening_momentum_shadow_service.py",
        "method",
        "OpeningMomentumShadowService",
        "_variant_signal_at",
    ),
    (
        "app.services.opening_momentum_shadow_service._variant_entry_at",
        "app/services/opening_momentum_shadow_service.py",
        "method",
        "OpeningMomentumShadowService",
        "_variant_entry_at",
    ),
    (
        "app.services.opening_momentum_shadow_service"
        "._variant_decision_due",
        "app/services/opening_momentum_shadow_service.py",
        "method",
        "OpeningMomentumShadowService",
        "_variant_decision_due",
    ),
    (
        "app.services.opening_momentum_shadow_service._observe_variants",
        "app/services/opening_momentum_shadow_service.py",
        "method",
        "OpeningMomentumShadowService",
        "_observe_variants",
    ),
    (
        "app.services.opening_momentum_shadow_service"
        "._opening_range_stop_loss_pct",
        "app/services/opening_momentum_shadow_service.py",
        "method",
        "OpeningMomentumShadowService",
        "_opening_range_stop_loss_pct",
    ),
    (
        "app.services.opening_momentum_shadow_service"
        "._minute_path_complete",
        "app/services/opening_momentum_shadow_service.py",
        "method",
        "OpeningMomentumShadowService",
        "_minute_path_complete",
    ),
    (
        "app.services.opening_momentum_shadow_service._exit_outcome",
        "app/services/opening_momentum_shadow_service.py",
        "method",
        "OpeningMomentumShadowService",
        "_exit_outcome",
    ),
    (
        "app.services.opening_momentum_shadow_service._close_if_due",
        "app/services/opening_momentum_shadow_service.py",
        "method",
        "OpeningMomentumShadowService",
        "_close_if_due",
    ),
    (
        "app.services.opening_momentum_shadow_service._signal_turnover",
        "app/services/opening_momentum_shadow_service.py",
        "method",
        "OpeningMomentumShadowService",
        "_signal_turnover",
    ),
    (
        "app.services.opening_momentum_shadow_service._coerce_candles",
        "app/services/opening_momentum_shadow_service.py",
        "method",
        "OpeningMomentumShadowService",
        "_coerce_candles",
    ),
    (
        "app.services.opening_momentum_shadow_service"
        "._historical_candles_before",
        "app/services/opening_momentum_shadow_service.py",
        "method",
        "OpeningMomentumShadowService",
        "_historical_candles_before",
    ),
    (
        "app.domain.opening_momentum.OpeningMomentumConfig",
        "app/domain/opening_momentum.py",
        "class",
        None,
        "OpeningMomentumConfig",
    ),
    (
        "app.domain.opening_momentum"
        ".evaluate_stocks_in_play_opening_range_breakout",
        "app/domain/opening_momentum.py",
        "func",
        None,
        "evaluate_stocks_in_play_opening_range_breakout",
    ),
    (
        "app.domain.opening_momentum._evaluate_opening_range_breakout",
        "app/domain/opening_momentum.py",
        "func",
        None,
        "_evaluate_opening_range_breakout",
    ),
    (
        "app.domain.opening_momentum._rank_opening_observations",
        "app/domain/opening_momentum.py",
        "func",
        None,
        "_rank_opening_observations",
    ),
    (
        "app.domain.opening_momentum.shadow_round_trip_return_bps",
        "app/domain/opening_momentum.py",
        "func",
        None,
        "shadow_round_trip_return_bps",
    ),
    (
        "app.domain.opening_momentum_universe"
        ".opening_momentum_evidence_config_version",
        "app/domain/opening_momentum_universe.py",
        "func",
        None,
        "opening_momentum_evidence_config_version",
    ),
    (
        "app.domain.opening_momentum_policy.opening_execution_config",
        "app/domain/opening_momentum_policy.py",
        "func",
        None,
        "opening_execution_config",
    ),
    (
        "app.domain.universe_selection.selector.UniverseSelectionConfig",
        "app/domain/universe_selection/selector.py",
        "class",
        None,
        "UniverseSelectionConfig",
    ),
    (
        "app.domain.universe_selection.selector._dollar_volume",
        "app/domain/universe_selection/selector.py",
        "func",
        None,
        "_dollar_volume",
    ),
    (
        "app.domain.universe_selection.selector._candidate_metrics",
        "app/domain/universe_selection/selector.py",
        "func",
        None,
        "_candidate_metrics",
    ),
    (
        "app.domain.universe_selection.catalog:CATALOG_SOURCE_VERSION",
        "app/domain/universe_selection/catalog.py",
        "const",
        None,
        "CATALOG_SOURCE_VERSION",
    ),
    (
        "app.services.universe_selection_service._research_candlesticks",
        "app/services/universe_selection_service.py",
        "func",
        None,
        "_research_candlesticks",
    ),
    (
        "app.core.broker.get_candlesticks",
        "app/core/broker.py",
        "method",
        "BrokerGateway",
        "get_candlesticks",
    ),
    (
        "app.core.broker.get_forward_adjusted_candlesticks",
        "app/core/broker.py",
        "method",
        "BrokerGateway",
        "get_forward_adjusted_candlesticks",
    ),
    (
        "app.core.broker._get_candlesticks_inner",
        "app/core/broker.py",
        "method",
        "BrokerGateway",
        "_get_candlesticks_inner",
    ),
]


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _source_by_path() -> dict[str, str]:
    return {
        relpath: (_backend_root() / relpath).read_text(encoding="utf-8")
        for relpath in sorted({entry[1] for entry in _MANIFEST})
    }


def _find_node(
    tree: ast.Module,
    kind: str,
    owner: str | None,
    name: str,
) -> ast.AST | None:
    if kind == "const":
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name
                for target in stmt.targets
            ):
                return stmt
        return None
    if kind == "class":
        for stmt in tree.body:
            if isinstance(stmt, ast.ClassDef) and stmt.name == name:
                return stmt
        return None
    if kind == "func":
        for stmt in tree.body:
            if isinstance(
                stmt, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and stmt.name == name:
                return stmt
        return None
    if kind == "method":
        for stmt in tree.body:
            if isinstance(stmt, ast.ClassDef) and stmt.name == owner:
                for sub in stmt.body:
                    if isinstance(
                        sub, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ) and sub.name == name:
                        return sub
        return None
    return None


def _manifest_pins(source_by_path: dict[str, str]) -> dict[str, str]:
    pins: dict[str, str] = {}
    for display, relpath, kind, owner, name in _MANIFEST:
        node = _find_node(
            ast.parse(source_by_path[relpath]), kind, owner, name
        )
        assert node is not None, (
            f"code manifest entry missing from source: {display}"
        )
        pins[display] = hashlib.sha256(
            ast.dump(node).encode("utf-8")
        ).hexdigest()
    return pins


def _combined_pin(pins: dict[str, str]) -> str:
    lines = [
        f"{name}={digest}" for name, digest in sorted(pins.items())
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _target_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> om_service._UniverseVariant:
    """Rebuild the target variant from the service's own variant table."""

    monkeypatch.setattr(
        settings,
        "opening_momentum_challenger_enabled",
        True,
    )
    engine: Engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    db = Session(bind=engine)
    try:
        service = OpeningMomentumShadowService(db)
        for identity in service._variant_identities():
            if identity.variant == _VARIANT:
                return identity
        raise AssertionError(
            f"variant {_VARIANT} disappeared from the variant table"
        )
    finally:
        db.close()
        engine.dispose()


def _spec_version(top_n: int) -> str:
    return (
        "forward-only-5m-orb-stocks-in-play-"
        f"top{top_n}-"
        f"{om_service._INDEX_CATALOG_STOCKS_IN_PLAY_ORB_VERSION_SUFFIX}"
    )


def _five_minute_orb_config(
    **overrides: Any,
) -> OpeningMomentumConfig:
    values: dict[str, Any] = {
        "signal_minutes": 5,
        "holding_minutes": 60,
        "minimum_market_return_bps": -10_000.0,
        "minimum_candidate_return_bps": 0.0,
        "minimum_excess_return_bps": 0.0,
        "one_side_fee_rate": 0.0005,
        "one_side_slippage_bps": 10.0,
        "stop_loss_pct": float(
            om_service._OPENING_RANGE_STOP_MAX_PCT,
        ),
    }
    values.update(overrides)
    return replace(
        opening_execution_config(OpeningMomentumConfig()),
        **values,
    )


def _config_version_for(
    config: OpeningMomentumConfig,
    top_n: int,
) -> str:
    inner = f"{config.version_hash()}:{_spec_version(top_n)}:{top_n}"
    return opening_momentum_evidence_config_version(
        inner,
        universe_algorithm_version=UNIVERSE_ALGORITHM_VERSION,
        catalog_source_version=CATALOG_SOURCE_VERSION,
    )


def _catalog_payload() -> list[dict[str, Any]]:
    return [
        {
            "symbol": candidate.symbol,
            "alias": candidate.alias,
            "sector": candidate.sector,
            "memberships": list(candidate.memberships),
        }
        for candidate in INDEX_CANDIDATE_CATALOG
    ]


def _catalog_sha256(payload: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _replace_once(source: str, old: str, new: str) -> str:
    count = source.count(old)
    assert count == 1, (
        f"mutation anchor is not unique ({count} occurrences): {old!r}"
    )
    return source.replace(old, new, 1)


def _freeze_failure_message(actual: str) -> str:
    return (
        "Opening-momentum confirmatory preregistration violated: the "
        "frozen rule no longer matches the recorded constants.\n"
        f"  recorded: {_FROZEN_SOURCE_PIN}\n"
        f"  actual:   {actual}\n"
        "WHY THIS FAILED: the confirmatory test registered in "
        "backend/app/domain/OPENING_MOMENTUM_PREREGISTRATION.md is only "
        "meaningful against ONE immutable rule (config_version, catalog "
        "content, and the implementing code).  Quietly changing any of "
        "them mid-window is overfitting with extra steps; this test "
        "exists to make that impossible to do silently.\n"
        "WHAT THIS MEANS: per the preregistration doc, ANY change to the "
        "registered rule resets the evidence clock to zero - all "
        "collected trades belong to the OLD rule and cannot be counted "
        "toward the confirmatory PASS of the NEW one.\n"
        "WHAT TO DO: if this change was accidental, revert it.  If it is "
        "deliberate, it requires a new version, a new E, and a written "
        "decision recorded in OPENING_MOMENTUM_PREREGISTRATION.md, with "
        "the updated hashes, ALL IN THE SAME COMMIT.  Never update a "
        "hash to silence this test."
    )


def test_target_variant_descriptor_matches_frozen_config_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the variant rebuilt from the service's own variant table
    identity = _target_identity(monkeypatch)
    # Then its identity fields are frozen as registered
    assert identity.algorithm_version == _ALGORITHM_VERSION
    assert identity.universe_source == _UNIVERSE_SOURCE
    assert (
        identity.config_version == _FROZEN_CONFIG_VERSION
    ), _freeze_failure_message(identity.config_version)
    # And the same version is reproducible from the pure domain sources
    recomputed = _config_version_for(
        _five_minute_orb_config(),
        top_n=10,
    )
    assert recomputed == _FROZEN_CONFIG_VERSION, (
        "the config_version no longer reproduces from "
        "opening_execution_config + the five-minute ORB overlay + the "
        "variant spec; the hash inputs have drifted"
    )


def test_frozen_rule_fields_match_preregistration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the rebuilt variant descriptor
    identity = _target_identity(monkeypatch)
    config = identity.decision_config
    # Then every non-hashed field that matters matches the registration
    assert identity.opening_activity_top_n == 10
    assert identity.opening_activity_baseline == "DAILY_ADV_PROXY"
    assert identity.opening_activity_lookback_sessions == 20
    assert identity.minimum_opening_activity_ratio is None
    assert identity.minimum_breakout_depth_bps is None
    assert identity.candidate_selection_mode == (
        "OPENING_ACTIVITY_TOP_N_THEN_BREAKOUT"
    )
    assert identity.minimum_data_coverage == 0.95
    assert identity.opening_range_stop is True
    assert identity.signal_model == "OPENING_RANGE_BREAKOUT"
    assert identity.required_symbols == ()
    assert identity.excluded_symbols == ()
    assert (
        identity.forward_evidence_start_date
        == _FROZEN_FORWARD_EVIDENCE_START_DATE
    )
    assert config.signal_minutes == 5
    assert config.execution_delay_minutes == 1
    assert config.holding_minutes == 60
    assert config.minimum_universe_size == 8
    assert config.one_side_fee_rate == 0.0005
    assert config.one_side_slippage_bps == 10.0
    assert config.round_trip_cost_bps == 30.0
    assert config.stop_loss_pct == 4.0
    # Entry gates are deliberately disabled for this variant
    assert config.minimum_market_return_bps == -10_000.0
    assert config.minimum_candidate_return_bps == 0.0
    assert config.minimum_excess_return_bps == 0.0


def test_index_candidate_catalog_content_is_pinned() -> None:
    # Given the canonical catalog payload (symbol/alias/sector/memberships)
    payload = _catalog_payload()
    # Then its canonical-JSON SHA-256 is pinned
    assert _catalog_sha256(payload) == _FROZEN_CATALOG_SHA256, (
        "INDEX_CANDIDATE_CATALOG content changed: the registered "
        "universe seed is no longer the one frozen at registration"
    )
    assert CATALOG_SOURCE_VERSION == _FROZEN_CATALOG_SOURCE_VERSION
    # And mutating one catalog entry changes the pin
    mutated = [dict(entry) for entry in payload]
    mutated[0]["alias"] = "MUTATED"
    assert _catalog_sha256(mutated) != _FROZEN_CATALOG_SHA256


def test_code_manifest_source_pins_match_frozen_values() -> None:
    # Given the manifest evaluated over the real sources
    pins = _manifest_pins(_source_by_path())
    # Then the combined pin equals the recorded constant
    assert _combined_pin(pins) == _FROZEN_SOURCE_PIN, _freeze_failure_message(
        _combined_pin(pins)
    )
    assert len(pins) == len(_MANIFEST)


def test_mutation_top_n_changes_config_version_and_source_pin() -> None:
    # Given a mutated copy of the service source (top10 -> top11)
    sources = _source_by_path()
    service_key = "app/services/opening_momentum_shadow_service.py"
    sources[service_key] = _replace_once(
        sources[service_key],
        '"INDEX_CATALOG_STOCKS_IN_PLAY_ORB_TOP10_CHALLENGER",\n'
        "        10,",
        '"INDEX_CATALOG_STOCKS_IN_PLAY_ORB_TOP10_CHALLENGER",\n'
        "        11,",
    )
    # Then the source pin trips
    assert (
        _combined_pin(_manifest_pins(sources)) != _FROZEN_SOURCE_PIN
    )
    # And a recomputed config_version with top_n=11 also differs
    assert (
        _config_version_for(_five_minute_orb_config(), top_n=11)
        != _FROZEN_CONFIG_VERSION
    )


def test_mutation_coverage_changes_source_pin_only() -> None:
    # Given a mutated copy of the service source (coverage 0.95 -> 0.90)
    sources = _source_by_path()
    service_key = "app/services/opening_momentum_shadow_service.py"
    sources[service_key] = _replace_once(
        sources[service_key],
        "_EARLY_BROAD_MINIMUM_COVERAGE = 0.95",
        "_EARLY_BROAD_MINIMUM_COVERAGE = 0.90",
    )
    # Then the source pin trips
    assert (
        _combined_pin(_manifest_pins(sources)) != _FROZEN_SOURCE_PIN
    )
    # And the config_version is unchanged: coverage is NOT a hash input,
    # so only the source pin guards it
    assert (
        _config_version_for(_five_minute_orb_config(), top_n=10)
        == _FROZEN_CONFIG_VERSION
    )


def test_mutation_activity_baseline_changes_source_pin() -> None:
    # Given a mutated copy of the service source (ADV proxy baseline
    # swapped inside the index-catalog identity loop)
    sources = _source_by_path()
    service_key = "app/services/opening_momentum_shadow_service.py"
    source = sources[service_key]
    marker = "for spec in _INDEX_CATALOG_STOCKS_IN_PLAY_ORB_SPECS:"
    assert source.count(marker) == 2
    split = source.rindex(marker)
    head, tail = source[:split], source[split:]
    sources[service_key] = head + _replace_once(
        tail,
        'opening_activity_baseline="DAILY_ADV_PROXY"',
        'opening_activity_baseline="DAILY_ADV_PROXY_MUTANT"',
    )
    # Then the source pin trips
    assert (
        _combined_pin(_manifest_pins(sources)) != _FROZEN_SOURCE_PIN
    )


def test_mutation_cost_changes_config_version_and_source_pin() -> None:
    # Given a mutated copy of the service source (slippage 10 -> 11)
    sources = _source_by_path()
    service_key = "app/services/opening_momentum_shadow_service.py"
    sources[service_key] = _replace_once(
        sources[service_key],
        "one_side_slippage_bps=10.0,",
        "one_side_slippage_bps=11.0,",
    )
    # Then the source pin trips
    assert (
        _combined_pin(_manifest_pins(sources)) != _FROZEN_SOURCE_PIN
    )
    # And the cost is a config_version input, so it also changes the hash
    mutated_config = _five_minute_orb_config(
        one_side_slippage_bps=11.0,
    )
    assert mutated_config.round_trip_cost_bps == 32.0
    assert (
        _config_version_for(mutated_config, top_n=10)
        != _FROZEN_CONFIG_VERSION
    )


def test_mutation_exit_body_changes_source_pin() -> None:
    # Given a mutated copy of the service source (exit reason literal)
    sources = _source_by_path()
    service_key = "app/services/opening_momentum_shadow_service.py"
    sources[service_key] = _replace_once(
        sources[service_key],
        'reason="FIXED_HOLD_EXIT",',
        'reason="FIXED_HOLD_EXIT_MUTANT",',
    )
    # Then the source pin trips via the _exit_outcome function body
    pins = _manifest_pins(sources)
    assert pins[
        "app.services.opening_momentum_shadow_service._exit_outcome"
    ] != _manifest_pins(_source_by_path())[
        "app.services.opening_momentum_shadow_service._exit_outcome"
    ]
    assert _combined_pin(pins) != _FROZEN_SOURCE_PIN


def test_preregistration_doc_agrees_with_pinned_contract() -> None:
    # Given the registered doc and the current pins
    doc = _DOC_PATH.read_text(encoding="utf-8")
    pins = _manifest_pins(_source_by_path())
    # Then the doc carries the frozen identities and decision numbers
    assert _FROZEN_CONFIG_VERSION in doc
    assert _ALGORITHM_VERSION in doc
    assert f"E = {_CONFIRMATORY_E}" in doc
    assert "2026-09-28 13:30 UTC" in doc
    for token in ("125", "252", "26"):
        assert token in doc, f"doc lost the sample constant {token}"
    assert "α = 0.05" in doc
    assert "stress_L = L - 20" in doc
    for token in ("60", "90", "+30 bps"):
        assert token in doc, f"doc lost the futility constant {token}"
    # And the doc lists the code manifest with the same names and hashes
    for display, digest in pins.items():
        assert f"{display} = {digest}" in doc, (
            f"doc code manifest is stale for {display}"
        )
    assert f"combined = {_FROZEN_SOURCE_PIN}" in doc
    # And the doc pins the catalog content hash
    assert _FROZEN_CATALOG_SHA256 in doc
