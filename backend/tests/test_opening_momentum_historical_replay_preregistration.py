"""Governance pin for the registered opening-momentum HISTORICAL REPLAY.

Implements the mechanical half of
``backend/app/domain/OPENING_MOMENTUM_HISTORICAL_REPLAY.md``: the registered
retrospective validation of the frozen forward rule
``INDEX_CATALOG_STOCKS_IN_PLAY_ORB_TOP10_CHALLENGER`` (config
``44e3c377...def9``) on 2023-09-01..2026-04-30 under
``analysis_id = opening-momentum-top10-pit-historical-v1``.

This module pins:

1. the frozen plan constants (analysis_id, window, warm-up, ADV windows,
   n/W minimums, the 20 bps stress, gate thresholds, verdict names);
2. agreement between the doc and the code constants;
3. an AST-normalised per-function source pin (``ast.dump`` without
   attributes) over the replay CLI adapter manifest, written into the doc's
   "code manifest" section exactly as the forward contract does.

Any change to a pinned semantic trips CI and forces a deliberate, written
decision: a new analysis_id suffix, updated hashes, and a decision record in
the doc, all in the same commit.  Never update a hash to silence this test.

The FORWARD pin test (``test_opening_momentum_preregistration.py``) is a
separate contract and is not modified here.  Record-only research: this
registration authorises no orders and no forward-cohort changes.
"""

from __future__ import annotations

import ast
import hashlib
from datetime import date
from pathlib import Path

import pytest

from app.cli import opening_momentum_historical_replay as replay
from app.cli.opening_momentum_historical_replay import (
    ANALYSIS_ID,
    ADV_LOOKBACK_BARS,
    ENTRY_OFFSET,
    EXIT_OFFSET,
    FROZEN_CONFIG_VERSION,
    GATE_MEMBER_DATA_MISSING_MAX_SHARE,
    GATE_SESSION_INPUT_COVERAGE,
    GATE_STILL_LISTED_MISSING_MAX_MEMBER_DAYS,
    HOLDING_MINUTES,
    MIN_COMPLETED_BARS,
    MIN_TRADES,
    MIN_WEEKS,
    OPENING_ACTIVITY_TOP_N,
    STRESS_COST_BPS,
    STOP_LOSS_CAP_PCT,
    WARMUP_SESSIONS,
    WINDOW_END,
    WINDOW_START,
    VERDICT_CORROBORATES,
    VERDICT_DOES_NOT_CORROBORATE,
    VERDICT_INCONCLUSIVE,
    frozen_config_version,
)

_DOC_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "domain"
    / "OPENING_MOMENTUM_HISTORICAL_REPLAY.md"
)
_CLI_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "cli"
    / "opening_momentum_historical_replay.py"
)

_FROZEN_COMBINED_PIN = (
    "b2e9eef58025ef4d4259bdb9d2e26a1ffe1e49c76418ecf4c56ff90e09a408aa"
)

# The CLI adapter manifest: (display name, kind, definition name).  The
# "func" kind covers module-level functions; "const" covers module-level
# assignments.  This list is duplicated in the doc's "code manifest"
# section; the doc-agreement test asserts the two stay in sync.
_MANIFEST: list[tuple[str, str, str]] = [
    ("build_plan_payload", "func", "build_plan_payload"),
    ("build_session_observation", "func", "build_session_observation"),
    ("classify_provider_error", "func", "classify_provider_error"),
    ("company_dedupe", "func", "company_dedupe"),
    ("compute_descriptives", "func", "compute_descriptives"),
    ("cross_check_trading_days", "func", "cross_check_trading_days"),
    ("decide_verdict", "func", "decide_verdict"),
    (
        "evaluate_session_decision",
        "func",
        "evaluate_session_decision",
    ),
    ("frozen_config_version", "func", "frozen_config_version"),
    ("frozen_decision_config", "func", "frozen_decision_config"),
    ("is_fetch_window_open", "func", "is_fetch_window_open"),
    ("one_sided_t95", "func", "one_sided_t95"),
    (
        "pit_universe_for_session",
        "func",
        "pit_universe_for_session",
    ),
    ("rebuild_session_adv", "func", "rebuild_session_adv"),
    ("run_evaluate", "func", "run_evaluate"),
    ("run_fetch", "func", "run_fetch"),
    ("run_seal", "func", "run_seal"),
    ("settle_session_exit", "func", "settle_session_exit"),
    (
        "week_clustered_statistic",
        "func",
        "week_clustered_statistic",
    ),
    ("ADV_LOOKBACK_BARS", "const", "ADV_LOOKBACK_BARS"),
    ("ANALYSIS_ID", "const", "ANALYSIS_ID"),
    ("ENTRY_OFFSET", "const", "ENTRY_OFFSET"),
    ("EXIT_OFFSET", "const", "EXIT_OFFSET"),
    ("FROZEN_CONFIG_VERSION", "const", "FROZEN_CONFIG_VERSION"),
    (
        "GATE_MEMBER_DATA_MISSING_MAX_SHARE",
        "const",
        "GATE_MEMBER_DATA_MISSING_MAX_SHARE",
    ),
    (
        "GATE_SESSION_INPUT_COVERAGE",
        "const",
        "GATE_SESSION_INPUT_COVERAGE",
    ),
    (
        "GATE_STILL_LISTED_MISSING_MAX_MEMBER_DAYS",
        "const",
        "GATE_STILL_LISTED_MISSING_MAX_MEMBER_DAYS",
    ),
    ("HOLDING_MINUTES", "const", "HOLDING_MINUTES"),
    ("MIN_COMPLETED_BARS", "const", "MIN_COMPLETED_BARS"),
    ("MIN_TRADES", "const", "MIN_TRADES"),
    ("MIN_WEEKS", "const", "MIN_WEEKS"),
    ("ONE_SIDED_T95_BY_DF", "const", "ONE_SIDED_T95_BY_DF"),
    (
        "OPENING_ACTIVITY_TOP_N",
        "const",
        "OPENING_ACTIVITY_TOP_N",
    ),
    ("STOP_LOSS_CAP_PCT", "const", "STOP_LOSS_CAP_PCT"),
    ("STRESS_COST_BPS", "const", "STRESS_COST_BPS"),
    ("WARMUP_SESSIONS", "const", "WARMUP_SESSIONS"),
    ("WINDOW_END", "const", "WINDOW_END"),
    ("WINDOW_START", "const", "WINDOW_START"),
]


