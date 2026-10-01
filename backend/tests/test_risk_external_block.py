"""W2 pure core external-safety-block guard on RiskController (Phase2a).

Behavioural contract (Phase2a §W2 core guard API):

* ``ExternalSafetyBlock`` frozen facts + ``ResumeBlockedError``;
* ``raise_external_block`` / ``publish_external_block`` (epoch CAS — an
  old scan can never clear a newer raise) / ``external_block()`` (
  deterministic under multiple sources);
* ``resume()`` raises ``ResumeBlockedError`` WITHOUT touching the pause
  latch; ``resume_eligibility()`` denies with an external reason;
  ``resume_if_pause_reason`` returns False while blocked (original
  generation semantics otherwise preserved);
* ``trading_state()``: kill switch still HALTED; an external block is at
  least REDUCING;
* ``check()`` / ``pause()`` / limits completely unchanged; the empty
  guard path is byte-identical legacy behaviour;
* no DB/network/services imports — pure in-memory state under the
  controller's own lock.

RED: on the pristine bc9ef3c5 source these guard members do not exist;
per the Phase2a contract a NEW pure-type skeleton is permitted before the
behavioural RED, but every assertion below is behavioural (state, values,
exceptions), never import/signature trivia.
"""
from __future__ import annotations

import threading

import pytest

from app.core.risk import RiskController, RiskResult, TradingState

from app.core.risk import ExternalSafetyBlock, ResumeBlockedError


def _guard_available() -> bool:
    return ExternalSafetyBlock is not None


class TestExternalBlockRaisePublish:
    def test_raise_returns_epoch_and_external_block_reports_it(self) -> None:
        if not _guard_available():
            pytest.fail("ExternalSafetyBlock skeleton missing (behavioural RED)")
        risk = RiskController()
        epoch = risk.raise_external_block(
            "passive_recovery", "pending passive recovery verification",
        )
        assert epoch == 1
        block = risk.external_block()
        assert block == ExternalSafetyBlock(
            source="passive_recovery",
            reason="pending passive recovery verification",
            epoch=1,
        )

    def test_second_raise_increments_epoch(self) -> None:
        if not _guard_available():
            pytest.fail("ExternalSafetyBlock skeleton missing (behavioural RED)")
        risk = RiskController()
        first = risk.raise_external_block("passive_recovery", "r1")
        second = risk.raise_external_block("passive_recovery", "r2")
        assert first == 1 and second == 2
        block = risk.external_block()
        assert block is not None
        assert block.reason == "r2"
        assert block.epoch == 2

    def test_publish_clear_on_matching_epoch(self) -> None:
        if not _guard_available():
            pytest.fail("ExternalSafetyBlock skeleton missing (behavioural RED)")
        risk = RiskController()
        epoch = risk.raise_external_block("passive_recovery", "scan hard")
        cleared = risk.publish_external_block(
            "passive_recovery", None, based_on_epoch=epoch,
        )
        assert cleared is True
        assert risk.external_block() is None

    def test_publish_stale_epoch_cannot_clear_newer_raise(self) -> None:
        if not _guard_available():
            pytest.fail("ExternalSafetyBlock skeleton missing (behavioural RED)")
        risk = RiskController()
        stale_epoch = risk.raise_external_block("passive_recovery", "old scan")
        # A newer raise lands while the old scan is in flight.
        risk.raise_external_block("passive_recovery", "NEWER uncertainty")
        cleared = risk.publish_external_block(
            "passive_recovery", None, based_on_epoch=stale_epoch,
        )
        assert cleared is False
        block = risk.external_block()
        assert block is not None
        assert block.reason == "NEWER uncertainty"

    def test_publish_replace_reason_advances(self) -> None:
        if not _guard_available():
            pytest.fail("ExternalSafetyBlock skeleton missing (behavioural RED)")
        risk = RiskController()
        epoch = risk.raise_external_block("passive_recovery", "preliminary")
        replaced = risk.publish_external_block(
            "passive_recovery", "reconciled: HOLDING", based_on_epoch=epoch,
        )
        assert replaced is True
        block = risk.external_block()
        assert block is not None and block.reason == "reconciled: HOLDING"


