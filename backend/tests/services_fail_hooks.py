"""Fail-injection hook wrappers for the writer-X lifecycle tests.

Imported by tests/test_passive_entry_lifecycle.py via sys.path injection;
kept as a sibling module so the wrappers stay importable in both the RED
(old) and GREEN (new) trees without touching production code.
"""
from __future__ import annotations

from typing import Any


class _FailClaimCommitHooks:
    """claim_submission wins the CAS but the COMMIT raises.

    Simulates the post-CAS commit failure (durability unproven, no broker
    call happened). Matches the facade's ``_CasCommitFailed`` contract by
    re-raising the same exception type the real bundle raises.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def begin_execution(self, ref: Any, token: str) -> Any:
        return self._inner.begin_execution(ref, token)

    def resolve_policy(self, owner: Any, request: Any) -> Any:
        return self._inner.resolve_policy(owner, request)

    def claim_submission(
        self, owner: Any, final_order: Any, cash: Any,
    ) -> bool:
        # Run the real CAS, then force the commit stage to fail.
        from app.services.passive_allocation_service import _CasCommitFailed

        try:
            self._inner.claim_submission(owner, final_order, cash)
        except _CasCommitFailed:
            raise
        raise _CasCommitFailed(
            "submit-right CAS commit failed: injected",
        )

    def record_outcome(self, owner: Any, fact: Any) -> None:
        return self._inner.record_outcome(owner, fact)

    def current_gate_issue(self) -> str | None:
        return self._inner.current_gate_issue()

    def now(self) -> Any:
        return self._inner.now()

    def owner_intent_for(self, mandate_id: int, claim_token: str) -> Any:
        return self._inner.owner_intent_for(mandate_id, claim_token)

    def record_unresolved_reference(
        self,
        reference: str,
        reason: str,
        *,
        broker_order_id: str | None = None,
    ) -> None:
        recorder = getattr(self._inner, "record_unresolved_reference", None)
        if recorder is None:
            return
        return recorder(
            reference, reason, broker_order_id=broker_order_id,
        )


class _FailRecordOutcomeHooks:
    """record_outcome raises for the given outcomes (DB down)."""

    def __init__(self, inner: Any, fail_outcomes: set[str]) -> None:
        self._inner = inner
        self._fail_outcomes = set(fail_outcomes)

    def begin_execution(self, ref: Any, token: str) -> Any:
        return self._inner.begin_execution(ref, token)

    def resolve_policy(self, owner: Any, request: Any) -> Any:
        return self._inner.resolve_policy(owner, request)

    def claim_submission(
        self, owner: Any, final_order: Any, cash: Any,
    ) -> bool:
        return self._inner.claim_submission(owner, final_order, cash)

    def record_outcome(self, owner: Any, fact: Any) -> None:
        if fact.outcome in self._fail_outcomes:
            raise RuntimeError("outcome DB down (injected)")
        return self._inner.record_outcome(owner, fact)

    def current_gate_issue(self) -> str | None:
        return self._inner.current_gate_issue()

    def now(self) -> Any:
        return self._inner.now()

    def owner_intent_for(self, mandate_id: int, claim_token: str) -> Any:
        return self._inner.owner_intent_for(mandate_id, claim_token)

    def record_unresolved_reference(
        self,
        reference: str,
        reason: str,
        *,
        broker_order_id: str | None = None,
    ) -> None:
        recorder = getattr(self._inner, "record_unresolved_reference", None)
        if recorder is None:
            return
        return recorder(
            reference, reason, broker_order_id=broker_order_id,
        )


class _FailOwnerIntentHooks:
    """owner_intent_for propagates a DB error (no catch-to-None)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def begin_execution(self, ref: Any, token: str) -> Any:
        return self._inner.begin_execution(ref, token)

    def resolve_policy(self, owner: Any, request: Any) -> Any:
        return self._inner.resolve_policy(owner, request)

    def claim_submission(
        self, owner: Any, final_order: Any, cash: Any,
    ) -> bool:
        return self._inner.claim_submission(owner, final_order, cash)

    def record_outcome(self, owner: Any, fact: Any) -> None:
        return self._inner.record_outcome(owner, fact)

    def current_gate_issue(self) -> str | None:
        return self._inner.current_gate_issue()

    def now(self) -> Any:
        return self._inner.now()

    def owner_intent_for(self, mandate_id: int, claim_token: str) -> Any:
        raise RuntimeError("intent lookup DB down (injected)")

    def record_unresolved_reference(
        self,
        reference: str,
        reason: str,
        *,
        broker_order_id: str | None = None,
    ) -> None:
        recorder = getattr(self._inner, "record_unresolved_reference", None)
        if recorder is None:
            return
        return recorder(
            reference, reason, broker_order_id=broker_order_id,
        )
