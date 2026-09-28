"""Governance pin for the registered monthly-trend rule
SPY_MONTHLY_SMA10_CASH_V1.

Implements the mechanical half of
``backend/app/domain/SPY_MONTHLY_SMA10_PREREGISTRATION.md``: the frozen
rule constants, doc/code constant agreement, and an AST-normalised
source pin (``ast.dump`` without attributes) over the pure module and
the CLI glue, recorded in the doc's code-manifest section.

Any change to a pinned semantic trips CI and forces a deliberate,
written decision: a new analysis_id suffix, updated hashes, and a
decision record in the doc, all in the same commit.

NEVER update a hash to silence this test.  See the failure message.
"""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from app.cli import spy_monthly_sma10_replay as cli
from app.cli.spy_monthly_sma10_replay import (
    ANALYSIS_ID,
    COMMISSION_FIXED_USD,
    COMMISSION_NOTIONAL_RATE,
    DATA_END,
    DATA_START,
    DISCLOSURE_BENCHMARK,
    INSTRUMENT,
    REPLAY_CLI_VERSION,
    SCORING_MONTHS,
)
from app.core.accounting_fees import (
    SEC98_FIXED_USD,
    SEC98_NOTIONAL_RATE,
)
from app.domain.monthly_trend import sma10

_DOC_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "domain"
    / "SPY_MONTHLY_SMA10_PREREGISTRATION.md"
)
_PURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "domain"
    / "monthly_trend"
    / "sma10.py"
)
_CLI_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "cli"
    / "spy_monthly_sma10_replay.py"
)

_FROZEN_COMBINED_PIN = "f56194f783b8ffeaea7bda811db3d5eaed06978420d04bcae0b810ae680c84ff"

#: Decision 14.7 item 8: the pin covers the WHOLE pure module, the
#: WHOLE CLI, the sealed calendar module, the sealed split evidence and
#: the reused dependencies (ORB helper module + accounting fees) - as
#: FULL-FILE ast.dump hashes, not a function list.
_PINNED_FILES: tuple[tuple[str, Path], ...] = (
    (
        "app.domain.monthly_trend.sma10",
        Path(__file__).resolve().parents[1]
        / "app" / "domain" / "monthly_trend" / "sma10.py",
    ),
    (
        "app.domain.monthly_trend.nyse_calendar",
        Path(__file__).resolve().parents[1]
        / "app" / "domain" / "monthly_trend" / "nyse_calendar.py",
    ),
    (
        "app.cli.spy_monthly_sma10_replay",
        Path(__file__).resolve().parents[1]
        / "app" / "cli" / "spy_monthly_sma10_replay.py",
    ),
    (
        "app.domain.monthly_trend.data.splits.json",
        Path(__file__).resolve().parents[1]
        / "app" / "domain" / "monthly_trend" / "data" / "splits.json",
    ),
    (
        "dep.app.core.accounting_fees",
        Path(__file__).resolve().parents[1]
        / "app" / "core" / "accounting_fees.py",
    ),
    (
        "dep.app.cli.opening_momentum_historical_replay",
        Path(__file__).resolve().parents[1]
        / "app" / "cli" / "opening_momentum_historical_replay.py",
    ),
)