class TestResumeBlocked:
    def test_resume_raises_without_modifying_pause(self) -> None:
        if not _guard_available():
            pytest.fail("ExternalSafetyBlock skeleton missing (behavioural RED)")
        risk = RiskController()
        risk.pause("owner manual review", auto_resumable=False)
        risk.raise_external_block("passive_recovery", "hard")
        with pytest.raises(ResumeBlockedError):
            risk.resume()
        # The pause latch is INTACT — the block only refuses the resume.
        assert risk.paused is True
        assert risk.pause_reason == "owner manual review"

    def test_resume_eligibility_denied_with_external_reason(self) -> None:
        if not _guard_available():
            pytest.fail("ExternalSafetyBlock skeleton missing (behavioural RED)")
        risk = RiskController()
        risk.raise_external_block("passive_recovery", "hard uncertainty")
        result = risk.resume_eligibility()
        assert isinstance(result, RiskResult)
        assert result.approved is False
        assert "external" in result.reason.lower() or "passive" in (
            result.reason.lower()
        )

    def test_resume_if_pause_reason_blocked_returns_false(self) -> None:
        if not _guard_available():
            pytest.fail("ExternalSafetyBlock skeleton missing (behavioural RED)")
        risk = RiskController()
        risk.pause("transient operational pause", auto_resumable=True)
        generation = risk._safety_generation
        risk.raise_external_block("passive_recovery", "pending scan")
        resumed = risk.resume_if_pause_reason(
            "transient operational pause",
            expected_generation=generation,
        )
        assert resumed is False
        assert risk.paused is True  # pause untouched by the refusal


class TestTradingStateAndLegacyPaths:
    def test_kill_switch_still_wins_over_external_block(self) -> None:
        if not _guard_available():
            pytest.fail("ExternalSafetyBlock skeleton missing (behavioural RED)")
        risk = RiskController()
        risk.raise_external_block("passive_recovery", "hard")
        risk.enable_kill_switch("test")
        assert risk.trading_state() is TradingState.HALTED

    def test_external_block_at_least_reducing_when_active(self) -> None:
        if not _guard_available():
            pytest.fail("ExternalSafetyBlock skeleton missing (behavioural RED)")
        risk = RiskController()
        assert risk.trading_state() is TradingState.ACTIVE
        risk.raise_external_block("passive_recovery", "hard")
        assert risk.trading_state() in {
            TradingState.REDUCING, TradingState.HALTED,
        }
        assert risk.trading_state() is TradingState.REDUCING

    def test_check_pause_limits_unchanged_without_block(self) -> None:
        # The empty-guard path must be byte-identical legacy behaviour.
        risk = RiskController()
        assert risk.check().approved is True
        risk.pause("manual")
        assert risk.check().approved is False
        assert "manual" in risk.check().reason
        risk.resume()
        assert risk.check().approved is True
        assert risk.trading_state() is TradingState.ACTIVE

    def test_pause_and_daily_limits_still_work_with_block(self) -> None:
        if not _guard_available():
            pytest.fail("ExternalSafetyBlock skeleton missing (behavioural RED)")
        risk = RiskController()
        risk.raise_external_block("passive_recovery", "hard")
        # pause()/check() semantics untouched by the guard's presence.
        risk.pause("operational reason")
        result = risk.check()
        assert result.approved is False
        assert "operational reason" in result.reason

    def test_deterministic_external_block_multiple_sources(self) -> None:
        if not _guard_available():
            pytest.fail("ExternalSafetyBlock skeleton missing (behavioural RED)")
        risk = RiskController()
        risk.raise_external_block("zeta_source", "late")
        risk.raise_external_block("alpha_source", "early")
        block = risk.external_block()
        assert block is not None
        # Deterministic: sorted-source selection, not insertion order.
        assert block.source == "alpha_source"


class TestPurity:
    def test_no_io_imports_in_risk_module(self) -> None:
        import app.core.risk as risk_module

        source = open(risk_module.__file__, encoding="utf-8").read()
        for forbidden in (
            "import requests",
            "from app.database",
            "from app.services",
            "sqlalchemy",
            "httpx",
        ):
            assert forbidden not in source, forbidden

    def test_raise_is_thread_serialized(self) -> None:
        if not _guard_available():
            pytest.fail("ExternalSafetyBlock skeleton missing (behavioural RED)")
        risk = RiskController()
        epochs: list[int] = []
        lock = threading.Lock()

        def raise_ten() -> None:
            for _ in range(10):
                epoch = risk.raise_external_block(
                    "passive_recovery", "concurrent",
                )
                with lock:
                    epochs.append(epoch)

        threads = [
            threading.Thread(target=raise_ten) for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert sorted(epochs) == list(range(1, 41))
