# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""Round-4 regression tests: gate-p3a review-3 findings 1 and 2 (P0).

Finding 1 (P0): the recovered funded-margin verdict must come ONLY from
a submission event that passes ``_submission_event_matches_order`` for
THIS order row — a same-ID event from another account / wrong symbol /
wrong side / wrong time must never contribute its evidence block, and
the verdict is entry-only (literal BUY on both the row and the event;
SELL / BUY_TO_COVER / SELL_SHORT never qualify). The current enabled
flag is NOT re-evaluated: a historically-marked pending order keeps its
protective retry management after restart even when the flag is OFF.

Finding 2 (P0): decoding of the persisted evidence must fail closed.
Malformed / missing / wrong-shaped / pathologically nested JSON yields
``False`` instead of raising, mismatched events are filtered (id /
symbol / side / time) BEFORE any funded-evidence decoding, and
``RecursionError`` is handled everywhere this recovery path newly
traverses (including the provenance matcher) so a corrupted historical
payload can no longer abort OFF startup recovery.

All tests drive the REAL ``AppRunner._load_pending_orders`` over a
private isolated SQLite database with a fake broker; the positive
controls submit through the REAL quote-trigger path (no manual
``funded_margin_entry`` stamping anywhere).
"""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import datetime, timedelta, timezone

import pytest

from app.models import OrderRecord, TradeEvent
from app.runner import AppRunner
from tests.test_funded_margin_exception_round3 import (
    FP_A,
    FP_B,
    _HarnessBroker,
    _RealPersistHarness,
)

# Pathologically nested payload: json.loads raises RecursionError.
_DEEP_PAYLOAD = '{"a": ' + '[' * 50000


def _marked_payload(fingerprint: str) -> str:
    return json.dumps(
        {
            "broker_identity_fingerprint": fingerprint,
            "funded_margin": {
                "applied": True,
                "limiting_factor": "REQUESTED_QUANTITY",
            },
        }
    )


def _unmarked_payload(fingerprint: str) -> str:
    return json.dumps({"broker_identity_fingerprint": fingerprint})


class _RecoveryLab:
    """Private-DB lab: real submit, crafted historical events, real recovery."""

    def __init__(self, tmpdir: str) -> None:
        self.harness = _RealPersistHarness(tmpdir)

    def close(self) -> None:
        self.harness.close()

    # -- real submission path (positive controls / valid current event) --

    def submit(self, *, flag_on: bool) -> str:
        """REAL quote-trigger submit; returns the broker order id."""
        broker = _HarnessBroker()
        runner = self.harness.build_armed_runner(broker)
        # Bound identity so the persisted payload proves the account,
        # exactly like a production runner with credentials applied.
        runner._broker_identity_fingerprint = FP_A
        if not flag_on:
            runner._trade_svc.funded_margin_enabled = False
        self.harness.drive_quote(runner)
        pending = runner._trade_svc.pending_order_for("NVDA.US")
        assert pending is not None, "the real submit path must submit"
        return str(pending.broker_order_id)

    # -- crafted historical rows/events (adversarial evidence) ----------

    def add_order_row(
        self,
        order_id: str,
        *,
        side: str = "BUY",
        symbol: str = "NVDA.US",
    ) -> datetime:
        created_at = datetime.now(timezone.utc)
        with self.harness._factory() as db:
            db.add(
                OrderRecord(
                    broker_order_id=order_id,
                    symbol=symbol,
                    side=side,
                    quantity=10.0,
                    price=100.0,
                    status="SUBMITTED",
                    created_at=created_at,
                    config_snapshot="{}",
                )
            )
            db.commit()
        return created_at

    def add_event(
        self,
        order_id: str,
        *,
        symbol: str,
        side: str,
        created_at: datetime,
        payload_json: str | None,
    ) -> None:
        with self.harness._factory() as db:
            db.add(
                TradeEvent(
                    event_type="ORDER_SUBMITTED",
                    symbol=symbol,
                    broker_order_id=order_id,
                    side=side,
                    status="SUBMITTED",
                    message="crafted historical event",
                    payload_json=payload_json,
                    created_at=created_at,
                )
            )
            db.commit()

    # -- real startup recovery -------------------------------------------

    def recover(
        self,
        *,
        fingerprint: str = FP_A,
    ) -> tuple[AppRunner, list[str]]:
        from app.core.engine import StrategyParams

        runner = AppRunner()
        runner.broker = _HarnessBroker()
        runner._broker_identity_fingerprint = fingerprint
        runner.engine.params = StrategyParams(
            symbol="NVDA.US",
            market="US",
            buy_low=100.0,
            sell_high=200.0,
        )
        with self.harness._factory() as db:
            issues = runner._load_pending_orders(db)
        return runner, issues

    def durable_snapshot(self) -> tuple[list, list]:
        with self.harness._factory() as db:
            events = [
                (
                    e.id,
                    e.event_type,
                    e.symbol,
                    e.broker_order_id,
                    e.side,
                    e.status,
                    e.payload_json,
                    e.created_at,
                )
                for e in db.query(TradeEvent).order_by(TradeEvent.id).all()
            ]
            orders = [
                (
                    o.id,
                    o.broker_order_id,
                    o.symbol,
                    o.side,
                    o.status,
                    float(o.quantity),
                    float(o.price),
                    o.executed_quantity,
                    o.executed_price,
                )
                for o in db.query(OrderRecord).order_by(OrderRecord.id).all()
            ]
        return events, orders


# Same-ID mismatched submission-event flavors, each carrying an
# ``applied: true`` evidence block that must NEVER mark the order.
_WRONG_EVENT_FLAVORS = {
    "identity": {
        "symbol": "NVDA.US",
        "side": "BUY",
        "age_seconds": 0.0,
        "payload": None,  # filled below (needs FP_B)
    },
    "symbol": {
        "symbol": "AMD.US",
        "side": "BUY",
        "age_seconds": 0.0,
        "payload": None,  # FP_A
    },
    "side_sell": {
        "symbol": "NVDA.US",
        "side": "SELL",
        "age_seconds": 0.0,
        "payload": None,  # FP_A
    },
    "side_buy_to_cover": {
        "symbol": "NVDA.US",
        "side": "BUY_TO_COVER",
        "age_seconds": 0.0,
        "payload": None,  # FP_A
    },
    "stale_time": {
        "symbol": "NVDA.US",
        "side": "BUY",
        "age_seconds": 7200.0,
        "payload": None,  # FP_A
    },
}
_WRONG_EVENT_FLAVORS["identity"]["payload"] = _marked_payload(FP_B)
for _flavor in ("symbol", "side_sell", "side_buy_to_cover", "stale_time"):
    _WRONG_EVENT_FLAVORS[_flavor]["payload"] = _marked_payload(FP_A)


class TestEvidenceBindsToValidatedSubmissionEvent:
    """Review-3 finding 1 (P0): cross-event evidence never contributes."""

    @pytest.fixture(autouse=True)
    def _lab(
        self, tmp_path_factory: pytest.TempPathFactory,
    ) -> Generator[None, None, None]:
        self.lab = _RecoveryLab(
            str(tmp_path_factory.mktemp("fm-r4-bind")),
        )
        yield
        self.lab.close()

    # -- positive controls (real submit -> real recovery) ----------------

    def test_effective_exception_restart_recovers_true(self) -> None:
        order_id = self.lab.submit(flag_on=True)
        runner, issues = self.lab.recover(fingerprint=FP_A)
        assert issues == []
        pending = runner._trade_svc.pending_order_by_broker_id(order_id)
        assert pending is not None
        assert pending.funded_margin_entry is True

    def test_off_restart_recovers_false(self) -> None:
        order_id = self.lab.submit(flag_on=False)
        runner, issues = self.lab.recover(fingerprint=FP_A)
        assert issues == []
        pending = runner._trade_svc.pending_order_by_broker_id(order_id)
        assert pending is not None
        assert pending.funded_margin_entry is False

    # -- same-ID mismatched marked event AFTER the valid current event ---

    @pytest.mark.parametrize("flavor", sorted(_WRONG_EVENT_FLAVORS))
    def test_wrong_event_after_valid_submit_never_marks(
        self, flavor: str,
    ) -> None:
        order_id = self.lab.submit(flag_on=False)
        spec = _WRONG_EVENT_FLAVORS[flavor]
        self.lab.add_event(
            order_id,
            symbol=spec["symbol"],
            side=spec["side"],
            created_at=(
                datetime.now(timezone.utc)
                - timedelta(seconds=spec["age_seconds"])
            ),
            payload_json=spec["payload"],
        )
        runner, issues = self.lab.recover(fingerprint=FP_A)
        assert issues == []
        pending = runner._trade_svc.pending_order_by_broker_id(order_id)
        assert pending is not None
        assert pending.funded_margin_entry is False

    # -- same-ID mismatched marked event BEFORE the valid current event --

    @pytest.mark.parametrize("flavor", sorted(_WRONG_EVENT_FLAVORS))
    def test_wrong_event_before_valid_event_never_marks(
        self, flavor: str,
    ) -> None:
        order_id = f"fm-r4-before-{flavor}"
        row_created = self.lab.add_order_row(order_id)
        spec = _WRONG_EVENT_FLAVORS[flavor]
        self.lab.add_event(
            order_id,
            symbol=spec["symbol"],
            side=spec["side"],
            created_at=(
                row_created - timedelta(seconds=spec["age_seconds"])
            ),
            payload_json=spec["payload"],
        )
        self.lab.add_event(
            order_id,
            symbol="NVDA.US",
            side="BUY",
            created_at=row_created,
            payload_json=_unmarked_payload(FP_A),
        )
        runner, issues = self.lab.recover(fingerprint=FP_A)
        assert issues == []
        pending = runner._trade_svc.pending_order_by_broker_id(order_id)
        assert pending is not None
        assert pending.funded_margin_entry is False

    # -- reduction rows/events never recover the flag ---------------------

    @pytest.mark.parametrize("side", ["SELL", "BUY_TO_COVER", "SELL_SHORT"])
    def test_reduction_rows_never_marked(self, side: str) -> None:
        order_id = f"fm-r4-row-{side.lower()}"
        created = self.lab.add_order_row(order_id, side=side)
        self.lab.add_event(
            order_id,
            symbol="NVDA.US",
            side=side,
            created_at=created,
            payload_json=_marked_payload(FP_A),
        )
        runner, issues = self.lab.recover(fingerprint=FP_A)
        assert issues == []
        pending = runner._trade_svc.pending_order_by_broker_id(order_id)
        assert pending is not None
        assert pending.funded_margin_entry is False

    # -- no valid provenance still fails closed ---------------------------

    def test_no_valid_provenance_fails_closed(self) -> None:
        order_id = "fm-r4-noprovenance"
        created = self.lab.add_order_row(order_id)
        self.lab.add_event(
            order_id,
            symbol="AMD.US",
            side="BUY",
            created_at=created,
            payload_json=_marked_payload(FP_A),
        )
        runner, issues = self.lab.recover(fingerprint=FP_A)
        assert issues, "a row without validated provenance must be reported"
        assert any(
            "submission provenance" in issue for issue in issues
        )
        assert runner._trade_svc.pending_order_by_broker_id(order_id) is None


class TestRecoveryDecodeFailsClosed:
    """Review-3 finding 2 (P0): malformed evidence never breaks recovery."""

    @pytest.fixture(autouse=True)
    def _lab(
        self, tmp_path_factory: pytest.TempPathFactory,
    ) -> Generator[None, None, None]:
        self.lab = _RecoveryLab(
            str(tmp_path_factory.mktemp("fm-r4-decode")),
        )
        yield
        self.lab.close()

    def _recover_row_with_single_event(
        self,
        order_id: str,
        *,
        event_side: str = "BUY",
        payload_json: str | None,
        runner_fingerprint: str,
    ) -> tuple[AppRunner, list[str]]:
        created = self.lab.add_order_row(order_id)
        self.lab.add_event(
            order_id,
            symbol="NVDA.US",
            side=event_side,
            created_at=created,
            payload_json=payload_json,
        )
        return self.lab.recover(fingerprint=runner_fingerprint)

    # -- deep payloads -----------------------------------------------------

    def test_deep_payload_on_mismatched_event_off_recovery_still_succeeds(
        self,
    ) -> None:
        order_id = self.lab.submit(flag_on=False)
        self.lab.add_event(
            order_id,
            symbol="AMD.US",
            side="SELL",
            created_at=datetime.now(timezone.utc),
            payload_json=_DEEP_PAYLOAD,
        )
        runner, issues = self.lab.recover(fingerprint=FP_A)
        assert issues == []
        pending = runner._trade_svc.pending_order_by_broker_id(order_id)
        assert pending is not None
        assert pending.funded_margin_entry is False

    def test_deep_payload_on_matching_event_alongside_valid_event(
        self,
    ) -> None:
        order_id = "fm-r4-deep-alongside"
        created = self.lab.add_order_row(order_id)
        self.lab.add_event(
            order_id,
            symbol="NVDA.US",
            side="BUY",
            created_at=created,
            payload_json=_DEEP_PAYLOAD,
        )
        self.lab.add_event(
            order_id,
            symbol="NVDA.US",
            side="BUY",
            created_at=created,
            payload_json=_unmarked_payload(FP_A),
        )
        runner, issues = self.lab.recover(fingerprint=FP_A)
        assert issues == []
        pending = runner._trade_svc.pending_order_by_broker_id(order_id)
        assert pending is not None
        assert pending.funded_margin_entry is False

    def test_deep_payload_on_matching_event_alone_fails_closed(
        self,
    ) -> None:
        runner, issues = self._recover_row_with_single_event(
            "fm-r4-deep-alone",
            payload_json=_DEEP_PAYLOAD,
            runner_fingerprint=FP_A,
        )
        assert any(
            "submission provenance" in issue for issue in issues
        )
        assert (
            runner._trade_svc.pending_order_by_broker_id(
                "fm-r4-deep-alone",
            )
            is None
        )

    def test_deep_payload_matching_event_runner_unbound_still_recovers(
        self,
    ) -> None:
        """Deep payload on a matching event with UNBOUND runner: no raise.

        Legacy pending representation for an unbound runner is unchanged
        (the order stays represented); the funded verdict itself is
        fail-closed to False because no current identity exists to prove
        the evidence against.
        """
        runner, issues = self._recover_row_with_single_event(
            "fm-r4-deep-unbound",
            payload_json=_DEEP_PAYLOAD,
            runner_fingerprint="",
        )
        assert issues == []
        pending = runner._trade_svc.pending_order_by_broker_id(
            "fm-r4-deep-unbound",
        )
        assert pending is not None
        assert pending.funded_margin_entry is False

    # -- wrong-shaped / malformed payloads on an otherwise valid event ----

    @pytest.mark.parametrize(
        ("marker", "evidence"),
        [
            ("bool-true", True),
            ("string-yes", "yes"),
            ("array", []),
            ("applied-string", {"applied": "true"}),
            ("applied-int", {"applied": 1}),
            ("applied-string-space", {"applied": "true "}),
            ("applied-false", {"applied": False}),
            ("applied-none", {"applied": None}),
            ("empty-dict", {}),
        ],
    )
    def test_wrong_evidence_shape_fails_closed(
        self, marker: str, evidence: object,
    ) -> None:
        order_id = f"fm-r4-shape-{marker}"
        payload = json.dumps(
            {
                "broker_identity_fingerprint": FP_A,
                "funded_margin": evidence,
            },
        )
        runner, issues = self._recover_row_with_single_event(
            order_id,
            payload_json=payload,
            runner_fingerprint=FP_A,
        )
        assert issues == []
        # The row stays represented through its valid provenance; only
        # the verdict fails closed.
        pendings = runner._trade_svc.pending_orders_for("NVDA.US")
        assert len(pendings) == 1
        assert pendings[0].broker_order_id == order_id
        assert pendings[0].funded_margin_entry is False

    @pytest.mark.parametrize(
        ("marker", "raw"),
        [
            ("array", "[]"),
            ("array-items", "[1, 2]"),
            ("int", "123"),
            ("string", '"scalar"'),
            ("null", "null"),
            ("bool", "true"),
            ("empty", ""),
            ("truncated", "{oops"),
            ("missing", None),
        ],
    )
    def test_non_dict_payload_fails_closed(self, marker: str, raw: str | None) -> None:
        order_id = f"fm-r4-nondict-{marker}"
        runner, issues = self._recover_row_with_single_event(
            order_id,
            payload_json=raw,
            runner_fingerprint="",
        )
        assert issues == []
        pending = runner._trade_svc.pending_order_by_broker_id(order_id)
        assert pending is not None
        assert pending.funded_margin_entry is False

    # -- identity binding ---------------------------------------------------

    def test_valid_evidence_with_extra_keys_recovers_true(self) -> None:
        """The real payload shape (limiting_factor) stays a valid block."""
        runner, issues = self._recover_row_with_single_event(
            "fm-r4-shape-extra-keys",
            payload_json=json.dumps(
                {
                    "broker_identity_fingerprint": FP_A,
                    "funded_margin": {
                        "applied": True,
                        "limiting_factor": "REQUESTED_QUANTITY",
                    },
                },
            ),
            runner_fingerprint=FP_A,
        )
        assert issues == []
        pending = runner._trade_svc.pending_order_by_broker_id(
            "fm-r4-shape-extra-keys",
        )
        assert pending is not None
        assert pending.funded_margin_entry is True

    def test_marked_event_with_matching_identity_recovers_true(self) -> None:
        runner, issues = self._recover_row_with_single_event(
            "fm-r4-marked-ok",
            payload_json=_marked_payload(FP_A),
            runner_fingerprint=FP_A,
        )
        assert issues == []
        pending = runner._trade_svc.pending_order_by_broker_id(
            "fm-r4-marked-ok",
        )
        assert pending is not None
        assert pending.funded_margin_entry is True

    def test_marked_event_without_payload_identity_fails_closed_when_bound(
        self,
    ) -> None:
        runner, issues = self._recover_row_with_single_event(
            "fm-r4-marked-noidentity",
            payload_json=json.dumps(
                {"funded_margin": {"applied": True}},
            ),
            runner_fingerprint=FP_A,
        )
        assert any(
            "submission provenance" in issue for issue in issues
        )
        assert (
            runner._trade_svc.pending_order_by_broker_id(
                "fm-r4-marked-noidentity",
            )
            is None
        )

    def test_marked_event_runner_unbound_missing_identity_fails_closed(
        self,
    ) -> None:
        """Unbound runner (credentials not applied yet) never recovers True.

        The contract requires the SAME CURRENT nonempty credential
        identity proven in the matching event's payload; a blank runner
        fingerprint means that proof is impossible. The order itself is
        still represented under the existing (unchanged) rules with the
        verdict fail-closed to False.
        """
        runner, issues = self._recover_row_with_single_event(
            "fm-r4-unbound-missing-identity",
            payload_json=json.dumps(
                {"funded_margin": {"applied": True}},
            ),
            runner_fingerprint="",
        )
        assert issues == []
        pending = runner._trade_svc.pending_order_by_broker_id(
            "fm-r4-unbound-missing-identity",
        )
        assert pending is not None
        assert pending.funded_margin_entry is False

    def test_marked_event_runner_unbound_old_identity_fails_closed(
        self,
    ) -> None:
        """Unbound runner + evidence carrying an OLD account identity.

        Even a well-formed marked block bound to a different historical
        account must not mark the order when the recovering runner has
        no current credential identity to prove it against.
        """
        runner, issues = self._recover_row_with_single_event(
            "fm-r4-unbound-old-identity",
            payload_json=_marked_payload(FP_B),
            runner_fingerprint="",
        )
        assert issues == []
        pending = runner._trade_svc.pending_order_by_broker_id(
            "fm-r4-unbound-old-identity",
        )
        assert pending is not None
        assert pending.funded_margin_entry is False

    def test_marked_event_matching_identity_recovers_true_with_feature_off(
        self,
    ) -> None:
        """Positive: matching CURRENT identity recovers the verdict.

        The recovery never consults the feature-enabled flag — pin that
        explicitly: the recovering runner holds the flag OFF (its
        default before ``_configure_live_safety``), yet a historically
        marked order whose evidence proves the SAME current identity
        still recovers True (protective retry management continues).
        """
        runner, issues = self._recover_row_with_single_event(
            "fm-r4-bound-matching-flag-off",
            payload_json=_marked_payload(FP_A),
            runner_fingerprint=FP_A,
        )
        assert issues == []
        assert runner._trade_svc.funded_margin_enabled is False
        pending = runner._trade_svc.pending_order_by_broker_id(
            "fm-r4-bound-matching-flag-off",
        )
        assert pending is not None
        assert pending.funded_margin_entry is True

    def test_marked_event_mismatched_bound_identity_fails_closed(
        self,
    ) -> None:
        """Bound runner whose ONLY provenance candidate is another account.

        With a nonempty current fingerprint, the per-event identity gate
        of ``_submission_event_matches_order`` rejects the mismatched
        event entirely: no validated provenance exists, so the existing
        representation rules report the row — the marked evidence never
        even reaches decoding.
        """
        runner, issues = self._recover_row_with_single_event(
            "fm-r4-bound-mismatch",
            payload_json=_marked_payload(FP_B),
            runner_fingerprint=FP_A,
        )
        assert any(
            "submission provenance" in issue for issue in issues
        )
        assert (
            runner._trade_svc.pending_order_by_broker_id(
                "fm-r4-bound-mismatch",
            )
            is None
        )


class TestRecoveryMutatesNothingDurable:
    """Recovery reads evidence; it must never rewrite it."""

    @pytest.fixture(autouse=True)
    def _lab(
        self, tmp_path_factory: pytest.TempPathFactory,
    ) -> Generator[None, None, None]:
        self.lab = _RecoveryLab(
            str(tmp_path_factory.mktemp("fm-r4-mutate")),
        )
        yield
        self.lab.close()

    def test_recovery_leaves_events_and_orders_unchanged(self) -> None:
        order_id = self.lab.submit(flag_on=True)
        self.lab.add_event(
            order_id,
            symbol="NVDA.US",
            side="BUY",
            created_at=datetime.now(timezone.utc),
            payload_json=_marked_payload(FP_B),
        )
        before = self.lab.durable_snapshot()
        runner, issues = self.lab.recover(fingerprint=FP_A)
        after = self.lab.durable_snapshot()
        assert issues == []
        assert after == before
        pending = runner._trade_svc.pending_order_by_broker_id(order_id)
        assert pending is not None
        assert pending.funded_margin_entry is True