def _file_ast_sha256(path: Path) -> str:
    """Full-file AST hash (ast.dump without attributes).  JSON files
    hash their canonical re-dump."""

    raw = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        canonical = json.dumps(
            json.loads(raw), ensure_ascii=True, sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return hashlib.sha256(
        ast.dump(ast.parse(raw)).encode("utf-8")
    ).hexdigest()


def _manifest_pins() -> dict[str, str]:
    return {
        display: _file_ast_sha256(path)
        for display, path in _PINNED_FILES
    }


def _combined_pin(pins: dict[str, str]) -> str:
    lines = [
        f"{name} = {digest}" for name, digest in sorted(pins.items())
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _freeze_failure_message(actual: str) -> str:
    return (
        "SPY_MONTHLY_SMA10_CASH_V2 registration violated: a pinned "
        "source file no longer matches its recorded hash.\n"
        f"  recorded: {_FROZEN_COMBINED_PIN}\n"
        f"  actual:   {actual}\n"
        "WHY THIS FAILED: the preregistered monthly-trend validation is "
        "only meaningful against ONE immutable implementation.  Quietly "
        "changing the pure module, the CLI, the sealed calendar, the "
        "split evidence or a reused dependency after outcomes are seen "
        "is result-shopping with extra steps.\n"
        "WHAT TO DO: if this change was accidental, revert it. If it "
        "is deliberate, record the written decision in "
        "SPY_MONTHLY_SMA10_PREREGISTRATION.md (motive, disposition), "
        "assign a new analysis_id, and update the doc hashes and this "
        "pin ALL IN THE SAME COMMIT. NEVER update a hash to silence "
        "this test."
    )


# The doc pins every file hash and the combined value.
def _doc_manifest_block() -> str:
    pins = _manifest_pins()
    lines = "\n".join(
        f"{name} = {digest}" for name, digest in sorted(pins.items())
    )
    return lines + f"\ncombined = {_combined_pin(pins)}"


def test_frozen_plan_constants() -> None:
    assert ANALYSIS_ID == "spy-monthly-sma10-cash-v2"
    assert REPLAY_CLI_VERSION == "spy-monthly-sma10-cash-replay-cli-v1"
    assert INSTRUMENT == "SPY.US"
    assert DISCLOSURE_BENCHMARK == "QQQ.US"
    assert DATA_START.isoformat() == "2010-06-01"
    assert DATA_END.isoformat() == "2022-01-31"
    assert cli.WINDOW_START_MONTH == (2012, 1)
    assert cli.WINDOW_END_MONTH == (2021, 12)
    assert cli.WARMUP_MONTHS_START == (2010, 6)
    assert cli.WARMUP_MONTHS_END == (2011, 12)
    assert len(SCORING_MONTHS) == 120
    assert SCORING_MONTHS[0] == (2012, 1)
    assert SCORING_MONTHS[-1] == (2021, 12)


def test_frozen_rule_constants() -> None:
    assert sma10.SIGNAL_LOOKBACK_MONTHS == 10
    assert sma10.SMA10_WINDOW == 10
    assert sma10.INITIAL_CASH_USD.to_eng_string() == "5000"
    assert sma10.MAX_SHARES_PER_ENTRY == 100
    assert sma10.MAX_ENTRY_NOTIONAL_USD.to_eng_string() == "5000"
    assert sma10.BASE_SLIPPAGE_BPS == 5.0
    assert sma10.STRESS_SLIPPAGE_BPS == 15.0
    assert sma10.DIVIDEND_WITHHOLDING_RATE == 0.30
    assert sma10.REQUIRED_MONTHS == 120
    assert sma10.MIN_CASH_MONTHS == 12
    assert sma10.MIN_INVESTED_MONTHS == 60
    assert sma10.CLAIM1_THRESHOLD == 0.0
    assert sma10.CLAIM2_THRESHOLD == -0.001
    assert sma10.CLAIM3_DOWNSIDE_REDUCTION == 0.8
    assert sma10.CLAIM3_THRESHOLD == 0.0
    assert sma10.CLAIM4_THRESHOLD == 0.0
    assert sma10.BOOTSTRAP_CONFIG.block_length == 12
    assert sma10.BOOTSTRAP_CONFIG.resamples == 10_000
    assert sma10.BOOTSTRAP_CONFIG.seed == 20260928


def test_commission_constants_match_accounting_fees() -> None:
    # The pure layer re-declares the SEC98 constants; numeric equality
    # with the core accounting source is pinned so drift is impossible.
    assert sma10.COMMISSION_FIXED_USD == SEC98_FIXED_USD
    assert sma10.COMMISSION_NOTIONAL_RATE == SEC98_NOTIONAL_RATE
    assert COMMISSION_FIXED_USD == SEC98_FIXED_USD
    assert COMMISSION_NOTIONAL_RATE == SEC98_NOTIONAL_RATE
    assert str(SEC98_FIXED_USD) == "1.568"
    assert str(SEC98_NOTIONAL_RATE) == "0.0000641"


def test_verdict_vocabulary() -> None:
    assert {
        sma10.VERDICT_CORROBORATES_RISK_MANAGEMENT_VALUE,
        sma10.VERDICT_DOES_NOT_CORROBORATE,
        sma10.VERDICT_INCONCLUSIVE,
        sma10.VERDICT_INSUFFICIENT_DATA,
        sma10.VERDICT_DATA_BLOCKED,
    } == {
        "CORROBORATES_RISK_MANAGEMENT_VALUE",
        "DOES_NOT_CORROBORATE",
        "INCONCLUSIVE",
        "INSUFFICIENT_DATA",
        "DATA_BLOCKED",
    }
    # Disjoint from the forward contract's PASS vocabulary.
    assert {
        sma10.VERDICT_CORROBORATES_RISK_MANAGEMENT_VALUE,
        sma10.VERDICT_DOES_NOT_CORROBORATE,
        sma10.VERDICT_INCONCLUSIVE,
        sma10.VERDICT_INSUFFICIENT_DATA,
        sma10.VERDICT_DATA_BLOCKED,
    }.isdisjoint({"PASS", "NOT_CERTIFIED"})


# ------------------------------------------------------------- source pins


def test_code_manifest_source_pins_match_frozen_values() -> None:
    pins = _manifest_pins()
    assert _combined_pin(pins) == _FROZEN_COMBINED_PIN, (
        _freeze_failure_message(_combined_pin(pins))
    )


def test_mutation_breaks_the_pin(tmp_path: Path) -> None:
    # A one-character semantic change to the PURE MODULE must trip the
    # full-file hash (guards semantics, not formatting).
    source = _PURE_PATH.read_text(encoding="utf-8")
    marker = "return 1 if current > average else 0"
    assert marker in source
    mutated = source.replace(
        marker, "return 1 if current >= average else 0", 1
    )
    mutated_path = tmp_path / "sma10_mutated.py"
    mutated_path.write_text(mutated, encoding="utf-8")
    original_hash = _file_ast_sha256(_PURE_PATH)
    mutated_hash = _file_ast_sha256(mutated_path)
    assert original_hash != mutated_hash


def test_doc_agrees_with_pinned_constants_and_manifest() -> None:
    doc = _DOC_PATH.read_text(encoding="utf-8")
    assert ANALYSIS_ID in doc
    for token in (
        "SPY_MONTHLY_SMA10_CASH_V1",
        "2012-01",
        "2021-12",
        "120",
        "0.0000641",
        "1.568",
        "5 bps",
        "15 bps",
        "30%",
        "20260928",
        "10,000",
        "223",
        "33 USD",
        "block length 12",
    ):
        assert token in doc, f"doc lost the plan constant {token}"
    for name in (
        sma10.VERDICT_CORROBORATES_RISK_MANAGEMENT_VALUE,
        sma10.VERDICT_DOES_NOT_CORROBORATE,
        sma10.VERDICT_INCONCLUSIVE,
        sma10.VERDICT_INSUFFICIENT_DATA,
        sma10.VERDICT_DATA_BLOCKED,
    ):
        assert f"`{name}`" in doc
    pins = _manifest_pins()
    for display, digest in pins.items():
        assert f"{display} = {digest}" in doc, (
            f"doc code manifest is stale for {display}"
        )
    assert f"combined = {_FROZEN_COMBINED_PIN}" in doc


def test_doc_pins_are_recomputed_not_hardcoded_only() -> None:
    pins = _manifest_pins()
    doc = _DOC_PATH.read_text(encoding="utf-8")
    assert f"combined = {_combined_pin(pins)}" in doc


def test_doc_records_data_contract_and_honesty() -> None:
    doc = _DOC_PATH.read_text(encoding="utf-8")
    for token in (
        "NoAdjust",
        "ForwardAdjust",
        "corporate_actions.json",
        "SPY ∩ QQQ",
        "2010-06",
        "2000/2008 无法测试",
        "发表后 OOS",
        "DATA_BLOCKED",
        "--rerun-reason",
        "import-corporate-actions",
    ):
        assert token in doc, f"doc lost the data-contract token {token}"


def test_doc_records_registered_decisions() -> None:
    # §4.3: every decision where the spec was silent is written down.
    doc = _DOC_PATH.read_text(encoding="utf-8")
    for token in (
        "pay date",
        "除息日",
        "首个交易日开盘",
        "下一个",
        "清算",
        "相等即现金",
        "FLAT",
    ):
        assert token in doc, f"doc lost the registered decision {token}"


def test_registered_actions_source_is_pinned() -> None:
    # §14.6: the SSGA distributions workbook is enforced by its FULL
    # sha256 and its verified URL; the 48-quarter content fact is a
    # registered constant.
    assert cli.REGISTERED_ACTIONS_SOURCE_URL == (
        "https://www.ssga.com/library-content/products/fund-data/"
        "etfs/us/spdr-etf-historical-distributions.xlsx"
    )
    assert cli.REGISTERED_ACTIONS_SOURCE_SHA256 == (
        "51a16a450298a663b3fa088883a17c75f4464e87c911b0e83902a0ad"
        "46877c54"
    )
    assert cli.REGISTERED_SPY_WINDOW_EVENTS == 48
    assert cli.REGISTERED_ACTIONS_WINDOW_START == date(2010, 1, 1)
    assert cli.REGISTERED_ACTIONS_WINDOW_END == date(2021, 12, 31)


def test_registered_decision_14_5_is_recorded_in_doc() -> None:
    doc = _DOC_PATH.read_text(encoding="utf-8")
    assert "14.5" in doc
    for token in (
        "spdr-etf-historical-distributions.xlsx",
        "51a16a45",
        "48",
        "zipfile",
        "openpyxl",
        "price-only",
        "Invesco",
        "拆股完整性屏幕",
        "DATA_BLOCKED",
    ):
        assert token in doc, f"doc lost the §14.5 token {token}"
    # The decision predates any outcome and keeps the analysis_id.
    assert "任何结果之前" in doc
    assert "不分配新的 `analysis_id`" in doc


def test_registered_decision_14_6_is_recorded_in_doc() -> None:
    # Decision 14.6 (orchestrator review, still pre-outcome): the URL
    # is corrected and the registered hash is ENFORCED, not recorded.
    doc = _DOC_PATH.read_text(encoding="utf-8")
    assert "14.6" in doc
    for token in (
        "https://www.ssga.com/library-content/products/fund-data/"
        "etfs/us/spdr-etf-historical-distributions.xlsx",
        "51a16a450298a663b3fa088883a17c75f4464e87c911b0e83902a0ad"
        "46877c54",
        "registered change decision",
        "577,780",
        "每季恰好一条",
        "48",
    ):
        assert token in doc, f"doc lost the §14.6 token {token}"
    # The JSON path is synthetic-tests-only and real runs cannot use it.
    assert "仅限合成测试" in doc
    # The prefix/suffix constants are gone from the code.
    assert not hasattr(cli, "REGISTERED_ACTIONS_SOURCE_SHA256_PREFIX")
    assert not hasattr(cli, "REGISTERED_ACTIONS_SOURCE_SHA256_SUFFIX")


def test_split_discontinuity_constants() -> None:
    assert sma10.SPLIT_ANOMALY_FACTOR_HIGH == 1.5
    assert sma10.SPLIT_ANOMALY_FACTOR_LOW == pytest.approx(2.0 / 3.0)


def test_orb_replay_files_untouched_contract() -> None:
    # This lane IMPORTS the hardened ORB helpers; the ORB module's own
    # pin test still guards it.  Here we only assert the import surface
    # this CLI depends on actually exists (renames there must be a
    # deliberate, coordinated change).
    from app.cli import opening_momentum_historical_replay as orb

    for name in (
        "_Throttle",
        "_RetryableProvider",
        "_page_forward_daily",
        "_atomic_write_json",
        "_file_sha256",
        "_read_gzip_json",
        "_require_clean_worktree",
        "_claim_next_attempt",
        "_write_attempt_receipt",
        "_load_attempt_receipt",
        "_sealed_plan_digest",
        "_verify_sealed_file",
        "_proc_cmdline",
        "is_fetch_window_open",
    ):
        assert hasattr(orb, name), (
            f"ORB replay helper renamed/removed: {name}; this lane's "
            "reuse contract is broken and needs a written decision"
        )


def test_synthentic_purity_no_forbidden_imports() -> None:
    # The pure module must import nothing beyond the standard library.
    tree = ast.parse(_PURE_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in {
                    "math", "random", "dataclasses", "datetime",
                    "decimal", "typing", "__future__",
                }, f"pure module imports {alias.name}"
        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root in {
                "math", "random", "dataclasses", "datetime", "decimal",
                "typing", "__future__",
            }, f"pure module imports from {node.module}"
    # And no wall clock / I/O in the pure module.
    source = _PURE_PATH.read_text(encoding="utf-8")
    assert "datetime.now" not in source
    assert "open(" not in source
    assert "Path(" not in source