def _display(kind: str, name: str) -> str:
    prefix = (
        "app.cli.opening_momentum_historical_replay."
        if kind == "func"
        else "app.cli.opening_momentum_historical_replay:"
    )
    return f"{prefix}{name}"


def _manifest_pins(source: str) -> dict[str, str]:
    tree = ast.parse(source)
    pins: dict[str, str] = {}
    wanted_funcs = {
        name for _, kind, name in _MANIFEST if kind == "func"
    }
    wanted_consts = {
        name for _, kind, name in _MANIFEST if kind == "const"
    }
    for node in tree.body:
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name in wanted_funcs
        ):
            pins[_display("func", node.name)] = hashlib.sha256(
                ast.dump(node).encode("utf-8")
            ).hexdigest()
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
            )
            for target in targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id in wanted_consts
                ):
                    pins[_display("const", target.id)] = hashlib.sha256(
                        ast.dump(node).encode("utf-8")
                    ).hexdigest()
    assert len(pins) == len(_MANIFEST), (
        f"code manifest entries missing from source: "
        f"{len(_MANIFEST) - len(pins)}"
    )
    return pins


def _combined_pin(pins: dict[str, str]) -> str:
    lines = [
        f"{name} = {digest}" for name, digest in sorted(pins.items())
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _freeze_failure_message(actual: str) -> str:
    return (
        "Historical-replay registration violated: the frozen replay plan "
        "no longer matches the recorded constants.\n"
        f"  recorded: {_FROZEN_COMBINED_PIN}\n"
        f"  actual:   {actual}\n"
        "WHY THIS FAILED: the retrospective validation registered in "
        "backend/app/domain/OPENING_MOMENTUM_HISTORICAL_REPLAY.md is only "
        "meaningful against ONE immutable plan (analysis_id, window, "
        "gates, statistics, and the adapter glue). Quietly changing any "
        "of them after outcomes are seen is result-shopping with extra "
        "steps; this test exists to make that impossible to do silently.\n"
        "WHAT THIS MEANS: per the replay doc, ANY change to the "
        "registered plan invalidates existing results - reruns after the "
        "change are not a first OOS and must be labelled with a new "
        "analysis_id suffix.\n"
        "WHAT TO DO: if this change was accidental, revert it. If it is "
        "deliberate, record the written decision in "
        "OPENING_MOMENTUM_HISTORICAL_REPLAY.md (motive, disposition of "
        "existing artefacts), assign a new analysis_id, and update the "
        "doc hashes and this pin ALL IN THE SAME COMMIT. Never update a "
        "hash to silence this test."
    )


def test_frozen_plan_constants() -> None:
    assert ANALYSIS_ID == "opening-momentum-top10-pit-historical-v1"
    assert WINDOW_START == date(2023, 9, 1)
    assert WINDOW_END == date(2026, 4, 30)
    assert WARMUP_SESSIONS == 21
    assert ADV_LOOKBACK_BARS == 20
    assert MIN_COMPLETED_BARS == 21
    assert OPENING_ACTIVITY_TOP_N == 10
    assert STOP_LOSS_CAP_PCT == 4.0
    assert HOLDING_MINUTES == 60
    assert ENTRY_OFFSET == 6  # the 09:36 ET entry bar
    assert EXIT_OFFSET == 66  # 09:36 + 60 minutes
    assert STRESS_COST_BPS == 20.0
    assert MIN_TRADES == 125
    assert MIN_WEEKS == 26
    assert GATE_SESSION_INPUT_COVERAGE == 0.95
    assert GATE_MEMBER_DATA_MISSING_MAX_SHARE == 0.02
    assert GATE_STILL_LISTED_MISSING_MAX_MEMBER_DAYS == 0
    assert {VERDICT_CORROBORATES, VERDICT_DOES_NOT_CORROBORATE, VERDICT_INCONCLUSIVE} == {
        "CORROBORATES",
        "DOES_NOT_CORROBORATE",
        "INCONCLUSIVE",
    }


def test_frozen_rule_hash_is_the_registered_forward_rule() -> None:
    # The replay may only run against the forward-registered rule.
    assert FROZEN_CONFIG_VERSION == (
        "44e3c377d37ca27f921eef983a985f8e45667c283d72ddb056aafbe0d026def9"
    )
    assert frozen_config_version() == FROZEN_CONFIG_VERSION, (
        "the replay no longer reproduces the registered forward "
        "config_version; the replay must be re-registered before it may "
        "touch any data"
    )


def test_verdict_vocabulary_is_disjoint_from_forward_contract() -> None:
    forward_vocabulary = {
        "PASS",
        "NOT_CERTIFIED",
        "INSUFFICIENT_DATA",
    }
    replay_vocabulary = {
        VERDICT_CORROBORATES,
        VERDICT_DOES_NOT_CORROBORATE,
        VERDICT_INCONCLUSIVE,
    }
    assert replay_vocabulary.isdisjoint(forward_vocabulary)
    # And the forward contract's pinned test file is unchanged: its own
    # suite guards it; here we only assert the doc boundary statement.
    doc = _DOC_PATH.read_text(encoding="utf-8")
    assert "永不" in doc and "PASS" in doc


def test_code_manifest_source_pins_match_frozen_values() -> None:
    source = _CLI_PATH.read_text(encoding="utf-8")
    pins = _manifest_pins(source)
    assert _combined_pin(pins) == _FROZEN_COMBINED_PIN, (
        _freeze_failure_message(_combined_pin(pins))
    )


def test_mutation_breaks_the_pin() -> None:
    # A one-character change to a pinned adapter function must trip the
    # combined pin, proving the pin guards semantics and not formatting.
    source = _CLI_PATH.read_text(encoding="utf-8")
    assert "keep GOOGL and drop GOOG" in source
    mutated = source.replace(
        "keep GOOGL and drop GOOG",
        "keep GOOGL and drop GOOGL",
        1,
    )
    assert mutated != source
    pins = _manifest_pins(mutated)
    assert _combined_pin(pins) != _FROZEN_COMBINED_PIN


def test_doc_agrees_with_pinned_constants_and_manifest() -> None:
    doc = _DOC_PATH.read_text(encoding="utf-8")
    # Identity and window
    assert ANALYSIS_ID in doc
    assert "2023-09-01" in doc
    assert "2026-04-30" in doc
    # Statistical contract numbers
    for token in ("125", "26", "t(0.95", "20 bps", "0.95", "2%"):
        assert token in doc, f"doc lost the plan constant {token}"
    assert "50 bps" in doc or "50bps" in doc
    # Verdict names present and forward names not used as verdicts here
    for name in (
        VERDICT_CORROBORATES,
        VERDICT_DOES_NOT_CORROBORATE,
        VERDICT_INCONCLUSIVE,
    ):
        assert f"`{name}`" in doc
    # The doc lists the code manifest with the same names and hashes
    source = _CLI_PATH.read_text(encoding="utf-8")
    pins = _manifest_pins(source)
    for display, digest in pins.items():
        assert f"{display} = {digest}" in doc, (
            f"doc code manifest is stale for {display}"
        )
    assert f"combined = {_FROZEN_COMBINED_PIN}" in doc
    # The doc pins the frozen forward rule hash it validates
    assert FROZEN_CONFIG_VERSION[:16] in doc
    # No-peeking protocol present
    assert "--rerun-reason" in doc
    assert "superseded" in doc.lower()


def test_doc_pins_are_recomputed_not_hardcoded_only() -> None:
    # The doc's combined pin must equal the recomputed one right now.
    pins = _manifest_pins(_CLI_PATH.read_text(encoding="utf-8"))
    doc = _DOC_PATH.read_text(encoding="utf-8")
    assert f"combined = {_combined_pin(pins)}" in doc


def test_one_sided_t_table_is_monotone_and_anchored() -> None:
    table = replay.ONE_SIDED_T95_BY_DF
    assert len(table) == 150
    assert all(
        table[index] <= table[index - 1]
        for index in range(1, len(table))
    ), "one-sided t critical values must be non-increasing in df"
    # Anchors against the standard one-sided 95% table.
    assert table[0] == pytest.approx(6.3137515, abs=1e-6)
    assert table[4] == pytest.approx(2.0150484, abs=1e-6)
    assert table[29] == pytest.approx(1.6972609, abs=1e-6)
    assert table[119] == pytest.approx(1.6576514, abs=1e-6)
    assert table[149] == pytest.approx(1.6550755, abs=1e-6)
